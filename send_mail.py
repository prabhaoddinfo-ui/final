"""
send_mail.py — Email sending helpers
Provides send_one_email, send_bulk_emails, and test_smtp_connection.
Used by both main.py (Streamlit) and backend.py (FastAPI).
"""

import base64
import logging
import smtplib
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ─── SMTP connection test ─────────────────────────────────────────────────────

def test_smtp_connection(
    host: str,
    port: int = 587,
    username: str = "",
    password: str = "",
    use_tls: bool = True,
    use_ssl: bool = False,
) -> tuple[bool, str]:
    """
    Verify that an SMTP server is reachable and credentials are accepted.

    Returns:
        (success: bool, message: str)
    """
    if not host:
        return False, "SMTP host is required"

    try:
        if use_ssl:
            ctx = ssl.create_default_context()
            server = smtplib.SMTP_SSL(host, port, timeout=10, context=ctx)
        else:
            server = smtplib.SMTP(host, port, timeout=10)
            server.ehlo()
            if use_tls:
                server.starttls()
                server.ehlo()

        if username and password:
            server.login(username, password)

        server.quit()
        return True, f"Connected to {host}:{port} successfully"

    except smtplib.SMTPAuthenticationError:
        return False, "Authentication failed. For Gmail, use an App Password (not your regular password)."
    except smtplib.SMTPConnectError as e:
        return False, f"Cannot connect to {host}:{port} — {e}"
    except socket.timeout:
        return False, f"Connection to {host}:{port} timed out"
    except Exception as e:
        return False, str(e)


# ─── Single email ─────────────────────────────────────────────────────────────

def send_one_email(
    smtp_host: str,
    smtp_port: int = 587,
    smtp_user: str = "",
    smtp_pass: str = "",
    use_ssl: bool = False,
    use_tls: bool = True,
    from_addr: str = "",
    to_addr: str = "",
    subject: str = "No Subject",
    body: str = "",
    use_html: bool = False,
    attachments: Optional[List[Dict]] = None,
    timeout: int = 30,
    max_retries: int = 3,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Send a single email with optional file attachments.

    Returns a result dict:
        {to, status, error, attempts}
    Status values: "sent" | "failed" | "dry-run"
    """
    if dry_run:
        return {"to": to_addr, "status": "dry-run", "error": None, "attempts": 0}

    last_error = "Unknown error"

    for attempt in range(1, max_retries + 1):
        try:
            # ── Build message ────────────────────────────────────────────────
            msg = MIMEMultipart("mixed")
            msg["From"]    = from_addr or smtp_user or "sender@example.com"
            msg["To"]      = to_addr
            msg["Subject"] = subject
            msg["Date"]    = formatdate(localtime=True)

            mime_type = "html" if use_html else "plain"
            msg.attach(MIMEText(body, mime_type, "utf-8"))

            for att in (attachments or []):
                content = att.get("content", "")
                if not content:
                    continue
                filename = str(att.get("filename", "attachment.bin"))
                try:
                    file_data = base64.b64decode(content)
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(file_data)
                    encoders.encode_base64(part)
                    part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
                    msg.attach(part)
                except Exception as ae:
                    logger.warning(f"Attachment error ({filename}): {ae}")

            # ── Connect ──────────────────────────────────────────────────────
            if use_ssl:
                ctx = ssl.create_default_context()
                server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=timeout, context=ctx)
            else:
                server = smtplib.SMTP(smtp_host, smtp_port, timeout=timeout)
                server.ehlo()
                if use_tls:
                    server.starttls()
                    server.ehlo()

            if smtp_user and smtp_pass:
                server.login(smtp_user, smtp_pass)

            server.send_message(msg)
            server.quit()

            logger.info(f"Sent → {to_addr} (attempt {attempt})")
            return {"to": to_addr, "status": "sent", "error": None, "attempts": attempt}

        except smtplib.SMTPAuthenticationError as e:
            last_error = f"Auth failed: {str(e)[:120]}. For Gmail use an App Password."
            logger.error(f"Auth error for {to_addr}: {e}")
            break  # Retrying won't help for auth failures

        except smtplib.SMTPRecipientsRefused as e:
            last_error = f"Recipient refused: {str(e)[:120]}"
            logger.error(f"Refused {to_addr}: {e}")
            break

        except Exception as e:
            last_error = str(e)[:200]
            logger.warning(f"Attempt {attempt}/{max_retries} failed for {to_addr}: {last_error}")
            if attempt < max_retries:
                time.sleep(2)

    return {"to": to_addr, "status": "failed", "error": last_error, "attempts": max_retries}


# ─── Bulk send ────────────────────────────────────────────────────────────────

def send_bulk_emails(
    recipients: List[str],
    smtp_config: Dict[str, Any],
    subject: str = "No Subject",
    body: str = "",
    attachments: Optional[List[Dict]] = None,
    use_html: bool = False,
    dry_run: bool = False,
    workers: int = 3,
    rate_limit: float = 0.5,
    max_retries: int = 3,
) -> List[Dict[str, Any]]:
    """
    Send emails to multiple recipients using a thread pool.

    The *smtp_config* dict supports both flat-key ("smtp_host", "smtp_port", …)
    and short-key ("host", "port", …) naming conventions.

    Returns a list of result dicts (one per recipient).
    """
    # Resolve keys — accept both naming styles
    host      = smtp_config.get("host") or smtp_config.get("smtp_host", "")
    port      = int(smtp_config.get("port") or smtp_config.get("smtp_port", 587))
    user      = smtp_config.get("username") or smtp_config.get("smtp_user", "")
    password  = smtp_config.get("password") or smtp_config.get("smtp_pass", "")
    from_addr = smtp_config.get("from_addr") or smtp_config.get("from_email", "") or user
    use_ssl   = smtp_config.get("use_ssl", False)
    use_tls   = smtp_config.get("use_tls", True)

    results: List[Dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(
                send_one_email,
                smtp_host=host,
                smtp_port=port,
                smtp_user=user,
                smtp_pass=password,
                use_ssl=use_ssl,
                use_tls=use_tls,
                from_addr=from_addr,
                to_addr=recipient,
                subject=subject,
                body=body,
                use_html=use_html,
                attachments=attachments or [],
                timeout=30,
                max_retries=max_retries,
                dry_run=dry_run,
            ): recipient
            for recipient in recipients
        }

        for i, future in enumerate(as_completed(future_map)):
            recipient = future_map[future]
            try:
                result = future.result()
            except Exception as e:
                result = {"to": recipient, "status": "failed", "error": str(e), "attempts": 0}
            results.append(result)

            # Rate limiting between sends
            if rate_limit > 0 and i < len(recipients) - 1:
                time.sleep(rate_limit)

    return results