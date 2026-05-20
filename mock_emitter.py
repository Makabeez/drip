"""
drip.mock_emitter
=================

Stand-in for AlphaDrip's emitter. Implements x402 v2 challenge-response
on GET /signals/latest, forwards verification + settlement to our
local facilitator, returns a synthetic cascade signal on success.

Mounted as a sub-app inside serve.py at /emitter/*.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any

import httpx
from fastapi import APIRouter, Header, Response
from fastapi.responses import JSONResponse

from cascade_sim import CascadeSimulator

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

SELLER_ADDRESS = os.environ["DRIP_SELLER_ADDRESS"]
ARC_USDC = os.environ.get("ARC_USDC", "0x3600000000000000000000000000000000000000")
ARC_CHAIN_ID = int(os.environ.get("ARC_CHAIN_ID", "5042002"))
SIGNAL_PRICE_MICROS = int(os.environ.get("SIGNAL_PRICE_MICROS", "1000"))
CADENCE_SECONDS = float(os.environ.get("EMITTER_CADENCE_SECONDS", "5"))

# Facilitator URL — for internal calls. We run them in the same process
# so localhost is the right address.
FACILITATOR_URL = os.environ.get(
    "DRIP_FACILITATOR_INTERNAL_URL",
    "http://localhost:8090/facilitator",
)

# Public-facing facilitator URL (returned to clients in the 402 challenge).
# For our local development this is the same; in production behind a tunnel
# it would be the public URL.
FACILITATOR_PUBLIC_URL = os.environ.get(
    "DRIP_FACILITATOR_URL",
    FACILITATOR_URL,
)


# ----------------------------------------------------------------------------
# Cascade simulator — singleton
# ----------------------------------------------------------------------------

simulator = CascadeSimulator(cadence_seconds=CADENCE_SECONDS)


# ----------------------------------------------------------------------------
# x402 challenge builder
# ----------------------------------------------------------------------------

def _build_challenge() -> str:
    """Build the x402 v2 payment-required header value (base64-encoded)."""
    challenge = {
        "x402Version": 2,
        "resource": {
            "url": "/signals/latest",
            "description": "Drip cascade signal (mock)",
            "mimeType": "application/json",
        },
        "accepts": [
            {
                "scheme": "exact",
                "network": f"eip155:{ARC_CHAIN_ID}",
                "asset": ARC_USDC,
                "amount": str(SIGNAL_PRICE_MICROS),
                "payTo": SELLER_ADDRESS,
                "maxTimeoutSeconds": 600,
                "facilitator": FACILITATOR_PUBLIC_URL,
                "extra": {
                    "name": "USDC",
                    "version": "2",
                    "verifyingContract": ARC_USDC,
                    "assetTransferMethod": "eip3009",
                },
            }
        ],
    }
    return base64.b64encode(
        json.dumps(challenge, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")


# ----------------------------------------------------------------------------
# Router
# ----------------------------------------------------------------------------

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, Any]:
    sig = simulator.peek()
    return {
        "ok": True,
        "seller": SELLER_ADDRESS,
        "price_micros": SIGNAL_PRICE_MICROS,
        "cadence_seconds": CADENCE_SECONDS,
        "current_signal": sig.as_dict() if sig else None,
    }


@router.get("/signals/latest")
async def signals_latest(
    x_payment: str | None = Header(default=None, alias="x-payment"),
):
    """
    Two-phase x402 endpoint:
        1. No x-payment header → return 402 + challenge
        2. With x-payment → verify + settle, return signal
    """
    # No payment → return challenge
    if x_payment is None:
        return Response(
            status_code=402,
            content="{}",
            media_type="application/json",
            headers={"payment-required": _build_challenge()},
        )

    # Decode the payment envelope
    try:
        envelope = json.loads(base64.b64decode(x_payment).decode("utf-8"))
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"error": f"could not decode x-payment header: {e}"},
        )

    # Forward to facilitator for verification
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            v_resp = await client.post(f"{FACILITATOR_URL}/verify", json=envelope)
            v_body = v_resp.json()
        except Exception as e:
            logger.exception("facilitator /verify call failed")
            return JSONResponse(
                status_code=500,
                content={"error": f"facilitator unreachable: {e}"},
            )

    if not v_body.get("isValid"):
        # Replay challenge — consumer must retry with a valid signature
        return Response(
            status_code=402,
            content=json.dumps({"error": v_body.get("invalidReason", "invalid payment")}),
            media_type="application/json",
            headers={"payment-required": _build_challenge()},
        )

    # Check there's actually a signal to deliver before settling.
    # If there's no signal, we don't want to charge the consumer.
    sig = simulator.consume()
    if sig is None:
        return Response(
            status_code=204,
            content="",
            media_type="application/json",
        )

    # Settle on-chain
    async with httpx.AsyncClient(timeout=45.0) as client:
        try:
            s_resp = await client.post(f"{FACILITATOR_URL}/settle", json=envelope)
            s_body = s_resp.json()
        except Exception as e:
            logger.exception("facilitator /settle call failed")
            return JSONResponse(
                status_code=500,
                content={"error": f"settlement failed: {e}"},
            )

    if not s_body.get("success"):
        return JSONResponse(
            status_code=502,
            content={"error": s_body.get("errorReason", "settlement failed")},
        )

    tx_hash = s_body.get("transaction", "")
    logger.info(
        "Settled signal %s %s conf=%.3f tx=%s",
        sig.symbol, sig.direction, sig.confidence, tx_hash,
    )

    # Return signal + payment proof
    return JSONResponse(
        status_code=200,
        content={
            "signal": sig.as_dict(),
            "tx_hash": tx_hash,
        },
        headers={"x-payment-response": tx_hash},
    )