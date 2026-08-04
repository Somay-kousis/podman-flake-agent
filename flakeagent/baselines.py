"""Score the gold set with rules that do no reasoning at all.

A model result only means something next to the cheapest thing that could have
produced it. `eval.py` already compares against the majority class; this module
adds the rules that need no model *and no log* -- just fields the dossier
already carries.

It exists because one of them wins. `history.failure_rate >= 0.19` scores 92%
on the current gold set, beating the 85% majority class and beating both
measured model arms (29% and 19%) by a factor of three. That is not a result
about how easy the task is. It is a result about the gold set, and it is the
reason `docs/RESULTS.md` no longer treats the two model arms as a measurement
of triage skill.

Two things make these numbers weaker than they look, and both are printed
rather than left for a reader to work out:

  * The threshold was chosen after seeing the labels. 0.19 sits in the one-point
    gap between the two classes' observed ranges. Fitted on the evaluation set,
    it is an upper bound on what a held-out threshold would score, not an
    estimate of one.

  * The `real_bug` class is six job instances of a single issue (#23281). Any
    rule that separates that one bug's jobs from everything else scores ~90%
    without generalising past one bug.

So read these as diagnostics of the gold set, not as a leaderboard.

    python3 -m flakeagent.baselines
    python3 -m flakeagent.eval baselines
"""

import collections
import glob
import json
import os

from . import store

# Chosen by inspecting the gap between the classes -- see the module docstring.
# Named rather than inlined so its provenance travels with it.
FITTED_THRESHOLD = 0.19


def load_gold(conn):
    """{job_id: category} for dossier-keyed gold labels."""
    return {r["fkey"].split(":", 1)[1]: r["category"] for r in
            conn.execute("SELECT fkey, category FROM gold_labels "
                         "WHERE fkey LIKE 'job:%'")}


def load_rows(gold, dossier_dir="data/dossiers"):
    """(gold, job_name, failure_rate, issue) per labelled dossier."""
    rows = []
    for path in sorted(glob.glob(os.path.join(dossier_dir, "*.json"))):
        with open(path) as fh:
            doc = json.load(fh)
        job_id = str((doc.get("job") or {}).get("id"))
        if job_id not in gold:
            continue
        rows.append({
            "gold": gold[job_id],
            "job_name": (doc.get("job") or {}).get("name") or "",
            "failure_rate": (doc.get("history") or {}).get("failure_rate"),
        })
    return rows


# Each rule sees only what its name says it sees. None of them read the log.
RULES = {
    "always race_condition":
        lambda r: "race_condition",
    "job name contains 'machine'":
        lambda r: "real_bug" if "machine" in r["job_name"] else "race_condition",
    f"failure_rate >= {FITTED_THRESHOLD} (fitted)":
        lambda r: "real_bug" if (r["failure_rate"] or 0) >= FITTED_THRESHOLD
        else "race_condition",
}


def main(argv=None):
    conn = store.connect(None)
    gold = load_gold(conn)
    rows = load_rows(gold)
    if not rows:
        print("no labelled dossiers found")
        return 1

    print(f"{len(rows)} labelled dossiers\n")
    print("  rule                                     accuracy")
    print("  " + "-" * 52)
    for name, rule in RULES.items():
        ok = sum(1 for r in rows if rule(r) == r["gold"])
        print(f"  {name:<40} {ok:>2}/{len(rows)} = {ok / len(rows):>4.0%}")

    # The distribution is the point: a rule can only beat the majority class by
    # separating a class that is this small and this concentrated.
    dist = collections.Counter(r["gold"] for r in rows)
    print(f"\n  gold distribution: "
          f"{', '.join(f'{k}={v}' for k, v in dist.most_common())}")

    issues = collections.Counter(
        r["issue_number"] for r in
        conn.execute("SELECT category, issue_number FROM gold_labels "
                     "WHERE fkey LIKE 'job:%' AND category='real_bug'"))
    if issues:
        print(f"  real_bug spans {len(issues)} distinct issue(s): "
              f"{', '.join(f'#{n} x{k}' for n, k in issues.most_common())}")
        if len(issues) == 1:
            print("  ^ effective sample size for real_bug is 1, not "
                  f"{sum(issues.values())}. No rule scored here generalises.")

    print("\nthreshold was fitted on these same labels -- see module docstring")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
