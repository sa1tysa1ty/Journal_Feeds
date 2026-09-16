"""Weekly digest as a GitHub Issue.

Chosen over SMTP because it needs no credentials at all: the workflow's own
GITHUB_TOKEN can open an issue, GitHub mails the notification to whatever
address the account uses, and the digest stays searchable in the repo.

    python src/digest.py --dry-run     # write digest-preview.md, open nothing
    python src/digest.py               # open the issue (needs GITHUB_TOKEN)

Environment when not a dry run:
    GITHUB_TOKEN        provided automatically by Actions
    GITHUB_REPOSITORY   "owner/repo", also automatic
    SITE_URL            optional, links back to the dashboard
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request

from common import DATA, ROOT, load_json

SCORED_PATH = DATA / "scored.json"
BANDS = ("must_read", "recommended")
LABEL = {"must_read": "必读", "recommended": "推荐"}

# GitHub rejects an issue body over 65536 characters. Degrade in stages rather
# than letting a busy week fail to post at all.
BODY_LIMIT = 60000
MAX_RECOMMENDED = 40


def recent(items: list[dict], days: int) -> list[dict]:
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    out = [i for i in items
           if i.get("band") in BANDS and (i.get("first_seen") or "") >= cutoff]
    out.sort(key=lambda i: (-i["score"], i.get("published", "")))
    return out


def render(items: list[dict], days: int, site_url: str,
           abstracts: bool = True, cap_recommended: int | None = None) -> str:
    groups: dict[str, list[dict]] = {b: [] for b in BANDS}
    for item in items:
        groups[item["band"]].append(item)
    totals = {b: len(groups[b]) for b in BANDS}   # count before any trimming
    trimmed = 0
    if cap_recommended is not None and len(groups["recommended"]) > cap_recommended:
        trimmed = len(groups["recommended"]) - cap_recommended
        groups["recommended"] = groups["recommended"][:cap_recommended]

    lines = [
        f"过去 {days} 天新增 **{len(items)}** 条"
        f"(必读 {totals['must_read']} · 推荐 {totals['recommended']})。",
        "",
    ]
    if site_url:
        lines += [f"[完整看板]({site_url}) · [Atom feed]({site_url.rstrip('/')}/feed.xml)", ""]

    if not items:
        lines.append("本周没有新的必读或推荐条目。")
        return "\n".join(lines)

    for band in BANDS:
        if not groups[band]:
            continue
        lines += [f"## {LABEL[band]}", ""]
        for item in groups[band]:
            authors = "、".join(item.get("authors", [])) or "—"
            bits = " · ".join(x for x in [
                item["journal"],
                f"Vol. {item['volume']}" if item.get("volume") else "",
                f"No. {item['issue']}" if item.get("issue") else "",
                item.get("published", ""),
            ] if x)
            stage = " `预印`" if item.get("stage") == "online-first" else ""
            terms = "、".join(dict.fromkeys(
                w["term"] for w in item.get("why", [])
                if w["kind"] in ("keyword", "author") and w["points"] > 0))[:150]

            lines.append(f"### [{item['title']}]({item['url']}){stage}")
            lines.append(f"*{authors}*")
            lines.append("")
            lines.append(f"{bits} · **{item['score']}**")
            if abstracts and item.get("abstract"):
                lines.append("")
                lines.append("<details><summary>摘要</summary>")
                lines.append("")
                lines.append(item["abstract"][:1500])
                lines.append("")
                lines.append("</details>")
            if terms:
                lines.append("")
                lines.append(f"<sub>命中:{terms}</sub>")
            lines.append("")
        if band == "recommended" and trimmed:
            lines.append(f"<sub>另有 {trimmed} 条推荐未列出,见看板。</sub>")
            lines.append("")
    return "\n".join(lines)


def api(path: str, token: str, payload: dict | None = None) -> dict | None:
    url = f"https://api.github.com{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "toc-radar")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        print(f"[error] {path}: HTTP {exc.code} {exc.read().decode()[:300]}",
              file=sys.stderr)
    except OSError as exc:
        print(f"[error] {path}: {exc}", file=sys.stderr)
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    items = recent(load_json(SCORED_PATH, []), args.days)
    site_url = os.environ.get("SITE_URL", "")
    body = render(items, args.days, site_url)
    # Stage 1: cap the recommended list. Stage 2: drop abstracts too.
    if len(body) > BODY_LIMIT:
        body = render(items, args.days, site_url, cap_recommended=MAX_RECOMMENDED)
    if len(body) > BODY_LIMIT:
        body = render(items, args.days, site_url, abstracts=False,
                      cap_recommended=MAX_RECOMMENDED)
    if len(body) > BODY_LIMIT:
        body = body[:BODY_LIMIT] + "\n\n<sub>(内容过长已截断,完整列表见看板。)</sub>"
    counts = {b: sum(1 for i in items if i["band"] == b) for b in BANDS}
    title = (f"TOC Radar · {dt.date.today().isoformat()} · "
             f"必读 {counts['must_read']} / 推荐 {counts['recommended']}")

    if args.dry_run:
        out = ROOT / "digest-preview.md"
        out.write_text(f"# {title}\n\n{body}\n", encoding="utf-8")
        print(f"[dry-run] {len(items)} items -> {out}")
        return 0

    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        print("[skip] GITHUB_TOKEN / GITHUB_REPOSITORY not set", file=sys.stderr)
        return 0
    if not items:
        print("[skip] nothing new this week; not opening an issue")
        return 0

    # Assign to the repo owner so the notification arrives even for an account
    # whose watching is set to "participating and @mentions" only.
    owner = repo.split("/")[0]
    payload = {"title": title, "body": body, "assignees": [owner]}
    result = api(f"/repos/{repo}/issues", token, payload)
    if result is None:
        # assignee can fail on some account setups; retry without it rather
        # than losing the digest
        print("[warn] retrying without assignee", file=sys.stderr)
        result = api(f"/repos/{repo}/issues", token,
                     {"title": title, "body": body})
    if result is None:
        return 1
    print(f"[done] opened issue #{result['number']}: {result['html_url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
