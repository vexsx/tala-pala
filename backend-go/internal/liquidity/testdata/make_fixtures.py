"""Trim the money/dollar research outputs into the fixtures this package's tests read.

Run once, by hand, with the research directory as the only argument:

    python3 -I make_fixtures.py <research>/money

Standard library only. Every output is a plain selection of rows and columns from
a research CSV/JSON -- nothing is recomputed here, so a number a test compares
against is the number the research published.

Inputs (all under <research>/money):
  evidence/usd_irt_daily_toman.csv   TGJU price_dollar_rl daily, toman (g = Gregorian, j = TGJU's Jalali)
  evidence/_panel_raw.csv            the Jalali-month panel before analysis (M2, base money, method,
                                     Gregorian month end as CBI/tsdview printed it)
  evidence/table_monthly_money_usd.csv, table_annual_money_usd.csv
  evidence/backtest_metrics_L3_{avg,close}.csv, dm_money_models_vs_drift.csv
  evidence/band_table_m2g_close_monotone.csv, interval_calibration.csv
  evidence/scenario_paths_m2_proportional.csv, scenario_summary_table.csv, scenario_meta.json
  evidence/liquidity_dollar_summary.json
"""
import csv
import json
import sys
from pathlib import Path

SRC = Path(sys.argv[1]) / 'evidence'
OUT = Path(__file__).resolve().parent


def rows(name):
    with open(SRC / name, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def write(name, header, data):
    with open(OUT / name, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f, lineterminator='\n')
        w.writerow(header)
        w.writerows(data)


# Daily USD closes: the Gregorian day, TGJU's own Jalali date for it, the close.
write('usd_irt_daily_toman.csv', ['g', 'j', 'close'],
      [[r['g'], r['j'], r['close']] for r in rows('usd_irt_daily_toman.csv')])

# Money: every month that carries M2 or base money, with the method and the
# Gregorian month end the source printed.
money = []
for r in rows('_panel_raw.csv'):
    if not r['m2'] and not r['base']:
        continue
    money.append([r['jm'], r['greg_month_end'], r['m2'], r['m2_src'], r['base'], r['base_method'],
                  r['base_src'], r['m1'], r['quasi_money']])
write('money_monthly.csv',
      ['jalali_month', 'gregorian_month_end', 'm2', 'm2_src', 'base', 'base_method', 'base_src', 'm1',
       'quasi_money'], money)

# The research's own monthly and annual tables (the columns the aggregates reproduce).
mcols = ['jalali_month', 'usd_avg_toman', 'usd_close_toman', 'usd_ndays', 'm2', 'base_reported',
         'base_method', 'm2_mom_pct', 'm2_yoy_pct', 'm2_ytd_since_esfand_pct', 'base_mom_pct',
         'base_yoy_pct', 'base_yoy_method', 'money_multiplier_m2_over_base',
         'usd_over_m2_index_1397_04_eq_100']
write('expected_monthly.csv', mcols, [[r[c] for c in mcols] for r in rows('table_monthly_money_usd.csv')])
acols = ['jalali_year', 'm2_esfand', 'm2_yoy_pct', 'base_esfand_reported', 'base_yoy_pct',
         'base_yoy_method', 'money_multiplier', 'usd_year_avg_toman', 'usd_esfand_avg_toman',
         'usd_esfand_close_toman']
write('expected_annual.csv', acols, [[r[c] for c in acols] for r in rows('table_annual_money_usd.csv')
                                    if int(r['jalali_year']) >= 1390])

# Backtest: the three models the API publishes, full window, both targets.
bt = []
for target in ('avg', 'close'):
    for r in rows(f'backtest_metrics_L3_{target}.csv'):
        if r['window'] == 'full 1394-01..' and r['model'] in ('RW', 'DRIFT_all', 'M2G') \
                and r['h_months'] in ('3', '6', '12', '24'):
            bt.append([target, r['h_months'], r['model'], r['n_origins'], r['RMSE_log'],
                       r['DM_sq_p_better']])
write('expected_backtest.csv', ['target', 'h', 'model', 'n', 'rmse', 'dm_p_vs_rw'], bt)
dm = [[r['variant'].split('_')[1], r['h'], r['n'], r['DM_sq_p_model_better']]
      for r in rows('dm_money_models_vs_drift.csv')
      if r['variant'] in ('L3_avg', 'L3_close') and r['window'] == 'full' and r['model'] == 'M2G']
write('expected_dm_vs_trend.csv', ['target', 'h', 'n', 'dm_p_vs_trend'], dm)

# Band quantiles for production (M2G errors, month-end close) and their calibration.
bcols = ['h', 'n', 'q050', 'q950', 'q050_mono', 'q950_mono']
write('expected_band.csv', bcols, [[r[c] for c in bcols] for r in rows('band_table_m2g_close_monotone.csv')])
write('expected_calibration.csv', ['target', 'h', 'nominal', 'n', 'coverage'],
      [[r['target'], r['h'], r['nominal'], r['n'], r['realised_coverage']]
       for r in rows('interval_calibration.csv') if r['model'] == 'M2G'])

# Scenario paths (rounded to the toman by the research) and the band edges at 3/6/12/24.
sp = rows('scenario_paths_m2_proportional.csv')
keys = [k for k in sp[0] if k.endswith('_path')]
write('expected_scenario_paths.csv', ['h', 'period', 'last_12m', 'three_year', 'slowest_three_year'],
      [[r['h_months'], r['approx_jalali_month']] + [r[k] for k in keys] for r in sp])
write('expected_scenario_band.csv', ['scenario', 'h', 'path', 'q05', 'q95'],
      [[r['scenario'], r['h'], r['path'], r['q05'], r['q95']] for r in rows('scenario_summary_table.csv')])

meta = json.load(open(SRC / 'scenario_meta.json', encoding='utf-8'))
ld = json.load(open(SRC / 'liquidity_dollar_summary.json', encoding='utf-8'))
json.dump({
    'anchor_close_toman': meta['anchor_close_toman'],
    'anchor_date': meta['anchor_date'],
    'anchor_jalali': meta['anchor_jalali'],
    'scenario_annual_m2_growth_pct': list(meta['scenario_annual_m2_growth'].values()),
    'liquidity_dollar': {k: ld[k] for k in ('n_bases', 'm2_1404_12', 'usd_avg_1404_12', 'ld_min',
                                            'ld_median', 'ld_max', 'ld_min_base', 'ld_max_base')},
}, open(OUT / 'expected_meta.json', 'w', encoding='utf-8'), indent=1)
