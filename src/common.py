"""Shared helpers: config loading, HTTP with retry, paths."""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
DATA = ROOT / "data"
SITE = ROOT / "site"

CONTACT_EMAIL = os.environ.get("CROSSREF_MAILTO", "toc-radar@example.com")
USER_AGENT = f"toc-radar/0.1 (https://github.com/; mailto:{CONTACT_EMAIL})"

DATA.mkdir(exist_ok=True)
SITE.mkdir(exist_ok=True)


def load_yaml(name: str) -> dict:
    with open(CONFIG / name, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[warn] could not read {path}: {exc}", file=sys.stderr)
        return default


def save_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1, sort_keys=True)
    tmp.replace(path)


_session: requests.Session | None = None


def session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"User-Agent": USER_AGENT})
    return _session


def get_json(url: str, params: dict | None = None, tries: int = 4,
             timeout: int = 45) -> dict | None:
    """GET returning parsed JSON, with backoff. None on permanent failure."""
    delay = 2.0
    for attempt in range(1, tries + 1):
        try:
            resp = session().get(url, params=params, timeout=timeout)
        except requests.RequestException as exc:
            print(f"[warn] {url} attempt {attempt}: {exc}", file=sys.stderr)
        else:
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError as exc:
                    print(f"[warn] bad JSON from {url}: {exc}", file=sys.stderr)
                    return None
            if resp.status_code == 404:
                return None
            # 429 / 5xx -> retry
            print(f"[warn] {url} attempt {attempt}: HTTP {resp.status_code}",
                  file=sys.stderr)
            retry_after = resp.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                delay = max(delay, float(retry_after))
        if attempt < tries:
            time.sleep(delay)
            delay *= 2
    return None


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_JATS_LABEL_RE = re.compile(r"^\s*abstract\s*", re.IGNORECASE)


def clean_abstract(raw: str | None) -> str:
    """Crossref abstracts are JATS XML. Strip tags and normalise whitespace."""
    if not raw:
        return ""
    text = _TAG_RE.sub(" ", raw)
    text = (text.replace("&amp;", "&").replace("&lt;", "<")
                .replace("&gt;", ">").replace("&quot;", '"')
                .replace("&#x2018;", "'").replace("&#x2019;", "'")
                .replace("&#x201C;", '"').replace("&#x201D;", '"'))
    text = _WS_RE.sub(" ", text).strip()
    return _JATS_LABEL_RE.sub("", text).strip()


def first(value: Any) -> str:
    """Crossref returns most string fields as lists."""
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value) if value is not None else ""


def date_parts_to_iso(node: dict | None) -> str:
    """{'date-parts': [[2026, 3, 14]]} -> '2026-03-14' (partial dates padded)."""
    if not node:
        return ""
    parts = node.get("date-parts") or [[]]
    if not parts or not parts[0]:
        return ""
    nums = [n for n in parts[0] if isinstance(n, int)]
    if not nums:
        return ""
    year = nums[0]
    month = nums[1] if len(nums) > 1 else 1
    day = nums[2] if len(nums) > 2 else 1
    try:
        return f"{year:04d}-{month:02d}-{day:02d}"
    except (TypeError, ValueError):
        return ""
