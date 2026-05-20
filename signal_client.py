"""
drip.signal_client
==================

x402 v2 consumer for the AlphaDrip emitter.

Flow:
    1. GET /signals/latest
    2. Receive 402 + payment-required header (base64-encoded JSON challenge)
    3. Decode challenge, find eip155:5042002 (Arc Testnet) accept
    4. Sign EIP-3009 transferWithAuthorization
    5. POST same path with x-payment header (base64-encoded signed envelope)
    6. Receive 200 + signal body

x402 v2 spec reference:
    https://www.x402.org/spec
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
from eth_account import Account
from eth_account.messages import encode_typed_data

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

EMITTER_URL = os.environ.get("ALPHADRIP_EMITTER", "https://alphadrip.baserep.xyz")
SIGNAL_PATH = "/signals/latest"

# CAIP-2 network identifier for Arc Testnet
ARC_NETWORK_CAIP = "eip155:5042002"
ARC_CHAIN_ID = 5042002


# ----------------------------------------------------------------------------
# Data classes
# ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Signal:
    """A cascade signal received from the AlphaDrip emitter."""
    symbol: str
    direction: str  # "long" | "short"
    confidence: float
    vol_ratio: float
    timestamp_ms: int
    tx_hash: str  # on-chain payment proof (if returned by facilitator)
    raw: dict[str, Any]


class SignalClientError(Exception):
    """Raised on any unexpected emitter response."""


# ----------------------------------------------------------------------------
# Client
# ----------------------------------------------------------------------------

class SignalClient:
    """
    Subscribes to the AlphaDrip emitter and pays per signal via EIP-3009.

    Usage:
        client = SignalClient(consumer_private_key=os.environ["CONSUMER_PK"])
        signal = await client.fetch_signal()
        if signal and signal.confidence > 0.6:
            ...
    """

    def __init__(
        self,
        consumer_private_key: str,
        emitter_url: str = EMITTER_URL,
        timeout: float = 10.0,
    ) -> None:
        if not consumer_private_key:
            raise ValueError("consumer_private_key is required")
        self._account = Account.from_key(consumer_private_key)
        self._emitter_url = emitter_url.rstrip("/")
        self._timeout = timeout
        self._http = httpx.AsyncClient(timeout=timeout)

    @property
    def consumer_address(self) -> str:
        return self._account.address

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch_signal(self) -> Signal | None:
        """
        Fetch one signal. Returns None if no cascade is firing.
        Pays $0.003 USDC on Arc Testnet via EIP-3009.
        """
        probe_url = f"{self._emitter_url}{SIGNAL_PATH}"

        # Step 1: probe
        probe = await self._http.get(probe_url)

        if probe.status_code == 204:
            # No signal currently active
            return None

        if probe.status_code != 402:
            raise SignalClientError(
                f"Expected 402, got {probe.status_code}: {probe.text}"
            )

        # Step 2: extract challenge from payment-required header
        challenge_b64 = probe.headers.get("payment-required")
        if not challenge_b64:
            raise SignalClientError(
                "402 returned but no payment-required header present"
            )

        try:
            challenge = json.loads(base64.b64decode(challenge_b64).decode("utf-8"))
        except Exception as e:
            raise SignalClientError(f"Could not decode challenge: {e}") from e

        if challenge.get("x402Version") != 2:
            raise SignalClientError(
                f"Unsupported x402 version: {challenge.get('x402Version')}"
            )

        # Step 3: find Arc Testnet accept
        accepts = challenge.get("accepts", [])
        arc_accept = next(
            (a for a in accepts if a.get("network") == ARC_NETWORK_CAIP),
            None,
        )
        if arc_accept is None:
            networks = [a.get("network") for a in accepts]
            raise SignalClientError(
                f"No Arc Testnet ({ARC_NETWORK_CAIP}) option. "
                f"Available: {networks}"
            )

        # Step 4: sign EIP-3009 authorization
        x_payment_header = self._build_payment_header(arc_accept)

        # Step 5: GET again with x-payment header (x402 v2 retries same method)
        paid = await self._http.get(
            probe_url,
            headers={"x-payment": x_payment_header},
        )

        if paid.status_code != 200:
            raise SignalClientError(
                f"Payment rejected, status {paid.status_code}: {paid.text}"
            )

        # Step 6: parse signal
        body = paid.json()
        return self._parse_signal_response(body, paid.headers)

    # ------------------------------------------------------------------
    # Internal — EIP-3009 signing
    # ------------------------------------------------------------------

    def _build_payment_header(self, accept: dict[str, Any]) -> str:
        """
        Build the x-payment header per x402 v2 spec.

        Wire shape (base64-encoded JSON):
            {
              "x402Version": 2,
              "scheme": "exact",
              "network": "eip155:5042002",
              "payload": {
                "signature": "0x...",
                "authorization": {
                  "from": "0x...",
                  "to": "0x...",
                  "value": "3000",
                  "validAfter": "0",
                  "validBefore": "...",
                  "nonce": "0x..."
                }
              }
            }
        """
        extra = accept.get("extra", {})
        usdc_addr = extra.get("verifyingContract", accept["asset"])
        usdc_name = extra.get("name", "USDC")
        usdc_version = extra.get("version", "2")
        seller_addr = accept["payTo"]
        amount = accept["amount"]  # string of integer micros
        max_timeout_s = int(accept.get("maxTimeoutSeconds", 300))

        now_s = int(time.time())
        valid_after = 0
        # Bound validBefore to maxTimeoutSeconds from the accept,
        # but clip at 1 hour for safety so abandoned signed authorizations
        # don't sit around indefinitely waiting to be replayed.
        valid_before = now_s + min(max_timeout_s, 3600)
        nonce_bytes = os.urandom(32)
        nonce_hex = "0x" + nonce_bytes.hex()

        typed_data = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "TransferWithAuthorization": [
                    {"name": "from", "type": "address"},
                    {"name": "to", "type": "address"},
                    {"name": "value", "type": "uint256"},
                    {"name": "validAfter", "type": "uint256"},
                    {"name": "validBefore", "type": "uint256"},
                    {"name": "nonce", "type": "bytes32"},
                ],
            },
            "primaryType": "TransferWithAuthorization",
            "domain": {
                "name": usdc_name,
                "version": usdc_version,
                "chainId": ARC_CHAIN_ID,
                "verifyingContract": usdc_addr,
            },
            "message": {
                "from": self._account.address,
                "to": seller_addr,
                "value": int(amount),
                "validAfter": valid_after,
                "validBefore": valid_before,
                "nonce": nonce_bytes,
            },
        }

        encoded = encode_typed_data(full_message=typed_data)
        signed = self._account.sign_message(encoded)

        # x402 v2 envelope
        envelope = {
            "x402Version": 2,
            "scheme": "exact",
            "network": ARC_NETWORK_CAIP,
            "payload": {
                "signature": "0x" + signed.signature.hex(),
                "authorization": {
                    "from": self._account.address,
                    "to": seller_addr,
                    "value": str(int(amount)),
                    "validAfter": str(valid_after),
                    "validBefore": str(valid_before),
                    "nonce": nonce_hex,
                },
            },
        }

        return base64.b64encode(
            json.dumps(envelope, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")

    # ------------------------------------------------------------------
    # Internal — response parsing
    # ------------------------------------------------------------------

    def _parse_signal_response(
        self,
        body: dict[str, Any],
        headers: httpx.Headers,
    ) -> Signal:
        """
        Defensive parser — the emitter response shape isn't fully known
        until we run live. We try several common shapes and fall back
        to logging the raw body for inspection.
        """
        # Try common shapes:
        # 1. { signal: {...}, tx_hash: "..." }
        # 2. { data: {...} } at root
        # 3. flat { symbol, direction, ... }
        sig_payload = (
            body.get("signal")
            or body.get("data")
            or body  # flat
        )

        # tx_hash may be in body or in x-payment-response header
        tx_hash = (
            body.get("tx_hash")
            or body.get("txHash")
            or headers.get("x-payment-response", "")
            or ""
        )

        try:
            return Signal(
                symbol=str(sig_payload.get("symbol", "")),
                direction=str(sig_payload.get("direction", "")),
                confidence=float(sig_payload.get("confidence", 0.0)),
                vol_ratio=float(sig_payload.get("vol_ratio", 0.0)),
                timestamp_ms=int(sig_payload.get("timestamp_ms", 0)),
                tx_hash=str(tx_hash),
                raw=body,
            )
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("Unexpected signal shape, raw body: %s", body)
            raise SignalClientError(
                f"Could not parse signal response: {e}. Raw: {body}"
            ) from e


# ----------------------------------------------------------------------------
# Smoke test (run as script)
# ----------------------------------------------------------------------------

if __name__ == "__main__":
    import asyncio
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO)

    async def main() -> None:
        pk = os.environ.get("CONSUMER_PK")
        if not pk:
            print("Set CONSUMER_PK env var to run smoke test.")
            return

        client = SignalClient(consumer_private_key=pk)
        try:
            print(f"Consumer address: {client.consumer_address}")
            print(f"Emitter:          {client._emitter_url}")
            print(f"Fetching signal...")
            signal = await client.fetch_signal()
            if signal is None:
                print("No active cascade — emitter returned 204")
            else:
                print(f"\nSignal received:")
                print(f"  symbol:     {signal.symbol}")
                print(f"  direction:  {signal.direction}")
                print(f"  confidence: {signal.confidence}")
                print(f"  vol_ratio:  {signal.vol_ratio}")
                print(f"  tx_hash:    {signal.tx_hash}")
                print(f"  raw:        {signal.raw}")
        finally:
            await client.close()

    asyncio.run(main())