"""
drip.decision
=============

Pure decision engine. Given a signal, portfolio state, and market state,
returns an Action plus a complete ReasoningTrace.

This module has no side effects. It's the brain — executor.py is the hands.

Decision rules (locked for v1):
    - Skip if confidence < CONF_THRESHOLD (0.60)
    - Skip if daily kill switch tripped
    - Skip if position already open same direction (pyramid blocked)
    - Close + wait if position open opposite direction
    - Position size: fractional Kelly, capped at MAX_POSITION_PCT of NAV
    - Leverage: inversely scaled with vol_ratio
    - TP: +0.12% from entry
    - SL: -0.08% from entry
    - Time stop: 60s
"""

from __future__ import annotations

import logging
import os
from typing import Any

from reasoning import (
    ActionOutput,
    MarketInput,
    PortfolioInput,
    ReasoningTrace,
    SignalInput,
)
from signal_client import Signal

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Config (locked from .env or hardcoded defaults)
# ----------------------------------------------------------------------------

CONF_THRESHOLD = float(os.environ.get("CONF_THRESHOLD", "0.60"))
RISK_TP_PCT = float(os.environ.get("RISK_TP_PCT", "0.0012"))
RISK_SL_PCT = float(os.environ.get("RISK_SL_PCT", "0.0008"))
RISK_TIME_STOP_SECONDS = int(os.environ.get("RISK_TIME_STOP_SECONDS", "60"))
RISK_MAX_LEVERAGE = float(os.environ.get("RISK_MAX_LEVERAGE", "5"))
RISK_KELLY_FRACTION = float(os.environ.get("RISK_KELLY_FRACTION", "0.25"))
RISK_MAX_POSITION_PCT = float(os.environ.get("RISK_MAX_POSITION_PCT", "0.05"))


# ----------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------

def decide(
    signal: Signal,
    portfolio: dict[str, Any],
    market: dict[str, Any],
    kill_switch_tripped: bool = False,
) -> ReasoningTrace:
    """
    Make a trading decision. Returns a frozen ReasoningTrace with the action
    baked in.

    portfolio dict expected keys (from HL info.user_state):
        account_value, free_margin, margin_used, open_position_side,
        open_position_size_btc, open_position_entry_px,
        open_position_unrealized_pnl, daily_pnl, seconds_since_last_trade

    market dict expected keys (from HL info.all_mids and orderbook):
        mid_px, bid_px, ask_px, spread_bps, realized_vol_1h_pct,
        funding_rate_8h_pct
    """
    trace = ReasoningTrace()

    # Capture inputs
    trace.signal = SignalInput(
        symbol=signal.symbol,
        direction=signal.direction,
        confidence=signal.confidence,
        vol_ratio=signal.vol_ratio,
        timestamp_ms=signal.timestamp_ms,
        payment_tx_hash=signal.tx_hash,
    )
    trace.portfolio = PortfolioInput(**portfolio)
    trace.market = MarketInput(**market)

    # ---- Rule 1: kill switch ----
    trace.step(
        rule="daily_kill_switch",
        predicate="daily_pnl > -2% NAV",
        evaluated_to=not kill_switch_tripped,
        value_observed=portfolio["daily_pnl_usd"],
    )
    if kill_switch_tripped:
        trace.set_action(_hold(reason="daily_kill_switch"))
        trace.freeze()
        return trace

    # ---- Rule 2: confidence threshold ----
    passes_conf = signal.confidence >= CONF_THRESHOLD
    trace.step(
        rule="confidence_threshold",
        predicate=f"signal.confidence >= {CONF_THRESHOLD}",
        evaluated_to=passes_conf,
        value_observed=signal.confidence,
    )
    if not passes_conf:
        trace.set_action(_hold(reason="low_confidence"))
        trace.freeze()
        return trace

    # ---- Rule 3: existing position handling ----
    pos_side = portfolio["open_position_side"]
    if pos_side is not None:
        same_direction = pos_side == signal.direction
        trace.step(
            rule="position_direction_match",
            predicate=f"open_position_side == signal.direction",
            evaluated_to=same_direction,
            value_observed=f"{pos_side} vs {signal.direction}",
        )
        if same_direction:
            # Pyramid blocked
            trace.set_action(_hold(reason="same_direction_open"))
            trace.freeze()
            return trace
        else:
            # Close existing, wait one signal before re-entering
            trace.set_action(
                ActionOutput(
                    side="close",
                    size_usd=0.0,
                    size_btc=portfolio["open_position_size_btc"],
                    leverage=1.0,
                    tp_px=None,
                    sl_px=None,
                    time_stop_s=None,
                    hold_reason=None,
                )
            )
            trace.freeze()
            return trace

    # ---- Rule 4: position sizing ----
    account_value = portfolio["account_value_usd"]
    edge = signal.confidence - 0.5  # rough edge proxy
    # Variance approximation: vol_ratio scales realized vol
    variance = max(0.001, market["realized_vol_1h_pct"] * signal.vol_ratio)
    kelly_pct = RISK_KELLY_FRACTION * edge / variance
    sized_pct = min(kelly_pct, RISK_MAX_POSITION_PCT)
    size_usd = max(11.0, account_value * sized_pct)  # min $11 above HL's $10 floor

    trace.step(
        rule="position_sizing",
        predicate=f"size = min(Kelly * NAV, {RISK_MAX_POSITION_PCT * 100}% NAV)",
        evaluated_to=True,
        value_observed=round(size_usd, 2),
        notes=f"kelly_pct={kelly_pct:.4f}, sized_pct={sized_pct:.4f}",
    )

    # ---- Rule 5: leverage ----
    # Lower leverage when vol is high
    base_lev = 3.0
    vol_adj = max(0.5, min(2.0, 1.0 / signal.vol_ratio))
    leverage = min(RISK_MAX_LEVERAGE, base_lev * vol_adj)
    trace.step(
        rule="leverage_scaling",
        predicate=f"lev = min(max_lev, base_lev / vol_ratio)",
        evaluated_to=True,
        value_observed=round(leverage, 2),
        notes=f"vol_ratio={signal.vol_ratio:.2f}, vol_adj={vol_adj:.2f}",
    )

    # ---- Build the action ----
    mid_px = market["mid_px"]
    size_btc = round(size_usd / mid_px, 5)
    if size_btc < 0.00012:  # HL min for BTC is around $10 / px
        trace.step(
            rule="min_notional_check",
            predicate="size_btc >= HL min",
            evaluated_to=False,
            value_observed=size_btc,
        )
        trace.set_action(_hold(reason="min_notional"))
        trace.freeze()
        return trace

    is_long = signal.direction == "long"
    tp_px = mid_px * (1 + RISK_TP_PCT) if is_long else mid_px * (1 - RISK_TP_PCT)
    sl_px = mid_px * (1 - RISK_SL_PCT) if is_long else mid_px * (1 + RISK_SL_PCT)

    action = ActionOutput(
        side="long" if is_long else "short",
        size_usd=round(size_usd, 2),
        size_btc=size_btc,
        leverage=round(leverage, 2),
        tp_px=round(tp_px, 1),
        sl_px=round(sl_px, 1),
        time_stop_s=RISK_TIME_STOP_SECONDS,
        hold_reason=None,
    )
    trace.set_action(action)
    trace.freeze()
    return trace


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def _hold(reason: str) -> ActionOutput:
    """Build a hold action with a specific reason."""
    return ActionOutput(
        side="hold",
        size_usd=0.0,
        size_btc=0.0,
        leverage=0.0,
        tp_px=None,
        sl_px=None,
        time_stop_s=None,
        hold_reason=reason,  # type: ignore[arg-type]
    )