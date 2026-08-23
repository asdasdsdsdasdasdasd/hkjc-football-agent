# HKJC Football Scrape Pipeline

Also includes the **v3.2 live betting book** (HT goal unders + weekday FT corner unders).

## Run v3.2 (live book)

```bash
python3 -m pip install -r requirements.txt
npm install
python3 -m playwright install chromium

# 1. Snapshot tomorrow's HKJC odds
node pipeline/snapshot_hkjc_odds_browser.js --all --lang ch --date YYYY-MM-DD --days 1 \
  --out output/odds_snapshots/hkjc-browser-ch-all-YYYY-MM-DD.json

# 2. Need historical records in output/records-*.jsonl (or output/records.json)
#    so Poisson / form have training history.

# 3. Score + cap
PYTHONPATH=. python3 predict_v32.py --date YYYY-MM-DD \
  --snapshot output/odds_snapshots/hkjc-browser-ch-all-YYYY-MM-DD.json
```

v3.2 live cut (`cap_live_v31`):

- markets: `goal_ou_ht` under, or weekday `corner_ou_ft` under with line ≥ 10.5
- odds **1.55–1.80**, composite **0.10–0.50**, max 1 bet per match
- Saturday/Sunday: no corner FT unders
- composite is **market-aware** (`MARKET_AWARE_COMPOSITE` in `pipeline/revise_recommendations.py`): EV + disagreement with the market, not “form+xG all agree”

---


Deterministic Python + Playwright pipeline to discover HKJC football match results, scrape **corner counts** (詳細賽果) and **closing odds** (最後賠率), with SQLite checkpoints and resumable parallel workers.

Target site: [HKJC football results](https://bet.hkjc.com/ch/football/results#search)

---

## What it collects

For each match (`FBxxxx`):

| Field | Source |
|-------|--------|
| `match_id`, date, competition, teams, scores | Discover (GraphQL) + results table |
| `corners` | 詳細賽果 — 半場/全場開出角球 |
| `odds_closing` | 最後賠率 — 8 sections (主客和, 讓球, 入球大細, 角球大細, …) |

Excludes live / same-match-combo markets (`即場`, `同場過關`).

---

## Architecture

```
discover.js (HKJC GraphQL via hkjc-api)
        │
        ▼
  pipeline.db (SQLite, WAL)     ← pending / done / error checkpoints
        │
        ▼
pool_scrape.py (N × Playwright)  ← one calendar day per worker at a time
        │
        ▼
output/records-YYYY-MM-DD.jsonl  ← per-day shards
        │
        ▼
merge_records.py → output/records.json
```

**Recommended runner:** day pool (`run-hkjc-pipeline-pool.sh`) — workers claim days with pending work from a shared queue. Only **pending** or retryable **error** rows are scraped.

> **Note:** Discover uses HKJC GraphQL; scrape uses the results **web page**. In rare cases API metadata (teams/competition) may not match the row shown on the website for the same `match_id`. Treat scraped `teams` on the page as ground truth when validating odds.

---

## Prerequisites

```bash
cd hkjc-football-agent
npm install                    # discover.js (better-sqlite3, hkjc-api)
pip install playwright         # or use system python with playwright
python3 -m playwright install chromium
```

---

## Quick start

From repo root (`/mnt/d/openclaw`):

```bash
# Full run: discover → scrape (1 worker) → merge
./scripts/run-hkjc-pipeline-pool.sh --from 2025-06 --to 2026-06

# Resume scrape only (after IP block / crash)
./scripts/run-hkjc-pipeline-pool.sh --from 2025-06 --to 2026-06 --workers 1 --scrape-only

# Merge JSONL shards only
./scripts/run-hkjc-pipeline-pool.sh --from 2025-06 --to 2026-06 --merge-only
```

### Reset failed matches and retry

```bash
sqlite3 hkjc-football-agent/data/pipeline.db \
  "UPDATE matches SET status='pending', retries=0, last_error=NULL WHERE status='error';"

./scripts/run-hkjc-pipeline-pool.sh --from 2025-06 --to 2026-06 --workers 1 --scrape-only
```

### Reset bulk false errors from date-search timeouts

```bash
sqlite3 hkjc-football-agent/data/pipeline.db \
  "UPDATE matches SET status='pending', retries=0, last_error=NULL
   WHERE status='error' AND last_error LIKE 'Date search failed%';"
```

---

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/run-hkjc-pipeline-pool.sh` | **Primary** — day-pool workers (default **1** worker) |
| `scripts/run-hkjc-pipeline-multi.sh` | Legacy — one worker per calendar month |
| `scripts/run-hkjc-pipeline.sh` | Single-worker discover → scrape |

### Live odds snapshots

Use the browser scraper for upcoming odds snapshots. It opens the HKJC football page with Playwright and parses the visible all-competition odds tables; it does not call `hkjc-api`.

```bash
node pipeline/snapshot_hkjc_odds_browser.js --all --lang en --date 2026-07-03 --days 3
python3 pipeline/recommend_bets.py --snapshot-date 2026-07-03 --best-per-match
```

If `--out` is not supplied, the browser scraper automatically writes a timestamped snapshot:

```text
output/odds_snapshots/hkjc-browser-<lang>-all-<date>-<snapshot_at>.json
```

Downstream tools use `pipeline/betting/snapshots.py` to auto-select saved snapshots:

| Function | Behavior |
|----------|----------|
| `list_snapshots(date=...)` | Lists saved `output/odds_snapshots/*.json`, optionally filtered by snapshot date |
| `latest_snapshot(date=...)` | Picks the newest saved snapshot for that date |
| `latest_two_snapshots(date=...)` | Picks the previous and latest snapshots for movement / CLV checks |

That means this command does not need an explicit file path:

```bash
PYTHONPATH=. python3 pipeline/recommend_bets.py \
  --snapshot-date 2026-07-03 \
  --best-per-match \
  --out output/auto_snapshot_recommendations.csv
```

For the old-model direction workflow, use the latest auto-selected snapshot the same way:

```bash
PYTHONPATH=. python3 pipeline/pick_bet_direction.py \
  --snapshot-date 2026-07-03 \
  --min-train-matches 100 \
  --out output/bet_directions_old_model.csv
```

After multiple snapshots exist, score odds-movement signals and optionally build a supervised dataset:

```bash
PYTHONPATH=. python3 pipeline/predict_odds_movement.py \
  --snapshot-date 2026-07-03 \
  --horizon-minutes 30 \
  --out output/odds_movement_signals.csv \
  --dataset-out output/odds_movement_dataset_30m.csv
```

Optional match-result features can be supplied as JSON keyed by `match_id` (or `date` + `teams`) with fields such as `home_xg`, `away_xg`, `home_shots`, `away_shots`, `home_injuries`, `away_injuries`, `home_lineup_strength`, `away_lineup_strength`, `home_fatigue`, `away_fatigue`, `weather_goal_factor`, and `team_news_edge`:

```bash
PYTHONPATH=. python3 pipeline/fetch_external_features.py \
  --snapshot output/odds_snapshots/hkjc-browser-en-all-2026-07-03-YYYYMMDDTHHMMSSZ.json \
  --providers bsd,bigballs \
  --out output/external_features.json \
  --report-out output/external_feature_coverage.json

PYTHONPATH=. python3 pipeline/recommend_bets.py \
  --snapshot output/odds_snapshots/latest.json \
  --target-features output/external_features.json
```

`fetch_external_features.py` is intentionally coverage-gated. Prefer `--lang en` HKJC snapshots so provider team-name matching uses English names. Set `BSD_API_TOKEN` and/or `BIGBALLS_API_KEY` before using those providers. If fewer than 30% of HKJC matches receive usable features, it exits non-zero and marks the provider as rejected in the report.

### Pool options

```bash
./scripts/run-hkjc-pipeline-pool.sh --from YYYY-MM --to YYYY-MM \
  [--workers N]          # default 1; use 12+ only if HKJC is not rate-limiting you
  [--discover-only]
  [--scrape-only]
  [--merge-only]
  [--headed]             # show browser (debug)
  [--db PATH]
```

---

## Pipeline modules

| File | Role |
|------|------|
| `pipeline/discover.js` | Loop days, paginate GraphQL `matchResult`, upsert into SQLite |
| `pipeline/snapshot_hkjc_odds_browser.js` | Browser scraper for live/upcoming odds snapshots |
| `pipeline/predict_odds_movement.py` | Build odds-movement signals and supervised CLV rows from snapshots |
| `pipeline/fetch_external_features.py` | Fetch xG/shots/injury/lineup features and report HKJC coverage |
| `pipeline/apply_match_features.py` | Merge external xG/shots/injury/lineup/weather features into match JSON |
| `pipeline/scrape.py` | Playwright scraper; React date search; per-match corners + odds |
| `pipeline/pool_scrape.py` | Day queue + multiprocessing worker pool |
| `pipeline/db.py` | Schema, `fetch_pending`, day locks, checkpoints |
| `pipeline/parsers.py` | Corner + 8 closing-odds section parsers |
| `pipeline/storage.py` | JSONL append + merge into `records.json` |
| `pipeline/merge_records.py` | Merge `records-*.jsonl` shards |
| `pipeline/backtest_ev.py` | Walk-forward +EV backtest CLI |
| `pipeline/betting/` | Market adapters, Poisson model, settlement |
| `pipeline/config.py` | Timeouts, delays, worker defaults |

---

## Outputs

| Path | Description |
|------|-------------|
| `data/pipeline.db` | Checkpoint DB (gitignored) |
| `output/records-YYYY-MM-DD.jsonl` | Per-day scrape shards |
| `output/records.json` | Merged final dataset |
| `logs/pool-worker-NN.log` | Per-worker scrape log |
| `logs/pool-scrape-*.log` | Orchestrator log |

### Check progress

```bash
sqlite3 data/pipeline.db "SELECT status, COUNT(*) FROM matches GROUP BY status;"

tail -f logs/pool-worker-01.log
```

### Record shape (excerpt)

```json
{
  "date": "28/06/2026",
  "match_id": "FB0119",
  "competition": "世盃",
  "teams": "巴拿馬 對 英格蘭",
  "scores": { "half_time": "0 : 0", "full_time": "1 : 5" },
  "corners": { "half_time": { "total": 3, "home": 1, "away": 2 } },
  "odds_closing": {
    "主客和": [{ "selection": "巴拿馬 (主隊勝)", "odds": 15.0 }],
    "讓球": [{ "line": "[+2.5/+3]", "odds": 1.59 }]
  }
}
```

---

## How date search works (headless)

HKJC’s datepicker is React-controlled; UI clicks fail headless. The scraper:

1. Opens the results page and selects the **搜尋** tab
2. Waits for React to hydrate (`SEARCH_REACT_READY_MS`)
3. Walks the React fiber tree to call `onChangeSearchParams` + search button (component `Jf`)
4. Waits up to **90s** for result rows or 「共找到」
5. For each match: click 詳細賽果 → parse corners → back → click `%` → parse 最後賠率

Tune waits in `pipeline/config.py`:

| Constant | Default | Meaning |
|----------|---------|---------|
| `DEFAULT_POOL_WORKERS` | `1` | Concurrent browsers |
| `MATCH_DELAY_SEC` | `3.0` | Pause between matches |
| `SEARCH_RESULTS_TIMEOUT_MS` | `90000` | Step 4: wait for table |
| `SEARCH_REACT_READY_MS` | `4000` | Step 3: pre-search hydrate |
| `SEARCH_DAY_ATTEMPTS` | `6` | Retries per day search |

If a day search fails after all retries, matches **stay pending** (not bulk-marked error). The worker skips the day and tries another.

---

## Concurrency and rate limits

HKJC may throttle or block IPs under heavy automated load.

| Workers | Guidance |
|---------|----------|
| **1** | Safest after a block; slow but stable |
| **12** | Worked for bulk runs when not blocked |
| **32** | Often caused mass `Date search failed` timeouts |

Signs of blocking: date search timeouts, empty pages, HTTP errors. **Wait several hours** (or change network) before retrying; keep `--workers 1` and increase `MATCH_DELAY_SEC` if needed.

---

## Manual / debug commands

```bash
cd hkjc-football-agent

# Discover one range
node pipeline/discover.js --start 2026-06-01 --end 2026-06-30 --db data/pipeline.db

# Scrape one day
python3 pipeline/scrape.py --date 2026-06-28 --headed

# Scrape one match
python3 pipeline/scrape.py --match-id FB0119 --headed

# Pool scrape directly
python3 pipeline/pool_scrape.py --start 2025-06-01 --end 2026-06-30 --workers 1
```

---

## Legacy: OpenClaw agent

The older OpenClaw + DeepSeek browser agent is still available for experiments:

```bash
./scripts/run-openclaw-hkjc-football-agent.sh june
```

Production bulk scraping should use **this pipeline** (`run-hkjc-pipeline-pool.sh`), not OpenClaw.

---

## Known limitations

1. **Closing odds coverage** — many matches have scores but empty `odds_closing` (HKJC did not offer 最後賠率 for that market).
2. **Discover vs page mismatch** — GraphQL `teams`/`competition` can differ from the results table for the same `match_id`; validate before trusting World Cup / qualifier odds.
3. **Log line `odds_sections=8`** counts parser keys, not non-empty odds lines.
4. **Discover pagination** — `discover.js` dedupes by `match_id` and caps pages per day to avoid API duplicate loops.

---

## Backtest (+EV)

Walk-forward backtest on merged `records.json`. Reads only JSON; does not touch SQLite.

```bash
cd hkjc-football-agent

python3 -m pipeline.backtest_ev \
  --records output/records.json \
  --markets corner_ou_ft \
  --min-ev 0.05 \
  --min-train-matches 200 \
  --out output/backtest_ev.csv \
  --summary output/backtest_summary.json
```

### Markets (`--markets`, comma-separated)

| Key | HKJC section | Model |
|-----|--------------|-------|
| `corner_ou_ft` | 開出角球大細 | Poisson corners by **competition** + team home/away contribution |
| `corner_ou_ht` | 半場開出角球大細 | Same (half-time) |
| `goal_ou_ft` | 入球大細 | Poisson goals by competition + team split from scoreline |
| `goal_ou_ht` | 半場入球大細 | Same (half-time) |
| `match_1x2` | 主客和 | Competition home/draw/away rates + team home/away win bias |
| `match_1x2_ht` | 半場主客和 | Same (half-time) |

Model groups training data by `competition` (e.g. 美國職業聯賽 vs 日本乙組聯賽). Team rates are tracked **within that competition** — 長崎成功丸 in J2 is separate from the same name elsewhere. Estimates shrink toward the competition baseline when sample size is small.

Every offered line is evaluated; summary includes both **all_bets** and **best_per_match** (highest EV per match).

### Limitations

1. **Corner sample ~1.3k matches** with both outcomes and odds — weak statistical power.
2. **Closing odds only** — backtest assumes you could bet at the closing price; real executable prices are usually worse.
3. **Correlated multi-line bets** — treat `best_per_match` ROI as the less inflated metric.
4. **Poisson totals** — corners/goals are often over-dispersed; v1 keeps the model simple.

### Tests

```bash
python3 -m unittest pipeline.betting.tests.test_betting
```

---

## Model prediction workflow

This project now uses two related but deliberately separate ideas:

1. **Market baseline ranking** — use devigged HKJC odds to rank offered lines by bookmaker margin / market EV.
2. **Old model direction** — inside each offered market line, use the historical model to choose the higher-probability side.

Do not mix these up. The market baseline is not a profitable signal by itself; after devigging, its EV should normally be near or below zero. The old model is the only part that creates a model edge.

### Data inputs

| Input | Purpose |
|-------|---------|
| `output/records.json` | Historical training data, merged from scraped records |
| `output/records-YYYY-MM-DD.jsonl` | Per-day result shards used for retrospective checks |
| `output/odds_snapshots/*.json` | Forward-looking odds snapshots for upcoming matches |
| `output/top3_market_baseline_old_choice.csv` | Current CSV output for top3 market baseline rows with old-model side choice |

Historical evaluation must only train on matches before the target date:

```python
train_pool = [m for m in records if parse_match_date(m["date"]) < target_date]
```

That avoids look-ahead leakage. Breaking this rule makes the model look smarter than it is.

### Approach history

The current process came from several iterations. Keep these distinctions clear, because most losing systems die by mixing evaluation scopes.

| Approach | Status | Why |
|----------|--------|-----|
| Historical +EV backtest on `records.json` | Implemented | Gives walk-forward sanity check, but broad historical ROI was negative |
| Forward recommendation CLI | Implemented | Allows target matches without `scores` / `corners`, reusing existing adapters |
| Devigged implied probability | Implemented | Required to compare model probability against bookmaker margin |
| Model fair odds / breakeven odds | Implemented | Added because raw EV alone hides whether the offered price is actually good |
| Browser odds snapshot | Implemented | Needed for upcoming matches; avoids relying on `hkjc-api` for visible market tables |
| Auto latest snapshot selection | Implemented | `--snapshot-date` picks newest saved snapshot instead of hardcoding a file path |
| Odds movement / CLV signals | Implemented as support tooling | Useful for monitoring price movement; not yet the main bet trigger |
| External feature ingestion | Implemented but coverage-gated | Pulls xG, shots, injuries, lineup, weather if provider matching is good enough |
| BSD API integration | Implemented as provider adapter | Useful only when it matches HKJC teams and returns enough usable features |
| BigBalls API integration | Implemented as secondary provider adapter | Same coverage rule as BSD |
| Asian handicap (`讓球`) model | Not implemented | Needs separate settlement/model; adding it early would create fake precision |
| Pure accuracy prediction | Rejected | Betting needs calibrated probabilities and prices, not just winner accuracy |
| Blind `old_ev` threshold tuning | Rejected as standalone fix | July 1-2 showed higher EV thresholds can still keep bad high-confidence goal picks |

The practical lesson from the history is blunt: historical scores + closing odds alone are not enough to beat the market reliably. The pipeline can produce probabilities; profitability needs either better calibration, better filtering, or information not already priced in.

### Supported markets

| Market key | HKJC section | Current model |
|------------|--------------|---------------|
| `match_1x2` | 主客和 | Scoreline / 1X2 probability model |
| `match_1x2_ht` | 半場主客和 | Half-time 1X2 probability model |
| `goal_ou_ft` | 入球大細 | Poisson total goals |
| `goal_ou_ht` | 半場入球大細 | Poisson half-time goals |
| `corner_ou_ft` | 開出角球大細 | Poisson total corners |
| `corner_ou_ht` | 半場開出角球大細 | Poisson half-time corners |

The key direction tool is:

```bash
PYTHONPATH=. python3 pipeline/pick_bet_direction.py \
  --records output/records.json \
  --snapshot output/odds_snapshots/<snapshot>.json \
  --min-train-matches 100 \
  --out output/bet_directions_old_model.csv
```

`pick_bet_direction.py` is a direction tool. For each market line, it chooses the side with the higher old-model probability:

```text
goal_ou_ft [2.5] over vs under  -> choose whichever old model likes more
match_1x2 home/draw/away        -> choose highest old-model 1X2 probability
```

It outputs:

| Column | Meaning |
|--------|---------|
| `p_model_old` | Old-model probability for the chosen side |
| `p_market` | Devigged market-implied probability |
| `prob_edge` | `p_model_old - p_market` |
| `model_ev` | Expected return at HKJC odds |
| `model_breakeven_odds` | Fair odds from old model |
| `bet_recommendation` | `BET` if `model_ev > 0`, otherwise `NO_BET` |

### External features and BSD API

External features are optional. The model must not invent xG, shots, injury, lineup, weather, fatigue, or team-news data. Those values are only used when supplied through a feature JSON file and merged into each match under `external_features`.

The supported normalized feature keys live in `pipeline/betting/models/external_features.py`:

| Feature key | Meaning |
|-------------|---------|
| `home_xg`, `away_xg` | Expected goals inputs |
| `home_shots`, `away_shots` | Shot-volume inputs |
| `home_injuries`, `away_injuries` | Injury / unavailable-player counts |
| `home_lineup_strength`, `away_lineup_strength` | Lineup confidence or average player score |
| `home_fatigue`, `away_fatigue` | Optional schedule / fatigue signal |
| `weather_goal_factor` | Goal-environment adjustment from weather |
| `team_news_edge` | Optional manually supplied team-news edge |

Fetch provider features from the latest auto snapshot:

```bash
PYTHONPATH=. python3 pipeline/fetch_external_features.py \
  --snapshot-date 2026-07-03 \
  --providers bsd,bigballs \
  --min-score 0.72 \
  --min-coverage 0.30 \
  --out output/external_features.json \
  --report-out output/external_feature_coverage.json
```

BSD provider details:

| Setting / endpoint | Current behavior |
|--------------------|------------------|
| `BSD_BASE_URL` | Defaults to `https://sports.bzzoiro.com` |
| `BSD_API_TOKEN` | Optional; sent as `Authorization: Token <token>` |
| Event list | Tries `/api/events/` with `date_from`, `date_to`, `status=upcoming`, `tz=Asia/Hong_Kong` |
| Alternate event list | Tries `/api/events/?date=...` and `/api/v2/events/` variants |
| Event detail | Calls `/api/v2/events/<event_id>/` |
| Prediction detail | Calls `/api/v2/events/<event_id>/prediction/` |
| Lineup detail | Calls `/api/v2/events/<event_id>/lineups/` |
| Paid odds API | Not used; `/odds/api/` is treated as paid add-on and may return 402 |

The provider matching is fuzzy by team name. Prefer English HKJC snapshots (`--lang en`) when fetching external data, because English names match provider APIs better than Chinese names. Some aliases are hardcoded for World Cup teams, but do not rely on aliases as a general solution.

Coverage gate:

```text
feature_coverage = matches_with_usable_features / hkjc_matches
```

If `feature_coverage < --min-coverage`, `fetch_external_features.py` exits with code `2` and writes a report with:

| Report field | Meaning |
|--------------|---------|
| `hkjc_matches` | HKJC matches in the snapshot |
| `provider_events` | Events returned by external providers |
| `matched_matches` | HKJC matches matched to provider events |
| `feature_matches` | Matched events that produced usable numeric features |
| `feature_coverage` | Coverage ratio used for accept/reject |
| `decision` | `usable` or `reject_provider_for_now` |

Use accepted features in recommendation or direction runs:

```bash
PYTHONPATH=. python3 pipeline/recommend_bets.py \
  --snapshot-date 2026-07-03 \
  --target-features output/external_features.json \
  --best-per-match

PYTHONPATH=. python3 pipeline/pick_bet_direction.py \
  --snapshot-date 2026-07-03 \
  --target-features output/external_features.json \
  --out output/bet_directions_old_model_with_features.csv
```

The coverage gate is intentional. A provider with poor HKJC matching is worse than no provider, because it silently attaches the wrong xG or lineup to the wrong team.

### Top3 recommendation format

The current canvas/report format is intentionally simple:

```text
Date | Match | Teams | Candidate | Market EV | Market Breakeven | Old Model Info
```

The intended selection process is:

1. Extract every offered line from the target match.
2. Devig the market prices.
3. For each market line, let the old model choose the higher-probability side.
4. Rank all candidate lines by market-baseline EV.
5. Keep the top 3 rows per match.
6. Treat a row as a recommended bet only if old-model EV passes the chosen threshold.

Important: top3 is a display/ranking constraint, not the model itself. If a market does not appear in top3, do not include its goal/corner rows in the top3 performance report. If we want to inspect `match_1x2`, add only `match_1x2` as an explicit extra, not every non-top3 market line.

### Bet filters tested

We tested these filters on `records-2026-07-01.jsonl` and `records-2026-07-02.jsonl`:

| Filter | Meaning |
|--------|---------|
| `old_ev > 0` | Bet every top3 row with positive old-model EV |
| `old_ev > 0.1` | Stricter version; only bet rows with at least 10% expected return |
| `World Cup only` | Restrict to `competition == "世盃"` or `match_id` starting with `FB30` |
| `top3 + match_1x2` | Use top3 rows, then add one explicit full-time 1X2 old-model pick per match |

Settlement uses 1 unit flat stake. Quarter lines use `pipeline.betting.settlement.pnl_over_under()`, so half-win / half-loss cases are handled correctly.

### July 1-2 review findings

The July 1-2 retrospective used 20 matches:

| Date | Matches |
|------|---------|
| `2026-07-01` | 12 |
| `2026-07-02` | 8 |

World Cup subset:

| Date | Matches |
|------|---------|
| `2026-07-01` | `FB3005`, `FB3006`, `FB3007` |
| `2026-07-02` | `FB3008`, `FB3009`, `FB3010` |

For all July 1-2 top3 recommended bets with `old_ev > 0`:

| Bets | W-L-P | Accuracy | Net P&L | ROI |
|------|-------|----------|---------|-----|
| 34 | 17-17-0 | 50.0% | -4.365u | -12.8% |

With `old_ev > 0.1` across all July 1-2 matches:

| Bets | W-L-P | Accuracy | Net P&L | ROI |
|------|-------|----------|---------|-----|
| 23 | 11-12-0 | 47.8% | -4.090u | -17.8% |

Raising the EV threshold did not fix the full dataset. The problem was not too many weak bets; it was that some high-EV goal signals were simply wrong.

For World Cup only, using **top3 rows plus explicit `match_1x2`**, with `old_ev > 0.1`:

| Market | Bets | W-L-P | Accuracy | Net P&L | Read |
|--------|------|-------|----------|---------|------|
| Total | 15 | 9-6-0 | 60.0% | +0.830u | Small positive |
| `corner_ou_ft` | 3 | 3-0-0 | 100.0% | +2.540u | Best signal in this sample |
| `corner_ou_ht` | 2 | 1-1-0 | 50.0% | -0.550u | Not convincing |
| `goal_ou_ft` | 4 | 2-2-0 | 50.0% | -0.265u | Weak |
| `goal_ou_ht` | 3 | 2-1-0 | 66.7% | +0.095u | Small edge, tiny sample |
| `match_1x2` | 3 | 1-2-0 | 33.3% | -0.990u | Dragged performance down |

The old model bought these World Cup `match_1x2` rows when `old_ev > 0.1`:

| Match | Pick | Odds | Old p | Old EV | Result |
|-------|------|------|-------|--------|--------|
| `FB3005` 科特迪瓦 對 挪威 | home | 3.55 | 0.4828 | 0.7138 | Lose |
| `FB3007` 墨西哥 對 厄瓜多爾 | home | 2.01 | 0.5656 | 0.1369 | Win |
| `FB3009` 比利時 對 塞內加爾 | home | 1.84 | 0.6131 | 0.1282 | Lose |

### Practical current approach

The least bad rule from this small review is:

```text
World Cup only
+ top3 market-baseline rows
+ old-model side choice
+ old_ev > 0.1
+ prefer corner_ou_ft
```

Be careful with the wording: this is not proven profitable. It is just the only slice in this small sample that did not look like garbage. The cleanest signal was `corner_ou_ft`; `match_1x2` and broad `goal_ou_ft` should not be promoted without more evidence.

### Canvas artifacts used in this review

The Cursor canvases used during the review were:

| Canvas | Purpose |
|--------|---------|
| `old-model-top3-bets.canvas.tsx` | Upcoming top3 report for current snapshot, including requested extra matches |
| `july-01-02-market-predictions.canvas.tsx` | July 1-2 top3 rows with old-model info |
| `july-01-02-recommended-bet-results.canvas.tsx` | Accuracy and P&L for recommended July 1-2 bets |

These are analysis artifacts, not source-of-truth pipeline code.

---

## License / data

Scraped data is from HKJC public results pages. Use responsibly; respect site terms and rate limits.
