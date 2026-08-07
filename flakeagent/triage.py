"""Rule-based triage: a category per failed job, with no model in the loop.

This is the layer underneath `agent.py`. It reads the same dossiers, emits the
same `{job_id: category}` file, and is scored by the same `eval.py` -- so it
drops into the harness as another arm rather than as a separate thing that has
to be believed on its own terms.

It exists for three reasons.

1. IT IS THE NUMBER THE AGENT HAS TO BEAT.
   `baselines.py` already showed what happens without one: `gpt-oss-120b`
   scored 29% on the gold set while `history.failure_rate >= 0.19` scored 92%
   with no model and no log. A model arm reported without a rule arm beside it
   is not a measurement. Every run of this module prints its own abstention
   rate for the same reason.

2. IT RUNS IN CI, ON A SCHEDULE, FOR FREE.
   No key, no GPU, no per-job cost, ~2 seconds for a day of failures. That is
   what makes `.github/workflows/triage.yml` possible at all, and a maintainer
   reading the job summary does not care whether a model was involved.

3. THE ABSTENTIONS ARE THE SPEC FOR THE AGENT.
   Every job this returns `unknown` for is a job whose cause is not recoverable
   from step names and anchored error strings. That set -- not the whole
   corpus -- is what an agent has to earn its cost on.

Two design rules are inherited from the rest of the package and matter here:

BLINDED BY DEFAULT. The fields a human labels from -- rerun disagreement,
cross-commit failure rate, the maintainer's eventual fix, whether the diff was
inert -- are withheld from the rules that pick the category, exactly as they are
from the model. They are reported next to the verdict as advisory flags, and
`flags_informed_category` is always false. A rule that read `history` would
score like the 92% baseline and mean as little.

EVIDENCE IS A SLICE, NOT A CLAIM. Every rule returns the log lines it matched
on, verbatim. They are re-checked against the log with `agent.verify_evidence`
before being written out, so the contract that binds the model binds the rules
too, and a mismatch is a bug here rather than a lie in the output.

    python3 -m flakeagent.triage --dossiers tests/dossiers
    python3 -m flakeagent.triage --dossiers data/dossiers --summary data/summary.md
    python3 -m flakeagent.eval dossiers --predictions data/triage_preds.json
"""

import argparse
import json
import os
import re
import sys

from .agent import load, log_text, verify_evidence
from .dossier import blind
from .taxonomy import CATEGORIES

# ---------------------------------------------------------------------------
# Step roles
#
# Counted over 30 days of failed steps in data/flakes.db (`job_steps` where
# conclusion='failure'), which is why the list is short and specific rather
# than a guess at what CI might contain:
#
#   549  Run test on lima                        test
#   285  Check all required jobs                 aggregate
#   154  Run machine e2e                         test
#    30  Output failure log as GITHUB_STEP_SUMMARY  report
#    21  Validate source                         test
#    20  Check that the PR includes tests        test
#    19  Run cross build                         build
#    13  Set up job                              setup
#    10  Run lima-vm/lima-actions/setup@<sha>    setup
#    10  Build each commit                       build
#     6  Run unit tests                          test
#     5  Upload release artifacts                report
#     4  Build podman / Build docs / ...         build
#
# The role is the single most useful fact in the dossier and it costs nothing:
# a failure in a setup step means the suite never ran, so whatever the diff did
# is irrelevant. `agent.build_prompt` puts the step name first for the same
# reason.
# ---------------------------------------------------------------------------

STEP_ROLES = (
    ("aggregate", re.compile(r"(?i)check all required jobs")),
    # `^set up ` is deliberately broad: GitHub names every implicit setup step
    # that way, and a step this misses gets role `unknown`, which no rule acts
    # on -- so the cost of being broad here is bounded by the rules downstream.
    # `actions/setup-` catches `Run actions/setup-go@<sha>`, which is how a
    # pinned setup action appears and which none of the other patterns match.
    ("setup", re.compile(r"(?i)^set up |^checkout|actions/checkout|"
                         r"actions/setup-|lima-actions/setup|"
                         r"^install |^fetch |^restore |^cache ")),
    ("report", re.compile(r"(?i)^output failure log|^upload |^collect |"
                          r"actions/upload-artifact")),
    ("test", re.compile(r"(?i)^run test|^run machine|^run unit|^run installer|"
                        r"^run integration|^validate source|^check that the pr")),
    ("build", re.compile(r"(?i)^build |^run cross build|^make ")),
)

# Job names that are not a test result of their own. `Total Success` is the
# required-check aggregator: it fails because something else failed, and
# counting it triages the same failure twice.
AGGREGATOR_JOBS = re.compile(r"(?i)^total success$")


def step_role(name):
    """setup / test / build / report / aggregate / unknown for a step name."""
    for role, pattern in STEP_ROLES:
        if pattern.search(name or ""):
            return role
    return "unknown"


# ---------------------------------------------------------------------------
# Log patterns
#
# Anchored hard, on purpose. The first draft of this module matched /journald?/
# because Podman flakes are so often journal timeliness -- and it fired on 21 of
# 23 failures in a two-day window, because every job uploads an artifact named
# `journal-<suite>-<distro>.log` and the step name is in the log. A pattern that
# matches almost everything carries no information; these are strings that only
# appear when the thing they name actually went wrong.
# ---------------------------------------------------------------------------

PACKAGE_MANAGER = re.compile(
    r"(?m)^.*(?:"
    r"[EW]: Failed to fetch|"
    r"Unable to correct problems, you have held broken packages|"
    r"Some index files failed to download|"
    r"[Ff]ailed to download metadata for repo|"
    r"Error: Failed to download packages|"
    r"Cannot download.*repomd\.xml"
    r").*$")

NAME_RESOLUTION = re.compile(
    r"(?m)^.*(?:"
    r"Could not resolve host|"
    r"Temporary failure in name resolution|"
    r"no such host|"
    r"server misbehaving"
    r").*$")

REGISTRY_UNAVAILABLE = re.compile(
    r"(?m)^.*(?:"
    r"received unexpected HTTP status: 5\d\d|"
    r"50[23] (?:Bad Gateway|Service Unavailable)|"
    r"error pinging docker registry"
    r").*$")

RESOURCE = re.compile(
    r"(?m)^.*(?:"
    r"[Nn]o space left on device|"
    r"[Cc]annot allocate memory|"
    r"too many open files|"
    r"[Oo]ut of memory: Kill|"
    r"(?:clone|fork|pthread_create)[^\n]{0,60}Resource temporarily unavailable"
    r").*$")

NETWORK_TIMEOUT = re.compile(
    r"(?m)^.*(?:"
    r"i/o timeout|"
    r"TLS handshake timeout|"
    r"Client\.Timeout exceeded|"
    r"net/http: request canceled|"
    r"[Cc]onnection timed out"
    r").*$")


def _lines(pattern, text, cap=3):
    """Up to `cap` whole matched lines, stripped, deduplicated, in order."""
    seen, out = set(), []
    for m in pattern.finditer(text):
        line = m.group(0).strip()
        if line and line not in seen:
            seen.add(line)
            out.append(line)
        if len(out) >= cap:
            break
    return out


# ---------------------------------------------------------------------------
# Rules
#
# Ordered, first match wins. Each returns (category, confidence, reasoning,
# evidence) or None. Confidence is a stated prior, not a measurement -- the
# only honest number for a rule that has never been scored on a held-out set,
# and `eval.py` reports accuracy separately for exactly that reason.
# ---------------------------------------------------------------------------


def rule_aggregator(doc, log, role):
    """The required-check job mirrors other jobs; it is not a failure of its own."""
    name = (doc.get("job") or {}).get("name") or ""
    if AGGREGATOR_JOBS.search(name) or role == "aggregate":
        return ("unknown", 0.0,
                "aggregator job -- it fails because a job it gates failed, so "
                "triaging it would count the same failure twice", [])
    return None


def rule_setup_step(doc, log, role):
    """The suite never ran: whatever the diff changed cannot be the cause."""
    if role != "setup":
        return None
    step = ((doc.get("failing_step") or {}).get("first") or {})
    evidence = (_lines(PACKAGE_MANAGER, log, 2) or _lines(NAME_RESOLUTION, log, 2)
                or _lines(NETWORK_TIMEOUT, log, 2))
    return ("infra_blip", 0.9,
            f"failed in {step.get('name')!r}, a setup step -- the test suite "
            "never started, so the change under test cannot have caused this",
            evidence)


def rule_package_manager(doc, log, role):
    hits = _lines(PACKAGE_MANAGER, log)
    if not hits:
        return None
    return ("infra_blip", 0.85,
            "a package manager could not fetch from its mirror; that is the "
            "mirror's state, not the code's", hits)


def rule_name_resolution(doc, log, role):
    hits = _lines(NAME_RESOLUTION, log)
    if not hits:
        return None
    return ("infra_blip", 0.85, "a hostname did not resolve", hits)


def rule_registry(doc, log, role):
    hits = _lines(REGISTRY_UNAVAILABLE, log)
    if not hits:
        return None
    return ("infra_blip", 0.8,
            "a registry answered 5xx or refused to be pinged", hits)


def rule_resource(doc, log, role):
    hits = _lines(RESOURCE, log)
    if not hits:
        return None
    return ("resource_exhaustion", 0.8,
            "the runner ran out of a resource -- disk, memory, descriptors or "
            "process slots", hits)


def rule_network_timeout(doc, log, role):
    hits = _lines(NETWORK_TIMEOUT, log)
    if not hits:
        return None
    return ("network_timeout", 0.7,
            "a network operation exceeded its deadline; the endpoint answered "
            "or was reachable, it was just too slow", hits)


RULES = (
    ("aggregator_job", rule_aggregator),
    ("setup_step_failure", rule_setup_step),
    ("package_manager_failure", rule_package_manager),
    ("name_resolution_failure", rule_name_resolution),
    ("registry_unavailable", rule_registry),
    ("resource_exhaustion", rule_resource),
    ("network_timeout", rule_network_timeout),
)

# The rules above cannot reach these two. `race_condition` needs the timing of
# an interleaving, `real_bug` needs to know what the diff did -- neither is a
# string you can grep for, and pretending otherwise is how a maintainer gets
# told to press rerun on a genuine bug. They are left to the agent, and the
# abstention count below is the honest statement of that gap.
UNREACHABLE = ("race_condition", "real_bug")


def advisory_flags(doc):
    """Deterministic facts a maintainer wants, from the fields the rules cannot see.

    These are the labeller's evidence -- `dossier.blind` withholds them, and
    they stay withheld from the category. Reporting them anyway is not a
    contradiction: the maintainer reading the summary is not being scored, and
    "the same commit already passed this job" is the single most actionable
    line the tool can print.
    """
    out = []
    att = doc.get("attempts") or {}
    if att.get("disagreement"):
        out.append("rerun disagreement: this exact commit both passed and "
                   "failed this job -- flaky by definition")

    pr = doc.get("pull_request") or {}
    if pr.get("all_paths_inert"):
        out.append(f"inert diff: all {pr.get('inert_file_count')} changed paths are "
                   "vendored/docs/dependency files")

    hist = doc.get("history") or {}
    rate, obs = hist.get("failure_rate"), hist.get("observations")
    if rate is not None and obs:
        out.append(f"history: failed {hist.get('failures')} of {obs} stored runs "
                   f"of this job ({rate:.0%})")

    sib = doc.get("siblings") or {}
    if sib.get("total"):
        out.append(f"siblings: {sib.get('failed')} of {sib['total']} jobs in this "
                   "run failed")

    for issue in (doc.get("known_fixes") or {}).get("issues") or []:
        out.append(f"known issue #{issue.get('number')}: {issue.get('title')}")
    return out


def triage(doc, blinded=True):
    """One verdict for one dossier. `doc` is the full dossier; blinding is here."""
    seen = blind(doc) if blinded else doc
    log = log_text(seen)
    step = ((seen.get("failing_step") or {}).get("first") or {})
    role = step_role(step.get("name"))

    category, confidence, reasoning, evidence = "unknown", 0.0, "", []
    fired = None
    for name, fn in RULES:
        got = fn(seen, log, role)
        if got:
            fired = name
            category, confidence, reasoning, evidence = got
            break

    if fired is None:
        reasoning = ("no rule matched: the step role and the anchored error "
                     "strings do not distinguish between the remaining causes")

    assert category in CATEGORIES, category
    tests = (seen.get("log_window") or {}).get("failing_tests") or []
    return {
        "category": category,
        "confidence": confidence,
        "rule": fired,
        "step_role": role,
        "step": step.get("name"),
        # Carried even when no rule fires: an abstention that still names the
        # test that failed is worth reading, and a bare "unknown" is not.
        "failing_tests": [t.get("name") for t in tests[:3] if t.get("name")],
        "reasoning": reasoning,
        "evidence": evidence,
        "blinded": blinded,
        "flags": advisory_flags(doc),
        "flags_informed_category": False,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _job_line(doc, verdict):
    job = doc.get("job") or {}
    url = job.get("html_url")
    name = job.get("name") or job.get("id")
    label = f"[{name}]({url})" if url else name
    return label, verdict


def markdown(rows, window=None):
    """The job summary. Written for someone deciding whether to press rerun."""
    triaged = [r for r in rows if r[1]["rule"] != "aggregator_job"]
    out = ["# CI flake triage", ""]
    if window:
        out.append(f"`{window}`")
        out.append("")

    if not triaged:
        out += ["No failed jobs in the window.", ""]
        return "\n".join(out)

    counts = {}
    for _, v in triaged:
        counts[v["category"]] = counts.get(v["category"], 0) + 1
    resolved = sum(n for c, n in counts.items() if c != "unknown")

    out += [
        f"**{len(triaged)} failed jobs.** Rules resolved **{resolved}**, "
        f"abstained on **{counts.get('unknown', 0)}**.",
        "",
        "| category | jobs |",
        "|---|---:|",
    ]
    for cat, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        out.append(f"| `{cat}` | {n} |")
    out.append("")

    for cat in sorted(counts, key=lambda c: (c == "unknown", c)):
        # The abstentions are the long tail and nobody has to act on them, so
        # they are folded away -- but they are still listed, because a summary
        # that hides what it could not answer is the wrong summary.
        fold = cat == "unknown"
        out += [f"## {cat}", ""]
        if fold:
            out += [f"<details><summary>{counts[cat]} jobs the rules could not "
                    "classify</summary>", ""]
        for label, v in triaged:
            if v["category"] != cat:
                continue
            head = f"- {label}"
            if v["rule"]:
                head += f" — `{v['rule']}` (confidence {v['confidence']})"
            out.append(head)
            for name in v.get("failing_tests") or []:
                out.append(f"  - failing test: `{name[:160]}`")
            out.append(f"  - {v['reasoning']}")
            for line in v["evidence"]:
                out.append(f"  - `{line[:200]}`")
            for flag in v["flags"]:
                out.append(f"  - _{flag}_")
        if fold:
            out += ["", "</details>"]
        out.append("")

    out += [
        "---",
        "",
        "Rules only. No model was called, and no rule can return "
        "`race_condition` or `real_bug` — neither is recoverable from step "
        "names and error strings, so those failures land in `unknown` by "
        "design. The advisory lines in _italics_ are withheld from the "
        "category decision; they are shown because a maintainer is not being "
        "scored.",
        "",
    ]
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dossiers", nargs="+", default=["data/dossiers"])
    ap.add_argument("--limit", type=int)
    ap.add_argument("--no-blind", action="store_true",
                    help="let the rules see the labeller's evidence; scores "
                         "from this are not a measurement -- see baselines.py")
    ap.add_argument("--out", default="data/triage_preds.json",
                    help="{job_id: category}, the file eval.py scores")
    ap.add_argument("--verdicts", default="data/triage_verdicts.json")
    ap.add_argument("--summary", help="write the markdown report here "
                                      "(e.g. $GITHUB_STEP_SUMMARY)")
    ap.add_argument("--append", action="store_true",
                    help="append to --summary instead of truncating; "
                         "$GITHUB_STEP_SUMMARY is a shared file, so use this there")
    ap.add_argument("--window", help="a label for the summary, e.g. 'last 2 days'")
    ap.add_argument("--only-labelled", action="store_true",
                    help="restrict to dossiers with a gold label -- the same "
                         "subset the model arms were run over, so the two are "
                         "comparable")
    ap.add_argument("--db")
    args = ap.parse_args(argv)

    files = load(args.dossiers, None)
    if args.only_labelled:
        from . import store
        conn = store.connect(args.db)
        keep = {r["fkey"].split(":", 1)[1] for r in
                conn.execute("SELECT fkey FROM gold_labels WHERE fkey LIKE 'job:%'")}
        files = [f for f in files
                 if str(json.load(open(f)).get("job", {}).get("id")) in keep]
    if args.limit:
        files = files[:args.limit]
    if not files:
        sys.exit(f"no dossiers found in {args.dossiers}; run `flakeagent.dossier` first")

    rows, preds, verdicts = [], {}, {}
    counts, unverified = {}, 0

    for path in files:
        with open(path) as fh:
            doc = json.load(fh)
        job_id = str((doc.get("job") or {}).get("id") or
                     os.path.basename(path).split("-")[-1].removesuffix(".json"))
        verdict = triage(doc, blinded=not args.no_blind)

        # The rules slice their evidence out of the log, so this can only fail
        # if a pattern or the slicing is wrong. Checking it anyway keeps the
        # rules under the same contract as the model.
        hit, total = verify_evidence(verdict, blind(doc) if not args.no_blind else doc)
        if total and hit < total:
            unverified += 1
            print(f"  ! {job_id}: {total - hit} evidence line(s) not found in the log",
                  file=sys.stderr)
        verdict["evidence_verified"], verdict["evidence_quoted"] = hit, total

        rows.append(_job_line(doc, verdict))
        verdicts[job_id] = verdict
        if verdict["rule"] != "aggregator_job":
            preds[job_id] = verdict["category"]
            counts[verdict["category"]] = counts.get(verdict["category"], 0) + 1

        print(f"  {job_id}  {verdict['category']:<20} "
              f"{verdict['rule'] or '-':<24} {(verdict['step'] or '')[:34]}")

    total_triaged = sum(counts.values())
    abstained = counts.get("unknown", 0)
    print(f"\n{len(files)} dossiers, {total_triaged} triaged, "
          f"{len(files) - total_triaged} aggregator job(s) excluded")
    print("verdicts     " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if total_triaged:
        print(f"abstention   {abstained}/{total_triaged} = "
              f"{abstained / total_triaged:.0%} -- these are what the agent is for")
    if unverified:
        print(f"UNVERIFIED   {unverified} verdict(s) -- a bug in a rule, not in a model")

    for path, payload in ((args.out, preds), (args.verdicts, verdicts)):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
    print(f"\nwrote {args.out} ({len(preds)}) and {args.verdicts}")

    if args.summary:
        with open(args.summary, "a" if args.append else "w") as fh:
            fh.write(markdown(rows, args.window))
        print(f"wrote {args.summary}")

    print(f"score with: python3 -m flakeagent.eval dossiers --predictions {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
