"""
drip.facilitator
================

Self-hosted x402 facilitator for Arc Testnet.

Exposes two endpoints per x402 v2 spec:
    POST /verify  — check signature validity without submitting
    POST /settle  — submit transferWithAuthorization on-chain, return tx hash

The facilitator holds the private key of the wallet that pays gas to submit
the EIP-3009 authorization. The consumer's signature authorizes the
facilitator to move USDC from consumer → seller; the facilitator pays the
gas in USDC (Arc uses USDC as native gas).

This is mounted as a sub-app inside serve.py at /facilitator/*
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from eth_account import Account
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from web3 import Web3

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

ARC_RPC = os.environ.get("ARC_RPC", "https://rpc.testnet.arc.network")
ARC_USDC = os.environ.get("ARC_USDC", "0x3600000000000000000000000000000000000000")
ARC_CHAIN_ID = int(os.environ.get("ARC_CHAIN_ID", "5042002"))
FACILITATOR_PK = os.environ["DRIP_SELLER_PK"]  # same wallet as seller

# ----------------------------------------------------------------------------
# Web3 + signer
# ----------------------------------------------------------------------------

w3 = Web3(Web3.HTTPProvider(ARC_RPC))
facilitator_account = Account.from_key(FACILITATOR_PK)

# Minimal USDC ABI — only what we need
USDC_ABI = [
    # transferWithAuthorization with (v, r, s) — older Circle USDC ABI
    {
        "inputs": [
            {"name": "from", "type": "address"},
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "validAfter", "type": "uint256"},
            {"name": "validBefore", "type": "uint256"},
            {"name": "nonce", "type": "bytes32"},
            {"name": "v", "type": "uint8"},
            {"name": "r", "type": "bytes32"},
            {"name": "s", "type": "bytes32"},
        ],
        "name": "transferWithAuthorization",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    # transferWithAuthorization with raw signature bytes — newer ABI
    {
        "inputs": [
            {"name": "from", "type": "address"},
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "validAfter", "type": "uint256"},
            {"name": "validBefore", "type": "uint256"},
            {"name": "nonce", "type": "bytes32"},
            {"name": "signature", "type": "bytes"},
        ],
        "name": "transferWithAuthorization",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "a", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

usdc = w3.eth.contract(
    address=Web3.to_checksum_address(ARC_USDC),
    abi=USDC_ABI,
)

# Lock around nonce management — Arc nonce is sequential, so concurrent
# settlements must serialize through this lock to avoid `nonce too low`.
_nonce_lock = asyncio.Lock()


# ----------------------------------------------------------------------------
# Pydantic models — x402 v2 payload shapes
# ----------------------------------------------------------------------------

class Authorization(BaseModel):
    from_: str  # "from" is a Python keyword
    to: str
    value: str  # string of integer micros
    validAfter: str
    validBefore: str
    nonce: str  # 0x-prefixed bytes32

    class Config:
        fields = {"from_": "from"}
        populate_by_name = True


class Payload(BaseModel):
    signature: str  # 0x-prefixed 65-byte sig
    authorization: dict[str, Any]  # validated manually due to "from" keyword clash


class VerifyRequest(BaseModel):
    x402Version: int
    scheme: str
    network: str
    payload: Payload


class SettleRequest(VerifyRequest):
    pass


class VerifyResponse(BaseModel):
    isValid: bool
    invalidReason: str | None = None
    payer: str


class SettleResponse(BaseModel):
    success: bool
    transaction: str | None = None
    network: str
    errorReason: str | None = None
    payer: str


# ----------------------------------------------------------------------------
# Validation helpers
# ----------------------------------------------------------------------------

def _validate_basic(req: VerifyRequest) -> tuple[dict[str, Any], str | None]:
    """Static checks that don't need on-chain calls. Returns (auth_dict, error)."""
    if req.x402Version != 2:
        return {}, f"unsupported x402 version: {req.x402Version}"
    if req.scheme != "exact":
        return {}, f"unsupported scheme: {req.scheme}"
    if req.network != f"eip155:{ARC_CHAIN_ID}":
        return {}, f"unsupported network: {req.network}"

    auth = req.payload.authorization
    required = {"from", "to", "value", "validAfter", "validBefore", "nonce"}
    if not required.issubset(auth.keys()):
        missing = required - auth.keys()
        return {}, f"authorization missing fields: {missing}"

    now = int(time.time())
    valid_before = int(auth["validBefore"])
    valid_after = int(auth["validAfter"])
    if now < valid_after:
        return {}, "authorization not yet valid"
    if now >= valid_before:
        return {}, "authorization expired"

    return auth, None


# ----------------------------------------------------------------------------
# Router
# ----------------------------------------------------------------------------

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, Any]:
    try:
        bal = usdc.functions.balanceOf(facilitator_account.address).call()
        chain_id = w3.eth.chain_id
        return {
            "ok": True,
            "facilitator": facilitator_account.address,
            "chain_id": chain_id,
            "usdc_balance": bal / 1e6,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/verify", response_model=VerifyResponse)
async def verify(req: VerifyRequest) -> VerifyResponse:
    """Validate the signature without submitting on-chain."""
    auth, err = _validate_basic(req)
    if err:
        return VerifyResponse(
            isValid=False,
            invalidReason=err,
            payer=auth.get("from", ""),
        )

    # Verify signature recovers to auth.from
    try:
        recovered = _recover_signer(req, auth)
    except Exception as e:
        return VerifyResponse(
            isValid=False,
            invalidReason=f"signature decode error: {e}",
            payer=auth["from"],
        )

    if recovered.lower() != auth["from"].lower():
        return VerifyResponse(
            isValid=False,
            invalidReason=f"signature mismatch: recovered {recovered}, expected {auth['from']}",
            payer=auth["from"],
        )

    return VerifyResponse(isValid=True, payer=auth["from"])


@router.post("/settle", response_model=SettleResponse)
async def settle(req: SettleRequest) -> SettleResponse:
    """Submit the authorization on-chain. Returns tx hash on success."""
    auth, err = _validate_basic(req)
    if err:
        return SettleResponse(
            success=False, network=req.network, errorReason=err, payer=auth.get("from", "")
        )

    # Decode signature into (v, r, s)
    try:
        sig_hex = req.payload.signature
        if sig_hex.startswith("0x"):
            sig_hex = sig_hex[2:]
        sig_bytes = bytes.fromhex(sig_hex)
        if len(sig_bytes) != 65:
            raise ValueError(f"expected 65-byte sig, got {len(sig_bytes)}")
        r = sig_bytes[0:32]
        s = sig_bytes[32:64]
        v = sig_bytes[64]
        if v < 27:
            v += 27
    except Exception as e:
        return SettleResponse(
            success=False,
            network=req.network,
            errorReason=f"signature decode error: {e}",
            payer=auth["from"],
        )

    # Build transferWithAuthorization call. Try (v,r,s) first; if that
    # contract doesn't have it, fall back to raw signature bytes.
    async with _nonce_lock:
        nonce = w3.eth.get_transaction_count(facilitator_account.address)
        gas_price = w3.eth.gas_price

        try:
            # Build tx
            try:
                fn = usdc.functions.transferWithAuthorization(
                    Web3.to_checksum_address(auth["from"]),
                    Web3.to_checksum_address(auth["to"]),
                    int(auth["value"]),
                    int(auth["validAfter"]),
                    int(auth["validBefore"]),
                    bytes.fromhex(auth["nonce"][2:] if auth["nonce"].startswith("0x") else auth["nonce"]),
                    v,
                    r,
                    s,
                )
                tx = fn.build_transaction({
                    "from": facilitator_account.address,
                    "nonce": nonce,
                    "gas": 200000,
                    "gasPrice": gas_price,
                    "chainId": ARC_CHAIN_ID,
                })
            except Exception:
                # Try raw-signature variant
                fn = usdc.functions.transferWithAuthorization(
                    Web3.to_checksum_address(auth["from"]),
                    Web3.to_checksum_address(auth["to"]),
                    int(auth["value"]),
                    int(auth["validAfter"]),
                    int(auth["validBefore"]),
                    bytes.fromhex(auth["nonce"][2:] if auth["nonce"].startswith("0x") else auth["nonce"]),
                    sig_bytes,
                )
                tx = fn.build_transaction({
                    "from": facilitator_account.address,
                    "nonce": nonce,
                    "gas": 200000,
                    "gasPrice": gas_price,
                    "chainId": ARC_CHAIN_ID,
                })

            signed = facilitator_account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

            # Wait briefly for receipt
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
            if receipt["status"] != 1:
                return SettleResponse(
                    success=False,
                    network=req.network,
                    errorReason=f"tx reverted: {tx_hash.hex()}",
                    transaction="0x" + tx_hash.hex(),
                    payer=auth["from"],
                )

            return SettleResponse(
                success=True,
                transaction="0x" + tx_hash.hex(),
                network=req.network,
                payer=auth["from"],
            )

        except Exception as e:
            logger.exception("settle failed")
            return SettleResponse(
                success=False,
                network=req.network,
                errorReason=f"on-chain submission failed: {e}",
                payer=auth["from"],
            )


# ----------------------------------------------------------------------------
# Signature recovery (EIP-712)
# ----------------------------------------------------------------------------

def _recover_signer(req: VerifyRequest, auth: dict[str, Any]) -> str:
    """Recover the signer of the EIP-3009 authorization."""
    from eth_account.messages import encode_typed_data

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
            "name": "USDC",
            "version": "2",
            "chainId": ARC_CHAIN_ID,
            "verifyingContract": ARC_USDC,
        },
        "message": {
            "from": auth["from"],
            "to": auth["to"],
            "value": int(auth["value"]),
            "validAfter": int(auth["validAfter"]),
            "validBefore": int(auth["validBefore"]),
            "nonce": bytes.fromhex(auth["nonce"][2:] if auth["nonce"].startswith("0x") else auth["nonce"]),
        },
    }
    encoded = encode_typed_data(full_message=typed_data)

    sig = req.payload.signature
    return Account.recover_message(encoded, signature=sig)