# backtest/ — hl_bot 回测（趋势 + 均值回归）

复用仓库 `src/hl_bot` 的指标、环境路由（`hl_bot.regime`）和参数口径，对现行策略（V0/L0）、Claude Code 提出的改版（V1–V4）以及均值回归（MR）做组合回测。

## 文件

| 文件 | 说明 |
|---|---|
| `bt.py` | 回测引擎 + 全部版本定义（V0–V3、L0、L0+MR、V4、V4+MR、cc口径 两行），输出 `results*.pkl` 并打印汇总 |
| `report.py` | 读 `results*.pkl`，生成 `report_tables*.md`（组合、分币种、分策略、出场原因、通过标准、分阶段）和 `equity*.png` |
| `fetch.py` | 从 **Hyperliquid info API** 拉近 ~250 天 1h/4h/1d K 线 + fundingHistory 到 `data/`（已存在的文件跳过；每次请求间隔 2s） |
| `fetch_long.py` | 长周期数据到 `data_long/`：BTC/ETH/SOL 用 Binance USDT-M 公开数据集（data.binance.vision），HYPE 用 OKX HYPE-USDT-SWAP；**不访问 Hyperliquid**；原始文件缓存在 `data_long/raw/`，间隔 ≥1.2s |
| `make_report_long.py` | 把长周期 4 币 / 实盘 3 币两次结果汇总成 `report_long_tables.md`（`report_long.md` 的明细表部分） |
| `report_tables.md`, `equity.png` | HL 数据（2026-03-20 → 2026-09-29）结果 |
| `report_long.md`, `equity_long.png` | 长周期（2023-10-01 → 2026-09-28）结果与结论 |
| `REVIEW.md` | 对 Claude Code 版本的审查记录与修复清单 |

数据文件（`data/`、`data_long/`）和 `*.pkl` 不入库，需要时用 fetch 脚本重新生成。

## 运行

仓库布局：`backtest/` 与 `src/` 同级，`bt.py` 会自动把 `../src` 加入 `sys.path`。放在其他位置运行时设置 `PYTHONPATH=<repo>/src`（或做软链接）。依赖：仓库本身的依赖（`python-dotenv` 等）+ `matplotlib`；`fetch_long.py` 需要 `requests`。建议设 `PYTHONDONTWRITEBYTECODE=1`，避免在 `src/` 里生成 `__pycache__`。

```bash
cd backtest
# 1) 默认：Hyperliquid 近 6 个月数据（data/）
python fetch.py            # 可选；会访问 HL info API，和实盘共用 IP 限速，已有文件会跳过
python bt.py && python report.py            # → results.pkl / report_tables.md / equity.png

# 2) 长周期：Binance/OKX 2023-07 起（data_long/）
python fetch_long.py                         # 首次约 10 分钟；之后走 raw/ 缓存
BT_DATA=long python bt.py && BT_DATA=long python report.py
BT_DATA=long BT_SYMS=ETH,SOL,HYPE python bt.py && BT_DATA=long BT_SYMS=ETH,SOL,HYPE python report.py
python make_report_long.py                   # → report_long_tables.md
```

环境变量：
- `BT_DATA=long`：使用 `data_long/`，回测起点 2023-10-01，样本外 35%（默认 HL 数据：起点 2026-03-20，样本外 40%）
- `BT_SYMS=ETH,SOL,HYPE`：只回测部分标的（输出文件带 `_ETH-SOL-HYPE` 后缀）
- `BT_OOS_FRAC=0.35`：覆盖样本外比例

## 相对 Claude Code 版本的修复

1. **前视偏差**：下一根开盘成交时，用于仓位计算 / MR 日亏停机 / 杠杆上限的权益原来按该根 K 线**收盘价**估值（当时还不知道），现改为按开盘价估值。此问题继承自最早的回测版本；单这一项就让 V1 从 −38.1% 变为 −48.2%（HL 数据）。
2. **实盘口径**：实盘进程加载的是 `config.toml`（`config.local.toml` 没有任何代码读取；标的由 `.env` 里的 `HL_SYMBOLS` 决定），所以 L0/V4 的限制改为趋势最多 2 仓、组合风险 ≤4%，MR 最多 1 仓、每币每日 3 次、日亏 2% 停。Claude 用的 3 仓/6%/2/4/3% 只是 dataclass 默认值（保留为 "cc口径" 两行对照）。另外建模了日 5% / 周 10% 降杠杆。
3. **冷却口径与实盘一致**（`runner._record_stop`）：任何止损成交（不论盈亏、不论趋势/MR）和 MR 时间止损都触发 2 根 4h 冷却，且冷却同样拦截 MR 开仓。原版只在趋势仓亏损止损时冷却。
4. **出场原因细分**：`stop_initial`（初始止损）、`stop_trail-`（止损已上移但仍在均价下方）、`stop_be/trail+`（保本/吊灯/锁利，止损在均价上方），另有 `trend_exit`、`time`、`lock`、`mr_*`、`end`。
5. **导入路径**：Claude 版用的就是 `Path(__file__).resolve().parent.parent / "src"`（适合仓库布局），但在 `/workspace/hl_backtest*` 下运行会指向不存在的 `/workspace/src`，上一轮曾临时改成绝对路径；现在恢复为相对路径，仓库外运行用 `PYTHONPATH=<repo>/src`。
6. **OOS 统计口径**：样本内/外的交易改为按**开仓时间**归属（原来按平仓时间，跨越分割点的长持仓会把样本内赚的钱算进 OOS PF；例如 HL 数据 L0 的 OOS PF 由 1.67 变为 0.74）。收益/回撤仍按资金曲线分段。
7. 新增：`BT_DATA`/`BT_SYMS` 开关、分阶段表现表、通过标准表、长周期数据脚本。

## 注意事项

- 均值回归在实盘逻辑下几乎不触发（HL 6 个月 0 笔，长周期 3 年 1–2 笔），MR 相关版本和非 MR 版本结果基本相同。
- 未建模：资金费逆向减仓、maker 挂单不成交（MR 限价按穿价即成交，开盘越过限价时按开盘价成交但仍计 maker 费）、突破追单市价、交易所强平（只用止损距离 ≤5% 近似）。
- V4 的 1.5R/3R 分批目标以加仓后的新均价计算。
- 长周期数据不是 Hyperliquid：价格与 HL 重叠段差异很小（中位 0.01–0.04%），但资金费率明显低于 HL，资金费成本被低估；HYPE 2025-02→05 资金费用 BTC 代理；HYPE 约 2025-04 才开始交易。
- 所有参数事先固定，没有做寻优；但只有一条历史路径，分阶段结果显示收益高度依赖 2023Q4–2024Q1 的牛市。
