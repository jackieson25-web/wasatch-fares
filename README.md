# Wasatch Fares

Watches round-trip fares from Salt Lake City (SLC) and Washington, DC
(DCA or IAD; BWI excluded) for one trip window and
emails an alert when a fare drops well below its usual price. After every
check it also publishes a page showing the lowest price found for each city.

- **Trip:** 10 nights, leaving May 8 – Jun 10, 2027, home by Jun 20
- **From SLC:** Dublin, Warsaw, Shanghai, Lima, Buenos Aires (1 stop max, 2 for Shanghai). Fixed "usual" fares of $725 / $850 / $1,000 / $1,000 / $1,325; an alert fires at 30% under ($508 / $595 / $700 / $700 / $928)
- **From DCA/IAD:** Dublin, Warsaw, Cairo (1 stop max). Fixed "usual" fares of $700 / $760 / $900, so an alert fires at 30% under ($490 / $532 / $630)
- **Checks:** 4 times a day on GitHub Actions
- **Alerts:** Great deal = 30%+ below usual; Scorching deal = 50%+ below usual (🔥 in the email subject).
  A city stays quiet for 7 days after an alert unless it gets 10% cheaper.

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
