"""
drip.telegram_alerts
====================

Thin wrapper around Telegram Bot API for Risk Manager alerts.
Reuses Makaclaw's bot token + chat ID (same droplet, same chat).
Every Drip alert is prefixed with TG_ALERT_PREFIX (default: "🤖 DRIP")
so they're scannable among Makaclaw's normal audit stream.

Usage:
    from drip.telegram_alerts import alert, AlertLevel

    await alert(AlertLevel.WARN, "Margin ratio 0.42 — auto-deleveraging")
    await alert(AlertLevel.KILL, "Daily loss -2.1% NAV — halting 24h")
"""

from __future__ import annotations

import asyncio
import logging
import os
from enum import Enum

import httpx

logger = logging.getLogger(__name__)


TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
TG_ALERT_PREFIX = os.environ.get("TG_ALERT_PREFIX", "🤖 DRIP")

# Rate limit: Telegram allows 30 msg/sec per bot. We're far below that,
# but during a kill-switch event the Risk Manager could fire many alerts
# in quick succession. Soft cap via in-process semaphore.
_send_semaphore = asyncio.Semaphore(5)


class AlertLevel(str, Enum):
    INFO = "ℹ️"
    WARN = "⚠️"
    KILL = "🛑"
    WIN = "✅"
    ERROR = "❌"


async def alert(level: AlertLevel, message: str, parse_mode: str = "Markdown") -> bool:
    """
    Send an alert to Telegram. Returns True on success, False on failure
    (logs the error but never raises — alerts should not crash the agent).
    """
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        logger.warning("TG credentials missing — alert dropped: %s", message)
        return False

    formatted = f"{level.value} {TG_ALERT_PREFIX} {message}"

    async with _send_semaphore:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": TG_CHAT_ID,
                        "text": formatted,
                        "parse_mode": parse_mode,
                        "disable_web_page_preview": True,
                    },
                )
                if resp.status_code != 200:
                    logger.error(
                        "Telegram send failed: %s %s",
                        resp.status_code,
                        resp.text,
                    )
                    return False
                return True
        except Exception as exc:  # noqa: BLE001 — never let alerts crash agent
            logger.exception("Telegram alert exception: %s", exc)
            return False


# Convenience wrappers — semantic call sites in risk.py read better
async def info(msg: str) -> bool:
    return await alert(AlertLevel.INFO, msg)


async def warn(msg: str) -> bool:
    return await alert(AlertLevel.WARN, msg)


async def kill(msg: str) -> bool:
    return await alert(AlertLevel.KILL, msg)


async def win(msg: str) -> bool:
    return await alert(AlertLevel.WIN, msg)


async def error(msg: str) -> bool:
    return await alert(AlertLevel.ERROR, msg)
