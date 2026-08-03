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
import re
import sys
import time
import urllib.error
import urllib.request

from . import store
from .taxonomy import CATEGORIES, SCHEMA, SYSTEM  # noqa: F401  (re-exported)

MODEL_API = "claude-opus-5"
MODEL_OLLAMA = "llama3.1"
MODEL_GROQ = "openai/gpt-oss-120b"


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


class GroqBackend:
    """Groq's OpenAI-compatible endpoint. Standard library only, like the rest.

    Two things had to be discovered by running it rather than by reading docs:

    1. Groq sits behind Cloudflare, which rejects Python's default
       `Python-urllib/3.x` User-Agent with HTTP 403 and body `error code: 1010`.
       That reads exactly like an auth failure and sends you hunting for a bad
       key. An explicit User-Agent is mandatory, not cosmetic.

    2. Only the `openai/gpt-oss-*` models accept a real `json_schema` response
       format; `llama-3.3-70b-versatile` and `llama-3.1-8b-instant` 400 on it and
       offer only `json_object` (free-form JSON, no enforcement), and
       `qwen/qwen3.6-27b` rejects both. Schema enforcement is not a nicety here:
       it is what keeps `unknown` reachable and stops off-vocabulary categories
       reaching `eval.py`, which joins to gold labels by string equality.

    Keys rotate round-robin. The free tier limits per key per minute, so three
    keys is three times the headroom; on a 429 the caller is asked to slow down
    rather than the request being silently dropped.
    """

    ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
    UA = "podman-flake-agent/0.1 (+https://github.com/Somay-kousis/podman-flake-agent)"
    SCHEMA_MODELS = ("openai/gpt-oss",)

    def __init__(self, model=MODEL_GROQ, keys=None, max_tokens=1200):
        from .gh import read_env_file
        if not keys:
            env = read_env_file()
            keys = [env[k] for k in sorted(env) if k.startswith("GROQ_API_KEY")]
        if not keys:
            sys.exit("no GROQ_API_KEY* found in .env")
        self.keys = keys
        self._i = 0
        self.model = model
        self.name = f"groq:{model}"
        self.schema_capable = model.startswith(self.SCHEMA_MODELS)
        # Groq's free tier bills prompt + max_tokens against an 8,000 tokens-per-
        # minute ceiling, so an over-generous reservation fails the request
        # outright with HTTP 413 before the model ever runs. The verdict JSON is
        # a few hundred tokens; reserving 4k spent a third of the budget on
        # headroom that was never used.
        self.max_tokens = max_tokens

    def _key(self):
        k = self.keys[self._i % len(self.keys)]
        self._i += 1
        return k

    def classify(self, prompt):
        body = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": prompt}],
        }
        if self.schema_capable:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "verdict", "schema": SCHEMA, "strict": True},
            }
        else:
            # No enforcement available -- ask for JSON and validate on the way out.
            body["response_format"] = {"type": "json_object"}

        last = None
        for attempt in range(len(self.keys) * 4):
            req = urllib.request.Request(
                self.ENDPOINT, data=json.dumps(body).encode(),
                headers={"Authorization": f"Bearer {self._key()}",
                         "Content-Type": "application/json",
                         "User-Agent": self.UA})
            try:
                with urllib.request.urlopen(req, timeout=180) as resp:
                    d = json.load(resp)
                break
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}: {e.read().decode()[:200]}"
                if e.code == 429:
                    # TPM is a rolling per-minute window, so the useful wait is
                    # tens of seconds, not two. Try each key first -- they are
                    # separate orgs with separate budgets -- then wait out the
                    # window rather than hammering it.
                    m = re.search(r"try again in ([0-9.]+)s", last)
                    if attempt < len(self.keys) - 1:
                        time.sleep(1)
                    else:
                        time.sleep(min(65.0, float(m.group(1)) + 1 if m else 20.0))
                    continue
                if e.code == 413:
                    # Per-request size, not a rate limit -- another key has the
                    # same ceiling, so retrying anywhere is wasted.
                    raise RuntimeError(last + "  [shrink the prompt or max_tokens]") from e
                raise RuntimeError(last) from e
        else:
            raise RuntimeError(f"all keys rate-limited: {last}")

        msg = d["choices"][0]["message"]
        verdict = json.loads(msg["content"])
        usage = d.get("usage") or {}
        return verdict, usage.get("prompt_tokens"), usage.get("completion_tokens")


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
