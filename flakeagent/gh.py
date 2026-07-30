"""GitHub REST client: read-only by construction, cached, conditional, polite.

This talks to a live open-source project's CI. Three properties matter more than
speed, and all three are structural rather than conventions to remember:

1. READ-ONLY BY CONSTRUCTION.
   There is exactly one request path and its method is the literal "GET".
   No function in this module accepts a method argument. Nothing here can
   create, edit, or delete anything upstream -- not an issue, not a comment,
   not a reaction. Verify with: grep -n 'method' flakeagent/gh.py

2. CONDITIONAL REQUESTS.
   Every cached response keeps its ETag. Revalidation sends If-None-Match, and
   **a 304 costs no rate-limit quota** -- that is GitHub's documented guidance
   and the main reason re-running this pipeline is nearly free for them.

3. A RESERVE FLOOR.
   We stop while quota remains (default 100) rather than draining a token to
   zero, and we back off properly on 429 / secondary limits / 5xx instead of
   retrying into a wall.

Requests are serial. No concurrency, by choice.

Stdlib only -- see README for why.
"""

import gzip
import hashlib
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

GITHUB_API = "https://api.github.com"
DEFAULT_CACHE = Path(__file__).resolve().parent.parent / "data" / "cache"

UA = "podman-flake-agent/0.1 (research prototype; contact via GitHub)"

# Stop with this much quota left rather than exhausting the token.
DEFAULT_RESERVE = 100
MAX_ATTEMPTS = 5
MAX_BACKOFF = 60.0


ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

# Values that mean "you pasted the example, not your token". Treating these as
# unset gives a useful warning instead of a wall of 401s -- an invalid token is
# worse than none, because GitHub stops serving the anonymous tier entirely.
PLACEHOLDERS = {
    "github_pat_your_token_here", "your_token_here", "ghp_your_token_here",
    "xxx", "changeme", "todo",
}


def read_env_file(path=ENV_FILE):
    """Read KEY=value pairs from a .env file, leniently.

    Accepts `KEY=v`, `KEY = v`, `export KEY=v`, quoted values, `#` comments --
    so a hand-written file works whatever style you reach for. The file is
    gitignored; it must never be committed.
    """
    out = {}
    try:
        text = Path(path).read_text()
    except (OSError, UnicodeDecodeError):
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip("\"'").strip()
        if key:
            out[key.upper()] = value
    return out


def resolve_token(explicit=None):
    """Find a usable token: explicit arg, then $GITHUB_TOKEN, then .env.

    A placeholder value is treated as absent, and an env-var placeholder does
    not shadow a real token in .env -- otherwise a stale line in ~/.zshrc would
    silently break a correctly-written file.
    """
    env_file = read_env_file()
    candidates = [
        explicit,
        os.environ.get("GITHUB_TOKEN"),
        env_file.get("GITHUB_TOKEN"),
        env_file.get("GITHUB_TOKENS"),  # a plausible typo; accept it
        env_file.get("GH_TOKEN"),
    ]
    for c in candidates:
        if c and c.strip().lower() not in PLACEHOLDERS:
            return c.strip()
    return None


class _CrossHostAuthStripper(urllib.request.HTTPRedirectHandler):
    """Drop Authorization when a redirect crosses to a different host.

    GitHub answers /actions/jobs/{id}/logs and /actions/artifacts/{id}/zip with a
    302 to a signed storage URL on productionresultssa18.blob.core.windows.net.
    urllib re-sends every original header to that host, and Azure rejects a
    request carrying both its own SAS signature and a bearer token:

        401 Server failed to authenticate the request.

    The signature is already in the redirect URL's query string, so the header is
    not merely unnecessary -- it breaks the request, and it leaks the token to a
    third-party host. Strip it whenever the host changes.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        if urllib.parse.urlparse(req.full_url).netloc != urllib.parse.urlparse(newurl).netloc:
            for name in ("Authorization", "authorization"):
                new.headers.pop(name, None)
                new.unredirected_hdrs.pop(name, None)
        return new


_OPENER = urllib.request.build_opener(_CrossHostAuthStripper)


class RateLimited(RuntimeError):
    """Raised when the API budget is exhausted or the reserve floor is hit."""

    def __init__(self, reset_epoch, remaining=None, reserve=None):
        self.reset_epoch = reset_epoch
        wait = max(0, int((reset_epoch or 0) - time.time()))
        if remaining is not None and reserve is not None and remaining > 0:
            msg = (f"stopping with {remaining} requests left (reserve floor {reserve}); "
                   f"limit resets in {wait // 60}m{wait % 60}s")
        else:
            msg = (f"GitHub API rate limit exhausted; resets in {wait // 60}m{wait % 60}s. "
                   "Set GITHUB_TOKEN to raise the limit from 60/hr to 5000/hr.")
        super().__init__(msg)


class GitHub:
    def __init__(self, token=None, cache_dir=None, verbose=False,
                 reserve=DEFAULT_RESERVE, revalidate=False):
        self.token = resolve_token(token)
        self.cache_dir = Path(cache_dir or DEFAULT_CACHE)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.verbose = verbose
        self.reserve = reserve
        # revalidate=True re-checks cached entries with If-None-Match instead of
        # trusting them blindly. Cheap (304s are free) but not free in wall time.
        self.revalidate = revalidate

        self.calls = 0          # requests that actually left the machine
        self.cache_hits = 0     # served from disk without a request
        self.not_modified = 0   # 304s -- revalidated, quota-free
        self.sleeps = 0.0       # total seconds spent backing off
        self.remaining = None   # x-ratelimit-remaining from the last response

    # -- plumbing ---------------------------------------------------------

    def _headers(self, extra=None):
        h = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": UA,
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if extra:
            h.update(extra)
        return h

    def _cache_path(self, url):
        key = hashlib.sha256(url.encode()).hexdigest()[:20]
        hint = urllib.parse.urlparse(url).path.strip("/").replace("/", "_")[-60:]
        return self.cache_dir / f"{hint}__{key}.json"

    def _load(self, url):
        p = self._cache_path(url)
        if not p.exists():
            return None
        try:
            env = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        return env if isinstance(env, dict) and "body" in env else None

    def _store(self, url, body, headers):
        self._cache_path(url).write_text(json.dumps({
            "url": url,
            "etag": headers.get("ETag"),
            "last_modified": headers.get("Last-Modified"),
            "fetched_at": time.time(),
            "body": body,
        }))

    def _note_limits(self, headers):
        """Enforce the reserve floor, scaled to whichever pool answered.

        GitHub reports these headers per rate-limit pool, and the pools differ by
        two orders of magnitude: core is 5,000/hr, search is 30/min. A fixed
        reserve of 100 is sensible for core and impossible for search -- every
        search response is permanently "below the floor", so the client refuses
        to make the request it was asked to make.

        So scale: keep the configured reserve for large pools, and fall back to a
        small proportional floor for small ones.
        """
        rem = headers.get("x-ratelimit-remaining")
        if rem is None:
            return
        try:
            self.remaining = int(rem)
        except ValueError:
            return

        try:
            limit = int(headers.get("x-ratelimit-limit", 0))
        except ValueError:
            limit = 0

        floor = self.reserve
        if 0 < limit <= self.reserve * 2:
            floor = max(2, limit // 10)

        if self.remaining <= floor:
            reset = headers.get("x-ratelimit-reset")
            raise RateLimited(int(reset) if reset else 0, self.remaining, floor)

    def _backoff(self, attempt, headers=None):
        delay = None
        if headers is not None:
            ra = headers.get("Retry-After")
            if ra:
                try:
                    delay = float(ra)
                except ValueError:
                    delay = None
        if delay is None:
            delay = min(MAX_BACKOFF, (2 ** attempt) + random.uniform(0, 1))
        if self.verbose:
            print(f"    backing off {delay:.1f}s (attempt {attempt})")
        self.sleeps += delay
        time.sleep(delay)

    @staticmethod
    def _is_secondary_limit(err_body):
        return b"secondary rate limit" in (err_body or b"").lower()

    def _get(self, url, extra_headers=None, check_reserve=True):
        """The one and only request path. Method is a literal GET.

        Returns (status, body_bytes_or_None, headers). status 304 means the
        caller's cached copy is still current.

        `check_reserve=False` is for /rate_limit only: it does not consume quota
        and reporting the budget must not itself be blocked by the budget.
        """
        for attempt in range(MAX_ATTEMPTS):
            req = urllib.request.Request(
                url, headers=self._headers(extra_headers), method="GET")
            try:
                with _OPENER.open(req, timeout=120) as resp:
                    data = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        data = gzip.decompress(data)
                    self.calls += 1
                    if check_reserve:
                        self._note_limits(resp.headers)
                    return resp.status, data, resp.headers

            except urllib.error.HTTPError as e:
                headers = e.headers or {}
                if e.code == 304:
                    self.not_modified += 1
                    # A 304 does not consume quota; do not count it as a call.
                    return 304, None, headers

                body = b""
                try:
                    body = e.read()
                except Exception:
                    pass

                hard_limit = headers.get("X-RateLimit-Remaining") == "0"
                if e.code in (429, 403) and (hard_limit or self._is_secondary_limit(body)):
                    if hard_limit:
                        raise RateLimited(int(headers.get("X-RateLimit-Reset", 0))) from e
                    # Secondary limit: back off and retry.
                    if attempt < MAX_ATTEMPTS - 1:
                        self._backoff(attempt, headers)
                        continue
                if e.code >= 500 and attempt < MAX_ATTEMPTS - 1:
                    self._backoff(attempt, headers)
                    continue
                raise

            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < MAX_ATTEMPTS - 1:
                    self._backoff(attempt)
                    continue
                raise RuntimeError(f"network error for {url}: {e}") from e

        raise RuntimeError(f"gave up after {MAX_ATTEMPTS} attempts: {url}")

    # -- public -----------------------------------------------------------

    def get(self, path, refresh=False, **params):
        """GET a JSON endpoint, using the cache and conditional requests."""
        url = path if path.startswith("http") else f"{GITHUB_API}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        cached = self._load(url)

        if cached and not refresh and not self.revalidate:
            self.cache_hits += 1
            return cached["body"]

        extra = {}
        if cached and not refresh:
            if cached.get("etag"):
                extra["If-None-Match"] = cached["etag"]
            elif cached.get("last_modified"):
                extra["If-Modified-Since"] = cached["last_modified"]

        if self.verbose:
            print(f"  GET {url}{' (revalidate)' if extra else ''}")

        status, data, headers = self._get(url, extra or None)
        if status == 304 and cached:
            self.cache_hits += 1
            return cached["body"]

        payload = json.loads(data)
        self._store(url, payload, headers)
        return payload

    def paginate(self, path, key=None, max_pages=10, per_page=100, **params):
        """Yield items across pages. `key` names the list field for search-style
        responses; omit it when the body is itself a list."""
        for page in range(1, max_pages + 1):
            chunk = self.get(path, per_page=per_page, page=page, **params)
            items = chunk if key is None else chunk.get(key, [])
            if not items:
                return
            yield from items
            if len(items) < per_page:
                return

    def download(self, url, dest):
        """Download a binary (an artifact zip) to `dest`. Cached by path."""
        dest = Path(dest)
        if dest.exists() and dest.stat().st_size > 0:
            self.cache_hits += 1
            return dest
        if self.verbose:
            print(f"  DOWNLOAD {url}")
        _, data, _ = self._get(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest

    def budget(self):
        """Remaining core-API quota. Bypasses the cache and the reserve check --
        /rate_limit consumes no quota, and asking "how much is left?" must work
        precisely when little is left."""
        _, data, _ = self._get(f"{GITHUB_API}/rate_limit", check_reserve=False)
        core = json.loads(data)["resources"]["core"]
        return core["remaining"], core["limit"], core["reset"]

    def stats(self):
        bits = [f"{self.calls} requests", f"{self.cache_hits} cached",
                f"{self.not_modified} not-modified"]
        if self.sleeps:
            bits.append(f"{self.sleeps:.0f}s backoff")
        if self.remaining is not None:
            bits.append(f"{self.remaining} quota left")
        return ", ".join(bits)
