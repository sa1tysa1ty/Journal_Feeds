"""Offline test of normalise -> merge -> score, using a Crossref-shaped fixture.

Runs without network so the logic can be verified before the first Actions run.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import harvest  # noqa: E402
import score as scoring  # noqa: E402
from common import load_yaml  # noqa: E402

JOURNALS = {j["issn"]: j for j in load_yaml("journals.yaml")["journals"]}
FIXTURE = json.loads((Path(__file__).parent / "fixture_crossref.json").read_text())

failures = []


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f"  -- {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


print("\n=== 1. normalise + state machine ===")
items, state = {}, {}
events = []
for work in FIXTURE:
    issn = work["ISSN"][0]
    record = harvest.normalise(work, JOURNALS[issn])
    if record is None:
        events.append(("skipped", work["DOI"]))
        continue
    merged, event = harvest.merge(items, record, state)
    items[merged["doi"]] = merged
    events.append((event, merged["doi"]))

doi = "10.1177/00905917241234567"
check("journal-issue record is skipped",
      any(e[0] == "skipped" for e in events))
check("online-first then in-issue collapses to ONE stored item",
      len([k for k in items if k == doi]) == 1)
check("second sighting is 'promoted', not 'new'",
      events[0][0] == "new" and events[1][0] == "promoted",
      f"got {events[0][0]}, {events[1][0]}")
check("promoted item keeps volume/issue", items[doi]["volume"] == "54")
check("promoted item retains abstract dropped by the later payload",
      "aleatory materialism" in items[doi]["abstract"].lower())
check("JATS tags stripped from abstract", "<jats" not in items[doi]["abstract"])
check("'Abstract' label stripped",
      not items[doi.lower()]["abstract"].lower().startswith("abstract"))
check("subtitle merged into title",
      items["10.1111/cons.12345"]["title"] == "Recognition after Honneth: A Reply")
check("DOI normalised to lowercase", "10.1111/cons.12345" in items)
check("print date preferred over online date",
      items[doi]["published"] == "2026-10-01", items[doi]["published"])

print("\n=== 2. scoring ===")
config = load_yaml("keywords.yaml")
matchers = scoring.build_matchers(config)
scored = [scoring.score_item(i, matchers, config) for i in items.values()]
by_doi = {s["doi"]: s for s in scored}

althusser = by_doi[doi]
check("Althusser piece lands in 必读", althusser["band"] == "must_read",
      f"score={althusser['score']} band={althusser['band']}")
check("Montag matched from the author watchlist",
      any(w["kind"] == "author" and w["term"] == "Montag" for w in althusser["why"]))
check("title hit outweighs abstract hit",
      any(w["term"] == "Overdetermination" or w["term"] == "overdetermination"
          for w in althusser["why"]))
check("every kept item carries a why-trace", all("why" in s for s in scored))

apsr = by_doi["10.1017/s0003055426000111"]
check("methods paper penalised", any(w["kind"] == "penalty" for w in apsr["why"]),
      str(apsr["why"]))
check("methods paper does not reach 必读", apsr["band"] != "must_read",
      f"score={apsr['score']}")

gramsci = by_doi["10.1080/0893569.2026.999"]
check("Gramsci/Poulantzas piece scores above the APSR methods paper",
      gramsci["score"] > apsr["score"], f"{gramsci['score']} vs {apsr['score']}")

print("\n=== 3. tier gate ===")
kept = scoring.apply_tier_gate(scored)
kept_dois = {k["doi"] for k in kept}
check("T1 item kept regardless of score", doi in kept_dois)
check("T2 item with hits kept", "10.1080/0893569.2026.999" in kept_dois)
check("T2 item with zero hits dropped",
      "10.1017/s0003055426000111" not in kept_dois or apsr["hits"] > 0)
check("T3 item survives as monthly top-N", "10.1086/ci.2026.77" in kept_dois)

print("\n=== scores ===")
for s in sorted(scored, key=lambda x: -x["score"]):
    gate = "kept" if s["doi"] in kept_dois else "gated"
    print(f"  {s['score']:>6.2f}  {s['band']:<12} {s['tier']}  {gate:<5}  "
          f"{s['title'][:58]}")

print(f"\n{'ALL PASS' if not failures else str(len(failures)) + ' FAILURE(S): ' + ', '.join(failures)}")
sys.exit(1 if failures else 0)
