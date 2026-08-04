# First measured results

Two models, 39 labelled failures, one harness. **Both models scored far below a
constant, and every verdict either of them was most confident about was wrong.**

> **Read [§6](#6-the-gold-set-cannot-support-a-real_bug-claim-at-all) first.**
> A later audit found the `real_bug` class is six job instances of *one* issue,
> and that a single float — `history.failure_rate >= 0.19` — scores 92% with no
> model and no log. The model numbers below are real, but what they measure is
> narrower than §1–§5 originally claimed, and §2's explanation is now only one
> of two candidates.

Raw outputs are in [`../results/`](../results/); the gold set and its caveats are
in [`../tests/gold_labels.json`](../tests/gold_labels.json). Reproduce with:

```bash
python3 -m flakeagent.agent --dossiers data/dossiers --only-labelled \
        --backend groq --model openai/gpt-oss-120b --out data/preds.json
python3 -m flakeagent.eval dossiers --predictions data/preds.json
```

---

## The numbers

| | gpt-oss-120b | gpt-oss-20b | constant baseline |
|---|---|---|---|
| Completed | 38/39 | **26/39** | 39/39 |
| Accuracy, all | 11/38 = **29%** | 5/26 = **19%** | 33/39 = **85%** |
| Accuracy when it decided | 11/35 = 31% | 5/22 = 23% | 85% |
| Predicted `real_bug` | 23/38 = 61% | 17/26 = 65% | — |
| **Correct at confidence ≥ 0.9** | **0/7** | **0/9** | — |
| Quoted lines found in the log | 142/162 = 88% | 104/107 = 97% | — |

The baseline is the strategy *"answer `race_condition` every time."* It scores 85%
because the gold set is 85% `race_condition`. Neither model beat it; both lost to
it by a wide margin.

---

## 1. Confidence is inverted at the top

| confidence | 120b correct | 20b correct |
|---|---|---|
| 0.4–0.5 | — | 0/4 |
| 0.6 | 2/6 | — |
| 0.7 | 5/13 | 3/6 |
| 0.8 | 4/11 | 2/7 |
| **0.9** | **0/8** | **0/6** |
| **1.0** | — | **0/3** |

Not merely uninformative — **anti-correlated where it matters most.** The 20b model
emitted confidence `1.0` three times and was wrong all three.

`classify.py` opens by asserting that *"a confident wrong answer is worse than no
classifier."* That was a design intuition when it was written. It is now a
measurement, and it argues that any deployment must gate on something other than
the model's own stated confidence, because that signal is actively misleading.

## 2. Both models collapse onto `real_bug`, and that is the task's fault

61% and 65% of verdicts were `real_bug` against a gold rate of 15%. The confusion
is one-directional:

```
race_condition -> real_bug        21
race_condition -> race_condition   9
race_condition -> unknown          3
```

This is `blind()` working exactly as designed. The evidence that a failure is a
flake — it passed on rerun at the same commit, it fails across unrelated commits,
a maintainer later fixed it as a flake — is deliberately withheld, because that is
the evidence the gold labels were derived from. What remains is one log window of
one failing test, and **from a single log a flake and a bug are not
distinguishable.** A human given the same window would have the same problem.

That is the finding, not a bug to be prompt-engineered away: *the categories are
not separable from the artifact the classifier is currently given.*

## 3. The taxonomy is partly to blame

Some of those 21 may be disagreement about words rather than about facts. The gold
labels say `race_condition` because the maintainer's fix commit addressed
concurrency. `taxonomy.py` defines `real_bug` as *"the diff under test broke
this"* — but a race condition **is** a genuine defect, and a model reading
`real_bug` as "a real defect exists" would answer `real_bug` and not be obviously
wrong.

Before treating this as a model failure, the category definitions need to be
testable by two independent readers on the same dossier. That has not been done.

## 4. Structured output is not free on small models

`gpt-oss-20b` failed **13 of 39** requests outright with `Failed to validate JSON`
and `Generated JSON does not match the expected schema` — a 33% hard failure rate
*with* schema enforcement switched on. `gpt-oss-120b` failed once.

For the "local AI is a plus" ambition this matters more than the accuracy gap: the
small model is not merely less accurate, it is unreliable at the output contract.
Anything built on small local models needs a repair path, not just a schema.

## 5. Evidence verification earned its place

10 of 38 `120b` verdicts quoted at least one line that is not in the log they were
shown. Those verdicts read as well-sourced; only the substring check exposed them.

Note the direction: the **smaller** model hallucinated less (97% vs 88%), because
it quoted less (107 lines vs 162). Fluency and citation volume track each other,
and neither tracks correctness.

## 6. The gold set cannot support a `real_bug` claim at all

Found while preparing a second arm, before spending the run. The plan was to
un-blind `attempts` and `history` — legitimate, because the gold labels were
derived from maintainer fix commits, not from rerun history. Checking what that
would expose is what turned this up.

**Every one of the six `real_bug` labels comes from a single issue, [#23281](
https://github.com/podman-container-tools/podman/issues/23281)** — one nil
pointer dereference, fixed by guarding an empty file in
`pkg/machine/compression`. Six job instances of one bug, across two job names
(`macos machine applehv`, `windows machine hyperv`).

The effective sample size for `real_bug` is **1, not 6.**

That confound is measurable. `flakeagent/baselines.py` scores rules that read no
log and call no model:

| rule | accuracy |
|---|---|
| always `race_condition` | 33/39 = 85% |
| job name contains `machine` | 35/39 = 90% |
| **`history.failure_rate >= 0.19`** | **36/39 = 92%** |

One float beats the majority class, and beats both model arms by more than
three times. The two classes barely overlap on it — `race_condition` spans
0.013–0.183, `real_bug` spans 0.183–0.323 — because one persistently broken
`podman machine` test naturally has a high historical failure rate.

Two caveats on that 92%, both printed by the tool rather than left implicit: the
threshold was **fitted on these same 39 labels**, so it is an upper bound and
not a held-out estimate; and it separates one bug's jobs, not a concept.

### What this changes

- **The second arm is cancelled as designed.** Un-blinding `history` hands the
  model the field a threshold already exploits at 92%. A jump in accuracy would
  have measured whether a model can compare a float to a constant. Nothing about
  triage.
- **§2's explanation is now one of two.** The collapse onto `real_bug` may be
  blinding removing the flake signal, as written — or it may be that the class
  is one bug and there was never a general `real_bug` concept to predict. These
  are not distinguishable on this gold set.
- **§1 and §5 survive intact.** Inverted confidence and unverifiable quoting are
  properties of the outputs, not of the label distribution.
- **The real finding may be the inversion.** The signal that separates these
  classes lives in `history`, a counted field — not in the log window the model
  was given. That argues the useful design is an agent that *requests* evidence
  rather than one that reads logs harder.

### Why the gold set is like this, structurally

Labels come from `flakes`-labelled issues with maintainer fix commits. That
label selects for flakes. Real bugs are not filed as flakes, so they enter the
set only by accident — as #23281 did, and only once.

**A balanced flake-vs-bug gold set cannot be mined from the `flakes` label
alone.** `real_bug` examples need a different source: failures on PRs whose fix
changed the code under test. That is a separate mining path, not more labelling
effort on the current one.

---

## What these numbers do not support

- **Any claim that one model is better.** 39 items, two classes, and 13 missing
  results for the 20b arm. The gap is directionally clear but the sample is small,
  and `eval.py` prints a warning below 30 scored items for that reason.
- **Any claim about `infra_blip`, `network_timeout` or `resource_exhaustion`.**
  The gold set contains none of them. `infra_blip` structurally cannot appear,
  because infrastructure blips are not fixed by commits and therefore never enter
  `fix_commits`.
- **A verdict on prompt design.** Only one prompt was tested. The A/B that would
  matter — thin summary plus retrieval tools versus the whole dossier inline — has
  not been run.

## What to do next, in order

Reordered after §6. Fixing the gold set now gates everything downstream — no
arm run against the current one can be interpreted.

1. **Mine `real_bug` examples from a source that is not the `flakes` label.**
   Failures on PRs whose merged fix touched the code under test. Until
   `real_bug` spans more than one issue, no accuracy number on this set means
   anything, and `baselines.py` will keep beating every model.
2. **Report `baselines.py` beside every model result, permanently.** The floor
   is 92%, not 85%, and it was invisible until something looked.
3. **Test whether the taxonomy is separable by humans** — two readers, same
   dossiers, measure agreement. Still unresolved, still cheap.
4. **Then** run the evidence arm, on a gold set where `real_bug` is not one bug.
5. **Do not gate anything on model confidence.** Section 1 is unambiguous and is
   unaffected by the gold-set problem.
6. **Add a repair path for malformed structured output** before any local-model
   deployment claim.
