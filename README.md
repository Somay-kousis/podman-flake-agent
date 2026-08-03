# podman-flake-agent

A prototype for [LFX Mentorship: Agentic CI Flake Categorization and Analysis](https://github.com/podman-container-tools/podman/issues/29265)
(podman-container-tools/podman #29265).

Ingests failing GitHub Actions runs from Podman's CI, extracts the failing test
from logformatter's HTML, asks a model *why* it failed, and reports the verdict —
with an evaluation harness measuring whether the verdict is any good.

**Status: prototype.** Written to demonstrate approach and judgment, not to be
deployed. It files nothing and posts nothing.

---

## Why this exists

Podman migrated off Cirrus CI in **May 2026** — commit `3743b9f806`, "Goodbye
Cirrus". That migration orphaned `hack/ci/logformatter`, the 38KB Perl script
that classified every subtest as pass / fail / skip / **flake**. It is keyed
entirely to Cirrus environment variables (`CIRRUS_TASK_ID`,
`CIRRUS_CHANGE_IN_REPO`) and is no longer invoked by any workflow; the only
surviving reference is a stale comment at `Makefile:726`.

So the documented triage process today is a human reading a bar graph:

> `CONTRIBUTING.md:355` — "Most notably, the tests will occasionally flake…
> Alternating red/green bars is indicative of a testing 'flake', and should be
> examined (anybody can do this)"

…followed by hand-written heuristics — one task failing usually means networking
or a brief outage; multiple tasks failing implies a shared cause; everything
failing means something serious. That taxonomy is almost exactly the
infra-blip / race-condition / network-timeout split the mentorship describes.
`REVIEWING.md:24` separately asks every reviewer to check failures against known
flakes by hand.

PR [#29091](https://github.com/podman-container-tools/podman/pull/29091)
(Luap99, open) restores logformatter under GHA and adds
`hack/ci/github_log_summary.py` to push failed-test summaries into
`GITHUB_STEP_SUMMARY` — *"so maintainers can see if the failed log was just some
flake or an actual problem with the PR."* **This prototype is designed to sit
directly downstream of that PR** and reuses its parsing strategy rather than
competing with it.

---

## The three problems that actually matter

Anything can call an LLM on a log. These are the parts that decide whether the
result is deployable.

### 1. Ground truth has to be mined

`GINKGO_FLAKE_ATTEMPTS ?= 0` (`Makefile:150`) — Podman does **not** automatically
re-run failing tests. So "failed, then passed on re-run" is not a signal anyone
hands you. `ingest.py signals` mines three proxies instead:

| Signal | Strength | How it's derived |
|---|---|---|
| `rerun_disagreement` | 0.9 | Same job name, **same commit SHA**, different outcomes across `run_attempt`s. The strongest available evidence. |
| `cross_pr` | 0.4–0.85 | Same test failing on ≥2 unrelated commits — a property of the test, not of any one diff. |
| `main_failure` | 0.6 | Post-merge failure on `main`, the signal `CONTRIBUTING.md:353` points maintainers at. |

### 2. Cost is the design constraint

`ci.yml` `big-tests` is 4 distros × 2 tests × 2 privilege levels × 2 modes
(minus exclusions), plus `small-tests` — 30+ jobs per PR, each uploading a full
journal. Feeding raw logs to a model is not affordable.

So `parse.py` extracts only the failing test's block before any inference.
Measured on Podman's own logformatter fixtures:

| Fixture | Before | After | Reduction |
|---|---:|---:|---:|
| `simple-ginkgo` | 11,149 chars | 1,109 | **90.1%** |
| `simple-python` | 8,832 | 584 | **93.4%** |
| `simple-bats` | 1,712 | 368 | **78.5%** |
| `bats-with-timestamps` | 5,482 | 1,299 | **76.3%** |

Reproduce: `python3 tests/test_parse.py`.

### 3. A confident wrong answer is worse than no tool

Calling a real race condition an "infra blip" tells a maintainer to press
re-run on a genuine bug. So `unknown` is a first-class verdict, abstention rate
is reported alongside accuracy, and `eval.py` explicitly counts the dangerous
confusion — real bugs waved through as re-runnable flakes.

---

## Design notes

**Line-oriented parsing, not DOM nesting.** Upstream's
`github_log_summary.py` keeps `tt` elements containing a `log-failed`
descendant. That's correct for its purpose but too coarse here:
`<div class='tt'>` is opened **once around the whole processed output**
(`logformatter:249`), not per test. And in real ginkgo output the failure
summary (`• [FAILED]` plus its `h2.log-failed` name components) is emitted
*outside* the `div.ginkgo-timeline` blocks holding the diagnostic detail — no
single DOM subtree contains both. logformatter is a line-oriented emitter, so
this parser treats the HTML as a stream of annotated lines and captures bounded
regions. That's what yields per-test granularity, which is what makes
deduplication against the `flakes` issues possible at all.

*(One consequence worth reporting upstream: logformatter folds the full podman
command line into a `title` attribute containing newlines, so a single tag can
span many source lines. Any per-line tag-stripping leaves raw markup in the
extracted text — it cost ~11% of the ginkgo reduction above until fixed.)*

**Non-destructive classification history.** A flake is not a static fact — it
appears, gets diagnosed, gets fixed, regresses. `store.consolidate()` returns
`ADD` / `UPDATE` / `INVALIDATE` / `NOOP` and never destructively overwrites; rows
are closed with `valid_to` and superseded. Re-analysing an unchanged failure is a
`NOOP`, not a duplicate row. This is what keeps "what did we believe in June, and
were we right?" answerable.

**Zero required dependencies.** Standard library only — matching upstream's own
`github_log_summary.py`, and so a reviewer can clone and run without a
virtualenv. The `anthropic` SDK is imported lazily and needed only for
`--backend api`; the local path has no dependencies at all.

---

## Running it

### Without a token or a network

Everything here works from a fresh clone with nothing configured:

```bash
python3 tests/test_parse.py                       # parser vs real logformatter fixtures
python3 tests/test_steps.py                       # step attribution
python3 tests/test_agent.py                       # dossier -> prediction, stub model
python3 examples/read_dossier.py tests/dossiers/*.json | head -40
python3 -m flakeagent.agent --dossiers tests/dossiers --dry-run
```

`tests/dossiers/` holds real committed failures. **This is what agent
development iterates against** — no token, no API budget, no waiting.

### With a token

Put it in a gitignored `.env` at the repo root:

```
GITHUB_TOKEN = ghp_...
```

Fine-grained, **public repositories, read-only**. Nothing here writes to GitHub;
the client is GET-only by construction.

```bash
./hack/full_fetch.sh data/flakes.db 30      # everything, ~45-60 min, resumable
python3 -m flakeagent.fetch status --db data/flakes.db
python3 -m flakeagent.dossier --recent 400 --out data/dossiers/
```

Or a stage at a time:

```bash
python3 -m flakeagent.fetch runs   --days 30    # + validate.yml, unit-tests.yml
python3 -m flakeagent.fetch jobs                # + per-step outcomes
python3 -m flakeagent.fetch logs                # gzipped, ~14% of raw
python3 -m flakeagent.fetch prfiles             # diff relevance
python3 -m flakeagent.fetch issues              # + pasted log excerpts
python3 -m flakeagent.fetch timeline            # precise issue -> fix links
python3 -m flakeagent.fetch fixes               # broad issue -> fix links
```

Every response is cached and revalidated with ETags, so re-running costs almost
nothing — a `304` consumes no rate-limit quota.

### Labelling and scoring

Two namespaces, because a `flakes` issue is a recurring problem and a dossier is
one job that failed. Only the second can score a prediction.

**Learn the domain** from issues — cheap, and it tells you what the categories
should actually be:

```bash
python3 -m flakeagent.labels list                # 242 with an identified fix
python3 -m flakeagent.labels show --issue 28940  # symptom and fix, side by side
```

It shows *"set ulimits flake — crun: clone: Resource temporarily unavailable"*
next to *"test system: increase nproc ulimit to avoid flake"*, and suggests
nothing. An eval set built with the same heuristics a classifier uses cannot
tell you whether the classifier works.

**Build the eval set** from dossiers — these are what you can score:

```bash
python3 -m flakeagent.labels dossiers            # ranked by independent evidence
python3 -m flakeagent.labels show --dossier <job_id>
python3 -m flakeagent.labels set  --dossier <job_id> --category infra_blip --note "why"
python3 -m flakeagent.labels stats
```

`show --dossier` leads with evidence that owes nothing to the log — rerun
disagreement, how many commits the test failed on, whether the diff is inert,
the maintainer's fix — and puts the log last. **Decide from the top.** If you
have to read the log to decide, that item is a weak eval case.

**Then withhold that evidence** from whatever you are scoring:

```bash
python3 -m flakeagent.dossier --job <id> --blind --out data/blind/
python3 -m flakeagent.eval dossiers --predictions preds.json
```

`--blind` drops the fields the labelling view showed you. Otherwise a high score
means "it reads logs the way I do", not "it is right" — a misleading log misleads
you both identically.

`preds.json` is `{"<job_id>": "<category>"}` from any source. The scorer reports
abstention beside accuracy, names real bugs predicted as re-runnable, and warns
below 30 items that a percentage is not an accuracy claim.

### Running a model over the dossiers

`agent.py` is the dossier → prediction path. It blinds each dossier, builds a
prompt, calls a backend, and writes the `{job_id: category}` file `eval.py`
scores:

```bash
python3 -m flakeagent.agent --dossiers data/dossiers --dry-run      # no model called
python3 -m flakeagent.agent --dossiers data/dossiers --limit 30 --backend ollama
python3 -m flakeagent.eval  dossiers --predictions data/preds.json
```

Measured over the 400 generated dossiers, the prompt is a **median 1,678
characters (~419 tokens)**, max 13,080.

Three things it does deliberately:

- **Blinded by default.** The evidence a human labels from is withheld from the
  model. `--no-blind` exists to quantify the gap, but you have to ask, and the
  mode is recorded in every verdict.
- **Evidence is checked, not trusted.** The schema asks for verbatim log lines;
  nothing in a schema can make them real. Every quote is matched against the log
  the model was shown, and the hit rate is reported per verdict and summarised
  as `UNVERIFIED n` at the end. It never edits the category — it reports.
- **`--dry-run` is the way in.** Every prompt built and counted, no model, no
  key. Three of the worst bugs in this repo's history shipped in code that had
  been written and reviewed but never run.

Output is split on purpose: `preds.json` is only `{job_id: category}` so it
stays diffable, and `verdicts.json` carries reasoning, evidence, verification
counts and token usage.

### Earlier prototype

`classify.py` still reads the older `test_failures` path rather than dossiers;
`agent.py` supersedes it for dossier work but reuses its two backends and the
`search_flake_issues` tool. `report.py` is dry-run only and has no `--post`
flag.

---

## What this does *not* do

- **No model accuracy number yet.** The gold set now exists --
  [`tests/gold_labels.json`](tests/gold_labels.json), 39 failed jobs labelled from the
  maintainer's own issue title and fix commit, never from the log window, so the
  labels stay independent of what the classifier is shown. But it is **heavily
  imbalanced: always answering `race_condition` scores 85%**, which is the bar any
  model has to beat. `infra_blip` has zero examples and structurally cannot get any
  from this evidence stream, because infrastructure blips are not fixed by commits.
  Report per-class precision and recall, not overall accuracy. Every other number in
  this README is a size or reduction measurement.
- **Fork PRs have no diff context** — GitHub omits the PR association for them,
  and that is ~160 of 184 runs. See [`docs/FETCH_AUDIT.md`](docs/FETCH_AUDIT.md).
- **Ginkgo HTML parsing is 0%** on the real corpus. Step-window slicing routes
  around it rather than solving it.
- **No posting, ever.** Nothing in this package can write to GitHub.
- **Only failures are observable.** A test that flakes but passes leaves no
  trace, so any rate computed here is a rate among observed failures, not among
  runs. This one bounds every conclusion — see the audit.

## Layout

| Path | |
|---|---|
| `flakeagent/gh.py` | GET-only GitHub client — cached, conditional, rate-limit aware |
| `flakeagent/fetch.py` | the acquisition pipeline (runs, jobs, logs, issues, fixes…) |
| `flakeagent/logslice.py` | narrow a 500KB log to the failing step, ~95% |
| `flakeagent/dossier.py` | one JSON per failed job — the consumer interface |
| `flakeagent/labels.py` | hand-assign ground truth from maintainer fixes |
| `flakeagent/corpus.py` | log excerpts harvested from `flakes` issues |
| `flakeagent/store.py` | SQLite persistence + migrations |
| `flakeagent/schema.sql` | the data model |
| `flakeagent/parse.py` | logformatter HTML → failures (earlier path) |
| `flakeagent/taxonomy.py` | the categories, schema and system prompt — defined once |
| `flakeagent/agent.py` | dossier → prompt → verdict → `preds.json` |
| `flakeagent/eval.py` | scores `preds.json` against the gold labels |
| `flakeagent/classify.py`, `report.py` | earlier prototype, pre-dossier |
| `docs/` | **[glossary](docs/GLOSSARY.md)** · **[handbook](docs/HANDBOOK.md)** · [roadmap](docs/ROADMAP.md) · [dossier schema](docs/DOSSIER.md) · [fetch audit](docs/FETCH_AUDIT.md) · [log anatomy](docs/LOG_ANATOMY.md) · [decision map](docs/MAP.md) · [plans](docs/plans/) |
| `tests/dossiers/` | committed real failures for offline development |

Licensed Apache-2.0, matching Podman.
