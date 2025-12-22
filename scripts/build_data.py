# scripts/build_data.py
# Canonical rebuild (Option A):
# - Cash equity is broker-truth from Daily_Adjustments only (daily_pnl + deposits/withdrawals/fees/corrections)
# - Trades are R-only analytics (KPIs + streaks + daily R aggregates)
# - Drawdown tiers (0–12 / 12–18 / >18) are sticky until NEW ATH
# - Equity-step resizing (5%) uses EQUITY END (cash equity end-of-day)
# - Effective risk ($) = base_equity_for_sizing * risk_pct * dd_multiplier

import csv
import json
import os
import re
import sys
from datetime import datetime, timezone
from urllib.request import urlopen
from collections import defaultdict

DATA_DIR = "data"

TRADES_URL = os.environ.get("TRADES_CSV_URL", "").strip()
ADJ_URL = os.environ.get("ADJUSTMENTS_CSV_URL", "").strip()
SETTINGS_URL = os.environ.get("SETTINGS_CSV_URL", "").strip()

# ---- CONFIG (can be moved to Settings later if you want) ----
RISK_PCT = 0.01
RESIZE_STEP_PCT = 0.05

# DD tiers (sticky until new ATH)
# 0–12% => 1.0
# 12–18% => 0.9
# >18% => 0.7
DD_T1 = 0.12
DD_T2 = 0.18
DD_M1 = 0.9
DD_M2 = 0.7


# --------------------- Helpers ---------------------

def fetch_csv(url: str):
  if not url:
    raise RuntimeError("Missing CSV URL env var.")
  with urlopen(url) as resp:
    raw = resp.read().decode("utf-8", errors="replace")
  reader = csv.DictReader(raw.splitlines())
  return list(reader)

def norm_key(s: str) -> str:
  if s is None:
    return ""
  s = str(s).strip().lower()
  for ch in [" ", "-", "–", "—", ".", ":", "/"]:
    s = s.replace(ch, "_")
  while "__" in s:
    s = s.replace("__", "_")
  return s

def parse_de_number(s):
  """
  Robust parse:
  - accepts "100000", "100.000", "100.000,50", "100000,50"
  - if you ever add currency symbols, it will still work
  """
  if s is None:
    return None
  s = str(s).strip()
  if not s:
    return None
  s = re.sub(r"[^0-9,\.\-]", "", s)  # keep digits minus dot comma
  if not s or s in {"-", ",", "."}:
    return None
  s = s.replace(".", "")
  s = s.replace(",", ".")
  try:
    return float(s)
  except:
    return None

def truthy_include(row: dict):
  v = str(row.get("include", "TRUE")).strip().upper()
  return v != "FALSE"

def parse_date_ddmmyyyy(s: str):
  """
  Returns ISO date string YYYY-MM-DD.

  Accepts:
  - DD.MM.YYYY  (German)
  - MM/DD/YYYY  (US, can appear from Sheets)
  - YYYY-MM-DD  (ISO)
  """
  s = str(s or "").strip()

  # DD.MM.YYYY
  m = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})$", s)
  if m:
    dd, mm, yyyy = m.group(1), m.group(2), m.group(3)
    return f"{yyyy}-{mm}-{dd}"

  # MM/DD/YYYY
  m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
  if m:
    mm, dd, yyyy = m.group(1).zfill(2), m.group(2).zfill(2), m.group(3)
    return f"{yyyy}-{mm}-{dd}"

  # YYYY-MM-DD
  m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
  if m:
    yyyy, mm, dd = m.group(1), m.group(2), m.group(3)
    return f"{yyyy}-{mm}-{dd}"

  raise ValueError(f"Invalid date format (expected DD.MM.YYYY): {s}")

def ensure_data_dir():
  os.makedirs(DATA_DIR, exist_ok=True)

def write_json(name: str, obj):
  path = os.path.join(DATA_DIR, name)
  with open(path, "w", encoding="utf-8") as f:
    json.dump(obj, f, ensure_ascii=False, indent=2)

def read_starting_equity(settings_rows):
  if not settings_rows:
    raise RuntimeError("Settings CSV is empty.")

  headers = list(settings_rows[0].keys())
  headers_norm = [norm_key(h) for h in headers]
  header_map = {norm_key(h): h for h in headers}

  # Direct column candidates (incl. variants)
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

  # Key/value fallback
  key_col_candidates = ["key", "setting", "name", "parameter", "field", "variable"]
  val_col_candidates = ["value", "val", "amount", "number", "usd", "equity"]

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

  if (key_col is None or val_col is None) and len(headers) >= 2:
    key_col = key_col or headers[0]
    val_col = val_col or headers[1]

  target_keys = set(direct_candidates) | {
    "starting_capital",
    "starting_capital_$",
    "starting_account",
    "starting_account_$",
  }

  for r in settings_rows:
    if not truthy_include(r):
      continue
    k = norm_key(r.get(key_col, ""))
    if k in target_keys:
      val = parse_de_number(r.get(val_col))
      if val is not None:
        return val

  raise RuntimeError(
    'Could not find starting equity in Settings CSV. '
    'Expected a column like "starting_equity_$" or a key/value row.'
  )

def dd_tier_and_multiplier(drawdown_abs: float):
  """
  drawdown_abs is positive fraction (e.g. 0.13 for -13% drawdown).
  Returns (tier_label, multiplier).
  """
  if drawdown_abs > DD_T2:
    return (">18%", DD_M2)
  if drawdown_abs > DD_T1:
    return ("12–18%", DD_M1)
  return ("0–12%", 1.0)

def compute_streak_stats(is_win_list):
  """
  Input: list[bool] with True for win, False for loss.
  Returns:
    longest_win, avg_win, longest_loss, avg_loss
  """
  if not is_win_list:
    return (0, 0.0, 0, 0.0)

  win_runs = []
  loss_runs = []
  cur = 0
  cur_is_win = None

  for is_win in is_win_list:
    if cur_is_win is None:
      cur_is_win = is_win
      cur = 1
    elif is_win == cur_is_win:
      cur += 1
    else:
      # close run
      if cur_is_win:
        win_runs.append(cur)
      else:
        loss_runs.append(cur)
      cur_is_win = is_win
      cur = 1

  # close final
  if cur_is_win:
    win_runs.append(cur)
  else:
    loss_runs.append(cur)

  longest_win = max(win_runs) if win_runs else 0
  longest_loss = max(loss_runs) if loss_runs else 0
  avg_win = (sum(win_runs) / len(win_runs)) if win_runs else 0.0
  avg_loss = (sum(loss_runs) / len(loss_runs)) if loss_runs else 0.0
  return (longest_win, avg_win, longest_loss, avg_loss)


# --------------------- Core build ---------------------

def main():
  ensure_data_dir()

  settings_rows = fetch_csv(SETTINGS_URL)
  trades_rows = fetch_csv(TRADES_URL)
  adjs_rows = fetch_csv(ADJ_URL)

  starting_equity = read_starting_equity(settings_rows)

  # ---- Normalize & include-filter ----
  trades = []
  for r in trades_rows:
    if not truthy_include(r):
      continue
    date_iso = parse_date_ddmmyyyy(r.get("date", ""))
    instrument = str(r.get("instrument", "")).strip()
    result_r = parse_de_number(r.get("result_r"))
    if result_r is None:
      # skip invalid numeric rows
      continue
    trades.append({
      "date": date_iso,
      "instrument": instrument,
      "result_r": result_r,
      "trade_id": str(r.get("trade_id", "")).strip() or None,
      "notes": str(r.get("notes", "")).strip() or None,
    })

  adjustments = []
  for r in adjs_rows:
    if not truthy_include(r):
      continue
    date_iso = parse_date_ddmmyyyy(r.get("date", ""))
    adj_type = str(r.get("type", "")).strip().lower()  # in sheet column is "type"
    amt = parse_de_number(r.get("amount_$"))
    if amt is None:
      continue
    adjustments.append({
      "date": date_iso,
      "type": adj_type,
      "amount_$": amt,
      "event_id": str(r.get("event_id", "")).strip() or None,
      "notes": str(r.get("notes", "")).strip() or None,
    })

  # ---- Partition by date ----
  trades_by_date = defaultdict(list)
  for t in trades:
    trades_by_date[t["date"]].append(t)

  adjs_by_date = defaultdict(list)
  for a in adjustments:
    adjs_by_date[a["date"]].append(a)

  # ---- Date spine ----
  all_dates = sorted(set(list(trades_by_date.keys()) + list(adjs_by_date.keys())))

  # ---- Daily aggregates from inputs ----
  # Trades daily: count, sum R, win/loss counts, gross profit/loss R
  daily_trade_count = defaultdict(int)
  daily_r_sum = defaultdict(float)
  daily_win_count = defaultdict(int)
  daily_loss_count = defaultdict(int)

  for t in trades:
    d = t["date"]
    daily_trade_count[d] += 1
    daily_r_sum[d] += t["result_r"]
    if t["result_r"] > 0:
      daily_win_count[d] += 1
    elif t["result_r"] < 0:
      daily_loss_count[d] += 1

  # Adjustments daily sums by type
  daily_adj_sum_total = defaultdict(float)
  daily_adj_sum_by_type = defaultdict(lambda: defaultdict(float))
  for a in adjustments:
    d = a["date"]
    daily_adj_sum_total[d] += a["amount_$"]
    daily_adj_sum_by_type[d][a["type"]] += a["amount_$"]

  # ---- Build equity curve (Option A: broker truth only) ----
  cash_equity = starting_equity
  ath = starting_equity

  # Resizing base equity
  base_equity_for_sizing = starting_equity
  next_resize_threshold = base_equity_for_sizing * (1.0 - RESIZE_STEP_PCT)

  # Sticky tier multiplier state
  dd_multiplier = 1.0
  dd_tier = "0–12%"

  equity_daily = []
  daily_aggregates = []

  for d in all_dates:
    day_start = cash_equity

    # "Apply daily_pnl after trades" is conceptually true,
    # but in Option A trades do not affect cash equity,
    # so we just apply adjustments for that date.
    day_adj_total = daily_adj_sum_total.get(d, 0.0)
    cash_equity += day_adj_total
    day_end = cash_equity

    # ATH update + sticky reset
    new_ath = False
    if day_end > ath:
      ath = day_end
      new_ath = True
      dd_multiplier = 1.0
      dd_tier = "0–12%"

    # drawdown based on ATH
    drawdown_pct = (day_end / ath) - 1.0 if ath != 0 else 0.0
    drawdown_abs = abs(drawdown_pct) if drawdown_pct < 0 else 0.0

    # update tier if NOT new ATH (sticky until ATH)
    if not new_ath:
      tier_label, tier_mult = dd_tier_and_multiplier(drawdown_abs)
      dd_tier = tier_label
      dd_multiplier = tier_mult

    # equity-step resizing on EQUITY END
    # Resizing is independent of tier; it changes base_equity_for_sizing downward in 5% steps.
    if day_end <= next_resize_threshold:
      base_equity_for_sizing = day_end
      next_resize_threshold = base_equity_for_sizing * (1.0 - RESIZE_STEP_PCT)

    base_risk = base_equity_for_sizing * RISK_PCT
    effective_risk = base_risk * dd_multiplier

    equity_daily.append({
      "date": d,
      "equity_start_$": round(day_start, 2),
      "equity_end_$": round(day_end, 2),
      "cash_equity_$": round(day_end, 2),
      "ath_$": round(ath, 2),
      "drawdown_pct": round(drawdown_pct, 6),
      "dd_tier": dd_tier,
      "dd_multiplier": dd_multiplier,
      "base_equity_for_sizing_$": round(base_equity_for_sizing, 2),
      "next_resize_threshold_$": round(next_resize_threshold, 2),
      "base_risk_$": round(base_risk, 2),
      "effective_risk_$": round(effective_risk, 2),
    })

    daily_aggregates.append({
      "date": d,
      "trade_count": int(daily_trade_count.get(d, 0)),
      "daily_R": round(daily_r_sum.get(d, 0.0), 4),
      "wins": int(daily_win_count.get(d, 0)),
      "losses": int(daily_loss_count.get(d, 0)),
      "daily_adjustments_total_$": round(day_adj_total, 2),
      "daily_pnl_$": round(daily_adj_sum_by_type[d].get("daily_pnl", 0.0), 2),
      "withdrawal_$": round(daily_adj_sum_by_type[d].get("withdrawal", 0.0), 2),
      "deposit_$": round(daily_adj_sum_by_type[d].get("deposit", 0.0), 2),
      "fees_$": round(daily_adj_sum_by_type[d].get("fees", 0.0), 2),
      "correction_$": round(daily_adj_sum_by_type[d].get("correction", 0.0), 2),
    })

  # ---- Trades enriched (R-only analytics + streak context) ----
  # Sort trades by (date, instrument, trade_id fallback) to get deterministic order.
  # Note: streaks are computed on this sorted list; filtered streaks will be recomputed in-browser.
  trades_sorted = sorted(
    trades,
    key=lambda t: (
      t["date"],
      t["instrument"],
      t["trade_id"] or "",
      t["result_r"],
    )
  )

  trades_enriched = []
  cum_R = 0.0
  # streak counters (global)
  win_streak = 0
  loss_streak = 0
  is_win_list = []

  for t in trades_sorted:
    r = t["result_r"]
    cum_R += r

    is_win = r > 0
    is_loss = r < 0

    if is_win:
      win_streak += 1
      loss_streak = 0
    elif is_loss:
      loss_streak += 1
      win_streak = 0
    else:
      # breakeven breaks both streaks
      win_streak = 0
      loss_streak = 0

    if is_win or is_loss:
      is_win_list.append(is_win)

    trades_enriched.append({
      "date": t["date"],
      "instrument": t["instrument"],
      "result_r": round(r, 4),
      "is_win": bool(is_win),
      "is_loss": bool(is_loss),
      "is_be": bool(not is_win and not is_loss),
      "cum_R": round(cum_R, 4),
      "win_streak": int(win_streak),
      "loss_streak": int(loss_streak),
      "trade_id": t["trade_id"],
      "notes": t["notes"],
    })

  # ---- Full-period KPIs (Trades only, in R) ----
  trade_rs = [t["result_r"] for t in trades_sorted]
  wins = [r for r in trade_rs if r > 0]
  losses = [r for r in trade_rs if r < 0]
  be = [r for r in trade_rs if r == 0]

  trade_count = len(trade_rs)
  win_count = len(wins)
  loss_count = len(losses)

  winrate = (win_count / trade_count) if trade_count else None

  gross_profit = sum(wins)
  gross_loss_abs = abs(sum(losses))  # losses are negative
  profit_factor = (gross_profit / gross_loss_abs) if gross_loss_abs > 0 else (None if trade_count == 0 else float("inf"))

  avg_win_R = (sum(wins) / win_count) if win_count else None
  avg_loss_R = (sum(losses) / loss_count) if loss_count else None  # negative number

  expectancy_R = (sum(trade_rs) / trade_count) if trade_count else None

  longest_win_streak, avg_win_streak, longest_loss_streak, avg_loss_streak = compute_streak_stats(is_win_list)

  # Recovery factor (cash equity based): net profit / max drawdown (absolute)
  # net profit = last equity - starting equity
  net_profit_cash = (equity_daily[-1]["cash_equity_$"] - starting_equity) if equity_daily else 0.0
  max_dd_abs = 0.0
  for e in equity_daily:
    dd_abs = abs(e["drawdown_pct"]) if e["drawdown_pct"] < 0 else 0.0
    if dd_abs > max_dd_abs:
      max_dd_abs = dd_abs
  recovery_factor = (net_profit_cash / (starting_equity * max_dd_abs)) if (starting_equity > 0 and max_dd_abs > 0) else None

  kpis_full = {
    "trade_count": trade_count,
    "win_count": win_count,
    "loss_count": loss_count,
    "breakeven_count": len(be),

    "total_R": round(sum(trade_rs), 4) if trade_count else 0.0,
    "winrate": round(winrate, 6) if winrate is not None else None,
    "profit_factor": round(profit_factor, 6) if (profit_factor is not None and profit_factor != float("inf")) else profit_factor,
    "expectancy_R": round(expectancy_R, 6) if expectancy_R is not None else None,

    "avg_win_R": round(avg_win_R, 6) if avg_win_R is not None else None,
    "avg_loss_R": round(avg_loss_R, 6) if avg_loss_R is not None else None,

    "longest_winning_streak": int(longest_win_streak),
    "avg_winning_streak": round(avg_win_streak, 6),
    "longest_losing_streak": int(longest_loss_streak),
    "avg_losing_streak": round(avg_loss_streak, 6),

    "starting_equity_$": round(starting_equity, 2),
    "ending_equity_$": round(equity_daily[-1]["cash_equity_$"], 2) if equity_daily else round(starting_equity, 2),
    "net_profit_$": round(net_profit_cash, 2),
    "max_drawdown_pct": round(-max_dd_abs, 6) if max_dd_abs > 0 else 0.0,  # negative
    "recovery_factor": round(recovery_factor, 6) if recovery_factor is not None else None,
  }

  meta = {
    "last_updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "sources": {
      "trades_csv": TRADES_URL,
      "adjustments_csv": ADJ_URL,
      "settings_csv": SETTINGS_URL,
    },
    "config": {
      "risk_pct": RISK_PCT,
      "resize_step_pct": RESIZE_STEP_PCT,
      "dd_tiers": {
        "t1": DD_T1,
        "t2": DD_T2,
        "m1": DD_M1,
        "m2": DD_M2,
        "reset": "new_ath_only"
      },
      "cash_equity_driver": "daily_adjustments_only (broker truth)",
      "daily_pnl_includes_commissions": True,
      "fees_additional_to_daily_pnl": True,
    },
    "counts": {
      "trades_rows_included": len(trades),
      "adjustment_rows_included": len(adjustments),
      "unique_dates": len(all_dates),
    },
  }

  # Write outputs
  write_json("meta.json", meta)
  write_json("equity.json", equity_daily)
  write_json("daily_aggregates.json", daily_aggregates)
  write_json("trades_enriched.json", trades_enriched)
  write_json("kpis_full.json", kpis_full)

  print("OK: Wrote data/*.json")

if __name__ == "__main__":
  try:
    main()
  except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)
