#!/usr/bin/env python3
"""
Fetch configured FDA pages, compare stable content fingerprints to watch/state.json,
send email when a page changes, then update state.

Environment (email):
  SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASSWORD
  EMAIL_FROM, EMAIL_TO (comma-separated for multiple recipients)

Optional:
  FDA_WATCH_BASELINE_EMAIL=1 — send email on first run when establishing baseline
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.error
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "watch" / "config.json"
STATE_PATH = ROOT / "watch" / "state.json"


def load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp.replace(path)


def normalize_html(html: str) -> bytes:
    """Reduce noise from scripts, styles, and whitespace churn."""
    html = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.I | re.S)
    html = re.sub(r"<style\b[^>]*>.*?</style>", "", html, flags=re.I | re.S)
    html = re.sub(r"\s+", " ", html)
    return html.strip().encode("utf-8")


def fingerprint(url: str, timeout: int = 60) -> tuple[str, str | None]:
    """
    Returns (sha256_hex, etag_or_none).
    Uses ETag when present for a cheaper comparison signal; stored hash is always body-based.
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "FDA-Page-Watch/1.0 (+https://github.com/actions)",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        etag = resp.headers.get("ETag") or resp.headers.get("etag")

    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        text = raw.decode(errors="replace")

    digest = hashlib.sha256(normalize_html(text)).hexdigest()
    return digest, etag


def send_email(subject: str, body: str) -> None:
    host = os.environ.get("SMTP_HOST", "").strip()
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").strip()
    from_addr = os.environ.get("EMAIL_FROM", "").strip()
    to_raw = os.environ.get("EMAIL_TO", "").strip()
    port = int(os.environ.get("SMTP_PORT", "587"))

    if not all([host, from_addr, to_raw]):
        print(
            "Email skipped: set SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_FROM, EMAIL_TO",
            file=sys.stderr,
        )
        return

    recipients = [x.strip() for x in to_raw.split(",") if x.strip()]
    if not recipients:
        print("Email skipped: EMAIL_TO empty", file=sys.stderr)
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(body, "plain", "utf-8"))

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=60) as server:
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
        if user and password:
            server.login(user, password)
        server.sendmail(from_addr, recipients, msg.as_string())

    print(f"Sent email to {len(recipients)} recipient(s).")


def main() -> int:
    config = load_json(CONFIG_PATH)
    state = load_json(STATE_PATH)
    pages_cfg = config.get("pages") or []
    pages_state: dict = state.setdefault("pages", {})

    baseline_email = os.environ.get("FDA_WATCH_BASELINE_EMAIL", "").strip() in (
        "1",
        "true",
        "yes",
    )

    changes: list[tuple[dict, str, str | None, str | None]] = []
    errors: list[str] = []

    for entry in pages_cfg:
        pid = entry["id"]
        url = entry["url"]
        label = entry.get("label") or pid
        prev = pages_state.get(pid)
        prev_hash = (prev or {}).get("sha256") if isinstance(prev, dict) else None
        try:
            digest, etag = fingerprint(url)
        except urllib.error.HTTPError as e:
            errors.append(f"{label}: HTTP {e.code} {e.reason}")
            continue
        except urllib.error.URLError as e:
            errors.append(f"{label}: {e.reason}")
            continue
        except Exception as e:
            errors.append(f"{label}: {e}")
            continue

        if prev_hash is None:
            pages_state[pid] = {"sha256": digest, "etag": etag, "url": url}
            print(f"[baseline] {label}: recorded fingerprint {digest[:12]}…")
            if baseline_email:
                changes.append((entry, digest, None, etag))
            continue

        if prev_hash != digest:
            changes.append((entry, digest, prev_hash, etag))

        pages_state[pid] = {"sha256": digest, "etag": etag, "url": url}

    if changes:
        lines = [
            "One or more watched FDA pages changed since the last check.",
            "",
        ]
        for entry, new_h, old_h, etag in changes:
            label = entry.get("label") or entry["id"]
            url = entry["url"]
            lines.append(f"• {label}")
            lines.append(f"  {url}")
            if old_h:
                lines.append(f"  fingerprint: {old_h[:12]}… → {new_h[:12]}…")
            else:
                lines.append(f"  fingerprint: {new_h[:12]}… (baseline)")
            if etag:
                lines.append(f"  ETag: {etag}")
            lines.append("")

        if errors:
            lines.append("Fetch warnings (other pages may still be updated):")
            for e in errors:
                lines.append(f"  - {e}")

        subject = "FDA page watch: update detected"
        body = "\n".join(lines)
        try:
            send_email(subject, body)
        except Exception as e:
            print(f"Failed to send email: {e}", file=sys.stderr)
            return 1

    save_json(STATE_PATH, state)

    if errors and not changes:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1

    for e in errors:
        print(f"WARN: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
