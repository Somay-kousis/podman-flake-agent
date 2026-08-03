"""Classify why a test failed: infra blip, race condition, network timeout, ...

Two design decisions worth defending, both taken from the constraint that this
has to run over ~30 failing jobs per PR without being ruinous:

1. STRUCTURED OUTPUT, NOT PROMPT-ONLY INSTRUCTIONS.
   The verdict is constrained to a JSON schema rather than asked for in prose.
   Prompt-only "reply with JSON like {...}" costs tokens on every call, drifts,
   and needs defensive re-parsing. A schema is enforced once.

2. `unknown` IS A REACHABLE ANSWER.
   A classifier that never abstains is worse than no classifier: confidently
   calling a real race condition an "infra blip" tells a maintainer to hit
   rerun on a genuine bug. Abstention rate is reported by eval.py as a
   first-class metric, not hidden.

Backends:
  --backend ollama   local model, stdlib only, no API key   (issue #29265: "Local AI is a plus")
  --backend api      Claude via the official `anthropic` SDK (pip install anthropic)

The Anthropic path additionally exposes a `search_flake_issues` tool so the
model can check the 42 open `flakes` issues itself rather than having every
issue title pre-stuffed into the prompt.
"""

import argparse
import json
import sys
import urllib.request

from . import store
from .taxonomy import CATEGORIES, SCHEMA, SYSTEM  # noqa: F401  (re-exported)

MODEL_API = "claude-opus-5"
MODEL_OLLAMA = "llama3.1"


def build_prompt(failure, evidence_rows):
    signals = "\n".join(
        f"- {e['signal']} (strength {e['strength']}): {e['detail']}"
        for e in evidence_rows
    ) or "- none mined yet"

    return f"""Test: {failure['name']}
Suite: {failure['kind']}

Mined flake signal (from CI history, not from this log):
{signals}

Failure output:
```
{failure['text'][:12000]}
```

Classify this failure."""


# -- backends -------------------------------------------------------------

class OllamaBackend:
    """Local model over Ollama's HTTP API. No API key, no third-party package."""

    def __init__(self, model=MODEL_OLLAMA, host="http://localhost:11434"):
        self.model = model
        self.host = host
        self.name = f"ollama:{model}"

    def classify(self, prompt):
        body = json.dumps({
            "model": self.model,
            "system": SYSTEM,
            "prompt": prompt,
            "format": SCHEMA,   # Ollama constrains sampling to the schema
            "stream": False,
            "options": {"temperature": 0},
        }).encode()

        req = urllib.request.Request(
            f"{self.host}/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            payload = json.loads(resp.read())

        return (
            json.loads(payload["response"]),
            payload.get("prompt_eval_count"),
            payload.get("eval_count"),
        )


class AnthropicBackend:
    """Claude via the official SDK, with a tool for deduplicating against the
    existing `flakes` issues."""

    def __init__(self, conn, model=MODEL_API):
        try:
            import anthropic
        except ImportError:
            sys.exit("the api backend needs `pip install anthropic` "
                     "(the ollama backend has no dependencies)")
        self.anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.model = model
        self.conn = conn
        self.name = f"anthropic:{model}"

    TOOLS = [{
        "name": "search_flake_issues",
        "description": (
            "Search Podman's open `flakes`-labelled issues by keyword. Call this "
            "before setting `duplicate_of` so you can cite a real issue number "
            "rather than guessing one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords from the test name or error."}
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "strict": True,
    }]

    def _search(self, query):
        terms = [t for t in query.lower().split() if len(t) > 3][:6]
        if not terms:
            return "no results"
        clause = " OR ".join("LOWER(title) LIKE ?" for _ in terms)
        rows = self.conn.execute(
            f"SELECT number, title, state FROM known_issues WHERE {clause} LIMIT 8",
            [f"%{t}%" for t in terms],
        ).fetchall()
        if not rows:
            return "no results"
        return "\n".join(f"#{r['number']} [{r['state']}] {r['title']}" for r in rows)

    def count_tokens(self, prompt):
        """Never estimate with a third-party tokenizer -- ask the API."""
        return self.client.messages.count_tokens(
            model=self.model,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        ).input_tokens

    def classify(self, prompt):
        messages = [{"role": "user", "content": prompt}]
        tokens_in = tokens_out = 0

        # Tool loop: let the model look up existing issues before it answers.
        for _ in range(4):
            resp = self.client.messages.create(
                model=self.model,
                # Thinking is on by default on Opus 5 and max_tokens caps
                # thinking + response together -- leave headroom.
                max_tokens=16000,
                system=SYSTEM,
                messages=messages,
                tools=self.TOOLS,
                output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
            )
            tokens_in += resp.usage.input_tokens
            tokens_out += resp.usage.output_tokens

            if resp.stop_reason == "refusal":
                return ({"category": "unknown", "confidence": 0.0,
                         "reasoning": "model declined to answer", "evidence": [],
                         "suggested_action": "review manually", "duplicate_of": None},
                        tokens_in, tokens_out)

            if resp.stop_reason != "tool_use":
                text = next(b.text for b in resp.content if b.type == "text")
                return json.loads(text), tokens_in, tokens_out

            messages.append({"role": "assistant", "content": resp.content})
            messages.append({"role": "user", "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": b.id,
                    "content": self._search(b.input["query"]),
                }
                for b in resp.content if b.type == "tool_use"
            ]})

        raise RuntimeError("tool loop did not converge")


# -- driver ---------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", choices=["ollama", "api"], default="ollama")
    ap.add_argument("--model")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--db")
    ap.add_argument("--dry-run", action="store_true",
                    help="print prompts and token counts; call no model")
    args = ap.parse_args(argv)

    conn = store.connect(args.db)
    rows = store.failure_frequency(conn, limit=args.limit)
    if not rows:
        print("nothing to classify; run `ingest artifacts` first")
        return 1

    backend = None
    if not args.dry_run:
        backend = (OllamaBackend(args.model or MODEL_OLLAMA) if args.backend == "ollama"
                   else AnthropicBackend(conn, args.model or MODEL_API))

    verbs = {}
    for row in rows:
        failure = {
            "name": row["name"],
            "kind": row["kind"],
            "text": store.sample_text(conn, row["fkey"]),
        }
        prompt = build_prompt(failure, store.evidence_for(conn, row["fkey"]))

        if args.dry_run:
            print(f"--- {row['fkey']}  ({len(prompt)} chars, ~{len(prompt)//4} tokens)")
            continue

        try:
            verdict, tin, tout = backend.classify(prompt)
        except Exception as e:
            print(f"  ! {row['fkey']}: {e}", file=sys.stderr)
            continue

        verdict.update(model=backend.name, backend=args.backend,
                       tokens_in=tin, tokens_out=tout)
        verb = store.consolidate(conn, row["fkey"], verdict)
        verbs[verb] = verbs.get(verb, 0) + 1
        conn.commit()

        print(f"  [{verb:10}] {verdict['category']:20} "
              f"conf={verdict.get('confidence')} {row['name'][:45]}")

    if verbs:
        print("\nwrite path:", ", ".join(f"{k}={v}" for k, v in sorted(verbs.items())))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
