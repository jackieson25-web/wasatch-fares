"""Stops all Ignav calls if Ignav's published price goes above the approved price.

Before any Ignav request, watch.py and deals.py call allowed(). It reads
https://ignav.com/pricing, finds the "$X per 1,000 requests" rate, and compares it
with the approved rate (Jack approved $2.00 per 1,000 on 2026-11-20).

- At or below the approved rate: Ignav calls go ahead.
- Above it: no Ignav calls are made. Jack gets one email (to SMTP_USER only).
  Calls stay off until he approves the new rate: change APPROVED_PER_1K below
  to it (ask Claude, or edit this file on GitHub).
- Price not readable (page changed or down): no calls either. Jack is emailed
  after 3 such checks in a row, so a brief outage doesn't email him.
"""

import json
import os
import re
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from html import unescape
from pathlib import Path
from urllib.request import Request, urlopen

PRICING_URL = "https://ignav.com/pricing"
# Jack approved $2.00 per 1,000 on 2026-11-20. Only change this when Jack approves a new price.
APPROVED_PER_1K = 2.00
UNREADABLE_ALERT_AFTER = 3

# "$2 per 1,000 successful requests", "$2 / 1,000 requests", "$0.50 per 100 requests"
RATE = re.compile(r"\$\s*(\d+(?:\.\d+)?)\s*(?:per|/)\s*(\d[\d,]*)\s*(?:successful\s+)?(?:fare\s+)?requests?",
                  re.I)


def published_rate(html):
    """Highest $ per 1,000 requests stated on the page, or None if none found."""
    text = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html)))
    rates = [float(p) / int(n.replace(",", "")) * 1000 for p, n in RATE.findall(text)
             if int(n.replace(",", "")) > 0]
    return max(rates) if rates else None


def fetch_rate():
    req = Request(PRICING_URL, headers={"User-Agent": "Mozilla/5.0 (fare-watch Ignav price check)"})
    with urlopen(req, timeout=30) as r:
        return published_rate(r.read().decode("utf-8", errors="replace"))


def _email(subject, text):
    user, pw = os.environ.get("SMTP_USER", ""), os.environ.get("SMTP_PASSWORD", "")
    msg = EmailMessage()
    msg["From"] = f"Fare Watch <{user or 'fare-watch@localhost'}>"
    msg["To"] = user                     # Jack only, never the shared alert list
    msg["Subject"] = subject
    msg.set_content(text)
    if not (user and pw):
        Path("outbox").mkdir(exist_ok=True)
        p = Path("outbox") / f"price-guard-{datetime.now():%Y%m%d-%H%M%S-%f}.eml"
        p.write_bytes(bytes(msg))
        print(f"price guard: email not configured, saved to {p}", flush=True)
        return
    with smtplib.SMTP_SSL(os.environ.get("SMTP_HOST", "smtp.gmail.com"),
                          int(os.environ.get("SMTP_PORT", "465")),
                          context=ssl.create_default_context(), timeout=30) as s:
        s.login(user, pw)
        s.send_message(msg)


def allowed(state_dir, notify=True, rate_fn=fetch_rate):
    """True if Ignav calls may be made. state_dir keeps who-was-emailed-when.
    notify=False checks without ever emailing (deals.py; watch.py does the emailing)."""
    state_file = Path(state_dir) / "price_guard.json"
    try:
        state = json.loads(state_file.read_text())
    except (OSError, ValueError):
        state = {}

    try:
        rate = rate_fn()
    except Exception as e:                       # network error, page down
        rate, why = None, str(e)
    else:
        why = "no \"$ per 1,000 requests\" price found on the page"

    now = datetime.now(timezone.utc).isoformat(timespec="minutes")
    ok = rate is not None and rate <= APPROVED_PER_1K + 1e-9
    if ok:
        print(f"price guard: Ignav ${rate:.2f}/1k, approved ${APPROVED_PER_1K:.2f}/1k, OK", flush=True)
        state = {"last_ok": now, "last_rate": rate}
    elif rate is not None:
        print(f"price guard: Ignav now ${rate:.2f}/1k, above approved ${APPROVED_PER_1K:.2f}/1k. "
              "NO Ignav calls until approved.", flush=True)
        if notify and state.get("alerted_rate") != rate:
            _email(f"⚠ Ignav raised its price to ${rate:.2f} per 1,000. Fare Watch is paused",
                   f"Ignav's pricing page ({PRICING_URL}) now says ${rate:.2f} per 1,000 requests.\n"
                   f"You approved ${APPROVED_PER_1K:.2f}, so Fare Watch and the sale-fare checks have "
                   "stopped making Ignav calls. Nothing is being charged at the new price.\n\n"
                   "To approve the new price, tell Claude \"approve the Ignav price\", or edit "
                   "price_guard.py in the wasatch-fares repo on GitHub and change the 2.00 in the "
                   f"APPROVED_PER_1K line to {rate:.2f}. The next run picks it up.\n\n"
                   f"At the new price, the usual ~5,300 requests a month would cost about "
                   f"${rate * 5.3:.2f} a month.")
            state["alerted_rate"] = rate
        state["blocked_since"] = state.get("blocked_since") or now
    else:
        state["unreadable"] = state.get("unreadable", 0) + 1
        print(f"price guard: couldn't read Ignav's price ({why}); skipping Ignav calls this run "
              f"({state['unreadable']} in a row)", flush=True)
        if notify and state["unreadable"] >= UNREADABLE_ALERT_AFTER and not state.get("alerted_unreadable"):
            _email("⚠ Fare Watch paused: can't read Ignav's price",
                   f"Fare Watch checks {PRICING_URL} before every Ignav call and hasn't been able to "
                   f"read the price {state['unreadable']} checks in a row ({why}).\n\n"
                   "To be safe it's making no Ignav calls. It retries every run and resumes on its own "
                   "if the page comes back at the approved price. If Ignav redesigned the page, the "
                   "price check in price_guard.py needs updating.")
            state["alerted_unreadable"] = True

    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(state))
    except OSError:
        pass
    return ok
