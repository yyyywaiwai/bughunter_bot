from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class Job:
    id: int
    thread_id: str
    forum_id: str
    repo_path: str
    status: str
    approver_id: Optional[str]
    pr_url: Optional[str]
    worktree_path: Optional[str]
    branch: Optional[str]
    error: Optional[str]
    created_at: str
    updated_at: str


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT UNIQUE NOT NULL,
                forum_id TEXT NOT NULL,
                repo_path TEXT NOT NULL,
                status TEXT NOT NULL,
                approver_id TEXT,
                pr_url TEXT,
                worktree_path TEXT,
                branch TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def create_job(db_path: Path, thread_id: int, forum_id: int, repo_path: str) -> Job:
    now = _utcnow()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO jobs (thread_id, forum_id, repo_path, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (str(thread_id), str(forum_id), repo_path, "pending_approval", now, now),
        )
        conn.commit()
        job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    return get_job(db_path, job_id)


def get_job(db_path: Path, job_id: int) -> Optional[Job]:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_job(row) if row else None


def get_job_by_thread(db_path: Path, thread_id: int) -> Optional[Job]:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT * FROM jobs WHERE thread_id = ?", (str(thread_id),)).fetchone()
    return _row_to_job(row) if row else None


def approve_job(db_path: Path, job_id: int, approver_id: int) -> Optional[Job]:
    now = _utcnow()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = ?, approver_id = ?, updated_at = ?
            WHERE id = ?
            """,
            ("approved", str(approver_id), now, job_id),
        )
        conn.commit()
    return get_job(db_path, job_id)


def update_job_status(
    db_path: Path,
    job_id: int,
    status: str,
    *,
    pr_url: Optional[str] = None,
    worktree_path: Optional[str] = None,
    branch: Optional[str] = None,
    error: Optional[str] = None,
) -> Optional[Job]:
    now = _utcnow()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = ?, pr_url = COALESCE(?, pr_url),
                worktree_path = COALESCE(?, worktree_path),
                branch = COALESCE(?, branch),
                error = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, pr_url, worktree_path, branch, error, now, job_id),
        )
        conn.commit()
    return get_job(db_path, job_id)


def reset_job_for_rerun(db_path: Path, job_id: int, approver_id: int) -> Optional[Job]:
    now = _utcnow()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = ?, approver_id = ?, pr_url = NULL,
                worktree_path = NULL, branch = NULL, error = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            ("approved", str(approver_id), now, job_id),
        )
        conn.commit()
    return get_job(db_path, job_id)


def _row_to_job(row: tuple) -> Job:
    return Job(
        id=row[0],
        thread_id=row[1],
        forum_id=row[2],
        repo_path=row[3],
        status=row[4],
        approver_id=row[5],
        pr_url=row[6],
        worktree_path=row[7],
        branch=row[8],
        error=row[9],
        created_at=row[10],
        updated_at=row[11],
    )
