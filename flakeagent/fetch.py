#!/usr/bin/env python3
"""Fetch pipeline: pull everything useful about Podman's CI onto local disk.

    python3 -m flakeagent.fetch status
    python3 -m flakeagent.fetch runs        --days 30
    python3 -m flakeagent.fetch jobs
    python3 -m flakeagent.fetch artifacts   [--download] [--max-bytes 500M]
    python3 -m flakeagent.fetch annotations
    python3 -m flakeagent.fetch issues      [--label flakes | --all-issues]
    python3 -m flakeagent.fetch comments
    python3 -m flakeagent.fetch all         --days 30

Acquisition only. Nothing here classifies, scores, or judges anything -- that is
a later slice. The job is to get correct, complete data onto disk cheaply.

SAFETY
------
Every request goes through flakeagent.gh.GitHub, which is GET-only by
construction, revalidates with ETags (304s cost no quota), stops at a reserve
floor instead of draining the token, and backs off on secondary rate limits.
Nothing in this package can modify anything upstream. Use a read-only token.

Every fetcher is idempotent and resumable: interrupt any of them and re-run.
"""

import argparse
import gzip
import hashlib
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import logslice, store
from .corpus import QUERY as FLAKES_QUERY, extract_blocks, tag_block
from .gh import GITHUB_API, GitHub, RateLimited
from .ingest import parse_job_name

REPO = "podman-container-tools/podman"

# Workflows that actually produce test failures, measured 2026-07-30 across all
# 21 active workflows (see docs/FETCH_AUDIT.md):
#
#   ci.yml             1,076 runs,  429 failed  <- the main matrix
#   validate.yml          25 runs,   15 failed  <- recently split out of ci.yml
#   unit-tests.yml        10 runs,    9 failed  <- recently split out of ci.yml
#   zizmor.yml         3,251 runs,   20 failed  <- security lint; 3,251 pages for 20
#   machine-os-pr.yml     58 runs,    0 failed  <- has never failed
#
# machine-os-pr.yml was in this list and was costing calls for nothing.
# lima.yml is workflow_call-only: its jobs surface inside ci.yml runs.
DEFAULT_WORKFLOWS = ["ci.yml", "validate.yml", "unit-tests.yml"]

SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([KMG]?)B?$", re.I)
SIZE_MULT = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3}


def parse_size(text):
    m = SIZE_RE.match(str(text).strip())
    if not m:
        raise argparse.ArgumentTypeError(f"bad size: {text!r} (try 500M, 2G, 1048576)")
    return int(float(m.group(1)) * SIZE_MULT[m.group(2).upper()])


def since(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


MERGE_TITLE_RE = re.compile(r"Merge pull request #(\d+)\b")


def pr_number_of(run):
    """Best-effort PR number for a run. Returns None rather than a wrong answer.

    The obvious source -- run["pull_requests"] -- is unreliable in both
    directions, measured on a 7-day sample of this repo:

      * On `push` events it is populated but does NOT hold the merged PR. A
        push to main titled "Merge pull request #29344 from Luap99/contributing"
        carried pull_requests[0].number == 10, an unrelated PR. Trusting it
        attached a stranger's 44-file diff to an unrelated failure.
      * On `pull_request` events it was populated for 1 of 160 runs, because
        GitHub omits it for pull requests opened from forks -- which is how
        essentially all outside contribution to Podman arrives.

    So: for push events parse the merge commit's title, which is authoritative
    and free. For pull_request events use the array when present. Otherwise
    concede None; `fetch prfiles` can resolve the rest via the commit->PR
    endpoint, and a null is far better than a confident wrong number.
    """
    event = run.get("event")

    if event == "push":
        m = MERGE_TITLE_RE.search(run.get("display_title") or "")
        return int(m.group(1)) if m else None

    prs = run.get("pull_requests") or []
    if prs and isinstance(prs[0], dict) and prs[0].get("number"):
        return prs[0]["number"]

    if event == "pull_request":
        # Fork PR: head_branch is the source branch name, not a number.
        return None
    return None


# -- fetchers -------------------------------------------------------------

def fetch_runs(gh, conn, args):
    """Workflow runs in the rolling window.

    --status all is the default on purpose: rerun-disagreement needs a success
    to compare a failure against, so fetching only failures destroys the
    strongest ground-truth signal available.
    """
    cutoff = since(args.days)
    total = 0

    for wf in args.workflows:
        n = 0
        params = {"created": f">={cutoff}"}
        if args.status != "all":
            params["status"] = args.status

        print(f"  {wf}: runs created >= {cutoff}"
              f"{'' if args.status == 'all' else f' (status={args.status})'}")
        try:
            for run in gh.paginate(f"/repos/{REPO}/actions/workflows/{wf}/runs",
                                   key="workflow_runs", max_pages=args.max_pages,
                                   **params):
                store.upsert_run(conn, run, pr_number_of(run))
                n += 1
                if n % 100 == 0:
                    conn.commit()
                    print(f"    ...{n}")
        except RateLimited:
            conn.commit()
            raise
        conn.commit()
        print(f"    {n} runs")
        total += n

    print(f"\n{total} runs stored. {gh.stats()}")


def fetch_jobs(gh, conn, args):
    """Jobs for stored runs, including every attempt -- and their steps.

    job.steps[] is the point of this fetcher. It arrives free inside the jobs
    response and says which step failed, which is infra-vs-test attribution
    without touching a log.
    """
    if args.run:
        rows = [{"id": args.run, "run_attempt": 1}]
    else:
        # action_required (fork PRs awaiting approval) and cancelled runs never
        # produced job data -- in a 7-day sample that was 84 of 184 runs, 46% of
        # the calls this loop would otherwise make.
        where = ("" if args.include_all
                 else "WHERE conclusion IN ('failure','success')")
        rows = conn.execute(
            f"SELECT id, run_attempt FROM runs {where} ORDER BY id DESC LIMIT ?",
            (args.limit,)).fetchall()

    if not rows:
        print("no runs stored; run `fetch runs` first")
        return

    jobs = steps = 0
    for i, row in enumerate(rows, 1):
        run_id, attempts = row["id"], (row["run_attempt"] or 1)

        for attempt in range(1, attempts + 1):
            path = (f"/repos/{REPO}/actions/runs/{run_id}/attempts/{attempt}/jobs"
                    if attempts > 1 else
                    f"/repos/{REPO}/actions/runs/{run_id}/jobs")
            try:
                payload = gh.get(path, per_page=100)
            except RateLimited:
                conn.commit()
                raise
            except Exception as e:
                print(f"  ! run {run_id} attempt {attempt}: {e}", file=sys.stderr)
                continue

            for job in payload.get("jobs", []):
                job["run_id"] = run_id
                job.setdefault("run_attempt", attempt)
                store.upsert_job(conn, job, parse_job_name(job["name"]))
                store.add_steps(conn, job["id"], job.get("steps"))
                jobs += 1
                steps += len(job.get("steps") or [])

        conn.commit()
        if i % 25 == 0:
            print(f"  ...{i}/{len(rows)} runs, {jobs} jobs, {steps} steps")

    print(f"\n{jobs} jobs, {steps} steps across {len(rows)} runs. {gh.stats()}")


def fetch_artifacts(gh, conn, args):
    """Artifact metadata always; content only with --download.

    A full failing run carries ~46 journals at 155KB-997KB each (~32MB), so
    content is opt-in and capped.
    """
    rows = conn.execute(
        "SELECT id FROM runs WHERE conclusion != 'success' ORDER BY id DESC LIMIT ?",
        (args.limit,)).fetchall()
    if not rows:
        print("no runs stored; run `fetch runs` first")
        return

    cache = gh.cache_dir.parent / "artifacts"
    meta = downloaded = skipped_expired = 0
    bytes_pulled = 0

    for i, row in enumerate(rows, 1):
        run_id = row["id"]
        try:
            payload = gh.get(f"/repos/{REPO}/actions/runs/{run_id}/artifacts",
                             per_page=100)
        except RateLimited:
            conn.commit()
            raise
        except Exception as e:
            print(f"  ! run {run_id}: {e}", file=sys.stderr)
            continue

        for art in payload.get("artifacts", []):
            local = None
            if art.get("expired"):
                skipped_expired += 1
            elif args.download and _wanted(art["name"], args.pattern):
                size = art.get("size_in_bytes") or 0
                if args.max_bytes and bytes_pulled + size > args.max_bytes:
                    print(f"    size cap reached ({bytes_pulled:,} B); "
                          "stopping downloads")
                    args.download = False
                else:
                    try:
                        local = gh.download(art["archive_download_url"],
                                            cache / f"{run_id}-{art['id']}.zip")
                        bytes_pulled += size
                        downloaded += 1
                    except RateLimited:
                        conn.commit()
                        raise
                    except Exception as e:
                        print(f"    ! {art['name']}: {e}", file=sys.stderr)
            store.upsert_artifact(conn, run_id, art, local)
            meta += 1

        conn.commit()
        if i % 25 == 0:
            print(f"  ...{i}/{len(rows)} runs, {meta} artifacts")

    print(f"\n{meta} artifact records, {downloaded} downloaded "
          f"({bytes_pulled:,} bytes), {skipped_expired} expired/skipped. {gh.stats()}")


def _wanted(name, pattern):
    return True if not pattern else bool(re.search(pattern, name))


def fetch_logs(gh, conn, args):
    """Raw job logs for failed jobs -- the primary content source.

    ~500KB per failed job, containing the actual ginkgo/bats output, versus
    ~32MB of journal artifacts per run. Stored gzipped (~10x smaller).

    The request 302s to Azure blob storage; gh.py strips Authorization on the
    cross-host hop, without which this returns 401.
    """
    rows = conn.execute(
        """SELECT id, run_id, name FROM jobs
           WHERE conclusion='failure' ORDER BY id DESC LIMIT ?""",
        (args.limit,)).fetchall()
    if not rows:
        print("no failed jobs stored; run `fetch jobs` first")
        return

    out_dir = Path(__file__).resolve().parent.parent / "data" / "logs"
    out_dir.mkdir(parents=True, exist_ok=True)
    repo_root = out_dir.parent.parent

    stored = skipped = 0
    raw_total = gz_total = 0

    for i, row in enumerate(rows, 1):
        dest = out_dir / f"{row['run_id']}-{row['id']}.log.gz"
        if dest.exists() and not args.refresh:
            skipped += 1
            continue

        if args.max_bytes and raw_total >= args.max_bytes:
            print(f"    size cap reached ({raw_total:,} raw bytes); stopping")
            break

        try:
            # Not gh.get(): the response is plain text, not JSON, and it 302s to
            # blob storage. gh._get handles the redirect and header stripping.
            _, data, _ = gh._get(
                f"{GITHUB_API}/repos/{REPO}/actions/jobs/{row['id']}/logs")
        except RateLimited:
            conn.commit()
            raise
        except Exception as e:
            print(f"  ! job {row['id']} ({row['name'][:40]}): {e}", file=sys.stderr)
            continue

        text = data.decode("utf-8", errors="replace").lstrip("﻿")
        lines = text.splitlines()
        first_ts, last_ts = logslice.log_bounds(lines)

        with gzip.open(dest, "wt", encoding="utf-8") as fh:
            fh.write(text)

        meta = {
            "bytes_raw": len(data),
            "bytes_stored": dest.stat().st_size,
            "line_count": len(lines),
            "first_ts": first_ts.isoformat() if first_ts else None,
            "last_ts": last_ts.isoformat() if last_ts else None,
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        raw_total += meta["bytes_raw"]
        gz_total += meta["bytes_stored"]

        store.upsert_job_log(conn, row["id"], row["run_id"],
                             dest.relative_to(repo_root), meta)
        stored += 1
        conn.commit()
        if i % 10 == 0:
            print(f"  ...{i}/{len(rows)} jobs, {stored} logs")

    ratio = f"{gz_total / raw_total:.1%}" if raw_total else "n/a"
    print(f"\n{stored} logs stored, {skipped} already present. "
          f"{raw_total:,} raw -> {gz_total:,} gzipped ({ratio}). {gh.stats()}")


def resolve_pr_numbers(gh, conn, args):
    """Fill in pr_number for failed runs that lack one, via commit -> PR.

    Needed because GitHub omits `pull_requests` for fork PRs, which is nearly
    all of Podman's inbound contribution -- 159 of 160 pull_request runs in a
    7-day sample had no number attached. One call per commit.
    """
    rows = conn.execute(
        """SELECT id, head_sha FROM runs
           WHERE pr_number IS NULL AND conclusion='failure'
             AND event='pull_request'
           ORDER BY id DESC LIMIT ?""", (args.limit,)).fetchall()
    if not rows:
        return 0

    resolved = 0
    for i, row in enumerate(rows, 1):
        try:
            prs = gh.get(f"/repos/{REPO}/commits/{row['head_sha']}/pulls",
                         per_page=10)
        except RateLimited:
            conn.commit()
            raise
        except Exception as e:
            print(f"  ! run {row['id']}: {e}", file=sys.stderr)
            continue
        if isinstance(prs, list) and prs:
            conn.execute("UPDATE runs SET pr_number=? WHERE id=?",
                         (prs[0]["number"], row["id"]))
            resolved += 1
        if i % 25 == 0:
            conn.commit()
            print(f"  ...resolved {resolved}/{i}")
    conn.commit()
    print(f"  resolved {resolved} PR numbers from commit SHAs")
    return resolved


def fetch_prfiles(gh, conn, args):
    """Which files each PR changed -- evidence for 'could this diff have done it?'"""
    resolve_pr_numbers(gh, conn, args)

    rows = conn.execute(
        """SELECT DISTINCT pr_number FROM runs
           WHERE pr_number IS NOT NULL AND conclusion='failure'
           ORDER BY pr_number DESC LIMIT ?""", (args.limit,)).fetchall()
    if not rows:
        print("no failed runs with a PR number; run `fetch runs` first")
        return

    prs = files = 0
    for i, row in enumerate(rows, 1):
        n = row["pr_number"]
        try:
            for f in gh.paginate(f"/repos/{REPO}/pulls/{n}/files",
                                 per_page=100, max_pages=3):
                store.add_pr_file(conn, n, f)
                files += 1
        except RateLimited:
            conn.commit()
            raise
        except Exception as e:
            print(f"  ! PR #{n}: {e}", file=sys.stderr)
            continue
        prs += 1
        conn.commit()
        if i % 25 == 0:
            print(f"  ...{i}/{len(rows)} PRs, {files} files")

    print(f"\n{prs} PRs, {files} changed files. {gh.stats()}")


def fetch_annotations(gh, conn, args):
    """Check-run annotations for failed jobs. Often thin, occasionally the
    actual error message. Cheap enough to take."""
    rows = conn.execute(
        """SELECT id, check_run_url FROM jobs
           WHERE conclusion='failure' AND check_run_url IS NOT NULL
           ORDER BY id DESC LIMIT ?""", (args.limit,)).fetchall()
    if not rows:
        print("no failed jobs with a check_run_url; run `fetch jobs` first")
        return

    found = 0
    for i, row in enumerate(rows, 1):
        try:
            anns = gh.get(f"{row['check_run_url']}/annotations", per_page=50)
        except RateLimited:
            conn.commit()
            raise
        except Exception as e:
            print(f"  ! job {row['id']}: {e}", file=sys.stderr)
            continue
        for a in anns if isinstance(anns, list) else []:
            store.add_annotation(conn, row["id"], a)
            found += 1
        conn.commit()
        if i % 50 == 0:
            print(f"  ...{i}/{len(rows)} jobs, {found} annotations")

    print(f"\n{found} annotations across {len(rows)} failed jobs. {gh.stats()}")


def fetch_issues(gh, conn, args):
    """Issues plus their pasted log blocks (reuses corpus.py's extraction)."""
    query = FLAKES_QUERY if not args.all_issues else f"repo:{REPO} is:issue"
    issues = samples = 0

    for issue in gh.paginate("/search/issues", key="items", q=query,
                             per_page=100, max_pages=args.max_pages):
        store.upsert_issue(conn, issue)
        issues += 1
        for idx, block in enumerate(extract_blocks(issue.get("body"))):
            era, suite = tag_block(block)
            cur = conn.execute(
                """INSERT OR IGNORE INTO corpus_samples
                   (issue_number, source, block_index, era, suite, text)
                   VALUES (?,'body',?,?,?,?)""",
                (issue["number"], idx, era, suite, block))
            samples += cur.rowcount
        if issues % 100 == 0:
            conn.commit()
            print(f"  ...{issues} issues")
    conn.commit()
    print(f"\n{issues} issues, {samples} new log samples. {gh.stats()}")


def fetch_comments(gh, conn, args):
    """Comments on stored issues -- one request per issue, so token-gated."""
    rows = conn.execute(
        "SELECT number FROM known_issues ORDER BY number DESC LIMIT ?",
        (args.limit,)).fetchall()
    if not rows:
        print("no issues stored; run `fetch issues` first")
        return
    if not gh.token:
        print("warning: no GITHUB_TOKEN — this is one request per issue and "
              "will hit the 60/hr limit almost immediately.\n", file=sys.stderr)

    comments = samples = 0
    for i, row in enumerate(rows, 1):
        n = row["number"]
        try:
            for c in gh.paginate(f"/repos/{REPO}/issues/{n}/comments",
                                 per_page=100, max_pages=3):
                store.upsert_comment(conn, n, c)
                comments += 1
                for idx, block in enumerate(extract_blocks(c.get("body"))):
                    era, suite = tag_block(block)
                    cur = conn.execute(
                        """INSERT OR IGNORE INTO corpus_samples
                           (issue_number, source, block_index, era, suite, text)
                           VALUES (?,?,?,?,?,?)""",
                        (n, f"comment:{c['id']}", idx, era, suite, block))
                    samples += cur.rowcount
        except RateLimited:
            conn.commit()
            raise
        except Exception as e:
            print(f"  ! #{n}: {e}", file=sys.stderr)
        conn.commit()
        if i % 50 == 0:
            print(f"  ...{i}/{len(rows)} issues, {comments} comments")

    print(f"\n{comments} comments, {samples} new log samples. {gh.stats()}")


ISSUE_REF_RE = re.compile(r"#(\d{4,5})\b")

# The search API allows 30 requests/minute authenticated -- a tenth of the core
# pool's effective rate, and the only place in this project that hits it. Pace
# rather than discover the secondary limit the hard way.
SEARCH_INTERVAL = 2.2


def fetch_timeline(gh, conn, args):
    """Issue timeline events: labelling, closing, cross-references.

    The precise half of fix-commit linkage -- a `closed` or `cross-referenced`
    event is the maintainer explicitly connecting an issue to the work that
    resolved it, rather than us inferring it from a commit message.
    """
    rows = conn.execute(
        "SELECT number FROM known_issues ORDER BY number DESC LIMIT ?",
        (args.limit,)).fetchall()
    if not rows:
        print("no issues stored; run `fetch issues` first")
        return

    events = linked = 0
    for i, row in enumerate(rows, 1):
        n = row["number"]
        try:
            for ev in gh.paginate(f"/repos/{REPO}/issues/{n}/timeline",
                                  per_page=100, max_pages=3):
                store.add_issue_event(conn, n, ev)
                events += 1
                # A cross-reference from a PR, or a commit that closed the
                # issue, is a candidate fix.
                sha = ev.get("commit_id")
                src = ((ev.get("source") or {}).get("issue") or {})
                pr = src.get("number") if src.get("pull_request") else None
                if sha or pr:
                    # `source.issue.title` exists only on `cross-referenced`
                    # events. A `referenced` event -- a commit mentioning the
                    # issue, which is the fix-commit case and the majority --
                    # has no source, and falling back to `ev["event"]` stored
                    # the literal string "referenced" as the commit message for
                    # 1,593 of 1,928 rows. Leave it NULL instead: an absent
                    # message is honest and `backfill-fixes` can find it by SHA,
                    # whereas a placeholder is indistinguishable from a real one.
                    store.add_fix_commit(
                        conn, sha or f"pr-{pr}", n, "timeline", pr_number=pr,
                        message=src.get("title"),
                        author=(ev.get("actor") or {}).get("login"),
                        committed_at=ev.get("created_at"))
                    linked += 1
        except RateLimited:
            conn.commit()
            raise
        except Exception as e:
            print(f"  ! #{n}: {e}", file=sys.stderr)
        conn.commit()
        if i % 25 == 0:
            print(f"  ...{i}/{len(rows)} issues, {events} events, {linked} links")

    print(f"\n{events} timeline events, {linked} fix candidates. {gh.stats()}")


def fetch_backfill_fixes(gh, conn, args):
    """Fill in commit messages for fix links that only ever stored a SHA.

    `fetch timeline` recorded the *event type* where the commit message belonged
    (see the note in `fetch_timeline`), so most fix links carry the placeholder
    "referenced" or nothing at all. The SHA was always stored correctly, so the
    message is one `/commits/{sha}` call away -- and those responses are cached
    and ETag-revalidated like everything else, so re-running costs almost
    nothing.

    This matters more than its size suggests. The maintainer's own fix is the
    strongest independent evidence the dossier can carry: labelling `resource
    _exhaustion` because a maintainer wrote "increase nproc ulimit to avoid
    flake" is transcription, not opinion, and it is the only kind of label that
    stays meaningful when the thing being scored also reads the log.
    """
    rows = conn.execute(
        """SELECT DISTINCT sha FROM fix_commits
           WHERE length(sha) = 40
             AND (message IS NULL OR message = '' OR message = 'referenced')
           LIMIT ?""",
        (args.limit,)).fetchall()
    if not rows:
        print("no fix commits are missing a message")
        return

    print(f"{len(rows)} commit messages to backfill")
    filled = missing = 0
    for i, row in enumerate(rows, 1):
        sha = row["sha"]
        try:
            commit = gh.get(f"/repos/{REPO}/commits/{sha}")
        except RateLimited:
            conn.commit()
            raise
        except Exception as e:
            print(f"  ! {sha[:9]}: {e}", file=sys.stderr)
            missing += 1
            continue

        info = commit.get("commit") or {}
        message = (info.get("message") or "").strip()
        if not message:
            missing += 1
            continue

        conn.execute(
            """UPDATE fix_commits SET message = ?, author = COALESCE(author, ?),
                      committed_at = COALESCE(committed_at, ?)
               WHERE sha = ?""",
            (message[:4000],
             ((info.get("author") or {}).get("name")
              or (commit.get("author") or {}).get("login")),
             (info.get("author") or {}).get("date"),
             sha))
        filled += 1
        conn.commit()
        if i % 50 == 0:
            print(f"  ...{i}/{len(rows)}, {filled} filled")

    print(f"\n{filled} messages backfilled, {missing} unavailable. {gh.stats()}")


def fetch_fixes(gh, conn, args):
    """Commits that fixed a flake, linked to the issue that reported it.

    The only supervised ground truth available: a `flakes` issue says a test was
    flaky, and the commit closing it says what was actually wrong. Everything
    else this project stores is unlabelled observation.

    Runs the broad half -- commit search parsed for issue references. Pair it
    with `fetch timeline` for the precise half.
    """
    found = linked = 0
    pages = max(1, args.max_pages)

    for page in range(1, pages + 1):
        try:
            payload = gh.get("/search/commits", q=f"repo:{REPO} flake",
                             per_page=100, page=page)
        except RateLimited:
            conn.commit()
            raise
        except Exception as e:
            print(f"  ! search page {page}: {e}", file=sys.stderr)
            break

        items = payload.get("items", [])
        if not items:
            break
        if page == 1:
            print(f"  {payload.get('total_count')} commits mention 'flake'")

        for item in items:
            commit = item.get("commit", {})
            message = commit.get("message", "")
            refs = {int(m) for m in ISSUE_REF_RE.findall(message)}
            found += 1
            if not refs:
                continue
            for ref in refs:
                # A reference may be to the issue or to the PR; store both and
                # let the reader decide. Only keep refs we know as issues.
                known = conn.execute(
                    "SELECT 1 FROM known_issues WHERE number=?", (ref,)).fetchone()
                store.add_fix_commit(
                    conn, item["sha"], ref if known else None, "search",
                    pr_number=None if known else ref,
                    message=message,
                    author=(commit.get("author") or {}).get("name"),
                    committed_at=(commit.get("author") or {}).get("date"))
                linked += 1
        conn.commit()
        print(f"  ...page {page}: {found} commits scanned, {linked} references")

        if len(items) < 100:
            break
        time.sleep(SEARCH_INTERVAL)   # stay under 30 search requests/minute

    print(f"\n{found} commits scanned, {linked} issue references stored. {gh.stats()}")


def fetch_all(gh, conn, args):
    for name, fn in [("runs", fetch_runs), ("jobs", fetch_jobs),
                     ("logs", fetch_logs), ("prfiles", fetch_prfiles),
                     ("artifacts", fetch_artifacts),
                     ("annotations", fetch_annotations), ("issues", fetch_issues),
                     ("timeline", fetch_timeline), ("fixes", fetch_fixes)]:
        print(f"\n{'=' * 60}\n{name}\n{'=' * 60}")
        fn(gh, conn, args)


def fetch_status(gh, conn, args):
    print("database")
    print("-" * 52)
    for table, label in [
        ("runs", "workflow runs"), ("jobs", "jobs"), ("job_steps", "job steps"),
        ("artifacts", "artifacts (metadata)"), ("annotations", "annotations"),
        ("job_logs", "job logs (gzipped)"), ("pr_files", "PR changed files"),
        ("known_issues", "issues"), ("issue_comments", "issue comments"),
        ("issue_events", "issue timeline events"),
        ("fix_commits", "fix commits (ground truth)"),
        ("corpus_samples", "log samples from issues"),
        ("test_failures", "parsed test failures"),
    ]:
        try:
            n = conn.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()["n"]
        except Exception:
            n = "-"
        print(f"  {label:<28}{n:>10}")

    dl = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(size_in_bytes),0) b FROM artifacts "
        "WHERE local_path IS NOT NULL").fetchone()
    print(f"  {'artifacts downloaded':<28}{dl['n']:>10}  ({dl['b']:,} bytes)")

    row = conn.execute("SELECT MIN(created_at) a, MAX(created_at) b FROM runs").fetchone()
    if row and row["a"]:
        print(f"\nrun window: {row['a'][:10]} -> {row['b'][:10]}")

    # Ground-truth coverage: how many flake reports have an identified fix.
    linked = conn.execute(
        "SELECT COUNT(DISTINCT issue_number) n FROM fix_commits "
        "WHERE issue_number IS NOT NULL").fetchone()["n"]
    issues = conn.execute("SELECT COUNT(*) n FROM known_issues").fetchone()["n"]
    if issues:
        print(f"ground truth: {linked}/{issues} issues have an identified fix "
              f"({linked / issues:.0%})")

    fails = conn.execute(
        """SELECT s.name, COUNT(*) n FROM job_steps s
           WHERE s.conclusion='failure' GROUP BY s.name ORDER BY n DESC LIMIT 10"""
    ).fetchall()
    if fails:
        print("\nmost frequently failing steps (infra vs test, no logs needed):")
        for r in fails:
            print(f"  {r['n']:>5}  {r['name'][:60]}")

    print("\napi budget")
    print("-" * 52)
    try:
        rem, lim, reset = gh.budget()
        import time as _t
        mins = max(0, int(reset - _t.time())) // 60
        print(f"  {rem}/{lim} remaining, resets in {mins}m")
        if lim <= 60:
            print("  WARNING: unauthenticated. A 30-day fetch needs ~1,200-1,500\n"
                  "           requests. Set GITHUB_TOKEN (read-only, public repos).")
    except Exception as e:
        print(f"  could not read budget: {e}")


COMMANDS = {
    "runs": fetch_runs, "jobs": fetch_jobs, "logs": fetch_logs,
    "prfiles": fetch_prfiles, "artifacts": fetch_artifacts,
    "annotations": fetch_annotations, "issues": fetch_issues,
    "comments": fetch_comments, "timeline": fetch_timeline,
    "fixes": fetch_fixes, "backfill-fixes": fetch_backfill_fixes,
    "all": fetch_all, "status": fetch_status,
}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=sorted(COMMANDS))
    ap.add_argument("--days", type=int, default=30, help="rolling window (runs)")
    ap.add_argument("--status", default="all", choices=["all", "failure", "success"],
                    help="run status filter; 'all' is needed for rerun signals")
    ap.add_argument("--workflows", type=lambda s: [x.strip() for x in s.split(",")],
                    default=DEFAULT_WORKFLOWS)
    ap.add_argument("--run", type=int, help="jobs: a single run id")
    ap.add_argument("--include-all", "--all-runs", dest="include_all",
                    action="store_true",
                    help="jobs/artifacts: include action_required and cancelled "
                         "runs too (they have no job data; ~46%% of runs)")
    ap.add_argument("--refresh", action="store_true",
                    help="logs: re-download even if the file already exists")
    ap.add_argument("--all-issues", action="store_true",
                    help="issues: every issue, not just label:flakes")
    ap.add_argument("--download", action="store_true",
                    help="artifacts: also fetch content (needs a token)")
    ap.add_argument("--max-bytes", type=parse_size, default=parse_size("500M"),
                    help="artifacts: total download cap (default 500M)")
    ap.add_argument("--pattern", help="artifacts: only download names matching this regex")
    ap.add_argument("--limit", type=int, default=500, help="max rows to process")
    ap.add_argument("--max-pages", type=int, default=10)
    ap.add_argument("--reserve", type=int, default=100,
                    help="stop with this much API quota still unspent")
    ap.add_argument("--revalidate", action="store_true",
                    help="re-check cached entries with If-None-Match (304s are free)")
    ap.add_argument("--db")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    gh = GitHub(verbose=args.verbose, reserve=args.reserve,
                revalidate=args.revalidate)
    if not gh.token and args.command not in ("status",):
        print("note: no GITHUB_TOKEN — 60 requests/hour. Set a read-only token "
              "for anything beyond a small sample.\n", file=sys.stderr)

    conn = store.connect(args.db)
    try:
        COMMANDS[args.command](gh, conn, args)
    except RateLimited as e:
        print(f"\nstopped: {e}", file=sys.stderr)
        print("progress is saved — re-run the same command to resume.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted — progress is saved, re-run to resume.", file=sys.stderr)
        return 130
    finally:
        conn.commit()
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
