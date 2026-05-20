"""
drip.loop
=========

The main autonomous agent loop. Brings together signal_client,
decision, executor, risk, and dashboard into a single process.

Run with:
    poetry run python loop.py

Three concurrent components in one event loop:
    1. Agent loop — polls signals, decides, executes, manages risk
    2. Dashboard server — FastAPI on STATE_PORT (default 8086)
    3. (Mock emitter runs in a separate process via tmux)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

import uvicorn  # noqa: E402

from dashboard import build_app  # noqa: E402
from cctp import CCTPBridge, BridgeResult  # noqa: E402
from decision import decide  # noqa: E402
from executor import HLExecutor  # noqa: E402
from reasoning import TRACE_TABLE_DDL, persist_trace  # noqa: E402
from risk import RiskManager  # noqa: E402
from signal_client import Signal, SignalClient, SignalClientError  # noqa: E402
from telegram_alerts import alert, AlertLevel, info as tg_info  # noqa: E402


logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

POLL_INTERVAL = float(os.environ.get("AGENT_POLL_INTERVAL", "2.0"))
TIME_STOP_SECONDS = int(os.environ.get("RISK_TIME_STOP_SECONDS", "60"))
SQLITE_PATH = os.environ.get(
    "SQLITE_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "drip.sqlite"),
)
STATE_PORT = int(os.environ.get("STATE_PORT", "8086"))


# ----------------------------------------------------------------------------
# Loop
# ----------------------------------------------------------------------------

class AgentLoop:
    def __init__(self) -> None:
        self._client = SignalClient(consumer_private_key=os.environ["CONSUMER_PK"])
        self._executor = HLExecutor()
        self._db = _init_db()

        # Bootstrap initial account value for RiskManager
        initial_state = self._executor.get_state()
        initial_av = float(initial_state.get("marginSummary", {}).get("accountValue", 0))
        self._risk = RiskManager(self._db, initial_account_value=initial_av)

        # Track open position metadata that HL doesn't store
        self._position_opened_at: float | None = None
        self._account_value_at_open: float | None = None
        self._last_trade_at: float = 0.0
        self._started_at: float = time.time()

        # Counters for telemetry
        self._signals_received: int = 0
        self._trades_opened: int = 0
        self._trades_closed: int = 0

        # CCTP bridge — Consumer wallet sends, HL Master receives on Arb Sepolia
        # The bridge is enabled if CONSUMER_PK + HL_MASTER_ADDRESS are set.
        # Manual /cctp/trigger endpoint fires it; autonomous trigger is gated
        # behind CCTP_AUTO_TRIGGER_ENABLED (default false for safety).
        self._cctp: CCTPBridge | None = None
        self._cctp_recipient: str | None = None
        self._cctp_lock = asyncio.Lock()
        self._cctp_last_trigger_at: float = 0.0
        try:
            if os.environ.get("CONSUMER_PK") and os.environ.get("HL_MASTER_ADDRESS"):
                self._cctp = CCTPBridge(
                    private_key=os.environ["CONSUMER_PK"],
                    db=self._db,
                )
                self._cctp_recipient = os.environ["HL_MASTER_ADDRESS"]
                logger.info(
                    "CCTP bridge ready: Consumer → HL Master (%s) on Arb Sepolia",
                    self._cctp_recipient,
                )
            else:
                logger.warning("CCTP disabled: CONSUMER_PK or HL_MASTER_ADDRESS missing")
        except Exception as e:
            logger.exception("CCTP bridge init failed: %s", e)
            self._cctp = None

    # ------------------------------------------------------------------
    # Public accessors (used by dashboard /state endpoint)
    # ------------------------------------------------------------------

    def get_account_state(self) -> dict[str, Any]:
        return self._executor.get_state()

    def get_risk_snapshot(self) -> dict[str, Any]:
        return self._risk.snapshot()

    def get_counters(self) -> dict[str, Any]:
        return {
            "started_at": self._started_at,
            "signals_received": self._signals_received,
            "trades_opened": self._trades_opened,
            "trades_closed": self._trades_closed,
            "position_opened_at": self._position_opened_at,
        }

    # ------------------------------------------------------------------
    # CCTP — Circle CCTP V2 bridge: Arc Testnet → Arbitrum Sepolia
    # ------------------------------------------------------------------

    def get_cctp_bridges(self, limit: int = 10) -> list[dict[str, Any]]:
        """Return last N CCTP bridges from SQLite for dashboard rendering."""
        if not self._cctp:
            return []
        try:
            return self._cctp.list_bridges(limit=limit)
        except Exception:
            logger.exception("list_bridges failed")
            return []

    async def manual_trigger_cctp(self, amount_usdc: float = 1.0) -> "BridgeResult":
        """
        Fire a CCTP bridge from Consumer (Arc) → HL Master (Arb Sepolia).

        Called by the dashboard POST /cctp/trigger endpoint. Serializes via
        an asyncio.Lock so only one bridge runs at a time. Enforces a 60s
        cooldown between bridges to prevent demo-button mashing.
        """
        if not self._cctp:
            raise RuntimeError("CCTP bridge not initialized (check env vars)")

        # Cooldown: don't allow back-to-back bridges within 60s
        now = time.time()
        cooldown_remaining = 60 - (now - self._cctp_last_trigger_at)
        if cooldown_remaining > 0:
            raise RuntimeError(
                f"CCTP bridge cooldown: try again in {int(cooldown_remaining)}s"
            )

        async with self._cctp_lock:
            self._cctp_last_trigger_at = time.time()
            logger.info(
                "Manual CCTP trigger: bridging %.2f USDC → HL Master on Arb Sepolia",
                amount_usdc,
            )
            try:
                await alert(
                    AlertLevel.INFO,
                    f"🌉 CCTP bridge starting: {amount_usdc:.2f} USDC → HL Master (Arb Sepolia)",
                )
            except Exception:
                pass

            result = await self._cctp.bridge_to_arb_sepolia(
                amount_usdc=amount_usdc,
                recipient=self._cctp_recipient,
            )

            try:
                if result.success:
                    await alert(
                        AlertLevel.INFO,
                        f"🌉 CCTP bridge complete in {result.total_seconds}s\n"
                        f"  burn: {result.burn_tx[:10]}…\n"
                        f"  mint: {result.mint_tx[:10]}…",
                    )
                else:
                    await alert(
                        AlertLevel.WARN,
                        f"🌉 CCTP bridge failed: {result.error}",
                    )
            except Exception:
                pass

            return result

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        logger.info(
            "Agent loop starting: poll=%.1fs time_stop=%ds",
            POLL_INTERVAL,
            TIME_STOP_SECONDS,
        )
        await tg_info(
            f"Drip agent online. Daily kill threshold: ${self._risk.snapshot()['daily_loss_threshold_usd']:.2f}"
        )
        try:
            while True:
                try:
                    await self._tick()
                except Exception as e:
                    logger.exception("tick raised: %s", e)
                await asyncio.sleep(POLL_INTERVAL)
        finally:
            await self._client.close()
            self._db.close()

    # ------------------------------------------------------------------
    # Single iteration
    # ------------------------------------------------------------------

    async def _tick(self) -> None:
        if await self._maybe_intratrade_close():
            return
        if await self._maybe_time_stop():
            return

        try:
            signal = await self._client.fetch_signal()
        except SignalClientError as e:
            logger.warning("fetch_signal error: %s", e)
            return

        if signal is None:
            return

        self._signals_received += 1
        logger.info(
            "[#%d] signal: %s %s conf=%.3f vol=%.2f tx=%s",
            self._signals_received,
            signal.symbol,
            signal.direction,
            signal.confidence,
            signal.vol_ratio,
            (signal.tx_hash[:10] + "...") if signal.tx_hash else "?",
        )

        state = self._executor.get_state()
        mid_px = self._executor.get_mid_price(signal.symbol)
        portfolio = _build_portfolio(
            state, mid_px, self._risk.daily_pnl_usd, self._last_trade_at
        )
        market = _build_market(mid_px, signal.vol_ratio)

        trace = decide(
            signal=signal,
            portfolio=portfolio,
            market=market,
            kill_switch_tripped=self._risk.kill_switch_tripped,
        )
        logger.info("  decision: %s", trace.to_telegram_summary())

        verdict = self._risk.check_pretrade(state, trace.action.side)
        if verdict.veto:
            logger.warning("  ✗ pre-trade VETO: %s", verdict.reason)
            await alert(AlertLevel.WARN, f"Trade vetoed: {verdict.reason}")
            trace.set_execution_result(
                {"success": False, "error": f"vetoed:{verdict.reason}", "action_taken": "vetoed"}
            )
            persist_trace(self._db, trace)
            return

        pre_av = float(state["marginSummary"]["accountValue"])
        result = self._executor.execute(trace.action)
        trace.set_execution_result(result.to_dict())

        if result.success and result.action_taken == "opened":
            self._position_opened_at = time.time()
            self._account_value_at_open = pre_av
            self._last_trade_at = time.time()
            self._trades_opened += 1
            logger.info(
                "  ✓ OPENED %s size=%s @ %s oid=%s",
                trace.action.side, result.fill_size, result.fill_price, result.order_id,
            )

        elif result.success and result.action_taken == "closed":
            await asyncio.sleep(1)
            post_state = self._executor.get_state()
            post_av = float(post_state["marginSummary"]["accountValue"])
            base_av = self._account_value_at_open or pre_av
            await self._risk.record_close(base_av, post_av)

            self._position_opened_at = None
            self._account_value_at_open = None
            self._last_trade_at = time.time()
            self._trades_closed += 1

            pnl = post_av - base_av
            sign = "+" if pnl >= 0 else ""
            logger.info(
                "  ✓ CLOSED size=%s @ %s oid=%s  PnL=%s$%.4f",
                result.fill_size, result.fill_price, result.order_id, sign, pnl,
            )

        elif not result.success:
            logger.error("  ✗ execution failed: %s", result.error)
            await alert(AlertLevel.ERROR, f"Execution failed: {result.error}")

        try:
            persist_trace(self._db, trace)
        except Exception:
            logger.exception("persist_trace failed")

    # ------------------------------------------------------------------
    # Intra-trade liq protection
    # ------------------------------------------------------------------

    async def _maybe_intratrade_close(self) -> bool:
        if self._position_opened_at is None:
            return False
        state = self._executor.get_state()
        verdict = await self._risk.check_intratrade(state)
        if not verdict.force_close:
            return False
        logger.warning("Intratrade force-close: %s", verdict.reason)
        await alert(AlertLevel.WARN, f"Force-closing position: {verdict.reason}")
        return await self._force_close(reason=verdict.reason or "intratrade")

    # ------------------------------------------------------------------
    # Time-stop check
    # ------------------------------------------------------------------

    async def _maybe_time_stop(self) -> bool:
        if self._position_opened_at is None:
            return False
        elapsed = time.time() - self._position_opened_at
        if elapsed < TIME_STOP_SECONDS:
            return False
        logger.info("Time stop fired: position open %.1fs", elapsed)
        return await self._force_close(reason="time_stop")

    # ------------------------------------------------------------------
    # Shared force-close helper
    # ------------------------------------------------------------------

    async def _force_close(self, reason: str) -> bool:
        pre_state = self._executor.get_state()
        pre_av = float(pre_state["marginSummary"]["accountValue"])

        result = self._executor._close()
        if not result.success:
            logger.error("  ✗ force-close failed: %s", result.error)
            return False

        await asyncio.sleep(1)
        post_state = self._executor.get_state()
        post_av = float(post_state["marginSummary"]["accountValue"])
        base_av = self._account_value_at_open or pre_av
        await self._risk.record_close(base_av, post_av)

        pnl = post_av - base_av
        sign = "+" if pnl >= 0 else ""
        logger.info(
            "  ✓ FORCE-CLOSED (%s) @ %s oid=%s  PnL=%s$%.4f",
            reason, result.fill_price, result.order_id, sign, pnl,
        )

        self._position_opened_at = None
        self._account_value_at_open = None
        self._last_trade_at = time.time()
        self._trades_closed += 1
        return True


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def _init_db() -> sqlite3.Connection:
    Path(SQLITE_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SQLITE_PATH)
    conn.executescript(TRACE_TABLE_DDL)
    conn.commit()
    return conn


def _build_portfolio(
    state: dict[str, Any],
    mid_px: float,
    daily_pnl_usd: float,
    last_trade_at: float,
) -> dict[str, Any]:
    ms = state.get("marginSummary", {})
    account_value = float(ms.get("accountValue", 0))
    total_used = float(ms.get("totalMarginUsed", 0))
    free_margin = account_value - total_used

    positions = state.get("assetPositions", [])
    side: str | None = None
    size_btc = 0.0
    entry_px = 0.0
    unrealized = 0.0
    for p in positions:
        pos = p.get("position", {})
        if pos.get("coin") != "BTC":
            continue
        szi = float(pos.get("szi", 0))
        if szi == 0:
            continue
        side = "long" if szi > 0 else "short"
        size_btc = abs(szi)
        entry_px = float(pos.get("entryPx", 0))
        unrealized = float(pos.get("unrealizedPnl", 0))
        break

    return {
        "account_value_usd": account_value,
        "free_margin_usd": free_margin,
        "margin_used_usd": total_used,
        "open_position_side": side,
        "open_position_size_btc": size_btc,
        "open_position_entry_px": entry_px,
        "open_position_unrealized_pnl_usd": unrealized,
        "daily_pnl_usd": daily_pnl_usd,
        "seconds_since_last_trade": (
            time.time() - last_trade_at if last_trade_at else 9999.0
        ),
    }


def _build_market(mid_px: float, vol_ratio: float) -> dict[str, Any]:
    return {
        "mid_px": mid_px,
        "bid_px": mid_px * (1 - 0.0001),
        "ask_px": mid_px * (1 + 0.0001),
        "spread_bps": 2.0,
        "realized_vol_1h_pct": 0.005 * vol_ratio,
        "funding_rate_8h_pct": 0.0001,
    }


# ----------------------------------------------------------------------------
# Entry point — runs agent loop + dashboard server concurrently
# ----------------------------------------------------------------------------

async def _serve_dashboard(agent: AgentLoop) -> None:
    """Run the dashboard FastAPI on STATE_PORT alongside the agent."""
    app = build_app(
        get_account_state=agent.get_account_state,
        get_risk_snapshot=agent.get_risk_snapshot,
        get_loop_counters=agent.get_counters,
        sqlite_path=SQLITE_PATH,
        get_cctp_bridges=agent.get_cctp_bridges,
        trigger_cctp_bridge=agent.manual_trigger_cctp,
    )
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=STATE_PORT,
        log_level=os.environ.get("LOG_LEVEL", "warning").lower(),
    )
    server = uvicorn.Server(config)
    logger.info("Dashboard starting on :%d", STATE_PORT)
    await server.serve()


async def _main() -> None:
    agent = AgentLoop()
    await asyncio.gather(
        agent.run(),
        _serve_dashboard(agent),
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        logger.info("Agent loop stopped by user")
