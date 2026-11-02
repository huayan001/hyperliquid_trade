# 短线研究（A/B/C 与 B2）

独立于趋势引擎的短线试验，只复用 `hl_bot.indicators`。参数在看结果之前写进 `PLAN.md` / `B2/B2_PLAN.md`。结论：**短线区间方向到此停止**，不要接入实盘。

## A / B / C（预注册，全部失败）

`short.py` 一次跑完三个事先固定的候选，没有网格搜索：

| 候选 | 规则 |
|---|---|
| A | 1h 布林带回归，日线/4h ADX 过滤非趋势 |
| B | 4h Donchian 区间边缘反向（fade） |
| C | 日线趋势内的 RSI(2) 回调 |

长数据约 3 年、4 币组合：A **−42.9%**、B **−23.5%**、C **−52%**。样本内外都亏，2024Q2–Q4 震荡段也不赚钱。明细见 `report_short.md`。

## B2（Simons 式扫描 + VAL 选择 + 一次性 TEST，未通过）

在 B 的方向上做统计扫描（`B2/scan.py`：4164 个假设，BH FDR），再在验证段选出一个变体，TEST 只跑一次。

- TRAIN PF **2.14**，VAL PF **2.34**，TEST PF **1.18**（已消耗，见 `B2/test_done.flag`）。6 项预注册标准过 4 项（PF 1.18 < 1.2，仅 2/4 币种盈利）。
- HL 近 6 个月：PF **0.69**，收益 **−3.66%**。
- FDR 后剩下的 **9 个**存活假设全是**顺突破方向**，没有一个是边缘反向（edge-fade）。原 B 假设在 TRAIN 里就不成立；筛出来的突破优势到 TEST / HL 也消失。

`B2/B2_PLAN.md` 的 sha256 记在 `B2/B2_PLAN.sha256`（`cd backtest/short/B2 && sha256sum -c B2_PLAN.sha256`）。偏离说明见 `B2/B2_DEVIATIONS.md`。

## 怎么重跑

数据不在库里。先在 `backtest/` 生成 `data_long/`（`python fetch_long.py`）和 `data/`（`python fetch.py`）。脚本用相对 `backtest/short/` 的路径找 `../data_long`、`../data`、`../../src`。

```bash
cd backtest/short
python short.py            # → results_short.pkl
python short.py report     # → report_short_tables.md、equity_short.png

cd B2
python scan.py             # 扫描，写 scan_results.csv / thresholds.json
python b2.py val           # 只在 VAL 上选变体
python b2.py test          # TEST 已消耗：存在 test_done.flag 时不要再跑
python b2_report.py
```

`*.pkl` 与 `data/`、`data_long/`（含 `backtest/short/` 下）由仓库 `.gitignore` 忽略。

## TEST 样本外已经用过

`python b2.py test` 只能跑一次。任何新的 B 变体都需要**新的一段 holdout 数据**，不能拿这次 TEST 的结果再调参。
