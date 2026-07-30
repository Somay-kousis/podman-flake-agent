"""Extract failed-test blocks from logformatter HTML.

ATTRIBUTION & DIVERGENCE
------------------------
Starting point: Paul Holzinger's `hack/ci/github_log_summary.py`, added in
podman-container-tools/podman#29091. That script walks the HTML with
html.parser and keeps `tt` elements containing a `log-failed` descendant.

We diverge, for a reason worth stating because it is also the reason the
token-reduction numbers work:

`hack/ci/logformatter` is a *line-oriented* emitter. It processes stdin one
line at a time and wraps each in spans/divs; it does not build a nested
document. Critically, `<div class='tt'>` is opened once around the whole
processed output (logformatter:249) -- it is not a per-test container. So
"keep the `tt` ancestor of any failure" keeps a whole log section.

Worse for structure: in real ginkgo output the failure summary
(`• [FAILED]` followed by `h2.log-failed` name components) is emitted
*outside* the `div.ginkgo-timeline` blocks that hold the diagnostic detail.
No single DOM subtree contains both.

So this parser treats the HTML as what it is: a stream of annotated lines.
It detects failure markers and captures bounded regions around them. That
yields per-test granularity, which nesting cannot, and it is what makes
deduplication against the `flakes`-labelled issues possible at all.
"""

import html as html_mod
import re
from dataclasses import dataclass

CLASS_RE = re.compile(r"""class\s*=\s*['"]([^'"]*)['"]""")
TAG_RE = re.compile(r"<[^>]+>")
TAG_SPAN_RE = re.compile(r"<[^>]*>", re.S)  # may span newlines (see _lines)

GINKGO_FAILED = re.compile(r"^[•·]?\s*\[FAILED\]")
GINKGO_H2 = re.compile(r"<h2[^>]*class=[\"'][^\"']*log-failed")
BATS_NOT_OK = re.compile(r"^not ok\s+(\d+)\s+(.*)$")
PY_FAIL = re.compile(r"^(?:FAIL|ERROR):\s+(\S+)\s*\(([^)]+)\)")

# Lines that end a captured region.
TERMINATORS = ("ginkgo-final-fail", "bats-summary", "log-passed")


@dataclass
class Failure:
    kind: str  # 'ginkgo' | 'bats' | 'python'
    name: str
    text: str
    source: str = ""

    def key(self):
        """Stable identity for cross-run correlation and issue dedup."""
        return f"{self.kind}:{' '.join(self.name.split())}"

    def to_dict(self):
        return {
            "kind": self.kind,
            "name": self.name,
            "key": self.key(),
            "text": self.text,
            "source": self.source,
        }


@dataclass
class Line:
    classes: frozenset
    text: str

    def has(self, *names):
        return any(n in self.classes for n in names)


def _lines(doc):
    """Flatten the document into (classes, plain-text) per source line.

    logformatter folds the full podman command line into a `title` attribute
    (`<span class="boring" title="--root /tmp/...\\n--runtime crun\\n...">`), so a
    single tag can span many source lines. Splitting first and stripping tags
    per line therefore leaves raw markup in the text. Collapse newlines inside
    tags before splitting -- those newlines belong to an attribute value, not to
    the log.
    """
    doc = TAG_SPAN_RE.sub(lambda m: m.group(0).replace("\n", " "), doc)

    out = []
    for raw in doc.splitlines():
        classes = set()
        for m in CLASS_RE.finditer(raw):
            classes.update(m.group(1).split())
        if GINKGO_H2.search(raw):
            classes.add("__h2_failed")
        text = html_mod.unescape(TAG_RE.sub("", raw)).rstrip()
        out.append(Line(frozenset(classes), text))
    return out


def _strip_timestamp(text):
    """logformatter prefixes '[+0298s] ' or 9 spaces of alignment."""
    return re.sub(r"^\s*(?:\[\+\d+s\]\s*)?", "", text)


def _capture(lines, start, stop_pred, limit=120):
    """Collect text from `start` until stop_pred says otherwise."""
    body = []
    for ln in lines[start : start + limit]:
        if body and stop_pred(ln):
            break
        t = _strip_timestamp(ln.text)
        if t.strip():
            body.append(t)
    return "\n".join(body).strip()


def _ginkgo(lines):
    """Ginkgo: `• [FAILED]` marker, then h2.log-failed name components.

    Detail lives in the preceding ginkgo-timeline divs, so we reach backwards
    to the enclosing timeline block for evidence.
    """
    failures = []
    for i, ln in enumerate(lines):
        if not (ln.has("log-failed") and GINKGO_FAILED.match(_strip_timestamp(ln.text))):
            continue

        # Name: the h2.log-failed components following the marker. Ginkgo emits
        # them as suite -> context -> spec, but NOT on contiguous lines --
        # interleaved detail lines sit between them -- so scan a window and
        # stop only at a real boundary.
        parts = []
        for ln2 in lines[i + 1 : i + 16]:
            if ln2.has("__h2_failed"):
                parts.append(ln2.text.strip())
            elif ln2.has(*TERMINATORS) or GINKGO_FAILED.match(_strip_timestamp(ln2.text)):
                break
        name = " ".join(p for p in parts if p) or "<unnamed ginkgo spec>"

        # Evidence: back to the start of the enclosing timeline block.
        start = i
        for j in range(i - 1, max(-1, i - 200), -1):
            if lines[j].has("ginkgo-it", "ginkgo-beforeeach", "ginkgo-aftereach"):
                start = j
                break

        body = _capture(
            lines,
            start,
            lambda ln2: ln2.has(*TERMINATORS),
            limit=200,
        )
        failures.append(Failure("ginkgo", name, body))
    return failures


def _bats(lines):
    """Bats: `not ok N <name>` plus the indented failure block beneath it."""
    failures = []
    for i, ln in enumerate(lines):
        m = BATS_NOT_OK.match(_strip_timestamp(ln.text))
        if not m:
            continue
        body = _capture(
            lines,
            i,
            lambda ln2: (
                ln2.has(*TERMINATORS)
                or BATS_NOT_OK.match(_strip_timestamp(ln2.text))
                or _strip_timestamp(ln2.text).startswith("ok ")
            ),
        )
        failures.append(Failure("bats", m.group(2).strip(), body))
    return failures


def _python(lines):
    """apiv2/docker-py suites: unittest `FAIL: test_x (module.Class)`."""
    failures = []
    for i, ln in enumerate(lines):
        m = PY_FAIL.match(_strip_timestamp(ln.text))
        if not m:
            continue
        body = _capture(
            lines,
            i,
            lambda ln2: (
                ln2.has(*TERMINATORS) or PY_FAIL.match(_strip_timestamp(ln2.text))
            ),
        )
        failures.append(Failure("python", f"{m.group(2)}.{m.group(1)}", body))
    return failures


def parse_html(doc, source=""):
    """Return every failure found in a logformatter HTML document."""
    lines = _lines(doc)
    failures = _ginkgo(lines) + _bats(lines) + _python(lines)
    for f in failures:
        f.source = source
    return _dedupe(failures)


def _dedupe(failures):
    """Ginkgo repeats failing specs in its end-of-run summary; unittest
    reports both the per-test FAIL and an aggregate line."""
    seen, out = set(), []
    for f in failures:
        k = (f.key(), f.text[:200])
        if k not in seen:
            seen.add(k)
            out.append(f)
    return out


def reduction(doc, failures):
    """How much text the model is spared. ~4 chars/token is a rule of thumb;
    exact tokenisation is model-specific, so report chars and an estimate."""
    before = len(doc)
    after = sum(len(f.text) for f in failures)
    pct = (1 - after / before) * 100 if before else 0.0
    return {
        "chars_before": before,
        "chars_after": after,
        "reduction_pct": round(pct, 1),
        "est_tokens_before": before // 4,
        "est_tokens_after": after // 4,
    }
