"""
drip.dashboard
==============

FastAPI app for the Drip agent dashboard.

Endpoints:
    GET /            → static index.html (Bloomberg-style dashboard)
    GET /state       → live agent state (account, position, risk, recent trades)
    GET /health      → simple health probe

Mounted as a background uvicorn task by loop.py. Reads state from:
    - HLExecutor.get_state() — current account value, positions
    - RiskManager.snapshot() — kill switch, daily PnL, peak
    - SQLite traces — recent trade history
    - loop counters — signals_received, trades_opened/closed, uptime
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger(__name__)


STATE_PORT = int(os.environ.get("STATE_PORT", "8086"))
STATIC_DIR = Path(__file__).parent / "static"


def build_app(
    get_account_state,
    get_risk_snapshot,
    get_loop_counters,
    sqlite_path: str,
    get_cctp_bridges=None,
    trigger_cctp_bridge=None,
) -> FastAPI:
    """
    Factory that wires the dashboard to live agent components.

    Args:
        get_account_state: callable → HL user_state dict
        get_risk_snapshot: callable → RiskManager.snapshot() dict
        get_loop_counters: callable → dict with started_at, signals_received,
                           trades_opened, trades_closed, position_opened_at
        sqlite_path: path to drip.sqlite for trace queries
        get_cctp_bridges: optional callable → list of CCTP bridge dicts
        trigger_cctp_bridge: optional async callable(amount_usdc) →
                             BridgeResult; called by POST /cctp/trigger
    """
    app = FastAPI(title="Drip Dashboard")

    # Serve static assets if the dir exists
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "service": "drip-dashboard"}

    @app.get("/")
    async def root():
        index = STATIC_DIR / "index.html"
        if not index.exists():
            return JSONResponse(
                status_code=503,
                content={"error": "static/index.html not found", "expected": str(index)},
            )
        return FileResponse(str(index), media_type="text/html")

    @app.get("/state")
    async def state() -> dict[str, Any]:
        """Live agent state — polled by dashboard every 2s."""
        try:
            hl_state = get_account_state()
        except Exception as e:
            logger.exception("get_account_state failed")
            hl_state = {"error": str(e), "marginSummary": {}, "assetPositions": []}

        risk = get_risk_snapshot()
        counters = get_loop_counters()

        # Position
        ms = hl_state.get("marginSummary", {})
        positions = hl_state.get("assetPositions", [])
        pos_payload: dict[str, Any] | None = None
        for p in positions:
            pos = p.get("position", {})
            szi = float(pos.get("szi", 0))
            if szi == 0:
                continue
            pos_payload = {
                "coin": pos.get("coin"),
                "side": "long" if szi > 0 else "short",
                "size_btc": abs(szi),
                "entry_px": float(pos.get("entryPx", 0)),
                "unrealized_pnl_usd": float(pos.get("unrealizedPnl", 0)),
                "leverage": pos.get("leverage", {}),
            }
            break

        # Recent traces (last 20)
        try:
            conn = sqlite3.connect(sqlite_path)
            rows = conn.execute(
                """
                SELECT trace_id, created_at_ms, side, size_usd, payment_tx_hash, json_blob
                FROM traces
                ORDER BY created_at_ms DESC
                LIMIT 20
                """
            ).fetchall()
            conn.close()
        except Exception as e:
            logger.exception("trace query failed")
            rows = []

        traces = []
        for row in rows:
            tid, created, side, size_usd, payment_tx, blob = row
            try:
                full = json.loads(blob) if blob else {}
            except Exception:
                full = {}

            action = full.get("action") or {}
            exec_result = full.get("execution_result") or {}
            signal = full.get("signal") or {}
            traces.append(
                {
                    "trace_id": tid,
                    "trace_id_short": tid[:8],
                    "created_at_ms": created,
                    "side": side,
                    "hold_reason": action.get("hold_reason"),
                    "size_usd": size_usd,
                    "leverage": action.get("leverage"),
                    "tp_px": action.get("tp_px"),
                    "sl_px": action.get("sl_px"),
                    "signal_confidence": signal.get("confidence"),
                    "signal_vol_ratio": signal.get("vol_ratio"),
                    "payment_tx_hash": payment_tx,
                    "exec_success": exec_result.get("success"),
                    "exec_action": exec_result.get("action_taken"),
                    "fill_price": exec_result.get("fill_price"),
                    "fill_size": exec_result.get("fill_size"),
                    "order_id": exec_result.get("order_id"),
                    "exec_error": exec_result.get("error"),
                }
            )

        # Daily-PnL sparkline: pull all closed trades from today's traces in chronological order
        try:
            conn = sqlite3.connect(sqlite_path)
            spark_rows = conn.execute(
                """
                SELECT created_at_ms, json_blob
                FROM traces
                WHERE created_at_ms >= ?
                ORDER BY created_at_ms ASC
                """,
                (int(time.time() * 1000) - 24 * 3600 * 1000,),
            ).fetchall()
            conn.close()
        except Exception:
            spark_rows = []

        cumulative_pnl = 0.0
        sparkline: list[dict[str, float]] = []
        for ts, blob in spark_rows:
            try:
                full = json.loads(blob)
                exec_result = full.get("execution_result") or {}
                if exec_result.get("action_taken") == "closed" and exec_result.get("success"):
                    # We don't store PnL in the trace directly (it's computed in loop.py).
                    # For the sparkline we just count closes; real PnL lives on risk.daily_pnl_usd.
                    sparkline.append({"ts": ts, "n": len(sparkline) + 1})
            except Exception:
                pass

        # CCTP bridges (last 10) — only if callable was provided
        bridges: list[dict[str, Any]] = []
        cctp_enabled = bool(get_cctp_bridges)
        trigger_enabled = bool(trigger_cctp_bridge) and os.environ.get(
            "CCTP_TRIGGER_ENABLED", "true"
        ).lower() not in ("false", "0", "no")
        if cctp_enabled:
            try:
                bridges = get_cctp_bridges(limit=10)
            except Exception:
                logger.exception("get_cctp_bridges failed")
                bridges = []

        return {
            "now_ms": int(time.time() * 1000),
            "uptime_seconds": int(time.time() - counters.get("started_at", time.time())),
            "network": os.environ.get("HL_NETWORK", "testnet"),
            "account": {
                "value_usd": float(ms.get("accountValue", 0)),
                "withdrawable_usd": float(hl_state.get("withdrawable", 0)),
                "margin_used_usd": float(ms.get("totalMarginUsed", 0)),
                "total_notional_usd": float(ms.get("totalNtlPos", 0)),
            },
            "position": pos_payload,
            "position_opened_at_ms": (
                int(counters["position_opened_at"] * 1000)
                if counters.get("position_opened_at")
                else None
            ),
            "risk": risk,
            "counters": {
                "signals_received": counters.get("signals_received", 0),
                "trades_opened": counters.get("trades_opened", 0),
                "trades_closed": counters.get("trades_closed", 0),
            },
            "traces": traces,
            "sparkline": sparkline,
            "cctp": {
                "enabled": cctp_enabled,
                "trigger_enabled": trigger_enabled,
                "bridges": bridges,
            },
        }

    @app.post("/cctp/trigger")
    async def cctp_trigger(amount_usdc: float = 1.0) -> dict[str, Any]:
        """
        Manually fire a CCTP bridge from Consumer → HL Master on Arb Sepolia.

        Demo-only endpoint. Production use should integrate with the autonomous
        low-margin trigger inside the agent loop (see `CCTPManager` in loop.py).
        """
        if not trigger_cctp_bridge:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "CCTP trigger not configured",
                    "hint": "loop.py must inject trigger_cctp_bridge into dashboard",
                },
            )
        if os.environ.get("CCTP_TRIGGER_ENABLED", "true").lower() in (
            "false",
            "0",
            "no",
        ):
            return JSONResponse(
                status_code=403,
                content={
                    "error": "CCTP trigger is disabled",
                    "hint": "set CCTP_TRIGGER_ENABLED=true in .env to enable",
                },
            )
        # Hard cap so a bad-faith request can't drain the wallet
        amount_usdc = max(0.1, min(amount_usdc, 5.0))
        try:
            result = await trigger_cctp_bridge(amount_usdc)
            return {"ok": True, "result": result.to_dict() if hasattr(result, "to_dict") else result}
        except Exception as e:
            logger.exception("CCTP trigger failed")
            return JSONResponse(
                status_code=500,
                content={"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"},
            )

    return app
