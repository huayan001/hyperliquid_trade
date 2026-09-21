from hl_bot.config import BotConfig
from hl_bot.exchange.client import HyperliquidClient
from hl_bot.exchange.equity import (
    combine_live_equity,
    free_spot_usdc,
    perps_account_value,
)


def _perps(account_value: float = 0.0, cross: float | None = None, withdrawable: float = 0.0) -> dict:
    return {
        "marginSummary": {"accountValue": str(account_value)},
        "crossMarginSummary": {"accountValue": str(cross if cross is not None else account_value)},
        "withdrawable": str(withdrawable),
        "assetPositions": [],
    }


def _spot(total: float = 478.749044, hold: float = 0.0, *, coin: str = "USDC", token: int = 0) -> dict:
    return {
        "balances": [
            {"coin": coin, "token": token, "total": str(total), "hold": str(hold), "entryNtl": "0.0"}
        ]
    }


def test_flat_unified_matches_app_available() -> None:
    """实盘对照：perps=0 + 现货 USDC≈478 → 权益≈478（App「可用」）。"""
    breakdown = combine_live_equity(_perps(0.0), _spot(478.749044), "unifiedAccount")
    assert abs(breakdown.equity - 478.749044) < 1e-9
    assert breakdown.perps_value == 0.0
    assert abs(breakdown.free_spot_usdc - 478.749044) < 1e-9
    assert breakdown.formula == "spot_usdc_unified"
    assert breakdown.included == ("spot_usdc",)


def test_standard_sums_perps_and_free_spot_no_double_count() -> None:
    breakdown = combine_live_equity(_perps(200.0), _spot(478.749, hold=10.0), "disabled")
    assert abs(breakdown.free_spot_usdc - 468.749) < 1e-9
    assert abs(breakdown.equity - (200.0 + 468.749)) < 1e-9
    assert breakdown.formula == "perps_plus_spot"
    assert breakdown.included == ("perps", "spot_usdc")


def test_unified_does_not_double_count_when_both_present() -> None:
    """unified 下 perps accountValue 与现货是同一桶，只计现货。"""
    breakdown = combine_live_equity(_perps(478.749), _spot(478.749), "unifiedAccount")
    assert abs(breakdown.equity - 478.749) < 1e-9
    assert breakdown.formula == "spot_usdc_unified"
    assert "perps" not in breakdown.included


def test_unknown_mode_sums_separate_buckets() -> None:
    breakdown = combine_live_equity(_perps(100.0), _spot(50.0), None)
    assert abs(breakdown.equity - 150.0) < 1e-9
    assert breakdown.formula == "perps_plus_spot"


def test_hold_is_subtracted_and_non_usdc_ignored() -> None:
    spot = {
        "balances": [
            {"coin": "HYPE", "token": 150, "total": "10", "hold": "0"},
            {"coin": "USDC", "token": 0, "total": "478.749", "hold": "20.0"},
        ]
    }
    assert abs(free_spot_usdc(spot) - 458.749) < 1e-9


def test_perps_falls_back_to_withdrawable_when_account_value_zero() -> None:
    raw = _perps(account_value=0.0, cross=0.0, withdrawable=12.5)
    assert abs(perps_account_value(raw) - 12.5) < 1e-9
    assert perps_account_value(_perps(account_value=80.0, withdrawable=10.0)) == 80.0


def test_account_equity_live_flat_spot() -> None:
    cfg = BotConfig()
    cfg.dry_run = False
    cfg.account_address = "0x17d6e255ceCFff2BA7A727b59a2Ba8bCCBD127cd"
    client = HyperliquidClient(cfg)
    client.connect_sdk = lambda **kwargs: None

    def fake_info(payload):
        kind = payload["type"]
        if kind == "clearinghouseState":
            return _perps(0.0)
        if kind == "spotClearinghouseState":
            return _spot(478.749044)
        if kind == "userAbstraction":
            return "unifiedAccount"
        raise AssertionError(kind)

    client._post_info = fake_info
    assert abs(client.account_equity(10_000.0) - 478.749044) < 1e-9


def test_account_equity_unified_both_present_no_double_count() -> None:
    cfg = BotConfig()
    cfg.dry_run = False
    cfg.account_address = "0xabc"
    client = HyperliquidClient(cfg)
    client.connect_sdk = lambda **kwargs: None

    def fake_info(payload):
        kind = payload["type"]
        if kind == "clearinghouseState":
            return _perps(478.749)
        if kind == "spotClearinghouseState":
            return _spot(478.749)
        if kind == "userAbstraction":
            return "unifiedAccount"
        raise AssertionError(kind)

    client._post_info = fake_info
    assert abs(client.account_equity(1.0) - 478.749) < 1e-9


def test_account_equity_standard_sum() -> None:
    cfg = BotConfig()
    cfg.dry_run = False
    cfg.account_address = "0xabc"
    client = HyperliquidClient(cfg)
    client.connect_sdk = lambda **kwargs: None

    def fake_info(payload):
        kind = payload["type"]
        if kind == "clearinghouseState":
            return _perps(120.0)
        if kind == "spotClearinghouseState":
            return _spot(80.0)
        if kind == "userAbstraction":
            return "disabled"
        raise AssertionError(kind)

    client._post_info = fake_info
    assert abs(client.account_equity(1.0) - 200.0) < 1e-9


def test_account_equity_spot_ok_when_perps_429() -> None:
    cfg = BotConfig()
    cfg.dry_run = False
    cfg.account_address = "0xabc"
    client = HyperliquidClient(cfg)
    client.connect_sdk = lambda **kwargs: None

    def fake_info(payload):
        if payload["type"] == "clearinghouseState":
            raise RuntimeError("Hyperliquid /info HTTP 429: rate limited")
        if payload["type"] == "spotClearinghouseState":
            return _spot(478.749)
        raise AssertionError(payload["type"])

    client._post_info = fake_info
    assert abs(client.account_equity(10_000.0) - 478.749) < 1e-9


def test_account_equity_fallback_when_perps_zero_and_spot_fails() -> None:
    """perps=0 且现货 429 时不得静默报 $0。"""
    cfg = BotConfig()
    cfg.dry_run = False
    cfg.account_address = "0xabc"
    client = HyperliquidClient(cfg)
    client.connect_sdk = lambda **kwargs: None

    def fake_info(payload):
        if payload["type"] == "clearinghouseState":
            return _perps(0.0)
        raise RuntimeError("Hyperliquid /info HTTP 429")

    client._post_info = fake_info
    assert client.account_equity(10_000.0) == 10_000.0


def test_account_equity_fallback_when_both_fail() -> None:
    cfg = BotConfig()
    cfg.dry_run = False
    cfg.account_address = "0xabc"
    client = HyperliquidClient(cfg)
    client.connect_sdk = lambda **kwargs: None
    client._post_info = lambda payload: (_ for _ in ()).throw(RuntimeError("429"))
    assert client.account_equity(42.0) == 42.0


def test_dry_run_equity_path_unchanged() -> None:
    cfg = BotConfig()
    cfg.dry_run = True
    cfg.account_address = "0xabc"
    client = HyperliquidClient(cfg)
    called = {"n": 0}

    def boom(payload):
        called["n"] += 1
        raise AssertionError("dry-run must not hit /info")

    client._post_info = boom
    assert client.account_equity(10_000.0) == 10_000.0
    assert called["n"] == 0
