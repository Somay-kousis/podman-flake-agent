#!/usr/bin/env python3
"""Harvest real failure logs out of Podman's `flakes`-labelled issues.

    python3 -m flakeagent.corpus harvest [--with-comments] [--limit N]
    python3 -m flakeagent.corpus stats
    python3 -m flakeagent.corpus show --issue 28868
    python3 -m flakeagent.corpus export --dir tests/corpus
    python3 -m flakeagent.corpus coverage

WHY
---
Two problems this solves at once.

1. Live CI artifacts are unreachable without credentials (artifact download
   returns 401, job logs 403) and, until podman#29091 merges, the only artifact
   produced is `journal-*.log` -- there is no logformatter HTML to parse. So the
   existing HTML path has no live input.

2. The 2023-era fixtures in tests/fixtures/ predate the May 2026 move to GitHub
   Actions. GHA prefixes every log line with an ISO-8601 timestamp
   (`2026-06-04T19:25:19.1292813Z`), which those fixtures never exercise.

The `flakes` issues fix both: 372 of them (open + closed), ~88% carrying a
pasted log block, spanning both eras, all public. And because a maintainer wrote
the title, each sample arrives with a human diagnosis attached -- which is what
gold labels get derived from later.
"""

import argparse
import json
import re
import sys
from pathlib import Path

from . import store
from .gh import GitHub, RateLimited
from .parse import parse_html

REPO = "podman-container-tools/podman"
QUERY = f"repo:{REPO} is:issue label:flakes"

FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.S)
DETAILS_RE = re.compile(r"<details>(.*?)</details>", re.S | re.I)
# 4+ spaces then anything: `[^\n]*` (not `\S`) so a deeper-indented continuation
# line -- a traceback's `  File "x.py"` -- doesn't break the run.
INDENT_RE = re.compile(r"(?:^[ \t]{4}[^\n]*\n?){5,}", re.M)
# Markdown nests lists with the same 4-space indent, so an indented run is only
# a log if it isn't mostly bullets.
LIST_LINE_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")
HTML_TAG_RE = re.compile(r"<[^>]+>")

# GitHub Actions stamps every line; Cirrus used relative offsets + artifact URLs.
GHA_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z", re.M)
CIRRUS_RE = re.compile(r"api\.cirrus-ci\.com|cirrus-ci\.com/task/|\[\+\d+s\]")

GINKGO_RE = re.compile(r"\[It\]|\[FAILED\]|Timeline >>|TOP-LEVEL|Summarizing \d+ Failure")
BATS_RE = re.compile(r"^not ok \d+|# #\| FAIL", re.M)
PYTHON_RE = re.compile(r"^(?:FAIL|ERROR): \w+ \(|Traceback \(most recent call last\)", re.M)

MIN_BLOCK_LINES = 3


# -- extraction -----------------------------------------------------------

def extract_blocks(markdown):
    """Pull candidate log blocks out of an issue body or comment.

    Order matters: fenced blocks first, then <details>, then indented runs.
    Text already claimed by an earlier form is blanked out so the same lines
    are not emitted twice under two different shapes.
    """
    if not markdown:
        return []

    blocks, remaining = [], markdown

    for pattern in (FENCE_RE, DETAILS_RE):
        for m in pattern.finditer(remaining):
            blocks.append(m.group(1))
        remaining = pattern.sub("\n", remaining)

    for m in INDENT_RE.finditer(remaining):
        candidate = re.sub(r"^[ \t]{4}", "", m.group(0), flags=re.M)
        lines = [ln for ln in candidate.splitlines() if ln.strip()]
        if lines and sum(bool(LIST_LINE_RE.match(ln)) for ln in lines) / len(lines) > 0.5:
            continue  # a nested markdown list, not a log
        blocks.append(candidate)

    out = []
    for b in blocks:
        # <details> bodies are markdown and often wrap a fence we already took.
        b = HTML_TAG_RE.sub("", b).strip("\n")
        if len([ln for ln in b.splitlines() if ln.strip()]) >= MIN_BLOCK_LINES:
            out.append(b)
    return out


def tag_block(text):
    """Classify a block by CI era and test suite. Regex only -- no model."""
    era = "gha" if GHA_TS_RE.search(text) else ("cirrus" if CIRRUS_RE.search(text) else "unknown")

    # Check bats/python before ginkgo: a python traceback can mention [It] if
    # the failure text quotes a ginkgo spec, but `FAIL: x (mod)` is unambiguous.
    if BATS_RE.search(text):
        suite = "bats"
    elif PYTHON_RE.search(text):
        suite = "python"
    elif GINKGO_RE.search(text):
        suite = "ginkgo"
    else:
        suite = "unknown"

    return era, suite


# -- commands -------------------------------------------------------------

def cmd_harvest(gh, conn, args):
    issues = seen = stored = 0

    for issue in gh.paginate("/search/issues", key="items", q=QUERY,
                             per_page=100, max_pages=6):
        if args.limit and issues >= args.limit:
            break
        issues += 1
        store.upsert_issue(conn, issue)

        sources = [("body", issue.get("body"))]

        if args.with_comments and issue.get("comments"):
            try:
                for c in gh.paginate(f"/repos/{REPO}/issues/{issue['number']}/comments",
                                     per_page=100, max_pages=2):
                    sources.append((f"comment:{c['id']}", c.get("body")))
            except RateLimited:
                raise
            except Exception as e:
                print(f"  ! #{issue['number']} comments: {e}", file=sys.stderr)

        for source, text in sources:
            for idx, block in enumerate(extract_blocks(text)):
                seen += 1
                era, suite = tag_block(block)
                cur = conn.execute(
                    """INSERT OR IGNORE INTO corpus_samples
                       (issue_number, source, block_index, era, suite, text)
                       VALUES (?,?,?,?,?,?)""",
                    (issue["number"], source, idx, era, suite, block),
                )
                stored += cur.rowcount

        conn.commit()
        if issues % 25 == 0:
            print(f"  ...{issues} issues, {stored} samples")

    print(f"\n{issues} issues scanned, {seen} blocks found, {stored} new samples stored")
    print(gh.stats())


def cmd_stats(conn, args):
    total = conn.execute("SELECT COUNT(*) n FROM corpus_samples").fetchone()["n"]
    if not total:
        print("corpus is empty; run `harvest` first")
        return

    issues = conn.execute(
        "SELECT COUNT(DISTINCT issue_number) n FROM corpus_samples").fetchone()["n"]
    print(f"{total} samples across {issues} issues\n")

    print(f"{'':<10}" + "".join(f"{s:>10}" for s in
                                ["ginkgo", "bats", "python", "unknown", "TOTAL"]))
    print("-" * 60)
    for era in ["gha", "cirrus", "unknown"]:
        row = [era]
        for suite in ["ginkgo", "bats", "python", "unknown"]:
            n = conn.execute(
                "SELECT COUNT(*) n FROM corpus_samples WHERE era=? AND suite=?",
                (era, suite)).fetchone()["n"]
            row.append(n)
        print(f"{row[0]:<10}" + "".join(f"{v:>10}" for v in row[1:]) + f"{sum(row[1:]):>10}")

    print("\nsize distribution (lines per sample):")
    sizes = sorted(len(r["text"].splitlines())
                   for r in conn.execute("SELECT text FROM corpus_samples"))
    for label, i in [("min", 0), ("p25", len(sizes)//4), ("median", len(sizes)//2),
                     ("p75", 3*len(sizes)//4), ("max", len(sizes)-1)]:
        print(f"  {label:<8}{sizes[i]:>7}")

    print("\nlargest samples:")
    for r in conn.execute(
        """SELECT issue_number, suite, era, LENGTH(text) AS n FROM corpus_samples
           ORDER BY n DESC LIMIT 5"""):
        print(f"  #{r['issue_number']:<7} {r['suite']:<8} {r['era']:<8} {r['n']:>9,} chars")


def cmd_show(conn, args):
    rows = conn.execute(
        "SELECT * FROM corpus_samples WHERE issue_number=? ORDER BY source, block_index",
        (args.issue,)).fetchall()
    if not rows:
        print(f"no samples for #{args.issue}")
        return

    title = conn.execute("SELECT title FROM known_issues WHERE number=?",
                         (args.issue,)).fetchone()
    print(f"#{args.issue} {title['title'] if title else ''}\n")

    for r in rows:
        print("=" * 72)
        print(f"{r['source']} block {r['block_index']}  "
              f"[era={r['era']} suite={r['suite']}]  {len(r['text'])} chars")
        print("=" * 72)
        text = r["text"]
        print(text if args.full else text[:args.chars])
        if not args.full and len(text) > args.chars:
            print(f"\n... [{len(text) - args.chars} more chars; --full to see all]")
        print()


def cmd_export(conn, args):
    out = Path(args.dir)
    out.mkdir(parents=True, exist_ok=True)
    n = 0
    for r in conn.execute("SELECT * FROM corpus_samples ORDER BY issue_number, id"):
        src = r["source"].replace(":", "-")
        name = f"{r['issue_number']}-{src}-{r['block_index']}-{r['era']}-{r['suite']}.log"
        (out / name).write_text(r["text"] + "\n")
        n += 1
    print(f"exported {n} samples -> {out}")


def cmd_coverage(conn, args):
    """How much of this real corpus can the CURRENT parser handle?

    Expected to be low: parse_html() keys on logformatter's CSS classes and
    these samples are raw runner output. The number is the point -- it says
    what to build next instead of guessing.
    """
    rows = conn.execute("SELECT * FROM corpus_samples").fetchall()
    if not rows:
        print("corpus is empty; run `harvest` first")
        return

    hits = {}
    totals = {}
    examples = []

    for r in rows:
        key = (r["era"], r["suite"])
        totals[key] = totals.get(key, 0) + 1
        try:
            found = parse_html(r["text"], source=f"#{r['issue_number']}")
        except Exception:
            found = []
        if found:
            hits[key] = hits.get(key, 0) + 1
            if len(examples) < 5:
                examples.append((r["issue_number"], r["suite"], found[0].name[:60]))

    n_hit = sum(hits.values())
    print(f"current parser (parse_html) on {len(rows)} real samples: "
          f"{n_hit} yielded a Failure = {n_hit/len(rows):.1%}\n")

    print(f"{'era':<10}{'suite':<10}{'samples':>9}{'parsed':>8}{'rate':>8}")
    print("-" * 45)
    for key in sorted(totals):
        t, h = totals[key], hits.get(key, 0)
        print(f"{key[0]:<10}{key[1]:<10}{t:>9}{h:>8}{h/t:>8.0%}")

    if examples:
        print("\nsamples it did parse:")
        for num, suite, name in examples:
            print(f"  #{num} [{suite}] {name}")
    else:
        print("\nnothing parsed — as expected for raw (non-logformatter) text.")


COMMANDS = {"harvest": cmd_harvest, "stats": cmd_stats, "show": cmd_show,
            "export": cmd_export, "coverage": cmd_coverage}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=sorted(COMMANDS))
    ap.add_argument("--with-comments", action="store_true",
                    help="also scan issue comments (one API call per issue — needs a token)")
    ap.add_argument("--limit", type=int, default=0, help="stop after N issues")
    ap.add_argument("--issue", type=int, help="show: issue number")
    ap.add_argument("--chars", type=int, default=1500, help="show: truncate at N chars")
    ap.add_argument("--full", action="store_true", help="show: print the whole block")
    ap.add_argument("--dir", default="tests/corpus", help="export: destination")
    ap.add_argument("--db")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    conn = store.connect(args.db)
    try:
        if args.command == "harvest":
            gh = GitHub(verbose=args.verbose)
            if not gh.token:
                print("note: no GITHUB_TOKEN — bodies only (search is 10 req/min "
                      "unauthenticated); --with-comments will be slow/blocked.\n",
                      file=sys.stderr)
            cmd_harvest(gh, conn, args)
        elif args.command == "show":
            if not args.issue:
                ap.error("show requires --issue")
            cmd_show(conn, args)
        else:
            COMMANDS[args.command](conn, args)
    except RateLimited as e:
        print(f"\n{e}", file=sys.stderr)
        return 2
    finally:
        conn.commit()
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
