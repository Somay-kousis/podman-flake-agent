# Roadmap — origin to project end

One sequence, start to finish: what shipped, where the project stands today, and
what remains between here and the last day of the mentorship term.

New to the vocabulary? **[`GLOSSARY.md`](GLOSSARY.md)** explains every term used
here — flake, dossier, blinding, gold label, and the rest — in plain language.

`MAP.md` is the companion to this file and answers a different question: *how*
decisions were reached, including the wrong turns. This file is the timeline and
the forward plan.

*State as of 2026-08-03. `Done` dates are commit dates; term dates are LFX's
published calendar.*

---

## 1. The whole thing at a glance

```mermaid
%%{init: {"flowchart": {"curve": "basis"}}}%%
flowchart TD
    START(["Jul 30 — read the repo"])

    subgraph BUILT["Built — Jul 30 to Aug 3"]
        P1["Prototype<br/>parse the CI logs<br/>76-93% smaller"]
        P2["Corpus<br/>372 flake reports<br/>read from GitHub"]
        P3["Fetch layer<br/>read-only client<br/>logs down 95%"]
        P4["Dataset<br/>400 dossiers<br/>one per failed job"]
        P5["Agent<br/>dossier in,<br/>verdict out"]
    end

    subgraph UPSTREAM["Sent to Podman — Aug 1 to 2"]
        U1["PR 29376 open<br/>all checks green"]
        U2["PR 29370 withdrawn<br/>someone was ahead"]
    end

    NOW(["Aug 3 — we are here"])

    subgraph GAP["The one thing missing"]
        G1["0 gold labels<br/>so nothing can be<br/>marked right or wrong"]
    end

    subgraph TODO["Before Aug 18"]
        T1["Label 30-50 by hand"]
        T2["Run the agent, score it"]
        T3["Review Luap99's PR 29091"]
        T4["Write the cover letter"]
        T5["Submit Aug 16"]
    end

    TERM(["Sep 7 to Nov 27<br/>the term, if selected"])

    START --> P1 --> P2 --> P3 --> P4 --> P5 --> NOW
    P3 --> U1
    P3 --> U2
    U1 --> NOW
    U2 --> NOW
    NOW --> G1 --> T1 --> T2 --> T4
    NOW --> T3 --> T4
    T4 --> T5 --> TERM

    classDef done  fill:#dfe7ef,stroke:#5d6d7e,color:#34495e
    classDef now   fill:#fff0d9,stroke:#e67e22,color:#7e4a11
    classDef block fill:#fde8e8,stroke:#c0392b,color:#7b241c
    classDef next  fill:#e6f4ea,stroke:#1e8449,color:#145a32

    class P1,P2,P3,P4,P5,U1,U2 done
    class START,NOW,TERM now
    class G1 block
    class T1,T2,T3,T4,T5 next
```

**Grey** = shipped · **red** = the blocker · **green** = what's next · **amber** = milestones.

---

## 2. What each phase actually produced

```mermaid
flowchart LR
    subgraph IN["What goes in"]
        A1["A GitHub Actions run<br/>that failed"]
    end
    subgraph MID["What this project does to it"]
        B1["Find which STEP failed<br/>no log reading needed"]
        B2["Cut the 500 KB log<br/>down to the failure<br/>~95% smaller"]
        B3["Gather evidence<br/>the log cannot show<br/>reruns, history, the diff"]
        B4["Write it as one<br/>JSON file: a DOSSIER"]
    end
    subgraph OUT["What comes out"]
        C1["Hide the evidence<br/>a human labels from<br/>= BLINDING"]
        C2["Ask a model:<br/>why did this fail?"]
        C3["Check its quotes<br/>are really in the log"]
        C4["Score it against<br/>hand-made GOLD LABELS"]
    end

    A1 --> B1 --> B2 --> B3 --> B4 --> C1 --> C2 --> C3 --> C4

    classDef done fill:#dfe7ef,stroke:#5d6d7e,color:#34495e
    classDef gap  fill:#fde8e8,stroke:#c0392b,color:#7b241c
    class A1,B1,B2,B3,B4,C1,C2,C3 done
    class C4 gap
```

Everything grey is built and runs today. **Only the last box is missing**, and it
is missing because it needs human judgement, not more code.

| Phase | Date | What shipped | What broke, and was fixed |
|---|---|---|---|
| 0 · framing | Jul 30 | Established the premise: Podman left Cirrus CI in May 2026, orphaning `logformatter`, so flake triage reverted to a human reading a bar graph | Started as "become a Podman contributor", corrected to "this is an AI/agent slot" |
| 1 · prototype | Jul 30 | `parse / store / classify / eval / report`; **76–93%** log reduction | First parser nested on the DOM. `div.tt` opens once around the *whole* output, not per test. Rebuilt line-oriented |
| 2 · corpus | Jul 30 | 372 flake reports, 359 samples | Only 3 modern samples; parser coverage split **bats 96% / ginkgo 0%** |
| 3 · fetch | Jul 30 | Read-only client, ETag-cached; two-stage narrowing to **95.1%** | First live run found 3 bugs in an hour, incl. a redirect leaking the token to Azure |
| 4 · dataset | Jul 31 | 492 runs · 22,335 jobs · 153,425 steps · 400 dossiers | Issue matching used the *job* name, so the fix field was always empty; labels and scores used 3 incompatible IDs — nothing could ever have been scored |
| 5 · upstream | Aug 1–2 | PR #29376 open and green; #29370 withdrawn as a duplicate | — |
| 6 · agent | Aug 3 | `taxonomy.py`, `agent.py`, published repo | `fetch.py:548` stored the word `"referenced"` where the commit message belonged — **1,593 of 1,928 rows** |

---

## 3. Where it stands

| | |
|---|---|
| Code / docs | 4,699 lines across 15 modules · 2,622 lines of docs |
| Data | 492 runs · 22,335 jobs · 153,425 steps · 225 job logs · 372 issues |
| Dossiers | 400 generated · 5 committed as offline fixtures |
| Offline tests | `test_parse`, `test_steps`, `test_agent` — all passing, no token needed |
| Upstream | 1 of 2 permitted open PRs in use |
| **Gold labels** | **0** |
| **Accuracy** | **still none measured** |

**139 of 400 dossiers can be labelled without reading a log:**

```mermaid
flowchart TD
    ALL["400 dossiers"]
    E1["60<br/>a maintainer said<br/>what caused it"]
    E2["82<br/>same commit both<br/>passed and failed"]
    E3["3<br/>both"]
    E4["261<br/>log only<br/>weak eval cases"]

    ALL --> E1
    ALL --> E2
    ALL --> E3
    ALL --> E4

    classDef strong fill:#e6f4ea,stroke:#1e8449,color:#145a32
    classDef weak   fill:#eef1f5,stroke:#7f8c9b,color:#2c3e50
    class E1,E2,E3 strong
    class ALL,E4 weak
```

The backfill moved the first group from 45 to 60. Worth stating plainly: three
quarters of the fix commit IDs return HTTP 422 — they belong to forks, or to
history that moved — so **60 of 400 is what the linkage actually supports.** Not
a solved labelling problem.

**The one blocker.** Everything else is built. `gold_labels` is empty, so nothing
the agent produces can be scored, and every number in the README is still a size
measurement rather than a claim about being right.

---

## 4. Ahead — to the application, Aug 3 → Aug 18

Podman weights this differently from most projects, per
[@Luap99 on #29265](https://github.com/podman-container-tools/podman/issues/29265):
contributions are **not required**, the cover letter and resume are what get
read, *"that can also be some personal project"*, and there is a hard cap of two
open PRs. **So this repo is the artifact, and more PRs are not the lever.**

```mermaid
flowchart TD
    N(["Aug 3"])
    L1["Label 30-50 dossiers<br/>start with the 139<br/>that have real evidence"]
    L2["Run the agent<br/>preds.json"]
    L3["Score it<br/>first accuracy number"]
    L4["Put the number<br/>in the README"]
    R1["Review PR 29091<br/>does NOT use a PR slot<br/>expires when it merges"]
    R2["Report the title= gotcha<br/>also free"]
    W1["Cover letter<br/>lead with the repo<br/>and the number"]
    S(["Submit Aug 16<br/>48h early"])

    H1["Add a LICENSE file"]
    H2["Set the repo description"]

    N --> L1 --> L2 --> L3 --> L4 --> W1
    N --> R1 --> W1
    N --> R2
    N --> H1
    N --> H2
    W1 --> S

    classDef must  fill:#e6f4ea,stroke:#1e8449,color:#145a32
    classDef clock fill:#fff0d9,stroke:#e67e22,color:#7e4a11
    classDef small fill:#eef1f5,stroke:#7f8c9b,color:#2c3e50
    class L1,L2,L3,L4,W1 must
    class N,S,R1 clock
    class R2,H1,H2 small
```

1. **Label 30–50 dossiers.** The only thing between here and an accuracy number.
   `labels show --dossier` leads with the independent evidence and puts the log
   last. Decide from the top — if you need the log to decide, it is a weak case.
2. **Run and score.** `agent.py` → `preds.json` → `eval.py dossiers`. Report
   abstention beside accuracy. Replace the README's *"No accuracy numbers"*
   bullet, keeping the statement of what it does and does not cover.
3. **Review [PR #29091](https://github.com/podman-container-tools/podman/pull/29091)**
   having run it. Reviews do not consume the 2-PR budget. Your findings here are
   not available to anyone who has not parsed real output. **The only item with
   an external clock — it expires on merge.**
4. **Report the multi-line `title=` gotcha** — logformatter folds the podman
   command line into an HTML attribute containing newlines, so one tag spans many
   lines. Cost ~11% of the ginkgo reduction until fixed.
5. **Cover letter, submitted Aug 16.**

### Small and visible

- **No `LICENSE` file.** `README.md:295` and `HANDBOOK.md:673` both say
  Apache-2.0, but the file does not exist, so GitHub shows the repo as
  unlicensed.
- **No repository description** set on GitHub.
- `HANDBOOK.md` §11 and `MAP.md` §5 still rank more fetch work highly. Both
  predate the Aug 3 replan and now contradict this file.

### Not doing before Aug 18

Tracks A, B and C from `MAP.md`, including A2 (fork-PR diff context, ~80% of PR
runs). A2 is load-bearing for the *tool*, not the *application*.

---

## 5. Ahead — the term, if selected

| Date | |
|---|---|
| Aug 18, 23:59 UTC | applications close |
| Sep 2–4 | selections announced |
| Sep 7 | term begins |
| Oct 20 / Oct 21 | midterm evaluation / first stipend |
| Nov 24 / Nov 25 | final evaluation / second stipend |
| Nov 27 | last day |

Work that becomes worth doing once there is a mentor to agree the shape with:

- **Fork-PR diff resolution.** Whether the code change could even have caused the
  failure is among the strongest signals, and it is missing for ~80% of PR runs
  because GitHub does not report the PR for commits that live in a fork.
- **The remaining 1,196 placeholder fix links**, and a better issue-to-test match
  than word overlap — the dossier itself calls the current one *"a starting
  point, not a duplicate determination"*, and 24 of 89 matches are on the job
  name, the path `MAP.md` already identified as broken.
- **Local-model results.** `--backend ollama` exists and is untested against the
  gold set; issue #29265 names local AI as a plus.
- **Sit downstream of #29091 for real**, once it merges, rather than alongside it.
- **The write path.** Nothing in this package can post to GitHub — one GET path,
  no `--post` flag. Filing or commenting is a deliberate, mentor-agreed step.

⚠️ The LFX listing read *"This program is pending approval!"* and could not be
confirmed programmatically. If the project does not run, the repo still stands on
its own and the two CI PRs are unaffected.
