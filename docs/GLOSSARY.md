# Glossary

Every term this project uses, in plain language. Written to be read top to
bottom the first time, and dipped into after that.

Terms marked **★** are this project's own inventions — you will not find them in
Podman's docs, because they were coined here.

---

## 1. The problem

**Test suite** — a program that runs your code and checks it did the right
thing. Podman has thousands of these checks.

**CI (Continuous Integration)** — a robot that runs the whole test suite every
time anyone proposes a code change, so broken code is caught before it is
merged. Podman's CI runs 30+ separate jobs per proposed change.

**Flake / flaky test** — *the word this whole project exists for.* A test that
fails sometimes and passes other times **without the code changing.** The test
is unreliable, not the code. It is the most expensive kind of failure in CI,
because a human has to look at it and decide "is this a real bug, or just the
test being flaky again?" — and they have to do that every single time.

**Triage** — the act of making that decision. Podman's documented triage process
today is a human looking at a bar graph of red and green bars and using judgement
(`CONTRIBUTING.md:355`).

**Root cause** — *why* it actually failed. "The test failed" is a symptom;
"the container registry was briefly unreachable" is a root cause.

---

## 2. GitHub and CI vocabulary

**Repository / repo** — a project's folder of code plus its full history.

**Commit** — one saved change, with a message describing it.

**SHA** — the 40-character fingerprint identifying a commit uniquely, e.g.
`344d79131a2b…`. Also called the commit ID or hash.

**Branch** — a parallel line of work. `main` is the official one.

**PR (Pull Request)** — a formal proposal to merge your branch into `main`.
Reviewers comment on it; a maintainer merges or closes it.

**Fork** — your own copy of someone else's repo. You work in a fork and open PRs
back to the original. *Relevant here:* GitHub often will not tell you which PR a
fork's commit belongs to, which is why diff information is missing for ~80% of
the runs in this dataset.

**Diff** — the actual lines added and removed by a change.

**Maintainer** — someone with authority to merge. For Podman, @Luap99 and others.

**GitHub Actions** — GitHub's built-in CI. Podman moved to it in May 2026.

**Workflow** — one CI configuration file, e.g. `ci.yml`.

**Run** — one execution of a workflow, triggered by a push or a PR.

**Job** — one machine's worth of work inside a run. Podman's `ci.yml` fans out to
30+ jobs — different Linux distributions, root vs rootless, local vs remote.

**Step** — one command inside a job: "check out the code", "install packages",
"run the tests". *Relevant here:* **which step failed tells you most of what you
need without reading a single log line.** If "Set up job" failed, the tests never
even ran, so it cannot be a code bug.

**Runner** — the physical or virtual machine a job runs on. A sick runner can
cause failures that look like code bugs.

**`run_attempt`** — reruns are numbered. Attempt 1 failed, someone pressed
"re-run", attempt 2 is the same code again. **18% of runs in this dataset have
more than one attempt.**

**Log** — everything the job printed. Podman's are ~500 KB per failed job, up to
19 MB raw.

**Rate limit / quota** — GitHub caps API requests: 60/hour anonymous, 5,000/hour
with a token.

**Token / PAT** — a password-like string proving who you are to the API. This
project's is read-only and public-repos-only. Kept in a gitignored `.env`.

**ETag / `304 Not Modified`** — a fingerprint GitHub gives each response. Send it
back and if nothing changed you get a tiny `304` instead of the whole thing —
**free, does not count against quota.** This is why re-running the fetch is cheap.

**`422 Unprocessable Entity`** — here it means "that commit is not in this
repository" — it lived in a fork, or history moved. Three quarters of the fix
links hit this and are permanently unrecoverable.

---

## 3. Podman-specific

**Cirrus CI** — the CI system Podman used *before* May 2026.

**`logformatter`** — a 38 KB Perl script that turned raw CI output into readable
HTML, colour-coding each test as pass / fail / skip / **flake**. It was wired to
Cirrus, so when Podman left Cirrus it stopped being called by anything. **The gap
this project sits in.** PR #29091 (by @Luap99) is bringing it back.

**Ginkgo** — the Go testing framework for Podman's integration tests. Verbose,
structured output.

**bats** — Bash Automated Testing System, for the shell-based system tests.

**journald / journal** — Linux's system log. Podman flakes are frequently
*journald timeliness* problems: a log line is genuinely written but has not
appeared yet when the test looks for it. Classic race condition.

**rootless / rootful** — running containers as a normal user vs as
administrator. Different code paths, tested separately.

**`GINKGO_FLAKE_ATTEMPTS ?= 0`** — Podman does **not** automatically retry failed
tests. So "failed, then passed on retry" is not a label anyone hands you; it has
to be mined. That is why the evidence signals below exist.

---

## 4. This project's own terms ★

**Dossier ★** — *the central idea.* One JSON file per failed job, holding
everything known about that failure: which step failed, the narrowed log, what
happened on reruns, how often this test fails elsewhere, whether the code change
could plausibly be responsible, and any matching bug reports. It is assembled
from the GitHub API rather than by parsing text. 400 of them exist; 5 are
committed so anyone can work offline.

**Log window ★** — the slice of the log that actually matters. A 500 KB log is
cut to the failing step's time interval, then narrowed again around the failure
markers. **~95% smaller, median ~1,336 tokens.** Sending a whole log to a model
is unaffordable at 30 jobs per change.

**Step-window slicing ★** — the technique above. Notable because it replaced a
hard parsing problem: the HTML parser handled bats at 96% but ginkgo at **0%**,
and slicing gets the failure text out *without parsing the test framework at all.*

**Inert file ★** — a changed file that cannot possibly have caused a test
failure: documentation (`.md`, `.txt`), vendored dependencies, templates. If a
change touches *only* inert files and a test failed, the change is innocent and
it is a flake. Implemented in `dossier.py:_inert`.

**Independent evidence ★** — anything you can judge a failure by **without
reading its log.** The three main ones:

| Signal | Strength | What it means |
|---|---|---|
| **Rerun disagreement** | 0.9 | The *same commit* both passed and failed. Proof of a flake, no interpretation needed. 82 dossiers have this. |
| **Cross-PR** | 0.4–0.85 | The same test fails across unrelated changes — so it is a property of the test, not of any one change. |
| **Main failure** | 0.6 | It failed on `main`, after merging, where there is no PR to blame. |

**Blinding ★** — *the most important idea in the project.* Before showing a
failure to the model, **delete all the independent evidence.** Why: if you label
a failure by reading the log, and the model classifies it by reading the same
log, a high score only proves *"the model reads logs the way I do"* — you can
both be fooled by the same misleading log. So: **label from evidence the model
cannot see, then hide that evidence.** Only then does the score mean something.
Implemented as `dossier.blind()`.

**Gold label ★** — a hand-assigned, human-decided correct answer for one
dossier. The yardstick everything is measured against. **There are 39**, and
they have since been audited: all 6 `real_bug` labels come from one issue, so
the effective sample size for that class is 1. See
[RESULTS.md §6](RESULTS.md#6-the-gold-set-cannot-support-a-real_bug-claim-at-all).

**Rule layer ★** — the same triage task with no model in it: which step failed,
plus a short list of error strings that only appear when the thing they name
actually went wrong. `triage.py`. It exists so that a model result is never
reported without something free beside it, and so that CI has something it can
run every six hours for nothing.

**Step role ★** — what a failing step *was for*: `setup`, `test`, `build`,
`report`, or `aggregate`. A job that dies in a setup step never ran the tests, so
the change under test is innocent whatever the log says. Measured over 30 days,
this filters a quarter of failing steps away as noise and resolves 2.8% of the
rest outright — useful, and much less than it first looks.

**Abstention set ★** — the failures the rule layer answered `unknown` on: 83% of
a live two-day window. **This is the specification for the agent.** It is the
work that a model has to justify its cost on, and it is a set of files on disk
rather than an opinion.

**Baseline ★** — the cheapest thing that could have produced a number, reported
next to it. Two exist: always answering the most common category (85%), and
comparing one float to a constant (92%). Both beat every model arm measured so
far, which is a fact about the gold set rather than about models.

**Consolidation verbs ★** — `ADD` / `UPDATE` / `INVALIDATE` / `NOOP`. A flake is
not a static fact: it appears, gets diagnosed, gets fixed, comes back. Rather
than overwriting what was previously believed, old rows are closed and new ones
supersede them, so *"what did we think in June, and were we right?"* stays
answerable. Re-analysing an unchanged failure is a `NOOP`, not a duplicate.

**Provenance ★** — a record of where each field came from. The dossier's says
plainly: *"every field is fetched from the GitHub API or counted from stored
rows; nothing is inferred, scored, or classified."*

---

## 5. AI and evaluation vocabulary

**Model / LLM** — the AI doing the classifying. Two are supported: a local one
via **Ollama** (no key, no cost, runs on your machine) and Claude via the API.

**Prompt** — the text sent to the model. Here: the job facts, then the log
window, then "classify this failure."

**Token** — roughly ¾ of a word; how model input is measured and billed. This
project's prompts are a median **~419 tokens**.

**Context / context bloat** — how much text you push at the model. More is not
better: it costs money and buries the signal.

**Structured output / JSON schema** — forcing the model to answer in a fixed
shape (`category`, `confidence`, `evidence`…) rather than free prose, so the
answer can be processed by a program instead of re-parsed hopefully.

**Backend** — which model is being used. `--backend ollama` or `--backend api`.

**Ground truth** — what is actually true, as opposed to what the model guessed.
Here, the gold labels.

**Eval / evaluation harness** — the machinery that runs the model over known
cases and scores it.

**Accuracy** — the share it got right.

**Abstention** — the model answering `unknown` instead of guessing. **Counted as
a first-class outcome here, not a failure.** A classifier that never abstains is
worse than none: confidently calling a real bug an "infra blip" tells a
maintainer to press rerun on a genuine problem.

**Dangerous confusion ★** — the specific error that matters most: a **real bug**
predicted as something re-runnable. `eval.py` counts it separately from ordinary
mistakes, because it is the one that actually costs somebody something.

**Hallucination** — the model inventing something. Here, quoting a log line that
is not in the log. `agent.py` substring-checks every quote and reports the hit
rate, so this shows up as a number rather than having to be caught by reading.
`triage.py` is held to the same check, even though its quotes are sliced out of
the log and so cannot be invented -- if that check ever fails there, it is a bug
in a rule.

**Fixture** — a small piece of real data committed to the repo so tests can run
offline, with no network and no API key.

---

## 6. The six categories

What a verdict can be. Defined once in `taxonomy.py`, used by both the human
labelling and the model.

| Category | Plain meaning |
|---|---|
| `infra_blip` | Something outside the code broke — a registry or mirror was down, DNS failed, a package would not install, the machine died. |
| `network_timeout` | A network operation was too slow and gave up. Distinct from the above: the server *was* reachable, just slow. |
| `race_condition` | A timing or ordering problem. Two things happened in an unlucky order. Podman's most common flake, often journald timeliness. |
| `resource_exhaustion` | Ran out of something: disk, memory, process IDs, ports, file limits. |
| `real_bug` | **Not a flake.** The code change actually broke this. Specific and repeatable rather than timing-dependent. |
| `unknown` | The evidence does not distinguish. **A valid and useful answer**, not a failure to answer. |

---

## 7. The program

**LFX Mentorship** — the Linux Foundation's paid, remote, 12-week open-source
mentorship program. This project is an application to the Podman one.

**CNCF** — the Cloud Native Computing Foundation, which runs the projects LFX
mentorships attach to.

**Term** — one 12-week cycle. This is Term 3, 2026: Sep 7 – Nov 27.

**Mentor** — the maintainer who supervises the mentee. Here, @Luap99,
@timcoding1988, @mohanboddu.

**Cover letter** — the written application. For Podman it is what actually gets
read: *"contributions in Podman are not a requirement to apply… that can also be
some personal project."*

**Stipend** — the payment, region-adjusted, in two instalments after the midterm
and final evaluations.

---

## 8. Things you will see in this repo

**`.gitignore`** — a list of files git should never save. Here it keeps the
226 MB of cached data, the `.env` token file, and the databases out of the repo.

**SQLite** — a database that is a single file on disk. `data/flakes.db`.

**Schema** — the shape of the database: which tables exist and what columns.

**Read-only by construction** — this client has one code path, `GET`, with no
method argument. It is not that it *chooses* not to write to GitHub; it *cannot*.
Nothing in this package can file an issue or post a comment.

**Dry run** — build everything and show what would happen, but call no model and
change nothing. `--dry-run`.

**Offline** — runs with no network and no API key, using committed fixtures.
Four test suites do this, and `.github/workflows/tests.yml` runs all of them
plus every command-line entry point on every push.
