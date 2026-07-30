#!/usr/bin/env python3
"""Everything known about one failed CI job, as a single JSON document.

    python3 -m flakeagent.dossier --job 90896283356
    python3 -m flakeagent.dossier --run 30549428074 --out data/dossiers/
    python3 -m flakeagent.dossier --recent 20 --out data/dossiers/

This is the boundary between acquisition and everything built on top. A consumer
should never need to touch the database or the GitHub API -- one file holds the
failure, its context, and its history.

WHAT IT IS NOT
--------------
Nothing here is scored, ranked, or classified. There is no verdict field and no
confidence number. Every value is either fetched from GitHub or computed by
counting rows. Where a fact is absent it says so rather than guessing, and the
`provenance` block records when each part was fetched so a consumer can tell
fresh data from stale.

That separation is deliberate: judgement belongs to whatever reads this, and
mixing the two would make it impossible to tell an observation from an opinion.
"""

import argparse
import json
import sys
from pathlib import Path

from . import logslice, store

# Paths whose change cannot plausibly alter runtime behaviour. Used only to
# describe the diff -- the dossier states what the PR touched and lets the
# consumer decide what it means.
#
# `.github/workflows/` is deliberately NOT inert: workflow edits change how CI
# itself runs and absolutely can break a job.
INERT_PREFIXES = ("vendor/", "docs/", "test/tools/vendor/", ".github/ISSUE_TEMPLATE/")
INERT_FILES = ("go.mod", "go.sum", "LICENSE")
ACTIVE_PREFIXES = (".github/workflows/", "hack/", "contrib/")


def _inert(name):
    if name.startswith(ACTIVE_PREFIXES):
        return False
    if name.startswith(INERT_PREFIXES) or name in INERT_FILES:
        return True
    # Markdown anywhere is documentation, including .github/*_TEMPLATE.md.
    return name.lower().endswith((".md", ".txt"))


def _row(r):
    return dict(r) if r is not None else None


def job_section(conn, job_id):
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        return None
    d = _row(job)
    try:
        d["labels"] = json.loads(d.get("labels") or "[]")
    except (json.JSONDecodeError, TypeError):
        pass
    steps = [_row(s) for s in conn.execute(
        "SELECT * FROM job_steps WHERE job_id=? ORDER BY number", (job_id,))]
    d["steps"] = steps
    d["step_count"] = len(steps)
    return d


def failing_step_section(conn, job_id):
    """Which step failed. The cheapest root-cause signal available: it needs no
    log, no artifact, and no token beyond the jobs call itself."""
    rows = [_row(s) for s in conn.execute(
        """SELECT number, name, conclusion, started_at, completed_at
           FROM job_steps WHERE job_id=? AND conclusion='failure'
           ORDER BY number""", (job_id,))]
    if not rows:
        return {"found": False,
                "note": "no failing step recorded; job may predate step capture"}
    first = rows[0]
    return {"found": True, "steps": rows, "first": first,
            "note": "the step name distinguishes setup/infrastructure failures "
                    "from test failures without reading any log"}


def run_section(conn, run_id):
    run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    return _row(run)


def pull_request_section(conn, pr_number):
    if not pr_number:
        return {"present": False, "note": "run was not from a pull request"}
    files = [_row(f) for f in conn.execute(
        "SELECT * FROM pr_files WHERE pr_number=? ORDER BY filename", (pr_number,))]
    if not files:
        return {"number": pr_number, "present": True, "files_fetched": False,
                "note": "changed files not fetched; run `fetch prfiles`"}

    inert_files = [f for f in files if _inert(f["filename"])]
    return {
        "number": pr_number,
        "present": True,
        "files_fetched": True,
        "file_count": len(files),
        "files": files[:200],
        "additions": sum(f.get("additions") or 0 for f in files),
        "deletions": sum(f.get("deletions") or 0 for f in files),
        "all_paths_inert": len(inert_files) == len(files),
        "inert_file_count": len(inert_files),
        "note": "all changed paths are vendored/docs/dependency files"
                if len(inert_files) == len(files) else
                "the diff touches non-vendored source",
    }


def siblings_section(conn, run_id, job_id):
    """Other jobs in the same run. Many failing identically points away from the
    diff and toward the environment."""
    rows = [_row(j) for j in conn.execute(
        """SELECT id, name, conclusion, runner_name, started_at, completed_at
           FROM jobs WHERE run_id=? AND id!=? ORDER BY conclusion, name""",
        (run_id, job_id))]
    failed = [r for r in rows if r["conclusion"] == "failure"]
    return {"total": len(rows), "failed": len(failed),
            "failed_jobs": failed[:50],
            "conclusions": _counts(conn,
                "SELECT conclusion, COUNT(*) n FROM jobs WHERE run_id=? GROUP BY conclusion",
                (run_id,))}


def history_section(conn, job_name):
    """How this job name has fared across every run stored."""
    counts = _counts(conn,
        "SELECT conclusion, COUNT(*) n FROM jobs WHERE name=? GROUP BY conclusion",
        (job_name,))
    total = sum(counts.values())
    failures = counts.get("failure", 0)
    recent = [_row(r) for r in conn.execute(
        """SELECT j.id, j.conclusion, j.runner_name, r.head_sha, r.head_branch,
                  r.created_at
           FROM jobs j JOIN runs r ON r.id=j.run_id
           WHERE j.name=? ORDER BY r.created_at DESC LIMIT 20""", (job_name,))]
    by_runner = _counts(conn,
        """SELECT runner_name, COUNT(*) n FROM jobs
           WHERE name=? AND conclusion='failure' GROUP BY runner_name""",
        (job_name,))
    return {"observations": total, "failures": failures,
            "failure_rate": round(failures / total, 3) if total else None,
            "by_conclusion": counts,
            "failures_by_runner": by_runner,
            "recent": recent,
            "note": "computed only over runs stored locally, not all of CI history"}


def attempts_section(conn, head_sha, job_name):
    """Did the same commit produce a different outcome on another attempt?

    The strongest flake evidence there is: identical code, different result.
    Podman does not auto-retry (GINKGO_FLAKE_ATTEMPTS defaults to 0), so this
    only appears when a human pressed re-run -- which happened on 18% of runs in
    a 7-day sample.
    """
    runs = [_row(r) for r in conn.execute(
        """SELECT id, run_attempt, conclusion, created_at
           FROM runs WHERE head_sha=? ORDER BY run_attempt""", (head_sha,))]
    jobs = [_row(j) for j in conn.execute(
        """SELECT j.id, j.run_id, j.run_attempt, j.conclusion
           FROM jobs j JOIN runs r ON r.id=j.run_id
           WHERE r.head_sha=? AND j.name=? ORDER BY j.run_attempt""",
        (head_sha, job_name))]
    outcomes = {j["conclusion"] for j in jobs if j["conclusion"]}
    return {
        "runs_on_this_commit": len(runs),
        "max_attempt": max((r["run_attempt"] or 1) for r in runs) if runs else None,
        "job_outcomes_on_this_commit": sorted(outcomes),
        "disagreement": bool({"success", "failure"} <= outcomes),
        "runs": runs, "jobs": jobs,
        "note": "disagreement=true means this exact commit both passed and "
                "failed this job -- the code cannot be the difference",
    }


STOPWORDS = {"podman", "test", "tests", "should", "with", "when", "then",
             "from", "that", "this", "into", "flake", "flaky"}


def _terms(text):
    return {w.lower().strip(".,:()[]\"'`") for w in (text or "").split()
            if len(w) > 3}


def related_issues_section(conn, failing_tests, job_name, limit=8):
    """Existing `flakes` issues whose title overlaps the FAILING TEST's name.

    Matching on the *job* name does not work and was the original bug here: the
    job is called "macos machine applehv", which shares no distinctive word with
    any issue title, so every dossier came back with zero candidates and
    `known_fixes` was permanently empty.

    The failing test is "podman machine rm Remove running machine", which does
    match real issues (#23454 "hyperV machine rm: ...", #23472 "machine rm: ...").

    Still lexical overlap, still only a starting point -- but pointed at the
    right string. Each candidate records which test name produced it so a reader
    can judge the match rather than trust it.
    """
    sources = [(t["name"], "test") for t in (failing_tests or [])]
    if not sources:
        # Weak fallback. Rarely useful, but better than returning nothing
        # without saying why.
        sources = [(job_name, "job")]

    issues = conn.execute(
        "SELECT number, title, state FROM known_issues ORDER BY number DESC"
    ).fetchall()

    best = {}
    for text, origin in sources:
        terms = _terms(text) - STOPWORDS
        if not terms:
            continue
        for row in issues:
            overlap = terms & (_terms(row["title"]) - STOPWORDS)
            if len(overlap) < 2:
                continue
            prev = best.get(row["number"])
            if prev and len(prev["shared_terms"]) >= len(overlap):
                continue
            best[row["number"]] = {
                "number": row["number"], "title": row["title"],
                "state": row["state"], "shared_terms": sorted(overlap),
                "matched_on": text, "match_source": origin,
            }

    hits = sorted(best.values(), key=lambda h: -len(h["shared_terms"]))
    return {
        "candidates": hits[:limit],
        "matched_against": [s[0] for s in sources],
        "match_source": sources[0][1],
        "note": "lexical overlap with the failing test's name; a starting point, "
                "not a duplicate determination",
    }


def known_fixes_section(conn, related):
    """For each related issue, what a maintainer actually did about it.

    This is the only supervised signal in the dossier: the issue reports a
    symptom, the commit that closed it states the cause. It records what
    happened, not what we think -- e.g. issue #28940 "set ulimits flake - crun:
    clone: Resource temporarily unavailable" resolves to "test system: increase
    nproc ulimit to avoid flake", which identifies it as resource exhaustion
    without anyone classifying anything.
    """
    numbers = [c["number"] for c in related.get("candidates", [])]
    if not numbers:
        return {"issues": [], "note": "no related issues to look up"}

    out = []
    for n in numbers:
        issue = conn.execute(
            "SELECT number, title, state FROM known_issues WHERE number=?",
            (n,)).fetchone()
        fixes = [_row(f) for f in conn.execute(
            """SELECT sha, pr_number, message, author, committed_at, source
               FROM fix_commits WHERE issue_number=?
               ORDER BY committed_at DESC LIMIT 5""", (n,))]
        closed = conn.execute(
            """SELECT actor, created_at FROM issue_events
               WHERE issue_number=? AND event='closed'
               ORDER BY created_at DESC LIMIT 1""", (n,)).fetchone()
        out.append({
            "number": n,
            "title": issue["title"] if issue else None,
            "state": issue["state"] if issue else None,
            "closed_by": _row(closed),
            "fix_commits": fixes,
            "has_identified_fix": bool(fixes),
        })

    with_fix = sum(1 for o in out if o["has_identified_fix"])
    return {"issues": out, "with_identified_fix": with_fix,
            "note": "the fix commit states what was actually wrong; it is "
                    "evidence from a maintainer, not a classification by this tool"}


def _counts(conn, sql, params=()):
    return {str(r[0]): r[1] for r in conn.execute(sql, params)}


def build(conn, job_id, context_lines=logslice.FOCUS_AFTER):
    job = job_section(conn, job_id)
    if job is None:
        raise KeyError(f"job {job_id} not in the database")

    window = logslice.failing_step_window(conn, job_id)
    run = run_section(conn, job["run_id"]) or {}
    related = related_issues_section(
        conn, window.get("failing_tests"), job["name"])

    log_meta = conn.execute(
        "SELECT * FROM job_logs WHERE job_id=?", (job_id,)).fetchone()

    return {
        "schema_version": 1,
        "job": job,
        "failing_step": failing_step_section(conn, job_id),
        "log_window": {
            "available": window.get("available", False),
            "reason": window.get("reason"),
            "step": window.get("step"),
            "text": window.get("text", ""),
            "line_count": window.get("line_count"),
            "source_line_count": window.get("source_line_count"),
            "reduction_pct": window.get("reduction_pct"),
            "est_tokens": window.get("est_tokens"),
            "failure_markers": window.get("anchors"),
            "failing_tests": window.get("failing_tests", []),
        },
        "run": run,
        "pull_request": pull_request_section(conn, run.get("pr_number")),
        "siblings": siblings_section(conn, job["run_id"], job_id),
        "history": history_section(conn, job["name"]),
        "attempts": attempts_section(conn, run.get("head_sha"), job["name"]),
        "related_issues": related,
        "known_fixes": known_fixes_section(conn, related),
        "provenance": {
            "log_fetched_at": _row(log_meta).get("fetched_at") if log_meta else None,
            "log_bytes_raw": _row(log_meta).get("bytes_raw") if log_meta else None,
            "log_sha256": _row(log_meta).get("sha256") if log_meta else None,
            "note": "every field is fetched from the GitHub API or counted from "
                    "stored rows; nothing is inferred, scored, or classified",
        },
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--job", type=int)
    g.add_argument("--run", type=int, help="every failed job in this run")
    g.add_argument("--recent", type=int, help="the N most recent failed jobs")
    ap.add_argument("--out", help="write one JSON file per job into this directory")
    ap.add_argument("--blind", action="store_true",
                    help="withhold the evidence a human labels from, so a score "
                         "measures the classifier and not agreement with yourself")
    ap.add_argument("--db")
    args = ap.parse_args(argv)

    conn = store.connect(args.db)

    if args.job:
        ids = [args.job]
    elif args.run:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM jobs WHERE run_id=? AND conclusion='failure'", (args.run,))]
    else:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM jobs WHERE conclusion='failure' ORDER BY id DESC LIMIT ?",
            (args.recent,))]

    if not ids:
        print("no matching failed jobs", file=sys.stderr)
        return 1

    out_dir = Path(args.out) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    for job_id in ids:
        try:
            doc = build(conn, job_id)
        except KeyError as e:
            print(f"  ! {e}", file=sys.stderr)
            continue
        if args.blind:
            doc = blind(doc)
        text = json.dumps(doc, indent=2, default=str)
        if out_dir:
            path = out_dir / f"{doc['job']['run_id']}-{job_id}.json"
            path.write_text(text)
            print(f"  {path}  ({len(text):,} chars, "
                  f"~{doc['log_window']['est_tokens'] or 0:,} log tokens)")
        else:
            print(text)

    conn.close()
    return 0


# -- blinded view ---------------------------------------------------------

# Fields a human uses to label a failure, which must therefore be withheld from
# whatever is being scored.
#
# If you label from the log window and the classifier reads the same log window,
# a high score means "it reads logs the way I do", not "it is right" -- you can
# both be misled identically by a misleading log. Labelling from independent
# evidence and then withholding that evidence is what makes the score mean
# something.
BLIND_DROP_SECTIONS = ("known_fixes", "related_issues", "attempts", "history")
BLIND_DROP_PR_FIELDS = ("all_paths_inert", "inert_file_count", "note")


def blind(doc):
    """A dossier with the labeller's evidence removed. Returns a new dict."""
    out = {k: v for k, v in doc.items() if k not in BLIND_DROP_SECTIONS}

    pr = dict(out.get("pull_request") or {})
    for f in BLIND_DROP_PR_FIELDS:
        pr.pop(f, None)
    out["pull_request"] = pr

    sib = dict(out.get("siblings") or {})
    sib.pop("failed_jobs", None)      # names of sibling failures leak the pattern
    out["siblings"] = sib

    out["blinded"] = {
        "dropped": list(BLIND_DROP_SECTIONS),
        "note": "withheld so a label decided from this evidence stays "
                "independent of what the classifier sees",
    }
    return out


if __name__ == "__main__":
    raise SystemExit(main())
