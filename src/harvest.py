"""Harvest journal TOCs from Crossref.

Incremental: uses `from-index-date` so each run only pulls what Crossref has
newly indexed. Maintains an online-first -> in-issue state machine keyed by DOI
so an article that first appears as an online-first preprint and later gains a
volume/issue is not announced twice.

Usage:
    python src/harvest.py                 # incremental
    python src/harvest.py --days 90       # widen the window (first run)
    python src/harvest.py --validate      # only check that ISSNs resolve
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

from common import (CONTACT_EMAIL, DATA, clean_abstract, date_parts_to_iso,
                    first, get_json, load_json, load_yaml, save_json, save_text)

CROSSREF = "https://api.crossref.org"
ITEMS_PATH = DATA / "items.json"
STATE_PATH = DATA / "state.json"
SELECT = ",".join([
    "DOI", "title", "author", "container-title", "volume", "issue", "page",
    "issued", "created", "published", "published-online", "published-print",
    "abstract", "URL", "type", "ISSN", "subtitle",
])
SKIP_TYPES = {"journal-issue", "journal-volume", "journal"}


def validate_issn(issn: str) -> str | None:
    """Return the journal title Crossref has for this ISSN, or None."""
    payload = get_json(f"{CROSSREF}/journals/{issn}", params={"mailto": CONTACT_EMAIL})
    if not payload or payload.get("status") != "ok":
        return None
    return (payload.get("message") or {}).get("title")


def search_journal(title: str, limit: int = 3) -> list[tuple[str, list[str]]]:
    """Look a journal up by name so a wrong ISSN can be corrected rather than
    just reported. Returns [(title, [issn, ...]), ...]."""
    payload = get_json(f"{CROSSREF}/journals",
                       params={"query": title, "rows": limit, "mailto": CONTACT_EMAIL})
    if not payload or payload.get("status") != "ok":
        return []
    out = []
    for item in (payload.get("message") or {}).get("items", []):
        issns = [i for i in (item.get("ISSN") or []) if i]
        if issns:
            out.append((item.get("title") or "?", issns))
    return out


def fetch_journal(issn: str, since: str, cap: int = 2000) -> list[dict]:
    """Deep-page every work indexed for this ISSN since `since`."""
    out: list[dict] = []
    cursor = "*"
    while len(out) < cap:
        payload = get_json(
            f"{CROSSREF}/journals/{issn}/works",
            params={
                "filter": f"from-index-date:{since},type:journal-article",
                "rows": 100,
                "cursor": cursor,
                "select": SELECT,
                "mailto": CONTACT_EMAIL,
            },
        )
        if not payload:
            break
        message = payload.get("message") or {}
        batch = message.get("items") or []
        if not batch:
            break
        out.extend(batch)
        next_cursor = message.get("next-cursor")
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
        time.sleep(0.4)  # be polite even in the polite pool
    return out


def normalise(work: dict, journal: dict) -> dict | None:
    doi = (work.get("DOI") or "").lower().strip()
    title = first(work.get("title")).strip()
    if not doi or not title:
        return None
    if (work.get("type") or "") in SKIP_TYPES:
        return None

    authors = []
    for person in work.get("author") or []:
        name = " ".join(p for p in [person.get("given"), person.get("family")] if p)
        if name:
            authors.append(name.strip())

    volume = str(work.get("volume") or "").strip()
    issue = str(work.get("issue") or "").strip()

    # Prefer the print/issue date, fall back to online, then to indexing date.
    published = (date_parts_to_iso(work.get("published-print"))
                 or date_parts_to_iso(work.get("issued"))
                 or date_parts_to_iso(work.get("published"))
                 or date_parts_to_iso(work.get("published-online"))
                 or date_parts_to_iso(work.get("created")))

    subtitle = first(work.get("subtitle")).strip()
    if subtitle and subtitle.lower() not in title.lower():
        title = f"{title}: {subtitle}"

    return {
        "doi": doi,
        "title": title,
        "authors": authors,
        "journal": journal["name"],
        "issn": journal["issn"],
        "tier": journal["tier"],
        "volume": volume,
        "issue": issue,
        "page": str(work.get("page") or "").strip(),
        "published": published,
        "abstract": clean_abstract(work.get("abstract")),
        "url": work.get("URL") or f"https://doi.org/{doi}",
        # in-issue once Crossref reports a volume or an issue number
        "stage": "in-issue" if (volume or issue) else "online-first",
    }


def merge(existing: dict, incoming: dict, state: dict) -> tuple[dict, str | None]:
    """Merge a freshly fetched record into what we already stored.

    Returns (record, event) where event is 'new', 'promoted' or None.
    """
    doi = incoming["doi"]
    today = dt.date.today().isoformat()

    if doi not in existing:
        incoming["first_seen"] = today
        incoming["last_seen"] = today
        incoming["announced"] = False
        state[doi] = incoming["stage"]
        return incoming, "new"

    record = dict(existing[doi])
    prior_stage = state.get(doi, record.get("stage", "online-first"))
    event = None

    # Never regress: once in-issue, always in-issue.
    if prior_stage == "in-issue":
        incoming["stage"] = "in-issue"
        for field in ("volume", "issue", "page"):
            if not incoming[field] and record.get(field):
                incoming[field] = record[field]
    elif incoming["stage"] == "in-issue":
        event = "promoted"

    # Keep an abstract we already have if the new payload dropped it.
    if not incoming["abstract"] and record.get("abstract"):
        incoming["abstract"] = record["abstract"]

    incoming["first_seen"] = record.get("first_seen", today)
    incoming["last_seen"] = today
    incoming["announced"] = record.get("announced", False)
    state[doi] = incoming["stage"]
    return incoming, event


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=None,
                        help="look-back window in days (default: since last run, min 14)")
    parser.add_argument("--validate", action="store_true",
                        help="only verify that every configured ISSN resolves")
    args = parser.parse_args()

    config = load_yaml("journals.yaml")
    journals = config["journals"]

    if args.validate:
        bad, rows = [], []
        for journal in journals:
            title = validate_issn(journal["issn"])
            mark = "ok  " if title else "FAIL"
            print(f"{mark} {journal['issn']}  {journal['name']}"
                  + (f"  ->  {title}" if title else ""))
            rows.append((bool(title), journal, title))
            if not title:
                journal = dict(journal)
                journal["candidates"] = search_journal(journal["name"])
                for cand_title, issns in journal["candidates"]:
                    print(f"      candidate: {cand_title}  ->  {', '.join(issns)}")
                bad.append(journal)
            time.sleep(0.3)

        # Actions log text is not always retrievable, so the result is also
        # written to a file the workflow commits back to the repo.
        lines = [
            "# ISSN 校验报告", "",
            f"运行于 {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M UTC}",
            f"共 {len(journals)} 刊,解析成功 {len(journals) - len(bad)},失败 {len(bad)}。",
            "", "| 状态 | 层级 | ISSN | 配置中的刊名 | Crossref 返回的刊名 |",
            "|---|---|---|---|---|",
        ]
        for ok, journal, title in rows:
            lines.append(
                f"| {'ok' if ok else '**FAIL**'} | {journal['tier']} | "
                f"`{journal['issn']}` | {journal['name']} | {title or '—'} |")
        if bad:
            lines += ["", "## 解析失败", ""]
            for j in bad:
                lines.append(f"- **{j['name']}** (`{j['issn']}`, {j['tier']})"
                             + (f" — {j['note']}" if j.get("note") else ""))
                cands = j.get("candidates") or []
                if cands:
                    lines.append("  - Crossref 按刊名反查到的候选:")
                    for cand_title, issns in cands:
                        lines.append(f"    - {cand_title} — `{'`, `'.join(issns)}`")
                else:
                    lines.append("  - 按刊名也查不到,该刊很可能不在 Crossref。")
        save_text(DATA / "issn_report.md", "\n".join(lines) + "\n")
        print(f"\n[done] report -> data/issn_report.md")

        if bad:
            print(f"{len(bad)} ISSN(s) did not resolve:", file=sys.stderr)
            for journal in bad:
                print(f"  - {journal['name']} ({journal['issn']})", file=sys.stderr)
        return 0

    items: dict = load_json(ITEMS_PATH, {})
    state: dict = load_json(STATE_PATH, {})
    meta = load_json(DATA / "meta.json", {})

    if args.days is not None:
        window = args.days
    else:
        last_run = meta.get("last_run")
        if last_run:
            try:
                delta = (dt.date.today() - dt.date.fromisoformat(last_run)).days
                window = max(delta + 7, 14)  # overlap so nothing slips through
            except ValueError:
                window = 30
        else:
            window = 120  # first run
    since = (dt.date.today() - dt.timedelta(days=window)).isoformat()
    print(f"[info] harvesting {len(journals)} journals, from-index-date:{since}")

    stats = {"new": 0, "promoted": 0, "seen": 0, "failed_journals": []}

    for journal in journals:
        works = fetch_journal(journal["issn"], since)
        if not works:
            print(f"[warn] no results for {journal['name']} ({journal['issn']})")
            stats["failed_journals"].append(journal["name"])
            continue
        added = promoted = 0
        for work in works:
            record = normalise(work, journal)
            if record is None:
                continue
            merged, event = merge(items, record, state)
            items[merged["doi"]] = merged
            stats["seen"] += 1
            if event == "new":
                added += 1
            elif event == "promoted":
                promoted += 1
        stats["new"] += added
        stats["promoted"] += promoted
        print(f"[ok] {journal['name']}: {len(works)} works, "
              f"{added} new, {promoted} promoted to in-issue")

    save_json(ITEMS_PATH, items)
    save_json(STATE_PATH, state)
    meta["last_run"] = dt.date.today().isoformat()
    meta["last_stats"] = stats
    meta["total_items"] = len(items)
    save_json(DATA / "meta.json", meta)

    print(f"\n[done] {stats['new']} new, {stats['promoted']} promoted, "
          f"{len(items)} total in store")
    if stats["failed_journals"]:
        print(f"[note] no data from: {', '.join(stats['failed_journals'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
