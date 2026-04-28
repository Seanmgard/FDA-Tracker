#!/usr/bin/env python3
"""
Every run:
  1. Fetch each watched page and strip it to plain text.
  2. Compare to the last saved text.
  3. If anything changed, email a diff of what was added/removed.
  4. Save the new text.

Required environment variables (set as GitHub Actions secrets):
  SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_FROM, EMAIL_TO
Optional:
  SMTP_PORT  (default: 587)
"""

import difflib
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.request
from email.mime.text import MIMEText
from pathlib import Path

PAGES = [
    {
        "id": "labeling-changes",
        "label": "Animal Drug Safety-Related Labeling Changes",
        "url": "https://www.fda.gov/animal-veterinary/drug-labels/animal-drug-safety-related-labeling-changes",
    },
    {
        "id": "cvm-updates",
        "label": "CVM Updates (News & Events)",
        "url": "https://www.fda.gov/animal-veterinary/news-events/cvm-updates",
    },
]

STATE_FILE = Path(__file__).resolve().parents[1] / "watch" / "state.json"


def fetch(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; FDA-Watch/1.0)",
            "Accept": "text/html",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


def to_text(html: str) -> list[str]:
    """Convert HTML to a list of non-empty plain-text lines."""
    # Drop scripts and styles entirely
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.I | re.S)
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.I | re.S)
    # Replace block tags with newlines
    html = re.sub(r"<(br|p|div|li|tr|h[1-6])[^>]*>", "\n", html, flags=re.I)
    # Strip remaining tags
    html = re.sub(r"<[^>]+>", "", html)
    # Decode entities
    html = html.replace("&amp;", "&").replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'").replace("&quot;", '"')
    lines = [line.strip() for line in html.splitlines()]
    return [l for l in lines if l]


def send_email(subject: str, body: str) -> None:
    host = os.environ.get("SMTP_HOST", "")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    from_addr = os.environ.get("EMAIL_FROM", "")
    to_addrs = [x.strip() for x in os.environ.get("EMAIL_TO", "").split(",") if x.strip()]

    if not (host and from_addr and to_addrs):
        print("Email skipped — set SMTP_HOST, EMAIL_FROM, EMAIL_TO.", file=sys.stderr)
        return

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)

    ctx = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls(context=ctx)
        if user and password:
            s.login(user, password)
        s.sendmail(from_addr, to_addrs, msg.as_string())
    print(f"Email sent to {to_addrs}.")


def main() -> int:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state: dict = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}

    alert_sections: list[str] = []

    for page in PAGES:
        pid, label, url = page["id"], page["label"], page["url"]
        print(f"Fetching: {label}")

        try:
            html = fetch(url)
        except Exception as e:
            print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
            continue

        current_lines = to_text(html)
        previous_lines = state.get(pid, {}).get("lines")

        if previous_lines is None:
            print(f"  First run — saving baseline.")
            state[pid] = {"lines": current_lines}
            continue

        if current_lines == previous_lines:
            print(f"  No change.")
            state[pid] = {"lines": current_lines}
            continue

        # Something changed — compute a human-readable diff
        diff = list(difflib.unified_diff(
            previous_lines,
            current_lines,
            lineterm="",
            n=2,
        ))
        added   = [l[1:] for l in diff if l.startswith("+") and not l.startswith("+++")]
        removed = [l[1:] for l in diff if l.startswith("-") and not l.startswith("---")]

        section_lines = [
            f"{label}",
            f"  {url}",
            "",
        ]
        if added:
            section_lines.append("  ADDED:")
            section_lines.extend(f"    + {l}" for l in added)
        if removed:
            section_lines.append("  REMOVED:")
            section_lines.extend(f"    - {l}" for l in removed)

        alert_sections.append("\n".join(section_lines))
        state[pid] = {"lines": current_lines}
        print(f"  CHANGED (+{len(added)} lines, -{len(removed)} lines).")

    STATE_FILE.write_text(json.dumps(state, indent=2))

    if alert_sections:
        body = "\n\n".join([
            "An FDA page you're watching has been updated.\n",
            *alert_sections,
        ])
        try:
            send_email("FDA page watch: update detected", body)
        except Exception as e:
            print(f"Failed to send email: {e}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
