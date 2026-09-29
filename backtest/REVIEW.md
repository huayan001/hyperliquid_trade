# hl_backtest_v4 review notes (2026-09-29 CST)
Files: bt_cc_orig.py = Claude Code's bt.py (sys.path fixed only) -> bt_output_cc_asis.txt / report_tables_cc_asis.md / equity_cc_asis.png / results_cc_asis.pkl
       bt.py = fixed version -> bt_output.txt / report_tables.md / equity.png / results.pkl
       live_universe_ETH_SOL_HYPE.txt = L0/V4 rerun on the live symbol set only (BTC excluded)
Fixes in bt.py:
 F1 LIVE preset = effective live config.toml: trend max_positions=2, portfolio_risk_max=0.04; MR max_positions=1,
    max_trades_per_symbol_day=3, daily_loss_halt=0.02 (cc used dataclass defaults 3/6%/2/4/3%). cc's caps kept as "cc口径" rows.
 F2 Lookahead: equity used for sizing / MR daily-halt / leverage cap at next-bar-OPEN fill was marked at that bar's CLOSE. Now marked at OPEN.
    (inherited from the original hl_backtest/bt.py; alone moves V1 -38.1% -> -48.2%, V3 -15.3% -> -18.0%)
 F3 Trend daily 5% / weekly 10% de-lever x0.5 modelled (never triggered in this sample).
 F4 Cooldown per live runner._record_stop: any stop fill (win or loss, trend or MR) + MR time stop; blocks MR entries too.
 F5 Exit labels: stop_initial / stop_trail- (moved but still below entry) / stop_be/trail+ (at/above entry).
Mean reversion: 0 trades in every MR version - genuine: 1h ADX<20 + range regime almost never coincides with RSI<30/>70
 (3 candidate signals in 6 months, none filled). Live log since 2026-09-19 also shows 0 mean_reversion regime scans (2544 trend_long / 501 watch).

Update 2026-09-29 ~12:20 CST (long-data round):
 F6 OOS/IS trades now attributed by ENTRY time (was close time). HL data L0 OOS PF 1.67 -> 0.74; V0 1.29 -> 0.59.
 F7 sys.path now Path(__file__).resolve().parent.parent/"src" (repo layout backtest/ + src/); run elsewhere with PYTHONPATH=<repo>/src.
 New: BT_DATA=long / BT_SYMS / BT_OOS_FRAC switches, per-period table, fetch_long.py (Binance data.binance.vision + OKX HYPE; no HL API),
      make_report_long.py -> report_long.md. See README.md.
