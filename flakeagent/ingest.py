#!/usr/bin/env python3
"""Pull failing CI data out of GitHub Actions and mine flake signal from it.

    python3 -m flakeagent.ingest runs      --limit 30
    python3 -m flakeagent.ingest artifacts --limit 10
    python3 -m flakeagent.ingest issues
    python3 -m flakeagent.ingest signals
    python3 -m flakeagent.ingest summary

Set GITHUB_TOKEN. The unauthenticated budget (60/hr) will not survive a single
`runs` pass. Everything is cached under data/cache/, so re-running is free.
"""

import argparse
import io
import json
import re
import sys
import zipfile
from pathlib import Path

from . import store
from .gh import GitHub, RateLimited
from .parse import parse_html

REPO = "podman-container-tools/podman"
WORKFLOW = "ci.yml"

TESTS = {"sys", "int", "bud", "apiv2", "bindings", "compose_v2", "docker_py",
         "unit", "machine", "build", "upgrade", "farm"}
MODES = {"local", "remote"}
PRIVS = {"root", "rootless"}
DISTRO_RE = re.compile(r"^(fedora-\w+|debian-\w+|ubuntu-[\w.]+)$")


def parse_job_name(name):
    """ci.yml names matrix jobs '<test> <mode> <priv> <distro>' (big-tests) or
    a subset (small-tests). The reusable workflow appends ' / lima'."""
    out = {}
    head = name.split(" / ")[0]
    for tok in re.split(r"[\s,]+", head.strip()):
        t = tok.strip("()")
        if t in TESTS and "test" not in out:
            out["test"] = t
        elif t in MODES:
            out["mode"] = t
        elif t in PRIVS:
            out["priv"] = t
        elif DISTRO_RE.match(t):
            out["distro"] = t
    return out


def pr_number_of(run):
    prs = run.get("pull_requests") or []
    return prs[0]["number"] if prs else None


# -- commands -------------------------------------------------------------

def cmd_runs(gh, conn, args):
    """Failed workflow runs, plus every prior attempt (the rerun signal)."""
    seen = 0
    for run in gh.paginate(
        f"/repos/{REPO}/actions/workflows/{WORKFLOW}/runs",
        key="workflow_runs",
        status="failure",
        per_page=min(args.limit, 100),
        max_pages=max(1, args.limit // 100 + 1),
    ):
        if seen >= args.limit:
            break
        seen += 1
        store.upsert_run(conn, run, pr_number_of(run))

        attempts = run.get("run_attempt", 1)
        for attempt in range(1, attempts + 1):
            path = (f"/repos/{REPO}/actions/runs/{run['id']}/attempts/{attempt}/jobs"
                    if attempts > 1 else
                    f"/repos/{REPO}/actions/runs/{run['id']}/jobs")
            try:
                jobs = gh.get(path, per_page=100).get("jobs", [])
            except Exception as e:  # a deleted attempt shouldn't kill the pass
                print(f"  ! run {run['id']} attempt {attempt}: {e}", file=sys.stderr)
                continue
            for job in jobs:
                job["run_id"] = run["id"]
                job["_attempt"] = attempt
                store.upsert_job(conn, job, parse_job_name(job["name"]))
        conn.commit()
        print(f"  run {run['id']} attempt={attempts} sha={run['head_sha'][:8]} "
              f"branch={run.get('head_branch')}")
    print(f"\nstored {seen} failed runs ({gh.stats()})")


def cmd_artifacts(gh, conn, args):
    """Download the .logs artifacts and extract per-test failures."""
    rows = conn.execute(
        "SELECT DISTINCT run_id FROM jobs WHERE conclusion='failure' "
        "ORDER BY run_id DESC LIMIT ?", (args.limit,)
    ).fetchall()
    if not rows:
        print("no failed jobs stored; run `ingest runs` first")
        return

    cache = Path(gh.cache_dir).parent / "artifacts"
    total_failures = 0

    for row in rows:
        run_id = row["run_id"]
        try:
            arts = gh.get(f"/repos/{REPO}/actions/runs/{run_id}/artifacts").get("artifacts", [])
        except RateLimited:
            raise
        except Exception as e:
            print(f"  ! run {run_id}: {e}", file=sys.stderr)
            continue

        for art in arts:
            # PR #29091 renames these to '<test>-<mode>-<priv>-<distro>.logs'
            if not art["name"].endswith(".logs") and "journal" not in art["name"]:
                continue
            if art.get("expired"):
                continue
            zpath = cache / f"{run_id}-{art['id']}.zip"
            try:
                gh.download(art["archive_download_url"], zpath)
            except Exception as e:
                print(f"  ! artifact {art['id']}: {e}", file=sys.stderr)
                continue

            job = conn.execute(
                "SELECT id FROM jobs WHERE run_id=? AND conclusion='failure' LIMIT 1",
                (run_id,),
            ).fetchone()
            if not job:
                continue

            n = 0
            try:
                with zipfile.ZipFile(zpath) as z:
                    for member in z.namelist():
                        if not member.endswith(".html"):
                            continue
                        doc = z.read(member).decode("utf-8", errors="replace")
                        for f in parse_html(doc, source=f"{art['name']}/{member}"):
                            store.add_failure(conn, job["id"], f)
                            n += 1
            except zipfile.BadZipFile:
                print(f"  ! {zpath.name} is not a zip", file=sys.stderr)
                continue

            total_failures += n
            print(f"  run {run_id} {art['name']}: {n} failure(s)")
        conn.commit()

    print(f"\nextracted {total_failures} failures ({gh.stats()})")


def cmd_issues(gh, conn, args):
    """Cache the `flakes`-labelled issues -- the dedup target and eval corpus."""
    n = 0
    for issue in gh.paginate(
        "/search/issues", key="items",
        q=f"repo:{REPO} is:issue label:flakes", per_page=100, max_pages=3,
    ):
        store.upsert_issue(conn, issue)
        n += 1
    conn.commit()
    print(f"cached {n} `flakes` issues ({gh.stats()})")


def cmd_signals(gh, conn, args):
    """Mine flake ground truth. See README 'Ground truth' for why this is hard."""
    added = 0

    # 1. Rerun disagreement: same job name, same commit, different outcome
    #    across attempts. The strongest signal available.
    rows = conn.execute(
        """SELECT j.name, r.head_sha, r.id AS run_id,
                  GROUP_CONCAT(DISTINCT j.conclusion) AS outcomes
           FROM jobs j JOIN runs r ON r.id=j.run_id
           WHERE r.run_attempt > 1
           GROUP BY j.name, r.head_sha"""
    ).fetchall()
    for row in rows:
        outcomes = set((row["outcomes"] or "").split(","))
        if {"success", "failure"} <= outcomes:
            for f in conn.execute(
                """SELECT DISTINCT tf.fkey FROM test_failures tf
                   JOIN jobs j ON j.id=tf.job_id
                   WHERE j.name=? AND j.run_id=?""",
                (row["name"], row["run_id"]),
            ):
                store.add_evidence(
                    conn, f["fkey"], "rerun_disagreement", 0.9,
                    f"{row['name']} @ {row['head_sha'][:8]}: {sorted(outcomes)}",
                )
                added += 1

    # 2. Cross-commit recurrence: the same test failing on unrelated commits
    #    is a property of the test, not of any one diff.
    for row in conn.execute(
        """SELECT tf.fkey, COUNT(DISTINCT r.head_sha) AS shas
           FROM test_failures tf
           JOIN jobs j ON j.id=tf.job_id
           JOIN runs r ON r.id=j.run_id
           GROUP BY tf.fkey HAVING shas >= 2"""
    ):
        store.add_evidence(
            conn, row["fkey"], "cross_pr", min(0.85, 0.4 + 0.15 * row["shas"]),
            f"failed on {row['shas']} distinct commits",
        )
        added += 1

    # 3. Post-merge failures on main: CONTRIBUTING.md:353 points maintainers at
    #    exactly this signal.
    for row in conn.execute(
        """SELECT DISTINCT tf.fkey FROM test_failures tf
           JOIN jobs j ON j.id=tf.job_id
           JOIN runs r ON r.id=j.run_id
           WHERE r.head_branch='main'"""
    ):
        store.add_evidence(conn, row["fkey"], "main_failure", 0.6, "failed on main")
        added += 1

    conn.commit()
    print(f"recorded {added} evidence rows")


def cmd_summary(gh, conn, args):
    rows = store.failure_frequency(conn, limit=args.limit)
    if not rows:
        print("nothing ingested yet")
        return
    print(f"{'runs':>5} {'jobs':>5} {'shas':>5}  test")
    print("-" * 78)
    for r in rows:
        print(f"{r['runs']:>5} {r['jobs']:>5} {r['shas']:>5}  [{r['kind']}] {r['name'][:55]}")
        for e in store.evidence_for(conn, r["fkey"]):
            print(f"{'':>17}  ↳ {e['signal']} ({e['strength']}): {e['detail']}")


COMMANDS = {
    "runs": cmd_runs, "artifacts": cmd_artifacts, "issues": cmd_issues,
    "signals": cmd_signals, "summary": cmd_summary,
}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=sorted(COMMANDS))
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--db", default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    gh = GitHub(verbose=args.verbose)
    # `signals` and `summary` are pure SQL; only warn when the API is involved.
    if not gh.token and args.command in ("runs", "artifacts", "issues"):
        print("warning: GITHUB_TOKEN unset -- 60 requests/hour.\n", file=sys.stderr)

    conn = store.connect(args.db)
    try:
        COMMANDS[args.command](gh, conn, args)
    except RateLimited as e:
        print(f"\n{e}", file=sys.stderr)
        return 2
    finally:
        conn.commit()
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
