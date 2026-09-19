# hyperliquid_trade

Hyperliquid 永续合约策略机器人（MVP）。按配套的两份 **v1.1** 研究文档做环境路由：

- 日线趋势明确（EMA20/EMA50 + ADX > 20~25）→ [趋势跟踪波段](docs/Hyperliquid_趋势跟踪波段策略_v1.1.md)
- 1h ADX < 20 且区间结构成立 → [均值回归日内短线](docs/Hyperliquid_均值回归日内短线策略_v1.1.md)
- 中间地带或信号冲突 → **观望，不新开仓**

> 研究/教育用途，**不构成投资建议**。永续合约高杠杆可能导致本金全部损失。默认 **dry-run**：只拉公开行情、算指标、打印制度和订单意图，**不会下单**。

规则与代码的对应表见 [docs/v1.1规则对照.md](docs/v1.1规则对照.md)。

## 功能

- 制度路由 + 趋势模块（4h Donchian 收盘突破，**不等 EMA 回撤**；BTC 急涨可追；空头非对称）
- 均值回归模块（BB + RSI + VWAP，必须反转确认；Maker；扩张/急涨停开并提前离场）
- 风险：单笔风险% / 止损距离反推仓位，杠杆是结果不是输入；逐仓思维；组合与单日亏损限制；禁止摊平
- 持仓：资金费率年化（或 24h 均）异常飙升且自己在付费 → 提前离场（默认趋势开、震荡关）
- 趋势金字塔：首仓浮盈 ≥ 1×ATR 且趋势仍确认时可加不超过 50%；止损提到保本以上，总风险不因加仓变大；BTC 急涨追突破首仓默认不加
- 官方 Hyperliquid REST `/info` 拉 K 线、中间价、资金费率；实盘路径走 [官方 Python SDK](https://github.com/hyperliquid-dex/hyperliquid-python-sdk)
- 主网 / 测试网可切换；凭证只来自环境变量

## 不需要密钥的第一次运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 不写 .env 也可以：默认 dry-run + 主网公开行情
hl-bot scan
# 或
python -m hl_bot scan --network mainnet
```

成功时会看到每个标的的日线 EMA/ADX、1h 区间结构、当前制度、是否有信号，以及**模拟订单意图**（方向、价格、止损、反推杠杆）。没有 `HL_PRIVATE_KEY` 也能跑。

核心单测：

```bash
pytest
```

## 目录

```
docs/                          v1.1 策略原文与规则对照
src/hl_bot/
  indicators.py                EMA / ATR / ADX / Donchian / BB / RSI / VWAP
  regime.py                    环境路由
  strategies/trend.py          趋势跟踪
  strategies/mean_reversion.py 均值回归
  risk.py                      仓位与组合限制
  exchange/client.py           Hyperliquid REST + SDK
  runner.py / cli.py           扫描与循环
config.toml                    可调参数（不要把密钥写进来）
.env.example                   环境变量模板
```

## 配置

复制环境变量模板（密钥不要提交）：

```bash
cp .env.example .env
```

| 变量 | 含义 |
|---|---|
| `HL_NETWORK` | `mainnet` 或 `testnet` |
| `HL_DRY_RUN` | 默认 `true` |
| `HL_PAPER_EQUITY` | 无账户时的模拟权益，默认 `10000` |
| `HL_SYMBOLS` | 默认 `BTC,ETH,SOL,HYPE` |
| `HL_PRIVATE_KEY` | 仅实盘需要 |
| `HL_ACCOUNT_ADDRESS` | 使用 API Wallet / Agent 时填**主账户**地址 |
| `HL_ENABLE_LIVE` | 实盘总闸，必须为 `1` 且命令行加 `--live` |

标的、ADX 阈值、风险比例等见 `config.toml`。

## 建议顺序：dry-run → 测试网 → 主网

1. **dry-run / 主网公开数据**（上面第一次运行）。确认能拉到 BTC/ETH/SOL/HYPE 的 K 线，制度与信号解释符合文档。
2. **测试网纸盘循环**（仍不下真单）：

   ```bash
   hl-bot run --network testnet --interval 60
   ```

   本地状态写在 `state/paper_state.json`（已 gitignore）。
3. **测试网实盘**（会向 testnet 发单，请用小资金）：

   - 在 https://app.hyperliquid-testnet.xyz 准备测试账户
   - 建议用 API Wallet，`.env` 中 `HL_PRIVATE_KEY` 填 API 私钥，`HL_ACCOUNT_ADDRESS` 填主地址
   - `HL_NETWORK=testnet`
   - `HL_DRY_RUN=false`
   - `HL_ENABLE_LIVE=1`
   - 执行：`hl-bot scan --network testnet --live`（先扫一次）再考虑 `hl-bot run --live`

   少一层开关都不会发单。
4. **主网**：同一套开关，先改小 `config.toml` 里的 `risk_pct`，并确认逐仓与止损。

## CLI

```text
hl-bot scan [--network mainnet|testnet] [--symbols BTC,ETH]
hl-bot run [--interval 60]
hl-bot version
```

`--live` 必须与 `HL_ENABLE_LIVE=1` 同时出现。

## 实现要点（v1.1）

- 趋势突破用 **4h 收盘价** 相对前 20 根 Donchian，不用影线，也**不会**等待回踩 EMA。
- BTC 突破 K 实体 > 1.5×ATR 时允许市价追击，止损仍是 2×ATR，风险百分比不放大。
- 趋势空只在日线空头环境；BTC 空单额外要求 ADX>25，风险预算约为多单的 70%。
- 震荡策略必须等到反转确认 K；ADX 抬头、带宽扩张或大实体 K 时停止新开并优先平掉已有仓。
- 杠杆由「风险金额 / 止损距离」算出，再按标的上限截断；不是先写死 8x。
- 已有趋势仓每次扫描检查资金费；付费方向年化 ≥ `funding_annual_warn`（默认 50%）则主动平仓。
- 金字塔加仓不是马丁：浮亏一律拒绝；加仓后止损到加权均价之上，`open_risk_usd` 不得上升。

## 本 PR 明确不做

完整历史回测 UI、Web 控制台、Telegram 实盘推送（仅日志桩）。禁止马丁/摊平（与金字塔加仓不是一回事）。
