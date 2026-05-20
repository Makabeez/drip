"""
drip.risk
=========

Risk manager. Three primary functions:

    1. check_pretrade(state, action) -> RiskVerdict
       Called before HLExecutor.execute(). Can VETO a trade.

    2. check_intratrade(state) -> RiskVerdict
       Called every loop tick. Can force-close a position.

    3. record_close(execution_result, account_value_pre, account_value_post)
       Update daily PnL accumulator + reset checks.

State persistence:
    - kill_switch state persists in SQLite (table: risk_state)
    - resets automatically at UTC midnight (new day key)
    - peak_account_value tracked for drawdown calc

Triggers:
    - daily_kill_switch: today's PnL < -RISK_DAILY_LOSS_PCT * peak_account_value
    - liq_protection: marginUsed / accountValue > RISK_LIQ_MARGIN_RATIO
    - emergency_halt: account_value < 50% of initial

All material events emit Telegram alerts via telegram_alerts.py.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
from dataclasses import dataclass
from typing import Any

from telegram_alerts import alert, AlertLevel

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

DAILY_LOSS_PCT = float(os.environ.get("RISK_DAILY_LOSS_PCT", "0.02"))
LIQ_MARGIN_RATIO = float(os.environ.get("RISK_LIQ_MARGIN_RATIO", "0.40"))
EMERGENCY_HALT_PCT = float(os.environ.get("RISK_EMERGENCY_HALT_PCT", "0.50"))


# ----------------------------------------------------------------------------
# Verdict types
# ----------------------------------------------------------------------------

@dataclass
class RiskVerdict:
    """Outcome of a risk check."""
    ok: bool                   # True = proceed normally
    veto: bool = False         # True = block the trade (pretrade)
    force_close: bool = False  # True = close immediately (intratrade)
    reason: str | None = None  # Human-readable explanation


OK = RiskVerdict(ok=True)


# ----------------------------------------------------------------------------
# SQLite schema for persistent risk state
# ----------------------------------------------------------------------------

RISK_STATE_DDL = """
CREATE TABLE IF NOT EXISTS risk_state (
    day_key TEXT PRIMARY KEY,
    kill_switch_tripped INTEGER NOT NULL DEFAULT 0,
    kill_switch_tripped_at INTEGER,
    kill_switch_reason TEXT,
    daily_pnl_usd REAL NOT NULL DEFAULT 0,
    peak_account_value REAL NOT NULL DEFAULT 0,
    initial_account_value REAL NOT NULL DEFAULT 0,
    updated_at_ms INTEGER NOT NULL
);
"""


def _utc_day_key() -> str:
    """Returns 'YYYY-MM-DD' in UTC. Daily state keys on this."""
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


# ----------------------------------------------------------------------------
# RiskManager
# ----------------------------------------------------------------------------

class RiskManager:
    """
    Stateful risk gatekeeper. Holds today's PnL, kill switch state,
    and account value high-water mark. Persists across restarts via SQLite.
    """

    def __init__(self, db: sqlite3.Connection, initial_account_value: float = 0.0):
        self._db = db
        self._db.executescript(RISK_STATE_DDL)
        self._db.commit()

        self._day_key = _utc_day_key()
        self._load_or_init(initial_account_value)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_or_init(self, initial_account_value: float) -> None:
        """Load today's state or initialize a fresh row for today."""
        row = self._db.execute(
            "SELECT kill_switch_tripped, kill_switch_tripped_at, kill_switch_reason, "
            "daily_pnl_usd, peak_account_value, initial_account_value "
            "FROM risk_state WHERE day_key = ?",
            (self._day_key,),
        ).fetchone()

        if row is None:
            self._kill_switch_tripped = False
            self._kill_switch_tripped_at: int | None = None
            self._kill_switch_reason: str | None = None
            self._daily_pnl_usd = 0.0
            self._peak_account_value = initial_account_value
            self._initial_account_value = initial_account_value
            self._persist()
            logger.info(
                "RiskManager initialized fresh for %s (initial=$%.2f)",
                self._day_key,
                initial_account_value,
            )
        else:
            self._kill_switch_tripped = bool(row[0])
            self._kill_switch_tripped_at = row[1]
            self._kill_switch_reason = row[2]
            self._daily_pnl_usd = float(row[3])
            self._peak_account_value = float(row[4])
            self._initial_account_value = float(row[5])
            if self._initial_account_value == 0 and initial_account_value > 0:
                self._initial_account_value = initial_account_value
                self._persist()
            logger.info(
                "RiskManager loaded for %s: kill=%s pnl=$%.2f peak=$%.2f",
                self._day_key,
                self._kill_switch_tripped,
                self._daily_pnl_usd,
                self._peak_account_value,
            )

    def _persist(self) -> None:
        now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        self._db.execute(
            """
            INSERT INTO risk_state (
                day_key, kill_switch_tripped, kill_switch_tripped_at,
                kill_switch_reason, daily_pnl_usd, peak_account_value,
                initial_account_value, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(day_key) DO UPDATE SET
                kill_switch_tripped = excluded.kill_switch_tripped,
                kill_switch_tripped_at = excluded.kill_switch_tripped_at,
                kill_switch_reason = excluded.kill_switch_reason,
                daily_pnl_usd = excluded.daily_pnl_usd,
                peak_account_value = excluded.peak_account_value,
                initial_account_value = excluded.initial_account_value,
                updated_at_ms = excluded.updated_at_ms
            """,
            (
                self._day_key,
                int(self._kill_switch_tripped),
                self._kill_switch_tripped_at,
                self._kill_switch_reason,
                self._daily_pnl_usd,
                self._peak_account_value,
                self._initial_account_value,
                now_ms,
            ),
        )
        self._db.commit()

    def _maybe_rollover_day(self) -> None:
        """If UTC day has changed, persist the old day and start fresh."""
        current = _utc_day_key()
        if current == self._day_key:
            return
        logger.info("Day rollover: %s -> %s, resetting kill switch", self._day_key, current)
        self._day_key = current
        self._kill_switch_tripped = False
        self._kill_switch_tripped_at = None
        self._kill_switch_reason = None
        self._daily_pnl_usd = 0.0
        # Keep peak across days; initial stays the same
        self._persist()

    # ------------------------------------------------------------------
    # State accessors (used by loop.py + dashboard)
    # ------------------------------------------------------------------

    @property
    def kill_switch_tripped(self) -> bool:
        self._maybe_rollover_day()
        return self._kill_switch_tripped

    @property
    def daily_pnl_usd(self) -> float:
        self._maybe_rollover_day()
        return self._daily_pnl_usd

    def snapshot(self) -> dict[str, Any]:
        """For dashboard /state endpoint."""
        self._maybe_rollover_day()
        return {
            "day_key": self._day_key,
            "kill_switch_tripped": self._kill_switch_tripped,
            "kill_switch_tripped_at": self._kill_switch_tripped_at,
            "kill_switch_reason": self._kill_switch_reason,
            "daily_pnl_usd": round(self._daily_pnl_usd, 4),
            "daily_loss_threshold_usd": -DAILY_LOSS_PCT * self._peak_account_value,
            "peak_account_value": round(self._peak_account_value, 2),
            "initial_account_value": round(self._initial_account_value, 2),
        }

    # ------------------------------------------------------------------
    # Pre-trade gate
    # ------------------------------------------------------------------

    def check_pretrade(self, state: dict[str, Any], action_side: str) -> RiskVerdict:
        """
        Called by loop.py between decide() and execute().
        Returns a RiskVerdict — veto=True blocks the trade.
        """
        self._maybe_rollover_day()
        self._refresh_peak(state)

        # Already-closing actions always proceed (we want to be able to exit)
        if action_side in ("hold", "close"):
            return OK

        if self._kill_switch_tripped:
            return RiskVerdict(
                ok=False,
                veto=True,
                reason=f"kill_switch_active ({self._kill_switch_reason})",
            )

        # Emergency halt — account value dropped below 50% initial
        if self._initial_account_value > 0:
            ratio = float(state["marginSummary"]["accountValue"]) / self._initial_account_value
            if ratio < EMERGENCY_HALT_PCT:
                self._trip_kill_switch(
                    f"emergency_halt account ratio {ratio:.2%} < {EMERGENCY_HALT_PCT:.0%}"
                )
                return RiskVerdict(
                    ok=False, veto=True, reason="emergency_halt"
                )

        return OK

    # ------------------------------------------------------------------
    # Intra-trade liquidation watch
    # ------------------------------------------------------------------

    async def check_intratrade(self, state: dict[str, Any]) -> RiskVerdict:
        """
        Called every loop tick. Returns force_close=True if approaching liq.
        """
        self._maybe_rollover_day()

        ms = state.get("marginSummary", {})
        try:
            account_value = float(ms.get("accountValue", 0))
            margin_used = float(ms.get("totalMarginUsed", 0))
        except (TypeError, ValueError):
            return OK

        if account_value <= 0:
            return OK

        ratio = margin_used / account_value
        if ratio > LIQ_MARGIN_RATIO:
            msg = (
                f"liq_protection: margin_used/account = {ratio:.2%} "
                f"> threshold {LIQ_MARGIN_RATIO:.0%} (NAV=${account_value:.2f})"
            )
            logger.warning(msg)
            await alert(AlertLevel.WARN, msg)
            return RiskVerdict(ok=False, force_close=True, reason="liq_protection")

        return OK

    # ------------------------------------------------------------------
    # Post-close accounting
    # ------------------------------------------------------------------

    async def record_close(
        self,
        account_value_pre: float,
        account_value_post: float,
    ) -> None:
        """Update daily PnL based on account value delta around the close."""
        self._maybe_rollover_day()
        pnl_delta = account_value_post - account_value_pre
        self._daily_pnl_usd += pnl_delta

        # Update peak
        if account_value_post > self._peak_account_value:
            self._peak_account_value = account_value_post

        # Check daily loss threshold
        loss_limit = DAILY_LOSS_PCT * self._peak_account_value
        if self._daily_pnl_usd <= -loss_limit and not self._kill_switch_tripped:
            self._trip_kill_switch(
                f"daily_loss: pnl=${self._daily_pnl_usd:.2f} <= -${loss_limit:.2f} "
                f"({DAILY_LOSS_PCT:.0%} of peak ${self._peak_account_value:.2f})"
            )
            await alert(
                AlertLevel.KILL,
                f"Daily kill switch tripped. Today PnL ${self._daily_pnl_usd:.2f}. "
                f"Halting new trades for 24h.",
            )

        self._persist()
        logger.info(
            "  daily PnL: $%.4f (limit -$%.2f, kill=%s)",
            self._daily_pnl_usd,
            loss_limit,
            self._kill_switch_tripped,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _refresh_peak(self, state: dict[str, Any]) -> None:
        """Update peak account value if current exceeds it."""
        try:
            av = float(state["marginSummary"]["accountValue"])
            if av > self._peak_account_value:
                self._peak_account_value = av
                self._persist()
        except (KeyError, TypeError, ValueError):
            pass

    def _trip_kill_switch(self, reason: str) -> None:
        if self._kill_switch_tripped:
            return
        self._kill_switch_tripped = True
        self._kill_switch_tripped_at = int(
            dt.datetime.now(dt.timezone.utc).timestamp() * 1000
        )
        self._kill_switch_reason = reason
        self._persist()
        logger.warning("KILL SWITCH TRIPPED: %s", reason)