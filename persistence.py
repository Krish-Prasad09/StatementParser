"""
persistence.py
--------------
SQLite persistence store for processed statements, audit events, and quarantine queues.
Ensures durable duplicate and revision tracking across restarts and supports the Streamlit dashboard.
"""

import sqlite3
import json
import os
from contextlib import closing
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Tuple

from models import ExtractionResult
from utils import compute_sha256


DEFAULT_DB_PATH = "statements.db"


def get_db_connection(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Creates a connection to SQLite database and ensures schema exists."""
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    _create_tables(conn)
    return conn


def _create_tables(conn: sqlite3.Connection):
    """Initializes tables and indexes for statement results and deduplication."""
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS statements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_path TEXT NOT NULL,
        sha256_hash TEXT NOT NULL,
        format_id TEXT,
        status TEXT NOT NULL,
        issues_json TEXT NOT NULL,
        llm_calls_made INTEGER NOT NULL DEFAULT 0,
        record_json TEXT,
        diagnostic_message TEXT,
        created_at TEXT NOT NULL
    );
    """)
    try:
        cursor.execute("ALTER TABLE statements ADD COLUMN diagnostic_message TEXT;")
    except Exception:
        pass
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_statements_hash ON statements(sha256_hash);
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_statements_status ON statements(status);
    """)

    # Table for email-level ingestion events (persists ingest, ignore, needs_review, error)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS email_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id TEXT NOT NULL,
        subject TEXT,
        sender TEXT,
        received_at TEXT,
        status TEXT NOT NULL,
        reason TEXT,
        attachments_json TEXT,
        processing_state TEXT NOT NULL DEFAULT 'completed',
        processed_at TEXT NOT NULL
    );
    """)
    try:
        cursor.execute("ALTER TABLE email_events ADD COLUMN processing_state TEXT NOT NULL DEFAULT 'completed'")
    except Exception:
        pass
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_email_events_msg_id ON email_events(message_id);
    """)
    try:
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_email_events_message_unique ON email_events(message_id);")
    except sqlite3.IntegrityError:
        # A legacy database with duplicate events remains readable. New stores
        # receive the uniqueness protection required for worker claiming.
        pass
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_email_events_status ON email_events(status);
    """)

    # Table for durable failed learning attempts across restarts
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS failed_learning_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        format_id TEXT NOT NULL,
        provider_key TEXT,
        layout_kind TEXT,
        fingerprint_hash TEXT,
        failure_issue TEXT NOT NULL,
        diagnostic_message TEXT,
        failed_at TEXT NOT NULL
    );
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_failed_learning_fmt ON failed_learning_attempts(format_id);
    """)
    conn.commit()


class StatementStore:
    """Interface for saving, querying, and syncing parsed statements across restarts."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        with closing(get_db_connection(self.db_path)) as conn:
            pass

    def save_result(self, result: ExtractionResult, sha256_hash: str = "") -> int:
        """Saves an ExtractionResult into the database with its SHA-256 content hash."""
        effective_hash = sha256_hash or result.sha256_hash or ""
        if not effective_hash and result.file and os.path.exists(result.file):
            try:
                effective_hash = compute_sha256(result.file)
            except Exception:
                effective_hash = ""

        result.sha256_hash = effective_hash
        now_iso = datetime.now(timezone.utc).isoformat()
        issues_str = json.dumps(result.issues or [])
        record_str = result.model_dump_json()
        diag_msg = result.diagnostic_message or result.notes or ""

        with closing(get_db_connection(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO statements (
                    file_path, sha256_hash, format_id, status,
                    issues_json, llm_calls_made, record_json, diagnostic_message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                result.file,
                effective_hash,
                result.format_id or "unknown",
                result.status,
                issues_str,
                result.llm_calls_made,
                record_str,
                diag_msg,
                now_iso
            ))
            conn.commit()
            return cursor.lastrowid

    def get_seen_hashes(self) -> Dict[str, str]:
        """Returns {sha256_hash: file_path} for all previously ingested non-duplicate files."""
        seen: Dict[str, str] = {}
        with closing(get_db_connection(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT sha256_hash, file_path FROM statements WHERE sha256_hash != '' AND status IN ('validated', 'not_a_statement') ORDER BY id ASC"
            )
            for row in cursor.fetchall():
                h = row["sha256_hash"]
                if h and h not in seen:
                    seen[h] = row["file_path"]
        return seen

    def get_seen_revisions(self) -> Dict[Tuple[str, str, str], str]:
        """Returns {(provider_or_format, client_id, period_end): sha256_hash} from persisted records."""
        revisions: Dict[Tuple[str, str, str], str] = {}
        with closing(get_db_connection(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT sha256_hash, format_id, record_json FROM statements WHERE sha256_hash != '' AND status != 'duplicate' ORDER BY id ASC"
            )
            for row in cursor.fetchall():
                h = row["sha256_hash"]
                fmt_id = (row["format_id"] or "").strip().lower()
                rec_json = row["record_json"]
                if not rec_json:
                    continue
                try:
                    data = json.loads(rec_json)
                    for stmt in data.get("statements", []):
                        meta = stmt.get("statement", {})
                        prov = (meta.get("provider") or fmt_id).strip().lower()
                        cid = (meta.get("client_id") or "").strip().upper()
                        pend = (meta.get("period_end") or "").strip()
                        if prov and cid and pend:
                            revisions[(prov, cid, pend)] = h
                        if fmt_id and cid and pend:
                            revisions[(fmt_id, cid, pend)] = h
                except Exception:
                    pass
        return revisions

    def sync_to_pipeline(self, pipeline: Any):
        """Loads persisted seen_hashes, seen_statements, and ExtractionResult history into a ProcessingPipeline."""
        seen_hashes = self.get_seen_hashes()
        for h, fpath in seen_hashes.items():
            if h not in pipeline.ingestion.seen_hashes:
                pipeline.ingestion.seen_hashes[h] = fpath

        seen_revs = self.get_seen_revisions()
        for k, h in seen_revs.items():
            if k not in pipeline.ingestion.seen_statements:
                pipeline.ingestion.seen_statements[k] = h

        with closing(get_db_connection(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT file_path, record_json FROM statements WHERE status != 'duplicate' ORDER BY id ASC"
            )
            for row in cursor.fetchall():
                fpath = row["file_path"]
                rec_json = row["record_json"]
                if fpath and rec_json and fpath not in pipeline.processed_history:
                    try:
                        pipeline.processed_history[fpath] = ExtractionResult.model_validate_json(rec_json)
                    except Exception:
                        pass

    def get_all(self) -> List[Dict[str, Any]]:
        """Retrieves all parsed statements from SQLite."""
        with closing(get_db_connection(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM statements ORDER BY id DESC")
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_needs_review(self) -> List[Dict[str, Any]]:
        """Retrieves all quarantined statements needing human intervention."""
        with closing(get_db_connection(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM statements WHERE status = 'needs_review' ORDER BY id DESC")
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_validated(self) -> List[Dict[str, Any]]:
        """Retrieves all successfully validated statements."""
        with closing(get_db_connection(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM statements WHERE status = 'validated' ORDER BY id DESC")
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_by_hash(self, sha256_hash: str) -> Optional[Dict[str, Any]]:
        """Retrieves a statement by its content hash to prevent duplicate processing."""
        if not sha256_hash:
            return None
        with closing(get_db_connection(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM statements WHERE sha256_hash = ? ORDER BY id ASC LIMIT 1", (sha256_hash,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def update_statement_status(self, db_id: int, result: ExtractionResult):
        """Updates an existing statement row in SQLite after unlocking/reprocessing."""
        with closing(get_db_connection(self.db_path)) as conn:
            cursor = conn.cursor()
            issues_str = json.dumps(result.issues or [])
            record_str = result.model_dump_json()
            diag_msg = result.diagnostic_message or result.notes or ""
            cursor.execute("""
                UPDATE statements
                SET status = ?,
                    format_id = ?,
                    issues_json = ?,
                    llm_calls_made = ?,
                    record_json = ?,
                    diagnostic_message = ?
                WHERE id = ?
            """, (
                result.status,
                result.format_id or "unknown",
                issues_str,
                result.llm_calls_made,
                record_str,
                diag_msg,
                db_id
            ))
            conn.commit()

    def save_email_event(
        self,
        message_id: str,
        subject: str = "",
        sender: str = "",
        status: str = "ignore",
        reason: str = "",
        attachments: Optional[List[str]] = None,
        received_at: str = "",
        processing_state: str = ""
    ) -> int:
        """Persists an email-level ingestion event (ingest, ignore, needs_review, error)."""
        now_iso = datetime.now(timezone.utc).isoformat()
        att_json = json.dumps(attachments or [])
        clean_msg_id = (message_id or "").strip()
        eff_state = processing_state if processing_state else ("completed" if status in ("ignore", "needs_review") else "started")
        with closing(get_db_connection(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO email_events (
                    message_id, subject, sender, received_at,
                    status, reason, attachments_json, processing_state, processed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (clean_msg_id, subject, sender, received_at, status, reason, att_json, eff_state, now_iso))
            conn.commit()
            return cursor.lastrowid

    def claim_email_event(
        self,
        message_id: str,
        subject: str = "",
        sender: str = "",
        status: str = "ignore",
        reason: str = "",
        attachments: Optional[List[str]] = None,
        received_at: str = "",
        stale_after_seconds: int = 600,
    ) -> str:
        """Atomically claim an email for one worker.

        Returns ``claimed``, ``completed``, or ``in_progress``. A failed or
        abandoned claim may be retried; a live claim is never processed twice.
        """
        clean_msg_id = (message_id or "").strip()
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        initial_state = "started" if status == "ingest" else "completed"
        with closing(get_db_connection(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(
                "SELECT id, processing_state, processed_at FROM email_events WHERE message_id = ? ORDER BY id DESC LIMIT 1",
                (clean_msg_id,),
            )
            existing = cursor.fetchone()
            if existing:
                state = existing["processing_state"]
                if state == "completed":
                    conn.commit()
                    return "completed"
                if state == "started":
                    try:
                        claimed_at = datetime.fromisoformat(existing["processed_at"])
                        if claimed_at.tzinfo is None:
                            claimed_at = claimed_at.replace(tzinfo=timezone.utc)
                        is_stale = (now - claimed_at).total_seconds() >= stale_after_seconds
                    except (TypeError, ValueError):
                        is_stale = True
                    if not is_stale:
                        conn.commit()
                        return "in_progress"
                cursor.execute(
                    "UPDATE email_events SET subject = ?, sender = ?, received_at = ?, status = ?, reason = ?, attachments_json = ?, processing_state = ?, processed_at = ? WHERE id = ?",
                    (subject, sender, received_at, status, reason, json.dumps(attachments or []), initial_state, now_iso, existing["id"]),
                )
                conn.commit()
                return "claimed"

            cursor.execute(
                "INSERT INTO email_events (message_id, subject, sender, received_at, status, reason, attachments_json, processing_state, processed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (clean_msg_id, subject, sender, received_at, status, reason, json.dumps(attachments or []), initial_state, now_iso),
            )
            conn.commit()
            return "claimed"

    def update_email_event_processing_state(self, message_id: str, processing_state: str, reason: str = ""):
        """Updates the processing_state and reason of an existing email event."""
        if not message_id:
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        with closing(get_db_connection(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE email_events
                SET processing_state = ?, reason = CASE WHEN ? != '' THEN ? ELSE reason END, processed_at = ?
                WHERE message_id = ?
            """, (processing_state, reason, reason, now_iso, message_id.strip()))
            conn.commit()

    def update_email_event_status(self, message_id: str, status: str, reason: str = ""):
        """Updates the status and reason of an existing email event."""
        if not message_id:
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        with closing(get_db_connection(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE email_events
                SET status = ?, reason = CASE WHEN ? != '' THEN ? ELSE reason END, processed_at = ?
                WHERE message_id = ?
            """, (status, reason, reason, now_iso, message_id.strip()))
            conn.commit()

    def has_email_event(self, message_id: str) -> bool:
        """Checks if an email with the given message_id was already successfully processed."""
        if not message_id:
            return False
        with closing(get_db_connection(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id FROM email_events WHERE message_id = ? AND processing_state = 'completed' LIMIT 1",
                (message_id.strip(),)
            )
            return cursor.fetchone() is not None

    def get_email_events(self) -> List[Dict[str, Any]]:
        """Retrieves all email ingestion events ordered chronologically."""
        with closing(get_db_connection(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM email_events ORDER BY id DESC")
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_email_event_by_message_id(self, message_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves email event record by message_id."""
        if not message_id:
            return None
        with closing(get_db_connection(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM email_events WHERE message_id = ? LIMIT 1", (message_id.strip(),))
            row = cursor.fetchone()
            return dict(row) if row else None

    def save_failed_learning(
        self,
        format_id: str,
        provider_key: str = "",
        layout_kind: str = "",
        fingerprint_hash: str = "",
        failure_issue: str = "",
        diagnostic_message: str = ""
    ) -> int:
        """Persists a failed format learning attempt durably across restarts."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with closing(get_db_connection(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO failed_learning_attempts (
                    format_id, provider_key, layout_kind, fingerprint_hash,
                    failure_issue, diagnostic_message, failed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (format_id, provider_key, layout_kind, fingerprint_hash, failure_issue, diagnostic_message, now_iso))
            conn.commit()
            return cursor.lastrowid

    def has_failed_learning(self, format_id: str, fingerprint_hash: str = "") -> Optional[Dict[str, Any]]:
        """Checks if a format_id or fingerprint hash previously failed learning."""
        with closing(get_db_connection(self.db_path)) as conn:
            cursor = conn.cursor()
            if fingerprint_hash:
                cursor.execute(
                    "SELECT * FROM failed_learning_attempts WHERE format_id = ? AND fingerprint_hash = ? ORDER BY id DESC LIMIT 1",
                    (format_id, fingerprint_hash)
                )
            else:
                cursor.execute(
                    "SELECT * FROM failed_learning_attempts WHERE format_id = ? ORDER BY id DESC LIMIT 1",
                    (format_id,)
                )
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_failed_learnings(self) -> List[Dict[str, Any]]:
        """Retrieves all persisted failed learning attempts."""
        with closing(get_db_connection(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM failed_learning_attempts ORDER BY id DESC")
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def clear(self):
        """Clears all records (useful for test resets)."""
        with closing(get_db_connection(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM statements")
            cursor.execute("DELETE FROM email_events")
            cursor.execute("DELETE FROM failed_learning_attempts")
            try:
                cursor.execute("DELETE FROM sqlite_sequence WHERE name IN ('statements', 'email_events', 'failed_learning_attempts')")
            except Exception:
                pass
            conn.commit()
