#!/usr/bin/env node
/**
 * Generates: data/kpi_90d_weekly.json
 * Source:    data/kpi_snapshots_daily.json
 *
 * Rules:
 * - Only 90d values of: final_score, expectancy_R, profit_factor_R, recovery_factor_R, sharpe, tier
 * - Numeric KPI values: omit if not finite OR == 0
 * - delta_vs_prev: include only if both current and prev KPI values exist; delta may be 0
 * - tier_changed: include only if true (both tiers exist and differ)
 * - Point included only if it contains at least one numeric KPI
 * - Weekly normalization: target dates from min_date to max_date stepping +7 days;
 *   for each target choose last daily snapshot <= target (nearest previous).
 * - Dedup: do not emit same snapshot date twice.
 */

import fs from "node:fs";
import path from "node:path";

const ROOT = process.cwd();
const SRC = path.join(ROOT, "data", "kpi_snapshots_daily.json");
const OUT = path.join(ROOT, "data", "kpi_90d_weekly.json");

const DAY_MS = 24 * 60 * 60 * 1000;

const KPI_NUM_KEYS = [
  "final_score",
  "expectancy_R",
  "profit_factor_R",
  "recovery_factor_R",
  "sharpe",
  "winrate",
];

function isFiniteNumber(v) {
  return typeof v === "number" && Number.isFinite(v);
}

// IMPORTANT: numeric KPI value is INVALID if 0
function isValidKpiValue(v) {
  return isFiniteNumber(v) && v !== 0;
}

function round2(v) {
  return Math.round(v * 100) / 100;
}



function parseYMDToTs(ymd) {
  const ts = Date.parse(`${ymd}T00:00:00Z`);
  return Number.isFinite(ts) ? ts : NaN;
}

function tsToYMD(ts) {
  const d = new Date(ts);
  const y = d.getUTCFullYear();
  const m = String(d.getUTCMonth() + 1).padStart(2, "0");
  const da = String(d.getUTCDate()).padStart(2, "0");
  return `${y}-${m}-${da}`;
}

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, "utf8"));
}

function writeJson(file, obj) {
  fs.writeFileSync(file, JSON.stringify(obj, null, 2) + "\n", "utf8");
}

// binary search last index with rows[i].__ts <= targetTs
function lastIndexLE(rows, targetTs) {
  let lo = 0, hi = rows.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (rows[mid].__ts <= targetTs) { ans = mid; lo = mid + 1; }
    else hi = mid - 1;
  }
  return ans;
}

function get90(snap, key) {
  const obj = snap?.[key];
  const v = obj?.["90d"];
  return v;
}

function normalizeDailyRows(arr) {
  const rows = (Array.isArray(arr) ? arr : [])
    .filter(r => r && typeof r.date === "string")
    .map(r => ({ ...r, __ts: parseYMDToTs(r.date) }))
    .filter(r => Number.isFinite(r.__ts))
    .sort((a,b) => a.__ts - b.__ts);
  return rows;
}

function buildWeeklyPoints(rows) {
  if (!rows.length) return [];

  const minTs = rows[0].__ts;
  const maxTs = rows[rows.length - 1].__ts;

  const points = [];
  let prevPoint = null;
  let lastEmittedDate = null;

  for (let t = minTs; t <= maxTs; t += 7 * DAY_MS) {
    const idx = lastIndexLE(rows, t);
    if (idx < 0) continue;

    const snap = rows[idx];
    const snapDate = snap.date;

    // Dedup by snapshot date
    if (snapDate === lastEmittedDate) continue;

    // Extract tier (string)
    const tier90 = (() => {
      const v = get90(snap, "tier");
      return (typeof v === "string" && v.trim()) ? v.trim() : null;
    })();

    // Extract numeric KPIs, omit invalid (0 or non-finite)
    const vals = {};
    for (const k of KPI_NUM_KEYS) {
      const v = get90(snap, k);
      if (isValidKpiValue(v)) vals[k] = round2(v);
    }


    // Skip point if it has no numeric KPI at all
    if (Object.keys(vals).length === 0) continue;

    const point = { date: snapDate, ...vals };
    if (tier90) point.tier = tier90;

    // delta_vs_prev (deltas may be 0) only where both values exist
    if (prevPoint) {
      const delta = {};
      for (const k of KPI_NUM_KEYS) {
        if (Object.prototype.hasOwnProperty.call(point, k) && Object.prototype.hasOwnProperty.call(prevPoint, k)) {
          // Both exist => include delta (may be 0)
          delta[k] = round2(point[k] - prevPoint[k]);
        }
      }
      if (Object.keys(delta).length) point.delta_vs_prev = delta;

      // tier_changed: only include if true
      if (prevPoint.tier && point.tier && prevPoint.tier !== point.tier) {
        point.tier_changed = true;
      }
    }

    points.push(point);
    prevPoint = point;
    lastEmittedDate = snapDate;
  }

  return points;
}

function main() {
  if (!fs.existsSync(SRC)) {
    console.error(`Missing source file: ${SRC}`);
    process.exit(1);
  }

  const raw = readJson(SRC);
  const rows = normalizeDailyRows(raw);
  const points = buildWeeklyPoints(rows);

  const out = {
    schema_version: "kpi90_weekly_v1",
    generated_at: new Date().toISOString(),
    source: "kpi_snapshots_daily.json (90d only)",
    min_date: points.length ? points[0].date : null,
    max_date: points.length ? points[points.length - 1].date : null,
    points
  };

  writeJson(OUT, out);
  console.log(`Wrote ${OUT} (${points.length} points)`);
}

main();
