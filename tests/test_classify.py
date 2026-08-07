#!/usr/bin/env python3
"""Offline test for AnthropicBackend._search() -- no network, no API key.

It's the only piece of the agentic arm that can be tested without one: the
tool itself is plain keyword matching against a local table, and it never
touches `self.client`. This does not exercise the tool-*calling* loop --
that needs a real model deciding to call the tool -- only the lookup the
loop hands off to. See docs/RESULTS.md for why the loop itself is unmeasured.

Run: python3 tests/test_classify.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flakeagent import store

ISSUES = [
    (28940, "podman update - set ulimits flake -- crun: clone: Resource "
            "temporarily unavailable", "closed"),
    (29091, "CI: restore logformatter under GHA", "open"),
    (27412, "machine: Volume ops test flakes on fedora-rawhide", "open"),
    (30012, "network reload flakes under rootless", "open"),
]


def seeded_conn():
    """A temp db with schema.sql applied and ISSUES loaded -- store.connect
    does both; nothing here is dossier- or fetch-specific."""
    import tempfile
    db = Path(tempfile.mkdtemp()) / "t.db"
    conn = store.connect(db)
    conn.executemany(
        "INSERT INTO known_issues (number, title, state) VALUES (?, ?, ?)",
        ISSUES,
    )
    conn.commit()
    return conn


def search(conn, query):
    """Call AnthropicBackend._search() without needing anthropic installed
    or a real instance -- it only reads self.conn, so a bare stand-in with
    just that attribute is enough. Importing the real class would trip its
    `import anthropic` guard on a machine that doesn't have the package,
    which is exactly the case this offline suite has to run under."""
    from flakeagent.classify import AnthropicBackend
    stub = AnthropicBackend.__new__(AnthropicBackend)  # skip __init__
    stub.conn = conn
    return AnthropicBackend._search(stub, query)


def main():
    conn = seeded_conn()
    ok = True

    def check(label, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'} {label}: {got!r}"
              + ("" if good else f" (expected {want!r})"))

    def check_contains(label, got, needle):
        nonlocal ok
        good = needle in got
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'} {label}: "
              + ("contains" if good else "MISSING") + f" {needle!r}")

    print("keyword match:")
    r = search(conn, "ulimits crun clone")
    check_contains("finds the ulimits issue", r, "#28940")
    check("only the one match", r.count("#"), 1)

    r = search(conn, "volume ops machine init with volume")
    check_contains("finds the volume issue by overlap", r, "#27412")

    print("\nno match:")
    check("unrelated query", search(conn, "completely unrelated gibberish zzz"),
          "no results")

    print("\nempty after the length-4 filter:")
    # every token in the query is <= 3 chars, so `_search`'s
    # `len(t) > 3` filter drops all of them before a query is even built --
    # this must return the same "no results" sentinel, not error on an
    # empty IN-clause.
    check("all-short-token query", search(conn, "a is to it if of"), "no results")

    print("\nempty query:")
    check("blank string", search(conn, ""), "no results")

    print("\nstate is carried through:")
    r = search(conn, "logformatter restore GHA")
    check_contains("shows [open]", r, "[open] CI: restore logformatter")

    r = search(conn, "ulimits crun")
    check_contains("shows [closed]", r, "[closed]")

    print("\n" + ("all checks passed" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
