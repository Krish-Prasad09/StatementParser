"""
send_test_email.py
------------------
Utility to send test financial statement emails to the configured Gmail inbox via SMTP SSL.
Enables a complete end-to-end demo loop:
1. python send_test_email.py --file sample_data/pms/01_aurum_A1_standard.pdf
2. python watch_mailbox.py --once
"""

import os
import sys
import smtplib
import random
import argparse
from email.message import EmailMessage
from typing import Optional, Dict, Any

# Ensure stdout handles utf-8
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


DEFAULT_STATEMENT_SUBJECTS = [
    "Your PMS Portfolio Statement for September 2026",
    "Quarterly Wealth Statement - Period Ended 30-Sep-2026",
    "Monthly Investment Valuation & Holdings Report",
    "Consolidated Portfolio Statement - September 2026",
    "Portfolio Management Service - Periodic Statement",
    "Alternative Investment Fund (AIF) Valuation Report"
]


def load_env():
    """Loads environment variables from local .env if present."""
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_file):
        return
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


def send_statement_email(
    file_path: Optional[str] = None,
    subject: Optional[str] = None,
    body: Optional[str] = None,
    to_email: Optional[str] = None,
    smtp_host: str = "smtp.gmail.com",
    smtp_port: int = 465
) -> Dict[str, Any]:
    """
    Sends an email with an optional attachment to the configured test mailbox.
    Uses credentials from .env (IMAP_USER and IMAP_PASSWORD / App Password).
    """
    load_env()

    sender_email = os.environ.get("IMAP_USER", "").strip()
    sender_password = os.environ.get("IMAP_PASSWORD", "").strip()
    recipient = to_email or sender_email

    if not sender_email or not sender_password:
        return {
            "status": "error",
            "error": "IMAP_USER or IMAP_PASSWORD not configured in environment or .env file."
        }

    # Generate subject if not provided
    if not subject:
        if file_path:
            base = os.path.basename(file_path)
            subject = f"{random.choice(DEFAULT_STATEMENT_SUBJECTS)} ({base})"
        else:
            subject = random.choice(DEFAULT_STATEMENT_SUBJECTS)

    # Generate body if not provided
    if not body:
        if file_path:
            body = (
                f"Dear Investor,\n\n"
                f"Please find attached your periodic wealth statement: {os.path.basename(file_path)}.\n\n"
                f"This statement covers your portfolio valuation, holdings breakdown, and transaction history.\n\n"
                f"Sincerely,\nPortfolio Operations Team"
            )
        else:
            body = (
                "Dear Investor,\n\n"
                "Your latest portfolio statement is ready for review on the secure investor portal.\n\n"
                "Visit: https://portal.example.com/statements/view\n\n"
                "Sincerely,\nClient Relations"
            )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender_email
    msg["To"] = recipient
    msg.set_content(body)

    # Attach file if specified
    attached_filename = None
    if file_path:
        if not os.path.exists(file_path):
            return {"status": "error", "error": f"Attachment file not found: {file_path}"}

        attached_filename = os.path.basename(file_path)
        ext = os.path.splitext(attached_filename)[1].lower()

        # Select MIME type
        if ext == ".pdf":
            maintype, subtype = "application", "pdf"
        elif ext == ".xlsx":
            maintype, subtype = "application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        elif ext == ".csv":
            maintype, subtype = "text", "csv"
        elif ext in [".xls", ".xlsm"]:
            maintype, subtype = "application", "vnd.ms-excel"
        elif ext == ".zip":
            maintype, subtype = "application", "zip"
        else:
            maintype, subtype = "application", "octet-stream"

        with open(file_path, "rb") as f:
            file_bytes = f.read()

        msg.add_attachment(
            file_bytes,
            maintype=maintype,
            subtype=subtype,
            filename=attached_filename
        )

    # Connect to SMTP and send
    clean_pwd = sender_password.replace(" ", "")
    try:
        # Try SSL on port 465 first
        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=20) as server:
            server.login(sender_email, clean_pwd)
            server.send_message(msg)
    except Exception as ssl_err:
        try:
            # Fallback to STARTTLS on port 587
            with smtplib.SMTP(smtp_host, 587, timeout=20) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(sender_email, clean_pwd)
                server.send_message(msg)
        except Exception as tls_err:
            return {
                "status": "error",
                "error": f"SMTP failed (SSL error: {ssl_err}; STARTTLS error: {tls_err})"
            }

    return {
        "status": "success",
        "sender": sender_email,
        "recipient": recipient,
        "subject": subject,
        "attachment": attached_filename
    }


def main():
    parser = argparse.ArgumentParser(description="Send test financial statement emails to the configured mailbox.")
    parser.add_argument("--file", "-f", help="Path to statement file to attach (PDF, Excel, CSV)")
    parser.add_argument("--subject", "-s", help="Custom email subject line (random realistic subject if omitted)")
    parser.add_argument("--body", "-b", help="Custom email body text")
    parser.add_argument("--to", help="Recipient email address (defaults to IMAP_USER from .env)")
    parser.add_argument("--random-sample", action="store_true", help="Pick a random sample statement from sample_data/pms")

    args = parser.parse_args()

    file_to_send = args.file
    if args.random_sample and not file_to_send:
        samples = [
            "sample_data/pms/01_aurum_A1_standard.pdf",
            "sample_data/pms/04_banyan_B1_quarterly_2pages.pdf",
            "sample_data/pms/08_delta_D1_ledger.pdf",
            "sample_data/aif/12_evergreen_E1_cat2.pdf",
            "sample_data/excel_csv/16_hestia_statement.xlsx"
        ]
        file_to_send = random.choice([s for s in samples if os.path.exists(s)])

    print(f"Preparing to send test email...")
    if file_to_send:
        print(f"  Attachment: {file_to_send}")

    res = send_statement_email(
        file_path=file_to_send,
        subject=args.subject,
        body=args.body,
        to_email=args.to
    )

    if res.status == "success":
        print("\n✅ Email sent successfully!")
        print(f"  To:         {res['recipient']}")
        print(f"  Subject:    {res['subject']}")
        print(f"  Attachment: {res.get('attachment') or 'None'}")
        print("\nNext step: Run 'python watch_mailbox.py --once' to poll and ingest this statement.")
    else:
        print(f"\n❌ Failed to send email: {res.get('error')}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
