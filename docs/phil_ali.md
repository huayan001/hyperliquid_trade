# 菲阿里四价 — 接入说明

纯规则版本，只做加密货币永续。黄金、XAU、PAXG 不在范围内，代码里遇到这些名字会直接拒绝。

趋势机器人的 `hl-bot scan` / `hl-bot run` **不会**调用这套逻辑。保活脚本也不会拉起它。总闸默认关闭，所以同步到日本 VPS 之后，现有趋势循环保持原样。

## 规则

| 项目 | 纯规则 |
|---|---|
| 信号 | 昨天最高 YH、昨天最低 YL |
| 做多 | 当天第一根已收盘 1h 的 high > YH |
| 做空 | 当天第一根已收盘 1h 的 low < YL |
| 成交 | 跳空越过价位按开盘，否则按 YH 或 YL |
| 过滤 | 不看开盘和昨收谁高谁低 |
| 止盈 / 盘中止损 | 没有 |
| 出场 | UTC 23:00 那根 1h 收盘（回测里用当天数据的最后一根，完整日就是这根） |
| 频率 | 每个标的每个 UTC 日最多 1 笔 |
| 同根双破 | 整天不做 |
| 仓位 | 权益 × 1% / R。多单 R = 进场价 − YL，空单 R = YH − 进场价 |
| 启动 | 第一次看到某个标的时快进到最新 K 线，不追已经走过的突破 |

回测研究快照（BTC/ETH/SOL，不含黄金）在 [backtest/phil_ali/REPORT.md](../backtest/phil_ali/REPORT.md)。

## 怎么开

只认环境变量。`config.toml` 里没有开关，改配置文件不会启用。

| 变量 | 默认 | 作用 |
|---|---|---|
| `HL_PHIL_ALI_ENABLED` | `0` | 总闸。不是 `1` 时，后面的模式一律忽略 |
| `HL_PHIL_ALI_MODE` | `off` | `shadow`、`paper` 或 `live` |
| `HL_PHIL_ALI_ARM_LIVE` | `0` | 实盘发单的额外闸，默认不武装 |
| `HL_PHIL_ALI_SYMBOLS` | `BTC,ETH,SOL` | 不跟随 `HL_SYMBOLS`。可以写成 `ETH,SOL,HYPE`，不能写黄金 |
| `HL_PHIL_ALI_RISK_FRAC` | `0.01` | 单笔风险，上限 5% |
| `HL_PHIL_ALI_PAPER_EQUITY` | `10000` | 纸面/影子起始权益，不用趋势机器人的 `HL_PAPER_EQUITY` |
| `HL_PHIL_ALI_POLL_SECONDS` | `300` | `--loop` 的间隔，至少 30 秒 |
| `HL_PHIL_ALI_LEVERAGE` | `10` | 只在真正发单前设置杠杆 |
| `HL_PHIL_ALI_STATE_DIR` | `state/phil_ali` | 自己的状态目录，不写 `state/paper_state.json` |

命令：

```bash
# 默认：打印未启用，退出码 0，不访问交易所
hl-bot phil-ali
python -m hl_bot.phil_ali
```

影子或纸面（仍不下单）：

```bash
HL_PHIL_ALI_ENABLED=1 HL_PHIL_ALI_MODE=shadow hl-bot phil-ali
HL_PHIL_ALI_ENABLED=1 HL_PHIL_ALI_MODE=paper hl-bot phil-ali --loop
```

`shadow` 和 `paper` 都在本地记账。`shadow` 再追加 `state/phil_ali/shadow.jsonl`。K 线缓存在 `state/phil_ali/candles/`，和趋势循环用的 `state/candles/` 分开。

## 实盘发单

这一版**没有打开实盘**。下面五件事必须同时成立，`hl-bot phil-ali` 才会调用下单接口：

1. `HL_PHIL_ALI_ENABLED=1`
2. `HL_PHIL_ALI_MODE=live`
3. `HL_PHIL_ALI_ARM_LIVE=1`
4. `HL_ENABLE_LIVE=1` 且 `HL_DRY_RUN=false`
5. 命令行带 `--live`

少一层就只推进「今天已经看过」的游标，不建仓、不发单。纯规则没有盘中止损，所以菲阿里自己的订单不附带保护止损单。

不要和正在跑的趋势进程共用同一批仓位。趋势循环会给自己管理的标的补保护止损、对齐杠杆。若以后真要实盘试菲阿里，用单独账户，或避开趋势进程正在交易的标的。未启用时两边互不影响。

## VPS

日本 VPS 上的部署、systemd、`scripts/ensure_live.sh` 都不归这个改动管。不要把 `phil-ali` 写进保活脚本。代码同步上去之后，不设上面的环境变量就不会跑。纸面或影子要另起一个进程，确认日志里没有下单，再考虑要不要武装实盘。
