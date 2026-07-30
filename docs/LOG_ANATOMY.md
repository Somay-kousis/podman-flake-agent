# What Podman's CI actually produces

Written to build intuition before designing prompts. Everything below is real
output from `hack/ci/logformatter.t` (Podman's own test fixtures — the `<<<`
blocks are raw runner output, the `>>>` blocks are what logformatter emits),
not invented examples.

Reproduce:

```bash
python3 tests/extract_raw_inputs.py /path/to/podman/hack/ci/logformatter.t  # <<< half
python3 tests/extract_fixtures.py   /path/to/podman/hack/ci/logformatter.t  # >>> half
```

---

## Who produces what

| Producer | Artifact | Shape | Rough size |
|---|---|---|---|
| `ginkgo` (Go e2e/int suite) | stdout | Timeline blocks per spec, `• [FAILED]` markers | Large — 1,889 specs in one run |
| `bats` (shell system tests) | stdout | TAP: `ok N` / `not ok N` + `#` comment block | Medium |
| `pytest`/`unittest` (apiv2, docker-py) | stdout | `FAIL: test_x (module.Class)` + traceback | Medium |
| `hack/ci/logformatter` (Perl) | `hack/ci/logs/<test>.html` | The above, wrapped in CSS classes | ~2× the raw text |
| `hack/ci/logcollector.sh journal` | `hack/ci/logs/journal-<test>.log` | `journalctl -b` — **the firehose** | Very large |
| `hack/ci/github_log_summary.py` (PR #29091) | `$GITHUB_STEP_SUMMARY` | Failed tests only, as markdown | Small |
| GitHub Actions itself | job log | Everything, with `::group::` folding | Large |

The Vercel comparison is apt for exactly one of these: **`journal-<test>.log`**.
It's the full systemd boot journal for the runner. In the sample below, one int
run executed 1,889 specs over 1,608 seconds of continuous container
create/start/stop/remove churn, with podman debug logging on. That's the log
you cannot feed to a model, and it's why the parse step exists.

The other artifacts are *structured*, which is the opening: you don't have to
understand the whole log, only find the failure markers.

---

## Example 1 — bats (the easy case)

**Raw**, straight from the runner:

```
1..4
ok 1 hi
ok 2 bye # skip no reason
not ok 3 fail
# (from function `assert' in file ./helpers.bash, line 343,
#  from function `expect_output' in file ./helpers.bash, line 370,
#  in test file ./run.bats, line 786)
# $ /path/to/podman foo -bar
# time="2023-01-05T15:15:20Z" level=debug msg="this is debug"
# time="2023-01-05T15:15:20Z" level=warning msg="this is warning"
# #| FAIL: exit code is 123; expected 321
ok 4 blah
```

**After logformatter** — same content, now class-tagged:

```html
<span class='bats-failed'><a name='t--00003'>not ok 3 fail</a></span>
<span class='bats-log'># (from function `assert&#39; in file ./<a class="codelink" href="...helpers.bash#L343">helpers.bash, line 343</a>,</span>
<span class='bats-log'># $ <b><span title="/path/to/podman">podman</span> foo -bar</b></span>
<span class='bats-log'># time=<span class='log-warning'>&quot;...&quot;</span> level=<span class='log-warning'>warning</span> ...</span>
<span class='bats-log-failblock'># #| FAIL: exit code is 123; expected 321</span>
```

Note what logformatter added that's genuinely useful: `bats-failed`,
`bats-log-failblock`, `log-warning`, `log-debug`, and source links. It has
already done the "which lines matter" work — that's why reusing its classes
beats writing a fresh log parser.

**Extracted** (1,712 → 368 chars, 78.5% smaller):

```
not ok 3 fail
# (from function `assert' in file ./helpers.bash, line 343,
#  from function `expect_output' in file ./helpers.bash, line 370,
#  in test file ./run.bats, line 786)
# $ podman foo -bar
# time="2023-01-05T15:15:20Z" level=debug msg="this is debug"
# time="2023-01-05T15:15:20Z" level=warning msg="this is warning"
# #| FAIL: exit code is 123; expected 321
```

Everything a human would need is there: the assertion, the call chain, the
command, the actual-vs-expected. This case is easy.

---

## Example 2 — ginkgo (the hard case, and the interesting one)

Raw excerpt. Watch what happens:

```
[+0271s] • [3.327 seconds]
[+0271s] Podman restart
[+0271s]   podman restart non-stop container with short timeout
[+0271s]   Timeline >>
[+0271s]   > Enter [BeforeEach] Podman restart - .../restart_test.go:21 @ 04/17/23 10:00:28.653
[+0271s]   > Enter [It] podman restart non-stop container with short timeout - ...
[+0271s]   Running: /var/tmp/go/src/.../bin/podman --storage-opt vfs.imagestore=/tmp/imagecachedir --root /tmp/podman_test2968516396/root --runroot /tmp/podman_test2968516396/runroot --runtime crun --conmon /usr/bin/conmon --network-config-dir /tmp/podman_test2968516396/root/etc/networks --network-backend netavark --cgroup-manager systemd --tmpdir /tmp/podman_test2968516396 --events-backend file --db-backend sqlite --storage-driver vfs run -d --name test1 --env STOPSIGNAL=SIGKILL quay.io/libpod/alpine:latest sleep 999
[+0271s]   7f5f8fb3d043984cdff65994d14c4fd157479d20e0a0fcf769c35b50e8975edc
[+0271s]   time="..." level=warning msg="StopSignal SIGTERM failed to stop container test1 in 2 seconds, resorting to SIGKILL"
[+0271s]   << Timeline
[+0298s] • [FAILED] [6.071 seconds]
[+0298s] TOP-LEVEL [AfterEach]
[+0298s] /var/tmp/go/src/.../test/e2e/common_test.go:117
[+0298s]   Podman pod create
[+0298s]     podman pod correctly sets up PIDNS
[+0298s]   Timeline >>
[+0298s]   << Timeline
[+1741s] Summarizing 1 Failure:
[+1741s]   [FAIL] TOP-LEVEL [AfterEach] Podman pod create podman pod correctly sets up PIDNS
[+1741s]   /var/tmp/go/src/.../test/e2e/common_test.go:657
```

### Five things worth noticing

**1. The bulk of the log is one repeated string.** Every `Running:` line
carries the same twelve podman flags — `--storage-opt`, `--root`, `--runroot`,
`--runtime`, `--conmon`, `--network-config-dir`, `--network-backend`,
`--cgroup-manager`, `--tmpdir`, `--events-backend`, `--db-backend`,
`--storage-driver`. That's ~450 chars of pure noise per command, and there are
thousands of commands. logformatter already solves this: it folds them into a
`title=` attribute and renders `# podman [options] stop --all -t 0`.

*(This is also where the parser bug was: those `title` attributes contain
newlines, so one HTML tag spans many source lines. Strip tags line-by-line and
raw markup leaks straight into your prompt. Cost ~11% of the reduction.)*

**2. The passing test has rich detail. The failing test has almost none.**
The `podman restart` spec that *passed* gets a full timeline — every command,
every container ID, a warning about SIGKILL. The spec that **FAILED** gets:

```
Timeline >>
<< Timeline
```

Empty. This is backwards from what you'd want, and it's the single most
important thing to understand about this problem. The failure detail isn't in
the failure block.

**3. The failure points at a shared helper, not the test.**
`common_test.go:117` and `common_test.go:657` are Podman's shared e2e
setup/teardown, not `pod_infra_container_test.go` where the spec lives. So
"where did it fail" and "what broke" are different questions. An agent that
reports the file:line as the cause will be wrong most of the time.

**4. `TOP-LEVEL [AfterEach]` means cleanup failed, not the test body.** The
spec's own assertions may have passed; teardown is what blew up. Podman's e2e
tests share a storage root and clean up after each other, so failures leak
across specs. This is a strong prior for `race_condition` — and a reason a
naive reading ("PIDNS test failed") misidentifies the subject entirely.

**5. Timestamps are relative and non-uniform.** `[+0271s]` → `[+0298s]` →
`[+1741s]`. The gap from the failure to the summary is 24 minutes, because the
suite kept running. Duration is the signal that separates
`network_timeout` from `infra_blip`, so those offsets matter.

**Extracted** (11,149 → 1,109 chars, 90.1% smaller) — the exact text the model
receives, plus mined history:

```
Test: TOP-LEVEL [AfterEach] Podman pod create podman pod correctly sets up PIDNS
Suite: ginkgo

Mined flake signal (from CI history, not from this log):
- rerun_disagreement (strength 0.9): int local root fedora-current @ aaaaaaaa: ['failure', 'success']
- cross_pr (strength 0.7): failed on 2 distinct commits

Failure output:
```
→ Enter [AfterEach] Podman restart - .../restart_test.go:30 @ 04/17/23 10:00:31.334
# podman [options] stop --all -t 0
7f5f8fb3d043984cdff65994d14c4fd157479d20e0a0fcf769c35b50e8975edc
# podman [options] pod rm -fa -t 0
# podman [options] rm -fa -t 0
← Exit  [AfterEach] Podman restart - .../restart_test.go:30 @ 04/17/23 10:00:31.979 (645ms)
<< Timeline
• [FAILED] [6.071 seconds]
TOP-LEVEL [AfterEach]
/var/tmp/go/src/.../test/e2e/common_test.go:117
Podman pod create
podman pod correctly sets up PIDNS
Timeline >>
<< Timeline
Summarizing 1 Failure:
[FAIL] TOP-LEVEL [AfterEach] Podman pod create podman pod correctly sets up PIDNS
/var/tmp/go/src/.../test/e2e/common_test.go:657
Ran 1889 of 2014 Specs in 1607.919 seconds
```

Classify this failure.
```

**1,469 chars ≈ 367 tokens.** That is the whole point of the parse step.

---

## Example 3 — Python (apiv2 / docker-py)

Raw:

```
Search for image ... FAIL
======================================================================
FAIL: test_search_image (compat.test_images.TestImages)
Search for image
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/var/tmp/go/src/.../test/python/docker/compat/test_images.py", line 90, in test_search_image
    self.assertGreater(len(r), 0)
AssertionError: 0 not greater than 0
----------------------------------------------------------------------
Ran 30 tests in 20.732s
FAILED (failures=1, skipped=1)
```

Extracted: 8,832 → 584 chars (93.4% smaller — the best ratio of the three,
because unittest's failure block is already well delimited).

Note `assertGreater(len(r), 0)` on an image **search** — a search returning
zero results is a textbook `infra_blip` (registry unreachable) masquerading as
an assertion failure. The traceback alone doesn't tell you that; the category
does.

---

## What the model returns

Constrained to a schema, so it's parseable without defensive re-parsing:

```json
{
  "category": "race_condition",
  "confidence": 0.82,
  "reasoning": "Failure is in TOP-LEVEL [AfterEach] at common_test.go:117, shared teardown rather than the spec body. The preceding spec's cleanup ran `stop --all` and `rm -fa` against a shared storage root; the PIDNS spec then failed during its own teardown with an empty timeline. Consistent with cleanup racing the next spec's setup rather than a defect in pod PIDNS handling.",
  "evidence": [
    "TOP-LEVEL [AfterEach]",
    "/var/tmp/go/src/.../test/e2e/common_test.go:117",
    "# podman [options] rm -fa -t 0"
  ],
  "suggested_action": "Needs a fix, not a re-run.",
  "duplicate_of": 24220
}
```

`category` is an enum including `unknown`, so abstention is representable
rather than being forced into a wrong bucket.

---

## Thinking like the agent

Given the above, the questions the classifier is really answering:

| Question | Where the answer lives |
|---|---|
| Did the test body fail, or teardown? | `[It]` vs `[AfterEach]` / `[BeforeEach]` in the failure name |
| Is this test's failure about this test? | The file:line — shared helper vs the spec's own file |
| Was it slow, or was it broken? | Relative timestamps and the `[N seconds]` on the FAILED marker |
| Is it this PR's fault? | **Not in the log at all** — comes from mined history (`cross_pr`, `rerun_disagreement`) |
| Has someone already reported it? | The 42 open `flakes` issues, via the `search_flake_issues` tool |

The last two are the ones that make this an *agent* rather than a log
classifier: the log alone genuinely cannot tell you whether a failure is a
flake. Two runs of the same commit disagreeing can.

---

## Where the current prototype is weak

Reading the above should make the gaps obvious:

- **The failure block is often empty**, so the highest-value next step is
  pulling correlated context — the `journal-<test>.log` window around the
  failure timestamp, and the preceding spec's teardown. That's what
  `get_journal_context` in the plan is for, and it isn't implemented yet.
- **Cross-spec contamination isn't modelled.** A failure in spec B caused by
  spec A's teardown needs the *previous* spec, which the current per-failure
  extraction throws away.
- **Everything here is from a 2023-era fixture.** Real 2026 logs under PR
  #29091's output shape need re-checking against live artifacts.
