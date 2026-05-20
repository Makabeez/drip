"""
drip.cascade_sim
================

Synthetic BTC cascade signal generator. Stand-in for AlphaDrip's real
cascade engine while we develop locally.

Generates realistic-looking signals:
    - Alternating-with-streak direction (real cascades cluster)
    - Confidence biased between 0.55 and 0.92
    - vol_ratio that reflects "cascade strength"
    - Sub-second time bursts: when a cascade fires, 2-4 signals come close together

The shape of the output matches what we expect from the real AlphaDrip
emitter — same fields, same types — so signal_client.py will work
unchanged against either.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SimSignal:
    symbol: str
    direction: str  # "long" | "short"
    confidence: float
    vol_ratio: float
    timestamp_ms: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "confidence": round(self.confidence, 4),
            "vol_ratio": round(self.vol_ratio, 3),
            "timestamp_ms": self.timestamp_ms,
        }


class CascadeSimulator:
    """
    Produces a new signal at most every `cadence_seconds`. Holds the
    "current" signal in memory; the emitter route returns it when polled.
    A signal expires `signal_ttl_seconds` after generation (so the emitter
    returns 204 if nobody pays for it in time).
    """

    def __init__(
        self,
        cadence_seconds: float = 5.0,
        signal_ttl_seconds: float = 8.0,
        symbol: str = "BTC",
    ) -> None:
        self.cadence_seconds = cadence_seconds
        self.signal_ttl_seconds = signal_ttl_seconds
        self.symbol = symbol

        # State
        self._current: SimSignal | None = None
        self._current_expires_at: float = 0.0
        self._last_direction: str = "long"
        self._streak: int = 0  # how many signals in current direction

        # Task control
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run())
            logger.info(
                "Cascade simulator started: cadence=%.1fs ttl=%.1fs",
                self.cadence_seconds,
                self.signal_ttl_seconds,
            )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Cascade simulator stopped")

    # ------------------------------------------------------------------
    # Public API used by the emitter route
    # ------------------------------------------------------------------

    def peek(self) -> SimSignal | None:
        """Get the current signal if it hasn't expired. Does not consume."""
        if self._current is None:
            return None
        if time.time() > self._current_expires_at:
            return None
        return self._current

    def consume(self) -> SimSignal | None:
        """Get and clear the current signal (one-shot delivery semantics)."""
        sig = self.peek()
        if sig is not None:
            self._current = None
            self._current_expires_at = 0
        return sig

    # ------------------------------------------------------------------
    # Internal — generator loop
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.cadence_seconds)
            if self._stop.is_set():
                break
            sig = self._generate()
            self._current = sig
            self._current_expires_at = time.time() + self.signal_ttl_seconds
            logger.info(
                "New signal: %s %s conf=%.3f vol_ratio=%.2f",
                sig.symbol,
                sig.direction,
                sig.confidence,
                sig.vol_ratio,
            )

    def _generate(self) -> SimSignal:
        """Build a single realistic-looking signal."""
        # Direction logic: 60% same as last, 40% flip.
        # Within a streak, confidence builds; on a flip, confidence resets lower.
        if random.random() < 0.60 and self._streak < 4:
            direction = self._last_direction
            self._streak += 1
            # Confidence grows with streak
            confidence = min(0.92, 0.62 + 0.06 * self._streak + random.uniform(-0.04, 0.06))
        else:
            direction = "short" if self._last_direction == "long" else "long"
            self._streak = 1
            # Fresh-direction signals start cautious
            confidence = 0.58 + random.uniform(-0.03, 0.08)

        # vol_ratio: cascade intensity. Higher when streak is long.
        vol_ratio = 1.05 + 0.08 * self._streak + random.uniform(-0.10, 0.20)
        vol_ratio = max(0.85, min(2.2, vol_ratio))

        self._last_direction = direction

        return SimSignal(
            symbol=self.symbol,
            direction=direction,
            confidence=confidence,
            vol_ratio=vol_ratio,
            timestamp_ms=int(time.time() * 1000),
        )