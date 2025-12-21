import csv
import json
import os
import sys
from datetime import datetime, timezone
from urllib.request import urlopen

DATA_DIR = "data"

TRADES_URL = os.environ.get("TRADES_CSV_URL", "").strip()
ADJ_URL = os.environ.get("ADJUSTMENTS_CSV_URL", "").strip()
SETTINGS_URL = os.environ.get("SETTINGS_CSV_URL", "").strip()

def fetch_csv(url: str):
  if not url:
    raise RuntimeError("Missing CSV URL env var.")
  with urlopen(url) as resp:
    raw = resp.read().decode("utf-8", errors="replace")
  reader = csv.DictReader(raw.splitlines())
  return list(reader)

def parse_de_number(s: str):
  if s is None:
    return None
  s = str(s).strip()
  if not s:
    return None
  # 1.234,56 -> 1234.56
  s = s.replace(" ", "")
  s = s.replace(".", "")
  s = s.replace(",", ".")
  try:
    return float(s)
  except:
    return None

def truthy_include(row: dict):
  v = str(row.get("include", "TRUE")).strip().upper()
  return v != "FALSE"

def read_starting_equity(settings_rows):
  # expects a column named starting_equity_$ in Settings sheet
  for r in settings_rows:
    if not truthy_include(r):
      continue
    if "starting_equity_$" in r:
      val = parse_de_number(r.get("starting_equity_$"))
      if val is not None:
        return val
  raise RuntimeError('Could not find "starting_equity_$" in Settings CSV.')

def ensure_data_dir():
  os.makedirs(DATA_DIR, exist_ok=True)

def write_json(name: str, obj):
  path = os.path.join(DATA_DIR, name)
  with open(path, "w", encoding="utf-8") as f:
    json.dump(obj, f, ensure_ascii=False, indent=2)

def main():
  ensure_data_dir()

  # Fetch sources
  settings = fetch_csv(SETTINGS_URL)
  trades = fetch_csv(TRADES_URL)
  adjs = fetch_csv(ADJ_URL)

  # Filter include TRUE
  trades_inc = [r for r in trades if truthy_include(r)]
  adjs_inc = [r for r in adjs if truthy_include(r)]

  starting_equity = read_starting_equity(settings)

  # Minimal “pipeline-alive” outputs (we’ll fill real logic next)
  # NOTE: Option A is locked: cash equity will be broker-truth from daily_pnl + withdrawals/deposits/fees.
  meta = {
    "last_updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "sources": {
      "trades_csv": TRADES_URL,
      "adjustments_csv": ADJ_URL,
      "settings_csv": SETTINGS_URL,
    },
    "starting_equity_$": starting_equity,
    "counts": {
      "trades_rows_total": len(trades),
      "trades_rows_included": len(trades_inc),
      "adjustment_rows_total": len(adjs),
      "adjustment_rows_included": len(adjs_inc),
    },
    "model": {
      "cash_equity_driver": "broker_truth_daily_adjustments_only",
      "trades_unit": "R",
      "daily_adjustments_unit": "$",
      "daily_pnl_applied": "after_trades_same_date (by date, regardless of entry time)"
    }
  }

  # Placeholder files (so dashboard can safely read them immediately)
  write_json("meta.json", meta)
  write_json("equity.json", [])
  write_json("daily_aggregates.json", [])
  write_json("trades_enriched.json", [])
  write_json("kpis_full.json", {
    "trade_count": 0,
    "winrate": None,
    "profit_factor": None,
    "expectancy_R": None,
    "avg_win_R": None,
    "avg_loss_R": None,
    "longest_winning_streak": None,
    "avg_winning_streak": None,
    "longest_losing_streak": None,
    "avg_losing_streak": None,
  })

  print("OK: Wrote data/*.json")

if __name__ == "__main__":
  try:
    main()
  except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)
