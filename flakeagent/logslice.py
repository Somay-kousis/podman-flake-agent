"""Cut a job log down to the failing step, using timestamps alone.

WHY THIS EXISTS
---------------
A GitHub Actions job log is ~500KB and covers every step: checkout, Go setup,
dependency installs, the test run, cleanup. Only one step failed, and only that
step's output is worth reading.

Two facts make the cut free:

  1. `job_steps` records `started_at` and `completed_at` per step.
  2. Every GHA log line is prefixed with an ISO-8601 timestamp
     (`2026-07-30T14:18:41.1789310Z ...`).

So the failing step's output is the lines whose timestamp falls inside that
step's interval. Verified against job 90896283356: step 7 ran
14:07:41Z -> 14:36:55Z, and `[FAILED] Timed out after 600.001s.` is stamped
14:18:41Z.

No CSS classes, no logformatter, no suite-specific regex, and no dependence on
which CI era produced the log. It sidesteps the ginkgo parser gap entirely: we
do not need to *understand* the output to isolate it.

Degrades rather than fails -- an unparseable or timestamp-free log returns the
tail instead of raising.
"""

import gzip
import re
from datetime import datetime, timezone
from pathlib import Path

# GHA stamps every line; the fractional part varies in length.
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s?(.*)$")

DEFAULT_CONTEXT = 200

# Deliberately generous. step_window feeds focus_window, and truncating here
# first silently discards anchors: at 4000 this dropped the
# "[FAILED] Timed out after 600.001s." line from a 4,500-line step, leaving the
# focus stage to anchor only on the end-of-run summary. Bounding is focus_window's
# job -- this cap exists only to stop a pathological log exhausting memory.
MAX_WINDOW_LINES = 60000

# Everything after these is runner housekeeping -- git config, chmod, rm -rf,
# orphan-process cleanup. A region that reaches one stops there; it is never
# evidence about why a test failed.
NOISE_AFTER = [
    re.compile(r"^\s*Post job cleanup\."),
    re.compile(r"##\[group\]Run '/var/tmp/cleanup"),
    re.compile(r"^\s*Cleaning up orphan processes"),
    re.compile(r"^\s*A job completed hook has been configured"),
]

# Second-stage narrowing.
#
# Slicing to the failing step is not enough on its own: measured on job
# 90896283356, "Run machine e2e" ran 29 minutes and produced 4,500 of the log's
# 5,316 lines -- a 24.8% cut, still ~270KB. The failing step usually *is* the
# log. So within the step window, anchor on markers that denote an actual
# reported failure and keep only regions around them.
#
# These are deliberately strong. A bare `Error:` matched 67 times in that same
# log, and nearly all were *expected* output from negative tests
# ("Error: foobar: VM does not exist" is a test asserting a VM is absent).
# Anchoring on it would point a reader at healthy assertions.
FAIL_ANCHORS = [
    re.compile(r"^\s*[•·]?\s*\[FAILED\]"),          # ginkgo spec failure
    re.compile(r"Summarizing \d+ Failure"),          # ginkgo end-of-run summary
    re.compile(r"^\s*not ok \d+"),                   # bats / TAP
    re.compile(r"^\s*#\s*#\|\s*FAIL"),               # bats failure block
    re.compile(r"^\s*(?:FAIL|ERROR): \w+ \("),       # python unittest
    re.compile(r"##\[error\]"),                      # GitHub Actions marker
    re.compile(r"^\s*Panic in Spec"),                # ginkgo panic
    re.compile(r"^\s*\[FAIL\]"),
]

FOCUS_BEFORE = 40
FOCUS_AFTER = 120
FOCUS_MAX_LINES = 900

# A line cap alone is not enough. logformatter folds podman's twelve repeated
# flags into a `title` attribute; a raw GHA job log does not, so every
# "Running: .../podman --storage-opt ... --storage-driver vfs run ..." line is
# ~450 characters. Measured across 193 real windows: median 1,331 tokens, but
# the worst was 41,191 -- exactly 900 lines, all of them enormous.
#
# So bound characters too, keeping the tail: failures report at the end of a
# step, and the head of an over-long window is setup chatter.
FOCUS_MAX_CHARS = 48000  # ~12k tokens


def parse_ts(text):
    """ISO-8601 with a Z suffix -> aware datetime. None when unparseable."""
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def read_log(path):
    """Read a stored log, gzipped or plain. Strips the UTF-8 BOM GHA emits."""
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    return text.lstrip("﻿").splitlines()


def split_line(line):
    """-> (timestamp or None, remaining text)."""
    m = TS_RE.match(line)
    if not m:
        return None, line
    return parse_ts(m.group(1)), m.group(2)


def log_bounds(lines):
    """First and last parseable timestamps in the log."""
    first = last = None
    for line in lines:
        ts, _ = split_line(line)
        if ts:
            first = first or ts
            last = ts
    return first, last


def step_window(lines, started_at, completed_at, context=DEFAULT_CONTEXT,
                strip_timestamps=True):
    """Lines whose timestamp lies within [started_at, completed_at].

    `context` extra lines are kept after the window: a step's failure summary is
    sometimes flushed a moment after the step is marked complete.
    """
    start, end = parse_ts(started_at), parse_ts(completed_at)
    if start is None or end is None:
        return {"lines": _tail(lines, context, strip_timestamps),
                "reason": "step has no usable timestamps; returned tail"}

    out, after, seen_window = [], 0, False
    for line in lines:
        ts, body = split_line(line)
        text = body if strip_timestamps else line

        if ts is None:
            # Continuation of a wrapped line: keep it if we're inside.
            if seen_window and after == 0:
                out.append(text)
            continue

        if start <= ts <= end:
            seen_window = True
            out.append(text)
        elif seen_window and ts > end:
            if after < context:
                out.append(text)
                after += 1
            else:
                break

    if not out:
        return {"lines": _tail(lines, context, strip_timestamps),
                "reason": "no lines fell inside the step interval; returned tail"}

    truncated = len(out) > MAX_WINDOW_LINES
    if truncated:
        # Keep the end: failures report at the end of a step.
        out = out[-MAX_WINDOW_LINES:]

    return {"lines": out,
            "reason": "sliced to the failing step's time interval"
                      + (" (truncated to the last "
                         f"{MAX_WINDOW_LINES} lines)" if truncated else "")}


# Which test failed, as opposed to which job failed.
#
# The distinction matters: the CI job is called "macos machine applehv", which
# tells you nothing and matches no issue title. The log says the failing spec is
# "podman machine rm [It] Remove running machine" -- which matches real issues
# (#23454, #23472). Everything downstream that looks a failure up by name needs
# this one, not the job's.
#
# Run over the already-narrowed window: ~250 lines where the markers are dense,
# rather than 5,000 where they are not. This is the ginkgo extraction that
# defeated the HTML parser at 0% coverage, and it is easy here because the
# window has already isolated the failure and no CSS classes are involved.
GINKGO_FAIL_LINE = re.compile(r"^\s*\[FAIL\]\s+(.+?)\s*$")
GINKGO_IT = re.compile(r"\s*\[(?:It|BeforeEach|AfterEach|JustBeforeEach|"
                       r"JustAfterEach|SynchronizedBeforeSuite|SynchronizedAfterSuite)\]\s*")
BATS_NOT_OK = re.compile(r"^\s*not ok\s+\d+\s+(.+?)\s*$")
PY_FAIL = re.compile(r"^\s*(?:FAIL|ERROR):\s+(\S+)\s*\(([^)]+)\)")


def extract_failing_tests(lines):
    """Names of the tests that failed. [] when nothing is identifiable.

    Never guesses -- an empty list is a truthful answer and the callers treat it
    as one.
    """
    out, seen = [], set()

    def add(kind, name, raw):
        name = " ".join(name.split())
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append({"kind": kind, "name": name, "raw": raw.strip()[:200]})

    for line in lines:
        m = GINKGO_FAIL_LINE.match(line)
        if m:
            # "podman machine rm [It] Remove running machine"
            #   -> "podman machine rm Remove running machine"
            add("ginkgo", GINKGO_IT.sub(" ", m.group(1)), line)
            continue

        m = BATS_NOT_OK.match(line)
        if m:
            # Strip bats' "|NNN|" numbering and trailing timing.
            name = re.sub(r"^\|\d+\|\s*", "", m.group(1))
            name = re.sub(r"\s+in \d+ms$", "", name)
            add("bats", name, line)
            continue

        m = PY_FAIL.match(line)
        if m:
            add("python", f"{m.group(2)}.{m.group(1)}", line)

    return out


def find_anchors(lines):
    """Indices of lines that report an actual failure."""
    return [i for i, line in enumerate(lines)
            if any(rx.search(line) for rx in FAIL_ANCHORS)]


def focus_window(lines, before=FOCUS_BEFORE, after=FOCUS_AFTER,
                 max_lines=FOCUS_MAX_LINES):
    """Narrow a step window to the regions around reported failures.

    Merges overlapping regions and marks elisions so a reader can see that the
    text is not contiguous. Returns the tail when nothing anchors, rather than
    guessing.
    """
    anchors = find_anchors(lines)
    if not anchors:
        keep, note = _cap(lines[-min(len(lines), max_lines):])
        return {"lines": keep, "anchors": 0,
                "reason": "no failure markers found; returned the tail"
                          + (f" ({note})" if note else "")}

    regions = []
    for idx in anchors:
        lo = max(0, idx - before)
        hi = min(len(lines), idx + after + 1)
        # Stop the region as soon as it runs into runner housekeeping.
        for j in range(idx + 1, hi):
            if any(rx.search(lines[j]) for rx in NOISE_AFTER):
                hi = j
                break
        if regions and lo <= regions[-1][1]:
            regions[-1] = (regions[-1][0], max(regions[-1][1], hi))
        else:
            regions.append((lo, hi))

    out = []
    prev_end = 0
    for lo, hi in regions:
        if lo > prev_end:
            out.append(f"        ... {lo - prev_end} lines elided ...")
        out.extend(lines[lo:hi])
        prev_end = hi
    if prev_end < len(lines):
        out.append(f"        ... {len(lines) - prev_end} lines elided ...")

    notes = []
    if len(out) > max_lines:
        out = out[-max_lines:]
        notes.append(f"capped at {max_lines} lines")

    out, note = _cap(out)
    if note:
        notes.append(note)

    return {"lines": out, "anchors": len(anchors),
            "reason": f"focused on {len(regions)} region(s) around "
                      f"{len(anchors)} failure marker(s)"
                      + (f" ({'; '.join(notes)})" if notes else "")}


def _cap(lines, budget=FOCUS_MAX_CHARS):
    """Bound a window by characters, keeping the tail.

    Must be applied on *every* return path out of focus_window. It originally
    was not: the no-anchor fallback returned the tail bounded only by line
    count, and four real windows came back at 22k-41k tokens because raw GHA
    log lines run ~450 characters each.
    """
    total = sum(len(l) + 1 for l in lines)
    if total <= budget:
        return lines, None
    kept, size = [], 0
    for line in reversed(lines):
        size += len(line) + 1
        if size > budget:
            break
        kept.append(line)
    dropped = len(lines) - len(kept)
    return list(reversed(kept)), (f"capped at {budget} chars, dropped "
                                  f"{dropped} leading line(s)")


def _tail(lines, context, strip_timestamps):
    tail = lines[-max(context, 50):]
    return [split_line(l)[1] if strip_timestamps else l for l in tail]


def failing_step_window(conn, job_id, context=DEFAULT_CONTEXT):
    """Assemble the failing step's log window for a job, from stored data.

    Returns a dict even when data is missing -- the caller gets a reason, not an
    exception.
    """
    row = conn.execute("SELECT * FROM job_logs WHERE job_id=?", (job_id,)).fetchone()
    if not row:
        return {"available": False, "reason": "no stored log for this job",
                "lines": [], "step": None}

    path = Path(row["path"])
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    if not path.exists():
        return {"available": False, "reason": f"log file missing: {path}",
                "lines": [], "step": None}

    lines = read_log(path)

    step = conn.execute(
        """SELECT number, name, started_at, completed_at FROM job_steps
           WHERE job_id=? AND conclusion='failure' ORDER BY number LIMIT 1""",
        (job_id,)).fetchone()

    if not step:
        sliced = {"lines": _tail(lines, context, True),
                  "reason": "no failing step recorded; returned tail"}
        step_info = None
    else:
        sliced = step_window(lines, step["started_at"], step["completed_at"], context)
        step_info = {"number": step["number"], "name": step["name"],
                     "started_at": step["started_at"],
                     "completed_at": step["completed_at"]}

    # Two stages: time-slice to the failing step, then focus on the failure
    # markers within it. The first alone is not enough -- the failing step is
    # usually most of the log.
    step_lines = sliced["lines"]
    focused = focus_window(step_lines)
    text = "\n".join(focused["lines"])

    return {
        "available": True,
        "step": step_info,
        "lines": focused["lines"],
        "text": text,
        "anchors": focused["anchors"],
        "failing_tests": extract_failing_tests(focused["lines"]),
        "reason": f"{sliced['reason']}; {focused['reason']}",
        "source_line_count": len(lines),
        "step_line_count": len(step_lines),
        "line_count": len(focused["lines"]),
        "chars": len(text),
        "est_tokens": len(text) // 4,
        "reduction_pct": round(
            (1 - len(focused["lines"]) / len(lines)) * 100, 1) if lines else 0.0,
    }
