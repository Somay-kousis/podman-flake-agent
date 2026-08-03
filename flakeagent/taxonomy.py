"""The category vocabulary, output schema, and system prompt — defined once.

This module exists because the vocabulary was previously duplicated in
`classify.py` and `labels.py`. They happened to agree, but nothing enforced it,
and a drift would have been silent: `eval.py` joins predictions to gold labels
by string equality, so a category renamed in one file and not the other scores
as wrong rather than raising. Every consumer now imports from here.

`unknown` is a first-class member of CATEGORIES, not an error path. A classifier
that never abstains is worse than no classifier: confidently calling a real race
condition an `infra_blip` tells a maintainer to press rerun on a genuine bug.
`eval.py` reports abstention beside accuracy for that reason.
"""

CATEGORIES = [
    "infra_blip",
    "race_condition",
    "network_timeout",
    "resource_exhaustion",
    "real_bug",
    "unknown",
]

# One-line reminders shown by `labels.py` while hand-labelling. Kept next to the
# system prompt below so the human and the model are held to the same
# definitions -- if these two drift, the gold labels stop meaning what the
# classifier was asked for.
HINTS = {
    "infra_blip": "registry/mirror down, DNS, package install failed, runner died",
    "race_condition": "ordering/timing dependency; journald timeliness; concurrent ops",
    "network_timeout": "reachable but too slow; a deadline was exceeded",
    "resource_exhaustion": "out of disk, memory, inodes, PIDs, ports, ulimits",
    "real_bug": "deterministic; the change under test broke it",
    "unknown": "the evidence does not distinguish -- a valid and useful answer",
}

SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": CATEGORIES},
        "confidence": {"type": "number"},
        "reasoning": {"type": "string"},
        "evidence": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Verbatim log lines supporting the verdict.",
        },
        "suggested_action": {"type": "string"},
        "duplicate_of": {
            "type": ["integer", "null"],
            "description": "Existing `flakes` issue number, or null.",
        },
    },
    "required": [
        "category", "confidence", "reasoning", "evidence",
        "suggested_action", "duplicate_of",
    ],
    "additionalProperties": False,
}

SYSTEM = """You triage failing CI tests for Podman \
(podman-container-tools/podman), whose suites are Ginkgo (Go), bats (shell), and
Python unittest.

Decide why THIS test failed. The categories:

- infra_blip: registry/mirror unavailable, DNS failure, package install failure,
  runner died. External to the code under test.
- network_timeout: a network operation exceeded its deadline. Distinguish from
  infra_blip: the endpoint was reachable but slow.
- race_condition: ordering/timing dependency in the test or the code. Signals
  include journald timeliness, missing-but-later-present logs, concurrent
  container/pod operations, cleanup racing startup.
- resource_exhaustion: out of disk, memory, inodes, PIDs, ports.
- real_bug: the diff under test broke this. Choose this when the failure is
  specific and deterministic rather than timing-dependent.
- unknown: the evidence does not distinguish between the above.

Rules:
- Prefer `unknown` over a confident guess. A wrong confident verdict is worse
  than an abstention, because maintainers act on it.
- `evidence` must quote lines that actually appear in the log. Do not paraphrase
  and do not invent line numbers.
- Podman flakes are frequently journald timeliness issues; the log's own
  comments often say so. Weigh that, but do not assume it.
"""


def abstained(category):
    """True for the one category that declines to answer."""
    return category == "unknown"


# Categories that tell a maintainer "just press rerun". Predicting one of these
# for a failure whose gold label is `real_bug` is the dangerous confusion --
# `eval.py` counts it separately from ordinary error.
RERUNNABLE = ("infra_blip", "network_timeout")
