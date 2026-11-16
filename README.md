# Wasatch Fares

Watches round-trip fares from Salt Lake City (SLC) for one trip window and
emails an alert when a fare drops well below its usual price. After every
check it also publishes a page showing the lowest price found for each city.

- **Trip:** 10 nights, leaving May 8 – Jun 10, 2027, home by Jun 20
- **Cities:** Dublin, Warsaw, Shanghai, Lima, Buenos Aires
- **Checks:** 4 times a day on GitHub Actions (1 stop max, 2 for Shanghai)
- **Alerts:** Great deal = 25%+ below usual, Rare deal = 40%+ below usual.
  A city stays quiet for 7 days after an alert unless it gets 10% cheaper.

Change the trip with `TRIP_START`, `TRIP_END` and `STAY_DAYS` near the top of
`watch.py`, and the cities with `DESTINATIONS`.

## Setup

Repository secrets (Settings → Secrets and variables → Actions):

- `IGNAV_API_KEY`: fare data key from https://ignav.com
- `ALERT_EMAIL`: who gets alerts (comma-separate several addresses)
- `SMTP_USER`: the Gmail address that sends them
- `SMTP_PASSWORD`: a Gmail app password

GitHub Pages must be set to deploy from **GitHub Actions**
(Settings → Pages → Source).

Test locally with fake fares: `MOCK=1 python watch.py`
