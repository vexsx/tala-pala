"""Read-only activation dry run: would the CURRENT rules keep the CURRENT models?

Answers, without training or activating anything:

  * how many walk-forward folds each symbol x horizon actually gets;
  * whether that clears HOLDOUT_MIN_TOTAL, i.e. whether an embargoed holdout
    exists at all (below it, Addendum 16 keeps naive active on purpose);
  * what is active in `model_versions` right now, and whether the artifact
    file backing it still loads.

Fold geometry is arithmetic on the series length (see walk_forward/_fold_step),
so this costs one query per series and fits no models. Run it BEFORE a
production retrain to see what a retrain would be allowed to change.

    docker compose exec -T prediction-service python -m scripts.activation_dryrun
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import create_db_engine, model_versions  # noqa: E402
from app.models.training import (  # noqa: E402
    FORECAST_SYMBOLS,
    HOLDOUT_FRACTION,
    HOLDOUT_MIN,
    HOLDOUT_MIN_TOTAL,
    HORIZON_SPECS,
    MAX_FOLDS,
    MIN_TRAIN_POINTS,
    _fold_step,
    horizon_enabled,
    load_series,
)


def fold_geometry(series, horizon_steps: int) -> dict:
    """Fold count / embargo / split sizes, mirroring walk_forward + split_folds."""
    n = len(series)
    last_now = n - 1 - horizon_steps
    first_now = MIN_TRAIN_POINTS - 1
    if last_now < first_now:
        return {"n_folds": 0, "step": 0, "selection": 0, "holdout": 0, "embargo": 0}
    step = _fold_step(series, horizon_steps, MAX_FOLDS)
    n_folds = len(range(first_now, last_now + 1, step))
    if n_folds < HOLDOUT_MIN_TOTAL:
        return {"n_folds": n_folds, "step": step, "selection": n_folds,
                "holdout": 0, "embargo": 0}
    n_hold = max(HOLDOUT_MIN, int(n_folds * HOLDOUT_FRACTION))
    embargo = max(0, -(-horizon_steps // step) - 1)  # ceil(h/step) - 1
    embargo = min(embargo, max(0, n_folds - n_hold - HOLDOUT_MIN))
    return {"n_folds": n_folds, "step": step,
            "selection": n_folds - n_hold - embargo,
            "holdout": n_hold, "embargo": embargo}


def main() -> int:
    settings = get_settings()
    engine = create_db_engine(settings.database_url)

    with engine.connect() as conn:
        active = {
            (r[0], r[1]): (r[2], r[3])
            for r in conn.execute(
                select(model_versions.c.symbol, model_versions.c.horizon,
                       model_versions.c.model_name, model_versions.c.artifact_path)
                .where(model_versions.c.is_active)
            )
        }

    print(f"HOLDOUT_MIN_TOTAL={HOLDOUT_MIN_TOTAL}  MAX_FOLDS={MAX_FOLDS}\n")
    header = f"{'symbol':<13}{'hz':<5}{'folds':>6}{'sel':>5}{'emb':>5}{'hold':>5}  {'holdout?':<9}{'active now':<16}artifact"
    print(header)
    print("-" * len(header))

    at_risk: list[str] = []
    series_cache: dict[str, object] = {}
    for symbol in FORECAST_SYMBOLS:
        for horizon, (freq, steps) in HORIZON_SPECS.items():
            key = f"{symbol}:{freq}"
            if key not in series_cache:
                series_cache[key] = load_series(engine, symbol, freq)
            series = series_cache[key]
            enabled, reason = horizon_enabled(freq, series)
            if not enabled:
                print(f"{symbol:<13}{horizon:<5}{'-':>6}{'-':>5}{'-':>5}{'-':>5}  "
                      f"{'disabled':<9}{'-':<16}{reason}")
                continue
            g = fold_geometry(series, steps)
            has_holdout = g["holdout"] > 0
            model, path = active.get((symbol, horizon), ("(none)", None))
            artifact = "n/a"
            if path:
                artifact = "present" if os.path.exists(path) else "MISSING"
            print(f"{symbol:<13}{horizon:<5}{g['n_folds']:>6}{g['selection']:>5}"
                  f"{g['embargo']:>5}{g['holdout']:>5}  {'yes' if has_holdout else 'NO':<9}"
                  f"{model:<16}{artifact}")
            if not has_holdout and model not in ("naive", "(none)"):
                at_risk.append(f"{symbol}/{horizon} ({model})")

    print()
    if at_risk:
        print("WOULD BE DEMOTED TO NAIVE on the next retrain (no embargoed holdout):")
        for item in at_risk:
            print(f"  - {item}")
    else:
        print("No active non-naive model loses its holdout under the current rules.")
    print("\nNothing was trained, activated, or deactivated by this script.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
