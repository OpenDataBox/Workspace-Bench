"""
incremental_fetch_pipeline.py — 高并发、可回滚的 RIP 后续文件增量下载系统。

这个版本把旧的 progress.json 流程升级为 SQLite/WAL 状态库 + run 事务目录。
源目录仍然只读；成功文件写入 runs/<run_id>/files，并通过 manifest 交给外部系统消费。
"""

import argparse as _argparse
import datetime as _datetime
import hashlib as _hashlib
import json as _json
import os as _os
import random as _random
import re as _re
import shutil as _shutil
import sqlite3 as _sqlite3
import sys as _sys
import threading as _threading
import time as _time
import uuid as _uuid
from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor
from concurrent.futures import as_completed as _as_completed
from dataclasses import dataclass as _dataclass
from typing import Dict as _Dict
from typing import Iterable as _Iterable
from typing import List as _List
from typing import Optional as _Optional
from typing import Tuple as _Tuple
from urllib.parse import urlparse as _urlparse

import requests as _requests

from pipeline.config import (
    EMPTY_FILE_THRESHOLD as _EMPTY_FILE_THRESHOLD,
    HEADERS as _HEADERS,
    LOGIN_WALL_PATTERNS as _LOGIN_WALL_PATTERNS,
    MAX_FILE_SIZE as _MAX_FILE_SIZE,
    MAX_URLS_PER_FILE as _MAX_URLS_PER_FILE,
    MIME_MAP as _MIME_MAP,
    MIN_FILE_SIZE as _MIN_FILE_SIZE,
    VALID_DOC_EXTENSIONS as _VALID_DOC_EXTENSIONS,
    VALID_DOC_MIMES as _VALID_DOC_MIMES,
)
from pipeline.downloader import (
    compute_file_hash as _compute_file_hash,
    detect_real_type_from_magic as _detect_real_type_from_magic,
    normalize_url as _normalize_url,
)
from pipeline.utils import ThreadSafeSet as _ThreadSafeSet
from pipeline.utils import log_event as _log_event
from pipeline.utils import setup_env as _setup_env


_TARGET_ROOTS = [
    ("chanpin", "/home/weizheng/RIP_final/chanpin_standard"),
    ("yunying", "/home/weizheng/RIP_final/yunying_standard"),
    ("kaifa", "/home/weizheng/RIP_final/kaifa_standard"),
    ("research", "/home/weizheng/RIP_final/research_standard"),
    ("houqin", "/home/weizheng/RIP_final/houqin_standard")
]

_DEFAULT_OUTPUT_DIR = "/home/weizheng/RIP后续文件_增量下载"
_DEFAULT_LLM_CONCURRENCY = 2
_DEFAULT_BRAVE_CONCURRENCY = 4
_DEFAULT_DOWNLOAD_CONCURRENCY = 16
_DEFAULT_VALIDATION_CONCURRENCY = 4
_DEFAULT_MAX_RETRIES = 2

_TARGET_FILE_TYPES = ["pdf", "xlsx", "pptx", "text"]
_FILE_TYPE_EXTENSION_MAP = {
    "pdf": [".pdf"],
    "xlsx": [".xlsx", ".xls"],
    "pptx": [".pptx", ".ppt"],
    "text": [".docx", ".doc", ".txt"],
}
_TEXT_MIME_MAP = {
    "text/plain": ".txt",
    "text/markdown": ".txt",
    "text/csv": ".txt",
    "application/json": ".txt",
    "application/xml": ".txt",
    "text/xml": ".txt",
}
_TEXT_MIMES = set(_TEXT_MIME_MAP)
_MIN_TEXT_FILE_SIZE = 256
_DOWNLOAD_CHUNK_SIZE = 1024 * 64
_REQUEST_TIMEOUT = 30
_HEAD_TIMEOUT = 10

_manifest_lock = _threading.Lock()
_artifact_lock = _threading.Lock()


def _now() -> str:
    return _datetime.datetime.now().isoformat(timespec="seconds")


def _make_run_id() -> str:
    stamp = _datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{stamp}_{_uuid.uuid4().hex[:8]}"


@_dataclass(frozen=True)
class _OutputPaths:
    base_dir: str
    runs_dir: str
    db_file: str
    latest_manifest_file: str


@_dataclass(frozen=True)
class _RunPaths:
    run_id: str
    run_dir: str
    files_dir: str
    tmp_dir: str
    logs_dir: str
    rolled_back_dir: str
    rolled_back_files_dir: str
    manifest_file: str
    rollback_file: str


@_dataclass(frozen=True)
class _Limiters:
    llm: _threading.Semaphore
    brave: _threading.Semaphore
    download: _threading.Semaphore
    validation: _threading.Semaphore


@_dataclass
class _RunContext:
    output_paths: _OutputPaths
    run_paths: _RunPaths
    db: "_PipelineDB"
    limiters: _Limiters
    max_retries: int
    seen_urls: _ThreadSafeSet
    seen_hashes: _ThreadSafeSet


def _build_output_paths(output_dir: str) -> _OutputPaths:
    base_dir = _os.path.abspath(output_dir)
    return _OutputPaths(
        base_dir=base_dir,
        runs_dir=_os.path.join(base_dir, "runs"),
        db_file=_os.path.join(base_dir, "state.db"),
        latest_manifest_file=_os.path.join(base_dir, "latest_manifest.jsonl"),
    )


def _build_run_paths(output_paths: _OutputPaths, run_id: str) -> _RunPaths:
    run_dir = _os.path.join(output_paths.runs_dir, run_id)
    rolled_back_dir = _os.path.join(run_dir, "rolled_back")
    return _RunPaths(
        run_id=run_id,
        run_dir=run_dir,
        files_dir=_os.path.join(run_dir, "files"),
        tmp_dir=_os.path.join(run_dir, "tmp"),
        logs_dir=_os.path.join(run_dir, "logs"),
        rolled_back_dir=rolled_back_dir,
        rolled_back_files_dir=_os.path.join(rolled_back_dir, "files"),
        manifest_file=_os.path.join(run_dir, "manifest.jsonl"),
        rollback_file=_os.path.join(run_dir, "rollback.jsonl"),
    )


def _setup_output(output_paths: _OutputPaths) -> None:
    _setup_env(output_paths.base_dir, output_paths.runs_dir)


def _setup_run_output(run_paths: _RunPaths) -> None:
    _setup_env(
        run_paths.run_dir,
        run_paths.files_dir,
        run_paths.tmp_dir,
        run_paths.logs_dir,
        run_paths.rolled_back_dir,
        run_paths.rolled_back_files_dir,
    )


class _PipelineDB:
    def __init__(self, db_file: str):
        self.db_file = db_file
        self._lock = _threading.RLock()

    def _connect(self):
        conn = _sqlite3.connect(self.db_file, timeout=30, check_same_thread=False)
        conn.row_factory = _sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    output_dir TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    rollback_of TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    target_path TEXT NOT NULL,
                    target_root TEXT NOT NULL,
                    target_root_name TEXT NOT NULL,
                    relative_target_path TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    stem TEXT NOT NULL,
                    ext TEXT NOT NULL,
                    file_type TEXT,
                    parent_hint TEXT,
                    leaf_name TEXT,
                    size INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    staged_artifact_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(run_id, target_path),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS candidates (
                    candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    task_id INTEGER NOT NULL,
                    url TEXT NOT NULL,
                    normalized_url TEXT NOT NULL,
                    title TEXT,
                    source TEXT,
                    reason TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(run_id, task_id, normalized_url),
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id)
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    task_id INTEGER NOT NULL,
                    target_path TEXT NOT NULL,
                    staged_file_path TEXT NOT NULL,
                    rollback_path TEXT,
                    sha256 TEXT,
                    source_url TEXT,
                    source_title TEXT,
                    detected_ext TEXT,
                    content_type TEXT,
                    validation_result TEXT,
                    manifest_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    rolled_back_at TEXT,
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id)
                );

                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    task_id INTEGER,
                    event_type TEXT NOT NULL,
                    message TEXT,
                    data_json TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_run_status
                    ON tasks(run_id, status, attempts);
                CREATE INDEX IF NOT EXISTS idx_artifacts_status_target
                    ON artifacts(status, target_path, artifact_id);
                CREATE INDEX IF NOT EXISTS idx_events_run_task
                    ON events(run_id, task_id, event_id);
                """
            )

    def create_run(self, run_id: str, output_dir: str, config: _Dict) -> None:
        ts = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, status, created_at, started_at, output_dir,
                    config_json, updated_at
                ) VALUES (?, 'running', ?, ?, ?, ?, ?)
                """,
                (run_id, ts, ts, output_dir, _json.dumps(config, ensure_ascii=False), ts),
            )
        self.add_event(run_id, None, "run_created", "run created", config)

    def mark_run_status(self, run_id: str, status: str) -> None:
        ts = _now()
        finished_at = ts if status in ("completed", "completed_with_failures", "rolled_back") else None
        with self._lock, self._connect() as conn:
            if finished_at:
                conn.execute(
                    "UPDATE runs SET status=?, finished_at=?, updated_at=? WHERE run_id=?",
                    (status, finished_at, ts, run_id),
                )
            else:
                conn.execute(
                    "UPDATE runs SET status=?, updated_at=? WHERE run_id=?",
                    (status, ts, run_id),
                )
        self.add_event(run_id, None, "run_status", status)

    def run_exists(self, run_id: str) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return row is not None

    def latest_run_with_active_artifacts(self) -> _Optional[str]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT r.run_id
                FROM runs r
                JOIN artifacts a ON a.run_id = r.run_id
                WHERE a.status='active' AND r.status != 'rolled_back'
                GROUP BY r.run_id
                ORDER BY r.created_at DESC
                LIMIT 1
                """
            ).fetchone()
        return row["run_id"] if row else None

    def upsert_tasks(self, run_id: str, empty_files: _List[_Dict]) -> int:
        ts = _now()
        inserted = 0
        with self._lock, self._connect() as conn:
            for item in empty_files:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO tasks (
                        run_id, target_path, target_root, target_root_name,
                        relative_target_path, filename, stem, ext, file_type,
                        parent_hint, leaf_name, size, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        run_id,
                        item["path"],
                        item["target_root"],
                        item["target_root_name"],
                        item["relative_target_path"],
                        item["filename"],
                        item["stem"],
                        item["ext"],
                        item.get("type"),
                        item.get("parent_hint", ""),
                        item.get("leaf_name", ""),
                        item["size"],
                        ts,
                        ts,
                    ),
                )
                inserted += cur.rowcount
        self.add_event(run_id, None, "tasks_upserted", f"{inserted} new tasks")
        return inserted

    def reset_interrupted_tasks(self, run_id: str) -> int:
        ts = _now()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE tasks
                SET status='pending',
                    last_error='reset interrupted task on resume',
                    updated_at=?
                WHERE run_id=?
                  AND status IN ('planning', 'searching', 'downloading', 'validating')
                """,
                (ts, run_id),
            )
        if cur.rowcount:
            self.add_event(run_id, None, "tasks_reset", f"{cur.rowcount} interrupted tasks reset")
        return cur.rowcount

    def fetch_schedulable_tasks(
        self,
        run_id: str,
        max_retries: int,
        limit: _Optional[int] = None,
    ) -> _List[_Dict]:
        sql = """
            SELECT *
            FROM tasks
            WHERE run_id=?
              AND status IN ('pending', 'retryable_failed')
              AND attempts < ?
            ORDER BY task_id
        """
        params = [run_id, max_retries]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def start_task_attempt(self, task_id: int, status: str) -> int:
        ts = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status=?, attempts=attempts + 1, last_error=NULL, updated_at=?
                WHERE task_id=?
                """,
                (status, ts, task_id),
            )
            row = conn.execute("SELECT attempts FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return int(row["attempts"])

    def set_task_status(self, task_id: int, status: str, message: _Optional[str] = None) -> None:
        ts = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET status=?, last_error=?, updated_at=? WHERE task_id=?",
                (status, message, ts, task_id),
            )

    def fail_task(self, task: _Dict, max_retries: int, message: str) -> str:
        status = "retryable_failed" if int(task["attempts"]) < max_retries else "exhausted"
        ts = _now()
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT attempts FROM tasks WHERE task_id=?", (task["task_id"],)).fetchone()
            attempts = int(row["attempts"]) if row else int(task["attempts"])
            status = "retryable_failed" if attempts < max_retries else "exhausted"
            conn.execute(
                "UPDATE tasks SET status=?, last_error=?, updated_at=? WHERE task_id=?",
                (status, message[:500], ts, task["task_id"]),
            )
        self.add_event(task["run_id"], task["task_id"], "task_failed", message, {"status": status})
        return status

    def insert_candidates(self, run_id: str, task_id: int, candidates: _List[_Dict]) -> None:
        ts = _now()
        with self._lock, self._connect() as conn:
            for item in candidates:
                url = (item.get("url") or "").strip()
                if not url:
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO candidates (
                        run_id, task_id, url, normalized_url, title, source,
                        reason, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'discovered', ?, ?)
                    """,
                    (
                        run_id,
                        task_id,
                        url,
                        _normalize_url(url),
                        item.get("title", ""),
                        item.get("source", "brave_search"),
                        item.get("reason", ""),
                        ts,
                        ts,
                    ),
                )

    def update_candidate_status(
        self,
        run_id: str,
        task_id: int,
        url: str,
        status: str,
        error: _Optional[str] = None,
    ) -> None:
        ts = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE candidates
                SET status=?, error=?, updated_at=?
                WHERE run_id=? AND task_id=? AND normalized_url=?
                """,
                (status, (error or "")[:500], ts, run_id, task_id, _normalize_url(url)),
            )

    def insert_artifact(self, task: _Dict, record: _Dict) -> _Dict:
        ts = _now()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO artifacts (
                    run_id, task_id, target_path, staged_file_path, sha256,
                    source_url, source_title, detected_ext, content_type,
                    validation_result, manifest_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 'active', ?)
                """,
                (
                    task["run_id"],
                    task["task_id"],
                    record["target_path"],
                    record["staged_file_path"],
                    record.get("sha256"),
                    record.get("source_url"),
                    record.get("source_title"),
                    record.get("detected_ext"),
                    record.get("content_type"),
                    record.get("validation_result"),
                    ts,
                ),
            )
            artifact_id = cur.lastrowid
            record = dict(record)
            record["artifact_id"] = artifact_id
            record["run_id"] = task["run_id"]
            record["task_id"] = task["task_id"]
            manifest_json = _json.dumps(record, ensure_ascii=False, sort_keys=True)
            conn.execute(
                """
                UPDATE artifacts
                SET manifest_json=?
                WHERE artifact_id=?
                """,
                (manifest_json, artifact_id),
            )
            conn.execute(
                """
                UPDATE tasks
                SET status='staged',
                    staged_artifact_id=?,
                    last_error=NULL,
                    updated_at=?
                WHERE task_id=?
                """,
                (artifact_id, ts, task["task_id"]),
            )
        self.add_event(task["run_id"], task["task_id"], "artifact_staged", record["staged_file_path"])
        return record

    def active_artifact_records_for_latest(self) -> _List[str]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT a.manifest_json
                FROM artifacts a
                JOIN (
                    SELECT target_path, MAX(artifact_id) AS artifact_id
                    FROM artifacts
                    WHERE status='active'
                    GROUP BY target_path
                ) latest ON latest.artifact_id = a.artifact_id
                JOIN runs r ON r.run_id = a.run_id
                WHERE r.status != 'rolled_back'
                ORDER BY a.artifact_id
                """
            ).fetchall()
        return [row["manifest_json"] for row in rows if row["manifest_json"]]

    def active_artifact_records_for_run(self, run_id: str) -> _List[str]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT manifest_json
                FROM artifacts
                WHERE run_id=? AND status='active'
                ORDER BY artifact_id
                """,
                (run_id,),
            ).fetchall()
        return [row["manifest_json"] for row in rows if row["manifest_json"]]

    def active_artifacts_for_run(self, run_id: str) -> _List[_Dict]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM artifacts
                WHERE run_id=? AND status='active'
                ORDER BY artifact_id DESC
                """,
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def active_hashes(self) -> _List[str]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT sha256 FROM artifacts WHERE status='active' AND sha256 IS NOT NULL"
            ).fetchall()
        return [row["sha256"] for row in rows]

    def seen_urls_for_run(self, run_id: str) -> _List[str]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT normalized_url FROM candidates WHERE run_id=?",
                (run_id,),
            ).fetchall()
        return [row["normalized_url"] for row in rows]

    def mark_artifact_rolled_back(self, artifact_id: int, rollback_path: _Optional[str]) -> None:
        ts = _now()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT task_id, run_id FROM artifacts WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
            conn.execute(
                """
                UPDATE artifacts
                SET status='rolled_back', rollback_path=?, rolled_back_at=?
                WHERE artifact_id=?
                """,
                (rollback_path, ts, artifact_id),
            )
            if row:
                conn.execute(
                    """
                    UPDATE tasks
                    SET status='rolled_back', updated_at=?
                    WHERE task_id=? AND status='staged'
                    """,
                    (ts, row["task_id"]),
                )

    def task_status_counts(self, run_id: _Optional[str] = None) -> _Dict[str, int]:
        if run_id:
            sql = "SELECT status, COUNT(*) AS n FROM tasks WHERE run_id=? GROUP BY status"
            params = (run_id,)
        else:
            sql = "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
            params = ()
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return {row["status"]: int(row["n"]) for row in rows}

    def artifact_status_counts(self, run_id: _Optional[str] = None) -> _Dict[str, int]:
        if run_id:
            sql = "SELECT status, COUNT(*) AS n FROM artifacts WHERE run_id=? GROUP BY status"
            params = (run_id,)
        else:
            sql = "SELECT status, COUNT(*) AS n FROM artifacts GROUP BY status"
            params = ()
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return {row["status"]: int(row["n"]) for row in rows}

    def list_runs(self, run_id: _Optional[str] = None) -> _List[_Dict]:
        if run_id:
            sql = "SELECT * FROM runs WHERE run_id=? ORDER BY created_at DESC"
            params = (run_id,)
        else:
            sql = "SELECT * FROM runs ORDER BY created_at DESC LIMIT 20"
            params = ()
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def add_event(
        self,
        run_id: str,
        task_id: _Optional[int],
        event_type: str,
        message: str,
        data: _Optional[_Dict] = None,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO events (run_id, task_id, event_type, message, data_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    task_id,
                    event_type,
                    message[:1000] if message else "",
                    _json.dumps(data or {}, ensure_ascii=False),
                    _now(),
                ),
            )


def _target_extensions() -> set:
    exts = set()
    for file_type in _TARGET_FILE_TYPES:
        exts.update(_FILE_TYPE_EXTENSION_MAP.get(file_type, []))
    return exts


def _file_type_for_ext(ext: str) -> _Optional[str]:
    for file_type, exts in _FILE_TYPE_EXTENSION_MAP.items():
        if ext in exts:
            return file_type
    return None


def _scan_empty_files() -> _List[_Dict]:
    target_exts = _target_extensions()
    results: _List[_Dict] = []

    for root_name, root_path in _TARGET_ROOTS:
        if not _os.path.isdir(root_path):
            print(f"[警告] 目标根目录不存在，跳过: {root_path}")
            continue

        for current_dir, dirnames, filenames in _os.walk(root_path):
            dirnames[:] = [d for d in dirnames if d != "__MACOSX"]
            for filename in filenames:
                file_path = _os.path.join(current_dir, filename)
                stem, ext = _os.path.splitext(filename)
                ext_lower = ext.lower()
                if ext_lower not in target_exts:
                    continue

                try:
                    size = _os.path.getsize(file_path)
                except OSError:
                    continue
                if size > _EMPTY_FILE_THRESHOLD:
                    continue

                rel_path = _os.path.relpath(file_path, root_path)
                rel_dir = _os.path.dirname(rel_path)
                folder_path = _os.path.dirname(file_path)
                leaf_name = _os.path.basename(folder_path)
                parent_hint = ""
                if rel_dir and rel_dir != ".":
                    rel_parts = rel_dir.split(_os.sep)
                    if len(rel_parts) > 1:
                        parent_hint = "/".join(rel_parts[:-1])

                results.append({
                    "target_root_name": root_name,
                    "target_root": root_path,
                    "path": file_path,
                    "relative_target_path": rel_path,
                    "folder_path": folder_path,
                    "filename": filename,
                    "stem": stem,
                    "ext": ext_lower,
                    "type": _file_type_for_ext(ext_lower),
                    "size": size,
                    "parent_hint": parent_hint,
                    "leaf_name": leaf_name,
                })

    results.sort(key=lambda item: (item["target_root_name"], item["relative_target_path"].lower()))
    return results


def _print_dry_run(empty_files: _List[_Dict]) -> None:
    counts = {name: 0 for name, _ in _TARGET_ROOTS}
    for item in empty_files:
        counts[item["target_root_name"]] += 1

    print("Dry run: 只扫描，不下载、不写输出目录。")
    print(f"空文件总数: {len(empty_files)}")
    for root_name, root_path in _TARGET_ROOTS:
        print(f"  {root_name}: {counts[root_name]} ({root_path})")

    if empty_files:
        print("\n待处理路径:")
        for item in empty_files:
            print(f"  - {item['path']}")


def _target_family(ext: str) -> str:
    normalized = (ext or "").lower().lstrip(".")
    if normalized == "pdf":
        return "pdf"
    if normalized in ("xlsx", "xls"):
        return "xlsx"
    if normalized in ("pptx", "ppt"):
        return "pptx"
    if normalized in ("docx", "doc", "txt"):
        return "text"
    return normalized


def _is_text_family(ext: str) -> bool:
    return _target_family(ext) == "text"


def _is_text_mime(content_type: str) -> bool:
    ct = (content_type or "").split(";")[0].strip().lower()
    return ct in _TEXT_MIMES or ct.startswith("text/")


def _min_size_for_target(ext: str) -> int:
    return _MIN_TEXT_FILE_SIZE if _is_text_family(ext) else _MIN_FILE_SIZE


def _extensions_compatible(target_ext: str, detected_ext: str) -> bool:
    target = (target_ext or "").lower()
    detected = (detected_ext or "").lower()
    if target == ".pdf":
        return detected == ".pdf"
    if target in (".xlsx", ".xls"):
        return detected in (".xlsx", ".xls")
    if target in (".pptx", ".ppt"):
        return detected in (".pptx", ".ppt")
    if target in (".docx", ".doc", ".txt"):
        return detected in (".docx", ".doc", ".txt")
    return target == detected


def _content_type_allowed_for_target(content_type: str, target_ext: str, url: str) -> bool:
    ct = (content_type or "").split(";")[0].strip().lower()
    if not ct:
        return True
    if "text/html" in ct:
        return False
    if _is_text_mime(ct):
        return _is_text_family(target_ext)
    if ct in _VALID_DOC_MIMES:
        return True
    path_ext = _os.path.splitext(_urlparse(url).path)[1].lower()
    if path_ext in _VALID_DOC_EXTENSIONS:
        return _extensions_compatible(target_ext, path_ext)
    return False


def _pre_check_url_incremental(
    url: str,
    target_ext: str,
    seen_urls: _ThreadSafeSet,
    log_file: str,
) -> _Tuple[bool, str, str]:
    norm_url = _normalize_url(url)
    if not seen_urls.try_add(norm_url):
        return False, "URL 重复，跳过。", url

    try:
        response = _requests.head(
            url,
            headers=_HEADERS,
            timeout=_HEAD_TIMEOUT,
            allow_redirects=True,
        )
        response.raise_for_status()
    except _requests.exceptions.TooManyRedirects:
        return False, "重定向次数过多。", url
    except _requests.exceptions.HTTPError as e:
        return False, f"HTTP {e.response.status_code}", url
    except Exception as e:
        _log_event(f"      [预检] HEAD 失败 ({str(e)[:50]})，放行。", log_file)
        return True, "HEAD 失败但放行", url

    final_url = response.url
    for pattern in _LOGIN_WALL_PATTERNS:
        if pattern in final_url.lower():
            return False, f"重定向到登录页 ({pattern})。", final_url

    norm_final = _normalize_url(final_url)
    if norm_final != norm_url and not seen_urls.try_add(norm_final):
        return False, "重定向后 URL 重复。", final_url

    content_type = response.headers.get("Content-Type", "")
    if not _content_type_allowed_for_target(content_type, target_ext, final_url):
        return False, f"Content-Type={content_type} 与目标类型不兼容。", final_url

    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            size = int(content_length)
            if size < _min_size_for_target(target_ext):
                return False, f"文件太小 ({size/1024:.1f}KB)。", final_url
            if size > _MAX_FILE_SIZE:
                return False, f"文件过大 ({size/1024/1024:.1f}MB)。", final_url
        except ValueError:
            pass

    return True, "预检通过", final_url


def _extension_from_response(response: _requests.Response, url: str, target_ext: str) -> str:
    content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
    if content_type in _TEXT_MIME_MAP and _is_text_family(target_ext):
        return _TEXT_MIME_MAP[content_type]
    mapped = _MIME_MAP.get(content_type)
    if mapped:
        return mapped
    path_ext = _os.path.splitext(_urlparse(url).path)[1].lower()
    if path_ext in _VALID_DOC_EXTENSIONS:
        return path_ext
    return target_ext if target_ext in _VALID_DOC_EXTENSIONS else ".bin"


def _download_candidate(
    url: str,
    target_ext: str,
    run_paths: _RunPaths,
    task_id: int,
) -> _Tuple[bool, str, _Optional[str], _Optional[str]]:
    tmp_path = None
    try:
        response = _requests.get(
            url,
            headers=_HEADERS,
            timeout=_REQUEST_TIMEOUT,
            stream=True,
            allow_redirects=True,
        )
        response.raise_for_status()

        final_url = response.url
        for pattern in _LOGIN_WALL_PATTERNS:
            if pattern in final_url.lower():
                return False, f"重定向到登录页 ({pattern})。", final_url, None

        content_type = response.headers.get("Content-Type", "")
        if not _content_type_allowed_for_target(content_type, target_ext, final_url):
            return False, f"检测到不兼容内容 ({content_type})。", final_url, content_type

        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                size = int(content_length)
                if size < _min_size_for_target(target_ext):
                    return False, f"文件太小 ({size/1024:.1f}KB)。", final_url, content_type
            except ValueError:
                pass

        ext = _extension_from_response(response, final_url, target_ext)
        tid = _threading.current_thread().ident
        stamp = f"{_time.time():.6f}".replace(".", "")
        tmp_path = _os.path.join(run_paths.tmp_dir, f"download_{task_id}_{tid}_{stamp}{ext}")

        with open(tmp_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=_DOWNLOAD_CHUNK_SIZE):
                if chunk:
                    f.write(chunk)

        actual_size = _os.path.getsize(tmp_path)
        if actual_size < _min_size_for_target(target_ext):
            _remove_quietly(tmp_path)
            return False, f"实际文件太小 ({actual_size/1024:.1f}KB)，已删除。", final_url, content_type

        return True, tmp_path, final_url, content_type
    except Exception as e:
        _remove_quietly(tmp_path)
        return False, f"连接异常: {str(e)[:100]}", None, None


def _looks_like_plain_text(file_path: str) -> bool:
    try:
        with open(file_path, "rb") as f:
            raw = f.read(32 * 1024)
    except OSError:
        return False

    if not raw or b"\x00" in raw:
        return False

    decoded = ""
    for encoding in ("utf-8", "gb18030", "latin-1"):
        try:
            decoded = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if not decoded:
        return False

    stripped = decoded.lstrip("\ufeff").strip().lower()
    if stripped.startswith(("<!doctype", "<html", "<head")) or "<html" in stripped[:300]:
        return False
    printable = sum(1 for c in decoded if c.isprintable() or c in "\n\r\t")
    return printable / max(len(decoded), 1) > 0.85


def _verify_downloaded_file(
    file_path: str,
    target_ext: str,
    seen_hashes: _ThreadSafeSet,
    log_file: str,
) -> _Tuple[bool, str, _Optional[str], _Optional[str]]:
    detected_ext = _detect_real_type_from_magic(file_path)

    if detected_ext == ".html":
        if _is_text_family(target_ext) and _looks_like_plain_text(file_path):
            detected_ext = ".txt"
        else:
            _remove_quietly(file_path)
            return False, "内容实际是 HTML，已删除。", None, None

    if detected_ext == ".zip":
        _remove_quietly(file_path)
        return False, "普通 ZIP 而非 Office 文档，已删除。", None, None

    if detected_ext == ".ole":
        if target_ext in (".doc", ".docx"):
            detected_ext = ".doc"
        elif target_ext in (".xls", ".xlsx"):
            detected_ext = ".xls"
        elif target_ext in (".ppt", ".pptx"):
            detected_ext = ".ppt"

    if not detected_ext and _looks_like_plain_text(file_path):
        detected_ext = ".txt"

    if not detected_ext:
        current_ext = _os.path.splitext(file_path)[1].lower()
        if current_ext in _VALID_DOC_EXTENSIONS:
            detected_ext = current_ext

    if not detected_ext:
        _remove_quietly(file_path)
        return False, "无法识别真实文件类型，已删除。", None, None

    if not _extensions_compatible(target_ext, detected_ext):
        _remove_quietly(file_path)
        return False, f"类型不兼容: 目标 {target_ext}, 实际 {detected_ext}，已删除。", None, None

    file_hash = _compute_file_hash(file_path)
    if file_hash and not seen_hashes.try_add(file_hash):
        _remove_quietly(file_path)
        return False, f"内容哈希重复 ({file_hash[:16]}...)，已删除。", None, None

    _log_event(f"      [技术验证] 通过，实际类型 {detected_ext}", log_file)
    return True, "后验证通过", detected_ext, file_hash


def _sanitize_filename_part(value: str, max_len: int = 90) -> str:
    cleaned = _re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value or "")
    cleaned = _re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:max_len] or "downloaded"


def _stage_filename(root_name: str, relative_path: str, detected_ext: str) -> str:
    key = f"{root_name}/{relative_path}".replace(_os.sep, "/")
    digest = _hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    stem = _os.path.splitext(_os.path.basename(relative_path))[0]
    safe_stem = _sanitize_filename_part(stem, max_len=70)
    ext = detected_ext if detected_ext.startswith(".") else f".{detected_ext}"
    return f"{root_name}__{digest}__{safe_stem}{ext}"


def _unique_path(path: str) -> str:
    if not _os.path.exists(path):
        return path
    base, ext = _os.path.splitext(path)
    counter = 1
    while True:
        candidate = f"{base}_{counter}{ext}"
        if not _os.path.exists(candidate):
            return candidate
        counter += 1


def _stage_verified_file(file_path: str, task: _Dict, detected_ext: str, run_paths: _RunPaths) -> str:
    with _artifact_lock:
        filename = _stage_filename(task["target_root_name"], task["relative_target_path"], detected_ext)
        dest_path = _unique_path(_os.path.join(run_paths.files_dir, filename))
        _shutil.move(file_path, dest_path)
        return dest_path


def _remove_quietly(path: _Optional[str]) -> None:
    if not path:
        return
    try:
        if _os.path.exists(path):
            _os.remove(path)
    except OSError:
        pass


def _write_jsonl(path: str, records: _Iterable[str]) -> None:
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        for record in records:
            if record:
                f.write(record.rstrip("\n") + "\n")
    _os.replace(tmp_path, path)


def _append_jsonl(path: str, record: _Dict) -> None:
    line = _json.dumps(record, ensure_ascii=False, sort_keys=True)
    with _manifest_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def _rebuild_latest_manifest(db: _PipelineDB, output_paths: _OutputPaths) -> None:
    with _manifest_lock:
        _write_jsonl(output_paths.latest_manifest_file, db.active_artifact_records_for_latest())


def _rebuild_run_manifest(db: _PipelineDB, run_paths: _RunPaths) -> None:
    with _manifest_lock:
        _write_jsonl(run_paths.manifest_file, db.active_artifact_records_for_run(run_paths.run_id))


def _process_task(task: _Dict, ctx: _RunContext) -> None:
    db = ctx.db
    run_paths = ctx.run_paths
    log_file = _os.path.join(run_paths.logs_dir, f"{task['target_root_name']}.txt")
    target_ext = task["ext"]

    attempt = db.start_task_attempt(task["task_id"], "planning")
    task["attempts"] = attempt

    _log_event("", log_file)
    _log_event(
        f"  [task {task['task_id']} attempt {attempt}] 处理: {task['target_path']} ({task['size']}B)",
        log_file,
    )

    try:
        from pipeline.llm_agent import (
            build_instruction,
            call_llm_for_search_plan,
            search_downloadable_url_candidates,
        )
        from pipeline.validator import validate_content_relevance
    except Exception as e:
        message = f"无法加载 LLM 组件: {str(e)[:120]}"
        _log_event(f"    [初始化失败] {message}", log_file)
        db.fail_task(task, ctx.max_retries, message)
        return

    try:
        with ctx.limiters.llm:
            instruction = build_instruction(task, task.get("parent_hint", ""), task.get("leaf_name", ""))
            _log_event("    [Step 1] Instruction 已构造", log_file)
            _log_event("    [Step 2] 调用 LLM 生成 search plan...", log_file)
            search_plan = call_llm_for_search_plan(instruction, log_file)
    except Exception as e:
        message = f"LLM search plan 异常: {str(e)[:160]}"
        _log_event(f"    [Step 2] {message}", log_file)
        db.fail_task(task, ctx.max_retries, message)
        return

    queries = search_plan.get("queries", []) if isinstance(search_plan, dict) else []
    if not queries:
        message = "LLM 未返回有效 search plan"
        _log_event(f"    [Step 2] {message}", log_file)
        db.fail_task(task, ctx.max_retries, message)
        return

    for query_idx, query in enumerate(queries[:5], 1):
        _log_event(f"      [Query {query_idx}] {query}", log_file)

    db.set_task_status(task["task_id"], "searching")
    context = task.get("leaf_name", "")
    if task.get("parent_hint"):
        context = f"{task['parent_hint']}/{context}"

    try:
        with ctx.limiters.brave:
            url_candidates = search_downloadable_url_candidates(
                filename=task["stem"],
                ext=target_ext,
                context=context,
                max_results=_MAX_URLS_PER_FILE,
                llm_queries=queries,
            )
    except Exception as e:
        message = f"Brave Search 异常: {str(e)[:160]}"
        _log_event(f"    [Step 3] {message}", log_file)
        db.fail_task(task, ctx.max_retries, message)
        return

    db.insert_candidates(task["run_id"], task["task_id"], url_candidates)
    if not url_candidates:
        message = "Brave Search 未返回 URL"
        _log_event(f"    [Step 3] {message}", log_file)
        db.fail_task(task, ctx.max_retries, message)
        return

    _log_event(f"    [Step 3] 获得 {len(url_candidates)} 个候选", log_file)

    for url_idx, item in enumerate(url_candidates, 1):
        url = (item.get("url") or "").strip()
        title = item.get("title", "")
        if not url.startswith("http"):
            continue

        _log_event(f"    [{url_idx}/{len(url_candidates)}] {title or url[:80]}", log_file)
        db.set_task_status(task["task_id"], "downloading")
        db.update_candidate_status(task["run_id"], task["task_id"], url, "checking")

        with ctx.limiters.download:
            passed, reason, final_url = _pre_check_url_incremental(
                url,
                target_ext,
                ctx.seen_urls,
                log_file,
            )

        if not passed:
            db.update_candidate_status(task["run_id"], task["task_id"], url, "precheck_rejected", reason)
            _log_event(f"      [预检拒绝] {reason}", log_file)
            _time.sleep(0.1)
            continue

        with ctx.limiters.download:
            download_ok, download_info, downloaded_url, content_type = _download_candidate(
                final_url,
                target_ext,
                run_paths,
                task["task_id"],
            )

        if not download_ok:
            db.update_candidate_status(task["run_id"], task["task_id"], url, "download_failed", download_info)
            _log_event(f"      [下载失败] {download_info}", log_file)
            _time.sleep(0.2)
            continue

        tmp_path = download_info
        _log_event(f"      [下载成功] {_os.path.basename(tmp_path)}", log_file)

        tech_ok, tech_reason, detected_ext, file_hash = _verify_downloaded_file(
            tmp_path,
            target_ext,
            ctx.seen_hashes,
            log_file,
        )
        if not tech_ok:
            db.update_candidate_status(task["run_id"], task["task_id"], url, "tech_rejected", tech_reason)
            _log_event(f"      [技术验证拒绝] {tech_reason}", log_file)
            _time.sleep(0.1)
            continue

        db.set_task_status(task["task_id"], "validating")
        with ctx.limiters.validation:
            is_relevant, relevance_reason = validate_content_relevance(
                tmp_path,
                task["stem"],
                log_file,
            )

        if not is_relevant:
            _remove_quietly(tmp_path)
            db.update_candidate_status(task["run_id"], task["task_id"], url, "llm_rejected", relevance_reason)
            _log_event(f"      [内容不相关] {relevance_reason}", log_file)
            _time.sleep(0.1)
            continue

        staged_path = _stage_verified_file(tmp_path, task, detected_ext, run_paths)
        record = {
            "target_path": task["target_path"],
            "staged_file_path": staged_path,
            "target_root": task["target_root"],
            "target_root_name": task["target_root_name"],
            "relative_target_path": task["relative_target_path"],
            "source_url": downloaded_url or final_url,
            "source_title": title,
            "detected_ext": detected_ext,
            "sha256": file_hash,
            "content_type": content_type,
            "validation_result": f"relevant: {relevance_reason}",
            "validated_at": _now(),
        }
        record = db.insert_artifact(task, record)
        _append_jsonl(run_paths.manifest_file, record)
        _rebuild_latest_manifest(db, ctx.output_paths)
        db.update_candidate_status(task["run_id"], task["task_id"], url, "staged")
        _log_event(f"    【增量保存成功】{task['target_path']} -> {staged_path}", log_file)
        _time.sleep(_random.uniform(0.2, 0.6))
        return

    message = "候选全部失败，未找到替代"
    _log_event(f"    【未找到替代】{task['target_path']}", log_file)
    db.fail_task(task, ctx.max_retries, message)


def _run_status_from_task_counts(counts: _Dict[str, int]) -> str:
    if counts.get("pending") or counts.get("retryable_failed"):
        return "running"
    if counts.get("exhausted"):
        return "completed_with_failures"
    return "completed"


def _cmd_run(args: _argparse.Namespace) -> None:
    output_paths = _build_output_paths(args.output_dir)
    _setup_output(output_paths)

    db = _PipelineDB(output_paths.db_file)
    db.init_schema()

    empty_files = _scan_empty_files()
    if args.limit is not None:
        empty_files = empty_files[:args.limit]

    if args.resume_run_id:
        run_id = args.resume_run_id
        if not db.run_exists(run_id):
            raise SystemExit(f"找不到 run_id: {run_id}")
        db.mark_run_status(run_id, "running")
        print(f"恢复 run: {run_id}")
    else:
        run_id = _make_run_id()
        config = {
            "llm_concurrency": args.llm_concurrency,
            "brave_concurrency": args.brave_concurrency,
            "download_concurrency": args.download_concurrency,
            "validation_concurrency": args.validation_concurrency,
            "max_retries": args.max_retries,
            "limit": args.limit,
        }
        db.create_run(run_id, output_paths.base_dir, config)
        print(f"创建 run: {run_id}")

    run_paths = _build_run_paths(output_paths, run_id)
    _setup_run_output(run_paths)

    inserted = db.upsert_tasks(run_id, empty_files)
    reset_count = db.reset_interrupted_tasks(run_id)

    limiters = _Limiters(
        llm=_threading.Semaphore(args.llm_concurrency),
        brave=_threading.Semaphore(args.brave_concurrency),
        download=_threading.Semaphore(args.download_concurrency),
        validation=_threading.Semaphore(args.validation_concurrency),
    )
    ctx = _RunContext(
        output_paths=output_paths,
        run_paths=run_paths,
        db=db,
        limiters=limiters,
        max_retries=args.max_retries,
        seen_urls=_ThreadSafeSet(db.seen_urls_for_run(run_id)),
        seen_hashes=_ThreadSafeSet(db.active_hashes()),
    )

    task_workers = args.task_workers or max(
        args.download_concurrency,
        args.llm_concurrency + args.brave_concurrency + args.validation_concurrency,
        4,
    )
    batch_size = max(task_workers * 2, 1)

    print(f"输出目录: {output_paths.base_dir}")
    print(f"Run 目录: {run_paths.run_dir}")
    print(f"SQLite 状态库: {output_paths.db_file}")
    print(f"本次扫描任务: {len(empty_files)}，新增任务: {inserted}，重置中断任务: {reset_count}")
    print(
        "并发限制: "
        f"LLM={args.llm_concurrency}, Brave={args.brave_concurrency}, "
        f"Download={args.download_concurrency}, Validation={args.validation_concurrency}, "
        f"TaskWorkers={task_workers}"
    )

    with _ThreadPoolExecutor(max_workers=task_workers, thread_name_prefix="inc-worker") as executor:
        while True:
            tasks = db.fetch_schedulable_tasks(run_id, args.max_retries, limit=batch_size)
            if not tasks:
                break

            futures = {executor.submit(_process_task, task, ctx): task for task in tasks}
            for future in _as_completed(futures):
                task = futures[future]
                try:
                    future.result()
                except Exception as e:
                    message = f"线程异常: {str(e)[:180]}"
                    log_file = _os.path.join(run_paths.logs_dir, "errors.txt")
                    _log_event(f"[线程异常] {task['target_path']}: {message}", log_file)
                    db.fail_task(task, args.max_retries, message)

    task_counts = db.task_status_counts(run_id)
    final_status = _run_status_from_task_counts(task_counts)
    db.mark_run_status(run_id, final_status)
    _rebuild_run_manifest(db, run_paths)
    _rebuild_latest_manifest(db, output_paths)

    artifact_counts = db.artifact_status_counts(run_id)
    print("\n" + "=" * 60)
    print(f"Run 完成: {run_id}")
    print(f"状态: {final_status}")
    print(f"任务状态: {task_counts}")
    print(f"Artifact 状态: {artifact_counts}")
    print(f"Run manifest: {run_paths.manifest_file}")
    print(f"Latest manifest: {output_paths.latest_manifest_file}")
    print("=" * 60)


def _cmd_dry_run(args: _argparse.Namespace) -> None:
    empty_files = _scan_empty_files()
    if args.limit is not None:
        empty_files = empty_files[:args.limit]
    _print_dry_run(empty_files)


def _cmd_status(args: _argparse.Namespace) -> None:
    output_paths = _build_output_paths(args.output_dir)
    db = _PipelineDB(output_paths.db_file)
    if not _os.path.exists(output_paths.db_file):
        print(f"状态库不存在: {output_paths.db_file}")
        return
    db.init_schema()
    runs = db.list_runs(args.run_id)
    if not runs:
        print("没有 run 记录。")
        return
    for run in runs:
        task_counts = db.task_status_counts(run["run_id"])
        artifact_counts = db.artifact_status_counts(run["run_id"])
        print("-" * 60)
        print(f"run_id: {run['run_id']}")
        print(f"status: {run['status']}")
        print(f"created_at: {run['created_at']}")
        print(f"updated_at: {run['updated_at']}")
        print(f"tasks: {task_counts}")
        print(f"artifacts: {artifact_counts}")
    print("-" * 60)
    print(f"latest_manifest: {output_paths.latest_manifest_file}")


def _rollback_run(db: _PipelineDB, output_paths: _OutputPaths, run_id: str) -> None:
    run_paths = _build_run_paths(output_paths, run_id)
    _setup_run_output(run_paths)
    artifacts = db.active_artifacts_for_run(run_id)
    if not artifacts:
        print(f"run {run_id} 没有 active artifact 可回滚。")
        return

    for artifact in artifacts:
        src = artifact["staged_file_path"]
        rollback_path = None
        note = "moved"
        if src and _os.path.exists(src):
            rollback_path = _unique_path(_os.path.join(run_paths.rolled_back_files_dir, _os.path.basename(src)))
            _shutil.move(src, rollback_path)
        else:
            note = "source file missing"

        db.mark_artifact_rolled_back(artifact["artifact_id"], rollback_path)
        record = {
            "run_id": run_id,
            "artifact_id": artifact["artifact_id"],
            "target_path": artifact["target_path"],
            "staged_file_path": src,
            "rollback_path": rollback_path,
            "rolled_back_at": _now(),
            "note": note,
        }
        _append_jsonl(run_paths.rollback_file, record)

    _rebuild_run_manifest(db, run_paths)
    _rebuild_latest_manifest(db, output_paths)
    db.mark_run_status(run_id, "rolled_back")
    print(f"已回滚 run: {run_id}")
    print(f"回滚记录: {run_paths.rollback_file}")
    print(f"Latest manifest 已重建: {output_paths.latest_manifest_file}")


def _cmd_rollback(args: _argparse.Namespace) -> None:
    output_paths = _build_output_paths(args.output_dir)
    db = _PipelineDB(output_paths.db_file)
    if not _os.path.exists(output_paths.db_file):
        raise SystemExit(f"状态库不存在: {output_paths.db_file}")
    db.init_schema()

    run_id = args.run_id
    if args.latest:
        run_id = db.latest_run_with_active_artifacts()
        if not run_id:
            raise SystemExit("没有可回滚的 latest run。")
    if not run_id:
        raise SystemExit("请提供 --run-id 或 --latest。")
    if not db.run_exists(run_id):
        raise SystemExit(f"找不到 run_id: {run_id}")
    _rollback_run(db, output_paths, run_id)


def _add_output_arg(parser: _argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--output-dir",
        default=_DEFAULT_OUTPUT_DIR,
        help=f"增量输出目录，默认: {_DEFAULT_OUTPUT_DIR}",
    )


def _parse_args(argv: _Optional[_Iterable[str]] = None) -> _argparse.Namespace:
    raw = list(argv if argv is not None else _sys.argv[1:])
    commands = {"run", "status", "rollback", "dry-run"}
    if not raw:
        raw = ["run"]
    elif raw[0] in ("-h", "--help"):
        pass
    elif raw[0] not in commands:
        if "--dry-run" in raw:
            raw = ["dry-run"] + [x for x in raw if x != "--dry-run"]
        else:
            raw = ["run"] + raw

    parser = _argparse.ArgumentParser(
        description="RIP 后续文件高并发增量下载系统：SQLite 状态库 + run manifest + rollback。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="启动新批次或恢复旧批次")
    _add_output_arg(run_parser)
    run_parser.add_argument("--llm-concurrency", type=int, default=_DEFAULT_LLM_CONCURRENCY)
    run_parser.add_argument("--brave-concurrency", type=int, default=_DEFAULT_BRAVE_CONCURRENCY)
    run_parser.add_argument("--download-concurrency", type=int, default=_DEFAULT_DOWNLOAD_CONCURRENCY)
    run_parser.add_argument("--validation-concurrency", type=int, default=_DEFAULT_VALIDATION_CONCURRENCY)
    run_parser.add_argument("--max-retries", type=int, default=_DEFAULT_MAX_RETRIES)
    run_parser.add_argument("--resume-run-id")
    run_parser.add_argument("--limit", type=int, help="只处理扫描结果中的前 N 个文件，便于 demo")
    run_parser.add_argument("--task-workers", type=int, help="内部 task worker 数；默认按各阶段并发自动计算")
    run_parser.set_defaults(func=_cmd_run)

    dry_parser = subparsers.add_parser("dry-run", help="只扫描空文件，不写输出目录")
    dry_parser.add_argument("--limit", type=int)
    dry_parser.set_defaults(func=_cmd_dry_run)

    status_parser = subparsers.add_parser("status", help="查看 run/task/artifact 状态")
    _add_output_arg(status_parser)
    status_parser.add_argument("--run-id")
    status_parser.set_defaults(func=_cmd_status)

    rollback_parser = subparsers.add_parser("rollback", help="回滚某个 run 的 active artifact")
    _add_output_arg(rollback_parser)
    rollback_group = rollback_parser.add_mutually_exclusive_group(required=True)
    rollback_group.add_argument("--run-id")
    rollback_group.add_argument("--latest", action="store_true")
    rollback_parser.set_defaults(func=_cmd_rollback)

    return parser.parse_args(raw)


def _main(argv: _Optional[_Iterable[str]] = None) -> None:
    args = _parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    _main(_sys.argv[1:])
    raise SystemExit


__legacy_incremental_fetch_pipeline_v1__ = r'''
"""
incremental_fetch_pipeline.py — RIP 后续文件增量下载流程

这个入口与旧 main.py 解耦：只扫描空文件、搜索下载候选、验证内容，
然后把通过验证的文件放进独立增量目录，并用 manifest.jsonl 记录
“应该填充到哪里”。源目录全程只读，不覆盖、不删除、不生成协同文件。
"""

import argparse
import datetime
import hashlib
import json
import os
import random
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import requests

from pipeline.config import (
    EMPTY_FILE_THRESHOLD,
    HEADERS,
    LOGIN_WALL_PATTERNS,
    MAX_FILE_SIZE,
    MAX_URLS_PER_FILE,
    MIME_MAP,
    MIN_FILE_SIZE,
    VALID_DOC_EXTENSIONS,
    VALID_DOC_MIMES,
)
from pipeline.downloader import (
    compute_file_hash,
    detect_real_type_from_magic,
    normalize_url,
)
from pipeline.utils import ThreadSafeSet, log_event, setup_env


# ================= 路径与运行默认值 =================

TARGET_ROOTS = [
    ("chanpin", "/home/weizheng/RIP后续文件/chanpin"),
    ("yunyin", "/home/weizheng/RIP后续文件/yunyin"),
    ("kaifa", "/home/weizheng/RIP后续文件/kaifa"),
]

DEFAULT_OUTPUT_DIR = "/home/weizheng/RIP后续文件_增量下载"
DEFAULT_WORKERS = 8

TARGET_FILE_TYPES = ["pdf", "xlsx", "pptx", "text"]
FILE_TYPE_EXTENSION_MAP = {
    "pdf": [".pdf"],
    "xlsx": [".xlsx", ".xls"],
    "pptx": [".pptx", ".ppt"],
    "text": [".docx", ".doc", ".txt"],
}

TEXT_MIME_MAP = {
    "text/plain": ".txt",
    "text/markdown": ".txt",
    "text/csv": ".txt",
    "application/json": ".txt",
    "application/xml": ".txt",
    "text/xml": ".txt",
}

TEXT_MIMES = set(TEXT_MIME_MAP)
MIN_TEXT_FILE_SIZE = 256
DOWNLOAD_CHUNK_SIZE = 1024 * 64
REQUEST_TIMEOUT = 30
HEAD_TIMEOUT = 10


# ================= 线程共享状态 =================

_progress_lock = threading.Lock()
_manifest_lock = threading.Lock()


@dataclass(frozen=True)
class OutputPaths:
    base_dir: str
    files_dir: str
    tmp_dir: str
    logs_dir: str
    progress_file: str
    manifest_file: str


def build_output_paths(output_dir: str) -> OutputPaths:
    base_dir = os.path.abspath(output_dir)
    return OutputPaths(
        base_dir=base_dir,
        files_dir=os.path.join(base_dir, "files"),
        tmp_dir=os.path.join(base_dir, "tmp"),
        logs_dir=os.path.join(base_dir, "logs"),
        progress_file=os.path.join(base_dir, "progress.json"),
        manifest_file=os.path.join(base_dir, "manifest.jsonl"),
    )


def setup_output(paths: OutputPaths) -> None:
    setup_env(
        paths.base_dir,
        paths.files_dir,
        paths.tmp_dir,
        paths.logs_dir,
    )


# ================= 扫描空文件 =================

def _target_extensions() -> set:
    exts = set()
    for file_type in TARGET_FILE_TYPES:
        exts.update(FILE_TYPE_EXTENSION_MAP.get(file_type, []))
    return exts


def _file_type_for_ext(ext: str) -> Optional[str]:
    for file_type, exts in FILE_TYPE_EXTENSION_MAP.items():
        if ext in exts:
            return file_type
    return None


def scan_empty_files() -> List[Dict]:
    """递归扫描三个目标根目录里的空文件。"""
    target_exts = _target_extensions()
    results: List[Dict] = []

    for root_name, root_path in TARGET_ROOTS:
        if not os.path.isdir(root_path):
            print(f"[警告] 目标根目录不存在，跳过: {root_path}")
            continue

        for current_dir, dirnames, filenames in os.walk(root_path):
            dirnames[:] = [d for d in dirnames if d != "__MACOSX"]

            for filename in filenames:
                file_path = os.path.join(current_dir, filename)
                stem, ext = os.path.splitext(filename)
                ext_lower = ext.lower()

                if ext_lower not in target_exts:
                    continue

                try:
                    size = os.path.getsize(file_path)
                except OSError:
                    continue

                if size > EMPTY_FILE_THRESHOLD:
                    continue

                rel_path = os.path.relpath(file_path, root_path)
                rel_dir = os.path.dirname(rel_path)
                folder_path = os.path.dirname(file_path)
                leaf_name = os.path.basename(folder_path)
                parent_hint = ""
                if rel_dir and rel_dir != ".":
                    rel_parts = rel_dir.split(os.sep)
                    if len(rel_parts) > 1:
                        parent_hint = "/".join(rel_parts[:-1])

                results.append({
                    "target_root_name": root_name,
                    "target_root": root_path,
                    "path": file_path,
                    "relative_target_path": rel_path,
                    "folder_path": folder_path,
                    "filename": filename,
                    "stem": stem,
                    "ext": ext_lower,
                    "type": _file_type_for_ext(ext_lower),
                    "size": size,
                    "parent_hint": parent_hint,
                    "leaf_name": leaf_name,
                })

    results.sort(key=lambda item: (
        item["target_root_name"],
        item["relative_target_path"].lower(),
    ))
    return results


def print_dry_run(empty_files: List[Dict]) -> None:
    counts = {name: 0 for name, _ in TARGET_ROOTS}
    for item in empty_files:
        counts[item["target_root_name"]] += 1

    print("Dry run: 只扫描，不下载、不写输出目录。")
    print(f"空文件总数: {len(empty_files)}")
    for root_name, root_path in TARGET_ROOTS:
        print(f"  {root_name}: {counts[root_name]} ({root_path})")

    if empty_files:
        print("\n待处理路径:")
        for item in empty_files:
            print(f"  - {item['path']}")


# ================= 进度与 manifest =================

def _empty_progress() -> Dict:
    return {
        "completed": {},
        "seen_urls": ThreadSafeSet(),
        "seen_hashes": ThreadSafeSet(),
        "stats": {
            "total_empty_files": 0,
            "total_llm_search_calls": 0,
            "total_urls_returned": 0,
            "total_precheck_passed": 0,
            "total_precheck_rejected": 0,
            "total_download_ok": 0,
            "total_download_fail": 0,
            "total_tech_verify_rejected": 0,
            "total_llm_verify_rejected": 0,
            "total_staged": 0,
            "total_skipped": 0,
            "total_manifest_written": 0,
        },
    }


def _load_manifest_state(manifest_file: str) -> Tuple[Dict[str, str], List[str]]:
    completed = {}
    hashes = []

    if not os.path.exists(manifest_file):
        return completed, hashes

    try:
        with open(manifest_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue

                target_path = item.get("target_path")
                if target_path:
                    completed[target_path] = "staged"
                file_hash = item.get("sha256")
                if file_hash:
                    hashes.append(file_hash)
    except OSError:
        pass

    return completed, hashes


def load_progress(paths: OutputPaths) -> Dict:
    progress = _empty_progress()
    manifest_completed, manifest_hashes = _load_manifest_state(paths.manifest_file)

    if not os.path.exists(paths.progress_file):
        progress["completed"].update(manifest_completed)
        progress["seen_hashes"] = ThreadSafeSet(manifest_hashes)
        return progress

    try:
        with open(paths.progress_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        completed = data.get("completed", {})
        if isinstance(completed, dict):
            progress["completed"].update(completed)

        progress["seen_urls"] = ThreadSafeSet(data.get("seen_urls", []))

        progress_hashes = list(data.get("seen_hashes", []))
        progress["seen_hashes"] = ThreadSafeSet(progress_hashes + manifest_hashes)

        stats = data.get("stats", {})
        if isinstance(stats, dict):
            progress["stats"].update(stats)
    except (OSError, json.JSONDecodeError, TypeError) as e:
        print(f"[警告] 进度文件不可用 ({e})，将使用 manifest 恢复成功项。")

    # manifest 代表已经产出的增量文件，应覆盖 progress 里的旧 skipped 状态。
    progress["completed"].update(manifest_completed)
    return progress


def save_progress(progress: Dict, paths: OutputPaths) -> None:
    data = {
        "completed": dict(progress["completed"]),
        "seen_urls": list(progress["seen_urls"]),
        "seen_hashes": list(progress["seen_hashes"]),
        "stats": dict(progress["stats"]),
    }

    tmp_path = paths.progress_file + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, paths.progress_file)
    except Exception as e:
        print(f"[警告] 保存进度失败: {e}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def _update_stats(stats: Dict, key: str, delta: int = 1) -> None:
    stats[key] = stats.get(key, 0) + delta


def append_manifest(record: Dict, paths: OutputPaths) -> None:
    line = json.dumps(record, ensure_ascii=False, sort_keys=True)
    with _manifest_lock:
        with open(paths.manifest_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")


# ================= URL 预检与下载 =================

def _target_family(ext: str) -> str:
    normalized = (ext or "").lower().lstrip(".")
    if normalized == "pdf":
        return "pdf"
    if normalized in ("xlsx", "xls"):
        return "xlsx"
    if normalized in ("pptx", "ppt"):
        return "pptx"
    if normalized in ("docx", "doc", "txt"):
        return "text"
    return normalized


def _is_text_family(ext: str) -> bool:
    return _target_family(ext) == "text"


def _is_text_mime(content_type: str) -> bool:
    ct = (content_type or "").split(";")[0].strip().lower()
    return ct in TEXT_MIMES or ct.startswith("text/")


def _min_size_for_target(ext: str) -> int:
    return MIN_TEXT_FILE_SIZE if _is_text_family(ext) else MIN_FILE_SIZE


def _extensions_compatible(target_ext: str, detected_ext: str) -> bool:
    target = (target_ext or "").lower()
    detected = (detected_ext or "").lower()

    if target == ".pdf":
        return detected == ".pdf"
    if target in (".xlsx", ".xls"):
        return detected in (".xlsx", ".xls")
    if target in (".pptx", ".ppt"):
        return detected in (".pptx", ".ppt")
    if target in (".docx", ".doc", ".txt"):
        return detected in (".docx", ".doc", ".txt")
    return target == detected


def _content_type_allowed_for_target(content_type: str, target_ext: str, url: str) -> bool:
    ct = (content_type or "").split(";")[0].strip().lower()
    if not ct:
        return True

    if "text/html" in ct:
        return False

    if _is_text_mime(ct):
        return _is_text_family(target_ext)

    if ct in VALID_DOC_MIMES:
        return True

    path_ext = os.path.splitext(urlparse(url).path)[1].lower()
    if path_ext in VALID_DOC_EXTENSIONS:
        return _extensions_compatible(target_ext, path_ext)

    return False


def pre_check_url_incremental(
    url: str,
    target_ext: str,
    seen_urls: ThreadSafeSet,
    log_file: str,
) -> Tuple[bool, str, str]:
    """HEAD 预检：URL 去重、登录墙、MIME 和大小。"""
    norm_url = normalize_url(url)
    if not seen_urls.try_add(norm_url):
        return False, "URL 重复，跳过。", url

    try:
        response = requests.head(
            url,
            headers=HEADERS,
            timeout=HEAD_TIMEOUT,
            allow_redirects=True,
        )
        response.raise_for_status()
    except requests.exceptions.TooManyRedirects:
        return False, "重定向次数过多。", url
    except requests.exceptions.HTTPError as e:
        return False, f"HTTP {e.response.status_code}", url
    except Exception as e:
        log_event(f"      [预检] HEAD 失败 ({str(e)[:50]})，放行。", log_file)
        return True, "HEAD 失败但放行", url

    final_url = response.url
    for pattern in LOGIN_WALL_PATTERNS:
        if pattern in final_url.lower():
            return False, f"重定向到登录页 ({pattern})。", final_url

    norm_final = normalize_url(final_url)
    if norm_final != norm_url and not seen_urls.try_add(norm_final):
        return False, "重定向后 URL 重复。", final_url

    content_type = response.headers.get("Content-Type", "")
    if not _content_type_allowed_for_target(content_type, target_ext, final_url):
        return False, f"Content-Type={content_type} 与目标类型不兼容。", final_url

    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            size = int(content_length)
            min_size = _min_size_for_target(target_ext)
            if size < min_size:
                return False, f"文件太小 ({size/1024:.1f}KB)。", final_url
            if size > MAX_FILE_SIZE:
                return False, f"文件过大 ({size/1024/1024:.1f}MB)。", final_url
        except ValueError:
            pass

    return True, "预检通过", final_url


def _sanitize_filename_part(value: str, max_len: int = 90) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:max_len] or "downloaded"


def _extension_from_response(response: requests.Response, url: str, target_ext: str) -> str:
    content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()

    if content_type in TEXT_MIME_MAP and _is_text_family(target_ext):
        return TEXT_MIME_MAP[content_type]

    mapped = MIME_MAP.get(content_type)
    if mapped:
        return mapped

    path_ext = os.path.splitext(urlparse(url).path)[1].lower()
    if path_ext in VALID_DOC_EXTENSIONS:
        return path_ext

    return target_ext if target_ext in VALID_DOC_EXTENSIONS else ".bin"


def download_candidate(
    url: str,
    target_ext: str,
    paths: OutputPaths,
    file_idx: int,
) -> Tuple[bool, str, Optional[str], Optional[str]]:
    """下载候选文件到 tmp 目录。返回 (ok, info/path, final_url, content_type)。"""
    try:
        response = requests.get(
            url,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
            stream=True,
            allow_redirects=True,
        )
        response.raise_for_status()

        final_url = response.url
        for pattern in LOGIN_WALL_PATTERNS:
            if pattern in final_url.lower():
                return False, f"重定向到登录页 ({pattern})。", final_url, None

        content_type = response.headers.get("Content-Type", "")
        if not _content_type_allowed_for_target(content_type, target_ext, final_url):
            return False, f"检测到不兼容内容 ({content_type})。", final_url, content_type

        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                size = int(content_length)
                if size < _min_size_for_target(target_ext):
                    return False, f"文件太小 ({size/1024:.1f}KB)。", final_url, content_type
            except ValueError:
                pass

        ext = _extension_from_response(response, final_url, target_ext)
        tid = threading.current_thread().ident
        stamp = f"{time.time():.6f}".replace(".", "")
        tmp_name = f"download_{file_idx}_{tid}_{stamp}{ext}"
        tmp_path = os.path.join(paths.tmp_dir, tmp_name)

        with open(tmp_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                if chunk:
                    f.write(chunk)

        actual_size = os.path.getsize(tmp_path)
        if actual_size < _min_size_for_target(target_ext):
            os.remove(tmp_path)
            return False, f"实际文件太小 ({actual_size/1024:.1f}KB)，已删除。", final_url, content_type

        return True, tmp_path, final_url, content_type
    except Exception as e:
        return False, f"连接异常: {str(e)[:100]}", None, None


# ================= 下载后验证与 staging =================

def _looks_like_plain_text(file_path: str) -> bool:
    try:
        with open(file_path, "rb") as f:
            raw = f.read(32 * 1024)
    except OSError:
        return False

    if not raw or b"\x00" in raw:
        return False

    decoded = ""
    for encoding in ("utf-8", "gb18030", "latin-1"):
        try:
            decoded = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue

    if not decoded:
        return False

    stripped = decoded.lstrip("\ufeff").strip().lower()
    if stripped.startswith(("<!doctype", "<html", "<head")) or "<html" in stripped[:300]:
        return False

    printable = sum(1 for c in decoded if c.isprintable() or c in "\n\r\t")
    return printable / max(len(decoded), 1) > 0.85


def verify_downloaded_file(
    file_path: str,
    target_ext: str,
    seen_hashes: ThreadSafeSet,
    log_file: str,
) -> Tuple[bool, str, Optional[str], Optional[str]]:
    """Magic bytes、文本识别、扩展名族兼容、哈希去重。"""
    detected_ext = detect_real_type_from_magic(file_path)

    if detected_ext == ".html":
        if _is_text_family(target_ext) and _looks_like_plain_text(file_path):
            detected_ext = ".txt"
        else:
            _remove_quietly(file_path)
            return False, "内容实际是 HTML，已删除。", None, None

    if detected_ext == ".zip":
        _remove_quietly(file_path)
        return False, "普通 ZIP 而非 Office 文档，已删除。", None, None

    if detected_ext == ".ole":
        if target_ext in (".doc", ".docx"):
            detected_ext = ".doc"
        elif target_ext in (".xls", ".xlsx"):
            detected_ext = ".xls"
        elif target_ext in (".ppt", ".pptx"):
            detected_ext = ".ppt"

    if not detected_ext and _looks_like_plain_text(file_path):
        detected_ext = ".txt"

    if not detected_ext:
        current_ext = os.path.splitext(file_path)[1].lower()
        if current_ext in VALID_DOC_EXTENSIONS:
            detected_ext = current_ext

    if not detected_ext:
        _remove_quietly(file_path)
        return False, "无法识别真实文件类型，已删除。", None, None

    if not _extensions_compatible(target_ext, detected_ext):
        _remove_quietly(file_path)
        return (
            False,
            f"类型不兼容: 目标 {target_ext}, 实际 {detected_ext}，已删除。",
            None,
            None,
        )

    file_hash = compute_file_hash(file_path)
    if file_hash and not seen_hashes.try_add(file_hash):
        _remove_quietly(file_path)
        return False, f"内容哈希重复 ({file_hash[:16]}...)，已删除。", None, None

    log_event(f"      [技术验证] 通过，实际类型 {detected_ext}", log_file)
    return True, "后验证通过", detected_ext, file_hash


def _stage_filename(root_name: str, relative_path: str, detected_ext: str) -> str:
    key = f"{root_name}/{relative_path}".replace(os.sep, "/")
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    original_name = os.path.basename(relative_path)
    stem = os.path.splitext(original_name)[0]
    safe_stem = _sanitize_filename_part(stem, max_len=70)
    ext = detected_ext if detected_ext.startswith(".") else f".{detected_ext}"
    return f"{root_name}__{digest}__{safe_stem}{ext}"


def _unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path

    base, ext = os.path.splitext(path)
    counter = 1
    while True:
        candidate = f"{base}_{counter}{ext}"
        if not os.path.exists(candidate):
            return candidate
        counter += 1


def stage_verified_file(file_path: str, file_info: Dict, detected_ext: str, paths: OutputPaths) -> str:
    filename = _stage_filename(
        file_info["target_root_name"],
        file_info["relative_target_path"],
        detected_ext,
    )
    dest_path = _unique_path(os.path.join(paths.files_dir, filename))
    shutil.move(file_path, dest_path)
    return dest_path


def _remove_quietly(path: Optional[str]) -> None:
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


# ================= 单文件处理 =================

def _mark_completed(progress: Dict, paths: OutputPaths, target_path: str, status: str) -> None:
    with _progress_lock:
        progress["completed"][target_path] = status
        save_progress(progress, paths)


def _process_single_file(
    file_info: Dict,
    file_idx: int,
    total_files: int,
    paths: OutputPaths,
    progress: Dict,
) -> None:
    log_file = os.path.join(paths.logs_dir, f"{file_info['target_root_name']}.txt")
    stats = progress["stats"]
    seen_urls = progress["seen_urls"]
    seen_hashes = progress["seen_hashes"]

    target_path = file_info["path"]
    target_ext = file_info["ext"]

    log_event("", log_file)
    log_event(
        f"  [{file_idx + 1}/{total_files}] 处理: {target_path} ({file_info['size']}B)",
        log_file,
    )

    try:
        from pipeline.llm_agent import (
            build_instruction,
            call_llm_for_search_plan,
            search_downloadable_url_candidates,
        )
        from pipeline.validator import validate_content_relevance
    except Exception as e:
        log_event(f"    [初始化失败] 无法加载 LLM 组件: {str(e)[:120]}", log_file)
        return

    instruction = build_instruction(
        file_info,
        file_info.get("parent_hint", ""),
        file_info.get("leaf_name", ""),
    )
    log_event("    [Step 1] Instruction 已构造", log_file)

    with _progress_lock:
        _update_stats(stats, "total_llm_search_calls")

    log_event("    [Step 2] 调用 LLM 生成 search plan...", log_file)
    search_plan = call_llm_for_search_plan(instruction, log_file)
    queries = search_plan.get("queries", []) if isinstance(search_plan, dict) else []
    if not queries:
        log_event("    [Step 2] LLM 未返回有效 search plan，跳过此文件。", log_file)
        with _progress_lock:
            _update_stats(stats, "total_skipped")
        _mark_completed(progress, paths, target_path, "skipped")
        return

    for query_idx, query in enumerate(queries[:5], 1):
        log_event(f"      [Query {query_idx}] {query}", log_file)

    context = file_info.get("leaf_name", "")
    if file_info.get("parent_hint"):
        context = f"{file_info['parent_hint']}/{context}"

    url_candidates = search_downloadable_url_candidates(
        filename=file_info["stem"],
        ext=target_ext,
        context=context,
        max_results=MAX_URLS_PER_FILE,
        llm_queries=queries,
    )

    with _progress_lock:
        _update_stats(stats, "total_urls_returned", len(url_candidates))

    if not url_candidates:
        log_event("    [Step 3] Brave Search 未返回 URL，跳过此文件。", log_file)
        with _progress_lock:
            _update_stats(stats, "total_skipped")
        _mark_completed(progress, paths, target_path, "skipped")
        return

    log_event(f"    [Step 3] 获得 {len(url_candidates)} 个候选", log_file)

    for url_idx, item in enumerate(url_candidates, 1):
        url = (item.get("url") or "").strip()
        title = item.get("title", "")
        if not url.startswith("http"):
            continue

        log_event(f"    [{url_idx}/{len(url_candidates)}] {title or url[:80]}", log_file)

        passed, reason, final_url = pre_check_url_incremental(
            url,
            target_ext,
            seen_urls,
            log_file,
        )
        if not passed:
            log_event(f"      [预检拒绝] {reason}", log_file)
            with _progress_lock:
                _update_stats(stats, "total_precheck_rejected")
            time.sleep(0.2)
            continue

        with _progress_lock:
            _update_stats(stats, "total_precheck_passed")

        download_ok, download_info, downloaded_url, content_type = download_candidate(
            final_url,
            target_ext,
            paths,
            file_idx + 1,
        )
        if not download_ok:
            log_event(f"      [下载失败] {download_info}", log_file)
            with _progress_lock:
                _update_stats(stats, "total_download_fail")
            time.sleep(0.3)
            continue

        tmp_path = download_info
        with _progress_lock:
            _update_stats(stats, "total_download_ok")
        log_event(f"      [下载成功] {os.path.basename(tmp_path)}", log_file)

        tech_ok, tech_reason, detected_ext, file_hash = verify_downloaded_file(
            tmp_path,
            target_ext,
            seen_hashes,
            log_file,
        )
        if not tech_ok:
            log_event(f"      [技术验证拒绝] {tech_reason}", log_file)
            with _progress_lock:
                _update_stats(stats, "total_tech_verify_rejected")
            time.sleep(0.2)
            continue

        is_relevant, relevance_reason = validate_content_relevance(
            tmp_path,
            file_info["stem"],
            log_file,
        )
        if not is_relevant:
            _remove_quietly(tmp_path)
            log_event(f"      [内容不相关] {relevance_reason}", log_file)
            with _progress_lock:
                _update_stats(stats, "total_llm_verify_rejected")
            time.sleep(0.2)
            continue

        staged_path = stage_verified_file(tmp_path, file_info, detected_ext, paths)
        record = {
            "target_path": target_path,
            "staged_file_path": staged_path,
            "target_root": file_info["target_root"],
            "target_root_name": file_info["target_root_name"],
            "relative_target_path": file_info["relative_target_path"],
            "source_url": downloaded_url or final_url,
            "source_title": title,
            "detected_ext": detected_ext,
            "sha256": file_hash,
            "content_type": content_type,
            "validated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        append_manifest(record, paths)

        with _progress_lock:
            _update_stats(stats, "total_staged")
            _update_stats(stats, "total_manifest_written")
            progress["completed"][target_path] = "staged"
            save_progress(progress, paths)

        log_event(f"    【增量保存成功】{target_path} -> {staged_path}", log_file)
        time.sleep(random.uniform(0.5, 1.0))
        return

    log_event(f"    【未找到替代】{target_path}", log_file)
    with _progress_lock:
        _update_stats(stats, "total_skipped")
        progress["completed"][target_path] = "skipped"
        save_progress(progress, paths)


# ================= 主流程 =================

def run_task(args: argparse.Namespace) -> None:
    paths = build_output_paths(args.output_dir)

    if args.reset_progress and os.path.exists(paths.progress_file):
        os.remove(paths.progress_file)
        print(f"已清除进度文件: {paths.progress_file}")

    empty_files = scan_empty_files()

    if args.dry_run:
        print_dry_run(empty_files)
        return

    setup_output(paths)
    progress = load_progress(paths)
    completed = progress["completed"]
    stats = progress["stats"]

    pending_files = [
        item for item in empty_files
        if item["path"] not in completed
    ]

    with _progress_lock:
        _update_stats(stats, "total_empty_files", len(pending_files))
        save_progress(progress, paths)

    print(f"输出目录: {paths.base_dir}")
    print(f"Manifest: {paths.manifest_file}")
    print(f"空文件总数: {len(empty_files)}")
    print(f"已完成/已跳过: {len(completed)}")
    print(f"本次待处理: {len(pending_files)}")
    print(f"并发线程数: {args.workers}")

    if not pending_files:
        print("没有待处理文件。")
        return

    with ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="inc-worker") as executor:
        futures = {}
        total = len(pending_files)
        for file_idx, file_info in enumerate(pending_files):
            future = executor.submit(
                _process_single_file,
                file_info=file_info,
                file_idx=file_idx,
                total_files=total,
                paths=paths,
                progress=progress,
            )
            futures[future] = file_info["path"]

        for future in as_completed(futures):
            target_path = futures[future]
            try:
                future.result()
            except Exception as e:
                log_file = os.path.join(paths.logs_dir, "errors.txt")
                log_event(f"[线程异常] {target_path}: {str(e)[:160]}", log_file)

    with _progress_lock:
        save_progress(progress, paths)

    print("\n" + "=" * 60)
    print("增量下载统计:")
    print(f"  扫描到空文件:        {len(empty_files)}")
    print(f"  本次待处理:          {len(pending_files)}")
    print(f"  LLM 搜索调用次数:    {stats['total_llm_search_calls']}")
    print(f"  Brave URL 总数:      {stats['total_urls_returned']}")
    print(f"  预检通过/拒绝:       {stats['total_precheck_passed']} / {stats['total_precheck_rejected']}")
    print(f"  下载成功/失败:       {stats['total_download_ok']} / {stats['total_download_fail']}")
    print(f"  技术验证拒绝:        {stats['total_tech_verify_rejected']}")
    print(f"  LLM 内容验证拒绝:    {stats['total_llm_verify_rejected']}")
    print(f"  增量保存成功:        {stats['total_staged']}")
    print(f"  未找到替代/跳过:     {stats['total_skipped']}")
    print(f"  Manifest 写入:       {stats['total_manifest_written']}")
    print(f"  URL 去重池:          {len(progress['seen_urls'])}")
    print(f"  哈希去重池:          {len(progress['seen_hashes'])}")
    print("=" * 60)
    print(f"进度已保存到: {paths.progress_file}")


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="RIP 后续文件增量下载 pipeline：下载到 staging 目录并记录 manifest，不改源目录。",
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"增量输出目录，默认: {DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"并发线程数，默认: {DEFAULT_WORKERS}",
    )
    parser.add_argument(
        "--reset-progress",
        action="store_true",
        help="只删除 progress.json；不会删除已下载文件或 manifest。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只扫描并打印待处理空文件，不创建输出目录、不下载。",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    run_task(parse_args(sys.argv[1:]))
'''
