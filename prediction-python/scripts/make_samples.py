"""Deterministic sample CSV generator (committed fixtures in data_samples/).

Generates ~450 daily rows per instrument using a seeded geometric random walk
with drift, clipped to realistic 2024–2026 ranges:

* ``ir_gold_18k_daily_sample.csv`` — IRT/gram, ~2.5m rising toward ~9m
* ``usd_irt_daily_sample.csv``     — IRT/USD, ~50k rising toward ~115k
* ``xauusd_daily_sample.csv``      — USD/ozt, ~2000 rising toward ~3400

Run: ``python scripts/make_samples.py`` (from prediction-python/).
Regeneration is byte-identical (fixed seed, fixed end date).
"""
from __future__ import annotations

import csv
import os
from datetime import date, timedelta

import numpy as np

N_ROWS = 450
END_DATE = date(2026, 7, 19)
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data_samples")

SPECS = [
    # (filename, seed, start_value, end_value_target, daily_vol, lo_clip, hi_clip, decimals)
    ("ir_gold_18k_daily_sample.csv", 42, 2_500_000.0, 8_800_000.0, 0.012, 2_400_000.0, 9_500_000.0, 0),
    ("usd_irt_daily_sample.csv", 43, 50_000.0, 112_000.0, 0.010, 48_000.0, 118_000.0, 0),
    ("xauusd_daily_sample.csv", 44, 2_050.0, 3_300.0, 0.009, 1_900.0, 3_450.0, 2),
]


def generate(seed: int, start: float, end_target: float, vol: float,
             lo: float, hi: float, n: int = N_ROWS) -> np.ndarray:
    rng = np.random.default_rng(seed)
    drift = np.log(end_target / start) / n
    shocks = rng.normal(loc=drift, scale=vol, size=n - 1)
    log_path = np.concatenate(([np.log(start)], np.log(start) + np.cumsum(shocks)))
    return np.clip(np.exp(log_path), lo, hi)


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    start_date = END_DATE - timedelta(days=N_ROWS - 1)
    for filename, seed, start, end_target, vol, lo, hi, decimals in SPECS:
        values = generate(seed, start, end_target, vol, lo, hi)
        path = os.path.join(OUT_DIR, filename)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["date", "value"])
            for i, value in enumerate(values):
                day = start_date + timedelta(days=i)
                writer.writerow([day.isoformat(), f"{value:.{decimals}f}"])
        print(f"wrote {path} ({len(values)} rows, "
              f"{values[0]:.0f} -> {values[-1]:.0f})")


if __name__ == "__main__":
    main()
