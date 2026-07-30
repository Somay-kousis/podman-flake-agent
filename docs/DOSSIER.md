# The dossier

One JSON document per failed CI job. This is the boundary between data
acquisition and everything built on top — a consumer never needs to touch the
database or the GitHub API.

```bash
python3 -m flakeagent.dossier --job 90896283356               # to stdout
python3 -m flakeagent.dossier --recent 400 --out data/dossiers/
python3 -m flakeagent.dossier --run 30549428074 --out data/dossiers/
```

Committed examples live in `tests/dossiers/` — real failures, usable with **no
token and no network**.

---

## The contract

**Nothing in a dossier is scored, ranked, or classified.** There is no verdict
field, no confidence number, no probability. Every value is either fetched from
the GitHub API or computed by counting rows.

Where a fact is missing the document says so and why, rather than omitting the
key or guessing a default. `provenance` records when each part was fetched, so
stale data is distinguishable from fresh.

That separation is the point: judgement belongs to whatever reads this. If the
dossier contained opinions you could not tell them from observations.

---

## Top-level keys

| Key | Answers |
|---|---|
| `schema_version` | Integer. Currently `1`. |
| `job` | What ran, where, for how long |
| `failing_step` | **Which step failed** — infra vs test, without reading a log |
| `log_window` | The failing step's output, narrowed ~95% |
| `run` | The workflow run it belonged to |
| `pull_request` | What change was under test |
| `siblings` | Did other jobs in the same run fail? |
| `history` | How often does this job fail? |
| `attempts` | **Did the same commit pass on another attempt?** |
| `related_issues` | Existing flake reports naming the same test |
| `known_fixes` | What a maintainer actually did about those |
| `provenance` | Where this came from and when |

---

## `failing_step`

The cheapest signal in the document. `job.steps[]` comes free with the jobs API
call — no log, no artifact, no extra request.

```json
{ "found": true,
  "first": { "number": 7, "name": "Run machine e2e",
             "started_at": "...", "completed_at": "..." } }
```

A job that died in `Install build dependencies` is infrastructure. One that died
in `Run machine e2e` is a test. That distinction costs nothing and resolves a
large share of triage before any model is involved.

`found: false` means the job predates step capture, not that no step failed.

## `log_window`

```json
{ "available": true,
  "text": "...",
  "failing_tests": [{"kind": "ginkgo", "name": "podman machine rm Remove running machine"}],
  "line_count": 261, "source_line_count": 5316,
  "reduction_pct": 95.1, "est_tokens": 4187,
  "failure_markers": 5,
  "reason": "sliced to the failing step's time interval; focused on 3 region(s)..." }
```

Two narrowing stages, neither of which parses the test suite:

1. **Time slice.** Every GHA log line carries an ISO timestamp and `job_steps`
   records each step's interval, so the failing step's output is a comparison.
2. **Anchor focus.** The failing step is usually most of the log, so regions are
   kept around markers that denote a *reported* failure — `[FAILED]`,
   `Summarizing N Failure`, `not ok N`, `##[error]`.

The anchors are deliberately strict. A bare `Error:` matched 67 times in one
sampled log and nearly all were **expected** output from negative tests
(`Error: foobar: VM does not exist` is a test asserting absence). Read `reason`
before trusting the window — it states which path produced it, including the
fallbacks.

`failing_tests` is the *test* name, not the job's. This matters: the job is
called `macos machine applehv`, which matches nothing; the test is
`podman machine rm Remove running machine`, which matches real issues.

## `pull_request`

```json
{ "number": 29344, "file_count": 2, "all_paths_inert": true,
  "note": "all changed paths are vendored/docs/dependency files" }
```

`all_paths_inert` is descriptive, not a verdict. It says the diff touched only
markdown, `vendor/`, `docs/`, `go.mod`/`go.sum`. `.github/workflows/` is
deliberately **not** inert — workflow edits genuinely break jobs.

> **Absent for most PR runs.** GitHub omits the PR association for fork PRs, and
> Podman takes nearly all contribution that way — 160 of 184 runs in a sample.
> Expect `present: false` far more often than not.

## `attempts`

```json
{ "runs_on_this_commit": 2, "max_attempt": 2,
  "job_outcomes_on_this_commit": ["failure", "success"],
  "disagreement": true }
```

**`disagreement: true` is the strongest flake evidence obtainable**: the same
commit both passed and failed, so the code cannot be the difference.

It exists only because a human pressed re-run — `GINKGO_FLAKE_ATTEMPTS` defaults
to `0`, so CI never generates it. Present on roughly 18% of runs.

## `related_issues` and `known_fixes`

```json
"related_issues": {
  "candidates": [{"number": 23472, "title": "machine rm: unable to clean up gvproxy...",
                  "shared_terms": ["machine","remove"], "match_source": "test"}],
  "matched_against": ["podman machine rm Remove running machine"] },

"known_fixes": {
  "issues": [{"number": 28940, "has_identified_fix": true,
              "fix_commits": [{"message": "test system: increase nproc ulimit to avoid flake",
                               "source": "search"}]}] }
```

`related_issues` is **lexical overlap only** — a starting point, never a
duplicate determination. `match_source` is `test` when matched on the failing
test's name and `job` when it fell back to the job name; a `job` match is weak.

`known_fixes` is the only supervised signal in the document. The issue reports a
symptom; the commit that closed it states the cause:

> **#28940** *"set ulimits flake — crun: clone: Resource temporarily unavailable"*
> → *"test system: increase nproc ulimit to avoid flake"*

That is evidence from a maintainer, not a classification by this tool.

---

## Limits to build around

Documented fully in [`FETCH_AUDIT.md`](FETCH_AUDIT.md); the ones that bite a
consumer directly:

| Limit | Effect on a dossier |
|---|---|
| Fork PRs unresolvable | `pull_request.present` is usually `false` |
| Artifacts expire at 90 days | Older jobs have no `log_window` |
| Cirrus-era logs gone | Pre-May-2026 issue links are dead |
| `history` is local-only | Counts cover stored runs, not all of CI history |
| **Flakes that passed leave no trace** | Every dossier is a *failure*. Any rate computed across them is a rate among observed failures, not among runs. |

That last one is the one to internalise before quoting a number from this data.

### Two traps found the hard way

**Aggregating a branch's steps conflates revisions.** A query over `job_steps`
found a step failing 30 times on an open PR's branch, which read as a live bug.
Bucketing by date showed **all 30 landed on one day — the day the PR opened** —
and every run since was clean. The author had fixed it within 24 hours.

A branch accumulates the whole history of a PR, so a step that failed in the
first revision is indistinguishable from one failing now unless the query says
so. **Always bucket by date before reporting a count as current.** Reporting the
un-bucketed version to a maintainer would have meant describing a bug they fixed
a month earlier.

**The time-slice degrades on very short steps.** `log_window` is cut by
comparing each line's timestamp against the failing step's interval. On a
29-minute test step that is precise. On a **1-second** step it is not — GHA log
lines are stamped when flushed, so a one-second window can capture output that
belongs to a neighbouring step. Observed returning `make install` output for a
1-second step.

Check `log_window.step.started_at` against `completed_at`: a sub-second or
one-second span means treat the window as approximate. Long steps — which is
where test failures live — are unaffected.

---

## Labelling and scoring

A dossier is one job that failed. A `flakes` issue is a recurring problem. They
are different objects, so an issue label cannot score a job prediction — the
label and the prediction have to describe the same thing.

`gold_labels` therefore holds two namespaces:

| Key | Means | Scoreable |
|---|---|---|
| `job:<id>` | this specific failure | **yes** |
| `issue:<n>` | this recurring problem | no — domain learning |

### Label from evidence the classifier won't see

If you decide a category by reading the log window, and the classifier reads the
same log window, a high score means *"it reads logs the way I do"* — not
*"it is right"*. A misleading log misleads you both identically.

So `labels show --dossier` puts the independent evidence first and the log last:

```
INDEPENDENT EVIDENCE  (judge from this)
  failing step   4: Run test on lima  (1245s)
  rerun          SAME COMMIT both passed and failed -> it is a flake
  spread         this job failed on 15 distinct commit(s)
  siblings       2 of 55 other jobs in the run also failed
  diff           7 file(s); ALL inert (docs/vendor) -- the diff cannot have caused this
  known fixes    #28940 -> "test system: increase nproc ulimit to avoid flake"

LOG  (the classifier sees this; try not to decide from it)
  ...
```

`labels dossiers` ranks candidates by how much of that evidence exists, so you
label the judgeable ones first. It excludes the `Total Success` gate job, which
fails whenever anything else does and would teach a classifier nothing.

### Then withhold it

```bash
python3 -m flakeagent.dossier --job <id> --blind --out data/blind/
```

`--blind` drops `known_fixes`, `related_issues`, `attempts`, `history`, and the
`all_paths_inert` verdict — exactly the fields the labelling view showed you.
What remains is the job, the failing step, the log window, and the raw diff.

### Score

```bash
python3 -m flakeagent.eval dossiers --predictions preds.json
```

`preds.json` is `{"<job_id>": "<category>"}` from whatever you built. The scorer
is deliberately decoupled from any classifier in this repo, reports abstention
alongside accuracy, and calls out the dangerous confusion explicitly — a
`real_bug` predicted as `infra_blip` or `network_timeout` tells a maintainer to
press re-run on a genuine bug.

It also warns when the sample is under 30 items, because a percentage over
twelve dossiers is not an accuracy claim.

---

## Reading one

```bash
python3 examples/read_dossier.py tests/dossiers/<file>.json
```

```python
import json
d = json.load(open(path))

step = d["failing_step"]["first"]["name"] if d["failing_step"]["found"] else None
tests = [t["name"] for t in d["log_window"]["failing_tests"]]
evidence = d["log_window"]["text"]
flake_hint = d["attempts"]["disagreement"]
diff_inert = d["pull_request"].get("all_paths_inert")
```

Guard every optional section — absent data is represented explicitly, so
`d["pull_request"]["present"]` and `d["log_window"]["available"]` are worth
checking before reaching further in.
