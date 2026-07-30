#!/usr/bin/env python3
"""Seed the database from the logformatter fixtures.

Lets the parse -> store -> signal -> classify -> report path be exercised
end-to-end with no GitHub API budget and no CI run. The runs/jobs are
synthetic; the failure text is real logformatter output.

Usage: python3 tests/seed_from_fixtures.py [--db PATH]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flakeagent import store
from flakeagent.parse import parse_html

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Two attempts of the same job on the same commit, disagreeing -> the
# rerun_disagreement signal. Plus a second commit failing the same test ->
# cross_pr. Plus a main-branch failure -> main_failure.
RUNS = [
    dict(id=9001, run_number=1, run_attempt=2, head_sha="a" * 40,
         head_branch="pr-1", event="pull_request", conclusion="failure",
         created_at="2026-07-28T10:00:00Z", html_url="https://example/1"),
    dict(id=9002, run_number=2, run_attempt=1, head_sha="b" * 40,
         head_branch="pr-2", event="pull_request", conclusion="failure",
         created_at="2026-07-29T10:00:00Z", html_url="https://example/2"),
    dict(id=9003, run_number=3, run_attempt=1, head_sha="c" * 40,
         head_branch="main", event="push", conclusion="failure",
         created_at="2026-07-30T10:00:00Z", html_url="https://example/3"),
]

JOBS = [
    dict(id=8001, run_id=9001, name="int local root fedora-current", conclusion="failure"),
    dict(id=8002, run_id=9001, name="int local root fedora-current", conclusion="success"),
    dict(id=8003, run_id=9002, name="sys remote rootless debian-sid", conclusion="failure"),
    dict(id=8004, run_id=9003, name="apiv2 root fedora-current", conclusion="failure"),
]

# Which fixture's failures land on which job.
PLACEMENT = {
    8001: ["simple-ginkgo", "simple-bats"],
    8003: ["simple-ginkgo", "bats-with-timestamps-2023-05-16"],
    8004: ["simple-python"],
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db")
    args = ap.parse_args()

    if not FIXTURES.exists():
        sys.exit("no fixtures; run tests/extract_fixtures.py first")

    conn = store.connect(args.db)
    from flakeagent.ingest import parse_job_name

    for run in RUNS:
        store.upsert_run(conn, run)
    for job in JOBS:
        store.upsert_job(conn, job, parse_job_name(job["name"]))

    n = 0
    for job_id, names in PLACEMENT.items():
        for name in names:
            path = FIXTURES / f"{name}.html"
            if not path.exists():
                continue
            for f in parse_html(path.read_text(), source=path.name):
                store.add_failure(conn, job_id, f)
                n += 1

    # A couple of real `flakes` issues so the dedup tool has something to hit.
    for issue in [
        {"number": 24571, "title": "pod checkpoint/restore - incomplete restore?",
         "state": "open", "labels": [{"name": "flakes"}], "body": "", "updated_at": "2025-04-15"},
        {"number": 24220, "title": "Yet another missing-logs-and-events flake: journald?",
         "state": "open", "labels": [{"name": "flakes"}], "body": "", "updated_at": "2025-05-27"},
        {"number": 27264, "title": "system tests: podman artifact tests flaky",
         "state": "open", "labels": [{"name": "flakes"}], "body": "", "updated_at": "2025-11-23"},
    ]:
        store.upsert_issue(conn, issue)

    conn.commit()
    print(f"seeded {len(RUNS)} runs, {len(JOBS)} jobs, {n} failures, 3 issues")
    conn.close()


if __name__ == "__main__":
    main()
