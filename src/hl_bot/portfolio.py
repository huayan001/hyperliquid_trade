"""账户净敞口过滤。

净空 = 空头开仓名义（|size| × entry）大于多头开仓名义。不看浮盈，也不看止损风险，
避免止损远近把「账上已经是空头」算成净多。

只拒绝新标的的做多。已有多仓的止损、减仓、Donchian 补仓、金字塔不经过这里。
"""

from __future__ import annotations

from hl_bot.models import Position, Side

# 名义差小于这个数视为打平，不算净空。
_NOTIONAL_EPS = 1e-6


def signed_open_notional(positions: list[Position]) -> float:
    """多头为正、空头为负的开仓名义合计。"""
    total = 0.0
    for pos in positions:
        if pos.size <= 0:
            continue
        sign = 1.0 if pos.side is Side.LONG else -1.0
        total += sign * abs(pos.size) * float(pos.entry_price)
    return total


def net_exposure_blocks_long(positions: list[Position], *, enabled: bool = True) -> str | None:
    """净空时返回拒绝新多的原因；否则 None。enabled=False 时始终放行。"""
    if not enabled:
        return None
    long_ntl = 0.0
    short_ntl = 0.0
    shorts: list[Position] = []
    for pos in positions:
        if pos.size <= 0:
            continue
        notional = abs(pos.size) * float(pos.entry_price)
        if pos.side is Side.SHORT:
            short_ntl += notional
            shorts.append(pos)
        elif pos.side is Side.LONG:
            long_ntl += notional
    signed = long_ntl - short_ntl
    if signed >= -_NOTIONAL_EPS:
        return None
    names = ", ".join(
        f"{pos.symbol}空 ${abs(pos.size) * float(pos.entry_price):.2f}" for pos in shorts
    )
    return (
        "净空敞口，禁止新开多"
        f"（空头名义 ${short_ntl:.2f} > 多头名义 ${long_ntl:.2f}，"
        f"净名义 ${signed:.2f}；{names}）"
    )
