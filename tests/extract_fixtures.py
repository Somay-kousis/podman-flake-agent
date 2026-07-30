#!/usr/bin/env python3
"""Pull real logformatter HTML out of podman's own hack/ci/logformatter.t.

That file is the upstream Perl test suite: each case is

    == case name
    <<<
    raw ginkgo/bats log
    >>>
    expected HTML

The `>>>` half is exactly what logformatter emits in CI, which makes it a
free, authentic corpus for testing our parser -- no API calls, no CI run.

Usage: extract_fixtures.py /path/to/podman/hack/ci/logformatter.t
"""

import re
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "fixtures"


def main(src):
    text = Path(src).read_text(encoding="utf-8", errors="replace")
    _, _, data = text.partition("\n__END__\n")
    if not data:
        sys.exit("no __END__ section found; is this logformatter.t?")

    OUT.mkdir(exist_ok=True)
    cases, name, section = {}, None, None

    for line in data.splitlines():
        if line.startswith("== "):
            name = line[3:].strip()
            cases[name] = []
            section = None
        elif line.startswith("<<<"):
            section = "input"
        elif line.startswith(">>>"):
            section = "expect"
        elif name and section == "expect":
            cases[name].append(line.replace("&TRAILINGSPACE;", " "))

    written = 0
    for name, lines in cases.items():
        body = "\n".join(lines).strip()
        if not body:
            continue
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()[:60]
        (OUT / f"{slug}.html").write_text(body + "\n")
        written += 1
        print(f"  {slug}.html  ({len(body)} chars)")

    print(f"\n{written} fixtures -> {OUT}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
