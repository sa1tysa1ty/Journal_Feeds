"""Render the scored store into the site payload and an Atom feed."""
from __future__ import annotations

import datetime as dt
import html
from pathlib import Path
from xml.sax.saxutils import escape

from common import DATA, SITE, load_json, load_yaml, save_json

SCORED_PATH = DATA / "scored.json"
FEED_ITEMS = 60
FEED_MIN_BAND = {"must_read", "recommended"}

BAND_LABEL = {"must_read": "必读", "recommended": "推荐", "browse": "浏览"}


def site_id(base_url: str) -> str:
    return base_url.rstrip("/") or "urn:toc-radar"


def build_feed(items: list[dict], base_url: str, title: str) -> str:
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    feed_items = [i for i in items if i["band"] in FEED_MIN_BAND][:FEED_ITEMS]

    parts = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<feed xmlns="http://www.w3.org/2005/Atom">',
        f"  <title>{escape(title)}</title>",
        f"  <id>{escape(site_id(base_url))}/</id>",
        f"  <updated>{now}</updated>",
        f'  <link rel="alternate" type="text/html" href="{escape(base_url)}/"/>',
        f'  <link rel="self" type="application/atom+xml" href="{escape(base_url)}/feed.xml"/>',
        "  <generator>toc-radar</generator>",
    ]

    for item in feed_items:
        authors = "、".join(item.get("authors", [])) or "—"
        stage = "预印(online-first)" if item.get("stage") == "online-first" else ""
        issue_bits = " ".join(x for x in [
            f"Vol. {item['volume']}" if item.get("volume") else "",
            f"No. {item['issue']}" if item.get("issue") else "",
            item.get("page", ""),
        ] if x)
        why_terms = "、".join(
            dict.fromkeys(w["term"] for w in item.get("why", [])
                          if w["kind"] in ("keyword", "author"))
        )

        body = (
            f"<p><strong>{html.escape(item['journal'])}</strong>"
            + (f" · {html.escape(issue_bits)}" if issue_bits else "")
            + (f" · {stage}" if stage else "")
            + f" · {BAND_LABEL[item['band']]} ({item['score']})</p>"
            + f"<p><em>{html.escape(authors)}</em></p>"
            + (f"<p>{html.escape(item['abstract'][:900])}</p>"
               if item.get("abstract") else "<p>(无摘要)</p>")
            + (f"<p>命中:{html.escape(why_terms)}</p>" if why_terms else "")
        )

        published = item.get("published") or dt.date.today().isoformat()
        parts += [
            "  <entry>",
            f"    <title>[{BAND_LABEL[item['band']]}] {escape(item['title'])}</title>",
            f"    <id>urn:doi:{escape(item['doi'])}</id>",
            f'    <link rel="alternate" type="text/html" href="{escape(item["url"])}"/>',
            f"    <updated>{published}T00:00:00Z</updated>",
            f"    <published>{published}T00:00:00Z</published>",
            f"    <author><name>{escape(authors)}</name></author>",
            f"    <category term=\"{escape(item['journal'])}\"/>",
            f"    <content type=\"html\">{escape(body)}</content>",
            "  </entry>",
        ]

    parts.append("</feed>")
    return "\n".join(parts) + "\n"


def main() -> int:
    scored = load_json(SCORED_PATH, [])
    journals_cfg = load_yaml("journals.yaml")
    meta = load_json(DATA / "meta.json", {})

    base_url = (meta.get("base_url") or "").rstrip("/")
    title = "TOC Radar · 政治理论期刊目录订阅"

    # Trim the payload the browser downloads: drop fields the UI never reads.
    slim = []
    for item in scored:
        slim.append({
            "doi": item["doi"],
            "title": item["title"],
            "authors": item.get("authors", []),
            "journal": item["journal"],
            "tier": item["tier"],
            "volume": item.get("volume", ""),
            "issue": item.get("issue", ""),
            "page": item.get("page", ""),
            "published": item.get("published", ""),
            "first_seen": item.get("first_seen", ""),
            "abstract": item.get("abstract", "")[:1200],
            "url": item["url"],
            "stage": item.get("stage", ""),
            "score": item["score"],
            "band": item["band"],
            "why": item.get("why", []),
            "has_abstract": item.get("has_abstract", False),
        })

    payload = {
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "counts": {
            "total": len(slim),
            "must_read": sum(1 for i in slim if i["band"] == "must_read"),
            "recommended": sum(1 for i in slim if i["band"] == "recommended"),
            "browse": sum(1 for i in slim if i["band"] == "browse"),
        },
        "journals": [
            {"name": j["name"], "tier": j["tier"], "issn": j["issn"]}
            for j in journals_cfg["journals"]
        ],
        "last_run": meta.get("last_run", ""),
        "items": slim,
    }

    save_json(SITE / "data.json", payload)
    (SITE / "feed.xml").write_text(build_feed(scored, base_url, title),
                                   encoding="utf-8")

    print(f"[done] site/data.json ({len(slim)} items), site/feed.xml "
          f"({min(len([i for i in scored if i['band'] in FEED_MIN_BAND]), FEED_ITEMS)} entries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
