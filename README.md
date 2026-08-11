# odds-capture-data

Public UFC moneyline odds capture. Polls BestFightOdds and Pinnacle hourly (five spaced
sub-polls per run) and commits the raw quotes to `data/`.

- `data/bfo_*.csv` — per-book quotes from BestFightOdds
- `data/pinnacle_*.csv` — Pinnacle moneylines
- `data/bfo_events_*.csv` — event dates seen on the BFO homepage

Data is scraped from publicly available sources and published as-is, with no warranty of
accuracy or completeness. Nothing here constitutes betting advice.

<!-- capture pipeline active -->
