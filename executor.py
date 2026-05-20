"""
drip.executor
=============

Wraps the Hyperliquid SDK. Single entry point: execute(action).

Receives ActionOutput from decision.py, places the corresponding order
via the API wallet, and returns a result dict. Handles:
    - Market open (long/short)
    - Market close (when action.side == "close")
    - TP/SL placement after fill (optional, deferred to D4 for risk.py)
    - Time stops (managed by loop.py, not here)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

from reasoning import ActionOutput

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

HL_NETWORK = os.environ.get("HL_NETWORK", "testnet")
HL_SYMBOL = os.environ.get("HL_SYMBOL", "BTC")
HL_MASTER_ADDRESS = os.environ["HL_MASTER_ADDRESS"]
HL_API_WALLET_PK = os.environ["HL_API_WALLET_PK"]
SLIPPAGE = 0.01  # 1% max slippage on market orders


def _base_url() -> str:
    return (
        constants.TESTNET_API_URL
        if HL_NETWORK == "testnet"
        else constants.MAINNET_API_URL
    )


# ----------------------------------------------------------------------------
# Result type
# ----------------------------------------------------------------------------

@dataclass
class ExecutionResult:
    """Standardized result from any execution attempt."""
    success: bool
    action_taken: str  # "opened" | "closed" | "skipped"
    order_id: int | None = None
    fill_size: float | None = None
    fill_price: float | None = None
    raw: dict[str, Any] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "action_taken": self.action_taken,
            "order_id": self.order_id,
            "fill_size": self.fill_size,
            "fill_price": self.fill_price,
            "error": self.error,
            "raw": self.raw,
        }


# ----------------------------------------------------------------------------
# Executor
# ----------------------------------------------------------------------------

class HLExecutor:
    """Thin wrapper around hyperliquid SDK Exchange + Info."""

    def __init__(self) -> None:
        self._api_wallet = Account.from_key(HL_API_WALLET_PK)
        self._exchange = Exchange(
            wallet=self._api_wallet,
            base_url=_base_url(),
            account_address=HL_MASTER_ADDRESS,
        )
        self._info = Info(_base_url(), skip_ws=True)
        logger.info(
            "HLExecutor initialized: network=%s symbol=%s api_wallet=%s master=%s",
            HL_NETWORK,
            HL_SYMBOL,
            self._api_wallet.address,
            HL_MASTER_ADDRESS,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(self, action: ActionOutput) -> ExecutionResult:
        """
        Dispatch on action.side:
            "hold"  → skip
            "long"  → market_open buy
            "short" → market_open sell
            "close" → market_close any open position
        """
        if action.side == "hold":
            return ExecutionResult(
                success=True,
                action_taken="skipped",
                raw={"reason": action.hold_reason},
            )

        if action.side == "close":
            return self._close()

        # open long or short
        is_buy = action.side == "long"
        return self._open(is_buy=is_buy, size_btc=action.size_btc)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _open(self, is_buy: bool, size_btc: float) -> ExecutionResult:
        try:
            result = self._exchange.market_open(
                name=HL_SYMBOL,
                is_buy=is_buy,
                sz=size_btc,
                px=None,
                slippage=SLIPPAGE,
            )
        except Exception as e:
            logger.exception("market_open failed")
            return ExecutionResult(
                success=False, action_taken="opened", error=str(e)
            )

        return self._parse_result(result, "opened")

    def _close(self) -> ExecutionResult:
        try:
            result = self._exchange.market_close(HL_SYMBOL)
        except Exception as e:
            logger.exception("market_close failed")
            return ExecutionResult(
                success=False, action_taken="closed", error=str(e)
            )

        if result is None:
            # SDK returns None when there was nothing to close
            return ExecutionResult(
                success=True,
                action_taken="closed",
                raw={"note": "no open position to close"},
            )

        return self._parse_result(result, "closed")

    def _parse_result(self, result: dict[str, Any], action_taken: str) -> ExecutionResult:
        """Pull order_id and fill details out of the SDK response shape."""
        if not isinstance(result, dict):
            return ExecutionResult(
                success=False,
                action_taken=action_taken,
                error=f"unexpected result type: {type(result)}",
                raw={"raw": result},
            )

        status = result.get("status")
        if status != "ok":
            return ExecutionResult(
                success=False,
                action_taken=action_taken,
                error=f"status={status}",
                raw=result,
            )

        # Drill into response.data.statuses[0].filled
        try:
            statuses = result["response"]["data"]["statuses"]
            first = statuses[0]
            filled = first.get("filled")
            if filled is None:
                # Could be 'resting' for a limit order, but market orders fill
                resting = first.get("resting")
                return ExecutionResult(
                    success=True,
                    action_taken=action_taken,
                    raw=result,
                    error=f"order resting (not filled): {resting}" if resting else None,
                )
            return ExecutionResult(
                success=True,
                action_taken=action_taken,
                order_id=int(filled["oid"]),
                fill_size=float(filled["totalSz"]),
                fill_price=float(filled["avgPx"]),
                raw=result,
            )
        except (KeyError, IndexError, ValueError, TypeError) as e:
            return ExecutionResult(
                success=False,
                action_taken=action_taken,
                error=f"could not parse response: {e}",
                raw=result,
            )

    # ------------------------------------------------------------------
    # State queries (used by loop.py + risk.py)
    # ------------------------------------------------------------------

    def get_state(self) -> dict[str, Any]:
        """Fetch current account + position state from HL."""
        return self._info.user_state(HL_MASTER_ADDRESS)

    def get_mid_price(self, symbol: str = None) -> float:
        """Current mid price for a symbol (default HL_SYMBOL)."""
        sym = symbol or HL_SYMBOL
        all_mids = self._info.all_mids()
        return float(all_mids[sym])