"""告警出口。Telegram 等为可选桩，默认只写日志。"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class AlertSink:
    def send(self, message: str) -> None:
        logger.info("alert: %s", message)


class TelegramAlertStub(AlertSink):
    """预留：未配置 token 时与日志告警相同，不发起网络请求。"""

    def __init__(self, token: str | None = None, chat_id: str | None = None) -> None:
        self.token = token
        self.chat_id = chat_id
        self.enabled = bool(token and chat_id)

    def send(self, message: str) -> None:
        if not self.enabled:
            super().send(message)
            return
        logger.info("telegram stub (未实现发送): %s", message)
