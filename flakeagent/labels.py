#!/usr/bin/env python3
"""Assign ground-truth categories to flake reports, by hand, from evidence.

    python3 -m flakeagent.labels list                  # unlabelled, fix identified
    python3 -m flakeagent.labels show  --issue 28940   # the evidence for one
    python3 -m flakeagent.labels set   --issue 28940 --category resource_exhaustion
    python3 -m flakeagent.labels stats                 # coverage + distribution
    python3 -m flakeagent.labels export --out data/gold.json

WHY BY HAND
-----------
This tool deliberately suggests nothing. It puts two things side by side --
what the reporter observed, and what the maintainer changed to fix it -- and
records the category you choose.

    #28940  "podman update - set ulimits flake -
             crun: clone: Resource temporarily unavailable"
    fix     "test system: increase nproc ulimit to avoid flake"

A human reads that pair and concludes `resource_exhaustion` in about two
seconds. A keyword rule would guess it too, and would also confidently guess
wrong on the ambiguous ones -- and those are exactly the cases a measured
accuracy number lives or dies on. An eval set contaminated by the same kind of
heuristic the classifier uses cannot tell you whether the classifier works.

So: the tool presents evidence, the judgement is yours, and `gold_labels` holds
only decisions a person actually made.
"""

import argparse
import json
import sys

from . import store
from .taxonomy import CATEGORIES, HINTS  # noqa: F401  (re-exported)


def _fixes(conn, number):
    return conn.execute(
        """SELECT sha, pr_number, message, author, committed_at, source
           FROM fix_commits WHERE issue_number=?
           ORDER BY committed_at DESC LIMIT 6""", (number,)).fetchall()


def cmd_list(conn, args):
    """Issues worth labelling: a flake report with an identified fix, no label yet."""
    # Order by how *readable* the evidence is, not how much of it there is.
    #
    # A search-sourced row carries the commit's actual message ("increase nproc
    # ulimit to avoid flake"), which is the whole point. Timeline rows are often
    # bare `referenced` events with no message, and a popular issue accumulates
    # dozens of them -- #24147 had 76. Sorting by count puts the noisiest issues
    # first and buries the ones a human can label in two seconds.
    rows = conn.execute(
        """SELECT k.number, k.title, k.state,
                  COUNT(f.sha) AS fixes,
                  SUM(CASE WHEN f.source='search' THEN 1 ELSE 0 END) AS described
           FROM known_issues k
           JOIN fix_commits f ON f.issue_number = k.number
           LEFT JOIN gold_labels g ON g.issue_number = k.number
           WHERE g.fkey IS NULL
           GROUP BY k.number
           HAVING fixes <= 8
           ORDER BY described DESC, fixes ASC, k.number DESC
           LIMIT ?""", (args.limit,)).fetchall()
    if not rows:
        print("nothing to label -- either no fix_commits yet "
              "(`fetch fixes`, `fetch timeline`) or everything is labelled")
        return
    print(f"{len(rows)} issue(s) with an identified fix and no label:\n")
    for r in rows:
        mark = "*" if r["described"] else " "
        print(f"  {mark} #{r['number']:<6} [{r['state']:<6}] {r['fixes']} fix(es)  "
              f"{r['title'][:64]}")
    print("\n  * = the fix commit's message is available, so the cause is stated")
    print(f"  python3 -m flakeagent.labels show --issue {rows[0]['number']}")


def _independent_evidence(conn, job_id):
    """Evidence about a failure that does NOT come from reading its log.

    This is the whole point of labelling dossiers this way. If you decide a
    category by reading the log window, and the classifier decides by reading
    the same log window, a high score means "the model reads logs the way I do"
    -- not "the model is right". You would both be misled by the same
    misleading log.

    So label from here, and hand the classifier the blinded view
    (`dossier --blind`), which withholds exactly these fields.
    """
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        return None
    run = conn.execute("SELECT * FROM runs WHERE id=?", (job["run_id"],)).fetchone()

    # Same commit, different outcome -- proof of a flake, no log involved.
    outcomes = {r["conclusion"] for r in conn.execute(
        """SELECT j.conclusion FROM jobs j JOIN runs r ON r.id=j.run_id
           WHERE r.head_sha=? AND j.name=?""",
        (run["head_sha"] if run else None, job["name"]))}

    # Did this test fail on unrelated commits too?
    spread = conn.execute(
        """SELECT COUNT(DISTINCT r.head_sha) n FROM jobs j JOIN runs r ON r.id=j.run_id
           WHERE j.name=? AND j.conclusion='failure'""", (job["name"],)).fetchone()["n"]

    # Could the diff even have caused it?
    files = conn.execute(
        "SELECT filename FROM pr_files WHERE pr_number=?",
        (run["pr_number"] if run else None,)).fetchall()

    # Which step -- setup vs test -- and how long it ran.
    step = conn.execute(
        """SELECT number, name, started_at, completed_at FROM job_steps
           WHERE job_id=? AND conclusion='failure' ORDER BY number LIMIT 1""",
        (job_id,)).fetchone()

    # Sibling jobs failing identically points at the environment.
    sib = conn.execute(
        """SELECT COUNT(*) t, SUM(conclusion='failure') f FROM jobs
           WHERE run_id=? AND id!=?""", (job["run_id"], job_id)).fetchone()

    return {"job": job, "run": run, "step": step, "outcomes": outcomes,
            "spread": spread, "files": [f["filename"] for f in files],
            "siblings": (sib["t"], sib["f"] or 0)}


def cmd_dossiers(conn, args):
    """Failed jobs worth labelling, ranked by how much INDEPENDENT evidence
    exists -- not by recency. A failure you can only judge from its log is a
    poor eval item, because your label would not be independent of the input."""
    # Exclude the aggregate gate job. "Total Success" fails whenever any other
    # job in the run fails -- it is a rollup, not a failure. It scores highly on
    # every independence signal (it "failed on 248 commits") precisely because it
    # mirrors everything else, and labelling it would teach a classifier nothing.
    rows = conn.execute(
        """SELECT j.id, j.name, r.head_sha, r.pr_number, r.head_branch
           FROM jobs j
           JOIN job_logs l ON l.job_id = j.id
           JOIN runs r ON r.id = j.run_id
           LEFT JOIN gold_labels g ON g.fkey = 'job:' || j.id
           WHERE j.conclusion='failure' AND g.fkey IS NULL
             AND j.name NOT IN ('Total Success')
             AND j.id NOT IN (SELECT job_id FROM job_steps
                              WHERE conclusion='failure'
                                AND name='Check all required jobs')
           ORDER BY j.id DESC LIMIT ?""", (args.limit * 4,)).fetchall()
    if not rows:
        print("nothing to label -- fetch logs first, or everything is labelled")
        return

    scored = []
    for r in rows:
        ev = _independent_evidence(conn, r["id"])
        if not ev:
            continue
        score = 0
        why = []
        if {"success", "failure"} <= ev["outcomes"]:
            score += 3
            why.append("rerun disagreement")
        if ev["spread"] >= 3:
            score += 2
            why.append(f"failed on {ev['spread']} commits")
        if ev["files"]:
            score += 1
            why.append(f"{len(ev['files'])}-file diff known")
        if ev["siblings"][1] >= 2:
            score += 1
            why.append(f"{ev['siblings'][1]} siblings failed")
        scored.append((score, r, why))

    scored.sort(key=lambda x: -x[0])
    print(f"{min(len(scored), args.limit)} failed job(s) with independent evidence:\n")
    for score, r, why in scored[:args.limit]:
        print(f"  [{score}] job {r['id']}  {r['name'][:40]}")
        print(f"        {', '.join(why) if why else 'log only -- weak eval item'}")
    print(f"\n  higher = more evidence you can judge WITHOUT reading the log")
    print(f"  python3 -m flakeagent.labels show --dossier {scored[0][1]['id']}")


def cmd_show(conn, args):
    if args.dossier:
        return _show_dossier(conn, args)
    issue = conn.execute(
        "SELECT number, title, state, body FROM known_issues WHERE number=?",
        (args.issue,)).fetchone()
    if not issue:
        print(f"#{args.issue} not stored; run `fetch issues`")
        return

    print("=" * 74)
    print(f"#{issue['number']}  [{issue['state']}]")
    print(f"{issue['title']}")
    print("=" * 74)

    body = (issue["body"] or "").strip()
    if body:
        print("\nREPORTED:")
        for line in body.splitlines()[:14]:
            print(f"  {line[:96]}")
        if len(body.splitlines()) > 14:
            print("  ...")

    fixes = _fixes(conn, args.issue)
    print(f"\nFIXED BY ({len(fixes)} candidate commit(s)):")
    for f in fixes:
        first = (f["message"] or "").splitlines()[0] if f["message"] else ""
        print(f"  [{f['source']:<8}] {f['sha'][:9]}  {first[:74]}")
        if f["author"]:
            print(f"             {f['author']} · {(f['committed_at'] or '')[:10]}")
    if not fixes:
        print("  (none identified)")

    samples = conn.execute(
        "SELECT text FROM corpus_samples WHERE issue_number=? LIMIT 1",
        (args.issue,)).fetchone()
    if samples:
        print("\nPASTED LOG (first 12 lines):")
        for line in samples["text"].splitlines()[:12]:
            print(f"  {line[:96]}")

    existing = conn.execute(
        "SELECT category, note FROM gold_labels WHERE issue_number=?",
        (args.issue,)).fetchone()
    print("\n" + "-" * 74)
    if existing:
        print(f"ALREADY LABELLED: {existing['category']}"
              + (f"  ({existing['note']})" if existing["note"] else ""))
    else:
        print("CATEGORIES:")
        for c in CATEGORIES:
            print(f"  {c:<22}{HINTS[c]}")
        print(f"\n  python3 -m flakeagent.labels set --issue {args.issue} "
              f"--category <one of the above>")


def _show_dossier(conn, args):
    """Present a failure for labelling, independent evidence FIRST.

    Deliberate ordering: everything you can judge without the log comes first,
    and the log window last and truncated. Label from the top of this page. The
    classifier will only ever see the bottom of it.
    """
    from . import logslice

    ev = _independent_evidence(conn, args.dossier)
    if not ev:
        print(f"job {args.dossier} not stored")
        return
    job, run, step = ev["job"], ev["run"], ev["step"]

    print("=" * 74)
    print(f"job {job['id']}  {job['name']}")
    print(f"runner {job['runner_name']} · branch {run['head_branch'] if run else '?'}"
          f" · {run['event'] if run else '?'}")
    print("=" * 74)

    print("\nINDEPENDENT EVIDENCE  (judge from this)")
    print("-" * 74)

    if step:
        secs = ""
        s, c = logslice.parse_ts(step["started_at"]), logslice.parse_ts(step["completed_at"])
        if s and c:
            secs = f"  ({int((c - s).total_seconds())}s)"
        print(f"  failing step   {step['number']}: {step['name']}{secs}")
        if any(k in (step["name"] or "").lower() for k in
               ("set up", "install", "checkout", "download", "configure", "build")):
            print("                 ^ a setup step -- the tests never ran")

    if {"success", "failure"} <= ev["outcomes"]:
        print(f"  rerun          SAME COMMIT both passed and failed -> it is a flake")
    elif ev["outcomes"]:
        print(f"  rerun          only {sorted(ev['outcomes'])} on this commit")

    print(f"  spread         this job failed on {ev['spread']} distinct commit(s)")
    print(f"  siblings       {ev['siblings'][1]} of {ev['siblings'][0]} other jobs "
          f"in the run also failed")

    if ev["files"]:
        from .dossier import _inert
        inert = [f for f in ev["files"] if _inert(f)]
        verdict = ("ALL inert (docs/vendor) -- the diff cannot have caused this"
                   if len(inert) == len(ev["files"]) else
                   f"{len(ev['files']) - len(inert)} source file(s) touched")
        print(f"  diff           {len(ev['files'])} file(s); {verdict}")
    else:
        print(f"  diff           unknown (fork PR or push without a merge title)")

    # Related issues and their maintainer fixes -- the strongest independent
    # signal, because someone actually diagnosed it.
    w = logslice.failing_step_window(conn, args.dossier)
    tests = w.get("failing_tests") or []
    if tests:
        print(f"  failing test   {tests[0]['name']}")
    from .dossier import related_issues_section, known_fixes_section
    rel = related_issues_section(conn, tests, job["name"])
    kf = known_fixes_section(conn, rel)
    fixed = [i for i in kf.get("issues", []) if i.get("has_identified_fix")]
    if fixed:
        print("  known fixes")
        for i in fixed[:3]:
            print(f"     #{i['number']} {(i['title'] or '')[:56]}")
            for c in i["fix_commits"][:1]:
                msg = (c["message"] or "").splitlines()[0]
                if msg and msg != "referenced":
                    print(f"        -> {msg[:60]}")

    print("\n" + "-" * 74)
    print("LOG  (the classifier sees this; try not to decide from it)")
    print("-" * 74)
    lines = (w.get("text") or "").splitlines()
    for line in lines[:args.log_lines]:
        print(f"  {line[:96]}")
    if len(lines) > args.log_lines:
        print(f"  ... {len(lines) - args.log_lines} more lines "
              f"(--log-lines N to see more)")

    existing = conn.execute("SELECT category, note FROM gold_labels WHERE fkey=?",
                            (f"job:{args.dossier}",)).fetchone()
    print("\n" + "=" * 74)
    if existing:
        print(f"ALREADY LABELLED: {existing['category']}"
              + (f"  ({existing['note']})" if existing["note"] else ""))
    else:
        for c in CATEGORIES:
            print(f"  {c:<22}{HINTS[c]}")
        print(f"\n  python3 -m flakeagent.labels set --dossier {args.dossier} "
              f"--category <one above> --note 'why'")


def cmd_set(conn, args):
    if args.category not in CATEGORIES:
        sys.exit(f"unknown category {args.category!r}; one of: {', '.join(CATEGORIES)}")

    # Two label namespaces, deliberately distinct:
    #   job:<id>    a specific failure -- the only kind eval can score, because
    #               the label and the prediction describe the same object
    #   issue:<n>   a recurring problem -- useful for learning the domain, but
    #               an issue is not a job failure and cannot be scored against one
    if args.dossier:
        job = conn.execute("SELECT name FROM jobs WHERE id=?",
                           (args.dossier,)).fetchone()
        if not job:
            sys.exit(f"job {args.dossier} not stored")
        conn.execute(
            """INSERT OR REPLACE INTO gold_labels (fkey, category, issue_number, note)
               VALUES (?,?,?,?)""",
            (f"job:{args.dossier}", args.category, None, args.note))
        conn.commit()
        print(f"job {args.dossier} -> {args.category}")
        print(f"  {job['name'][:70]}")
        return

    issue = conn.execute("SELECT title FROM known_issues WHERE number=?",
                         (args.issue,)).fetchone()
    if not issue:
        sys.exit(f"#{args.issue} not stored; run `fetch issues` first")
    conn.execute(
        """INSERT OR REPLACE INTO gold_labels (fkey, category, issue_number, note)
           VALUES (?,?,?,?)""",
        (f"issue:{args.issue}", args.category, args.issue, args.note))
    conn.commit()
    print(f"#{args.issue} -> {args.category}")
    print(f"  {issue['title'][:70]}")


def cmd_stats(conn, args):
    total = conn.execute("SELECT COUNT(*) n FROM gold_labels").fetchone()["n"]
    linked = conn.execute(
        "SELECT COUNT(DISTINCT issue_number) n FROM fix_commits "
        "WHERE issue_number IS NOT NULL").fetchone()["n"]
    issues = conn.execute("SELECT COUNT(*) n FROM known_issues").fetchone()["n"]

    jobs = conn.execute("SELECT COUNT(*) n FROM gold_labels "
                        "WHERE fkey LIKE 'job:%'").fetchone()["n"]
    print(f"labelled dossiers   {jobs}   <- the scoreable ones")
    print(f"labelled issues     {total - jobs}   (domain learning, not scoreable)")
    print(f"labellable          {linked}   (issues with an identified fix)")
    print(f"flakes issues       {issues}")
    if linked:
        print(f"coverage            {total / linked:.0%} of labellable")

    if total:
        print("\ndistribution:")
        for r in conn.execute(
                "SELECT category, COUNT(*) n FROM gold_labels "
                "GROUP BY category ORDER BY n DESC"):
            print(f"  {r['category']:<22}{r['n']:>5}")
        print("\n  a healthy eval set has several categories represented; "
              "one dominating\n  usually means the sample is biased, not that "
              "the world is")


def cmd_export(conn, args):
    rows = [dict(r) for r in conn.execute(
        """SELECT g.fkey, g.category, g.issue_number, g.note, k.title, k.state
           FROM gold_labels g LEFT JOIN known_issues k ON k.number = g.issue_number
           ORDER BY g.issue_number""")]
    text = json.dumps({"categories": CATEGORIES, "labels": rows}, indent=2)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
        print(f"{len(rows)} labels -> {args.out}")
    else:
        print(text)


COMMANDS = {"list": cmd_list, "dossiers": cmd_dossiers, "show": cmd_show,
            "set": cmd_set, "stats": cmd_stats, "export": cmd_export}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=sorted(COMMANDS))
    ap.add_argument("--issue", type=int)
    ap.add_argument("--dossier", type=int, help="a failed job id")
    ap.add_argument("--log-lines", type=int, default=25)
    ap.add_argument("--category")
    ap.add_argument("--note")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--out")
    ap.add_argument("--db")
    args = ap.parse_args(argv)

    if args.command == "show" and not (args.issue or args.dossier):
        ap.error("show requires --issue or --dossier")
    if args.command == "set" and not ((args.issue or args.dossier) and args.category):
        ap.error("set requires --issue or --dossier, plus --category")

    conn = store.connect(args.db)
    try:
        COMMANDS[args.command](conn, args)
    finally:
        conn.commit()
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
