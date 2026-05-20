"""
drip.cctp
=========

Circle CCTP V2 bridge: Arc Testnet → Arbitrum Sepolia.

Bridge flow (4 steps):
    1. Approve USDC on Arc Testnet for TokenMessengerV2 to spend
    2. Call depositForBurn() on Arc — burns USDC, emits MessageSent event
    3. Poll Circle IRIS attestation API until signature is ready (30-120s on testnet)
    4. Call receiveMessage() on Arbitrum Sepolia MessageTransmitterV2 — mints USDC

Result: USDC moves from sender on Arc Testnet → sender on Arbitrum Sepolia
(same EVM address, just different chain context).

Demo narrative:
    "When the agent's HL margin falls below threshold, it autonomously bridges
     USDC from its Arc operational wallet to its Arbitrum settlement wallet.
     From Arb Sepolia, Hyperliquid's existing deposit flow (or Circle's
     Crosschain Forwarding Service) takes over."

Persistence: every bridge attempt is logged to SQLite `cctp_bridges` table
with all four tx hashes (or whichever step failed). Dashboard renders these.

References:
    https://developers.circle.com/cctp/concepts/supported-chains-and-domains
    https://developers.circle.com/cctp/references/contract-addresses
    https://developers.circle.com/cctp/references/attestation-verification
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from eth_account import Account
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Constants — testnet contracts (same across all V2 testnets)
# ----------------------------------------------------------------------------

# Same addresses on Arc Testnet and Arbitrum Sepolia
TOKEN_MESSENGER_V2   = Web3.to_checksum_address("0x8FE6B999Dc680CcFDD5Bf7EB0974218be2542DAA")
MESSAGE_TRANSMITTER  = Web3.to_checksum_address("0xE737e5cEBEEBa77EFE34D4aa090756590b1CE275")

# Domain IDs
DOMAIN_ARC_TESTNET    = 26
DOMAIN_ARBITRUM       = 3

# Chain-specific USDC contracts (6 decimals on both)
USDC_ARC              = Web3.to_checksum_address("0x3600000000000000000000000000000000000000")
USDC_ARB_SEPOLIA      = Web3.to_checksum_address("0x75faf114eafb1BDbe2F0316DF893fd58CE46AA4d")

# RPCs
RPC_ARC_DEFAULT       = "https://rpc.testnet.arc.network"
RPC_ARB_SEPOLIA       = "https://sepolia-rollup.arbitrum.io/rpc"

# Circle IRIS attestation API (V2 testnet)
IRIS_API_BASE         = "https://iris-api-sandbox.circle.com/v2"


# ----------------------------------------------------------------------------
# Minimal ABIs
# ----------------------------------------------------------------------------

ERC20_ABI = [
    {"inputs":[{"name":"a","type":"address"}],"name":"balanceOf","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"type":"bool"}],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
]

# TokenMessengerV2.depositForBurn (V2 signature includes maxFee + minFinalityThreshold)
TOKEN_MESSENGER_ABI = [
    {
        "inputs": [
            {"name":"amount",                "type":"uint256"},
            {"name":"destinationDomain",     "type":"uint32"},
            {"name":"mintRecipient",         "type":"bytes32"},
            {"name":"burnToken",             "type":"address"},
            {"name":"destinationCaller",     "type":"bytes32"},
            {"name":"maxFee",                "type":"uint256"},
            {"name":"minFinalityThreshold",  "type":"uint32"},
        ],
        "name": "depositForBurn",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

# MessageTransmitterV2.receiveMessage
MESSAGE_TRANSMITTER_ABI = [
    {
        "inputs": [
            {"name": "message",     "type": "bytes"},
            {"name": "attestation", "type": "bytes"},
        ],
        "name": "receiveMessage",
        "outputs": [{"type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


# ----------------------------------------------------------------------------
# Persistence schema
# ----------------------------------------------------------------------------

CCTP_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS cctp_bridges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at_ms INTEGER NOT NULL,
    finished_at_ms INTEGER,
    sender_address TEXT NOT NULL,
    recipient_address TEXT NOT NULL,
    amount_usdc REAL NOT NULL,
    src_domain INTEGER NOT NULL,
    dst_domain INTEGER NOT NULL,
    approve_tx TEXT,
    burn_tx TEXT,
    attestation_received_ms INTEGER,
    mint_tx TEXT,
    status TEXT NOT NULL,
    error TEXT
);
"""


# ----------------------------------------------------------------------------
# Bridge result type
# ----------------------------------------------------------------------------

@dataclass
class BridgeResult:
    success: bool
    amount_usdc: float
    sender: str
    recipient: str
    approve_tx: str | None = None
    burn_tx: str | None = None
    mint_tx: str | None = None
    attestation_seconds: int | None = None
    total_seconds: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# ----------------------------------------------------------------------------
# CCTPBridge — the bridge orchestrator
# ----------------------------------------------------------------------------

class CCTPBridge:
    """
    Bridges USDC from Arc Testnet to Arbitrum Sepolia via Circle CCTP V2.
    """

    def __init__(
        self,
        private_key: str,
        db: sqlite3.Connection,
        src_rpc: str | None = None,
        dst_rpc: str | None = None,
    ) -> None:
        self._account = Account.from_key(private_key)
        self._db = db
        self._db.executescript(CCTP_TABLE_DDL)
        self._db.commit()

        # Web3 instances for source (Arc) and destination (Arb Sepolia)
        self._w3_src = Web3(Web3.HTTPProvider(src_rpc or os.environ.get("ARC_RPC", RPC_ARC_DEFAULT)))
        self._w3_dst = Web3(Web3.HTTPProvider(dst_rpc or RPC_ARB_SEPOLIA))

        # POA middleware for L2s (just in case)
        try:
            self._w3_src.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
            self._w3_dst.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        except Exception:
            pass

        # Contract instances
        self._usdc_src = self._w3_src.eth.contract(address=USDC_ARC, abi=ERC20_ABI)
        self._token_messenger = self._w3_src.eth.contract(
            address=TOKEN_MESSENGER_V2, abi=TOKEN_MESSENGER_ABI
        )
        self._message_transmitter = self._w3_dst.eth.contract(
            address=MESSAGE_TRANSMITTER, abi=MESSAGE_TRANSMITTER_ABI
        )

        logger.info("CCTPBridge initialized for %s", self._account.address)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def bridge_to_arb_sepolia(self, amount_usdc: float, recipient: str | None = None) -> BridgeResult:
        """
        Bridge `amount_usdc` from Arc Testnet to Arbitrum Sepolia.

        If `recipient` is None, defaults to self._account.address (same address on both chains).
        """
        amount_micros = int(amount_usdc * 1_000_000)
        recipient_addr = Web3.to_checksum_address(recipient or self._account.address)
        start = time.time()
        bridge_id = self._db_insert_started(
            sender=self._account.address,
            recipient=recipient_addr,
            amount=amount_usdc,
        )

        logger.info(
            "CCTP bridge starting: %.6f USDC, %s → %s (id=%d)",
            amount_usdc, self._account.address, recipient_addr, bridge_id,
        )

        try:
            # Pre-check: enough Arc USDC?
            balance = self._usdc_src.functions.balanceOf(self._account.address).call()
            if balance < amount_micros:
                raise CCTPBridgeError(
                    f"insufficient Arc USDC: have {balance/1e6:.6f}, need {amount_usdc:.6f}"
                )

            # Step 1: approve
            approve_tx = await self._step_approve(amount_micros)
            self._db_update(bridge_id, approve_tx=approve_tx)
            logger.info("  ✓ Step 1/4 — approved: %s", approve_tx)

            # Step 2: depositForBurn
            burn_tx, burn_receipt = await self._step_burn(
                amount_micros=amount_micros,
                recipient=recipient_addr,
            )
            self._db_update(bridge_id, burn_tx=burn_tx)
            logger.info("  ✓ Step 2/4 — burned on Arc: %s", burn_tx)

            # Step 3: poll IRIS for attestation
            attest_start = time.time()
            message_bytes, attestation_bytes = await self._step_poll_attestation(burn_tx)
            attest_seconds = int(time.time() - attest_start)
            self._db_update(bridge_id, attestation_received_ms=int(time.time() * 1000))
            logger.info("  ✓ Step 3/4 — attestation in %ds", attest_seconds)

            # Step 4: mint on Arb Sepolia
            mint_tx = await self._step_mint(message_bytes, attestation_bytes)
            self._db_update(bridge_id, mint_tx=mint_tx)
            logger.info("  ✓ Step 4/4 — minted on Arb Sepolia: %s", mint_tx)

            total_seconds = int(time.time() - start)
            self._db_finalize(bridge_id, "success", None)
            logger.info("CCTP bridge complete in %ds", total_seconds)

            return BridgeResult(
                success=True,
                amount_usdc=amount_usdc,
                sender=self._account.address,
                recipient=recipient_addr,
                approve_tx=approve_tx,
                burn_tx=burn_tx,
                mint_tx=mint_tx,
                attestation_seconds=attest_seconds,
                total_seconds=total_seconds,
            )

        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:200]}"
            self._db_finalize(bridge_id, "failed", err)
            logger.exception("CCTP bridge failed: %s", err)
            return BridgeResult(
                success=False,
                amount_usdc=amount_usdc,
                sender=self._account.address,
                recipient=recipient_addr,
                total_seconds=int(time.time() - start),
                error=err,
            )

    def list_bridges(self, limit: int = 10) -> list[dict[str, Any]]:
        """For dashboard /state endpoint."""
        cur = self._db.execute(
            """SELECT id, started_at_ms, finished_at_ms, sender_address, recipient_address,
                       amount_usdc, src_domain, dst_domain, approve_tx, burn_tx,
                       attestation_received_ms, mint_tx, status, error
               FROM cctp_bridges ORDER BY started_at_ms DESC LIMIT ?""",
            (limit,),
        )
        rows = cur.fetchall()
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in rows]

    # ------------------------------------------------------------------
    # Step implementations
    # ------------------------------------------------------------------

    async def _step_approve(self, amount_micros: int) -> str:
        """Approve TokenMessengerV2 to spend USDC on Arc Testnet."""
        # Check current allowance — skip approve if already enough
        current = self._usdc_src.functions.allowance(
            self._account.address, TOKEN_MESSENGER_V2
        ).call()
        if current >= amount_micros:
            logger.info("  approve already sufficient (%.6f USDC)", current / 1e6)
            return "0x" + "0" * 64 + " (skipped, allowance sufficient)"

        # Build tx — approve a generous amount to avoid re-approving every time
        approve_amount = max(amount_micros, 1_000_000_000)  # 1000 USDC default headroom
        tx = self._usdc_src.functions.approve(
            TOKEN_MESSENGER_V2, approve_amount
        ).build_transaction({
            "from":  self._account.address,
            "nonce": self._w3_src.eth.get_transaction_count(self._account.address),
            "gas":   200_000,
            "gasPrice": self._w3_src.eth.gas_price,
        })

        signed = self._account.sign_transaction(tx)
        tx_hash = self._w3_src.eth.send_raw_transaction(signed.raw_transaction)
        receipt = await self._wait_for_receipt(self._w3_src, tx_hash, timeout=60)
        if receipt.status != 1:
            raise CCTPBridgeError(f"approve failed: tx {tx_hash.hex()}")
        return tx_hash.hex()

    async def _step_burn(
        self,
        amount_micros: int,
        recipient: str,
    ) -> tuple[str, dict[str, Any]]:
        """Burn USDC on Arc via TokenMessengerV2.depositForBurn (V2 signature)."""
        # mintRecipient is a bytes32 (address padded left with zeros)
        mint_recipient_bytes32 = bytes(12) + Web3.to_bytes(hexstr=recipient)

        # destinationCaller: bytes32(0) = anyone can call receiveMessage on destination
        destination_caller = bytes(32)

        # V2 fee params:
        #   maxFee: maximum we're willing to pay for fast attestation, 0 = use standard
        #   minFinalityThreshold: 2000 = standard finality (60-120s on testnet),
        #                         1000 = fast finality (only on supported chains)
        # Arc is standard-only, so 2000 is the right value.
        max_fee = 0
        min_finality_threshold = 2000

        tx = self._token_messenger.functions.depositForBurn(
            amount_micros,
            DOMAIN_ARBITRUM,
            mint_recipient_bytes32,
            USDC_ARC,
            destination_caller,
            max_fee,
            min_finality_threshold,
        ).build_transaction({
            "from":  self._account.address,
            "nonce": self._w3_src.eth.get_transaction_count(self._account.address),
            "gas":   500_000,
            "gasPrice": self._w3_src.eth.gas_price,
        })

        signed = self._account.sign_transaction(tx)
        tx_hash = self._w3_src.eth.send_raw_transaction(signed.raw_transaction)
        receipt = await self._wait_for_receipt(self._w3_src, tx_hash, timeout=60)
        if receipt.status != 1:
            raise CCTPBridgeError(f"burn failed: tx {tx_hash.hex()}")
        return tx_hash.hex(), dict(receipt)

    async def _step_poll_attestation(self, burn_tx_hash: str) -> tuple[bytes, bytes]:
        """
        Poll Circle IRIS API for attestation.

        Endpoint: GET /v2/messages/{srcDomain}?transactionHash={burnTx}
        Returns: { messages: [{ message, attestation, status, ... }] }

        We wait until status == "complete" then return (message_bytes, attestation_bytes).
        """
        tx_hex = burn_tx_hash if burn_tx_hash.startswith("0x") else f"0x{burn_tx_hash}"
        url = f"{IRIS_API_BASE}/messages/{DOMAIN_ARC_TESTNET}?transactionHash={tx_hex}"
        max_wait_seconds = 300  # 5 min absolute cap
        poll_interval = 5
        elapsed = 0

        async with httpx.AsyncClient(timeout=15) as client:
            while elapsed < max_wait_seconds:
                try:
                    r = await client.get(url)
                    if r.status_code == 200:
                        body = r.json()
                        messages = body.get("messages", [])
                        if messages:
                            msg = messages[0]
                            status = msg.get("status")
                            if status == "complete":
                                message_hex = msg["message"]
                                attestation_hex = msg["attestation"]
                                return (
                                    Web3.to_bytes(hexstr=message_hex),
                                    Web3.to_bytes(hexstr=attestation_hex),
                                )
                            logger.info("  attestation pending (status=%s, elapsed=%ds)", status, elapsed)
                    elif r.status_code == 404:
                        logger.debug("  attestation not yet indexed (404)")
                    else:
                        logger.warning("  IRIS unexpected status %d: %s", r.status_code, r.text[:200])
                except Exception as e:
                    logger.warning("  IRIS poll error: %s", e)

                await asyncio.sleep(poll_interval)
                elapsed += poll_interval

        raise CCTPBridgeError(
            f"attestation not received after {max_wait_seconds}s for tx {burn_tx_hash}"
        )

    async def _step_mint(self, message: bytes, attestation: bytes) -> str:
        """Call receiveMessage on Arb Sepolia MessageTransmitterV2 to mint USDC."""
        tx = self._message_transmitter.functions.receiveMessage(
            message, attestation
        ).build_transaction({
            "from":  self._account.address,
            "nonce": self._w3_dst.eth.get_transaction_count(self._account.address),
            "gas":   300_000,
            "maxFeePerGas": int(self._w3_dst.eth.gas_price * 2),
            "maxPriorityFeePerGas": int(self._w3_dst.eth.gas_price * 0.5),
            "type": 2,
        })

        signed = self._account.sign_transaction(tx)
        tx_hash = self._w3_dst.eth.send_raw_transaction(signed.raw_transaction)
        receipt = await self._wait_for_receipt(self._w3_dst, tx_hash, timeout=120)
        if receipt.status != 1:
            raise CCTPBridgeError(f"mint failed: tx {tx_hash.hex()}")
        return tx_hash.hex()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _wait_for_receipt(
        self, w3: Web3, tx_hash, timeout: int = 60
    ) -> Any:
        """Async wrapper around web3 receipt polling."""
        start = time.time()
        while time.time() - start < timeout:
            try:
                return w3.eth.get_transaction_receipt(tx_hash)
            except Exception:
                pass
            await asyncio.sleep(2)
        raise CCTPBridgeError(f"receipt timeout for {tx_hash.hex() if hasattr(tx_hash, 'hex') else tx_hash}")

    def _db_insert_started(self, sender: str, recipient: str, amount: float) -> int:
        cur = self._db.execute(
            """INSERT INTO cctp_bridges
                (started_at_ms, sender_address, recipient_address, amount_usdc,
                 src_domain, dst_domain, status)
               VALUES (?, ?, ?, ?, ?, ?, 'in_progress')""",
            (
                int(time.time() * 1000),
                sender,
                recipient,
                amount,
                DOMAIN_ARC_TESTNET,
                DOMAIN_ARBITRUM,
            ),
        )
        self._db.commit()
        return cur.lastrowid

    def _db_update(self, bridge_id: int, **fields) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields.keys())
        values = list(fields.values()) + [bridge_id]
        self._db.execute(f"UPDATE cctp_bridges SET {sets} WHERE id = ?", values)
        self._db.commit()

    def _db_finalize(self, bridge_id: int, status: str, error: str | None) -> None:
        self._db.execute(
            """UPDATE cctp_bridges
               SET finished_at_ms = ?, status = ?, error = ?
               WHERE id = ?""",
            (int(time.time() * 1000), status, error, bridge_id),
        )
        self._db.commit()


# ----------------------------------------------------------------------------
# Exception
# ----------------------------------------------------------------------------

class CCTPBridgeError(Exception):
    pass


# ----------------------------------------------------------------------------
# CLI: standalone bridge for manual test
# ----------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    amount = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0  # default $1 USDC
    print(f"Bridging {amount} USDC Arc Testnet → Arbitrum Sepolia...")

    db_path = os.environ.get("SQLITE_PATH")
    if not db_path:
        raise SystemExit("SQLITE_PATH not set in .env")
    conn = sqlite3.connect(db_path)

    # Find the right PK — we use HL Master since we agreed it's the bridge wallet
    # but HL master uses the address, not a PK in env. So for CCTP we need a
    # separate PK env var. Falls back to CONSUMER_PK if HL_MASTER_PK isn't set.
    pk = os.environ.get("HL_MASTER_PK") or os.environ.get("CONSUMER_PK")
    if not pk:
        raise SystemExit("Need HL_MASTER_PK (or CONSUMER_PK) in .env")

    bridge = CCTPBridge(private_key=pk, db=conn)
    result = asyncio.run(bridge.bridge_to_arb_sepolia(amount_usdc=amount))

    print()
    print(json.dumps(result.to_dict(), indent=2))
