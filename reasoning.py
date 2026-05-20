"""
drip.reasoning
==============

Structured reasoning trace for every agent decision.

Why this exists
---------------
Most "AI trading agents" make decisions inside an LLM call and lose the
provenance. Drip's decision engine is pure Python heuristics, so the trace
is deterministic — given the same inputs we re-derive the same output.

The trace is:
    · Hashable (sha256 over canonicalized JSON) → on-chain commitment possible
    · Replayable (re-running the engine on stored inputs reproduces the action)
    · Composable (each decision references prior position state hash)

This hooks Agora research item #1 ("reasoning marketplace") for innovation
points, and gives judges a transparency story competitors won't have.

Storage
-------
Each trace is written to SQLite (table: traces) immediately after the
HL order is submitted. Optional D11 stretch: bundle daily traces, hash
the bundle, post the bundle hash to Arc via a one-tx commitment.

Schema versioning
-----------------
Bumped via REASONING_TRACE_SCHEMA_VERSION env var. Old traces stay readable
because each row stores its own schema_version.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

SCHEMA_VERSION = int(os.environ.get("REASONING_TRACE_SCHEMA_VERSION", "1"))
HASH_ALGO = os.environ.get("REASONING_TRACE_HASH_ALGO", "sha256")

Side = Literal["long", "short", "close", "hold"]
HoldReason = Literal[
    "low_confidence",
    "same_direction_open",
    "opposite_direction_open_waiting",
    "daily_kill_switch",
    "liquidation_protection",
    "min_notional",
    "leverage_cap",
    "cooldown",
    "manual_halt",
]


# ----------------------------------------------------------------------------
# Inputs (everything the decision engine read)
# ----------------------------------------------------------------------------

@dataclass(frozen=True)
class SignalInput:
    """The signal that triggered the decision."""
    symbol: str
    direction: str  # "long" | "short"
    confidence: float
    vol_ratio: float
    timestamp_ms: int
    payment_tx_hash: str  # ties this trace to the Arc tx that paid for the signal


@dataclass(frozen=True)
class PortfolioInput:
    """Account state at decision time."""
    account_value_usd: float
    free_margin_usd: float
    margin_used_usd: float
    open_position_side: Side | None  # None if flat
    open_position_size_btc: float
    open_position_entry_px: float
    open_position_unrealized_pnl_usd: float
    daily_pnl_usd: float
    seconds_since_last_trade: float


@dataclass(frozen=True)
class MarketInput:
    """Market context at decision time."""
    mid_px: float
    bid_px: float
    ask_px: float
    spread_bps: float
    realized_vol_1h_pct: float
    funding_rate_8h_pct: float


# ----------------------------------------------------------------------------
# Output (what the engine decided + why)
# ----------------------------------------------------------------------------

@dataclass(frozen=True)
class ActionOutput:
    """The action the engine emitted."""
    side: Side
    size_usd: float
    size_btc: float
    leverage: float
    tp_px: float | None
    sl_px: float | None
    time_stop_s: int | None
    hold_reason: HoldReason | None  # set iff side == "hold"


# ----------------------------------------------------------------------------
# Reasoning steps (the heuristic chain)
# ----------------------------------------------------------------------------

@dataclass(frozen=True)
class ReasoningStep:
    """
    One predicate evaluation in the decision chain.

    Example:
        rule="confidence_threshold"
        predicate="signal.confidence >= 0.6"
        evaluated_to=True
        value_observed=0.78
    """
    rule: str
    predicate: str
    evaluated_to: bool
    value_observed: float | str | bool
    notes: str = ""


# ----------------------------------------------------------------------------
# Full trace
# ----------------------------------------------------------------------------

@dataclass
class ReasoningTrace:
    """
    One full decision trace. Built incrementally by the decision engine,
    finalized and hashed before the executor places the order.
    """

    trace_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    schema_version: int = SCHEMA_VERSION
    created_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))

    # inputs
    signal: SignalInput | None = None
    portfolio: PortfolioInput | None = None
    market: MarketInput | None = None

    # the chain of evaluated rules, in order
    steps: list[ReasoningStep] = field(default_factory=list)

    # output
    action: ActionOutput | None = None

    # filled after the executor returns
    execution_result: dict[str, Any] | None = None  # HL response, fill data
    parent_trace_hash: str | None = None  # chains decisions together

    # finalized hash, computed on freeze()
    trace_hash: str | None = None

    # ------------------------------------------------------------------
    # Builder helpers (called by decision engine)
    # ------------------------------------------------------------------

    def step(
        self,
        rule: str,
        predicate: str,
        evaluated_to: bool,
        value_observed: float | str | bool,
        notes: str = "",
    ) -> ReasoningTrace:
        """Add a reasoning step. Returns self for chaining."""
        self.steps.append(
            ReasoningStep(
                rule=rule,
                predicate=predicate,
                evaluated_to=evaluated_to,
                value_observed=value_observed,
                notes=notes,
            )
        )
        return self

    def set_action(self, action: ActionOutput) -> ReasoningTrace:
        self.action = action
        return self

    def set_execution_result(self, result: dict[str, Any]) -> ReasoningTrace:
        self.execution_result = result
        return self

    # ------------------------------------------------------------------
    # Freezing / hashing
    # ------------------------------------------------------------------

    def freeze(self) -> str:
        """
        Compute trace_hash over canonicalized JSON of all fields except
        trace_hash itself. Returns the hash. Idempotent.
        """
        if self.trace_hash is not None:
            return self.trace_hash

        payload = self._canonical_dict()
        payload.pop("trace_hash", None)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))

        h = hashlib.new(HASH_ALGO)
        h.update(canonical.encode("utf-8"))
        self.trace_hash = h.hexdigest()
        return self.trace_hash

    def _canonical_dict(self) -> dict[str, Any]:
        """Deep-convert to plain dicts for stable JSON encoding."""
        return {
            "trace_id": self.trace_id,
            "schema_version": self.schema_version,
            "created_at_ms": self.created_at_ms,
            "signal": asdict(self.signal) if self.signal else None,
            "portfolio": asdict(self.portfolio) if self.portfolio else None,
            "market": asdict(self.market) if self.market else None,
            "steps": [asdict(s) for s in self.steps],
            "action": asdict(self.action) if self.action else None,
            "execution_result": self.execution_result,
            "parent_trace_hash": self.parent_trace_hash,
            "trace_hash": self.trace_hash,
        }

    def to_json(self) -> str:
        """Serialize for SQLite storage or Telegram debug dump."""
        return json.dumps(self._canonical_dict(), separators=(",", ":"))

    def to_telegram_summary(self) -> str:
        """One-line human-readable summary for Makaclaw alerts."""
        if self.action is None:
            return f"[trace {self.trace_id[:8]}] incomplete"

        a = self.action
        if a.side == "hold":
            return (
                f"[{self.trace_id[:8]}] HOLD "
                f"reason={a.hold_reason} steps={len(self.steps)}"
            )
        return (
            f"[{self.trace_id[:8]}] {a.side.upper()} "
            f"${a.size_usd:.2f} @ {a.leverage:.1f}x "
            f"TP={a.tp_px} SL={a.sl_px} steps={len(self.steps)}"
        )


# ----------------------------------------------------------------------------
# SQLite persistence helpers
# ----------------------------------------------------------------------------

TRACE_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS traces (
    trace_id TEXT PRIMARY KEY,
    parent_trace_hash TEXT,
    trace_hash TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL,
    side TEXT,
    size_usd REAL,
    payment_tx_hash TEXT,
    json_blob TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_traces_created ON traces (created_at_ms DESC);
CREATE INDEX IF NOT EXISTS idx_traces_parent ON traces (parent_trace_hash);
"""


def persist_trace(conn: Any, trace: ReasoningTrace) -> None:
    """Write a frozen trace to SQLite. Conn is sqlite3.Connection."""
    trace.freeze()
    a = trace.action
    s = trace.signal
    conn.execute(
        """
        INSERT INTO traces (
            trace_id, parent_trace_hash, trace_hash, schema_version,
            created_at_ms, side, size_usd, payment_tx_hash, json_blob
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trace.trace_id,
            trace.parent_trace_hash,
            trace.trace_hash,
            trace.schema_version,
            trace.created_at_ms,
            a.side if a else None,
            a.size_usd if a else None,
            s.payment_tx_hash if s else None,
            trace.to_json(),
        ),
    )
    conn.commit()
