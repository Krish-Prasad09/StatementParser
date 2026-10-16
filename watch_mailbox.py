"""
watch_mailbox.py
----------------
IMAP Mailbox Poller & Background Ingestion Worker.
Connects to an IMAP server over SSL, monitors for new emails with financial statement attachments,
ingests and parses them through the ProcessingPipeline, and persists results to SQLite.

Environment variables:
  IMAP_HOST: IMAP server hostname (e.g. imap.gmail.com)
  IMAP_PORT: IMAP port (default: 993)
  IMAP_USER: Username or email address
  IMAP_PASSWORD: Password or application-specific password
  IMAP_FOLDER: Mailbox folder to monitor (default: INBOX)
  POLL_INTERVAL_SECONDS: Polling interval in seconds (default: 60)
"""

import os
import sys
import time
import argparse
import imaplib
from typing import List, Dict, Any, Optional

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from pipeline import ProcessingPipeline
from persistence import StatementStore
from ingestion import IngestionEngine
from utils import compute_sha256


class EmailProcessingInProgress(Exception):
    """Raised when another worker currently owns the same email claim."""


def _load_local_env():
    """Loads the project's .env values without overriding explicit environment settings."""
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_file):
        return
    try:
        with open(env_file, "r", encoding="utf-8") as ef:
            for eline in ef:
                eline = eline.strip()
                if eline and not eline.startswith("#") and "=" in eline:
                    ek, ev = eline.split("=", 1)
                    ek, ev = ek.strip(), ev.strip().strip("\"'")
                    if ek and not os.environ.get(ek):
                        os.environ[ek] = ev
    except OSError:
        # Missing/unreadable optional local configuration is handled by the caller.
        pass


def process_eml_file(
    eml_path: str,
    pipeline: ProcessingPipeline,
    store: StatementStore,
    target_dir: str = "ingested_attachments"
) -> List[Dict[str, Any]]:
    """
    Processes an EML file via IngestionEngine, persists email_events to the database,
    filters noise (newsletters, factsheets), routes unsupported/ambiguous files to needs_review,
    syncs seen hashes and revisions from the database before parsing, runs pipeline,
    and persists ExtractionResult with its real SHA-256 hash to the database.
    """
    _load_local_env()
    store.sync_to_pipeline(pipeline)

    ingestion = IngestionEngine()
    email_res = ingestion.parse_eml_email(eml_path, extract_dir=target_dir)

    msg_id = email_res.get("message_id") or f"eml_{os.path.basename(eml_path)}"
    sender = email_res.get("sender", "Unknown")
    subject = email_res.get("subject", "Unknown")
    status = email_res.get("status", "ignore")
    reason = email_res.get("reason", "")
    att_filenames = [a["filename"] for a in email_res.get("attachments", [])]

    print(f"\nProcessing email: {os.path.basename(eml_path)}")
    print(f"  From:    {sender}")
    print(f"  Subject: {subject}")
    print(f"  Status:  {status}")

    claim = store.claim_email_event(
        message_id=msg_id,
        subject=subject,
        sender=sender,
        status=status,
        reason=reason,
        attachments=att_filenames,
        received_at=email_res.get("date", ""),
    )
    if claim == "completed":
        print(f"  [SKIPPED] Email message_id '{msg_id}' already processed in database.")
        return []
    if claim == "in_progress":
        raise EmailProcessingInProgress(f"Email message_id '{msg_id}' is already being processed.")

    if status == "ignore":
        print(f"  [SKIPPED] Email ignored: reason='{reason or 'non_statement'}'")
        return []

    processed = []

    # If email requires review (e.g. .zip, .xls, .xlsm, inline pdf missing filename, or ambiguous document)
    if status == "needs_review":
        print(f"  [QUARANTINED] Email routed to needs_review: reason='{reason}'")
        from models import ExtractionResult
        for att in email_res.get("attachments", []):
            att_path = att["path"]
            file_sha = compute_sha256(att_path) if os.path.exists(att_path) else ""
            res = ExtractionResult(
                file=att_path,
                format_id="unsupported_or_ambiguous",
                status="needs_review",
                issues=[reason or "ambiguous_attachment_requires_review"],
                statements=[],
                diagnostic_message=f"Email attachment quarantined: '{subject}' ({reason})",
                llm_calls_made=0,
                sha256_hash=file_sha
            )
            record_id = store.save_result(res, sha256_hash=file_sha)
            processed.append({
                "filename": att["filename"],
                "status": "needs_review",
                "issues": res.issues,
                "sha256_hash": file_sha,
                "db_id": record_id
            })
        if not processed:
            res = ExtractionResult(
                file=eml_path,
                format_id="email_review",
                status="needs_review",
                issues=[reason or "classification_uncertain"],
                statements=[],
                diagnostic_message=f"Email quarantined: '{subject}' ({reason})",
                llm_calls_made=0,
                sha256_hash=""
            )
            record_id = store.save_result(res)
            processed.append({
                "filename": os.path.basename(eml_path),
                "status": "needs_review",
                "issues": res.issues,
                "sha256_hash": "",
                "db_id": record_id
            })
        return processed

    try:
        # Supported statement attachments (.pdf, .xlsx, .csv) + Companion unsupported attachments
        from models import ExtractionResult
        for att in email_res.get("attachments", []):
            att_path = att["path"]
            ext = os.path.splitext(att["filename"])[1].lower()
            if ext not in [".pdf", ".xlsx", ".csv"]:
                unsupported_issue = "zip_attachment_requires_manual_review" if ext == ".zip" else (
                    "unsupported_statement_attachment_type" if ext in [".xls", ".xlsm"] else "ambiguous_attachment_requires_review"
                )
                file_sha = compute_sha256(att_path) if os.path.exists(att_path) else ""
                res = ExtractionResult(
                    file=att_path,
                    format_id="unsupported_companion_attachment",
                    status="needs_review",
                    issues=[unsupported_issue],
                    statements=[],
                    diagnostic_message=f"Companion attachment '{att['filename']}' requires review",
                    llm_calls_made=0,
                    sha256_hash=file_sha
                )
                record_id = store.save_result(res, sha256_hash=file_sha)
                processed.append({
                    "filename": att["filename"],
                    "status": "needs_review",
                    "issues": res.issues,
                    "sha256_hash": file_sha,
                    "db_id": record_id
                })
                continue

            print(f"  [PARSING] Statement attachment: {att['filename']}")
            res = pipeline.process_file(att_path)
            file_sha = res.sha256_hash or (compute_sha256(att_path) if os.path.exists(att_path) else "")
            print(f"  -> Format: {res.format_id} | Status: {res.status} | Issues: {res.issues}")

            record_id = store.save_result(res, sha256_hash=file_sha)
            processed.append({
                "filename": att["filename"],
                "status": res.status,
                "issues": res.issues,
                "sha256_hash": file_sha,
                "db_id": record_id
            })

        store.update_email_event_processing_state(msg_id, "completed")
        return processed
    except Exception as err:
        store.update_email_event_processing_state(msg_id, "failed", reason=str(err))
        raise


def fetch_and_process_unseen(
    mail: imaplib.IMAP4_SSL,
    pipeline: ProcessingPipeline,
    store: StatementStore,
    temp_dir: str = "temp_emails"
) -> int:
    """
    Fetches unseen emails using BODY.PEEK to prevent premature state modification,
    processes attachments inside a per-email try/except guard so one bad email
    cannot terminate the loop, and marks only successfully handled emails as Seen.
    """
    os.makedirs(temp_dir, exist_ok=True)
    status, messages = mail.search(None, "UNSEEN")
    if status != "OK" or not messages or not messages[0]:
        return 0

    mail_ids = messages[0].split()
    print(f"Found {len(mail_ids)} unseen message(s) in inbox.")

    count = 0
    for m_id in mail_ids:
        msg_str_id = m_id.decode() if isinstance(m_id, bytes) else str(m_id)
        temp_eml_path = os.path.join(temp_dir, f"mail_{msg_str_id}.eml")
        try:
            status, data = mail.fetch(m_id, "(BODY.PEEK[])")
            if status != "OK" or not data:
                continue

            raw_email = None
            for response_part in data:
                if isinstance(response_part, tuple) and len(response_part) > 1:
                    raw_email = response_part[1]
                    break

            if not raw_email:
                continue

            with open(temp_eml_path, "wb") as f:
                f.write(raw_email)

            process_eml_file(temp_eml_path, pipeline, store)
            mail.store(m_id, "+FLAGS", "\\Seen")
            count += 1
        except EmailProcessingInProgress as err:
            print(f"[INFO] Email ID {msg_str_id} remains unseen: {err}")
        except Exception as err:
            # Leave message unseen (or flag it) so one bad email never kills the loop
            print(f"[ERROR] Failed processing email ID {msg_str_id}: {err}", file=sys.stderr)
            try:
                mail.store(m_id, "+FLAGS", "\\Flagged")
            except Exception:
                pass
        finally:
            if os.path.exists(temp_eml_path):
                try:
                    os.remove(temp_eml_path)
                except Exception:
                    pass

    return count


_poll_inbox_iteration = fetch_and_process_unseen


def _connect_imap(host: str, port: int, user: str, password: str, folder: str, timeout: float = 30.0) -> imaplib.IMAP4_SSL:
    """Connects and selects folder on IMAP server with bounded socket timeout."""
    clean_pwd = password.strip().replace(" ", "") if password else ""
    mail = imaplib.IMAP4_SSL(host, port, timeout=timeout)
    mail.login(user, clean_pwd)
    mail.select(folder)
    return mail


def run_poller(
    host: str,
    port: int,
    user: str,
    password: str,
    folder: str = "INBOX",
    poll_interval: int = 60,
    run_once: bool = False,
    db_path: str = "statements.db",
    max_retries: int = 3
):
    """Main polling loop for IMAP service with automatic reconnect on transient IMAP errors."""
    store = StatementStore(db_path=db_path)
    pipeline = ProcessingPipeline(db_path=db_path)

    print(f"Connecting to IMAP {host}:{port} as {user}...")
    mail = None
    for attempt in range(1, max_retries + 1):
        try:
            mail = _connect_imap(host, port, user, password, folder)
            print(f"Successfully connected and monitoring folder: '{folder}'.")
            break
        except Exception as e:
            print(f"IMAP Connection attempt {attempt}/{max_retries} failed: {e}", file=sys.stderr)
            if attempt == max_retries:
                return
            time.sleep(min(5 * attempt, 30))

    try:
        while True:
            try:
                fetch_and_process_unseen(mail, pipeline, store)
            except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError) as conn_err:
                print(f"[WARN] IMAP connection dropped ({conn_err}). Reconnecting...", file=sys.stderr)
                try:
                    mail = _connect_imap(host, port, user, password, folder)
                except Exception as reconn_err:
                    print(f"[ERROR] Reconnect failed: {reconn_err}", file=sys.stderr)
            if run_once:
                break
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        print("\nShutting down mailbox poller.")
    finally:
        if mail is not None:
            try:
                mail.close()
                mail.logout()
            except Exception:
                pass


def safe_poll_inbox(
    db_path: str = "statements.db",
    host: Optional[str] = None,
    user: Optional[str] = None,
    password: Optional[str] = None,
    port: Optional[int] = None,
    folder: str = "INBOX"
) -> Dict[str, Any]:
    """
    Safely scans inbox once for new unseen emails, processes only emails not already
    persisted in email_events, writes statement results and email events to SQLite,
    and returns a summary dict. Safe to invoke on dashboard startup or via 'Check Inbox Now'.
    """
    _load_local_env()

    eff_host = host or os.getenv("IMAP_HOST")
    eff_user = user or os.getenv("IMAP_USER")
    eff_pwd = password or os.getenv("IMAP_PASSWORD")
    eff_port = port or int(os.getenv("IMAP_PORT", "993"))
    eff_folder = folder or os.getenv("IMAP_FOLDER", "INBOX")

    if not eff_host or not eff_user or not eff_pwd:
        return {
            "status": "not_configured",
            "count": 0,
            "message": "IMAP credentials not configured in environment or .env"
        }

    store = StatementStore(db_path=db_path)
    pipeline = ProcessingPipeline(db_path=db_path)

    mail = None
    try:
        mail = _connect_imap(eff_host, eff_port, eff_user, eff_pwd, eff_folder)
        processed_count = fetch_and_process_unseen(mail, pipeline, store)
        return {
            "status": "success",
            "count": processed_count,
            "message": f"Inbox scan completed. Processed {processed_count} unseen email(s)."
        }
    except Exception as e:
        return {
            "status": "error",
            "count": 0,
            "message": f"Inbox scan error: {str(e)}"
        }
    finally:
        if mail is not None:
            try:
                mail.close()
                mail.logout()
            except Exception:
                pass


def reprocess_review_queue(
    db_path: str = "statements.db",
    password: Optional[str] = None,
    templates_dir: str = "templates",
) -> List[Dict[str, Any]]:
    """
    Re-processes statements quarantined in 'needs_review' from SQLite persistence store
    using candidate password(s) or updated templates.
    """
    _load_local_env()
    store = StatementStore(db_path=db_path)
    pipeline = ProcessingPipeline(templates_dir=templates_dir, db_path=db_path)
    review_records = store.get_needs_review()
    print(f"Found {len(review_records)} statement(s) quarantined in needs_review.")

    updated = []
    for rec in review_records:
        file_path = rec.get("file_path")
        if not file_path or not os.path.exists(file_path):
            continue
        print(f"Re-processing: {file_path} (previous issues: {rec.get('issues_json')})")
        res = pipeline.process_file(file_path, password=password)
        print(f"  -> New Status: {res.status} | Issues: {res.issues}")
        file_sha = res.sha256_hash or (compute_sha256(file_path) if os.path.exists(file_path) else "")
        store.save_result(res, sha256_hash=file_sha)
        updated.append({
            "file": file_path,
            "status": res.status,
            "issues": res.issues
        })
    return updated


def main():
    parser = argparse.ArgumentParser(description="IMAP Statement Mailbox Poller & Background Processor")
    parser.add_argument("--test-eml", help="Path to local .eml file to process offline without live IMAP server")
    parser.add_argument("--db", default="statements.db", help="Path to SQLite persistence database")
    parser.add_argument("--once", action="store_true", help="Run a single poll iteration and exit")
    parser.add_argument("--interval", type=int, default=60, help="Polling interval in seconds")
    parser.add_argument("--reprocess-review", action="store_true", help="Re-process quarantined statements in needs_review with candidate password(s)")
    parser.add_argument("--password", help="Candidate password to unlock encrypted statements")
    parser.add_argument("--host", help="IMAP server hostname (e.g. imap.gmail.com)")
    parser.add_argument("--user", help="IMAP username / email address")
    parser.add_argument("--pass", dest="imap_pass", help="IMAP application password")
    parser.add_argument("--port", type=int, help="IMAP port (default: 993)")
    parser.add_argument("--folder", help="IMAP mailbox folder (default: INBOX)")
    args = parser.parse_args()

    # Load local credentials before offline/review modes as well as live polling.
    _load_local_env()

    # Reprocess review queue mode
    if args.reprocess_review:
        results = reprocess_review_queue(db_path=args.db, password=args.password)
        print(f"\nCompleted re-processing review queue: {len(results)} statement(s) evaluated.")
        return

    # Offline test mode
    if args.test_eml:
        if not os.path.exists(args.test_eml):
            print(f"Error: EML file '{args.test_eml}' not found.")
            sys.exit(1)
        store = StatementStore(db_path=args.db)
        pipeline = ProcessingPipeline(db_path=args.db)
        results = process_eml_file(args.test_eml, pipeline, store)
        print(f"\nCompleted offline processing of {args.test_eml}: {len(results)} statement(s) parsed.")
        return

    # Live IMAP mode
    host = args.host or os.getenv("IMAP_HOST")
    user = args.user or os.getenv("IMAP_USER")
    password = args.imap_pass or os.getenv("IMAP_PASSWORD")
    if password:
        password = password.strip().replace(" ", "")
    port = args.port or int(os.getenv("IMAP_PORT", "993"))
    folder = args.folder or os.getenv("IMAP_FOLDER", "INBOX")

    if not host or not user or not password:
        print("Notice: IMAP credentials not set in environment.")
        print("Please configure the following environment variables to poll live email:")
        print("  - IMAP_HOST (e.g. imap.gmail.com)")
        print("  - IMAP_USER (e.g. wealth.statements@example.com)")
        print("  - IMAP_PASSWORD (e.g. app-specific password)")
        print("\nAlternatively, test the poller offline against a sample email:")
        print("  python watch_mailbox.py --test-eml sample_data/emails/e01_statement_single_attachment.eml")
        print("  python watch_mailbox.py --reprocess-review --password <password>")
        sys.exit(0)

    run_poller(
        host=host,
        port=port,
        user=user,
        password=password,
        folder=folder,
        poll_interval=args.interval,
        run_once=args.once,
        db_path=args.db
    )


if __name__ == "__main__":
    main()
