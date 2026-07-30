-- podman-flake-agent storage.
--
-- The design point worth noting: `classifications` is bi-temporal. A flake is
-- not a static fact -- it appears, gets diagnosed, gets fixed, and regresses.
-- A row is never destructively updated; it is invalidated and superseded, so
-- "what did we believe about this test in June, and were we right?" stays
-- answerable. That is what makes the evaluation harness possible at all.

PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY,   -- GitHub run id
    run_number    INTEGER,
    run_attempt   INTEGER NOT NULL DEFAULT 1,
    head_sha      TEXT    NOT NULL,
    head_branch   TEXT,
    event         TEXT,                  -- pull_request | push | schedule
    conclusion    TEXT,                  -- success | failure | cancelled
    pr_number     INTEGER,
    created_at    TEXT,
    html_url      TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY,   -- GitHub job id
    run_id        INTEGER NOT NULL REFERENCES runs(id),
    name          TEXT    NOT NULL,      -- e.g. "int local root fedora-current"
    conclusion    TEXT,
    -- Parsed out of the matrix job name; see ingest.parse_job_name().
    test          TEXT,
    mode          TEXT,
    priv          TEXT,
    distro        TEXT,
    started_at    TEXT,
    completed_at  TEXT,
    html_url      TEXT
);

CREATE INDEX IF NOT EXISTS jobs_run ON jobs(run_id);

CREATE TABLE IF NOT EXISTS test_failures (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        INTEGER NOT NULL REFERENCES jobs(id),
    fkey          TEXT    NOT NULL,      -- 'ginkgo:<spec name>' -- stable identity
    kind          TEXT    NOT NULL,
    name          TEXT    NOT NULL,
    text          TEXT    NOT NULL,
    source        TEXT,
    UNIQUE(job_id, fkey)
);

CREATE INDEX IF NOT EXISTS failures_key ON test_failures(fkey);

-- Mined ground truth. With GINKGO_FLAKE_ATTEMPTS=0 upstream, "failed then
-- passed on rerun" is not generated for us -- it has to be inferred.
CREATE TABLE IF NOT EXISTS flake_evidence (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fkey          TEXT    NOT NULL,
    signal        TEXT    NOT NULL,      -- rerun_disagreement | cross_pr | main_failure
    strength      REAL    NOT NULL,      -- 0..1
    detail        TEXT,
    observed_at   TEXT    NOT NULL,
    UNIQUE(fkey, signal, detail)
);

CREATE INDEX IF NOT EXISTS evidence_key ON flake_evidence(fkey);

CREATE TABLE IF NOT EXISTS classifications (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fkey          TEXT    NOT NULL,
    category      TEXT    NOT NULL,      -- infra_blip|race_condition|network_timeout|
                                         -- resource_exhaustion|real_bug|unknown
    confidence    REAL,
    reasoning     TEXT,
    evidence      TEXT,                  -- JSON array of quoted log lines
    suggested_action TEXT,
    duplicate_of  INTEGER,               -- existing `flakes` issue number
    model         TEXT,
    backend       TEXT,                  -- ollama | api
    tokens_in     INTEGER,
    tokens_out    INTEGER,
    -- bi-temporal validity
    valid_from    TEXT NOT NULL,
    valid_to      TEXT,                  -- NULL = currently believed
    superseded_by INTEGER REFERENCES classifications(id)
);

CREATE INDEX IF NOT EXISTS class_key ON classifications(fkey);
CREATE INDEX IF NOT EXISTS class_current ON classifications(fkey, valid_to);

-- Known flakes already tracked upstream, for deduplication.
CREATE TABLE IF NOT EXISTS known_issues (
    number        INTEGER PRIMARY KEY,
    title         TEXT NOT NULL,
    state         TEXT,
    labels        TEXT,
    body          TEXT,
    updated_at    TEXT
);

-- Hand-labelled ground truth for eval.py.
CREATE TABLE IF NOT EXISTS gold_labels (
    fkey          TEXT PRIMARY KEY,
    category      TEXT NOT NULL,
    issue_number  INTEGER,
    note          TEXT
);

-- Log excerpts pasted into `flakes`-labelled issues by maintainers.
--
-- Why this exists: live CI artifacts need a token AND, today, only ship
-- `journal-*.log` (no logformatter HTML until podman#29091 lands). The issues
-- themselves are public, and ~88% of a 25-issue sample carried a pasted log
-- block. That makes them the one source of real failure text -- across both the
-- Cirrus and GitHub Actions eras -- that needs no credentials, and each comes
-- attached to a maintainer's own diagnosis in the issue title.
CREATE TABLE IF NOT EXISTS corpus_samples (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_number  INTEGER NOT NULL,
    source        TEXT    NOT NULL,     -- 'body' | 'comment:<id>'
    block_index   INTEGER NOT NULL,     -- nth extracted block within that source
    era           TEXT,                 -- gha | cirrus | unknown
    suite         TEXT,                 -- ginkgo | bats | python | unknown
    text          TEXT    NOT NULL,
    UNIQUE(issue_number, source, block_index)
);

CREATE INDEX IF NOT EXISTS corpus_issue ON corpus_samples(issue_number);
CREATE INDEX IF NOT EXISTS corpus_tags  ON corpus_samples(era, suite);

-- Per-step outcomes inside a job.
--
-- The highest-value data the API gives away for free. A job that dies in
-- "Install build dependencies" or "Set up Go" is infrastructure; one that dies
-- in "Run machine e2e" is a test. That is root-cause attribution with no log
-- parsing, no artifact download, and no token -- it arrives inside the ordinary
-- jobs response, which the earlier code discarded.
CREATE TABLE IF NOT EXISTS job_steps (
    job_id        INTEGER NOT NULL REFERENCES jobs(id),
    number        INTEGER NOT NULL,
    name          TEXT,
    status        TEXT,
    conclusion    TEXT,       -- success | failure | skipped | cancelled
    started_at    TEXT,
    completed_at  TEXT,
    PRIMARY KEY (job_id, number)
);

CREATE INDEX IF NOT EXISTS steps_failed ON job_steps(conclusion);

-- Artifact metadata is always recorded; `local_path` is set only when content
-- was actually downloaded (--download). Journals run 155KB-997KB each with ~46
-- per failing run, so pulling content is opt-in.
CREATE TABLE IF NOT EXISTS artifacts (
    id                   INTEGER PRIMARY KEY,   -- GitHub artifact id
    run_id               INTEGER NOT NULL,
    name                 TEXT NOT NULL,
    size_in_bytes        INTEGER,
    expired              INTEGER,
    created_at           TEXT,
    expires_at           TEXT,
    archive_download_url TEXT,
    local_path           TEXT,
    downloaded_at        TEXT
);

CREATE INDEX IF NOT EXISTS artifacts_run ON artifacts(run_id);

-- GitHub check-run annotations. Often thin ("Process completed with exit code
-- 2") but occasionally carry the real error; cheap enough to take.
CREATE TABLE IF NOT EXISTS annotations (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id           INTEGER NOT NULL,
    annotation_level TEXT,
    path             TEXT,
    start_line       INTEGER,
    end_line         INTEGER,
    title            TEXT,
    message          TEXT,
    UNIQUE(job_id, path, start_line, message)
);

CREATE TABLE IF NOT EXISTS issue_comments (
    id            INTEGER PRIMARY KEY,
    issue_number  INTEGER NOT NULL,
    user_login    TEXT,
    created_at    TEXT,
    body          TEXT
);

CREATE INDEX IF NOT EXISTS comments_issue ON issue_comments(issue_number);

-- Raw GitHub Actions job logs, stored gzipped on disk.
--
-- These supersede journal artifacts as the primary content source: ~500KB per
-- failed job versus ~32MB per run, and only a handful of jobs in a run fail.
-- The log carries the actual ginkgo/bats output in the 2026 GHA format, where
-- every line is prefixed with an ISO-8601 timestamp -- which is what makes
-- step-window slicing possible without a parser.
CREATE TABLE IF NOT EXISTS job_logs (
    job_id        INTEGER PRIMARY KEY,
    run_id        INTEGER,
    path          TEXT NOT NULL,      -- relative to the repo root
    bytes_raw     INTEGER,
    bytes_stored  INTEGER,
    line_count    INTEGER,
    first_ts      TEXT,
    last_ts       TEXT,
    sha256        TEXT,
    fetched_at    TEXT
);

CREATE INDEX IF NOT EXISTS job_logs_run ON job_logs(run_id);

-- Files a pull request changed.
--
-- Evidence for "could this diff plausibly have caused this failure?". A PR that
-- touches only go.mod/go.sum/vendor cannot have broken a container runtime test.
CREATE TABLE IF NOT EXISTS pr_files (
    pr_number     INTEGER NOT NULL,
    filename      TEXT NOT NULL,
    status        TEXT,
    additions     INTEGER,
    deletions     INTEGER,
    PRIMARY KEY (pr_number, filename)
);

-- Commits that fixed a flake, linked to the issue reporting it.
--
-- The only source of supervised ground truth this project has. A `flakes` issue
-- says a test was flaky; the commit that closed it says what was actually wrong
-- and what fixed it. Everything else here is unlabelled observation.
--
-- Two acquisition paths with different precision/recall tradeoffs, recorded in
-- `source`:
--   timeline -- /issues/{n}/timeline closed/cross-referenced events. Precise:
--               the maintainer explicitly linked them.
--   search   -- /search/commits parsed for #NNNNN. Broader: catches fixes that
--               never formally closed an issue, at the cost of false links.
CREATE TABLE IF NOT EXISTS fix_commits (
    sha           TEXT NOT NULL,
    issue_number  INTEGER,
    pr_number     INTEGER,
    message       TEXT,
    author        TEXT,
    committed_at  TEXT,
    source        TEXT NOT NULL,     -- 'timeline' | 'search'
    PRIMARY KEY (sha, issue_number, source)
);

CREATE INDEX IF NOT EXISTS fixes_issue ON fix_commits(issue_number);

-- Issue timeline events: when `flakes` was applied, who closed it, what
-- referenced it. Feeds fix_commits, and independently answers "how long did
-- this flake stay open" and "who triages flakes".
CREATE TABLE IF NOT EXISTS issue_events (
    id            INTEGER PRIMARY KEY,
    issue_number  INTEGER NOT NULL,
    event         TEXT,
    actor         TEXT,
    created_at    TEXT,
    label         TEXT,
    commit_id     TEXT,
    source_issue  INTEGER            -- the PR/issue that cross-referenced this one
);

CREATE INDEX IF NOT EXISTS events_issue ON issue_events(issue_number);
CREATE INDEX IF NOT EXISTS events_kind  ON issue_events(event);
