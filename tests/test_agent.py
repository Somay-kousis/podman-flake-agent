#!/usr/bin/env python3
"""Offline test of the dossier -> prediction path -- no network, no API budget.

Runs the whole driver against the five committed dossiers with a stub backend
standing in for the model, so the plumbing that a real run depends on is
exercised without a key or a GPU: blinding, prompt construction, evidence
verification, the off-vocabulary guard, and the two output files.

The evidence check is the part worth testing hardest. It is the only thing
standing between a fluent verdict and a fabricated one, and it is easy to get
subtly wrong -- quoting text that the prompt showed but the checker never sees
(or the reverse) turns honest quotes into false hallucination reports. The
`ansi` case below is exactly that trap.

Run: python3 tests/test_agent.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flakeagent import agent
from flakeagent.dossier import blind

FIXTURES = Path(__file__).resolve().parent / "dossiers"

ok = fail = 0


def check(label, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print(f"  ok   {label}: {got!r}")
    else:
        fail += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


class StubBackend:
    """Returns a fixed verdict. Records every prompt so the test can inspect it."""

    name = "stub"

    def __init__(self, verdict):
        self.verdict = verdict
        self.prompts = []

    def classify(self, prompt):
        self.prompts.append(prompt)
        return dict(self.verdict), 100, 20


def load_one():
    path = FIXTURES / "ginkgo_test-90583475284.json"
    return json.load(path.open())


print("== blinding is on by default ==")
doc = load_one()
b = blind(doc)
check("labeller evidence dropped",
      sorted(set(doc) - set(b)),
      ["attempts", "history", "known_fixes", "related_issues"])
check("log window survives", bool((b.get("log_window") or {}).get("text")), True)

print("\n== prompt carries the cheap signals before the log ==")
p = agent.build_prompt(b)
head = p.split("Log window")[0]
check("failing step named", "Failing step:" in head, True)
check("sibling counts present", "Sibling jobs in this run:" in head, True)
check("no None facets", "=None" in head, False)
check("log comes last", p.index("Log window") > p.index("Failing step:"), True)

print("\n== evidence verification ==")
real = agent.log_text(b).strip().splitlines()[1].strip()

cases = [
    ("verbatim quote matches", [real], (1, 1)),
    ("invented quote caught", ["this line is not in the log anywhere"], (0, 1)),
    ("whitespace-only diff still matches", ["  ".join(real.split())], (1, 1)),
    ("no citation is not a failure", [], (0, 0)),
    ("one of two", [real, "fabricated"], (1, 2)),
]
for label, ev, want in cases:
    check(label, agent.verify_evidence({"evidence": ev}, b), want)

print("\n== ansi: prompt and checker must read one string ==")
ansi_doc = {"log_window": {"text": "plain line\n\x1b[32;1mcoloured line\x1b[0m\n"}}
check("ansi stripped from log_text", "\x1b[" in agent.log_text(ansi_doc), False)
check("quote of the cleaned line verifies",
      agent.verify_evidence({"evidence": ["coloured line"]}, ansi_doc), (1, 1))

print("\n== driver end to end, stub model ==")
stub = StubBackend({
    "category": "race_condition", "confidence": 0.8,
    "reasoning": "stub", "evidence": [real],
    "suggested_action": "rerun", "duplicate_of": None,
})
agent.store = None                       # the stub path must not touch the DB

with tempfile.TemporaryDirectory() as tmp:
    preds_p, verd_p = Path(tmp) / "preds.json", Path(tmp) / "verdicts.json"
    real_ollama = agent.__dict__.get("OllamaBackend")
    import flakeagent.classify as classify_mod
    classify_mod.OllamaBackend = lambda *a, **k: stub

    rc = agent.main([
        "--dossiers", str(FIXTURES), "--out", str(preds_p),
        "--verdicts", str(verd_p), "--backend", "ollama",
    ])
    check("exit code", rc, 0)

    preds = json.loads(preds_p.read_text())
    verdicts = json.loads(verd_p.read_text())
    check("one prediction per fixture", len(preds), 5)
    check("preds.json is job_id -> category, nothing else",
          {type(k).__name__ for k in preds} | {v for v in preds.values()},
          {"str", "race_condition"})
    check("job ids are the dossier ids",
          "90583475284" in preds, True)

    v = verdicts["90583475284"]
    check("verdict records blinding", v["blinded"], True)
    check("verdict records verification", (v["evidence_verified"], v["evidence_quoted"]), (1, 1))
    check("verdict records prompt size", v["prompt_chars"] > 0, True)

print("\n== off-vocabulary categories are refused ==")
bad = StubBackend({"category": "flaky_probably", "confidence": 0.9, "reasoning": "",
                   "evidence": [], "suggested_action": "", "duplicate_of": None})
classify_mod.OllamaBackend = lambda *a, **k: bad
with tempfile.TemporaryDirectory() as tmp:
    preds_p = Path(tmp) / "preds.json"
    agent.main(["--dossiers", str(FIXTURES), "--out", str(preds_p),
                "--verdicts", str(Path(tmp) / "v.json"), "--backend", "ollama"])
    check("nothing written for bad category", json.loads(preds_p.read_text()), {})

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
