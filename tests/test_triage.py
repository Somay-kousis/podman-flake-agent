#!/usr/bin/env python3
"""Offline test of the rule layer -- no network, no model, no database.

The dossiers below are trimmed to the fields `triage.py` reads, but the log
lines inside them are copied verbatim from real failed jobs in
podman-container-tools/podman (run ids in each comment). Synthetic structure,
real text: a rule that only fires on text someone wrote to make it fire has not
been tested.

Half of these are negative cases, and they are the point. The one that matters
most is `journald_noise`: every Podman job uploads an artifact named
`journal-<suite>-<distro>.log`, so a /journald?/ pattern -- which is tempting,
because Podman flakes really are often journal timeliness -- matches almost
every log in the corpus and classifies nothing. It must return `unknown`.

Run: python3 tests/test_triage.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flakeagent import triage


def dossier(job_name, step_name, log, tests=(), **extra):
    doc = {
        "job": {"id": 1, "name": job_name, "html_url": "https://example.invalid/job/1"},
        "failing_step": {"first": {"name": step_name, "number": 4}},
        "log_window": {"text": log,
                       "failing_tests": [{"kind": "bats", "name": t} for t in tests]},
        "siblings": {"total": 55, "failed": 1},
    }
    doc.update(extra)
    return doc


# From run 30849692281, job 91810074171 -- apt could not fetch during
# lima-actions/setup, so the suite never started.
SETUP_APT = dossier(
    "int local root fedora-rawhide / lima",
    "Run lima-vm/lima-actions/setup@55627e31b78637bf254a8b2a14da8ea7d12564e5",
    "+ sudo apt-get update -qq\n"
    "E: Failed to fetch https://dl.google.com/linux/chrome-stable/deb/dists/stable/"
    "main/binary-amd64/Packages.gz  File has unexpected size (1411 != 1412). "
    "Mirror sync in progress? [IP: 173.194.47.136 443]\n"
    "E: Some index files failed to download. They have been ignored, or old ones "
    "used instead.\n"
    "##[error]Process completed with exit code 100.\n",
)

# The same apt failure, but reached from the test step instead of a setup step,
# so the step-role rule cannot fire and the log pattern has to carry it alone.
TEST_STEP_APT = dossier(
    "sys local root debian-sid / lima", "Run test on lima",
    "+ dnf install -y podman-tests\n"
    "Errors during downloading metadata for repository 'updates':\n"
    "  - Curl error (56): Failure when receiving data from the peer\n"
    "Error: Failed to download metadata for repo 'updates'\n",
)

DISK_FULL = dossier(
    "sys local root fedora-current / lima", "Run test on lima",
    "not ok 42 podman build\n"
    "# Error: writing blob: adding layer with blob: "
    "write /var/lib/containers/storage/overlay: no space left on device\n",
)

NET_TIMEOUT = dossier(
    "int remote root fedora-current / lima", "Run test on lima",
    "Error: initializing source docker://quay.io/libpod/testimage:20241011: "
    "pinging container registry quay.io: Get \"https://quay.io/v2/\": "
    "dial tcp 34.196.1.1:443: i/o timeout\n",
)

# Negative case, and the reason the patterns are anchored the way they are.
# This is an ordinary ginkgo failure; the only occurrences of "journal" are the
# artifact upload the workflow does for every job, pass or fail.
JOURNALD_NOISE = dossier(
    "sys local root fedora-prior / lima", "Run test on lima",
    "not ok 21 |030| podman run docker-archive in 8852ms\n"
    "# #| FAIL: exit code is 125; expected 0\n"
    "##[group]Run actions/upload-artifact@043fb46d\n"
    "with:\n  name: journal-sys-local-root-fedora-prior.log\n"
    "  path: ./hack/ci/journal.log\n"
    "No files were found with the provided path: ./hack/ci/journal.log.\n",
    tests=("podman run docker-archive",),
)

AGGREGATOR = dossier("Total Success", "Check all required jobs",
                     "##[error]Process completed with exit code 1.\n")

# Every advisory flag set to its loudest value, over a log no rule matches. The
# category must still be `unknown`: these are the fields a human labels from,
# and a rule that reads them scores like the 92% baseline in baselines.py while
# measuring nothing.
LOUD_FLAGS = dossier(
    "sys local root fedora-prior / lima", "Run test on lima",
    "not ok 17 podman network reload\n",
    attempts={"disagreement": True},
    history={"failure_rate": 0.91, "observations": 100, "failures": 91},
    pull_request={"all_paths_inert": True, "inert_file_count": 7},
    known_fixes={"issues": [{"number": 28940, "title": "set ulimits flake"}]},
)


CASES = [
    ("setup_apt", SETUP_APT, "infra_blip", "setup_step_failure"),
    ("test_step_apt", TEST_STEP_APT, "infra_blip", "package_manager_failure"),
    ("disk_full", DISK_FULL, "resource_exhaustion", "resource_exhaustion"),
    ("net_timeout", NET_TIMEOUT, "network_timeout", "network_timeout"),
    ("journald_noise", JOURNALD_NOISE, "unknown", None),
    ("aggregator", AGGREGATOR, "unknown", "aggregator_job"),
    ("loud_flags", LOUD_FLAGS, "unknown", None),
]

# The step names below are every distinct failing step seen over 30 days of
# podman CI (job_steps where conclusion='failure'), with the role each must get.
ROLES = [
    ("Run test on lima", "test"),
    ("Check all required jobs", "aggregate"),
    ("Run machine e2e", "test"),
    ("Output failure log as GITHUB_STEP_SUMMARY", "report"),
    ("Validate source", "test"),
    ("Check that the PR includes tests", "test"),
    ("Run cross build", "build"),
    ("Set up job", "setup"),
    ("Run lima-vm/lima-actions/setup@55627e31b7", "setup"),
    ("Build each commit", "build"),
    ("Run unit tests", "test"),
    ("Upload release artifacts", "report"),
    ("Install build dependencies", "setup"),
    # Not in the 30-day set, but this is how a pinned setup action is named and
    # it matched nothing before -- a silent gap rather than a wrong answer.
    ("Run actions/setup-go@b7ad1da", "setup"),
    ("Run actions/checkout@3d3c42e", "setup"),
    ("Set up Docker Buildx", "setup"),
]


def main():
    ok = True

    def check(label, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'} {label}: {got!r}"
              + ("" if good else f" (expected {want!r})"))

    print("step roles:")
    for name, want in ROLES:
        check(f"  {name[:42]:<42}", triage.step_role(name), want)

    print("\nverdicts:")
    for label, doc, want_cat, want_rule in CASES:
        v = triage.triage(doc)
        check(f"  {label:<16} category", v["category"], want_cat)
        check(f"  {label:<16} rule    ", v["rule"], want_rule)

        # Evidence is sliced out of the log, so it must always be findable in
        # it. If this ever fails it is the slicing that is wrong.
        log = " ".join(triage.log_text(doc).split())
        for line in v["evidence"]:
            check(f"  {label:<16} evidence in log", " ".join(line.split()) in log, True)

    print("\nblinding:")
    v = triage.triage(LOUD_FLAGS)
    # rerun disagreement, inert diff, history, siblings, known issue
    check("  flags are reported", len(v["flags"]), 5)
    check("  flags did not move the category", v["flags_informed_category"], False)
    check("  and the category is still unknown", v["category"], "unknown")

    print("\nmarkdown:")
    rows = [(f"[{d['job']['name']}]({d['job']['html_url']})", triage.triage(d))
            for _, d, _, _ in CASES]
    md = triage.markdown(rows, window="test")
    check("  aggregator is excluded from the report", "Total Success" in md, False)
    check("  counts the six triaged jobs", "**6 failed jobs.**" in md, True)
    check("  names the failing test in an abstention",
          "podman run docker-archive" in md, True)
    check("  folds the abstentions", "<details>" in md, True)

    print("\n" + ("all checks passed" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
