#!/usr/bin/env python3
"""Offline test of the step-attribution path -- no network, no API budget.

The payload below is the real shape returned by
/repos/podman-container-tools/podman/actions/runs/30549428074/jobs, trimmed to
two failing jobs. It is the data that makes infra-vs-test attribution possible
without reading a single log line.

Run: python3 tests/test_steps.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flakeagent import store
from flakeagent.ingest import parse_job_name

RUN_ID = 30549428074

JOBS = [
    {
        "id": 90896283356, "run_id": RUN_ID, "run_attempt": 1,
        "name": "macos machine applehv", "conclusion": "failure", "status": "completed",
        "runner_name": "MacM1-1", "runner_id": 12, "labels": [],
        "workflow_name": "ci", "head_sha": "c" * 40,
        "check_run_url": "https://api.github.com/repos/x/y/check-runs/90896283356",
        "started_at": "2026-07-30T14:06:52Z", "completed_at": "2026-07-30T14:37:10Z",
        "html_url": "https://github.com/x/y/actions/runs/1/job/2",
        "steps": [
            {"number": 1, "name": "Set up job", "status": "completed", "conclusion": "success",
             "started_at": "2026-07-30T14:06:52Z", "completed_at": "2026-07-30T14:06:53Z"},
            {"number": 2, "name": "Run actions/checkout@3d3c42e", "status": "completed",
             "conclusion": "success", "started_at": "2026-07-30T14:06:53Z",
             "completed_at": "2026-07-30T14:07:01Z"},
            {"number": 3, "name": "Run actions/setup-go@b7ad1da", "status": "completed",
             "conclusion": "success", "started_at": "2026-07-30T14:07:01Z",
             "completed_at": "2026-07-30T14:07:20Z"},
            {"number": 4, "name": "Pre-clean machine state", "status": "completed",
             "conclusion": "success", "started_at": "2026-07-30T14:07:20Z",
             "completed_at": "2026-07-30T14:07:25Z"},
            {"number": 5, "name": "Download test binaries", "status": "completed",
             "conclusion": "success", "started_at": "2026-07-30T14:07:25Z",
             "completed_at": "2026-07-30T14:07:40Z"},
            {"number": 6, "name": "Restore executable bits", "status": "completed",
             "conclusion": "success", "started_at": "2026-07-30T14:07:40Z",
             "completed_at": "2026-07-30T14:07:41Z"},
            {"number": 7, "name": "Run machine e2e", "status": "completed",
             "conclusion": "failure", "started_at": "2026-07-30T14:07:41Z",
             "completed_at": "2026-07-30T14:36:55Z"},
            {"number": 8, "name": "Post-run cleanup", "status": "completed",
             "conclusion": "success", "started_at": "2026-07-30T14:36:55Z",
             "completed_at": "2026-07-30T14:37:02Z"},
            {"number": 16, "name": "Complete runner", "status": "completed",
             "conclusion": "success", "started_at": "2026-07-30T14:37:05Z",
             "completed_at": "2026-07-30T14:37:09Z"},
        ],
    },
    {
        # Contrast case: dies during setup, not during the test. Same shape,
        # completely different root cause -- and no log needed to tell them apart.
        "id": 90896283999, "run_id": RUN_ID, "run_attempt": 1,
        "name": "int local root fedora-current", "conclusion": "failure",
        "status": "completed", "runner_name": "gh-runner-7", "runner_id": 7,
        "labels": ["cncf-ubuntu-8-32-x86"], "workflow_name": "ci",
        "head_sha": "c" * 40, "check_run_url": None,
        "started_at": "2026-07-30T14:06:52Z", "completed_at": "2026-07-30T14:11:02Z",
        "html_url": "https://github.com/x/y/actions/runs/1/job/3",
        "steps": [
            {"number": 1, "name": "Set up job", "status": "completed",
             "conclusion": "success", "started_at": "2026-07-30T14:06:52Z",
             "completed_at": "2026-07-30T14:06:53Z"},
            {"number": 2, "name": "Install build dependencies", "status": "completed",
             "conclusion": "failure", "started_at": "2026-07-30T14:06:53Z",
             "completed_at": "2026-07-30T14:11:00Z"},
            {"number": 3, "name": "Run test on lima", "status": "completed",
             "conclusion": "skipped", "started_at": None, "completed_at": None},
        ],
    },
]

RUN = {
    "id": RUN_ID, "run_number": 1, "run_attempt": 1, "head_sha": "c" * 40,
    "head_branch": "main", "event": "push", "conclusion": "failure",
    "created_at": "2026-07-30T14:05:00Z", "html_url": "https://github.com/x/y/actions/runs/1",
    "name": "ci", "path": ".github/workflows/ci.yml",
    "run_started_at": "2026-07-30T14:06:00Z", "updated_at": "2026-07-30T14:57:00Z",
    "actor": {"login": "someone"}, "display_title": "fix a thing",
}


def main():
    db = Path(tempfile.mkdtemp()) / "t.db"
    conn = store.connect(db)

    store.upsert_run(conn, RUN)
    for job in JOBS:
        store.upsert_job(conn, job, parse_job_name(job["name"]))
        store.add_steps(conn, job["id"], job["steps"])
    conn.commit()

    ok = True

    def check(label, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'} {label}: {got!r}"
              + ("" if good else f" (expected {want!r})"))

    n_steps = conn.execute("SELECT COUNT(*) n FROM job_steps").fetchone()["n"]
    check("steps stored", n_steps, 12)

    # The headline query: which step actually failed, per job.
    print("\nfailing step per job (no logs read):")
    rows = conn.execute(
        """SELECT j.name AS job, s.number, s.name AS step,
                  j.runner_name,
                  CAST((julianday(s.completed_at) - julianday(s.started_at))
                       * 86400 AS INTEGER) AS secs
           FROM job_steps s JOIN jobs j ON j.id = s.job_id
           WHERE s.conclusion = 'failure' ORDER BY j.id"""
    ).fetchall()
    for r in rows:
        print(f"    {r['job'][:34]:<34} step {r['number']:>2} "
              f"{r['step'][:30]:<30} {str(r['secs']) + 's':>7}  runner={r['runner_name']}")

    check("\n  one failing step per job", len(rows), 2)
    check("  e2e job failed in the test step", rows[0]["step"], "Run machine e2e")
    check("  int job failed in setup", rows[1]["step"], "Install build dependencies")

    # Matrix decomposition still works alongside the new columns.
    j = conn.execute("SELECT * FROM jobs WHERE id=90896283999").fetchone()
    check("matrix test parsed", j["test"], "int")
    check("matrix distro parsed", j["distro"], "fedora-current")
    check("runner captured", j["runner_name"], "gh-runner-7")
    check("labels captured", j["labels"], '["cncf-ubuntu-8-32-x86"]')

    # Idempotency: re-storing must not duplicate steps.
    for job in JOBS:
        store.add_steps(conn, job["id"], job["steps"])
    conn.commit()
    check("re-store is idempotent",
          conn.execute("SELECT COUNT(*) n FROM job_steps").fetchone()["n"], 12)

    conn.close()
    print("\n" + ("all checks passed" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
