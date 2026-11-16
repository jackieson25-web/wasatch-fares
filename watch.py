"""Personal flight-deal watcher.

Checks round-trip fares from Salt Lake City to a few international cities,
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
from urllib.error import HTTPError

# ---------------------------------------------------------------- settings
HOME = os.environ.get("HOME_AIRPORT", "SLC")

# Travel window: the whole trip (out and back) must fall between these dates.
TRIP_START = date.fromisoformat(os.environ.get("TRIP_START", "2027-05-08"))
TRIP_END = date.fromisoformat(os.environ.get("TRIP_END", "2027-06-20"))
STAY_DAYS = int(os.environ.get("STAY_DAYS", "10"))      # trip length in nights

# code, name, rough normal round trip from SLC (used until each travel month
# has enough history)
DESTINATIONS = [
    ("DUB", "Dublin",        950),
    ("WAW", "Warsaw",       1000),
    ("PVG", "Shanghai",     1100),
    ("LIM", "Lima",          800),
    ("EZE", "Buenos Aires", 1250),
]

MAX_STOPS = 1                 # default: skip 2-3 stop junk itineraries
CITY_STOPS = {"PVG": 2}       # per-city exceptions (2 stops saved ~$460 to Shanghai in a test)


def stops_for(dest):
    """Max stops for a city. A MAX_STOPS env var (used by test runs) overrides every city."""
    if os.environ.get("MAX_STOPS"):
        return int(os.environ["MAX_STOPS"])
    return CITY_STOPS.get(dest, MAX_STOPS)


def stops_label():
    if os.environ.get("MAX_STOPS"):
        return f"{os.environ['MAX_STOPS']} stop max"
    extra = ", ".join(f"{s} for {n}" for c, n, _ in DESTINATIONS if (s := CITY_STOPS.get(c)))
    return f"{MAX_STOPS} stop max" + (f" ({extra})" if extra else "")
RARE = float(os.environ.get("RARE_RATIO", "0.60"))      # 40%+ below normal
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
DB = Path(os.environ.get("DB_PATH", "data/fares-v4.db"))   # v4: 10-night trips only


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


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


def cheapest_dates(dest, stay):
    """[(depart, return, price)] for every departure date in the window."""
    if MOCK:
        return mock_dates(dest, stay)
    start, end = departure_window(stay)
    out, lo, failed = [], start, None
    while lo <= end:                      # Ignav allows at most 31 dates per request
        hi = min(lo + timedelta(days=30), end)
        try:
            data = api("/api/fares/cheapest-dates", {
                "origin": HOME, "destination": dest, "stay_days": stay, "market": "US",
                "max_stops": stops_for(dest),
                "departure_date_range": {"start": lo.isoformat(), "end": hi.isoformat()},
            })
            out += [(d["departure_date"], d.get("return_date"), float(d["price"]["amount"]))
                    for d in data.get("dates", []) if d.get("status") == "priced" and d.get("price")]
        except RuntimeError as e:         # skip this month, keep the rest
            log(f"  {dest} {lo:%b %d}-{hi:%b %d} skipped: {str(e)[:120]}")
            failed = e
        lo = hi + timedelta(days=1)
    if not out and failed:
        raise failed
    return out


def flight_details(dest, depart, ret):
    """Airline, stops and a booking link for one date pair (2 API calls)."""
    link = ("https://www.google.com/travel/flights?q=" +
            quote(f"Flights from {HOME} to {dest} on {depart} returning {ret}"))
    if MOCK:
        r = random.Random(dest + depart)
        return r.choice(["Delta", "United", "KLM"]), r.choice([0, 1]), link
    try:
        data = api("/api/fares/round-trip", {"origin": HOME, "destination": dest, "market": "US",
                                             "departure_date": depart, "return_date": ret,
                                             "max_stops": stops_for(dest)})
        its = sorted(data.get("itineraries", []), key=lambda i: i["price"]["amount"])
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


def mock_dates(dest, stay):
    seed = {d[0]: d[2] for d in DESTINATIONS}[dest]
    today = date.today()
    start, end = departure_window(stay)
    rr = random.Random(f"{dest}{today}")
    sale, depth, at = rr.random() < 0.3, rr.uniform(0.5, 0.8), rr.randint(0, 25)
    out = []
    for i in range((end - start).days + 1):
        d = start + timedelta(days=i)
        r = random.Random(f"{dest}{d}{today}")
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
    """)
    return c


def normal_price(c, dest, depart, seed):
    """25th percentile of fares for the same travel month: what you can
    usually get with flexible dates that month. Falls back to the seed price
    until there are MIN_HISTORY_DAYS separate days of data for that month."""
    rows = c.execute("""SELECT price, date(seen) FROM prices WHERE origin=? AND dest=?
                        AND substr(depart,1,7)=? AND seen >= datetime('now', ?)""",
                     (HOME, dest, depart[:7], f"-{HISTORY_DAYS} days")).fetchall()
    if len({r[1] for r in rows}) >= MIN_HISTORY_DAYS:
        return statistics.quantiles(sorted(r[0] for r in rows), n=4)[0], True
    return seed, False


def best_deal(c, code, seed, dates):
    """Best (ratio, depart, ret, price, normal, trusted) across dates, each
    judged against the normal price for its own travel month."""
    best = None
    for dep, ret, price in dates:
        normal, trusted = normal_price(c, code, dep, seed)
        r = price / normal
        if best is None or r < best[0]:
            best = (r, dep, ret, price, normal, trusted)
    return best


# ---------------------------------------------------------------- email
def pretty(iso):
    d = date.fromisoformat(iso)
    return d.strftime("%a %b ") + str(d.day)


def send_email(deals):
    if len(deals) == 1:
        d = deals[0]
        subject = f"✈ {d['name']} ${d['price']:.0f} round trip (usually ~${d['normal']:.0f})"
    else:
        subject = f"✈ {len(deals)} flight deals: " + ", ".join(f"{d['name']} ${d['price']:.0f}" for d in deals[:4])

    cards, text = [], []
    for d in deals:
        stops = "" if d["stops"] is None else (" · Nonstop" if d["stops"] == 0 else f" · {d['stops']} stop{'s' if d['stops'] > 1 else ''}")
        airline = f" · {escape(d['airline'])}" if d["airline"] else ""
        others = ", ".join(f"{pretty(o[0])} (${o[2]:.0f})" for o in d["others"][:5])
        color = "#b4232c" if d["tier"] == "Rare deal" else "#9a5b00"
        basis = "" if d["trusted"] else " · normal = starting estimate"
        cards.append(f"""
<div style="border:1px solid #e5e7eb;border-radius:12px;padding:16px;margin:0 0 14px">
  <div style="font-size:11px;font-weight:bold;letter-spacing:.06em;text-transform:uppercase;color:{color}">{d['tier']} · {HOME} → {d['dest']}</div>
  <div style="font-size:22px;font-weight:bold;margin:4px 0">{escape(d['name'])}</div>
  <div style="font-size:28px;font-weight:bold">${d['price']:.0f}
    <span style="font-size:14px;font-weight:normal;color:#6b7280;text-decoration:line-through">${d['normal']:.0f}</span>
    <span style="font-size:14px;color:#1f7a4d">{d['pct']}% off</span>
    <span style="font-size:12px;font-weight:normal;color:#6b7280">{basis}</span></div>
  <div style="color:#374151;margin:6px 0 10px;font-size:14px">{pretty(d['depart'])} – {pretty(d['ret'])}{airline}{stops}</div>
  {f'<div style="font-size:13px;color:#374151;margin:0 0 12px">Similar prices: {others}</div>' if others else ''}
  <a href="{escape(d['link'])}" style="display:inline-block;background:#14233a;color:#fff;font-weight:bold;text-decoration:none;padding:10px 16px;border-radius:8px">Check this fare →</a>
</div>""")
        text.append(f"{d['tier']}: {d['name']} ${d['price']:.0f} (usually ~${d['normal']:.0f}), "
                    f"{d['depart']} to {d['ret']}. {d['link']}" + (f"\n  Similar: {others}" if others else ""))

    html = f"""<div style="font-family:Helvetica,Arial,sans-serif;max-width:560px;margin:auto;color:#14233a">
<p style="font-size:15px">Round trip from {HOME}, economy, 1 adult, {stops_label()}. Cheap fares can disappear within hours, so confirm before you book.</p>
{''.join(cards)}</div>"""

    msg = EmailMessage()
    msg["From"] = f"Fare Watch <{SMTP_USER or 'fare-watch@localhost'}>"
    msg["To"] = TO
    msg["Subject"] = subject
    msg.set_content("\n\n".join(text))
    msg.add_alternative(html, subtype="html")

    if not (SMTP_USER and SMTP_PASSWORD and TO):
        Path("outbox").mkdir(exist_ok=True)
        p = Path("outbox") / f"{datetime.now():%Y%m%d-%H%M%S}.eml"
        p.write_bytes(bytes(msg))
        log(f"email not configured, saved to {p}")
        return True
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=30) as s:
        s.login(SMTP_USER, SMTP_PASSWORD)
        s.send_message(msg)
    log(f"emailed {TO}: {subject}")
    return True


# ---------------------------------------------------------------- web page
SITE = Path(os.environ.get("SITE_DIR", "site"))


def money(x):
    return f"${x:,.0f}"


def build_site(c, current):
    """Write site/index.html: lowest fare ever found per city, plus today's cheapest.
    The lowest-ever record is kept in its own table so it survives price-history cleanup."""
    c.execute("""CREATE TABLE IF NOT EXISTS lows (dest TEXT PRIMARY KEY, price REAL, depart TEXT,
                                                 ret TEXT, found TEXT)""")
    today = datetime.now(timezone.utc)
    for code, (dep, ret, price) in current.items():
        row = c.execute("SELECT price FROM lows WHERE dest=?", (code,)).fetchone()
        if row is None or price < row[0]:
            c.execute("INSERT OR REPLACE INTO lows VALUES (?,?,?,?,?)",
                      (code, price, dep, ret, today.date().isoformat()))
    c.commit()
    lows = {r[0]: r[1:] for r in c.execute("SELECT * FROM lows")}

    names = {d[0]: d[1] for d in DESTINATIONS}
    order = sorted((code for code in names if code in lows), key=lambda k: lows[k][0])
    try:
        from zoneinfo import ZoneInfo
        stamp = today.astimezone(ZoneInfo("America/Denver")).strftime("%a %b %-d, %-I:%M %p MT")
    except Exception:
        stamp = today.strftime("%a %b %d, %H:%M UTC")
    start, end = departure_window(STAY_DAYS)

    rows = []
    for code in order:
        lp, ldep, lret, found = lows[code]
        cur = current.get(code)
        if cur:
            gap = cur[2] - lp
            now = (f'<span class="now">Today {money(cur[2])}'
                   + (' · <b>at the low</b>' if gap < 1 else f' · {money(gap)} above low') + '</span>')
        else:
            now = '<span class="now">Not checked this time</span>'
        link = gflights(code, ldep, lret)
        rows.append(f"""
      <li class="row">
        <span class="code">{code}</span>
        <div class="mid"><div class="city">{escape(names[code])}</div>
          <div class="dates">{pretty(ldep)} – {pretty(lret)} · found {pretty(found)}</div></div>
        <div class="right"><span class="price">{money(lp)}</span>{now}</div>
        <a class="check" href="{escape(link)}" target="_blank" rel="noopener">Check these dates on Google Flights</a>
      </li>""")

    html = PAGE.format(
        stay=STAY_DAYS, start=pretty(start.isoformat()), end=pretty(end.isoformat()),
        home_by=pretty(TRIP_END.isoformat()), stops=stops_label(), stamp=stamp, rows="".join(rows),
        hours="4 times a day")
    SITE.mkdir(parents=True, exist_ok=True)
    (SITE / "index.html").write_text(html, encoding="utf-8")
    log(f"page written to {SITE / 'index.html'}")


def gflights(dest, depart, ret):
    return ("https://www.google.com/travel/flights?q=" +
            quote(f"Flights from {HOME} to {dest} on {depart} returning {ret}"))


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
.stamp {{ color:var(--muted); font-size:.9rem; font-variant-numeric:tabular-nums; }}
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
footer {{ border-top:1px solid var(--line); }}
footer .wrap {{ padding-block:20px 28px; color:var(--muted); font-size:.84rem; }}
</style></head>
<body>
<header class="band"><div class="wrap">
  <div class="logo"><svg viewBox="0 0 32 32" width="26" height="26" aria-hidden="true"><path d="M2 24 L11 11 L16 18 L21 9 L30 24 Z" fill="#e9a23b"/><path d="M21 9 L24 14 L19 14 Z" fill="#faf6ef"/></svg>Wasatch Fares</div>
  <div>
    <p class="eyebrow">Spring 2027 trip watch</p>
    <h1>{stay} days away, <em>somewhere far</em>, this May or June.</h1>
    <p class="lede">Round-trip fares from Salt Lake City to five cities, checked {hours}. This page shows the lowest price found for each one and updates after every check.</p>
    <div class="facts"><span class="fact">SLC departures</span><span class="fact">{stay} nights</span>
      <span class="fact">Leave {start} – {end}</span><span class="fact">Home by {home_by}</span><span class="fact">{stops}</span></div>
  </div>
</div></header>
<main><div class="wrap"><section aria-labelledby="low">
  <div class="head"><h2 id="low">Lowest prices found</h2><span class="stamp">Last checked {stamp}</span></div>
  <ol class="board">{rows}
  </ol>
  <p class="note">Each big number is the cheapest {stay}-night round trip seen for that city since tracking started: economy, 1 adult, taxes included. "Today" is the cheapest fare at the latest check. A record low can sell out within hours, so check today's price before you count on it.</p>
</section></div></main>
<footer><div class="wrap">Prices are what Ignav reported at the time of each check. Confirm on the booking site before you buy.</div></footer>
</body></html>
"""


# ---------------------------------------------------------------- main
def main():
    if not MOCK and not API_KEY:
        sys.exit("Set IGNAV_API_KEY (or MOCK=1 to test).")
    c = db()
    found, errors, current = [], 0, {}
    start, end = departure_window(STAY_DAYS)
    if start > end:
        log(f"travel window {TRIP_START} to {TRIP_END} has passed; nothing to check")
        return
    log(f"checking {len(DESTINATIONS)} cities from {HOME}: {STAY_DAYS}-night trips leaving "
        f"{pretty(start.isoformat())} to {pretty(end.isoformat())}, back by {pretty(TRIP_END.isoformat())}, {stops_label()}")
    for code, name, seed in DESTINATIONS:
        try:
            dates = cheapest_dates(code, STAY_DAYS)
        except Exception as e:
            log(f"{name}: failed: {e}")
            errors += 1
            continue
        if not dates:
            log(f"{name}: no prices returned")
            continue
        c.executemany("INSERT INTO prices(origin, dest, depart, ret, price) VALUES (?,?,?,?,?)",
                      [(HOME, code, d, r, p) for d, r, p in dates])
        c.commit()
        current[code] = min(dates, key=lambda x: x[2])
        ratio, dep, ret, price, normal, trusted = best_deal(c, code, seed, dates)
        tier = "Rare deal" if ratio <= RARE else "Great deal" if ratio <= GREAT else None
        log(f"{name:13} {pretty(dep):10} ${price:>5.0f}  normal ~${normal:>5.0f}{'' if trusted else '*'} "
            f"{round(100 * (1 - ratio)):>3}% off  {tier or ''}")
        if not tier:
            continue
        prev = c.execute("SELECT MIN(price) FROM alerts WHERE dest=? AND sent >= datetime('now', ?)",
                         (code, f"-{REALERT_DAYS} days")).fetchone()[0]
        if prev is not None and price > prev * 0.9:
            log(f"  already alerted at ${prev:.0f} recently, skipping")
            continue
        airline, stops, link = flight_details(code, dep, ret)
        others = sorted([x for x in dates if x[2] <= price * 1.08 and x[0] != dep])[:8]
        found.append(dict(dest=code, name=name, price=price, normal=round(normal), tier=tier,
                          pct=round(100 * (1 - ratio)), depart=dep, ret=ret, airline=airline,
                          stops=stops, link=link, others=others, trusted=trusted))

    try:
        build_site(c, current)
    except Exception as e:            # the page is a bonus; never block alerts
        log(f"page build failed: {e}")

    if found:
        found.sort(key=lambda d: -d["pct"])
        if send_email(found):
            c.executemany("INSERT INTO alerts(dest, price) VALUES (?,?)", [(d["dest"], d["price"]) for d in found])
    else:
        log("no new deals")
    c.execute("DELETE FROM prices WHERE seen < datetime('now', ?)", (f"-{HISTORY_DAYS} days",))
    c.commit()
    c.close()
    if errors == len(DESTINATIONS):
        sys.exit("every route failed")


if __name__ == "__main__":
    main()
