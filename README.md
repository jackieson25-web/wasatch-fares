# Wasatch Fares

Watches round-trip fares from Salt Lake City (SLC) and Washington, DC
(DCA or IAD; BWI excluded) for one trip window and
emails an alert when a fare drops well below its usual price. After every
check it also publishes a page showing the lowest price found for each city.

- **Trip:** 9–12 nights (10 every check; 9, 11 and 12 on the first check of each day), May 8 – Jun 20, 2027
- **From SLC:** Dublin, Warsaw, Shanghai, Lima, Berlin, Budapest (1 stop max, 2 for Shanghai). Fixed "usual" fares in `GROUPS` in watch.py; an alert fires at 30% under.
- **From DCA/IAD:** Dublin, Warsaw, Berlin (1 stop max). Fixed "usual" fares in `GROUPS`; an alert fires at 30% under.
- **Checks:** 4 times a day on GitHub Actions
- **Travelers:** 2 adults. Every search asks for 2 seats; prices are shown per person (and the total for 2), and the "usual" fares and alert thresholds are per person.
- **Alerts:** Great deal = 30%+ below usual; Scorching deal = 50%+ below usual (🔥 in the email subject).
  A city stays quiet for 7 days after an alert unless it gets 10% cheaper.

## Other emails and the page

- **New all-time low:** one email when a city beats its lowest price so far by $15+ per person (starts after 7 days of checks).
- **Weekly summary:** Sunday morning (first run after 6 AM MT): every city's price, its all-time low, and the change from a week ago.
- **Page:** a 60-day trend line per city (hover for each day's price), and a warning banner if prices haven't updated in 12+ hours.

- **Prices climbing:** one email when a city is $60+ (or 8%+) above its 14-day low, after 7 days of checks; then quiet for a week unless it climbs another $50.
- **Two-city trips:** once a day, fly into one city and home from the other (Dublin/Warsaw and Berlin/Warsaw, from both SLC and DC), both directions, on the 3 cheapest dates. Shown on the page and in the weekly summary. Getting between the two cities is up to you.
- **Date grid:** tap "Every departure date" under any city to see the cheapest trip leaving each day, shaded cheaper to pricier.
- **Madeira from DC:** 6-night round trip in June 2027 (DCA/IAD, not BWI), checked every run: one calendar scan, then the 2 cheapest dates rechecked. It alerts at 30% under its usual fare, like the cities. (Tried Lisbon 2 + Madeira 4 first; the stopover ticket cost too much.)
- **Speed:** Ignav searches run 6 at a time, so even the daily big run finishes in a few minutes when Ignav is slow.
- **Cost:** about 5,300 Ignav requests a month (~$10.60).

## Ignav price guard (price_guard.py)

Before any Ignav call, both scripts read https://ignav.com/pricing. If the price is
above the approved $2.00 per 1,000 requests (`APPROVED_PER_1K` in `price_guard.py`),
or the price can't be read, they make **no Ignav calls** and Jack is emailed. Calls
resume only after the approved price is raised to match.

## Sale fares (deals.py)

Every 30 minutes, `deals.py` reads The Flight Deal's public feed and keeps
posts that leave from Salt Lake City or Washington DC (DCA/IAD, not BWI). For each one it
runs a live Ignav search on the post's sample dates (about 1 request per deal,
pennies a month) and emails it marked **Confirmed** or **Not confirmed**.
Posts older than 48 hours are skipped, and each post is emailed once. To test it,
run the *Sale fares* workflow with "dry run" ticked; that logs the last 10 days of
matches without emailing.

Change the trip with `TRIP_START`, `TRIP_END` and `STAY_DAYS` near the top of
`watch.py`, and the home airports and cities with `GROUPS`.

## Setup

Repository secrets (Settings → Secrets and variables → Actions):

- `IGNAV_API_KEY`: fare data key from https://ignav.com
- `ALERT_EMAIL`: who gets alerts (comma-separate several addresses)
- `SMTP_USER`: the Gmail address that sends them
- `SMTP_PASSWORD`: a Gmail app password

GitHub Pages must be set to deploy from **GitHub Actions**
(Settings → Pages → Source).

Test locally with fake fares: `MOCK=1 python watch.py`
