#!/usr/bin/env python3
"""Structural sanity check for mermaid blocks in markdown.

    python3 hack/check_mermaid.py docs/*.md

Mermaid fails silently when rendered -- a broken diagram shows an error box or
nothing at all, and neither is visible from the markdown source. This catches
the structural mistakes that actually happen: unbalanced subgraph/end, a `class`
line naming a node that was never declared, a classDef that is referenced but
not defined, and edges pointing at undeclared ids.

Handles flowchart and erDiagram; other diagram types are checked for fence
balance only.
"""

import re
import sys

FENCE = re.compile(r"```mermaid\n(.*?)```", re.S)


def check_flowchart(body):
    problems = []

    ids = set(re.findall(r"(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)\s*[\[\(\{]", body))
    subs = set(re.findall(r"subgraph\s+([A-Za-z_][A-Za-z0-9_]*)", body))
    known = ids | subs

    classed, used_styles = set(), set()
    for line in body.splitlines():
        m = re.match(r"\s*class\s+([A-Za-z0-9_,]+)\s+(\w+)\s*$", line)
        if m:
            classed |= set(m.group(1).split(","))
            used_styles.add(m.group(2))
    defined_styles = set(re.findall(r"classDef\s+(\w+)", body))

    edge_ids = set()
    for m in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)\s*-[.-]*->", body):
        edge_ids.add(m.group(1))
    for m in re.finditer(r"-[.-]*->\s*(?:\|[^|]*\|\s*)?([A-Za-z_][A-Za-z0-9_]*)", body):
        edge_ids.add(m.group(1))

    opens = len(re.findall(r"^\s*subgraph\b", body, re.M))
    ends = len(re.findall(r"^\s*end\s*$", body, re.M))
    if opens != ends:
        problems.append(f"subgraph/end mismatch: {opens} open, {ends} end")
    if classed - known:
        problems.append(f"class names undeclared nodes: {sorted(classed - known)}")
    if used_styles - defined_styles:
        problems.append(f"class uses undefined classDef: {sorted(used_styles - defined_styles)}")
    if edge_ids - known:
        problems.append(f"edges reference undeclared ids: {sorted(edge_ids - known)}")

    return problems, f"{len(ids)} nodes, {len(subs)} subgraphs"


def check_er(body):
    """Brace balance across entity blocks only.

    Relationship lines carry cardinality notation -- `||--o{`, `}o--o|`,
    `}|..|{` -- whose braces are syntax, not blocks. Counting them naively
    reports every valid ER diagram as unbalanced.
    """
    problems = []
    block_lines = [l for l in body.splitlines() if "--" not in l and ".." not in l]
    stripped = "\n".join(block_lines)
    opens, closes = stripped.count("{"), stripped.count("}")
    if opens != closes:
        problems.append(f"unbalanced entity braces: {opens} open, {closes} close")
    entities = set(re.findall(r"^\s*(\w+)\s*\{", stripped, re.M))
    return problems, f"{len(entities)} entities"


def main(paths):
    ok = True
    for path in paths:
        src = open(path).read()
        blocks = FENCE.findall(src)
        fences = src.count("```")
        print(f"{path}: {len(blocks)} mermaid block(s), {fences} fences"
              + ("" if fences % 2 == 0 else "  !! ODD FENCE COUNT"))
        if fences % 2:
            ok = False

        for i, body in enumerate(blocks, 1):
            kind = "flowchart" if re.search(r"^\s*(flowchart|graph)\b", body, re.M) else (
                   "erDiagram" if "erDiagram" in body else "other")
            if kind == "flowchart":
                problems, summary = check_flowchart(body)
            elif kind == "erDiagram":
                problems, summary = check_er(body)
            else:
                problems, summary = [], "unchecked type"

            status = "ok " if not problems else "FAIL"
            print(f"  {status} block {i} ({kind}): {summary}")
            for p in problems:
                print(f"       !! {p}")
                ok = False

    print("\n" + ("all diagrams look structurally valid" if ok else "PROBLEMS ABOVE"))
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    raise SystemExit(main(sys.argv[1:]))
