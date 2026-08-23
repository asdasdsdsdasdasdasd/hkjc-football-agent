"""Pipeline constants and defaults."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"
LOGS_DIR = ROOT / "logs"

DEFAULT_DB = DATA_DIR / "pipeline.db"
DEFAULT_RECORDS_JSON = OUTPUT_DIR / "records.json"
DEFAULT_RECORDS_JSONL = OUTPUT_DIR / "records.jsonl"

RESULTS_URL = "https://bet.hkjc.com/ch/football/results#search"

ODDS_SECTIONS = [
    "主客和",
    "半場主客和",
    "讓球",
    "半場讓球",
    "入球大細",
    "半場入球大細",
    "球隊入球大細",
    "球隊半場入球大細",
    "開出角球大細",
    "半場開出角球大細",
    "球隊開出角球大細",
    "球隊半場開出角球大細",
]

EXCLUDE_ODDS_HEADERS = ("即場", "同場過關")

MAX_RETRIES = 3
MATCH_DELAY_SEC = 3.0
SEARCH_DAY_ATTEMPTS = 6
SEARCH_DAY_BACKOFF_SEC = 5.0
DAY_SEARCH_FAIL_BACKOFF_SEC = 30.0
DEFAULT_POOL_WORKERS = 1
PAGE_LOAD_TIMEOUT_MS = 90_000
DETAIL_WAIT_MS = 20_000
# Step 3: wait for React search UI before driving internal state
SEARCH_TAB_SETTLE_MS = 1_000
SEARCH_DATE_INPUT_TIMEOUT_MS = 45_000
SEARCH_REACT_READY_MS = 4_000
# Step 4: wait for HKJC to return and render result rows
SEARCH_RESULTS_TIMEOUT_MS = 90_000
SEARCH_POST_LOAD_MS = 2_000
