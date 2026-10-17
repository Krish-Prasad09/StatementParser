"""
persistence.py
--------------
Durable persistence store for processed statements, audit events, and quarantine queues
backed by PostgreSQL (Neon.tech / Supabase / AWS RDS).
Ensures durable duplicate and revision tracking across restarts and powers the Streamlit dashboard.
"""

import json
import os
import hashlib
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
import threading

from models import ExtractionResult
from utils import compute_sha256


DEFAULT_DB_PATH = ""

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None


def _load_env_if_needed():
    """
    Auto-discovers DATABASE_URL from Streamlit Cloud secrets or local .env file.
    This guarantees persistence.py connects to Cloud PostgreSQL (Neon.tech)
    without depending on another module loading .env first.
    """
    if os.getenv("DATABASE_URL"):
        return
    # 1. Check Streamlit Cloud secrets (st.secrets) if running inside Streamlit
    try:
        import streamlit as st
        if hasattr(st, "secrets"):
            if "DATABASE_URL" in st.secrets:
                os.environ["DATABASE_URL"] = str(st.secrets["DATABASE_URL"])
                return
            for val in st.secrets.values():
                if isinstance(val, dict) and "DATABASE_URL" in val:
                    os.environ["DATABASE_URL"] = str(val["DATABASE_URL"])
                    return
    except Exception:
        pass

    # 2. Check local .env file in project root
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_file):
        try:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip().strip("\"'")
                        if k and not os.environ.get(k):
                            os.environ[k] = v
        except Exception:
            pass


class _MockStoreState:
    """Thread-safe state container for an isolated in-memory test store."""
    def __init__(self):
        self.lock = threading.RLock()
        self.statements: List[Dict[str, Any]] = []
        self.statement_artifacts: Dict[str, Dict[str, Any]] = {}
        self.format_templates: Dict[str, Dict[str, Any]] = {}
        self.email_events: List[Dict[str, Any]] = []
        self.failed_learning_attempts: List[Dict[str, Any]] = []
        self._stmt_id_seq = 0
        self._email_id_seq = 0
        self._failed_id_seq = 0

    def clear(self):
        with self.lock:
            self.statements.clear()
            self.statement_artifacts.clear()
            self.format_templates.clear()
            self.email_events.clear()
            self.failed_learning_attempts.clear()
            self._stmt_id_seq = 0
            self._email_id_seq = 0
            self._failed_id_seq = 0


_MOCK_STORE_REGISTRY: Dict[str, _MockStoreState] = {}
_MOCK_REGISTRY_LOCK = threading.Lock()


def _get_mock_state(target: str) -> _MockStoreState:
    key = str(target or ":memory:")
    with _MOCK_REGISTRY_LOCK:
        if key not in _MOCK_STORE_REGISTRY:
            _MOCK_STORE_REGISTRY[key] = _MockStoreState()
        return _MOCK_STORE_REGISTRY[key]


def _is_pg_target(db_path: Optional[str] = None) -> Tuple[bool, str]:
    """
    Determines if target is a PostgreSQL connection or an isolated test mock store.
    Returns (is_pg, target_connection_or_identifier).
    - If db_path is an explicit postgresql:// URL -> (True, db_path)
    - If TEST_DATABASE_URL is provided in test mode -> (True, test_pg)
    - In test mode (STATEMENT_PARSER_TEST_MODE="1") -> (False, db_path or ":memory:")
    - If db_path is an explicit non-PostgreSQL path (e.g. temporary test .db path) -> (False, str(db_path))
    - Default (production app flow when db_path is None or "") -> (True, DATABASE_URL)
    """
    _load_env_if_needed()
    if db_path and (str(db_path).startswith("postgresql://") or str(db_path).startswith("postgres://")):
        return True, str(db_path)

    test_pg = (os.getenv("TEST_DATABASE_URL") or "").strip()
    if test_pg and (db_path in (":test:", "test") or os.getenv("STATEMENT_PARSER_TEST_MODE") == "1"):
        return True, test_pg

    # In test mode, never fall back to production DATABASE_URL
    if os.getenv("STATEMENT_PARSER_TEST_MODE") == "1":
        return False, str(db_path or ":memory:")

    # Explicit non-PostgreSQL target passed (e.g. tests passing temp .db files or :memory:)
    if db_path is not None and db_path != "":
        return False, str(db_path)

    # Production default: connect to configured PostgreSQL instance
    env_url = (os.getenv("DATABASE_URL") or "").strip()
    if env_url:
        return True, env_url
    return False, ":memory:"


class MockStatementStore:
    """Isolated, thread-safe in-memory store for unit and edge-case testing."""

    def __init__(self, target: str = ":memory:"):
        self.target = target
        self.db_path = target
        self.is_pg = False
        self.is_mock = True
        self._state = _get_mock_state(target)

    def save_result(self, result: ExtractionResult, sha256_hash: str = "") -> int:
        effective_file = result.file or ""
        if effective_file and os.path.isfile(effective_file):
            if not self.save_statement_artifact(effective_file):
                raise RuntimeError("Could not persist the source statement artifact")

        file_sha = sha256_hash or result.sha256_hash or ""
        if not file_sha and effective_file and os.path.exists(effective_file):
            try:
                file_sha = compute_sha256(effective_file)
            except Exception:
                file_sha = ""
        result.sha256_hash = file_sha

        with self._state.lock:
            self._state._stmt_id_seq += 1
            new_id = self._state._stmt_id_seq
            row = {
                "id": new_id,
                "file_path": effective_file,
                "sha256_hash": file_sha,
                "format_id": result.format_id,
                "status": result.status,
                "issues_json": json.dumps(result.issues or []),
                "llm_calls_made": result.llm_calls_made,
                "record_json": result.model_dump_json(),
                "diagnostic_message": getattr(result, "notes", "") or getattr(result, "diagnostic_message", "") or "",
                "created_at": datetime.now(timezone.utc).isoformat()
            }
            self._state.statements.append(row)
            return new_id

    def get_seen_hashes(self) -> Dict[str, str]:
        with self._state.lock:
            return {
                row["sha256_hash"]: row["file_path"]
                for row in self._state.statements
                if row.get("status") != "duplicate" and row.get("sha256_hash")
            }

    def get_seen_revisions(self) -> Dict[Tuple[str, str, str], str]:
        with self._state.lock:
            revs = {}
            for row in self._state.statements:
                if row.get("status") == "duplicate" or not row.get("record_json"):
                    continue
                try:
                    data = json.loads(row["record_json"])
                    for stmt_block in data.get("statements", []):
                        st_meta = stmt_block.get("statement", {})
                        prov = st_meta.get("provider") or row.get("format_id")
                        cid = st_meta.get("client_id")
                        pend = st_meta.get("period_end")
                        if prov and cid and pend and row.get("sha256_hash"):
                            revs[(prov, cid, pend)] = row["sha256_hash"]
                except Exception:
                    pass
            return revs

    def sync_to_pipeline(self, pipeline: Any):
        seen_hashes = self.get_seen_hashes()
        for h, fpath in seen_hashes.items():
            if h not in pipeline.ingestion.seen_hashes:
                pipeline.ingestion.seen_hashes[h] = fpath

        seen_revs = self.get_seen_revisions()
        for k, h in seen_revs.items():
            if k not in pipeline.ingestion.seen_statements:
                pipeline.ingestion.seen_statements[k] = h

        with self._state.lock:
            sorted_stmts = sorted(self._state.statements, key=lambda r: r["id"])
            for row in sorted_stmts:
                if row.get("status") == "duplicate":
                    continue
                fpath = row.get("file_path")
                rec_json = row.get("record_json")
                if fpath and rec_json and fpath not in pipeline.processed_history:
                    try:
                        pipeline.processed_history[fpath] = ExtractionResult.model_validate_json(rec_json)
                    except Exception:
                        pass

    def get_all(self) -> List[Dict[str, Any]]:
        with self._state.lock:
            return sorted([dict(r) for r in self._state.statements], key=lambda r: r["id"], reverse=True)

    def get_needs_review(self) -> List[Dict[str, Any]]:
        with self._state.lock:
            rows = [dict(r) for r in self._state.statements if r.get("status") == "needs_review"]
            for r in rows:
                restored_path = self.restore_statement_artifact(r.get("file_path", ""))
                if restored_path:
                    r["file_path"] = restored_path
            return sorted(rows, key=lambda r: r["id"], reverse=True)

    def save_statement_artifact(self, file_path: str) -> bool:
        if not file_path or not os.path.exists(file_path):
            return False
        try:
            with open(file_path, "rb") as f:
                content = f.read()
            sha = hashlib.sha256(content).hexdigest()
            fname = os.path.basename(file_path)
            with self._state.lock:
                self._state.statement_artifacts[file_path] = {
                    "file_path": file_path,
                    "sha256_hash": sha,
                    "file_name": fname,
                    "content": content,
                    "created_at": datetime.now(timezone.utc).isoformat()
                }
            return True
        except Exception:
            return False

    def restore_statement_artifact(self, file_path: str) -> Optional[str]:
        if not file_path:
            return None
        if os.path.isfile(file_path):
            return file_path
        with self._state.lock:
            art = self._state.statement_artifacts.get(file_path)
            if not art:
                return None
            digest = art["sha256_hash"]
            suffix = Path(art["file_name"]).suffix
            restore_dir = os.path.join(tempfile.gettempdir(), "activv_statement_artifacts")
            os.makedirs(restore_dir, exist_ok=True)
            restored_path = os.path.join(restore_dir, f"{digest}{suffix}")
            if not os.path.isfile(restored_path):
                temp_path = f"{restored_path}.tmp"
                with open(temp_path, "wb") as stream:
                    stream.write(bytes(art["content"]))
                os.replace(temp_path, restored_path)
            return restored_path

    def save_template(self, template: Dict[str, Any]) -> None:
        fmt_id = str(template.get("format_id") or "").strip()
        if not fmt_id:
            raise ValueError("Template must have format_id")
        with self._state.lock:
            self._state.format_templates[fmt_id] = {
                "format_id": fmt_id,
                "template_json": json.dumps(template),
                "updated_at": datetime.now(timezone.utc).isoformat()
            }

    def get_template(self, format_id: str) -> Optional[Dict[str, Any]]:
        with self._state.lock:
            row = self._state.format_templates.get(format_id)
            if not row:
                return None
            return json.loads(row["template_json"])

    def get_templates(self) -> List[Dict[str, Any]]:
        with self._state.lock:
            return [json.loads(r["template_json"]) for r in self._state.format_templates.values()]

    def delete_templates(self) -> int:
        with self._state.lock:
            count = len(self._state.format_templates)
            self._state.format_templates.clear()
            return count

    def get_validated(self) -> List[Dict[str, Any]]:
        with self._state.lock:
            return sorted(
                [dict(r) for r in self._state.statements if r.get("status") == "validated"],
                key=lambda r: r["id"],
                reverse=True
            )

    def get_by_hash(self, sha256_hash: str) -> Optional[Dict[str, Any]]:
        with self._state.lock:
            for r in reversed(self._state.statements):
                if r.get("sha256_hash") == sha256_hash:
                    return dict(r)
            return None

    def get_by_id(self, db_id: int) -> Optional[Dict[str, Any]]:
        with self._state.lock:
            for r in self._state.statements:
                if r.get("id") == db_id:
                    return dict(r)
            return None

    def update_statement_status(self, db_id: int, result: ExtractionResult):
        with self._state.lock:
            for r in self._state.statements:
                if r.get("id") == db_id:
                    r["status"] = result.status
                    r["issues_json"] = json.dumps(result.issues or [])
                    r["format_id"] = result.format_id
                    r["llm_calls_made"] = result.llm_calls_made
                    r["record_json"] = result.model_dump_json()
                    r["diagnostic_message"] = getattr(result, "notes", "") or getattr(result, "diagnostic_message", "") or ""
                    break

    def save_email_event(
        self,
        message_id: str,
        subject: Optional[str] = None,
        sender: Optional[str] = None,
        received_at: Optional[str] = None,
        status: str = "ingest",
        reason: Optional[str] = None,
        attachments: Optional[List[str]] = None,
        processing_state: str = "completed"
    ) -> int:
        clean_msg_id = message_id.strip("<>")
        with self._state.lock:
            self._state._email_id_seq += 1
            new_id = self._state._email_id_seq
            row = {
                "id": new_id,
                "message_id": clean_msg_id,
                "subject": subject,
                "sender": sender,
                "received_at": received_at,
                "status": status,
                "reason": reason,
                "attachments_json": json.dumps(attachments or []),
                "processing_state": processing_state,
                "processed_at": datetime.now(timezone.utc).isoformat()
            }
            self._state.email_events.append(row)
            return new_id

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
        clean_msg_id = (message_id or "").strip("<>")
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        initial_state = "started" if status == "ingest" else "completed"
        with self._state.lock:
            existing = None
            for r in reversed(self._state.email_events):
                if r.get("message_id") == clean_msg_id:
                    existing = r
                    break
            if existing:
                state = existing.get("processing_state")
                if state == "completed":
                    return "completed"
                if state == "started":
                    try:
                        claimed_at = datetime.fromisoformat(existing.get("processed_at", ""))
                        if claimed_at.tzinfo is None:
                            claimed_at = claimed_at.replace(tzinfo=timezone.utc)
                        is_stale = (now - claimed_at).total_seconds() >= stale_after_seconds
                    except (TypeError, ValueError):
                        is_stale = True
                    if not is_stale:
                        return "in_progress"
                existing.update({
                    "subject": subject,
                    "sender": sender,
                    "received_at": received_at,
                    "status": status,
                    "reason": reason,
                    "attachments_json": json.dumps(attachments or []),
                    "processing_state": initial_state,
                    "processed_at": now_iso
                })
                return "claimed"

            self._state._email_id_seq += 1
            new_id = self._state._email_id_seq
            row = {
                "id": new_id,
                "message_id": clean_msg_id,
                "subject": subject,
                "sender": sender,
                "received_at": received_at,
                "status": status,
                "reason": reason,
                "attachments_json": json.dumps(attachments or []),
                "processing_state": initial_state,
                "processed_at": now_iso
            }
            self._state.email_events.append(row)
            return "claimed"

    def update_email_event_processing_state(self, message_id: str, processing_state: str, reason: str = ""):
        clean_msg_id = message_id.strip("<>")
        with self._state.lock:
            for r in reversed(self._state.email_events):
                if r.get("message_id") == clean_msg_id:
                    r["processing_state"] = processing_state
                    if reason:
                        r["reason"] = reason
                    r["processed_at"] = datetime.now(timezone.utc).isoformat()
                    break

    def update_email_event_status(self, message_id: str, status: str, reason: str = ""):
        clean_msg_id = message_id.strip("<>")
        with self._state.lock:
            for r in reversed(self._state.email_events):
                if r.get("message_id") == clean_msg_id:
                    r["status"] = status
                    if reason:
                        r["reason"] = reason
                    r["processed_at"] = datetime.now(timezone.utc).isoformat()
                    break

    def has_email_event(self, message_id: str) -> bool:
        clean_msg_id = message_id.strip("<>")
        with self._state.lock:
            for r in reversed(self._state.email_events):
                if r.get("message_id") == clean_msg_id:
                    return r.get("processing_state") != "failed"
            return False

    def get_email_events(self) -> List[Dict[str, Any]]:
        with self._state.lock:
            return sorted([dict(r) for r in self._state.email_events], key=lambda r: r["id"], reverse=True)

    def get_email_event_by_message_id(self, message_id: str) -> Optional[Dict[str, Any]]:
        clean_msg_id = message_id.strip("<>")
        with self._state.lock:
            for r in reversed(self._state.email_events):
                if r.get("message_id") == clean_msg_id:
                    return dict(r)
            return None

    def save_failed_learning(
        self,
        format_id: str,
        provider_key: str = "",
        layout_kind: str = "",
        fingerprint_hash: str = "",
        failure_issue: str = "",
        diagnostic_message: str = ""
    ) -> int:
        clean_fhash = fingerprint_hash or ""
        with self._state.lock:
            for r in self._state.failed_learning_attempts:
                if r.get("format_id") == format_id and (r.get("fingerprint_hash") or "") == clean_fhash:
                    return r["id"]
            self._state._failed_id_seq += 1
            new_id = self._state._failed_id_seq
            row = {
                "id": new_id,
                "format_id": format_id,
                "provider_key": provider_key,
                "layout_kind": layout_kind,
                "fingerprint_hash": clean_fhash,
                "failure_issue": failure_issue,
                "diagnostic_message": diagnostic_message,
                "failed_at": datetime.now(timezone.utc).isoformat()
            }
            self._state.failed_learning_attempts.append(row)
            return new_id

    def has_failed_learning(self, format_id: str, fingerprint_hash: str = "") -> Optional[Dict[str, Any]]:
        clean_fhash = fingerprint_hash or ""
        with self._state.lock:
            for r in reversed(self._state.failed_learning_attempts):
                if r.get("format_id") == format_id and (not clean_fhash or r.get("fingerprint_hash") == clean_fhash):
                    return dict(r)
            return None

    def get_failed_learnings(self) -> List[Dict[str, Any]]:
        with self._state.lock:
            return sorted([dict(r) for r in self._state.failed_learning_attempts], key=lambda r: r["id"], reverse=True)

    def remove_failed_learning(self, format_id: str, fingerprint_hash: str = ""):
        clean_fhash = fingerprint_hash or ""
        with self._state.lock:
            self._state.failed_learning_attempts = [
                r for r in self._state.failed_learning_attempts
                if not (r.get("format_id") == format_id and (not clean_fhash or r.get("fingerprint_hash") == clean_fhash))
            ]

    def clear_transient_failed_learnings(self):
        with self._state.lock:
            self._state.failed_learning_attempts = [
                r for r in self._state.failed_learning_attempts
                if r.get("failure_issue") != "learning_failed_llm_unavailable"
            ]

    def supersede_needs_review(self, file_path: str, exclude_id: Optional[int] = None):
        target_norm = os.path.normpath(file_path)
        with self._state.lock:
            for r in self._state.statements:
                fp = r.get("file_path")
                if fp and os.path.normpath(fp) == target_norm:
                    if r.get("status") == "needs_review" and (exclude_id is None or r.get("id") != exclude_id):
                        r["status"] = "superseded_by_reprocess"

    def clear(self):
        self._state.clear()


def _get_pg_conn(conn_str: str):
    if not psycopg2:
        raise ImportError("psycopg2 is required for PostgreSQL. Install via: pip install psycopg2-binary")
    clean = conn_str.replace("&channel_binding=require", "").replace("channel_binding=require&", "")
    conn = psycopg2.connect(clean, cursor_factory=psycopg2.extras.RealDictCursor)
    _create_tables_pg(conn)
    return conn


def get_db_connection(db_path: Optional[str] = None):
    """Creates a connection to PostgreSQL database and ensures schema exists."""
    _, target = _is_pg_target(db_path)
    return _get_pg_conn(target)


def _create_tables_pg(conn):
    """Initializes tables and indexes for PostgreSQL."""
    with conn.cursor() as cursor:
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS statements (
            id SERIAL PRIMARY KEY,
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
        CREATE INDEX IF NOT EXISTS idx_statements_hash ON statements(sha256_hash);
        CREATE INDEX IF NOT EXISTS idx_statements_status ON statements(status);

        CREATE TABLE IF NOT EXISTS format_templates (
            format_id TEXT PRIMARY KEY,
            template_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS statement_artifacts (
            file_path TEXT PRIMARY KEY,
            sha256_hash TEXT NOT NULL,
            file_name TEXT NOT NULL,
            content BYTEA NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS email_events (
            id SERIAL PRIMARY KEY,
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
        CREATE INDEX IF NOT EXISTS idx_email_events_msg_id ON email_events(message_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_email_events_message_unique ON email_events(message_id);
        CREATE INDEX IF NOT EXISTS idx_email_events_status ON email_events(status);

        CREATE TABLE IF NOT EXISTS failed_learning_attempts (
            id SERIAL PRIMARY KEY,
            format_id TEXT NOT NULL,
            provider_key TEXT,
            layout_kind TEXT,
            fingerprint_hash TEXT,
            failure_issue TEXT NOT NULL,
            diagnostic_message TEXT,
            failed_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_failed_learning_fmt ON failed_learning_attempts(format_id);
        """)
        conn.commit()


class StatementStore:
    """Interface for saving, querying, and syncing parsed statements across restarts via PostgreSQL."""

    def __init__(self, db_path: Optional[str] = None):
        self.is_pg, self.target = _is_pg_target(db_path)
        self.db_path = self.target
        self.is_mock = not self.is_pg
        if self.is_mock:
            self._mock = MockStatementStore(self.target)
            self._state = self._mock._state
        else:
            with closing(self._get_connection()) as conn:
                pass

    def _get_connection(self):
        return _get_pg_conn(self.target)

    def _execute(self, cursor, sql: str, params: tuple = ()):
        cursor.execute(sql.replace("?", "%s"), params)

    def _insert(self, conn, cursor, sql: str, params: tuple = ()) -> int:
        cursor.execute(sql.replace("?", "%s") + " RETURNING id", params)
        row = cursor.fetchone()
        conn.commit()
        return row["id"] if isinstance(row, dict) else row[0]


    def save_result(self, result: ExtractionResult, sha256_hash: str = "") -> int:
        """Saves an ExtractionResult into the database with its SHA-256 content hash."""
        if self.is_mock:
            return self._mock.save_result(result, sha256_hash=sha256_hash)

        if result.file and os.path.isfile(result.file):
            if not self.save_statement_artifact(result.file):
                raise RuntimeError("Could not persist the source statement artifact")

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

        with closing(self._get_connection()) as conn:
            cursor = conn.cursor()
            sql = """
                INSERT INTO statements (
                    file_path, sha256_hash, format_id, status,
                    issues_json, llm_calls_made, record_json, diagnostic_message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            return self._insert(conn, cursor, sql, (
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

    def get_seen_hashes(self) -> Dict[str, str]:
        """Returns {sha256_hash: file_path} for all previously ingested non-duplicate files."""
        if self.is_mock:
            return self._mock.get_seen_hashes()

        seen: Dict[str, str] = {}
        with closing(self._get_connection()) as conn:
            cursor = conn.cursor()
            self._execute(
                cursor,
                "SELECT sha256_hash, file_path FROM statements WHERE sha256_hash != '' AND status IN ('validated', 'not_a_statement') ORDER BY id ASC"
            )
            for row in cursor.fetchall():
                h = row["sha256_hash"]
                if h and h not in seen:
                    seen[h] = row["file_path"]
        return seen

    def get_seen_revisions(self) -> Dict[Tuple[str, str, str], str]:
        """Returns {(provider_or_format, client_id, period_end): sha256_hash} from persisted records."""
        if self.is_mock:
            return self._mock.get_seen_revisions()

        revisions: Dict[Tuple[str, str, str], str] = {}
        with closing(self._get_connection()) as conn:
            cursor = conn.cursor()
            self._execute(
                cursor,
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
        if self.is_mock:
            return self._mock.sync_to_pipeline(pipeline)

        seen_hashes = self.get_seen_hashes()
        for h, fpath in seen_hashes.items():
            if h not in pipeline.ingestion.seen_hashes:
                pipeline.ingestion.seen_hashes[h] = fpath

        seen_revs = self.get_seen_revisions()
        for k, h in seen_revs.items():
            if k not in pipeline.ingestion.seen_statements:
                pipeline.ingestion.seen_statements[k] = h

        with closing(self._get_connection()) as conn:
            cursor = conn.cursor()
            self._execute(
                cursor,
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
        """Retrieves all parsed statements."""
        if self.is_mock:
            return self._mock.get_all()

        with closing(self._get_connection()) as conn:
            cursor = conn.cursor()
            self._execute(cursor, "SELECT * FROM statements ORDER BY id DESC")
            return [dict(row) for row in cursor.fetchall()]

    def get_needs_review(self) -> List[Dict[str, Any]]:
        """Retrieves all quarantined statements needing human intervention."""
        if self.is_mock:
            return self._mock.get_needs_review()

        with closing(self._get_connection()) as conn:
            cursor = conn.cursor()
            self._execute(cursor, "SELECT * FROM statements WHERE status = 'needs_review' ORDER BY id DESC")
            rows = [dict(row) for row in cursor.fetchall()]

        for row in rows:
            restored_path = self.restore_statement_artifact(row.get("file_path", ""))
            if restored_path:
                row["file_path"] = restored_path
        return rows

    def save_statement_artifact(self, file_path: str) -> bool:
        """Stores an ingested source file in the configured database for restart-safe review."""
        if self.is_mock:
            return self._mock.save_statement_artifact(file_path)

        if not file_path or not os.path.isfile(file_path):
            return False
        try:
            with open(file_path, "rb") as stream:
                content = stream.read()
            digest = hashlib.sha256(content).hexdigest()
            file_name = os.path.basename(file_path)
            now_iso = datetime.now(timezone.utc).isoformat()
            with closing(self._get_connection()) as conn:
                cursor = conn.cursor()
                self._execute(cursor, """
                    INSERT INTO statement_artifacts (file_path, sha256_hash, file_name, content, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(file_path) DO UPDATE SET
                        sha256_hash = excluded.sha256_hash,
                        file_name = excluded.file_name,
                        content = excluded.content,
                        created_at = excluded.created_at
                """, (file_path, digest, file_name, content, now_iso))
                conn.commit()
            return True
        except Exception:
            return False

    def restore_statement_artifact(self, file_path: str) -> Optional[str]:
        """Returns an existing source path or materializes its durable DB copy to temp storage."""
        if self.is_mock:
            return self._mock.restore_statement_artifact(file_path)

        if not file_path:
            return None
        if os.path.isfile(file_path):
            return file_path

        with closing(self._get_connection()) as conn:
            cursor = conn.cursor()
            self._execute(cursor, """
                SELECT sha256_hash, file_name, content
                FROM statement_artifacts WHERE file_path = ?
            """, (file_path,))
            row = cursor.fetchone()
        if not row:
            return None

        digest = row["sha256_hash"]
        suffix = Path(row["file_name"]).suffix
        restore_dir = os.path.join(tempfile.gettempdir(), "activv_statement_artifacts")
        os.makedirs(restore_dir, exist_ok=True)
        restored_path = os.path.join(restore_dir, f"{digest}{suffix}")
        if not os.path.isfile(restored_path):
            temp_path = f"{restored_path}.tmp"
            with open(temp_path, "wb") as stream:
                stream.write(bytes(row["content"]))
            os.replace(temp_path, restored_path)
        return restored_path

    def save_template(self, template: Dict[str, Any]) -> None:
        """Persists a validated format template in the configured database."""
        if self.is_mock:
            return self._mock.save_template(template)

        format_id = str(template.get("format_id") or "").strip()
        if not format_id:
            raise ValueError("A format template must include format_id")
        now_iso = datetime.now(timezone.utc).isoformat()
        with closing(self._get_connection()) as conn:
            cursor = conn.cursor()
            self._execute(cursor, """
                INSERT INTO format_templates (format_id, template_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(format_id) DO UPDATE SET
                    template_json = excluded.template_json,
                    updated_at = excluded.updated_at
            """, (format_id, json.dumps(template), now_iso))
            conn.commit()

    def get_template(self, format_id: str) -> Optional[Dict[str, Any]]:
        if self.is_mock:
            return self._mock.get_template(format_id)

        with closing(self._get_connection()) as conn:
            cursor = conn.cursor()
            self._execute(cursor, "SELECT template_json FROM format_templates WHERE format_id = ?", (format_id,))
            row = cursor.fetchone()
        return json.loads(row["template_json"]) if row else None

    def get_templates(self) -> List[Dict[str, Any]]:
        if self.is_mock:
            return self._mock.get_templates()

        with closing(self._get_connection()) as conn:
            cursor = conn.cursor()
            self._execute(cursor, "SELECT template_json FROM format_templates ORDER BY format_id")
            rows = cursor.fetchall()
        return [json.loads(row["template_json"]) for row in rows]

    def delete_templates(self) -> int:
        if self.is_mock:
            return self._mock.delete_templates()

        with closing(self._get_connection()) as conn:
            cursor = conn.cursor()
            self._execute(cursor, "DELETE FROM format_templates")
            count = cursor.rowcount
            conn.commit()
        return count

    def get_validated(self) -> List[Dict[str, Any]]:
        """Retrieves all successfully validated statements."""
        if self.is_mock:
            return self._mock.get_validated()

        with closing(self._get_connection()) as conn:
            cursor = conn.cursor()
            self._execute(cursor, "SELECT * FROM statements WHERE status = 'validated' ORDER BY id DESC")
            return [dict(row) for row in cursor.fetchall()]

    def get_by_hash(self, sha256_hash: str) -> Optional[Dict[str, Any]]:
        """Retrieves a statement by its content hash to prevent duplicate processing."""
        if self.is_mock:
            return self._mock.get_by_hash(sha256_hash)

        if not sha256_hash:
            return None
        with closing(self._get_connection()) as conn:
            cursor = conn.cursor()
            self._execute(cursor, "SELECT * FROM statements WHERE sha256_hash = ? ORDER BY id ASC LIMIT 1", (sha256_hash,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_by_id(self, db_id: int) -> Optional[Dict[str, Any]]:
        """Retrieves a statement by its primary key ID."""
        if self.is_mock:
            return self._mock.get_by_id(db_id)

        if not db_id:
            return None
        with closing(self._get_connection()) as conn:
            cursor = conn.cursor()
            self._execute(cursor, "SELECT * FROM statements WHERE id = ? LIMIT 1", (db_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def update_statement_status(self, db_id: int, result: ExtractionResult):
        """Updates an existing statement row after unlocking/reprocessing."""
        if self.is_mock:
            return self._mock.update_statement_status(db_id, result)

        with closing(self._get_connection()) as conn:
            cursor = conn.cursor()
            issues_str = json.dumps(result.issues or [])
            record_str = result.model_dump_json()
            diag_msg = result.diagnostic_message or result.notes or ""
            self._execute(cursor, """
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
        if self.is_mock:
            return self._mock.save_email_event(
                message_id=message_id,
                subject=subject,
                sender=sender,
                status=status,
                reason=reason,
                attachments=attachments,
                received_at=received_at,
                processing_state=processing_state
            )

        now_iso = datetime.now(timezone.utc).isoformat()
        att_json = json.dumps(attachments or [])
        clean_msg_id = (message_id or "").strip()
        eff_state = processing_state if processing_state else ("completed" if status in ("ignore", "needs_review") else "started")
        with closing(self._get_connection()) as conn:
            cursor = conn.cursor()
            sql = """
                INSERT INTO email_events (
                    message_id, subject, sender, received_at,
                    status, reason, attachments_json, processing_state, processed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            return self._insert(conn, cursor, sql, (
                clean_msg_id, subject, sender, received_at, status, reason, att_json, eff_state, now_iso
            ))

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
        """Atomically claim an email for one worker."""
        if self.is_mock:
            return self._mock.claim_email_event(
                message_id=message_id,
                subject=subject,
                sender=sender,
                status=status,
                reason=reason,
                attachments=attachments,
                received_at=received_at,
                stale_after_seconds=stale_after_seconds
            )

        clean_msg_id = (message_id or "").strip()
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        initial_state = "started" if status == "ingest" else "completed"
        with closing(self._get_connection()) as conn:
            cursor = conn.cursor()
            sel_sql = "SELECT id, processing_state, processed_at FROM email_events WHERE message_id = ? ORDER BY id DESC LIMIT 1 FOR UPDATE"
            self._execute(cursor, sel_sql, (clean_msg_id,))
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
                self._execute(
                    cursor,
                    "UPDATE email_events SET subject = ?, sender = ?, received_at = ?, status = ?, reason = ?, attachments_json = ?, processing_state = ?, processed_at = ? WHERE id = ?",
                    (subject, sender, received_at, status, reason, json.dumps(attachments or []), initial_state, now_iso, existing["id"]),
                )
                conn.commit()
                return "claimed"

            self._execute(
                cursor,
                "INSERT INTO email_events (message_id, subject, sender, received_at, status, reason, attachments_json, processing_state, processed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (clean_msg_id, subject, sender, received_at, status, reason, json.dumps(attachments or []), initial_state, now_iso),
            )
            conn.commit()
            return "claimed"

    def update_email_event_processing_state(self, message_id: str, processing_state: str, reason: str = ""):
        """Updates the processing_state and reason of an existing email event."""
        if self.is_mock:
            return self._mock.update_email_event_processing_state(message_id, processing_state, reason)

        if not message_id:
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        with closing(self._get_connection()) as conn:
            cursor = conn.cursor()
            self._execute(cursor, """
                UPDATE email_events
                SET processing_state = ?, reason = CASE WHEN ? != '' THEN ? ELSE reason END, processed_at = ?
                WHERE message_id = ?
            """, (processing_state, reason, reason, now_iso, message_id.strip()))
            conn.commit()

    def update_email_event_status(self, message_id: str, status: str, reason: str = ""):
        """Updates the status and reason of an existing email event."""
        if self.is_mock:
            return self._mock.update_email_event_status(message_id, status, reason)

        if not message_id:
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        with closing(self._get_connection()) as conn:
            cursor = conn.cursor()
            self._execute(cursor, """
                UPDATE email_events
                SET status = ?, reason = CASE WHEN ? != '' THEN ? ELSE reason END, processed_at = ?
                WHERE message_id = ?
            """, (status, reason, reason, now_iso, message_id.strip()))
            conn.commit()

    def has_email_event(self, message_id: str) -> bool:
        """Checks if an email with the given message_id was already successfully processed."""
        if self.is_mock:
            return self._mock.has_email_event(message_id)

        if not message_id:
            return False
        with closing(self._get_connection()) as conn:
            cursor = conn.cursor()
            self._execute(
                cursor,
                "SELECT id FROM email_events WHERE message_id = ? AND processing_state = 'completed' LIMIT 1",
                (message_id.strip(),)
            )
            return cursor.fetchone() is not None

    def get_email_events(self) -> List[Dict[str, Any]]:
        """Retrieves all email ingestion events ordered chronologically."""
        if self.is_mock:
            return self._mock.get_email_events()

        with closing(self._get_connection()) as conn:
            cursor = conn.cursor()
            self._execute(cursor, "SELECT * FROM email_events ORDER BY id DESC")
            return [dict(row) for row in cursor.fetchall()]

    def get_email_event_by_message_id(self, message_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves email event record by message_id."""
        if self.is_mock:
            return self._mock.get_email_event_by_message_id(message_id)

        if not message_id:
            return None
        with closing(self._get_connection()) as conn:
            cursor = conn.cursor()
            self._execute(cursor, "SELECT * FROM email_events WHERE message_id = ? LIMIT 1", (message_id.strip(),))
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
        if self.is_mock:
            return self._mock.save_failed_learning(
                format_id=format_id,
                provider_key=provider_key,
                layout_kind=layout_kind,
                fingerprint_hash=fingerprint_hash,
                failure_issue=failure_issue,
                diagnostic_message=diagnostic_message
            )

        now_iso = datetime.now(timezone.utc).isoformat()
        with closing(self._get_connection()) as conn:
            cursor = conn.cursor()
            sql = """
                INSERT INTO failed_learning_attempts (
                    format_id, provider_key, layout_kind, fingerprint_hash,
                    failure_issue, diagnostic_message, failed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """
            return self._insert(conn, cursor, sql, (
                format_id, provider_key, layout_kind, fingerprint_hash, failure_issue, diagnostic_message, now_iso
            ))

    def has_failed_learning(self, format_id: str, fingerprint_hash: str = "") -> Optional[Dict[str, Any]]:
        """Checks if a format_id or fingerprint hash previously failed learning."""
        if self.is_mock:
            return self._mock.has_failed_learning(format_id=format_id, fingerprint_hash=fingerprint_hash)

        with closing(self._get_connection()) as conn:
            cursor = conn.cursor()
            if fingerprint_hash:
                self._execute(
                    cursor,
                    "SELECT * FROM failed_learning_attempts WHERE format_id = ? AND fingerprint_hash = ? ORDER BY id DESC LIMIT 1",
                    (format_id, fingerprint_hash)
                )
            else:
                self._execute(
                    cursor,
                    "SELECT * FROM failed_learning_attempts WHERE format_id = ? ORDER BY id DESC LIMIT 1",
                    (format_id,)
                )
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_failed_learnings(self) -> List[Dict[str, Any]]:
        """Retrieves all persisted failed learning attempts."""
        if self.is_mock:
            return self._mock.get_failed_learnings()

        with closing(self._get_connection()) as conn:
            cursor = conn.cursor()
            self._execute(cursor, "SELECT * FROM failed_learning_attempts ORDER BY id DESC")
            return [dict(row) for row in cursor.fetchall()]

    def remove_failed_learning(self, format_id: str, fingerprint_hash: str = ""):
        """Removes failed learning records for a format_id or fingerprint hash."""
        if self.is_mock:
            return self._mock.remove_failed_learning(format_id=format_id, fingerprint_hash=fingerprint_hash)

        with closing(self._get_connection()) as conn:
            cursor = conn.cursor()
            if fingerprint_hash:
                self._execute(
                    cursor,
                    "DELETE FROM failed_learning_attempts WHERE format_id = ? AND fingerprint_hash = ?",
                    (format_id, fingerprint_hash)
                )
            else:
                self._execute(
                    cursor,
                    "DELETE FROM failed_learning_attempts WHERE format_id = ?",
                    (format_id,)
                )
            conn.commit()

    def clear_transient_failed_learnings(self):
        """Cleans up any transient server-outage failures from the persistence store."""
        if self.is_mock:
            return self._mock.clear_transient_failed_learnings()

        with closing(self._get_connection()) as conn:
            cursor = conn.cursor()
            self._execute(
                cursor,
                "DELETE FROM failed_learning_attempts WHERE failure_issue = 'learning_failed_llm_unavailable'"
            )
            conn.commit()

    def supersede_needs_review(self, file_path: str, exclude_id: Optional[int] = None):
        """Marks any prior 'needs_review' records for the given file as superseded."""
        if self.is_mock:
            return self._mock.supersede_needs_review(file_path=file_path, exclude_id=exclude_id)

        with closing(self._get_connection()) as conn:
            cursor = conn.cursor()
            if exclude_id is not None:
                self._execute(
                    cursor,
                    "UPDATE statements SET status = 'superseded_by_reprocess' WHERE file_path = ? AND status = 'needs_review' AND id != ?",
                    (file_path, exclude_id)
                )
            else:
                self._execute(
                    cursor,
                    "UPDATE statements SET status = 'superseded_by_reprocess' WHERE file_path = ? AND status = 'needs_review'",
                    (file_path,)
                )
            conn.commit()


    def clear(self):
        """Clears all records (useful for test resets)."""
        if self.is_mock:
            return self._mock.clear()

        with closing(self._get_connection()) as conn:
            cursor = conn.cursor()
            cursor.execute("TRUNCATE TABLE statements, statement_artifacts, email_events, failed_learning_attempts, format_templates RESTART IDENTITY CASCADE")
            conn.commit()

