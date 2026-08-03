# First measured results

Two models, 39 labelled failures, one harness. **Both models scored far below a
constant, and every verdict either of them was most confident about was wrong.**

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

1. **Test whether the taxonomy is separable by humans** before blaming the model.
   Two readers, same dossiers, measure agreement.
2. **Give the model the flake signal it is currently denied** — a second arm where
   rerun history *is* included, scored against a gold set that was not derived from
   it. That isolates how much of the 29% is missing evidence rather than missing
   reasoning.
3. **Do not gate anything on model confidence.** Section 1 is unambiguous.
4. **Add a repair path for malformed structured output** before any local-model
   deployment claim.
