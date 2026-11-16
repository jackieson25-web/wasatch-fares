# Fare Watch

Checks round-trip fares from Salt Lake City four times a day and emails you
when a flight to one of your places drops well below its normal price.

Destinations (edit `DESTINATIONS` in `watch.py`): Lima, Amsterdam, Zurich, Rome,
Tokyo (HND), Shanghai, New York (JFK), Honolulu, Buenos Aires, Warsaw.

- **Normal price** = what you can usually get on that route with flexible dates
  (25th percentile of the last 90 days of prices).
- **Great deal** = 25%+ below normal · **Rare deal** = 40%+ below normal.
- One email per check with all new deals. A city won't be emailed again for
  7 days unless it gets 10% cheaper.
- Trips are 7 nights, departing anywhere in the next 6 months. Change with the
  `STAY_DAYS` and `DAYS_AHEAD` environment variables.

## Setup (about 10 minutes)

1. **Fare data key.** Sign up at https://ignav.com and copy your API key.
   Expect about 1,200–1,500 requests a month: the first 1,000 are free, then
   $2 per 1,000.
2. **Gmail app password.** Turn on 2-Step Verification for your Google account,
   then create one at https://myaccount.google.com/apppasswords.
3. **GitHub repo.** Create a new **private** repo and upload these files,
   keeping the `.github/workflows` folder.
   (GitHub turns off schedules in public repos after 60 days without activity.)
4. **Secrets.** In the repo, go to Settings → Secrets and variables → Actions
   → New repository secret, and add:
   - `IGNAV_API_KEY`: your Ignav key
   - `ALERT_EMAIL`: where alerts go
   - `SMTP_USER`: the Gmail address that sends them (can be the same one)
   - `SMTP_PASSWORD`: the 16-character app password
5. **First run.** Go to Actions → Fare watch → Run workflow. The log shows each
   city's best price against its normal price.

After that it runs automatically at about 6am, noon, 6pm and midnight Mountain time.

## Test locally

```bash
MOCK=1 python watch.py     # fake fares; the email is saved to ./outbox
```

## Notes

- If GitHub's cache of price history ever gets cleared, the watcher starts
  over using the rough seed prices in `watch.py`. Real history rebuilds after
  one run.
- Tokyo uses Haneda (HND). To watch Narita too, add `("NRT", "Tokyo Narita", 1200)`
  (this adds about 120 requests a month).
