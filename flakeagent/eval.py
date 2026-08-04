#!/usr/bin/env python3
"""Measure whether the classifier is actually right.

This is the part that decides whether the tool is worth deploying. A classifier
that confidently calls a real race condition an "infra blip" tells a maintainer
to press re-run on a genuine bug -- that is worse than no tool, and you cannot
tell it apart from a good classifier without measuring.

So three numbers, not one:

  precision/recall per category  -- is it right, and on what
  abstention rate                -- how often it says `unknown`
                                    (high abstention + high precision is a
                                    usable tool; high precision alone is not
                                    if it abstains on everything)
  cost per PR                    -- tokens and dollars at Podman's real matrix
                                    size, because a tool nobody can afford to
                                    run is not deployed either

Gold labels come from hand-reading the `flakes`-labelled issues (see
gold_labels in schema.sql, and `label` below).

    python3 -m flakeagent.eval label --key '<fkey>' --category race_condition --issue 24571
    python3 -m flakeagent.eval score
"""

import argparse
import json
import sys

from . import store

# Claude Opus 5, USD per million tokens (input / output).
PRICE_IN, PRICE_OUT = 5.00, 25.00

# ci.yml big-tests: 4 distros x 2 tests x 2 priv x 2 modes, minus exclusions,
# plus small-tests. Failing jobs per PR is what actually gets classified.
JOBS_PER_PR = 30


def cmd_label(conn, args):
    conn.execute(
        "INSERT OR REPLACE INTO gold_labels (fkey, category, issue_number, note) "
        "VALUES (?,?,?,?)",
        (args.key, args.category, args.issue, args.note),
    )
    conn.commit()
    print(f"labelled {args.key} -> {args.category}")


def cmd_list(conn, args):
    rows = conn.execute(
        """SELECT f.fkey, f.kind, f.name, g.category AS gold
           FROM (SELECT DISTINCT fkey, kind, name FROM test_failures) f
           LEFT JOIN gold_labels g ON g.fkey = f.fkey"""
    ).fetchall()
    for r in rows:
        print(f"  [{r['gold'] or 'UNLABELLED':<20}] {r['fkey'][:80]}")
    print(f"\n{sum(1 for r in rows if r['gold'])}/{len(rows)} labelled")


def cmd_dossiers(conn, args):
    """Score predictions over labelled dossiers.

        python3 -m flakeagent.eval dossiers --predictions preds.json

    `preds.json` maps a job id to a category, whatever produced it:

        {"90647416963": "infra_blip", "90533766454": "race_condition"}

    Deliberately decoupled from any classifier in this repo. The label and the
    prediction describe the same object -- one job that failed -- which is what
    makes the number mean anything. Scoring issue labels against job
    predictions would not.
    """
    try:
        with open(args.predictions) as fh:
            preds = {str(k): v for k, v in json.load(fh).items()}
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"could not read {args.predictions}: {e}")

    gold = {r["fkey"].split(":", 1)[1]: r["category"] for r in conn.execute(
        "SELECT fkey, category FROM gold_labels WHERE fkey LIKE 'job:%'")}
    if not gold:
        sys.exit("no dossier labels yet -- `labels dossiers` then "
                 "`labels set --dossier <id> --category ...`")

    common = sorted(set(gold) & set(preds))
    print(f"labelled dossiers  {len(gold)}")
    print(f"predictions        {len(preds)}")
    print(f"scored             {len(common)}")
    if not common:
        sys.exit("\nno overlap between labels and predictions -- are the keys job ids?")

    missing = sorted(set(gold) - set(preds))
    if missing:
        print(f"unpredicted        {len(missing)}  (counted as wrong is a choice; "
              "they are excluded here)")

    cats = sorted({gold[k] for k in common} | {preds[k] for k in common})
    print(f"\n{'category':<22}{'prec':>7}{'rec':>7}{'tp':>5}{'fp':>5}{'fn':>5}")
    print("-" * 51)
    precisions, recalls = [], []
    for cat in cats:
        tp = sum(1 for k in common if preds[k] == cat and gold[k] == cat)
        fp = sum(1 for k in common if preds[k] == cat and gold[k] != cat)
        fn = sum(1 for k in common if preds[k] != cat and gold[k] == cat)
        p = tp / (tp + fp) if tp + fp else None
        rc = tp / (tp + fn) if tp + fn else None
        print(f"{cat:<22}"
              f"{f'{p:.2f}' if p is not None else '-':>7}"
              f"{f'{rc:.2f}' if rc is not None else '-':>7}{tp:>5}{fp:>5}{fn:>5}")
        if cat != "unknown":
            if p is not None:
                precisions.append(p)
            if rc is not None:
                recalls.append(rc)

    if precisions or recalls:
        print("-" * 51)
        mp = f"{sum(precisions)/len(precisions):.2f}" if precisions else "-"
        mr = f"{sum(recalls)/len(recalls):.2f}" if recalls else "-"
        print(f"{'macro (excl. unknown)':<22}{mp:>7}{mr:>7}")

    correct = sum(1 for k in common if preds[k] == gold[k])
    abstained = sum(1 for k in common if preds[k] == "unknown")
    decided = [k for k in common if preds[k] != "unknown"]
    dc = sum(1 for k in decided if preds[k] == gold[k])

    print(f"\naccuracy overall      {correct}/{len(common)} = {correct/len(common):.0%}")
    print(f"abstention rate       {abstained}/{len(common)} = {abstained/len(common):.0%}")
    if decided:
        print(f"accuracy when decided {dc}/{len(decided)} = {dc/len(decided):.0%}"
              "   <- the number that matters")

    waved = [k for k in common if gold[k] == "real_bug"
             and preds[k] in ("infra_blip", "network_timeout")]
    if waved:
        print(f"\n!! {len(waved)} real bug(s) called re-runnable -- the failure mode "
              "that makes CI worse:")
        for k in waved:
            print(f"     job {k}: predicted {preds[k]}")

    if len(common) < 30:
        print(f"\n  note: {len(common)} scored items is a small sample. Treat these "
              "as directional,\n  not as an accuracy claim.")


def cmd_score(conn, args):
    rows = conn.execute(
        """SELECT g.fkey, g.category AS gold, c.category AS pred,
                  c.confidence, c.tokens_in, c.tokens_out
           FROM gold_labels g
           JOIN classifications c ON c.fkey = g.fkey AND c.valid_to IS NULL"""
    ).fetchall()

    if not rows:
        print("no overlap between gold labels and classifications.\n"
              "label some failures first:  python3 -m flakeagent.eval label --key ... --category ...")
        return

    cats = sorted({r["gold"] for r in rows} | {r["pred"] for r in rows})
    print(f"n = {len(rows)} labelled failures with a current classification\n")
    print(f"{'category':<22}{'prec':>7}{'rec':>7}{'tp':>5}{'fp':>5}{'fn':>5}")
    print("-" * 51)

    # Average precision and recall over their OWN defined sets. Requiring both
    # to be defined would drop a category that the model predicted wrongly and
    # never had a true instance of -- silently hiding a false positive from the
    # headline number.
    precisions, recalls = [], []
    for cat in cats:
        tp = sum(1 for r in rows if r["pred"] == cat and r["gold"] == cat)
        fp = sum(1 for r in rows if r["pred"] == cat and r["gold"] != cat)
        fn = sum(1 for r in rows if r["pred"] != cat and r["gold"] == cat)
        p = tp / (tp + fp) if tp + fp else None
        rc = tp / (tp + fn) if tp + fn else None
        ps = f"{p:>7.2f}" if p is not None else f"{'-':>7}"
        rs = f"{rc:>7.2f}" if rc is not None else f"{'-':>7}"
        print(f"{cat:<22}{ps}{rs}{tp:>5}{fp:>5}{fn:>5}")
        if cat != "unknown":
            if p is not None:
                precisions.append(p)
            if rc is not None:
                recalls.append(rc)

    if precisions or recalls:
        print("-" * 51)
        mp = f"{sum(precisions)/len(precisions):>7.2f}" if precisions else f"{'-':>7}"
        mr = f"{sum(recalls)/len(recalls):>7.2f}" if recalls else f"{'-':>7}"
        print(f"{'macro (excl. unknown)':<22}{mp}{mr}")

    abstained = sum(1 for r in rows if r["pred"] == "unknown")
    correct = sum(1 for r in rows if r["pred"] == r["gold"])
    decided = [r for r in rows if r["pred"] != "unknown"]
    decided_correct = sum(1 for r in decided if r["pred"] == r["gold"])

    print(f"\naccuracy overall      {correct}/{len(rows)} = {correct/len(rows):.0%}")
    print(f"abstention rate       {abstained}/{len(rows)} = {abstained/len(rows):.0%}")
    if decided:
        print(f"accuracy when decided {decided_correct}/{len(decided)} = "
              f"{decided_correct/len(decided):.0%}   <- the number that matters")

    # The dangerous confusion: a real bug waved through as a flake.
    waved = [r for r in rows if r["gold"] == "real_bug"
             and r["pred"] in ("infra_blip", "network_timeout")]
    if waved:
        print(f"\n!! {len(waved)} real bug(s) misclassified as re-runnable flakes "
              "-- the failure mode that makes CI worse:")
        for r in waved:
            print(f"     {r['fkey'][:70]}  (confidence {r['confidence']})")

    tin = [r["tokens_in"] or 0 for r in rows]
    tout = [r["tokens_out"] or 0 for r in rows]
    if any(tin):
        ai, ao = sum(tin) / len(tin), sum(tout) / len(tout)
        per_pr = (ai * PRICE_IN + ao * PRICE_OUT) / 1e6 * JOBS_PER_PR
        print(f"\nmean tokens/classification   {ai:,.0f} in / {ao:,.0f} out")
        print(f"est. cost per PR             ${per_pr:.4f}  "
              f"({JOBS_PER_PR} failing jobs, Opus 5 pricing)")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("label", help="record a hand-read ground-truth label")
    p.add_argument("--key", required=True)
    p.add_argument("--category", required=True)
    p.add_argument("--issue", type=int)
    p.add_argument("--note")
    p.add_argument("--db")

    p = sub.add_parser("list", help="show labelled vs unlabelled failures")
    p.add_argument("--db")

    p = sub.add_parser("score", help="precision/recall/abstention/cost")
    p.add_argument("--db")

    p = sub.add_parser("dossiers",
                       help="score predictions against labelled dossiers")
    p.add_argument("--predictions", required=True,
                   help='JSON: {"<job_id>": "<category>", ...}')
    p.add_argument("--db")

    sub.add_parser("baselines",
                   help="score rules that read no log and call no model -- "
                        "the floor any model result has to clear")

    args = ap.parse_args(argv)
    if args.command == "baselines":            # owns its own connection
        from . import baselines
        return baselines.main()

    conn = store.connect(args.db)
    {"label": cmd_label, "list": cmd_list, "score": cmd_score,
     "dossiers": cmd_dossiers}[args.command](conn, args)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
