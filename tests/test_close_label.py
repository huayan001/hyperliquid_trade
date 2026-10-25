from hl_bot.alerts import AlertSink
from hl_bot.config import BotConfig
from hl_bot.funding import make_funding_exit_intent
from hl_bot.models import IntentAction, Position, Side, StrategyName
from hl_bot.runner import BotRunner, _action_side_label, _format_intent
from hl_bot.strategies.base import make_close_intent


def _long(*, symbol: str = "HYPE") -> Position:
    return Position(
        symbol=symbol,
        strategy=StrategyName.TREND,
        side=Side.LONG,
        size=0.85,
        entry_price=92.472,
        stop_price=88.0,
        leverage=5,
        opened_ts=1,
        extras={"_opened_size": 0.85},
    )


def _short() -> Position:
    return Position(
        symbol="ETH",
        strategy=StrategyName.MEAN_REVERSION,
        side=Side.SHORT,
        size=1.0,
        entry_price=100.0,
        stop_price=106.0,
        leverage=3,
        opened_ts=1,
        extras={"_opened_size": 1.0},
    )


def test_close_intent_order_side_is_opposite_of_position() -> None:
    intent = make_close_intent(_long(), 94.044, "资金费率异常飙升")
    assert intent.action is IntentAction.CLOSE
    assert intent.side is Side.SHORT
    assert intent.position_side() is Side.LONG


def test_close_long_label_is_ping_duo_not_short() -> None:
    intent = make_funding_exit_intent(_long(), 94.044, "资金费率异常飙升")
    assert _action_side_label(intent) == "平多"
    text = _format_intent(intent)
    assert "平多" in text
    assert "平空" not in text
    assert "short" not in text.lower()
    assert intent.position_side() is Side.LONG


def test_close_short_label_is_ping_kong() -> None:
    intent = make_close_intent(_short(), 99.0, "时间止损")
    assert intent.position_side() is Side.SHORT
    assert _action_side_label(intent) == "平空"
    assert "平多" not in _format_intent(intent)


def test_alert_close_long_does_not_say_short() -> None:
    messages: list[str] = []

    class Capture(AlertSink):
        def send(self, message: str) -> None:
            messages.append(message)

    cfg = BotConfig()
    runner = BotRunner(cfg, alerts=Capture())
    intent = make_close_intent(_long(), 94.044, "资金费率异常飙升")
    runner._execute(intent, now_ms=1, persist_paper=False)
    assert messages
    alert = messages[0]
    assert "close HYPE long" in alert
    assert "平多" in alert
    assert "short" not in alert.lower()
