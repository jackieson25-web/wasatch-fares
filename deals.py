"""Sale and mistake-fare filter.

Reads The Flight Deal's public feed, keeps only deals that leave from Salt Lake
City or Washington DC (DCA/IAD, never BWI), checks each one with a live Ignav
fare search on the post's sample dates, and emails the matches.

Run:  python deals.py               (live; Ignav check needs IGNAV_API_KEY)
      FEED_FILE=feed.xml python deals.py   (read a saved feed, for testing)
"""
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from html import escape
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import xml.etree.ElementTree as ET

# ---------------------------------------------------------------- settings
FEEDS = ["https://www.theflightdeal.com/feed/"]

# Cities as the feed writes them, and the airports that count for each.
HOMES = {
    "Salt Lake City": {"SLC"},
    "Washington D.C.": {"DCA", "IAD"},       # BWI deliberately left out
}
DEFAULT_AIRPORT = {"Salt Lake City": "SLC", "Washington D.C.": "IAD"}   # when a post names no airport
MAX_AGE_HOURS = 48        # The Flight Deal: posts older than ~2 days are usually gone
CONFIRM_SLACK = 1.10      # Ignav within 10% of the posted price counts as confirmed
FEED_FAIL_ALERT = 6       # email once after this many failed checks in a row (~3 hours)

API_KEY = os.environ.get("IGNAV_API_KEY", "")
TO = os.environ.get("ALERT_EMAIL", "")
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
STATE = Path(os.environ.get("DEALS_STATE", "deals-data/state.json"))
FEED_FILE = os.environ.get("FEED_FILE")   # testing: read a saved feed instead
DRY_RUN = os.environ.get("DRY_RUN") == "1"   # testing: log only, no email, no state change
TEST_EMAIL = os.environ.get("TEST_EMAIL") == "1"   # testing: email current matches to SMTP_USER only
if TEST_EMAIL:
    TO = SMTP_USER
MAX_AGE_HOURS = float(os.environ.get("MAX_AGE_HOURS", MAX_AGE_HOURS))
# The Flight Deal's Cloudflare started 403-ing script user agents on 2026-11-20; Jack chose a browser UA.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

MONTHS = {m: i for i, m in enumerate(
    "january february march april may june july august september october november december".split(), 1)}


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def get(url, tries=3):
    for attempt in range(tries):
        try:
            with urlopen(Request(url, headers={"User-Agent": UA}), timeout=30) as r:
                return r.read().decode("utf-8", errors="replace")
        except (HTTPError, OSError) as e:
            if attempt == tries - 1:
                raise RuntimeError(f"{url}: {e}")
            time.sleep(5 * (attempt + 1))


# ---------------------------------------------------------------- feed
TITLE = re.compile(r"^(?:The Shorthaul\s*[–-]\s*)?(?P<airline>[^:]+):\s*(?P<frm>.+?)\s+[–-]\s+(?P<to>.+?)"
                   r"(?P<vv>\s*\(and vice versa\))?\.\s+\$(?P<price>[\d,]+)")


def parse_feed(xml):
    """[{guid, title, link, published, airline, frm, to, both_ways, price}]"""
    out = []
    for item in ET.fromstring(xml).iter("item"):
        title = html.unescape(item.findtext("title") or "").strip()
        m = TITLE.match(title)
        if not m:
            continue
        try:
            published = parsedate_to_datetime(item.findtext("pubDate"))
        except (TypeError, ValueError):
            published = datetime.now(timezone.utc)
        out.append(dict(
            guid=item.findtext("guid") or item.findtext("link"), title=title,
            link=item.findtext("link"), published=published,
            airline=m["airline"].strip(), frm=m["frm"].strip(), to=m["to"].strip(),
            both_ways=bool(m["vv"]), price=float(m["price"].replace(",", ""))))
    return out


def home_of(place):
    """'Washington D.C.' / 'Salt Lake City, Utah' -> the HOMES key, or None."""
    for h in HOMES:
        if place.split(",")[0].strip().lower() == h.lower():
            return h
    return None


def matches(d):
    """Is this deal from one of our cities? 'X - Salt Lake City (and vice versa)'
    counts too, since it can be booked starting from SLC."""
    if home_of(d["frm"]):
        return home_of(d["frm"]), False
    if d["both_ways"] and home_of(d["to"]):
        return home_of(d["to"]), True
    return None, False


# ---------------------------------------------------------------- post details
def text_of(page):
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", page, flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t)))


DAY = r"(\d{1,2})(?:st|nd|rd|th)?"
SAMPLE = re.compile(r"Sample Travel Dates?:\s*([A-Za-z]+)\s+" + DAY + r"(?:,\s*(\d{4}))?\s*\W{1,3}\s*"
                    r"(?:([A-Za-z]+)\s+)?" + DAY + r"(?:,?\s*(\d{4}))?")


def post_details(page):
    """Airport codes and sample dates from the post's search instructions."""
    t = text_of(page)
    org = re.search(r"Origin:\W*([A-Z]{3})\b", t)
    dst = re.search(r"Destination:\W*([A-Z]{3})\b", t)
    dep = ret = None
    m = SAMPLE.search(t)
    if m:
        try:
            m1, d1, y1, m2, d2, y2 = m.groups()
            mo1 = MONTHS[m1.lower()]
            mo2 = MONTHS[m2.lower()] if m2 else mo1
            if not y2:             # "December 4th - 7th": the next time that date comes round
                t0 = date.today()
                y2 = t0.year + (1 if (mo1, int(d1)) <= (t0.month, t0.day) else 0) + (1 if mo1 > mo2 else 0)
            y2 = int(y2)
            y1 = int(y1) if y1 else (y2 - 1 if mo1 > mo2 else y2)
            dep, ret = date(y1, mo1, int(d1)), date(y2, mo2, int(d2))
            if ret <= dep or dep <= date.today():
                dep = ret = None
        except (KeyError, ValueError):
            dep = ret = None
    avail = re.search(r"Fare Availability:\s*(.{0,160}?)(?:\.|$)", t)
    return dict(origin=org and org[1], dest=dst and dst[1], depart=dep, ret=ret,
                availability=avail and avail[1].strip())


# ---------------------------------------------------------------- Ignav check
def ignav_price(origin, dest, depart, ret):
    """Cheapest live round-trip fare for the sample dates (1 billed request), or None."""
    body = {"origin": origin, "destination": dest, "market": "US",
            "departure_date": depart.isoformat(), "return_date": ret.isoformat()}
    for attempt in range(3):
        req = Request("https://ignav.com/api/fares/round-trip", data=json.dumps(body).encode(),
                      headers={"X-Api-Key": API_KEY, "Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=90) as r:
                its = json.load(r).get("itineraries", [])
            prices = [float(i["price"]["amount"]) for i in its if i.get("price")]
            return min(prices) if prices else None
        except HTTPError as e:
            if e.code not in (424, 429, 500, 502, 503) or attempt == 2:
                raise RuntimeError(f"Ignav HTTP {e.code}")
        except OSError as e:
            if attempt == 2:
                raise RuntimeError(f"Ignav: {e}")
        time.sleep(4 * (attempt + 1))


def check(deal):
    """Fills in origin/dest/dates and the Ignav verdict. Returns False to skip the deal."""
    try:
        info = post_details(get(deal["link"]))
    except RuntimeError as e:
        log(f"  post unreadable: {e}")
        info = dict(origin=None, dest=None, depart=None, ret=None, availability=None)
    home, reverse = deal["home"], deal["reverse"]
    origin, dest = info["origin"], info["dest"]
    if reverse:                    # post is written X -> SLC; we'd fly SLC -> X
        origin, dest = dest, origin
    if origin and origin not in HOMES[home]:
        if origin == "BWI":
            log("  skipped: BWI only")
            return False
        origin = None              # sample search from an airport we don't use
    # "(and vice versa)" fares are valid both ways, so the sample dates still apply.
    deal.update(origin=origin or DEFAULT_AIRPORT[home], dest=dest,
                depart=info["depart"], ret=info["ret"],
                availability=info["availability"], ignav=None, verdict="unchecked")

    if not (deal["origin"] and dest and deal["depart"]):
        deal["verdict"] = "unchecked"
    elif not API_KEY:
        deal["verdict"] = "unchecked"
    else:
        try:
            p = ignav_price(deal["origin"], dest, deal["depart"], deal["ret"])
            deal["ignav"] = p
            deal["verdict"] = ("none" if p is None else
                               "confirmed" if p <= deal["price"] * CONFIRM_SLACK else "higher")
        except RuntimeError as e:
            log(f"  Ignav check failed: {e}")
            deal["verdict"] = "error"
    return True


# ---------------------------------------------------------------- email
def gflights(d):
    if d["depart"]:
        q = f"Flights from {d['origin']} to {d['dest']} on {d['depart']} returning {d['ret']}"
    else:
        q = f"Flights from {d['origin'] or d['home']} to {d['dest'] or d['to']}"
    return "https://www.google.com/travel/flights?q=" + quote(q)


def badge(d):
    if d["verdict"] == "confirmed":
        return "#1f7a4d", f"Confirmed: Ignav found ${d['ignav']:.0f} on the sample dates"
    if d["verdict"] == "higher":
        return "#9a5b00", f"Not confirmed: Ignav's cheapest was ${d['ignav']:.0f} on the sample dates (may be gone, or only on the airline's site)"
    if d["verdict"] == "none":
        return "#9a5b00", "Not confirmed: Ignav found no fares on the sample dates"
    if d["verdict"] == "error":
        return "#6b7280", "Not checked: the Ignav search failed this time"
    return "#6b7280", "Not checked: the post didn't give usable airports or dates"


def send_email(deals):
    conf = [d for d in deals if d["verdict"] == "confirmed"]
    first = (conf or deals)[0]
    dest_city = (first["frm"] if first["reverse"] else first["to"]).split(",")[0]
    if len(deals) == 1:
        subject = f"🔥 Sale fare: {first['home']} → {dest_city} ${first['price']:.0f}" + (" ✓" if conf else "")
    else:
        subject = f"🔥 {len(deals)} sale fares: " + ", ".join(
            f"{(d['frm'] if d['reverse'] else d['to']).split(',')[0]} ${d['price']:.0f}" for d in deals[:4])

    cards, text = [], []
    for d in deals:
        city = (d["frm"] if d["reverse"] else d["to"])
        color, note = badge(d)
        route = f"{d['origin'] or d['home']} → {d['dest'] or city}"
        dates = (f"Sample dates: {d['depart']:%a %b} {d['depart'].day} – {d['ret']:%a %b} {d['ret'].day}"
                 if d["depart"] else "")
        avail = escape(d["availability"]) if d.get("availability") else ""
        cards.append(f"""
<div style="border:1px solid #e5e7eb;border-radius:12px;padding:16px;margin:0 0 14px">
  <div style="font-size:11px;font-weight:bold;letter-spacing:.06em;text-transform:uppercase;color:#6b7280">{escape(d['airline'])} · {escape(route)}</div>
  <div style="font-size:22px;font-weight:bold;margin:4px 0">{escape(city)}</div>
  <div style="font-size:28px;font-weight:bold">${d['price']:.0f} <span style="font-size:14px;font-weight:normal;color:#6b7280">round trip, posted price</span></div>
  <div style="font-size:14px;color:{color};margin:6px 0">{escape(note)}</div>
  <div style="font-size:14px;color:#374151;margin:0 0 4px">{dates}</div>
  {f'<div style="font-size:13px;color:#374151;margin:0 0 12px">{avail}</div>' if avail else ''}
  <a href="{escape(d['link'])}" style="display:inline-block;background:#14233a;color:#fff;font-weight:bold;text-decoration:none;padding:10px 16px;border-radius:8px;margin-right:8px">How to book →</a>
  <a href="{escape(gflights(d))}" style="display:inline-block;color:#14233a;font-weight:bold;padding:10px 0">Google Flights</a>
</div>""")
        text.append(f"{d['title']}\n  {note}\n  {dates}\n  {d['link']}")

    body = f"""<div style="font-family:Helvetica,Arial,sans-serif;max-width:560px;margin:auto;color:#14233a">
<p style="font-size:15px">New sale fares from your airports. These often last only hours. US airlines let you cancel free within 24 hours of booking, so book first and decide after.</p>
{''.join(cards)}
<p style="font-size:12px;color:#6b7280">Source: The Flight Deal. Ignav checks one sample date pair, so "not confirmed" doesn't always mean gone.</p></div>"""
    mail("\n\n".join(text), body, subject)


def mail(text, body, subject):
    msg = EmailMessage()
    msg["From"] = f"Fare Watch <{SMTP_USER or 'fare-watch@localhost'}>"
    msg["To"] = TO
    msg["Subject"] = ("[TEST] " if TEST_EMAIL else "") + subject
    msg.set_content(text)
    if body:
        msg.add_alternative(body, subtype="html")
    if not (SMTP_USER and SMTP_PASSWORD and TO):
        Path("outbox").mkdir(exist_ok=True)
        p = Path("outbox") / f"deals-{datetime.now():%Y%m%d-%H%M%S}.eml"
        p.write_bytes(bytes(msg))
        log(f"email not configured, saved to {p}")
        return
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context(), timeout=30) as s:
        s.login(SMTP_USER, SMTP_PASSWORD)
        s.send_message(msg)
    log(f"emailed: {subject}")


# ---------------------------------------------------------------- main
def load_state():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {"seen": {}, "fails": 0, "fail_alerted": False}


def save_state(s):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    s["seen"] = {k: v for k, v in s["seen"].items() if v >= cutoff}
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, indent=1))


def main():
    state = load_state()
    items, failed = [], []
    for url in FEEDS:
        try:
            items += parse_feed(Path(FEED_FILE).read_text(encoding="utf-8") if FEED_FILE else get(url))
        except (RuntimeError, ET.ParseError, OSError) as e:
            failed.append(f"{url}: {e}")
            log(f"feed failed: {e}")

    if failed and not items:
        state["fails"] = state.get("fails", 0) + 1
        if state["fails"] >= FEED_FAIL_ALERT and not state.get("fail_alerted"):
            mail("The deal feed has failed for about 3 hours in a row:\n\n" + "\n".join(failed) +
                 "\n\nIt keeps retrying every 30 minutes. If it doesn't recover, the site may have "
                 "changed its feed and the script needs updating.", None, "⚠ Deal feed isn't working")
            state["fail_alerted"] = True
        save_state(state)
        return     # exit 0: our own alert covers this, no GitHub failure emails every 30 min
    state["fails"], state["fail_alerted"] = 0, False

    now = datetime.now(timezone.utc)
    new = []
    for d in items:
        if d["guid"] in state["seen"] and not TEST_EMAIL:
            continue
        state["seen"][d["guid"]] = now.isoformat()
        if now - d["published"] > timedelta(hours=MAX_AGE_HOURS):
            continue
        home, reverse = matches(d)
        if not home:
            continue
        d.update(home=home, reverse=reverse)
        log(f"match: {d['title']}")
        if check(d):
            log(f"  {d['origin']}-{d['dest']} {d['depart']}..{d['ret']}: {d['verdict']}"
                + (f" (Ignav ${d['ignav']:.0f})" if d["ignav"] else ""))
            new.append(d)

    log(f"{len(items)} posts, {len(new)} new from SLC/DC")
    if new:
        order = {"confirmed": 0, "unchecked": 1, "error": 1, "higher": 2, "none": 3}
        new.sort(key=lambda d: (order[d["verdict"]], d["price"]))
        if DRY_RUN:
            log("dry run: no email sent, nothing marked as seen")
            return
        send_email(new)
    if not (DRY_RUN or TEST_EMAIL):
        save_state(state)


if __name__ == "__main__":
    main()
