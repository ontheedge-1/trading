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

def norm_key(s: str) -> str:
  """Normalize keys for robust matching."""
  if s is None:
    return ""
  s = str(s).strip().lower()
  # unify separators
  for ch in [" ", "-", "–", "—", ".", ":", "/"]:
    s = s.replace(ch, "_")
  # collapse multiple underscores
  while "__" in s:
    s = s.replace("__", "_")
  return s

def read_starting_equity(settings_rows):
  """
  Robustly reads starting equity from Settings CSV.

  Supports:
  A) Column header exists:
     starting_equity_$   (single row or multiple)
  B) Key/Value table:
     key,value   with a row where key == starting_equity_$
  C) Other common header pairs:
     setting/value, name/value, parameter/value, key/val, etc.
  """
  if not settings_rows:
    raise RuntimeError("Settings CSV is empty.")

  # Build normalized header list from first row keys
  headers = list(settings_rows[0].keys())
  headers_norm = [norm_key(h) for h in headers]
  header_map = {norm_key(h): h for h in headers}  # normalized -> actual

  # --- Case A: direct column ---
  # accept variants
  direct_candidates = [
    "starting_equity_$",
    "starting_equity_",
    "starting_equity",
    "start_equity_$",
    "start_equity",
    "initial_equity_$",
    "initial_equity",
    "starting_balance_$",
    "starting_balance",
    "start_balance_$",
    "start_balance",
  ]
  for cand in direct_candidates:
    if cand in headers_norm:
      col = header_map[cand]
      for r in settings_rows:
        if not truthy_include(r):
          continue
        val = parse_de_number(r.get(col))
        if val is not None:
          return val

  # --- Case B/C: key/value style ---
  # Detect likely key column and value column
  key_col_candidates = ["key", "setting", "name", "parameter", "field", "variable"]
  val_col_candidates = ["value", "val", "amount", "number", "usd", "$", "equity"]

  key_col = None
  val_col = None

  for kn in key_col_candidates:
    if kn in headers_norm:
      key_col = header_map[kn]
      break

  for vn in val_col_candidates:
    if vn in headers_norm:
      val_col = header_map[vn]
      break

  # If not found, try fallback: assume first two columns are key/value
  if key_col is None or val_col is None:
    if len(headers) >= 2:
      key_col = key_col or headers[0]
      val_col = val_col or headers[1]

  # Now scan rows for a key that matches starting equity
  target_keys = set(direct_candidates) | {
    "starting_capital",
    "starting_capital_$",
    "starting_account",
    "starting_account_$",
  }

  for r in settings_rows:
    if not truthy_include(r):
      continue
    k_raw = r.get(key_col, "")
    k = norm_key(k_raw)
    if k in target_keys:
      val = parse_de_number(r.get(val_col))
      if val is not None:
        return val

  # If still nothing, show helpful debug context
  raise RuntimeError(
    'Could not find starting equity in Settings CSV. '
    'Expected either a column "starting_equity_$" OR a key/value row where key == "starting_equity_$". '
    f"Detected headers: {headers}"
  )

def ensure_data_dir():
  os.makedirs(DATA_DIR, exist_ok=True)

def write_json(name: str, obj):
  path = os.path.join(DATA_DIR, name)
  with open(path, "w", encoding="utf-8") as f:
    json.dump(obj, f, ensure_ascii=False, indent=2)

def main():
  ensure_data_dir()

  settings = fetch_csv(SETTINGS_URL)
  trades = fetch_csv(TRADES_URL)
  adjs = fetch_csv(ADJ_URL)

  trades_inc = [r for r in trades if truthy_include(r)]
  adjs_inc = [r for r in adjs if truthy_include(r)]

  starting_equity = read_starting_equity(settings)

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

  # Pipeline-alive placeholder outputs (we’ll fill real logic next)
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
