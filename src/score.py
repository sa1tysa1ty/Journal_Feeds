"""Score harvested items into 必读 / 推荐 / 浏览.

Every score carries a `why` list naming the terms and authors that fired, so the
ranking can be audited and the weights hand-tuned in config/keywords.yaml.

Tier gating (the "分级订阅" part):
    T1  every article enters the store regardless of score
    T2  only articles that hit at least one keyword or watchlist author
    T3  only the month's top-N by score
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from common import DATA, load_json, load_yaml, save_json, save_text

ITEMS_PATH = DATA / "items.json"
SCORED_PATH = DATA / "scored.json"
T3_MONTHLY_TOP_N = 8


def compile_terms(terms: list[str]) -> list[tuple[str, re.Pattern]]:
    """Word-boundary, case-insensitive, diacritic-literal matching."""
    compiled = []
    for term in terms:
        escaped = re.escape(term)
        # allow the phrase to match across any whitespace run
        escaped = escaped.replace(r"\ ", r"\s+")
        compiled.append((term, re.compile(rf"(?<!\w){escaped}(?!\w)", re.IGNORECASE)))
    return compiled


def build_matchers(config: dict) -> dict:
    groups = []
    for group in config.get("keywords", []):
        groups.append({
            "group": group.get("group", ""),
            "weight": float(group.get("weight", 1.0)),
            "terms": compile_terms(group.get("terms", [])),
        })
    penalties = config.get("penalties") or {}
    authors = config.get("authors") or {}
    return {
        "groups": groups,
        "penalty_weight": float(penalties.get("weight", 0.0)),
        "penalty_terms": compile_terms(penalties.get("terms", [])),
        "author_weight": float(authors.get("weight", 0.0)),
        "author_terms": compile_terms(authors.get("names", [])),
    }


def score_item(item: dict, matchers: dict, config: dict) -> dict:
    title = item.get("title", "")
    abstract = item.get("abstract", "")
    author_blob = " ; ".join(item.get("authors", []))

    tier_weights = config["tier_weights"]
    score = float(tier_weights.get(item.get("tier", "T3"), 0.0))
    why: list[dict] = []
    hits = 0

    # Within a group, hits have diminishing returns and a hard cap. Without this
    # a single essay on Marx that says "dialectic", "totality" and "reification"
    # out-scores a genuinely on-topic Althusser article, and 必读 fills up with
    # anything vaguely Hegelian.
    extra_factor = float(config.get("repeat_hit_factor", 0.35))
    cap_factor = float(config.get("group_cap_factor", 2.5))

    for group in matchers["groups"]:
        matched = []
        for term, pattern in group["terms"]:
            in_title = bool(pattern.search(title))
            in_abstract = bool(pattern.search(abstract))
            if in_title or in_abstract:
                matched.append((term, "title" if in_title else "abstract",
                                2.0 if in_title else 1.0))
        if not matched:
            continue
        hits += len(matched)
        # best hit at full value, the rest heavily discounted
        matched.sort(key=lambda m: -m[2])
        raw = group["weight"] * matched[0][2]
        raw += sum(group["weight"] * m[2] * extra_factor for m in matched[1:])
        capped = min(raw, group["weight"] * cap_factor)
        scale = capped / raw if raw else 0.0
        score += capped
        for idx, (term, where, mult) in enumerate(matched):
            share = group["weight"] * mult * (1.0 if idx == 0 else extra_factor) * scale
            why.append({
                "kind": "keyword",
                "term": term,
                "where": where,
                "group": group["group"],
                "points": round(share, 2),
            })

    for name, pattern in matchers["author_terms"]:
        if pattern.search(author_blob):
            hits += 1
            score += matchers["author_weight"]
            why.append({
                "kind": "author",
                "term": name,
                "where": "author",
                "group": "watchlist",
                "points": round(matchers["author_weight"], 2),
            })

    for term, pattern in matchers["penalty_terms"]:
        if pattern.search(title) or pattern.search(abstract):
            score += matchers["penalty_weight"]
            why.append({
                "kind": "penalty",
                "term": term,
                "where": "title/abstract",
                "group": "降权",
                "points": round(matchers["penalty_weight"], 2),
            })

    recency = config.get("recency_bonus") or {}
    published = item.get("published") or ""
    if published:
        try:
            age = (dt.date.today() - dt.date.fromisoformat(published)).days
            if 0 <= age <= int(recency.get("within_days", 0)):
                bonus = float(recency.get("points", 0.0))
                score += bonus
                why.append({
                    "kind": "recency", "term": f"{age} 天内",
                    "where": "date", "group": "时新度",
                    "points": round(bonus, 2),
                })
        except ValueError:
            pass

    # An item with no abstract cannot be judged fairly on content; note it so the
    # front end can show a caveat rather than silently ranking it low.
    thresholds = config["thresholds"]
    if score >= float(thresholds["must_read"]):
        band = "must_read"
    elif score >= float(thresholds["recommended"]):
        band = "recommended"
    else:
        band = "browse"

    scored = dict(item)
    scored["score"] = round(score, 2)
    scored["band"] = band
    scored["why"] = sorted(why, key=lambda w: -abs(w["points"]))
    scored["hits"] = hits
    scored["has_abstract"] = bool(abstract)
    return scored


def apply_tier_gate(scored: list[dict]) -> list[dict]:
    """T1 keeps everything, T2 needs a hit, T3 keeps the month's top N."""
    kept = [s for s in scored if s["tier"] == "T1"]
    kept += [s for s in scored if s["tier"] == "T2" and s["hits"] > 0]

    by_month: dict[str, list[dict]] = {}
    for item in scored:
        if item["tier"] != "T3":
            continue
        month = (item.get("published") or "0000-00")[:7]
        by_month.setdefault(month, []).append(item)
    for month_items in by_month.values():
        month_items.sort(key=lambda s: (-s["score"], s.get("published", "")))
        kept += month_items[:T3_MONTHLY_TOP_N]

    return kept


def histogram(scored: list[dict]) -> None:
    """Print the score distribution so thresholds can be set against real data
    rather than guessed, and write it to a file the workflow commits back."""
    values = sorted(s["score"] for s in scored)
    if not values:
        print("no items")
        save_text(DATA / "score_report.md", "# 分数分布\n\n(无条目)\n")
        return
    n = len(values)
    out: list[str] = []

    def pct(p: float) -> float:
        return values[min(n - 1, int(n * p))]

    out.append(f"n = {n}   min {values[0]:.1f}   max {values[-1]:.1f}")
    out.append("percentile:  " + "  ".join(f"p{int(p*100)}={pct(p):.1f}"
                                           for p in (0.5, 0.75, 0.9, 0.95, 0.99)))
    lo, hi = values[0], values[-1]
    span = (hi - lo) or 1.0
    buckets = [0] * 20
    for v in values:
        buckets[min(19, int((v - lo) / span * 20))] += 1
    peak = max(buckets) or 1
    for i, count in enumerate(buckets):
        edge = lo + span * i / 20
        bar = "#" * int(40 * count / peak)
        out.append(f"  {edge:>7.1f} | {bar} {count}")
    out.append("")
    out.append("建议:必读线取 p95,推荐线取 p75 —— 即每次收获约 5% 进必读。")
    out.append(f"  must_read: {pct(0.95):.1f}")
    out.append(f"  recommended: {pct(0.75):.1f}")

    # per-journal and per-band breakdown, useful for spotting a journal that
    # floods the inbox or one that never scores at all
    by_journal: dict[str, list[float]] = {}
    for item in scored:
        by_journal.setdefault(item["journal"], []).append(item["score"])
    out.append("")
    out.append("| 期刊 | 条目 | 中位分 | 最高分 |")
    out.append("|---|---|---|---|")
    for name, vals in sorted(by_journal.items(), key=lambda kv: -len(kv[1])):
        vals.sort()
        med = vals[len(vals) // 2]
        out.append(f"| {name} | {len(vals)} | {med:.1f} | {vals[-1]:.1f} |")

    text = "\n".join(out)
    print("\n" + text)
    save_text(DATA / "score_report.md", "# 分数分布\n\n```\n"
             + "\n".join(out[:2 + 20]) + "\n```\n\n"
             + "\n".join(out[22:]) + "\n")


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--histogram", action="store_true",
                        help="print the score distribution and suggested thresholds")
    args = parser.parse_args()

    config = load_yaml("keywords.yaml")
    matchers = build_matchers(config)
    items = load_json(ITEMS_PATH, {})
    if not items:
        print("[warn] no harvested items; run harvest.py first")
        save_json(SCORED_PATH, [])
        return 0

    scored = [score_item(item, matchers, config) for item in items.values()]

    if args.histogram:
        histogram(scored)
        return 0

    kept = apply_tier_gate(scored)
    kept.sort(key=lambda s: (-s["score"], s.get("published", "")), reverse=False)
    kept.sort(key=lambda s: (s.get("published", ""), s["score"]), reverse=True)

    save_json(SCORED_PATH, kept)

    bands = {"must_read": 0, "recommended": 0, "browse": 0}
    for item in kept:
        bands[item["band"]] += 1
    dropped = len(scored) - len(kept)
    print(f"[done] scored {len(scored)} items -> kept {len(kept)} "
          f"(dropped {dropped} by tier gate)")
    print(f"       必读 {bands['must_read']} / 推荐 {bands['recommended']} "
          f"/ 浏览 {bands['browse']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
