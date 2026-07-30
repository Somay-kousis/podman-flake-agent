# Fetch audit — what we get, what we're missing, what is unreachable

An audit of the GitHub API surface for `podman-container-tools/podman`, made by
probing 25 endpoints read-only on 2026-07-30. The fetch layer was built
endpoint-by-endpoint as needs arose; this is the first time it has been checked
against what the API actually offers.

Section 3 is the one to read. It bounds what any tool built on this data can
honestly conclude.

---

## 0. Permissions — none beyond what we have

Of 25 endpoints probed, exactly three refused:

| Endpoint | Status |
|---|---|
| `/actions/runners` | `403 Resource not accessible by personal access token` |
| `/actions/secrets` | `403` |
| `/actions/variables` | `403` |

All three are things this project neither needs nor should hold. **Everything
else is reachable with a fine-grained, public-repositories, read-only token.**

**Recommendation: do not widen the token.** No feature described anywhere in this
repo requires more, and a read-only credential is the reason a bug in this code
cannot damage Podman.

### Rate-limit shape

| Pool | Limit | Notes |
|---|---|---|
| core | 5,000/hr authenticated · 60/hr anonymous | Everything built so far uses only this |
| **search** | **30/min authenticated** | `/search/commits`, `/search/issues`. Must be paced — a different constraint from the rest of the codebase |
| conditional requests | free | A `304` costs no quota. `gh.py` sends `If-None-Match` on every cached URL |

---

## 1. Fetched today

| Data | Command | Table |
|---|---|---|
| Workflow runs | `fetch runs` | `runs` |
| Jobs | `fetch jobs` | `jobs` |
| **Per-step outcomes** | `fetch jobs` | `job_steps` |
| Artifact metadata | `fetch artifacts` | `artifacts` |
| Artifact content | `fetch artifacts --download` | files + `artifacts.local_path` |
| Job logs | `fetch logs` | `job_logs` + gzipped files |
| Check annotations | `fetch annotations` | `annotations` |
| Issues | `fetch issues` | `known_issues` |
| Issue comments | `fetch comments` | `issue_comments` |
| PR changed files | `fetch prfiles` | `pr_files` |
| Log excerpts in issues | `corpus harvest` | `corpus_samples` |
| **Fix commits** | `fetch fixes` | `fix_commits` |
| **Issue timeline** | `fetch timeline` | `issue_events` |

---

## 2. Reachable, not yet fetched

Ordered by value. All confirmed `200` during the probe.

| Gap | Endpoint | Why it matters | Cost |
|---|---|---|---|
| PR reviews + review comments | `/pulls/{n}/reviews`, `/pulls/{n}/comments` | 11 reviews and 7 review comments on one PR. Maintainers often say *why* a failure was a flake here, and nowhere else. | 2/PR |
| Check-runs by SHA | `/commits/{sha}/check-runs` | 99 check-runs for a single commit — a fuller view than per-job annotations, including checks from apps outside Actions. | 1/commit |
| Test-file history | `/commits?path=test/e2e/x_test.go` | Answers "was this test edited shortly before it started flaking?" — a strong `real_bug` signal. | 1/file |
| Commit detail | `/commits/{sha}` | Changed files for **push** runs. Partially fills the fork-PR blind spot in §3. | 1/run |
| Run timing | `/actions/runs/{id}/timing` | Billable duration and queue delay. Queue delay is an infrastructure signal invisible in logs. | 1/run |
| Compare | `/compare/{base}...{head}` | Exactly what changed between a passing and a failing run. | 1/pair |
| Workflow file contents | `/contents/.github/workflows/ci.yml` | The matrix definition itself — lets the tool reason about job names rather than regex them. | 1 |
| Code search | `/search/code` | Locate a failing test's source from its name. **Search pool, 30/min.** | 1/lookup |
| Releases | `/releases` | Correlate flake spikes with release windows. | 1 |
| Contributors | `/contributors` | Who actually triages flakes. | 1 |

### Workflow coverage

Probed 2026-07-30. There are 21 active workflows; only some produce test failures.

| Workflow | Runs | Failed | Fetched |
|---|---:|---:|---|
| `ci.yml` | 1,076 | 429 | **yes** |
| `zizmor.yml` | 3,251 | 20 | no — security lint, 3,251 runs of pagination for 20 failures |
| `validate.yml` | 25 | 15 | **yes** (added by this audit) |
| `unit-tests.yml` | 10 | 9 | **yes** (added by this audit) |
| `machine-os-pr.yml` | 58 | **0** | no — was fetched, has never failed |

`validate.yml` and `unit-tests.yml` were recently split out of `ci.yml`, which is
why their failure rates are so high. That instability is exactly what is worth
capturing. `lima.yml` is `workflow_call`-only and has no runs of its own; its jobs
appear inside `ci.yml` runs.

---

## 3. Cannot be fetched, ever

Every row states how the limit was established. These bound what any classifier
built on this data can honestly claim.

### 3a. Destroyed or expired

| Limit | How we know | Consequence |
|---|---|---|
| **Cirrus-era logs are gone** | `api.cirrus-ci.com` and `cirrus-ci.com` both fail to resolve | Roughly half the `flakes` corpus links to Cirrus artifacts. Those URLs are dead. **Permanent** — the evidence for many historical flakes no longer exists anywhere. |
| **Artifacts expire after 90 days** | An artifact created 2026-07-30 carries `expires_at: 2026-10-28` | Backfill has a hard floor. Older runs keep metadata; their logs and journals are gone. |
| Deleted branches, force-pushed commits | — | Absent from the API with no trace. |

### 3b. Structurally unavailable

| Limit | How we know | Consequence |
|---|---|---|
| **Fork PR head commits** | `/commits/{sha}/pulls` returns `[]` for them; the commit isn't in the base repo | **160 of 184 runs** in a 7-day sample have no resolvable PR, so no diff context. Podman takes nearly all contribution via fork PRs. |
| Secrets, variables, runner inventory | `403` on all three | Correct and desirable. Not a gap to close. |
| `cancelled` / `action_required` runs | Jobs never started | 84 of 184 runs in the sample. Nothing exists to fetch. |

### 3c. Never captured in the first place

The most important category, because no amount of API access fixes any of it.

| Limit | Consequence |
|---|---|
| **Runtime state at the moment of failure** | No VM snapshot, no core dump, no memory or process state. Only what a test happened to write to a log. If the failing code didn't log it, it is unknowable. |
| **journald beyond the upload** | `hack/ci/logcollector.sh` runs `journalctl -b` once, at the end of the job. Anything rotated out before that is unrecoverable. |
| **Reasoning that never reached GitHub** | Diagnosis frequently happens in `#podman-dev:matrix.org`. An issue may record *that* something was a flake without ever recording *why* — and the why is what a classifier is trying to learn. |
| **Flakes that passed** | A test that flakes but succeeds on attempt 1 leaves no trace at all. |

### The selection bias, stated plainly

That last row deserves its own paragraph, because it is the single most important
limit on this project and it is invisible unless named.

**We only ever observe flakes that failed.** A test with a 5% failure rate is
recorded only in the 5% of runs where it lost the race. The 95% are
indistinguishable from a test that never flakes.

Consequences that follow directly:

- Any "flake rate" computed from this data is a rate *among observed failures*,
  not among runs. Do not quote it as the latter.
- A test that became *more* flaky and a test that simply ran more often look
  identical here.
- Rerun disagreement (§ `attempts` in the dossier) is the only direct evidence of
  a flake we can obtain — the same commit both passing and failing. It exists for
  **18% of runs**, and only because a human pressed re-run. `GINKGO_FLAKE_ATTEMPTS`
  defaults to `0`, so CI never generates this signal on its own.

This is also the strongest argument for upstream issue **#28842** (nightly cron
runs, currently unassigned): scheduled runs on an unchanging ref would generate
exactly the pass/fail distribution this data structurally lacks.

---

## 4. Method

```bash
python3 -m flakeagent.fetch status     # budget and inventory
```

The probe scripts are not committed — they were one-off, and every result they
produced is recorded above. To re-verify a specific claim, the endpoint and the
observed status are in the tables; re-run it against the live API.

Every request in this project is `GET`. Nothing here writes to Podman.
