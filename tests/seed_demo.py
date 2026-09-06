"""Populate the store from the offline fixture so the dashboard can be viewed
without network access. Overwrites data/. Run harvest.py afterwards for real data.

    python tests/seed_demo.py && python src/score.py && python src/render.py
    python -m http.server 8000 --directory site
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import harvest  # noqa: E402
from common import DATA, load_yaml, save_json  # noqa: E402

journals = {j["issn"]: j for j in load_yaml("journals.yaml")["journals"]}
fixture = json.loads((ROOT / "tests" / "fixture_crossref.json").read_text(encoding="utf-8"))

items, state = {}, {}
for work in fixture:
    record = harvest.normalise(work, journals[work["ISSN"][0]])
    if record is None:
        continue
    merged, _ = harvest.merge(items, record, state)
    items[merged["doi"]] = merged

save_json(DATA / "items.json", items)
save_json(DATA / "state.json", state)
save_json(DATA / "meta.json", {"base_url": "", "last_run": "", "demo": True})
print(f"seeded {len(items)} demo items into data/ (run score.py + render.py next)")
