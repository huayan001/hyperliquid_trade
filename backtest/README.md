# 回测

比较 BTC、ETH、SOL、HYPE 上四套做法（2026-03-20 至 2026-09-29）：

- **V0**：仓库现行趋势策略
- **V1 / V2**：1h 短线（V1 分批止盈；V2 在此基础上加金字塔）
- **V3**：V0 入场不变，出场改为吊灯止损 + 1R 保本

## 怎么跑

在 `backtest/` 目录下依次执行：

```bash
python fetch.py   # 从 Hyperliquid 下载 K 线到 backtest/data/
python bt.py      # 回测，写出 results.pkl
python report.py  # 根据 results.pkl 生成 equity.png 与 report_tables.md
```

`bt.py` 通过脚本所在目录定位仓库 `src/`，复用 `hl_bot` 的指标与环境路由。拉数据需要 `requests`，出图需要 `matplotlib`。

## 不入库

`backtest/data/` 与 `backtest/results.pkl` 已写入仓库根目录的 `.gitignore`，下载的行情和回测结果不会提交。
