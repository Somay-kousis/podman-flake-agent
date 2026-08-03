# Roadmap — origin to project end

One sequence, start to finish: what shipped, where the project stands today, and
what remains between here and the last day of the mentorship term.

`MAP.md` is the companion to this file and answers a different question. It
records *how* decisions were reached, including the wrong turns, as graphs.
This file is the flat timeline and the forward plan.

*State as of 2026-08-03. The `Done` dates are commit dates; the `Ahead` dates are
LFX's published term calendar.*

---

## 1. Done

### Phase 0 — framing (Jul 30)

Started as "how do I become a Podman contributor", corrected to "this is a
specific AI/agent mentorship slot". Everything downstream follows from that
correction. Research established the premise the project rests on: Podman left
Cirrus in May 2026 (`3743b9f806`, *"Goodbye Cirrus"*), which orphaned
`hack/ci/logformatter` — 38 KB of Perl keyed to `CIRRUS_TASK_ID`, invoked by
nothing, with one stale reference left at `Makefile:726`. Flake triage reverted
to a human reading a bar graph (`CONTRIBUTING.md:355`).

### Phase 1 — prototype (Jul 30)

`parse / store / classify / eval / report`. The first parser nested on the DOM
and failed: `div.tt` opens once around the *whole* processed output, not per
test, and in ginkgo the failure summary sits outside the timeline divs. Rebuilt
line-oriented, which is what yields per-test granularity. **76–93% reduction**
against Podman's own logformatter fixtures.

### Phase 2 — corpus (Jul 30)

372 `flakes` issues harvested, 359 samples. The reality check mattered more than
the corpus: only three GHA-era samples, median 8 lines, and parser coverage split
**bats 96% / ginkgo 0%**.

### Phase 3 — fetch layer (Jul 30)

Read-only GitHub client — one GET path, no method argument, ETag-revalidated,
rate-limit aware. Then the first live run, which found three bugs in under an
hour: a cross-host redirect leaking the token to Azure, PR attribution wrong in
both directions, and step-slicing alone giving 24.8% rather than the "few KB"
a written plan had claimed. Two-stage narrowing landed at **95.1%**.

### Phase 4 — dataset and labelling (Jul 31)

One database: **492 runs, 22,335 jobs, 153,425 steps, 225 job logs, 372 issues**.
400 dossiers generated, 5 committed as offline fixtures. Three bugs caught here
too — `related_issues` matched the job name so `known_fixes` was permanently
empty; log windows bounded by lines not characters reached 41,191 tokens; and
labelling, scoring and dossiers used three incompatible identities, so nothing
could ever have been scored. The fix that shaped everything after: **label from
evidence the classifier cannot see, then blind it.**

### Phase 5 — upstream contact (Aug 1–2)

| PR | State | |
|---|---|---|
| [#29370](https://github.com/podman-container-tools/podman/pull/29370) restore the `CI_DESIRED_RUNTIME` check | closed by author | withdrawn on finding #29301 was further along and used the better marker |
| [#29376](https://github.com/podman-container-tools/podman/pull/29376) run logformatter on the windows jobs again | **open, 14/14 green** | self-contained; sets `PODMAN_CI` on four Windows jobs, consumes it in `win-lib.ps1` |

**1 of the 2 permitted open PRs is in use.**

### Phase 6 — published, and the agentic path (Aug 3)

- Repo public at `Somay-kousis/podman-flake-agent`, 16 commits.
- **History loss and rebuild.** An accidental clone over the working tree
  destroyed the original `.git`. Files survived, objects did not. The history was
  rebuilt from the recorded log: messages and dates are the originals, trees are
  reconstructed, and the short SHAs cited in `plans/README.md` and `MAP.md` no
  longer resolve. Stated in `plans/README.md` rather than papered over.
- `taxonomy.py` — categories, schema and system prompt single-sourced. They had
  been duplicated in `classify.py` and `labels.py`; they agreed, but nothing
  enforced it, and `eval.py` joins on string equality, so a drift would have
  scored as wrong rather than raised.
- `agent.py` — **the dossier → prediction path, which did not previously exist.**
  `classify.py` read the pre-dossier `test_failures` table. Blinded by default,
  evidence substring-checked against the log the model was shown, output split
  into a diffable `preds.json` and a full `verdicts.json`. Prompt is a **median
  1,678 chars (~419 tokens)** across all 400 dossiers, max 13,080.
- `fetch.py:548` fixed — it stored `ev["event"]`, the literal string
  `"referenced"`, where the commit message belonged, on exactly the event type
  that carries fix commits. **1,593 of 1,928 rows were placeholder.**
  `backfill-fixes` refills by SHA, scoped by default to the 56 issues the
  dossiers actually reference.

---

## 2. Where it stands

| | |
|---|---|
| Code / docs | 4,699 lines across 15 modules · 2,622 lines of docs |
| Data | 492 runs · 22,335 jobs · 153,425 steps · 225 job logs · 372 issues |
| Dossiers | 400 generated · 5 committed as fixtures |
| Offline tests | `test_parse`, `test_steps`, `test_agent` — all passing, no token needed |
| **Gold labels** | **0** |
| **Accuracy** | **still none measured** |

**Labelable without reading a log — 139 of 400:**

| Evidence | Dossiers |
|---|---|
| maintainer stated the cause (readable fix commit) | 60 |
| rerun disagreement — same commit passed *and* failed | 82 |
| both | 3 |

The backfill moved the first row from 45 to 60. Worth stating plainly: three
quarters of the fix SHAs return HTTP 422 — fork commits, or history that moved —
so 60 of 400 is what the linkage actually supports. It is not a solved
labelling problem.

**The one blocker.** Everything else is built. `gold_labels` is empty, so nothing
the agent produces can be scored, and every number in the README remains a size
or reduction measurement rather than a classification claim.

---

## 3. Ahead — to the application (Aug 3 → Aug 18)

Podman weights this differently from most projects, per
[@Luap99 on #29265](https://github.com/podman-container-tools/podman/issues/29265):
contributions are **not required**, the LFX resume and cover letter are what get
read, *"that can also be some personal project"*, and there is a hard cap of two
open PRs. So the repo is the artifact, and more PRs are not the lever.

1. **Label 30–50 dossiers.** The only thing between here and an accuracy number.
   Start with the 139 that carry independent evidence; `labels show --dossier`
   leads with it and puts the log last. Decide from the top — if the log is
   needed to decide, that is a weak eval case.
2. **Run and score.** `agent.py` → `preds.json` → `eval.py dossiers`. Report
   abstention beside accuracy; the scorer already warns below 30 items. Replace
   the README's *"No accuracy numbers"* bullet with the result, keeping the
   statement of what it does and does not cover.
3. **E1 — review [PR #29091](https://github.com/podman-container-tools/podman/pull/29091)**
   having run it. Reviews do not consume the 2-PR budget. The findings here are
   not available to anyone who has not parsed real output: `div.tt` opening once,
   and the ginkgo failure summary sitting outside the timeline blocks. **Only item
   with an external clock — it expires on merge.**
4. **E3 — report the multi-line `title=` gotcha.** logformatter folds the podman
   command line into a `title` attribute containing newlines, so one tag spans
   many source lines; cost ~11% of the ginkgo reduction until fixed.
5. **Cover letter, submitted Aug 16** — 48h early, not Aug 18. Lead with the
   repo and the accuracy number. The strongest material is the judgement record,
   not the feature list.

### Housekeeping, small and visible

- **No `LICENSE` file.** `README.md:295` and `HANDBOOK.md:673` both say
  Apache-2.0, but there is no file, so GitHub shows the repo as unlicensed.
- **No repository description** set on GitHub.
- `HANDBOOK.md` §11 and `MAP.md` §5 still rank more fetch/parse work highly.
  Both predate the Aug 3 replan and now contradict this file.

### Not doing before Aug 18

Tracks A, B and C from `MAP.md` — including A2 (fork-PR diff context, ~80% of PR
runs). A2 is load-bearing for the *tool*, not the *application*. Resume after
selection.

---

## 4. Ahead — the term (if selected)

| Date | |
|---|---|
| Aug 18, 23:59 UTC | applications close |
| Sep 2–4 | selections announced |
| Sep 7 | term begins |
| Oct 20 / Oct 21 | midterm evaluation / first stipend |
| Nov 24 / Nov 25 | final evaluation / second stipend |
| Nov 27 | last day |

Work that becomes worth doing once there is a mentor to agree the shape with:

- **A2 — fork-PR diff resolution.** Diff relevance is among the strongest flake
  signals, and it is missing for ~80% of PR runs because `/commits/{sha}/pulls`
  returns empty when the head commit is not in the base repo.
- **The remaining 1,196 placeholder fix links**, plus a better issue↔test join
  than lexical overlap — the dossier itself calls the current one *"a starting
  point, not a duplicate determination"*, and 24 of 89 matches are on the job
  name, the path `MAP.md` already identified as broken.
- **Local-model results.** `--backend ollama` exists and is untested against the
  gold set; issue #29265 names local AI as a plus.
- **Sit downstream of #29091 for real**, once it merges, rather than alongside it.
- **The write path.** Nothing in this package can post to GitHub — one GET path,
  no `--post` flag. Filing or commenting is a deliberate, mentor-agreed step, not
  a default.

⚠️ The LFX listing read *"This program is pending approval!"* and could not be
confirmed programmatically. If the project does not run, the repo still stands on
its own and the two CI PRs are unaffected.
