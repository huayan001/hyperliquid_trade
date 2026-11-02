from hl_bot.config import BotConfig
from hl_bot.exchange.client import extract_order_fill
from hl_bot.models import Position, Side, StrategyName
from hl_bot.runner import BotRunner
from hl_bot.strategies.base import make_close_intent


def _pos(*, size: float = 0.85) -> Position:
    return Position(
        symbol="HYPE",
        strategy=StrategyName.TREND,
        side=Side.LONG,
        size=size,
        entry_price=92.472,
        stop_price=88.0,
        leverage=5,
        opened_ts=1,
        extras={"_opened_size": size},
    )


def _hl_filled(size: str, px: str) -> dict:
    return {
        "status": "ok",
        "response": {
            "type": "order",
            "data": {"statuses": [{"filled": {"totalSz": size, "avgPx": px}}]},
        },
    }


def _live_runner(tmp_path, place_result) -> BotRunner:
    cfg = BotConfig()
    cfg.dry_run = False
    cfg.enable_live = True
    cfg.state_path = str(tmp_path / "paper.json")
    runner = BotRunner(cfg)
    runner.client.connect_sdk = lambda **kwargs: None
    runner.client.place = lambda intent, **kwargs: place_result
    return runner


def test_extract_fill_none_and_errors() -> None:
    assert extract_order_fill(None) is None
    assert extract_order_fill({"status": "error"}) is None
    assert extract_order_fill({"status": "dry_run", "intent": "x"}) is None
    assert extract_order_fill(
        {"status": "ok", "response": {"data": {"statuses": [{"error": "no position"}]}}}
    ) is None
    assert extract_order_fill(
        {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 1}}]}}}
    ) is None


def test_extract_fill_from_sdk_and_wrapped_order() -> None:
    fill = extract_order_fill(_hl_filled("0.85", "94.044"))
    assert fill is not None
    assert abs(fill.size - 0.85) < 1e-9
    assert fill.price is not None
    assert abs(fill.price - 94.044) < 1e-9

    wrapped = {"status": "ok", "order": _hl_filled("0.4", "94.1"), "stop": {}}
    inner = extract_order_fill(wrapped)
    assert inner is not None
    assert abs(inner.size - 0.4) < 1e-9


def test_live_close_clears_local_position_on_fill(tmp_path) -> None:
    runner = _live_runner(tmp_path, _hl_filled("0.85", "94.044"))
    pos = _pos()
    runner.account.positions.append(pos)
    intent = make_close_intent(pos, 94.044, "资金费率异常飙升")
    runner._execute(intent, now_ms=1)
    assert runner.account.position_for("HYPE") is None
    saved = (tmp_path / "paper.json").read_text(encoding="utf-8")
    assert "HYPE" not in saved or '"positions": []' in saved


def test_live_close_none_does_not_clear(tmp_path) -> None:
    runner = _live_runner(tmp_path, None)
    runner.account.positions.append(_pos())
    intent = make_close_intent(runner.account.position_for("HYPE"), 94.044, "retry")
    runner._execute(intent, now_ms=1)
    leftover = runner.account.position_for("HYPE")
    assert leftover is not None
    assert abs(leftover.size - 0.85) < 1e-9


def test_live_partial_fill_reduces_size(tmp_path) -> None:
    runner = _live_runner(tmp_path, _hl_filled("0.40", "94.1"))
    runner.account.positions.append(_pos(size=0.85))
    intent = make_close_intent(runner.account.position_for("HYPE"), 94.1, "partial")
    runner._execute(intent, now_ms=1)
    leftover = runner.account.position_for("HYPE")
    assert leftover is not None
    assert abs(leftover.size - 0.45) < 1e-9
