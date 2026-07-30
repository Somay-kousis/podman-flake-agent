#!/usr/bin/env python3
"""Run the failure parser over the real logformatter fixtures.

No network, no API budget. Run: python3 tests/test_parse.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flakeagent.parse import parse_html, reduction

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# What upstream's own test data actually contains. Derived by reading the
# fixtures, so a regression in the parser shows up as a count mismatch.
EXPECTED = {
    "simple-bats": 1,
    "bats-with-timestamps-2023-05-16": 1,
    "simple-ginkgo": 1,
    "simple-python": 1,
}


def main():
    if not FIXTURES.exists():
        sys.exit("no fixtures; run tests/extract_fixtures.py first")

    failed = 0
    for path in sorted(FIXTURES.glob("*.html")):
        html = path.read_text()
        failures = parse_html(html, source=path.name)
        stats = reduction(html, failures)

        expected = EXPECTED.get(path.stem)
        ok = expected is None or len(failures) >= expected
        status = "ok  " if ok else "FAIL"
        if not ok:
            failed += 1

        print(f"{status} {path.stem}")
        print(
            f"       {len(failures)} failure(s) "
            f"(expected >= {expected})" if expected is not None else ""
        )
        print(
            f"       {stats['chars_before']:,} -> {stats['chars_after']:,} chars "
            f"({stats['reduction_pct']}% smaller, "
            f"~{stats['est_tokens_before']:,} -> ~{stats['est_tokens_after']:,} tokens)"
        )
        for f in failures:
            preview = " ".join(f.text.split())[:100]
            print(f"       [{f.kind}] {f.name[:70]}")
            print(f"              {preview}...")
        print()

    if failed:
        print(f"{failed} fixture(s) failed")
        return 1
    print("all fixtures parsed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
