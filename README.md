# Fare Radar

Flight deal tracker for Vancouver (YVR) and Manila (MNL).

Three times a day a GitHub Action pulls the cheapest round-trip fares travelers recently
found from each home airport (Travelpayouts / Aviasales Data API), marks routes priced
well under that route's usual fare, emails new deals through Gmail, and republishes the
page on GitHub Pages.

## Files

- `fetch_deals.py` gets fares and finds deals. Writes `deals.json` and `data/`.
- `notify.py` emails new deals. `python3 notify.py --dry-run` prints the email instead.
- `config.json` sets what gets emailed: minimum savings, deal types, and watched places
  (`dest` is a city code such as `TYO`, `SEL`, `LON`; `max_usd` is your target price in USD).
- `index.html` is the page. It reads `deals.json`.
- `.github/workflows/fare-check.yml` is the schedule.

## Repository secrets (Settings > Secrets and variables > Actions)

| Name | Value |
| --- | --- |
| `TP_TOKEN` | Travelpayouts API token |
| `GMAIL_USER` | Gmail address that sends the alerts |
| `GMAIL_APP_PASSWORD` | Gmail app password for that address |
| `ALERT_TO` | Where alerts go (optional, defaults to `GMAIL_USER`) |

## How deals are judged

A route's usual price is the median of all fares found for it over the coming year
(at least 4 fares needed). A fare 20% or more under that is a Deal, 35% or more a Hot
deal, and 55% or more a Possible error fare. Prices are cached from recent searches,
so always confirm before booking.
