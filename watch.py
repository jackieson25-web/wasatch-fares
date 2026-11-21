"""Personal flight-deal watcher.

Checks round-trip fares from Salt Lake City and Washington DC to a few cities,
works out what each route normally costs for that month of travel, and emails
you when one drops well below that.

Run:  python watch.py           (live, needs IGNAV_API_KEY)
      MOCK=1 python watch.py    (fake fares, for testing)
"""
import json
import os
import random
import smtplib
import sqlite3
import ssl
import statistics
import sys
import time
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

import price_guard
from urllib.error import HTTPError

# ---------------------------------------------------------------- settings
HOME = os.environ.get("HOME_AIRPORT", "SLC")

# Travel window: the whole trip (out and back) must fall between these dates.
TRIP_START = date.fromisoformat(os.environ.get("TRIP_START", "2027-05-08"))
TRIP_END = date.fromisoformat(os.environ.get("TRIP_END", "2027-06-20"))
STAY_DAYS = int(os.environ.get("STAY_DAYS", "10"))      # trip length in nights

# Each group is a set of home airports and the cities watched from them.
# Destinations: code, name, rough normal round trip (used until each travel
# month has enough history). A group with several airports takes the cheapest
# of them for each date.
GROUPS = [
    # FIXED "normal" fares: roughly what each trip went for in Nov 2026 checks.
    # An email goes out at 30% under: Dublin <= $508, Warsaw <= $595,
    # Shanghai <= $700, Lima <= $700, Buenos Aires <= $928.
    dict(key=HOME, label="Salt Lake City", short="SLC", origins=[HOME], fixed_normal=True,
         great=0.70, dests=[
        ("DUB", "Dublin",        725),
        ("WAW", "Warsaw",        850),
        ("PVG", "Shanghai",     1000),
        ("LIM", "Lima",         1000),
        ("EZE", "Buenos Aires", 1325),
    ]),
    # Searched as the WAS metro code (1 lookup instead of 2) with BWI thrown out.
    # Prices here are FIXED "normal" fares, not learned from history: roughly what
    # these trips were going for in Nov 2026 checks (DUB $679, WAW $765, CAI $810-941).
    # An email goes out at 30% under: Dublin <= $490, Warsaw <= $532, Cairo <= $630,
    # which is true sale territory for the East Coast in May/June.
    dict(key="DC", label="Washington, DC", short="DCA/IAD", origins=["WAS"], exclude=["BWI"],
         fixed_normal=True, great=0.70, dests=[
        ("DUB", "Dublin",   700),
        ("WAW", "Warsaw",   760),
        ("CAI", "Cairo",    900),
    ]),
]

MAX_STOPS = 1                 # default: skip 2-3 stop junk itineraries
CITY_STOPS = {(HOME, "PVG"): 2}   # per-route exceptions (2 stops saved ~$460 to Shanghai in a test)


def rkey(g, dest):
    """Storage key for a route. SLC routes keep the bare city code so old history still counts."""
    return dest if g["key"] == HOME else f"{g['key']}-{dest}"


def stops_for(g, dest):
    """Max stops for a route. A MAX_STOPS env var (used by test runs) overrides every route."""
    if os.environ.get("MAX_STOPS"):
        return int(os.environ["MAX_STOPS"])
    return CITY_STOPS.get((g["key"], dest), MAX_STOPS)


def stops_label(g=None):
    if os.environ.get("MAX_STOPS"):
        return f"{os.environ['MAX_STOPS']} stop max"
    groups = [g] if g else GROUPS
    extra = ", ".join(f"{s} for {n}" for gg in groups for c, n, _ in gg["dests"]
                      if (s := CITY_STOPS.get((gg["key"], c))))
    return f"{MAX_STOPS} stop max" + (f" ({extra})" if extra else "")
SCORCHING = float(os.environ.get("SCORCHING_RATIO", "0.50"))   # 50%+ below normal (replaced 40% "Rare" 11-20)
GREAT = float(os.environ.get("GREAT_RATIO", "0.75"))    # 25%+ below normal
REALERT_DAYS = 7          # quiet period per city unless it gets 10% cheaper
HISTORY_DAYS = 120
MIN_HISTORY_DAYS = 14     # distinct days of data for a travel month before trusting it

API_KEY = os.environ.get("IGNAV_API_KEY", "")
MOCK = os.environ.get("MOCK") == "1"
TO = os.environ.get("ALERT_EMAIL", "")
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
# v5 (2026-11-21): prices are for 2 travelers, stored PER PERSON. v4 was 1 adult, so it isn't mixed in.
DB = Path(os.environ.get("DB_PATH", "data/fares-v5.db"))

# Two travelers (Jack + wife). Ignav's calendar price is the whole-party total, so every
# price is divided by ADULTS and handled per person. Usual fares/thresholds stay per person.
ADULTS = int(os.environ.get("ADULTS", "2"))
NEW_LOW_MIN = 15          # $ per person a fare must beat the old all-time low by to email
NEW_LOW_MIN_DAYS = 7      # days of checks for a city before new-low emails start
DIGEST_WEEKDAY = 6        # Sunday (Mountain time): weekly summary email
DIGEST_HOUR = 6           # first run on Sunday at/after 6 AM MT sends it
PAGE_URL = "https://jackieson25-web.github.io/wasatch-fares/"
STALE_HOURS = 12          # page shows a warning if it hasn't updated in this long


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def per_person(amount):
    return float(amount) / ADULTS


# ---------------------------------------------------------------- fare data
def api(path, body, tries=3):
    """POST to Ignav. Retries temporary upstream errors (424/429/503 aren't billed)."""
    for attempt in range(tries):
        req = Request("https://ignav.com" + path, data=json.dumps(body).encode(),
                      headers={"X-Api-Key": API_KEY, "Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=90) as r:
                return json.load(r)
        except HTTPError as e:
            msg = f"{path} HTTP {e.code}: {e.read()[:300].decode(errors='ignore')}"
            if e.code not in (424, 429, 500, 502, 503) or attempt == tries - 1:
                raise RuntimeError(msg)
        except OSError as e:          # timeouts, dropped connections
            msg = f"{path}: {e}"
            if attempt == tries - 1:
                raise RuntimeError(msg)
        time.sleep(4 * (attempt + 1))


def departure_window(stay):
    """First and last departure dates that keep the whole trip inside the window."""
    return max(TRIP_START, date.today() + timedelta(days=1)), TRIP_END - timedelta(days=stay)


def cheapest_dates(g, dest, seed, stay):
    """[(depart, return, price, airport)] for every departure date in the window,
    cheapest of the group's airports for each date."""
    best, failed = {}, None
    for origin in g["origins"]:
        try:
            for dep, ret, price in (mock_dates(origin, dest, seed, stay) if MOCK
                                    else airport_dates(g, origin, dest, stay)):
                if dep not in best or price < best[dep][2]:
                    best[dep] = (dep, ret, price, origin)
        except RuntimeError as e:
            failed = e
    if not best and failed:
        raise failed
    return [best[d] for d in sorted(best)]


def airport_dates(g, origin, dest, stay):
    start, end = departure_window(stay)
    out, lo, failed = [], start, None
    while lo <= end:                      # Ignav allows at most 31 dates per request
        hi = min(lo + timedelta(days=30), end)
        try:
            data = api("/api/fares/cheapest-dates", {
                "origin": origin, "destination": dest, "stay_days": stay, "market": "US",
                "max_stops": stops_for(g, dest), "adults": ADULTS,
                "departure_date_range": {"start": lo.isoformat(), "end": hi.isoformat()},
            })
            out += [(d["departure_date"], d.get("return_date"), per_person(d["price"]["amount"]))
                    for d in data.get("dates", []) if d.get("status") == "priced" and d.get("price")]
        except RuntimeError as e:         # skip this month, keep the rest
            log(f"  {origin}-{dest} {lo:%b %d}-{hi:%b %d} skipped: {str(e)[:120]}")
            failed = e
        lo = hi + timedelta(days=1)
    if not out and failed:
        raise failed
    return out


def round_trip(g, origin, dest, depart, ret):
    """Cheapest-first itineraries for one date pair, minus any that leave from or
    come back to an excluded airport (e.g. BWI inside the WAS metro code)."""
    data = api("/api/fares/round-trip", {"origin": origin, "destination": dest, "market": "US",
                                         "departure_date": depart, "return_date": ret,
                                         "max_stops": stops_for(g, dest), "adults": ADULTS})
    skip = set(g.get("exclude", ()))

    def ok(i):
        out, back = i["outbound"].get("segments", []), i.get("inbound", {}).get("segments", [])
        return not ((out and out[0].get("departure_airport") in skip) or
                    (back and back[-1].get("arrival_airport") in skip))
    return sorted((i for i in data.get("itineraries", []) if ok(i)), key=lambda i: i["price"]["amount"])


def verify(g, dest, dates):
    """For groups with excluded airports, the date scan can't say which airport a
    fare is from, so reprice its cheapest date with a full search (1 API call) and
    keep only allowed airports. Returns ((depart, return, price, airport), itinerary)."""
    dep, ret, price, _ = min(dates, key=lambda x: x[2])
    if MOCK:
        return (dep, ret, round(price * 1.06), "IAD"), None
    its = round_trip(g, g["origins"][0], dest, dep, ret)
    if not its:
        return None, None
    best = its[0]
    airport = best["outbound"]["segments"][0].get("departure_airport") or g["origins"][0]
    log(f"  {dest} {dep}: calendar ${price:.0f} pp vs full search ${per_person(best['price']['amount']):.0f} pp "
        f"(raw ${float(best['price']['amount']):.0f} for {ADULTS})")
    return (dep, ret, per_person(best["price"]["amount"]), airport), best


def flight_details(g, origin, dest, depart, ret, best=None):
    """Airline, stops and a booking link for one date pair (1-2 API calls)."""
    link = gflights(origin, dest, depart, ret)
    if MOCK:
        r = random.Random(dest + depart)
        return r.choice(["Delta", "United", "KLM"]), r.choice([0, 1]), link
    try:
        if best is None:
            its = round_trip(g, origin, dest, depart, ret)
            if not its:
                return None, None, link
            best = its[0]
        segs = best["outbound"].get("segments", [])
        airline = best["outbound"].get("carrier") or (segs[0]["operating_carrier_name"] if segs else None)
        stops = max(0, len(segs) - 1)
        try:
            links = api("/api/fares/booking-links", {"ignav_id": best["ignav_id"]})
            for opt in links.get("booking_options", []):
                for l in opt.get("links", []):
                    if l.get("url"):
                        return airline, stops, l["url"]
        except RuntimeError:
            pass
        return airline, stops, link
    except Exception as e:
        log(f"  details for {dest} failed: {e}")
        return None, None, link


def mock_dates(origin, dest, seed, stay):
    today = date.today()
    start, end = departure_window(stay)
    rr = random.Random(f"{origin}{dest}{today}")
    sale, depth, at = rr.random() < 0.3, rr.uniform(0.5, 0.8), rr.randint(0, 25)
    out = []
    for i in range((end - start).days + 1):
        d = start + timedelta(days=i)
        r = random.Random(f"{origin}{dest}{d}{today}")
        f = r.uniform(0.93, 1.25)
        if sale and at <= i <= at + 15 and r.random() < 0.6:
            f = depth * r.uniform(0.95, 1.08)
        out.append((d.isoformat(), (d + timedelta(days=stay)).isoformat(), round(seed * f)))
    return out


# ---------------------------------------------------------------- storage
def db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS prices (origin TEXT, dest TEXT, depart TEXT, ret TEXT, price REAL,
                                       seen TEXT DEFAULT (datetime('now')));
    CREATE INDEX IF NOT EXISTS p_dest ON prices(origin, dest, depart);
    CREATE TABLE IF NOT EXISTS alerts (dest TEXT, price REAL, sent TEXT DEFAULT (datetime('now')));
    CREATE TABLE IF NOT EXISTS lows (dest TEXT PRIMARY KEY, price REAL, depart TEXT, ret TEXT,
                                     found TEXT, origin TEXT);
    -- today's cheapest per city after every check (verified price for DC): feeds charts + digest
    CREATE TABLE IF NOT EXISTS snapshots (dest TEXT, price REAL, depart TEXT, origin TEXT,
                                          seen TEXT DEFAULT (datetime('now')));
    CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
    """)
    return c


def normal_price(c, group, dest, depart, seed):
    """25th percentile of fares for the same travel month: what you can
    usually get with flexible dates that month. Falls back to the seed price
    until there are MIN_HISTORY_DAYS separate days of data for that month."""
    rows = c.execute("""SELECT price, date(seen) FROM prices WHERE origin=? AND dest=?
                        AND substr(depart,1,7)=? AND seen >= datetime('now', ?)""",
                     (group, dest, depart[:7], f"-{HISTORY_DAYS} days")).fetchall()
    if len({r[1] for r in rows}) >= MIN_HISTORY_DAYS:
        return statistics.quantiles(sorted(r[0] for r in rows), n=4)[0], True
    return seed, False


def best_deal(c, group, code, seed, dates):
    """Best (ratio, depart, ret, price, airport, normal, trusted) across dates,
    each judged against the normal price for its own travel month."""
    best = None
    for dep, ret, price, origin in dates:
        normal, trusted = normal_price(c, group, code, dep, seed)
        r = price / normal
        if best is None or r < best[0]:
            best = (r, dep, ret, price, origin, normal, trusted)
    return best


# ---------------------------------------------------------------- email
def pretty(iso):
    d = date.fromisoformat(iso)
    return d.strftime("%a %b ") + str(d.day)


def send_email(deals):
    if len(deals) == 1:
        d = deals[0]
        subject = f"{'🔥 SCORCHING: ' if d['tier'] == 'Scorching deal' else '✈ '}{d['name']} ${d['price']:.0f} per person (usually ~${d['normal']:.0f})"
    else:
        hot = any(d["tier"] == "Scorching deal" for d in deals)
        subject = f"{'🔥 SCORCHING: ' if hot else '✈ '}{len(deals)} flight deals: " + ", ".join(f"{d['name']} ${d['price']:.0f}" for d in deals[:4])

    cards, text = [], []
    for d in deals:
        stops = "" if d["stops"] is None else (" · Nonstop" if d["stops"] == 0 else f" · {d['stops']} stop{'s' if d['stops'] > 1 else ''}")
        airline = f" · {escape(d['airline'])}" if d["airline"] else ""
        others = ", ".join(f"{pretty(o[0])} (${o[2]:.0f})" for o in d["others"][:5])
        color = "#b4232c" if d["tier"] == "Scorching deal" else "#9a5b00"
        basis = "" if d["trusted"] else " · normal = starting estimate"
        cards.append(f"""
<div style="border:1px solid #e5e7eb;border-radius:12px;padding:16px;margin:0 0 14px">
  <div style="font-size:11px;font-weight:bold;letter-spacing:.06em;text-transform:uppercase;color:{color}">{d['tier']} · {d['origin']} → {d['dest']}</div>
  <div style="font-size:22px;font-weight:bold;margin:4px 0">{escape(d['name'])}</div>
  <div style="font-size:28px;font-weight:bold">${d['price']:.0f}<span style="font-size:14px;font-weight:normal"> per person</span>
    <span style="font-size:14px;font-weight:normal;color:#6b7280;text-decoration:line-through">${d['normal']:.0f}</span>
    <span style="font-size:14px;color:#1f7a4d">{d['pct']}% off</span>
    <span style="font-size:12px;font-weight:normal;color:#6b7280">{basis}</span></div>
  <div style="color:#374151;margin:6px 0 10px;font-size:14px">{money(d['price'] * ADULTS)} total for {ADULTS} · {pretty(d['depart'])} – {pretty(d['ret'])}{airline}{stops}</div>
  {f'<div style="font-size:13px;color:#374151;margin:0 0 12px">Similar prices: {others}</div>' if others else ''}
  <a href="{escape(d['link'])}" style="display:inline-block;background:#14233a;color:#fff;font-weight:bold;text-decoration:none;padding:10px 16px;border-radius:8px">Check this fare →</a>
</div>""")
        text.append(f"{d['tier']}: {d['name']} ${d['price']:.0f} per person, {money(d['price'] * ADULTS)} for {ADULTS} (usually ~${d['normal']:.0f} pp), "
                    f"{d['depart']} to {d['ret']}. {d['link']}" + (f"\n  Similar: {others}" if others else ""))

    html = f"""<div style="font-family:Helvetica,Arial,sans-serif;max-width:560px;margin:auto;color:#14233a">
<p style="font-size:15px">Round trip, economy, {ADULTS} adults (prices per person, {ADULTS} seats checked), {stops_label()}. Cheap fares can disappear within hours, so confirm before you book.</p>
{''.join(cards)}</div>"""
    return deliver(subject, "\n\n".join(text), html)


def deliver(subject, text, html):
    msg = EmailMessage()
    msg["From"] = f"Fare Watch <{SMTP_USER or 'fare-watch@localhost'}>"
    msg["To"] = TO
    msg["Subject"] = subject
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    if not (SMTP_USER and SMTP_PASSWORD and TO):
        Path("outbox").mkdir(exist_ok=True)
        p = Path("outbox") / f"{datetime.now():%Y%m%d-%H%M%S-%f}.eml"
        p.write_bytes(bytes(msg))
        log(f"email not configured, saved to {p}")
        return True
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=30) as s:
        s.login(SMTP_USER, SMTP_PASSWORD)
        s.send_message(msg)
    log(f"emailed {TO}: {subject}")
    return True


def daily(c, key, days=60):
    """[(day, cheapest per-person fare that day)] from the after-check snapshots."""
    return c.execute("""SELECT date(seen), MIN(price) FROM snapshots WHERE dest=?
                        AND seen >= datetime('now', ?) GROUP BY date(seen) ORDER BY 1""",
                     (key, f"-{days} days")).fetchall()


def week_change(c, key, price):
    """Per-person change vs the cheapest fare on the latest day at least 7 days ago, or None."""
    row = c.execute("""SELECT MIN(price) FROM snapshots WHERE dest=? AND date(seen) =
                         (SELECT MAX(date(seen)) FROM snapshots WHERE dest=? AND seen <= datetime('now', '-7 days'))""",
                    (key, key)).fetchone()
    return None if row[0] is None else price - row[0]


def change_words(diff):
    if diff is None:
        return "no week-ago price yet"
    if abs(diff) < 5:
        return "about the same as a week ago"
    return f"{'down' if diff < 0 else 'up'} {money(abs(diff))} from a week ago"


def card(title, lines, link=None, accent="#14233a"):
    btn = (f'<a href="{escape(link)}" style="display:inline-block;margin-top:8px;color:#14233a;font-weight:bold">'
           'Check this fare →</a>') if link else ""
    body = "".join(f'<div style="font-size:14px;color:#374151;margin-top:2px">{l}</div>' for l in lines)
    return (f'<div style="border:1px solid #e5e7eb;border-left:4px solid {accent};border-radius:10px;'
            f'padding:12px 14px;margin:0 0 10px"><div style="font-size:16px;font-weight:bold">{title}</div>'
            f'{body}{btn}</div>')


def send_new_lows(lows):
    """One email listing every city that just set a new all-time low."""
    if len(lows) == 1:
        d = lows[0]
        subject = f"📉 New low: {d['name']} {money(d['price'])} per person (was {money(d['prev'])})"
    else:
        subject = f"📉 {len(lows)} new lows: " + ", ".join(f"{d['name']} {money(d['price'])}" for d in lows[:4])
    cards, text = [], []
    for d in lows:
        lines = [f"<b>{money(d['price'])} per person</b> · {money(d['price'] * ADULTS)} for {ADULTS} · "
                 f"old low {money(d['prev'])}",
                 f"{pretty(d['depart'])} – {pretty(d['ret'])} · from {d['origin']}"]
        cards.append(card(escape(d["name"]), lines, d["link"], "#1f7a4d"))
        text.append(f"{d['name']}: new low {money(d['price'])} per person ({money(d['price'] * ADULTS)} for {ADULTS}), "
                    f"was ${d['prev']:.0f}. {d['depart']} to {d['ret']}. {d['link']}")
    html = f"""<div style="font-family:Helvetica,Arial,sans-serif;max-width:560px;margin:auto;color:#14233a">
<p style="font-size:15px">Cheapest price seen since tracking started (at least {money(NEW_LOW_MIN)} per person under the old low). Not necessarily a deal, but the best so far.</p>
{''.join(cards)}<p style="font-size:13px"><a href="{PAGE_URL}">All prices and trends →</a></p></div>"""
    return deliver(subject, "\n\n".join(text) + f"\n\n{PAGE_URL}", html)


def send_digest(c, current):
    """Sunday summary: every city's price now, all-time low, and week-over-week change."""
    lows = {r[0]: r[1] for r in c.execute("SELECT dest, price FROM lows")}
    sections, text, moved = [], [], []
    for g in GROUPS:
        rows = []
        for code, city, _ in g["dests"]:
            key = rkey(g, code)
            if key not in current:
                continue
            dep, ret, price, origin = current[key]
            diff = week_change(c, key, price)
            if diff is not None:
                moved.append(diff)
            lp = lows.get(key, price)
            low = "at its all-time low" if price - lp < 1 else f"all-time low {money(lp)}"
            accent = ("#1f7a4d" if diff is not None and diff <= -5 else
                      "#b4232c" if diff is not None and diff >= 5 else "#c9d1dc")
            rows.append((price, card(f"{escape(city)} · {money(price)} per person",
                                     [f"{money(price * ADULTS)} for {ADULTS} · {change_words(diff)} · {low}",
                                      f"Cheapest: {pretty(dep)} – {pretty(ret)} from {origin}"],
                                     gflights(origin, code, dep, ret), accent)))
            text.append(f"{g['short']} to {city}: {money(price)} pp ({money(price * ADULTS)} for {ADULTS}), "
                        f"{change_words(diff)}, {low}")
        if rows:
            sections.append(f'<h3 style="font-size:15px;margin:18px 0 8px">From {escape(g["label"])}</h3>'
                            + "".join(r[1] for r in sorted(rows, key=lambda r: r[0])))
    if not sections:
        return False
    downs, ups = sum(1 for d in moved if d <= -5), sum(1 for d in moved if d >= 5)
    subject = f"🗓 Weekly fares: {downs} down, {ups} up this week" if moved else "🗓 Weekly fares summary"
    html = f"""<div style="font-family:Helvetica,Arial,sans-serif;max-width:560px;margin:auto;color:#14233a">
<p style="font-size:15px">Your weekly look at {STAY_DAYS}-night round trips for {ADULTS}, prices per person. Green edge = cheaper than a week ago, red = pricier, gray = flat or too new to tell.</p>
{''.join(sections)}<p style="font-size:13px;margin-top:16px"><a href="{PAGE_URL}">See the trend charts →</a></p></div>"""
    return deliver(subject, "\n".join(text) + f"\n\n{PAGE_URL}", html)


def mountain_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Denver"))
    except Exception:
        return datetime.now(timezone.utc) - timedelta(hours=7)


def maybe_digest(c, current):
    now = mountain_now()
    iso = now.isocalendar()
    week = f"{iso[0]}-W{iso[1]:02d}"
    forced = os.environ.get("DIGEST_NOW") == "1"
    if not forced and not (now.weekday() == DIGEST_WEEKDAY and now.hour >= DIGEST_HOUR):
        return
    sent = c.execute("SELECT v FROM meta WHERE k='digest_week'").fetchone()
    if not forced and sent and sent[0] == week:
        return
    try:
        if send_digest(c, current):
            c.execute("INSERT OR REPLACE INTO meta(k, v) VALUES ('digest_week', ?)", (week,))
            c.commit()
            log(f"weekly summary sent ({week})")
    except Exception as e:            # never let the summary break a run
        log(f"weekly summary failed: {e}")


def sparkline(series, now_price):
    """Small inline SVG trend of the daily cheapest fare. Hover a day for its price."""
    if len(series) < 2:
        return '<div class="trend">Trend line starts after 2 days of checks</div>'
    W, H, P = 200, 40, 5
    ps = [p for _, p in series]
    lo, hi = min(ps), max(ps)
    span = max(hi - lo, 1.0)
    n = len(series)
    xs = [P + (W - 2 * P) * i / (n - 1) for i in range(n)]
    ys = [P + (H - 2 * P) * (1 - (p - lo) / span) for p in ps]
    path = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    step = (W - 2 * P) / (n - 1)
    hits = "".join(
        f'<g class="hit"><rect x="{max(0, x - step / 2):.1f}" y="0" width="{step:.1f}" height="{H}"/>'
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4"/><title>{pretty(d)}: {money(p)} per person</title></g>'
        for (d, p), x, y in zip(series, xs, ys))
    label = (f"Cheapest fare per person over the last {n} days: low {money(lo)}, high {money(hi)}, "
             f"now {money(now_price)}")
    return (f'<svg class="spark" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="{label}">'
            f'<path d="{path}"/><circle class="end" cx="{xs[-1]:.1f}" cy="{ys[-1]:.1f}" r="4"/>{hits}</svg>'
            f'<div class="trend">{n}-day range {money(lo)}–{money(hi)}</div>')


# ---------------------------------------------------------------- web page
SITE = Path(os.environ.get("SITE_DIR", "site"))


def money(x):
    return f"${x:,.0f}"


def build_site(c, current):
    """Write site/index.html: lowest fare ever found per city, plus today's cheapest.
    The lowest-ever record is kept in its own table so it survives price-history cleanup."""
    today = datetime.now(timezone.utc)
    for key, (dep, ret, price, origin) in current.items():
        row = c.execute("SELECT price FROM lows WHERE dest=?", (key,)).fetchone()
        if row is None or price < row[0]:
            c.execute("INSERT OR REPLACE INTO lows(dest, price, depart, ret, found, origin) VALUES (?,?,?,?,?,?)",
                      (key, price, dep, ret, today.date().isoformat(), origin))
    c.commit()
    lows = {r[0]: r[1:] for r in c.execute("SELECT dest, price, depart, ret, found, origin FROM lows")}

    try:
        from zoneinfo import ZoneInfo
        stamp = today.astimezone(ZoneInfo("America/Denver")).strftime("%a %b %-d, %-I:%M %p MT")
    except Exception:
        stamp = today.strftime("%a %b %d, %H:%M UTC")
    updated_ms = int(today.timestamp() * 1000)
    start, end = departure_window(STAY_DAYS)

    sections = []
    for g in GROUPS:
        names = {rkey(g, d[0]): (d[0], d[1]) for d in g["dests"]}
        order = sorted((k for k in names if k in lows),
                       key=lambda k: current[k][2] if k in current else lows[k][0])
        rows = []
        for key in order:
            code, name = names[key]
            lp, ldep, lret, found, lorig = lows[key]
            cur = current.get(key)
            if cur:                       # big number = today's cheapest fare
                dep, ret, price, origin = cur
                low = ('<span class="now"><b>All-time low</b></span>' if price - lp < 1 else
                       f'<span class="now">All-time low {money(lp)} · {pretty(ldep)} · found {pretty(found)}</span>')
            else:                         # city failed this check: fall back to the record
                dep, ret, price, origin = ldep, lret, lp, lorig or g["origins"][0]
                low = '<span class="now">All-time low · not checked this time</span>'
            via = f"From {origin} · " if g["key"] != HOME else ""
            moved = change_words(week_change(c, key, price)) if cur else ""
            trend = sparkline(daily(c, key), price)
            rows.append(f"""
      <li class="row">
        <span class="code">{code}</span>
        <div class="mid"><div class="city">{escape(name)}</div>
          <div class="dates">{via}{pretty(dep)} – {pretty(ret)}</div>
          <div class="chart">{trend}</div></div>
        <div class="right"><span class="price">{money(price)}</span><span class="pp">per person · {money(price * ADULTS)} for {ADULTS}</span>{low}<span class="now">{moved}</span></div>
        <a class="check" href="{escape(gflights(origin, code, dep, ret))}" target="_blank" rel="noopener">Check this fare on Google Flights</a>
      </li>""")
        if not rows:
            continue
        hid = g["key"].lower()
        sections.append(f"""
<section aria-labelledby="{hid}">
  <div class="head"><h2 id="{hid}">From {escape(g['label'])} <span class="sub">{escape(g['short'])} · {escape(stops_label(g))}</span></h2></div>
  <ol class="board">{''.join(rows)}
  </ol>
</section>""")

    html = PAGE.format(
        stay=STAY_DAYS, start=pretty(start.isoformat()), end=pretty(end.isoformat()),
        home_by=pretty(TRIP_END.isoformat()), stamp=stamp, sections="".join(sections),
        hours="4 times a day", adults=ADULTS, updated_ms=updated_ms, stale_hours=STALE_HOURS)
    SITE.mkdir(parents=True, exist_ok=True)
    (SITE / "index.html").write_text(html, encoding="utf-8")
    log(f"page written to {SITE / 'index.html'}")


def gflights(origin, dest, depart, ret):
    return ("https://www.google.com/travel/flights?q=" +
            quote(f"Flights from {origin} to {dest} on {depart} returning {ret} for {ADULTS} adults"))


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wasatch Fares</title>
<meta name="robots" content="noindex">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600;9..144,700&family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root {{ --bg:#faf6ef; --surface:#fff; --line:#e7e1d6; --ink:#14233a; --ink-2:#3a4b63; --muted:#6b7280;
  --band:#14233a; --band-ink:#faf6ef; --band-muted:#c9d1dc; --gold:#e9a23b; --gold-ink:#8a5300;
  --good:#1f7a4d; --serif:"Fraunces",Georgia,serif; --sans:"DM Sans",system-ui,-apple-system,"Segoe UI",sans-serif; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f1828; --surface:#172338; --line:#26344c; --ink:#eef1f5;
  --ink-2:#c3ccd8; --muted:#93a0b2; --band:#0a1220; --band-muted:#a9b4c3; --gold:#f0b14f; --gold-ink:#f5c77d;
  --good:#5fc38f; color-scheme:dark; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink); font-family:var(--sans); line-height:1.55; -webkit-font-smoothing:antialiased; }}
.wrap {{ max-width:880px; margin:0 auto; padding-left:max(16px,env(safe-area-inset-left,0px)); padding-right:max(16px,env(safe-area-inset-right,0px)); }}
h1,h2 {{ font-family:var(--serif); line-height:1.1; letter-spacing:-.01em; margin:0; text-wrap:balance; }}
a {{ color:inherit; }} a:focus-visible {{ outline:2px solid var(--gold); outline-offset:3px; }}
.band {{ background:var(--band); color:var(--band-ink); }}
.band .wrap {{ padding-block:22px 40px; display:grid; gap:28px; }}
.logo {{ display:inline-flex; align-items:center; gap:10px; font-family:var(--serif); font-weight:700; font-size:1.2rem; }}
.eyebrow {{ text-transform:uppercase; letter-spacing:.12em; font-size:.75rem; font-weight:700; color:var(--gold); margin:0 0 10px; }}
h1 {{ font-size:clamp(2rem,5.5vw,3.1rem); }} h1 em {{ font-style:italic; color:var(--gold); }}
.lede {{ color:var(--band-muted); max-width:36em; margin:14px 0 0; font-size:1.05rem; }}
.facts {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:18px; }}
.fact {{ border:1px solid rgba(250,246,239,.25); border-radius:999px; padding:5px 13px; font-size:.88rem; }}
main .wrap {{ padding-block:32px 48px; display:grid; gap:36px; }}
.head {{ display:flex; justify-content:space-between; align-items:baseline; flex-wrap:wrap; gap:8px 20px; }}
h2 {{ font-size:1.6rem; }}
section h2 {{ font-size:1.25rem; }}
.sub {{ font-family:var(--sans); font-size:.85rem; font-weight:500; color:var(--muted); letter-spacing:0; margin-left:6px; }}.stamp {{ color:var(--muted); font-size:.9rem; font-variant-numeric:tabular-nums; }}
.board {{ list-style:none; margin:14px 0 0; padding:0; background:var(--surface); border:1px solid var(--line); border-radius:14px; overflow:hidden; }}
.row {{ display:grid; grid-template-columns:64px minmax(0,1fr) auto; gap:4px 18px; align-items:center; padding:18px 20px; border-top:1px solid var(--line); }}
.row:first-child {{ border-top:0; }}
.mid {{ min-width:0; }}
.code {{ font-family:var(--serif); font-weight:700; font-size:1.3rem; letter-spacing:.04em; color:var(--gold-ink); }}
.city {{ font-weight:700; font-size:1.1rem; }}
.dates {{ color:var(--ink-2); font-size:.92rem; }}
.right {{ text-align:right; display:grid; justify-items:end; gap:4px; }}
.price {{ font-family:var(--serif); font-weight:700; font-size:1.8rem; line-height:1; font-variant-numeric:tabular-nums; }}
.now {{ font-size:.84rem; color:var(--muted); font-variant-numeric:tabular-nums; white-space:nowrap; }}
.now b {{ color:var(--good); }}
.check {{ grid-column:2/4; font-size:.88rem; font-weight:500; color:var(--ink-2); text-decoration:underline; text-decoration-color:var(--line); text-underline-offset:3px; justify-self:start; }}
.check:hover {{ text-decoration-color:var(--gold); }}
@media (max-width:520px) {{ .row {{ grid-template-columns:48px minmax(0,1fr) auto; padding:16px; gap:4px 12px; }}
  .code {{ font-size:1.05rem; }} .price {{ font-size:1.45rem; }} .now {{ white-space:normal; }} }}
.note {{ color:var(--muted); font-size:.88rem; margin:12px 0 0; max-width:60em; }}
.pp {{ font-size:.84rem; color:var(--ink-2); font-variant-numeric:tabular-nums; white-space:nowrap; }}
.chart {{ margin-top:8px; }}
.spark {{ display:block; max-width:100%; height:auto; overflow:visible; }}
.spark path {{ fill:none; stroke:var(--ink-2); stroke-width:2; stroke-linejoin:round; stroke-linecap:round; }}
.spark .end {{ fill:var(--gold); stroke:var(--surface); stroke-width:2; }}
.spark .hit rect {{ fill:transparent; }}
.spark .hit circle {{ fill:var(--ink); stroke:var(--surface); stroke-width:2; opacity:0; }}
.spark .hit:hover circle {{ opacity:1; }}
.trend {{ font-size:.8rem; color:var(--muted); font-variant-numeric:tabular-nums; margin-top:2px; }}
.stale {{ background:#fff4e0; color:#6b3d00; border:1px solid #f0c27a; border-radius:10px; padding:12px 16px; font-weight:500; }}
@media (prefers-color-scheme: dark) {{ .stale {{ background:#3a2a10; color:#f5d9a8; border-color:#7a5a20; }} }}
footer {{ border-top:1px solid var(--line); }}
footer .wrap {{ padding-block:20px 28px; color:var(--muted); font-size:.84rem; }}
</style></head>
<body>
<header class="band"><div class="wrap">
  <div class="logo"><svg viewBox="0 0 32 32" width="26" height="26" aria-hidden="true"><path d="M2 24 L11 11 L16 18 L21 9 L30 24 Z" fill="#e9a23b"/><path d="M21 9 L24 14 L19 14 Z" fill="#faf6ef"/></svg>Wasatch Fares</div>
  <div>
    <p class="eyebrow">Spring 2027 trip watch</p>
    <h1>{stay} days away, <em>somewhere far</em>, this May or June.</h1>
    <p class="lede">Round-trip fares from Salt Lake City and from Washington, DC, checked {hours}. This page shows today's cheapest fare for each city, next to the lowest ever found, and updates after every check.</p>
    <div class="facts"><span class="fact">SLC or DCA/IAD departures</span><span class="fact">{stay} nights</span>
      <span class="fact">Leave {start} – {end}</span><span class="fact">Home by {home_by}</span></div>
  </div>
</div></header>
<main><div class="wrap">
  <div id="stale" class="stale" role="status" hidden></div>
  <div class="head"><h2>Current prices</h2><span class="stamp">Last checked {stamp}</span></div>
  {sections}
  <p class="note">Each big number is the cheapest {stay}-night round trip for that city at the latest check, per person, with {adults} seats on the same flights: economy, taxes included. The line under each city is its cheapest fare each day; hover it for that day's price. For Washington it's the cheapest date from Reagan (DCA) or Dulles (IAD), never BWI, checked with a full fare search. Below it is the lowest fare seen since tracking started, with its departure date and the day it was found.</p>
</div></main>
<footer><div class="wrap">Prices are what Ignav reported at the time of each check. Confirm on the booking site before you buy.</div></footer>
<script>(function(){{var h=(Date.now()-{updated_ms})/36e5;if(h>{stale_hours}){{var e=document.getElementById("stale");
e.textContent="Heads up: these prices are "+Math.floor(h)+" hours old. The last few checks may have failed, so prices could be out of date.";e.hidden=false;}}}})();</script>
</body></html>
"""


# ---------------------------------------------------------------- main
def find_new_lows(c, current, skip, names):
    """Cities whose price now beats their all-time low by NEW_LOW_MIN per person.
    Waits for NEW_LOW_MIN_DAYS of checks so early runs don't email a 'new low' every time."""
    lows = {r[0]: r[1] for r in c.execute("SELECT dest, price FROM lows")}
    out = []
    for key, (dep, ret, price, origin) in current.items():
        prev = lows.get(key)
        days = c.execute("SELECT COUNT(DISTINCT date(seen)) FROM snapshots WHERE dest=?", (key,)).fetchone()[0]
        if key in skip or prev is None or days < NEW_LOW_MIN_DAYS or price > prev - NEW_LOW_MIN:
            continue
        name, code = names[key]
        log(f"new all-time low: {name} ${price:.0f} pp (was ${prev:.0f})")
        out.append(dict(name=name, price=price, prev=prev, depart=dep, ret=ret, origin=origin,
                        link=gflights(origin, code, dep, ret)))
    return out


def main():
    if not MOCK and not API_KEY:
        sys.exit("Set IGNAV_API_KEY (or MOCK=1 to test).")
    if not MOCK and not price_guard.allowed(DB.parent):
        log("Ignav price check failed, so no fares were checked this run (see price guard line above)")
        return
    c = db()
    found, errors, current = [], 0, {}
    start, end = departure_window(STAY_DAYS)
    if start > end:
        log(f"travel window {TRIP_START} to {TRIP_END} has passed; nothing to check")
        return
    log(f"{STAY_DAYS}-night trips leaving {pretty(start.isoformat())} to {pretty(end.isoformat())}, "
        f"back by {pretty(TRIP_END.isoformat())}, {stops_label()}")
    routes = [(g, d) for g in GROUPS for d in g["dests"]]
    names = {rkey(g, d[0]): (d[1] if g["key"] == HOME else f"{d[1]} from DC", d[0]) for g, d in routes}
    for g, (code, city, seed) in routes:
        key = rkey(g, code)
        name = names[key][0]
        try:
            dates = cheapest_dates(g, code, seed, STAY_DAYS)
        except Exception as e:
            log(f"{g['short']}-{code}: failed: {e}")
            errors += 1
            continue
        if not dates:
            log(f"{g['short']}-{code}: no prices returned")
            continue
        c.executemany("INSERT INTO prices(origin, dest, depart, ret, price) VALUES (?,?,?,?,?)",
                      [(g["key"], code, d, r, p) for d, r, p, _ in dates])
        c.commit()
        best_it = None
        if g.get("exclude"):
            try:
                v, best_it = verify(g, code, dates)
            except Exception as e:
                log(f"{g['short']}-{code}: repricing failed: {e}")
                errors += 1
                continue
            if v is None:
                log(f"{g['short']}-{code}: only {'/'.join(g['exclude'])} fares on the cheapest date, skipping")
                continue
            dates = [v]
        current[key] = min(dates, key=lambda x: x[2])
        if g.get("fixed_normal"):
            dep, ret, price, origin = current[key]
            ratio, normal, trusted = price / seed, seed, True
        else:
            ratio, dep, ret, price, origin, normal, trusted = best_deal(c, g["key"], code, seed, dates)
        tier = "Scorching deal" if ratio <= SCORCHING else "Great deal" if ratio <= g.get("great", GREAT) else None
        log(f"{origin}-{code} {city:13} {pretty(dep):10} ${price:>5.0f}  normal ~${normal:>5.0f}{'' if trusted else '*'} "
            f"{round(100 * (1 - ratio)):>3}% off  {tier or ''}")
        if not tier:
            continue
        prev = c.execute("SELECT MIN(price) FROM alerts WHERE dest=? AND sent >= datetime('now', ?)",
                         (key, f"-{REALERT_DAYS} days")).fetchone()[0]
        if prev is not None and price > prev * 0.9:
            log(f"  already alerted at ${prev:.0f} recently, skipping")
            continue
        airline, stops, link = flight_details(g, origin, code, dep, ret, best_it)
        others = sorted([x for x in dates if x[2] <= price * 1.08 and x[0] != dep])[:8]
        found.append(dict(key=key, dest=code, origin=origin, name=name, price=price, normal=round(normal),
                          tier=tier, pct=round(100 * (1 - ratio)), depart=dep, ret=ret, airline=airline,
                          stops=stops, link=link, others=others, trusted=trusted))

    c.executemany("INSERT INTO snapshots(dest, price, depart, origin) VALUES (?,?,?,?)",
                  [(k, v[2], v[0], v[3]) for k, v in current.items()])
    c.commit()
    new_lows = find_new_lows(c, current, {d["key"] for d in found}, names)

    try:
        build_site(c, current)
    except Exception as e:            # the page is a bonus; never block alerts
        log(f"page build failed: {e}")

    if found:
        found.sort(key=lambda d: -d["pct"])
        if send_email(found):
            c.executemany("INSERT INTO alerts(dest, price) VALUES (?,?)", [(d["key"], d["price"]) for d in found])
    else:
        log("no new deals")
    if new_lows:
        try:
            send_new_lows(new_lows)
        except Exception as e:
            log(f"new-low email failed: {e}")
    maybe_digest(c, current)
    c.execute("DELETE FROM prices WHERE seen < datetime('now', ?)", (f"-{HISTORY_DAYS} days",))
    c.commit()
    c.close()
    if errors == len(routes):
        sys.exit("every route failed")


if __name__ == "__main__":
    main()
