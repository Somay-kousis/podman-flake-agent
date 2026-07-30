#!/usr/bin/env python3
"""Read a dossier and print what an agent would actually use.

    python3 examples/read_dossier.py tests/dossiers/<file>.json

An executable version of the contract in docs/DOSSIER.md. Every optional
section is guarded, because absent data is represented explicitly rather than
omitted -- that is the part worth copying.
"""

import json
import sys


def main(path):
    d = json.load(open(path))

    print(f"job          {d['job']['name']}  (runner {d['job'].get('runner_name')})")
    print(f"conclusion   {d['job'].get('conclusion')}")

    # Cheapest signal: which step failed. Infra vs test, no log needed.
    step = d["failing_step"]
    if step.get("found"):
        f = step["first"]
        print(f"failing step {f['number']}: {f['name']}")
    else:
        print(f"failing step -- {step.get('note')}")

    # The failing TEST, which is not the job name.
    win = d["log_window"]
    if win.get("available"):
        tests = [t["name"] for t in win.get("failing_tests", [])]
        print(f"failing test {tests or '(not identified)'}")
        print(f"log window   {win['line_count']} of {win['source_line_count']} lines "
              f"({win['reduction_pct']}% smaller, ~{win['est_tokens']} tokens)")
        print(f"             {win['reason']}")
    else:
        print(f"log window   unavailable -- {win.get('reason')}")

    # Was the diff even capable of causing this?
    pr = d["pull_request"]
    if pr.get("present") and pr.get("files_fetched"):
        print(f"pull request #{pr['number']}: {pr['file_count']} files, "
              f"all_paths_inert={pr['all_paths_inert']}")
    else:
        print(f"pull request -- {pr.get('note')}")

    # The strongest flake evidence: same commit, different outcome.
    att = d["attempts"]
    print(f"attempts     {att['runs_on_this_commit']} run(s) on this commit, "
          f"outcomes {att['job_outcomes_on_this_commit']}, "
          f"disagreement={att['disagreement']}")

    sib = d["siblings"]
    print(f"siblings     {sib['failed']} of {sib['total']} other jobs failed")

    hist = d["history"]
    print(f"history      {hist['failures']}/{hist['observations']} failures "
          f"(rate {hist['failure_rate']}) -- {hist['note']}")

    # The only supervised signal present.
    kf = d.get("known_fixes", {})
    fixed = [i for i in kf.get("issues", []) if i.get("has_identified_fix")]
    if fixed:
        print("known fixes")
        for i in fixed[:3]:
            print(f"  #{i['number']} {(i['title'] or '')[:56]}")
            for c in i["fix_commits"][:1]:
                print(f"      -> {c['message'].splitlines()[0][:60]}")
    else:
        print(f"known fixes  none -- {kf.get('note', 'no related issues')}")

    print()
    print("--- log window (first 25 lines) ---")
    for line in (win.get("text") or "").splitlines()[:25]:
        print(f"  {line[:100]}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
