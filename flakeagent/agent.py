"""Run a model over dossiers and emit predictions `eval.py` can score.

`classify.py` predates the fetch layer: it reads the old `test_failures` table
via `store`, which is a per-test-name view assembled from parsed logformatter
HTML. The dossier replaced that -- one JSON per failed job, built from the API
rather than from parsing -- and nothing connected the two. This module is that
connection, and it is deliberately the only new thing: the schema, the system
prompt and both backends are reused from `taxonomy` and `classify`.

Three decisions worth defending:

1. BLINDED BY DEFAULT.
   The dossiers on disk carry the evidence a human labels from -- rerun
   disagreement, cross-commit frequency, the maintainer's eventual fix, whether
   the diff was inert. If the model reads those, a high score means "it agrees
   with the evidence I labelled from", which is not a measurement. `--no-blind`
   exists so the gap can be quantified, but you have to ask for it, and the
   output records which mode produced it.

2. EVIDENCE IS CHECKED AGAINST THE LOG, NOT TRUSTED.
   The schema asks for verbatim log lines. Nothing in a schema can force them to
   be real. `verify_evidence` substring-matches every quoted line against the
   log window the model was shown and records the hit rate, so a fluent verdict
   built on invented lines is visible as a number instead of having to be caught
   by reading. Unverifiable evidence does not change the category -- it is
   reported, not silently corrected.

3. THE DRY RUN IS THE DEFAULT WAY IN.
   `--dry-run` builds every prompt, counts every token and calls no model, so
   the expensive half can be reviewed before a key or a GPU is involved. This
   package's own history is the argument: the three worst bugs in it all shipped
   in code that had been written and reviewed but never executed.

Output is two files. `preds.json` is exactly the contract `eval.py dossiers`
expects -- `{"<job_id>": "<category>"}` -- and nothing else, so it stays
readable and diffable. `verdicts.json` keeps everything else: reasoning,
evidence, the verification result, token counts, and the prompt's size.

    python3 -m flakeagent.agent --dossiers data/dossiers --dry-run
    python3 -m flakeagent.agent --dossiers data/dossiers --limit 30 --backend ollama
    python3 -m flakeagent.eval dossiers --predictions data/preds.json
"""

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime

from . import store
from .dossier import blind
from .taxonomy import CATEGORIES

MAX_LOG_CHARS = 12000

# 111 of 400 dossiers carry ANSI colour codes -- the Windows PowerShell steps
# emit them heavily. Stripping saves only 0.4% of log characters, so this is not
# a token optimisation and should not be sold as one. It is here so that
# `verify_evidence` works: the model quotes what it was shown, and if the prompt
# carried `\x1b[32;1m` mid-line while the check grepped the raw text (or the
# reverse) honest quotes would be scored as invented. Both sides call
# `log_text`, so there is one string and no mismatch.
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def log_text(doc):
    """The log exactly as the model sees it -- the one source for prompt and check."""
    return ANSI.sub("", (doc.get("log_window") or {}).get("text") or "")


def _secs(a, b):
    """Duration between two GitHub ISO-8601 stamps, or None."""
    if not a or not b:
        return None
    try:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        return int((datetime.strptime(b, fmt) - datetime.strptime(a, fmt)).total_seconds())
    except (ValueError, TypeError):
        return None


def build_prompt(doc):
    """Render a dossier into the smallest prompt that still carries the signal.

    Ordered so the cheap structural facts come first and the log last: the
    failing step name alone separates infrastructure from test failures without
    reading a line of output, and the sibling counts say whether this was one
    job or the whole matrix. A model that can answer from the header should not
    have to reach the log at all.
    """
    job = doc.get("job") or {}
    run = doc.get("run") or {}
    sib = doc.get("siblings") or {}
    win = doc.get("log_window") or {}
    step = (doc.get("failing_step") or {}).get("first") or {}

    tests = win.get("failing_tests") or []
    if tests:
        names = "\n".join(f"- [{t.get('kind')}] {t.get('name')}" for t in tests[:5])
    else:
        names = "- not extracted from the log"

    lines = [f"Job: {job.get('name')}"]

    # Windows and macOS jobs populate none of these -- printing four `None`s
    # spends tokens asserting nothing. Omit what the API did not give us.
    facets = [f"{k}={job[k]}" for k in ("test", "mode", "priv", "distro") if job.get(k)]
    if facets:
        lines.append("Suite/mode: " + " ".join(facets))

    if step:
        dur = _secs(step.get("started_at"), step.get("completed_at"))
        lines.append(
            f"Failing step: {step.get('name')!r} (step {step.get('number')}"
            + (f", ran {dur}s" if dur is not None else "") + ")"
        )
    else:
        lines.append("Failing step: not identified")

    total, failed = sib.get("total"), sib.get("failed")
    if total:
        lines.append(
            f"Sibling jobs in this run: {failed} of {total} failed "
            "(one job failing alone points away from a shared cause)"
        )

    lines += [
        f"Trigger: {run.get('event')} on branch {run.get('head_branch')!r}",
        f"Change under test: {run.get('display_title')!r}",
        "",
        "Failing test(s) extracted from the log:",
        names,
        "",
        f"Log window ({win.get('line_count')} lines, "
        f"{win.get('reduction_pct')}% smaller than the {win.get('source_line_count')}-line "
        f"raw log; {win.get('failure_markers')} failure markers):",
        "```",
        (log_text(doc) or "(no log window available)")[:MAX_LOG_CHARS],
        "```",
        "",
        "Classify this failure.",
    ]
    return "\n".join(lines)


def verify_evidence(verdict, doc):
    """Substring-check every quoted evidence line against the log shown.

    Normalises whitespace only. A quote that survives that and still does not
    appear was not in the log -- either paraphrased or invented. Returns
    (matched, total); total 0 means the model cited nothing, which is different
    from citing badly and is reported as such.
    """
    log = " ".join(log_text(doc).split())
    quotes = [q for q in (verdict.get("evidence") or []) if q and q.strip()]
    matched = sum(1 for q in quotes if " ".join(q.split()) in log)
    return matched, len(quotes)


def load(paths, limit=None):
    files = []
    for p in paths:
        files.extend(sorted(glob.glob(os.path.join(p, "*.json"))) if os.path.isdir(p) else [p])
    if limit:
        files = files[:limit]
    return files


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dossiers", nargs="+", default=["data/dossiers"],
                    help="dossier directories or files (default: data/dossiers)")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--backend", choices=["ollama", "api"], default="ollama")
    ap.add_argument("--model")
    ap.add_argument("--no-blind", action="store_true",
                    help="show the labeller's evidence too; scores from this are "
                         "not a measurement of triage skill -- see the module docstring")
    ap.add_argument("--dry-run", action="store_true",
                    help="build prompts and count tokens; call no model")
    ap.add_argument("--out", default="data/preds.json")
    ap.add_argument("--verdicts", default="data/verdicts.json")
    ap.add_argument("--db")
    args = ap.parse_args(argv)

    files = load(args.dossiers, args.limit)
    if not files:
        sys.exit(f"no dossiers found in {args.dossiers}; run `flakeagent.dossier` first")

    backend = None
    if not args.dry_run:
        from .classify import MODEL_API, MODEL_OLLAMA, AnthropicBackend, OllamaBackend
        if args.backend == "ollama":
            backend = OllamaBackend(args.model or MODEL_OLLAMA)
        else:
            backend = AnthropicBackend(store.connect(args.db), args.model or MODEL_API)

    mode = "raw" if args.no_blind else "blinded"
    print(f"{len(files)} dossiers, {mode}, "
          f"{'dry run' if args.dry_run else backend.name}\n")

    preds, verdicts = {}, {}
    sizes, tin_total, tout_total = [], 0, 0
    counts, unverified = {}, 0

    for path in files:
        with open(path) as fh:
            doc = json.load(fh)
        if not args.no_blind:
            doc = blind(doc)

        job_id = str((doc.get("job") or {}).get("id") or
                     os.path.basename(path).split("-")[-1].removesuffix(".json"))
        prompt = build_prompt(doc)
        sizes.append(len(prompt))

        if args.dry_run:
            print(f"  {job_id}  {len(prompt):>6,} chars  ~{len(prompt)//4:>5,} tokens")
            continue

        try:
            verdict, tin, tout = backend.classify(prompt)
        except Exception as e:                      # noqa: BLE001 - one bad job must not stop the run
            print(f"  ! {job_id}: {e}", file=sys.stderr)
            continue

        cat = verdict.get("category")
        if cat not in CATEGORIES:                   # schema should prevent this; check anyway
            print(f"  ! {job_id}: off-vocabulary category {cat!r}", file=sys.stderr)
            continue

        hit, total = verify_evidence(verdict, doc)
        if total and hit < total:
            unverified += 1

        verdict.update(evidence_verified=hit, evidence_quoted=total,
                       prompt_chars=len(prompt), tokens_in=tin, tokens_out=tout,
                       blinded=not args.no_blind, model=backend.name)
        preds[job_id] = cat
        verdicts[job_id] = verdict
        counts[cat] = counts.get(cat, 0) + 1
        tin_total += tin or 0
        tout_total += tout or 0

        flag = "" if total == 0 else ("  ok" if hit == total else f"  !! {hit}/{total} quoted")
        print(f"  {job_id}  {cat:<20} conf={verdict.get('confidence')}{flag}")

    n = len(sizes)
    print(f"\nprompt size  median {sorted(sizes)[n // 2]:,} chars "
          f"(~{sorted(sizes)[n // 2] // 4:,} tokens), max {max(sizes):,}")

    if args.dry_run:
        print("dry run -- no model called, nothing written")
        return 0

    for path, payload in ((args.out, preds), (args.verdicts, verdicts)):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)

    print(f"verdicts     {', '.join(f'{k}={v}' for k, v in sorted(counts.items()))}")
    print(f"tokens       {tin_total:,} in, {tout_total:,} out")
    if unverified:
        print(f"UNVERIFIED   {unverified} verdict(s) quoted evidence not found in the log")
    print(f"\nwrote {args.out} ({len(preds)}) and {args.verdicts}")
    print(f"score with: python3 -m flakeagent.eval dossiers --predictions {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
