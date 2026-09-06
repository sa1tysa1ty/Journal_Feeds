"""Weekly digest email.

Sends everything first seen in the last N days that reached 必读 or 推荐.
SMTP settings come from environment (GitHub Actions secrets):

    SMTP_HOST      e.g. smtp.gmail.com  /  smtp.resend.com
    SMTP_PORT      587 (STARTTLS) or 465 (SSL)
    SMTP_USER
    SMTP_PASS
    MAIL_FROM
    MAIL_TO
    SITE_URL       optional, links back to the dashboard

`--dry-run` prints the HTML instead of sending, so the layout can be checked
without credentials.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage

from common import DATA, load_json

SCORED_PATH = DATA / "scored.json"
BAND_LABEL = {"must_read": "必读", "recommended": "推荐"}
BAND_COLOR = {"must_read": "#8c2f2f", "recommended": "#8a6d1f"}


def recent(items: list[dict], days: int) -> list[dict]:
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    out = [i for i in items
           if i.get("band") in BAND_LABEL and (i.get("first_seen") or "") >= cutoff]
    out.sort(key=lambda i: (-i["score"], i.get("published", "")))
    return out


def build_html(items: list[dict], days: int, site_url: str) -> str:
    today = dt.date.today().isoformat()
    groups: dict[str, list[dict]] = {"must_read": [], "recommended": []}
    for item in items:
        groups[item["band"]].append(item)

    rows = []
    for band in ("must_read", "recommended"):
        if not groups[band]:
            continue
        rows.append(
            f'<tr><td style="padding:22px 0 8px">'
            f'<div style="font:600 15px Georgia,serif;color:{BAND_COLOR[band]};'
            f'border-bottom:1px solid #e3e0da;padding-bottom:6px">'
            f'{BAND_LABEL[band]} · {len(groups[band])} 篇</div></td></tr>'
        )
        for item in groups[band]:
            authors = html.escape("、".join(item.get("authors", [])) or "—")
            issue = " · ".join(x for x in [
                html.escape(item["journal"]),
                f"Vol. {item['volume']}" if item.get("volume") else "",
                f"No. {item['issue']}" if item.get("issue") else "",
                item.get("published", ""),
            ] if x)
            terms = "、".join(dict.fromkeys(
                w["term"] for w in item.get("why", [])
                if w["kind"] in ("keyword", "author") and w["points"] > 0))[:160]
            stage = ('<span style="font:11px monospace;color:#8a6d1f;'
                     'border:1px solid #8a6d1f;border-radius:3px;padding:0 4px;'
                     'margin-left:6px">预印</span>'
                     if item.get("stage") == "online-first" else "")
            abstract = html.escape((item.get("abstract") or "")[:420])
            rows.append(f'''<tr><td style="padding:11px 0;border-bottom:1px solid #f0eee9">
  <div style="font:600 16px/1.4 Georgia,serif;margin-bottom:3px">
    <a href="{html.escape(item['url'])}" style="color:#1a1917;text-decoration:none">{html.escape(item['title'])}</a>{stage}
  </div>
  <div style="font:italic 13px Georgia,serif;color:#6b6862">{authors}</div>
  <div style="font:12px -apple-system,sans-serif;color:#6b6862;margin-top:2px">{issue}
    <span style="color:{BAND_COLOR[band]}">· {item['score']}</span></div>
  {f'<div style="font:13px/1.5 -apple-system,sans-serif;color:#55524d;margin-top:6px">{abstract}</div>' if abstract else ''}
  {f'<div style="font:11px monospace;color:#8b877f;margin-top:6px">命中:{html.escape(terms)}</div>' if terms else ''}
</td></tr>''')

    if not items:
        rows.append('<tr><td style="padding:30px 0;color:#6b6862;font:14px sans-serif">'
                    '本周没有新的必读或推荐条目。</td></tr>')

    footer = (f'<p style="font:12px -apple-system,sans-serif;color:#8b877f;margin-top:26px">'
              f'过去 {days} 天 · 共 {len(items)} 篇'
              + (f' · <a href="{html.escape(site_url)}" style="color:#8c2f2f">看完整面板</a>'
                 f' · <a href="{html.escape(site_url.rstrip("/"))}/feed.xml" '
                 f'style="color:#8c2f2f">Atom</a>' if site_url else "")
              + '</p>')

    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;background:#faf9f7;padding:26px 14px">
<table role="presentation" style="max-width:640px;margin:0 auto;background:#fff;
  border:1px solid #e3e0da;border-radius:8px;padding:24px 28px;border-collapse:collapse">
<tr><td>
  <div style="font:600 19px Georgia,serif;color:#1a1917">TOC Radar 周报</div>
  <div style="font:12px monospace;color:#8b877f;margin-top:3px">{today}</div>
</td></tr>
{''.join(rows)}
<tr><td>{footer}</td></tr>
</table></body></html>'''


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    items = recent(load_json(SCORED_PATH, []), args.days)
    site_url = os.environ.get("SITE_URL", "")
    body = build_html(items, args.days, site_url)

    if args.dry_run:
        out = DATA.parent / "digest-preview.html"
        out.write_text(body, encoding="utf-8")
        print(f"[dry-run] {len(items)} items -> {out}")
        return 0

    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    sender = os.environ.get("MAIL_FROM", user or "")
    recipient = os.environ.get("MAIL_TO", "")

    missing = [n for n, v in [("SMTP_HOST", host), ("SMTP_USER", user),
                              ("SMTP_PASS", password), ("MAIL_TO", recipient)] if not v]
    if missing:
        print(f"[skip] email not configured (missing {', '.join(missing)})",
              file=sys.stderr)
        return 0

    if not items:
        print("[skip] nothing new this week; not sending")
        return 0

    msg = EmailMessage()
    counts = {b: sum(1 for i in items if i["band"] == b) for b in BAND_LABEL}
    msg["Subject"] = (f"TOC Radar · 必读 {counts['must_read']} / "
                      f"推荐 {counts['recommended']} · {dt.date.today().isoformat()}")
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content("这封邮件需要支持 HTML 的客户端查看。")
    msg.add_alternative(body, subtype="html")

    context = ssl.create_default_context()
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as server:
                server.login(user, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as server:
                server.starttls(context=context)
                server.login(user, password)
                server.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        print(f"[error] send failed: {exc}", file=sys.stderr)
        return 1

    print(f"[done] sent {len(items)} items to {recipient}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
