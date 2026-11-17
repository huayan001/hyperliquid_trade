"""菲阿里四价（纯规则）。独立入口，默认关闭，不参与趋势主循环。"""

from hl_bot.phil_ali.gate import PhilAliGate, live_orders_allowed, load_gate

__all__ = ["PhilAliGate", "live_orders_allowed", "load_gate"]
