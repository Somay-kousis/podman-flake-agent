"""SQLite persistence with a non-destructive write path for classifications.

The consolidation verbs (ADD / UPDATE / INVALIDATE / NOOP) are borrowed from
memory-consolidation designs: re-running the classifier must not silently
rewrite history, because the evaluation harness needs to know what we believed
and when. Re-analysing an unchanged failure is a NOOP, not a duplicate row.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = Path(__file__).resolve().parent / "schema.sql"
DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "flakes.db"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Columns added to tables that predate the fetch pipeline. `executescript` on
# schema.sql creates missing *tables* but cannot add columns to existing ones,
# and SQLite has no "ADD COLUMN IF NOT EXISTS" -- so reconcile by hand.
MIGRATIONS = {
    "runs": [
        ("workflow_name", "TEXT"), ("path", "TEXT"), ("run_started_at", "TEXT"),
        ("updated_at", "TEXT"), ("actor_login", "TEXT"),
        ("previous_attempt_url", "TEXT"), ("display_title", "TEXT"),
    ],
    "jobs": [
        ("run_attempt", "INTEGER"), ("runner_name", "TEXT"), ("runner_id", "INTEGER"),
        ("labels", "TEXT"), ("workflow_name", "TEXT"), ("head_sha", "TEXT"),
        ("check_run_url", "TEXT"), ("status", "TEXT"), ("created_at", "TEXT"),
    ],
}


def _migrate(conn):
    for table, columns in MIGRATIONS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue  # table not created yet; schema.sql will handle it
        for name, decl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.commit()


def connect(path=None):
    path = Path(path or DEFAULT_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA.read_text())
    _migrate(conn)
    return conn


# -- ingest writes --------------------------------------------------------

def upsert_run(conn, run, pr_number=None):
    conn.execute(
        """INSERT INTO runs (id, run_number, run_attempt, head_sha, head_branch,
                             event, conclusion, pr_number, created_at, html_url,
                             workflow_name, path, run_started_at, updated_at,
                             actor_login, previous_attempt_url, display_title)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET conclusion=excluded.conclusion,
                                         run_attempt=excluded.run_attempt,
                                         updated_at=excluded.updated_at""",
        (
            run["id"], run.get("run_number"), run.get("run_attempt", 1),
            run["head_sha"], run.get("head_branch"), run.get("event"),
            run.get("conclusion"), pr_number, run.get("created_at"),
            run.get("html_url"), run.get("name"), run.get("path"),
            run.get("run_started_at"), run.get("updated_at"),
            (run.get("actor") or {}).get("login"),
            run.get("previous_attempt_url"), run.get("display_title"),
        ),
    )


def upsert_job(conn, job, parsed):
    conn.execute(
        """INSERT INTO jobs (id, run_id, name, conclusion, test, mode, priv,
                             distro, started_at, completed_at, html_url,
                             run_attempt, runner_name, runner_id, labels,
                             workflow_name, head_sha, check_run_url, status,
                             created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET conclusion=excluded.conclusion,
                                         status=excluded.status,
                                         completed_at=excluded.completed_at""",
        (
            job["id"], job["run_id"], job["name"], job.get("conclusion"),
            parsed.get("test"), parsed.get("mode"), parsed.get("priv"),
            parsed.get("distro"), job.get("started_at"),
            job.get("completed_at"), job.get("html_url"),
            job.get("run_attempt"), job.get("runner_name"), job.get("runner_id"),
            json.dumps(job.get("labels") or []), job.get("workflow_name"),
            job.get("head_sha"), job.get("check_run_url"), job.get("status"),
            job.get("created_at"),
        ),
    )


def add_steps(conn, job_id, steps):
    """Replace this job's steps. Steps are immutable once a job completes, so a
    plain REPLACE keeps re-fetches idempotent without accumulating rows."""
    for s in steps or []:
        if s.get("number") is None:
            continue
        conn.execute(
            """INSERT OR REPLACE INTO job_steps
               (job_id, number, name, status, conclusion, started_at, completed_at)
               VALUES (?,?,?,?,?,?,?)""",
            (job_id, s["number"], s.get("name"), s.get("status"),
             s.get("conclusion"), s.get("started_at"), s.get("completed_at")),
        )


def upsert_artifact(conn, run_id, art, local_path=None):
    conn.execute(
        """INSERT INTO artifacts (id, run_id, name, size_in_bytes, expired,
                                  created_at, expires_at, archive_download_url,
                                  local_path, downloaded_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET expired=excluded.expired,
                                         local_path=COALESCE(excluded.local_path,
                                                             artifacts.local_path),
                                         downloaded_at=COALESCE(excluded.downloaded_at,
                                                                artifacts.downloaded_at)""",
        (
            art["id"], run_id, art["name"], art.get("size_in_bytes"),
            1 if art.get("expired") else 0, art.get("created_at"),
            art.get("expires_at"), art.get("archive_download_url"),
            str(local_path) if local_path else None,
            now() if local_path else None,
        ),
    )


def upsert_job_log(conn, job_id, run_id, path, meta):
    conn.execute(
        """INSERT INTO job_logs (job_id, run_id, path, bytes_raw, bytes_stored,
                                 line_count, first_ts, last_ts, sha256, fetched_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(job_id) DO UPDATE SET
               path=excluded.path, bytes_raw=excluded.bytes_raw,
               bytes_stored=excluded.bytes_stored, line_count=excluded.line_count,
               first_ts=excluded.first_ts, last_ts=excluded.last_ts,
               sha256=excluded.sha256, fetched_at=excluded.fetched_at""",
        (job_id, run_id, str(path), meta.get("bytes_raw"), meta.get("bytes_stored"),
         meta.get("line_count"), meta.get("first_ts"), meta.get("last_ts"),
         meta.get("sha256"), now()),
    )


def add_pr_file(conn, pr_number, f):
    conn.execute(
        """INSERT OR REPLACE INTO pr_files
           (pr_number, filename, status, additions, deletions)
           VALUES (?,?,?,?,?)""",
        (pr_number, f["filename"], f.get("status"),
         f.get("additions"), f.get("deletions")),
    )


def add_fix_commit(conn, sha, issue_number, source, **kw):
    conn.execute(
        """INSERT OR REPLACE INTO fix_commits
           (sha, issue_number, pr_number, message, author, committed_at, source)
           VALUES (?,?,?,?,?,?,?)""",
        (sha, issue_number, kw.get("pr_number"), (kw.get("message") or "")[:4000],
         kw.get("author"), kw.get("committed_at"), source),
    )


def add_issue_event(conn, issue_number, ev):
    """Timeline events are heterogeneous; only a few fields exist on any one."""
    label = (ev.get("label") or {}).get("name")
    src = ev.get("source") or {}
    source_issue = (src.get("issue") or {}).get("number") if src else None
    conn.execute(
        """INSERT OR REPLACE INTO issue_events
           (id, issue_number, event, actor, created_at, label, commit_id, source_issue)
           VALUES (?,?,?,?,?,?,?,?)""",
        (ev.get("id"), issue_number, ev.get("event"),
         (ev.get("actor") or {}).get("login"), ev.get("created_at"),
         label, ev.get("commit_id"), source_issue),
    )


def add_annotation(conn, job_id, ann):
    conn.execute(
        """INSERT OR IGNORE INTO annotations
           (job_id, annotation_level, path, start_line, end_line, title, message)
           VALUES (?,?,?,?,?,?,?)""",
        (job_id, ann.get("annotation_level"), ann.get("path"),
         ann.get("start_line"), ann.get("end_line"), ann.get("title"),
         ann.get("message")),
    )


def upsert_comment(conn, issue_number, comment):
    conn.execute(
        """INSERT INTO issue_comments (id, issue_number, user_login, created_at, body)
           VALUES (?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET body=excluded.body""",
        (comment["id"], issue_number, (comment.get("user") or {}).get("login"),
         comment.get("created_at"), (comment.get("body") or "")[:60000]),
    )


def add_failure(conn, job_id, failure):
    conn.execute(
        """INSERT OR IGNORE INTO test_failures (job_id, fkey, kind, name, text, source)
           VALUES (?,?,?,?,?,?)""",
        (job_id, failure.key(), failure.kind, failure.name, failure.text, failure.source),
    )


def add_evidence(conn, fkey, signal, strength, detail):
    conn.execute(
        """INSERT OR IGNORE INTO flake_evidence (fkey, signal, strength, detail, observed_at)
           VALUES (?,?,?,?,?)""",
        (fkey, signal, strength, detail, now()),
    )


def upsert_issue(conn, issue):
    conn.execute(
        """INSERT INTO known_issues (number, title, state, labels, body, updated_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(number) DO UPDATE SET state=excluded.state,
                                             updated_at=excluded.updated_at""",
        (
            issue["number"], issue["title"], issue.get("state"),
            json.dumps([l["name"] for l in issue.get("labels", [])]),
            (issue.get("body") or "")[:20000], issue.get("updated_at"),
        ),
    )


# -- classification write path -------------------------------------------

def current_classification(conn, fkey):
    return conn.execute(
        "SELECT * FROM classifications WHERE fkey=? AND valid_to IS NULL "
        "ORDER BY id DESC LIMIT 1",
        (fkey,),
    ).fetchone()


def consolidate(conn, fkey, new):
    """Write a classification. Returns one of ADD / UPDATE / INVALIDATE / NOOP.

    ADD        -- nothing believed yet
    NOOP       -- identical verdict; record nothing, keep the original timestamp
    UPDATE     -- verdict changed; close the old row and supersede it
    INVALIDATE -- we previously had a verdict, now we abstain (`unknown`)
    """
    prev = current_classification(conn, fkey)
    ts = now()

    if prev and prev["category"] == new["category"]:
        return "NOOP"

    if prev is None:
        verb = "ADD"
    elif new["category"] == "unknown":
        verb = "INVALIDATE"
    else:
        verb = "UPDATE"

    cur = conn.execute(
        """INSERT INTO classifications
           (fkey, category, confidence, reasoning, evidence, suggested_action,
            duplicate_of, model, backend, tokens_in, tokens_out, valid_from)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            fkey, new["category"], new.get("confidence"), new.get("reasoning"),
            json.dumps(new.get("evidence", [])), new.get("suggested_action"),
            new.get("duplicate_of"), new.get("model"), new.get("backend"),
            new.get("tokens_in"), new.get("tokens_out"), ts,
        ),
    )

    if prev is not None:
        conn.execute(
            "UPDATE classifications SET valid_to=?, superseded_by=? WHERE id=?",
            (ts, cur.lastrowid, prev["id"]),
        )
    return verb


# -- reads ----------------------------------------------------------------

def failure_frequency(conn, limit=50):
    return conn.execute(
        """SELECT f.fkey, f.kind, f.name,
                  COUNT(DISTINCT f.job_id)  AS jobs,
                  COUNT(DISTINCT j.run_id)  AS runs,
                  COUNT(DISTINCT r.head_sha) AS shas
           FROM test_failures f
           JOIN jobs j ON j.id = f.job_id
           JOIN runs r ON r.id = j.run_id
           GROUP BY f.fkey
           ORDER BY runs DESC, jobs DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()


def evidence_for(conn, fkey):
    return conn.execute(
        "SELECT signal, strength, detail FROM flake_evidence WHERE fkey=?", (fkey,)
    ).fetchall()


def sample_text(conn, fkey):
    row = conn.execute(
        "SELECT text FROM test_failures WHERE fkey=? ORDER BY LENGTH(text) DESC LIMIT 1",
        (fkey,),
    ).fetchone()
    return row["text"] if row else ""
