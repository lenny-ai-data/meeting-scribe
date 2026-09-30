"""Accès SQLite : schéma, connexion et fonctions d'accès aux données.

Une connexion par opération : le processus API, le worker et le sous-processus
du pipeline écrivent dans la même base (mode WAL).
"""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import get_settings

ACTIVE_JOB_STATUSES = ("queued", "downloading", "preparing", "transcribing", "aligning", "diarizing")
TERMINAL_JOB_STATUSES = ("completed", "failed", "cancelled")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    status TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0,
    error TEXT,
    title TEXT,
    meeting_date TEXT,
    source_name TEXT,
    source_url TEXT,
    source_path TEXT,
    external_ref TEXT,
    callback_url TEXT,
    callback_status TEXT,
    model TEXT NOT NULL,
    language TEXT NOT NULL,
    device TEXT NOT NULL,
    num_speakers INTEGER,
    min_speakers INTEGER,
    max_speakers INTEGER,
    vocabulary TEXT,
    duration REAL
);

CREATE TABLE IF NOT EXISTS speakers (
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    position INTEGER NOT NULL,
    name TEXT,
    talk_time REAL NOT NULL DEFAULT 0,
    first_start REAL,
    samples TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (job_id, id)
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    summary_id TEXT,
    params TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status, id);

CREATE TABLE IF NOT EXISTS prompts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    content TEXT NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS summaries (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    error TEXT,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_id TEXT,
    prompt_name TEXT,
    system_prompt TEXT NOT NULL,
    meeting_prompt TEXT NOT NULL DEFAULT '',
    think INTEGER NOT NULL DEFAULT 0,
    temperature REAL,
    content TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def connect() -> sqlite3.Connection:
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")


def init_db() -> None:
    from .llm.prompts import DEFAULT_PROMPT_CONTENT, DEFAULT_PROMPT_NAME

    with db() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 0:
            ts = now_iso()
            conn.execute(
                "INSERT INTO prompts (id, name, content, is_default, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?)",
                (new_id(), DEFAULT_PROMPT_NAME, DEFAULT_PROMPT_CONTENT, ts, ts),
            )


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _update(table: str, key: str, value: Any, fields: dict[str, Any]) -> None:
    if not fields:
        return
    cols = ", ".join(f"{name} = ?" for name in fields)
    with db() as conn:
        conn.execute(f"UPDATE {table} SET {cols} WHERE {key} = ?", (*fields.values(), value))


# --- Jobs -------------------------------------------------------------------

def create_job(**fields: Any) -> dict[str, Any]:
    ts = now_iso()
    fields = {"id": new_id(), "created_at": ts, "updated_at": ts, "status": "queued", **fields}
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    with db() as conn:
        conn.execute(f"INSERT INTO jobs ({cols}) VALUES ({marks})", tuple(fields.values()))
    return get_job(fields["id"])


def get_job(job_id: str) -> dict[str, Any] | None:
    with db() as conn:
        return _row(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())


def list_jobs(limit: int = 50, offset: int = 0, status: str | None = None) -> tuple[list[dict], int]:
    where, args = ("WHERE status = ?", (status,)) if status else ("", ())
    with db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM jobs {where}", args).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM jobs {where} ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
            (*args, limit, offset),
        ).fetchall()
    return [dict(r) for r in rows], total


def update_job(job_id: str, **fields: Any) -> None:
    _update("jobs", "id", job_id, {**fields, "updated_at": now_iso()})


def delete_job(job_id: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))


# --- Intervenants -------------------------------------------------------------

def _speaker(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["samples"] = json.loads(data["samples"])
    return data


def get_speakers(job_id: str) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM speakers WHERE job_id = ? ORDER BY position", (job_id,)).fetchall()
    return [_speaker(r) for r in rows]


def replace_speakers(job_id: str, speakers: list[dict[str, Any]]) -> None:
    with transaction() as conn:
        conn.execute("DELETE FROM speakers WHERE job_id = ?", (job_id,))
        for position, sp in enumerate(speakers):
            conn.execute(
                "INSERT INTO speakers (job_id, id, position, name, talk_time, first_start, samples) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job_id, sp["id"], position, sp.get("name"), sp["talk_time"], sp.get("first_start"),
                 json.dumps(sp.get("samples", []), ensure_ascii=False)),
            )


def set_speaker_names(job_id: str, names: dict[str, str | None]) -> None:
    with transaction() as conn:
        for speaker_id, name in names.items():
            conn.execute("UPDATE speakers SET name = ? WHERE job_id = ? AND id = ?", (name, job_id, speaker_id))


def list_people() -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT name, COUNT(DISTINCT job_id) AS jobs FROM speakers WHERE name IS NOT NULL AND name != '' "
            "GROUP BY name ORDER BY jobs DESC, name COLLATE NOCASE"
        ).fetchall()
    return [dict(r) for r in rows]


# --- Tâches (file unique) -------------------------------------------------------

def _task(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    data["params"] = json.loads(data["params"])
    return data


def enqueue_task(kind: str, job_id: str, summary_id: str | None = None, params: dict | None = None) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (kind, job_id, summary_id, params, created_at) VALUES (?, ?, ?, ?, ?)",
            (kind, job_id, summary_id, json.dumps(params or {}), now_iso()),
        )
        return cur.lastrowid


def get_task(task_id: int) -> dict[str, Any] | None:
    with db() as conn:
        return _task(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())


def claim_next_task() -> dict[str, Any] | None:
    with transaction() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE status = 'queued' ORDER BY id LIMIT 1").fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE tasks SET status = 'running', started_at = ?, attempts = attempts + 1 WHERE id = ?",
            (now_iso(), row["id"]),
        )
    return get_task(row["id"])


def finish_task(task_id: int, status: str, error: str | None = None) -> None:
    _update("tasks", "id", task_id, {"status": status, "error": error, "finished_at": now_iso()})


def queued_tasks(job_id: str | None = None) -> list[dict[str, Any]]:
    query = "SELECT * FROM tasks WHERE status = 'queued'"
    args: tuple = ()
    if job_id:
        query += " AND job_id = ?"
        args = (job_id,)
    with db() as conn:
        return [_task(r) for r in conn.execute(query + " ORDER BY id", args).fetchall()]


def queue_position(job_id: str) -> int | None:
    """Position (1 = prochaine) de la première tâche en attente du job, None s'il n'en a pas."""
    with db() as conn:
        row = conn.execute(
            "SELECT id FROM tasks WHERE status = 'queued' AND job_id = ? ORDER BY id LIMIT 1", (job_id,)
        ).fetchone()
        if row is None:
            return None
        return conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'queued' AND id <= ?", (row["id"],)).fetchone()[0]


def recover_interrupted_tasks(max_attempts: int = 2) -> list[dict[str, Any]]:
    """Au démarrage : relance une fois les tâches interrompues, abandonne les autres. Renvoie les abandonnées."""
    abandoned = []
    with transaction() as conn:
        rows = conn.execute("SELECT * FROM tasks WHERE status = 'running'").fetchall()
        for row in rows:
            if row["attempts"] < max_attempts:
                conn.execute("UPDATE tasks SET status = 'queued' WHERE id = ?", (row["id"],))
            else:
                conn.execute(
                    "UPDATE tasks SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
                    ("Interrompue deux fois (redémarrage du service)", now_iso(), row["id"]),
                )
                abandoned.append(_task(row))
    return abandoned


# --- Prompts --------------------------------------------------------------------

def list_prompts() -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM prompts ORDER BY is_default DESC, name COLLATE NOCASE").fetchall()
    return [dict(r) for r in rows]


def get_prompt(prompt_id: str) -> dict[str, Any] | None:
    with db() as conn:
        return _row(conn.execute("SELECT * FROM prompts WHERE id = ?", (prompt_id,)).fetchone())


def get_default_prompt() -> dict[str, Any] | None:
    with db() as conn:
        return _row(conn.execute("SELECT * FROM prompts ORDER BY is_default DESC, created_at LIMIT 1").fetchone())


def save_prompt(prompt_id: str | None, name: str, content: str, is_default: bool) -> dict[str, Any]:
    ts = now_iso()
    with transaction() as conn:
        if is_default:
            conn.execute("UPDATE prompts SET is_default = 0")
        if prompt_id is None:
            prompt_id = new_id()
            conn.execute(
                "INSERT INTO prompts (id, name, content, is_default, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (prompt_id, name, content, int(is_default), ts, ts),
            )
        else:
            conn.execute(
                "UPDATE prompts SET name = ?, content = ?, is_default = MAX(is_default, ?), updated_at = ? WHERE id = ?",
                (name, content, int(is_default), ts, prompt_id),
            )
    return get_prompt(prompt_id)


def delete_prompt(prompt_id: str) -> None:
    with transaction() as conn:
        was_default = conn.execute("SELECT is_default FROM prompts WHERE id = ?", (prompt_id,)).fetchone()
        conn.execute("DELETE FROM prompts WHERE id = ?", (prompt_id,))
        if was_default and was_default[0]:
            conn.execute(
                "UPDATE prompts SET is_default = 1 WHERE id = (SELECT id FROM prompts ORDER BY created_at LIMIT 1)"
            )


def count_prompts() -> int:
    with db() as conn:
        return conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0]


# --- Comptes rendus ----------------------------------------------------------------

def create_summary(**fields: Any) -> dict[str, Any]:
    fields = {"id": new_id(), "created_at": now_iso(), "status": "queued", **fields}
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    with db() as conn:
        conn.execute(f"INSERT INTO summaries ({cols}) VALUES ({marks})", tuple(fields.values()))
    return get_summary(fields["id"])


def get_summary(summary_id: str) -> dict[str, Any] | None:
    with db() as conn:
        return _row(conn.execute("SELECT * FROM summaries WHERE id = ?", (summary_id,)).fetchone())


def list_summaries(job_id: str) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM summaries WHERE job_id = ? ORDER BY created_at DESC, rowid DESC", (job_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def update_summary(summary_id: str, **fields: Any) -> None:
    _update("summaries", "id", summary_id, fields)


def delete_summary(summary_id: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM summaries WHERE id = ?", (summary_id,))
