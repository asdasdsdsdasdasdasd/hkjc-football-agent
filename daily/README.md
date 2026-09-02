# Daily v3.2 books

Each cron run writes `YYYY-MM-DD.md` plus the official `*_v32_live.json` from `predict_v32.py`.

`*_v32_watch.json` is optional: rows that look like the live markets/odds band but fail `cap_live_v31` (usually composite outside 0.10–0.50). Watch rows are not live bets.
