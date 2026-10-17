# 菲阿里四价（PURE，仅 BTC / ETH / SOL）

研究回测在这个目录。实盘/纸面入口是另一个命令，**默认关闭**，说明见 [docs/phil_ali.md](../../docs/phil_ali.md)。

这里没有黄金。不拉 XAU，不拉 PAXG，也不读取这类文件。带开收偏向和 1.5R 止盈的旧版规则没有放进来，避免和纯规则混用。

## 规则

- 用**昨天**的四价：开、收、高、低。进场只看昨高 YH、昨低 YL。
- 当天第一根 `high > YH` 的 1h 做多；第一根 `low < YL` 的 1h 做空。
- 开盘已经站在突破价外面，就按开盘成交，否则按 YH 或 YL。
- 同一天每个标的只做一笔。一根 K 线同时破高低，这一天跳过。
- 不平仓条件里没有止盈，也没有盘中止损。UTC 日最后一根 1h 收盘平掉。
- 风险是当时权益的 1%。多单的 R 是进场价到昨低，空单是昨高到进场价。
- 默认往返费用 0.05%，报告里另给 0.08%。日末平仓，资金费按 0。

## 运行

K 线不入库。`data/` 已忽略。

```bash
cd backtest/phil_ali
python fetch.py          # 优先用 ../data_long，否则下 Binance USDT-M 的 BTC/ETH/SOL
python run_backtest.py   # 写入 out/REPORT.md、out/summary.json、成交明细
```

`fetch.py` 的标的写死为 BTC、ETH、SOL。已附一份不含黄金的研究快照：`REPORT.md` 和 `out/summary.json`（约 2023-07 至 2026-09）。本地重跑会覆盖 `out/` 里的结果。

规则引擎的单测不需要历史 K 线：

```bash
pytest tests/test_phil_ali.py
```

## 不要做的事

- 不要把 `data/`、成交明细 json、权益图提交上来。
- 不要为了跑这份回测去改日本 VPS 上的趋势进程。部署是另一件事，见 [docs/phil_ali.md](../../docs/phil_ali.md)。
