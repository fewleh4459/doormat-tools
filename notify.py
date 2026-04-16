"""Email notifications via Gmail SMTP.

Uses Gmail App Password — set these env vars in the Routine environment:
  GMAIL_USER          — the from address (e.g. oliver@beaudax.co.uk)
  GMAIL_APP_PASSWORD  — 16-char app password from Google Account settings
  NOTIFY_TO           — recipient (defaults to GMAIL_USER)

Call from a routine:
  from notify import send_summary
  send_summary(
      subject="Doormat Etsy Watcher",
      processed=12,
      errors=[],
      folders=["CMD Etsy/April 2026", "EMA Etsy/April 2026"],
      body_extra="Raster fallbacks: 1",
  )
"""

import os
import smtplib
import ssl
from email.message import EmailMessage


def send_summary(
    subject: str,
    processed: int,
    errors: list = None,
    folders: list = None,
    body_extra: str = "",
    silent_if_empty: bool = True,
) -> bool:
    """Email a summary to NOTIFY_TO (or GMAIL_USER if unset).

    Returns True on success, False on failure.
    If silent_if_empty=True and there's nothing to report (0 processed, 0 errors),
    returns False without sending.
    """
    errors = errors or []
    folders = folders or []

    # Skip silent runs by default
    if silent_if_empty and processed == 0 and not errors:
        return False

    user = os.environ.get("GMAIL_USER")
    pwd = os.environ.get("GMAIL_APP_PASSWORD")
    to = os.environ.get("NOTIFY_TO", user)

    if not (user and pwd and to):
        print("[notify] Email not sent — missing GMAIL_USER / GMAIL_APP_PASSWORD")
        return False

    # Build subject line with status tag
    status = "OK" if not errors else f"{len(errors)} ERRORS"
    full_subject = f"[{status}] {subject} — {processed} processed"

    # Build body
    lines = [
        f"Summary:",
        f"  Processed: {processed}",
        f"  Errors: {len(errors)}",
        "",
    ]
    if folders:
        lines.append("Folders with activity:")
        for f in folders:
            lines.append(f"  - {f}")
        lines.append("")
    if errors:
        lines.append("Error details:")
        for e in errors:
            lines.append(f"  - {e}")
        lines.append("")
    if body_extra:
        lines.append(body_extra)
        lines.append("")

    lines.append("— Sent by doormat-tools routine")
    body = "\n".join(lines)

    # Send
    msg = EmailMessage()
    msg["Subject"] = full_subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(body)

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
            server.login(user, pwd)
            server.send_message(msg)
        print(f"[notify] Emailed {to}: {full_subject}")
        return True
    except Exception as e:
        print(f"[notify] FAILED to send email: {e}")
        return False


if __name__ == "__main__":
    # Quick test when run directly
    send_summary(
        subject="Test",
        processed=3,
        errors=[],
        folders=["test/folder"],
        body_extra="This is a test email.",
        silent_if_empty=False,
    )
