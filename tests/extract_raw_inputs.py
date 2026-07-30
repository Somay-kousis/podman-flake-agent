#!/usr/bin/env python3
"""Extract the RAW ginkgo/bats logs from logformatter.t (the `<<<` half).

extract_fixtures.py pulls the `>>>` half (logformatter's HTML output). This
pulls the other side: what the test runner actually prints to stdout in CI,
before logformatter touches it. Useful for seeing what the pipeline starts from.

Usage: extract_raw_inputs.py /path/to/podman/hack/ci/logformatter.t
"""

import re
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "raw"


def main(src):
    text = Path(src).read_text(encoding="utf-8", errors="replace")
    _, _, data = text.partition("\n__END__\n")
    if not data:
        sys.exit("no __END__ section found")

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
        elif name and section == "input":
            cases[name].append(line.replace("&TRAILINGSPACE;", " "))

    for name, lines in cases.items():
        body = "\n".join(lines).strip()
        if not body:
            continue
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()[:60]
        (OUT / f"{slug}.log").write_text(body + "\n")
        print(f"  {slug}.log  ({len(body)} chars)")

    print(f"\n-> {OUT}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
