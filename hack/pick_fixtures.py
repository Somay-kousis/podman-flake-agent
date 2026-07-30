#!/usr/bin/env python3
"""Choose a handful of dossiers to commit as offline development fixtures.

    python3 hack/pick_fixtures.py --db data/flakes.db --out tests/dossiers

Picks for *variety*, not recency: agent development needs to see the shapes it
must handle, and the most recent N failures are usually the same shape repeated.

Categories sought, one each:

  ginkgo_test    a ginkgo spec failed inside a long test step
  bats_test      a bats/system test failure
  infra_step     the job died in setup -- not a test at all
  with_fix       a related issue whose fix a maintainer identified
  no_evidence    an honest hard case: little to go on

Public CI data throughout; nothing to redact.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flakeagent import dossier, store

# Steps that mean "the job never got as far as running tests".
INFRA_STEPS = ("set up job", "checkout", "install", "set up go", "download",
               "configure", "build podman", "fetch", "restore", "pre-clean")


def classify_shape(d):
    """What shape of failure is this? Descriptive only -- not a root cause."""
    step = (d.get("failing_step") or {}).get("first") or {}
    name = (step.get("name") or "").lower()
    tests = (d.get("log_window") or {}).get("failing_tests") or []
    kinds = {t["kind"] for t in tests}
    fixed = any(i.get("has_identified_fix")
                for i in (d.get("known_fixes") or {}).get("issues", []))

    shapes = []
    if any(k in name for k in INFRA_STEPS) and not tests:
        shapes.append("infra_step")
    if "ginkgo" in kinds:
        shapes.append("ginkgo_test")
    if "bats" in kinds:
        shapes.append("bats_test")
    if "python" in kinds:
        shapes.append("python_test")
    if fixed:
        shapes.append("with_fix")
    if not tests and not fixed:
        shapes.append("no_evidence")
    return shapes


WANTED = ["ginkgo_test", "bats_test", "infra_step", "with_fix", "no_evidence"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db")
    ap.add_argument("--out", default="tests/dossiers")
    ap.add_argument("--scan", type=int, default=400,
                    help="how many failed jobs to consider")
    args = ap.parse_args()

    conn = store.connect(args.db)
    ids = [r["id"] for r in conn.execute(
        """SELECT j.id FROM jobs j JOIN job_logs l ON l.job_id = j.id
           WHERE j.conclusion='failure' ORDER BY j.id DESC LIMIT ?""",
        (args.scan,))]
    if not ids:
        sys.exit("no failed jobs with stored logs; run `fetch logs` first")

    print(f"scanning {len(ids)} failed jobs with logs...")
    chosen, seen_shapes = {}, {}

    for job_id in ids:
        try:
            d = dossier.build(conn, job_id)
        except Exception as e:
            print(f"  ! job {job_id}: {e}", file=sys.stderr)
            continue
        shapes = classify_shape(d)
        for s in shapes:
            seen_shapes[s] = seen_shapes.get(s, 0) + 1

        # One job per fixture. A job that is both `ginkgo_test` and `with_fix`
        # would otherwise be written twice under different names -- five
        # fixtures covering four distinct failures, which defeats the point.
        if job_id in {jid for jid, _ in chosen.values()}:
            continue
        for want in WANTED:
            if want not in chosen and want in shapes:
                chosen[want] = (job_id, d)
                break
        if len(chosen) == len(WANTED):
            break

    print("\nshapes seen:")
    for s, n in sorted(seen_shapes.items(), key=lambda x: -x[1]):
        print(f"  {s:<14}{n:>5}")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    print(f"\nwriting {len(chosen)} fixture(s) -> {out}")
    for shape, (job_id, d) in chosen.items():
        path = out / f"{shape}-{job_id}.json"
        path.write_text(json.dumps(d, indent=2, default=str))
        step = (d["failing_step"].get("first") or {}).get("name", "-")
        tests = [t["name"] for t in d["log_window"].get("failing_tests", [])]
        print(f"  {path.name}")
        print(f"      step: {step[:52]}")
        print(f"      test: {(tests[0] if tests else '(none)')[:52]}")
        print(f"      {len(path.read_text()):,} bytes")

    missing = [w for w in WANTED if w not in chosen]
    if missing:
        print(f"\n  not found in this sample: {', '.join(missing)}")
    conn.close()


if __name__ == "__main__":
    main()
