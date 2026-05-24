<div align="center">

# DRIP

**Autonomous Hyperliquid perps agent that pays for its own signals on Arc and bridges its own capital with CCTP.**

[![Live tx](https://img.shields.io/badge/Arc%20Testnet-on--chain%20proof-4FC1FF?style=for-the-badge&logo=ethereum&logoColor=white)](https://testnet.arcscan.app/tx/0x583308ed6408c9709b8776765541b613c163eb27142d5e0ae637a176b5dc1688)
[![Dashboard](https://img.shields.io/badge/Dashboard-drip.baserep.xyz-4EC9B0?style=for-the-badge)](https://drip.baserep.xyz)
[![License](https://img.shields.io/badge/License-MIT-E3B341?style=for-the-badge)](LICENSE)
[![Hackathon](https://img.shields.io/badge/Agora-Agents%20Hackathon-B392F0?style=for-the-badge)](https://community.arc.network/public/events/agora-agents-hackathon-88fvefopg2)

[![Python](https://img.shields.io/badge/Python-3.12-blue?logo=python&logoColor=white)](https://www.python.org)
[![Arc](https://img.shields.io/badge/Built%20on-Arc-FF6B35)](https://www.arc.network)
[![Hyperliquid](https://img.shields.io/badge/Trading-Hyperliquid-00D4AA)](https://hyperliquid.xyz)
[![x402](https://img.shields.io/badge/Protocol-x402%20v2-9B6BFF)](https://www.x402.org)
[![CCTP](https://img.shields.io/badge/Circle-CCTP%20V2-1F75FE)](https://developers.circle.com/cctp)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)

</div>

## 🎬 Demo

[![Drip — 3-minute demo](https://img.youtube.com/vi/uKpg8CnP5Cw/maxresdefault.jpg)](https://youtu.be/uKpg8CnP5Cw)

*[Watch on YouTube](https://youtu.be/uKpg8CnP5Cw) — 3-minute walkthrough of the dashboard, live trading, kill-switch logic, and a live CCTP V2 bridge trigger Arc → Arbitrum Sepolia.*

---

> Three Circle primitives in one autonomous agent: x402 v2 micropayments for signal purchase, EIP-3009 transferWithAuthorization for settlement, and CCTP V2 for cross-chain capital management. No facilitator publicly supports Arc Testnet, so Drip ships its own. Every signal the agent buys settles on-chain in under a second, every trade is logged with a hashable reasoning trace, and when margin runs low, the agent bridges its own USDC from Arc to Arbitrum Sepolia.

---

## Why

Most "AI trading agents" are LLM-prompted toys with no economic accountability. Drip flips the model:

- The **signal layer charges the agent layer** via x402 micropayments
- The agent's PnL becomes the signal's quality benchmark
- Every decision leaves a USDC-denominated trail on Arc
- When margin runs low, the agent **bridges its own capital** across chains via Circle CCTP V2

Pay-per-signal at $0.001 only works on Arc — sub-second finality, gas in USDC, native EIP-3009. Cross-chain margin top-ups only work cleanly with CCTP V2. Drip combines **three Circle primitives** in one autonomous agent: x402 v2 micropayments, EIP-3009 settlements, and CCTP V2 bridging. This isn't a payment rail bolted onto an agent — it's the agent's metabolism.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          DRIP — AGENT LAYER                              │
│                                                                          │
│   ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐   │
│   │  Signal Client   │──▶│  Decision Engine │──▶│  HL Executor     │   │
│   │  · HTTP 402 pay  │   │  · Kelly sizing  │   │  · py SDK        │   │
│   │  · EIP-3009 sign │   │  · Vol-scaled lev│   │  · API wallet    │   │
│   │  · USDC on Arc   │   │  · Time stops    │   │  · Trade-only    │   │
│   └──────────────────┘   └──────────────────┘   └──────────────────┘   │
│           │                       │                       │             │
│           ▼                       ▼                       ▼             │
│   ┌──────────────────────────────────────────────────────────────────┐ │
│   │              Risk Manager (cross-cutting)                         │ │
│   │  · Daily kill switch (-5% NAV → halt 24h)                        │ │
│   │  · Liquidation protection (margin > 40% → auto-deleverage)       │ │
│   │  · Telegram alerts on every safety event                         │ │
│   │  · SQLite-persisted state, survives restarts                     │ │
│   └──────────────────────────────────────────────────────────────────┘ │
│                                  │                                       │
│                                  ▼                                       │
│   ┌──────────────────────────────────────────────────────────────────┐ │
│   │              CCTP V2 Bridge (autonomous capital)                  │ │
│   │  · Detects low HL margin → bridges from Arc to Arbitrum Sepolia  │ │
│   │  · approve → depositForBurn → IRIS attest → receiveMessage       │ │
│   │  · 8–60s end-to-end on testnet                                    │ │
│   │  · Manual trigger via dashboard + autonomous (production gated)   │ │
│   └──────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────┘
            ▲                                                  │
            │ HTTP 402 / signal                                │ orders
            │                                                  ▼
┌───────────┴────────────┐                          ┌───────────────────┐
│   x402 v2 EMITTER      │                          │   HYPERLIQUID     │
│   (self-hosted)        │                          │   (perp DEX)      │
│                        │                          │                   │
│  · Mock cascade engine │                          │  · BTC-PERP       │
│  · Express on :8091    │                          │  · WS user fills  │
│  · 402 challenge       │                          │  · API wallet     │
└────────────────────────┘                          └───────────────────┘
            │ x-payment header                                ▲
            ▼                                                 │
┌────────────────────────┐                                    │
│   x402 v2 FACILITATOR  │                                    │
│   (self-hosted)        │                                    │
│                        │                                    │
│  · /verify, /settle    │                                    │
│  · Submits EIP-3009    │                                    │
│  · On-chain on Arc     │                                    │
└───────────┬────────────┘                                    │
            │                                                 │
            ▼                                                 ▼
┌─────────────────────────────────┐   ┌────────────────────────────────┐
│         ARC TESTNET             │   │      ARBITRUM SEPOLIA          │
│                                 │   │                                │
│  USDC ─── transferWithAuth ──▶ Seller│  HL settlement chain          │
│  Consumer → Seller (signal pay) │   │  CCTP V2 mint destination     │
│  0.51s finality · gas in USDC   │   │  HL Master receives bridged $  │
└─────────────────────────────────┘   └────────────────────────────────┘
                  │                                  ▲
                  │ Consumer wallet                  │
                  └──── CCTP V2 burn ────────────────┘
                       (8s–60s, attest via Circle IRIS)
```

---

## Tech stack

| Layer | Component | Why |
|-------|-----------|-----|
| **Payment** | x402 v2 + EIP-3009 | Gasless micropayments, $0.001 per signal, sub-second finality on Arc |
| **Settlement** | Self-hosted facilitator on Arc Testnet | No public facilitator supports Arc yet — so we shipped one |
| **Execution** | `hyperliquid-python-sdk` | Battle-tested HL perps client with API-wallet permission model |
| **Decision** | Pure Python heuristics + Kelly sizing | Deterministic, hashable, replayable. No LLM in the trade loop. |
| **Risk** | Stateful manager, SQLite-persisted | Daily kill switch + liquidation protection survives restarts |
| **Telemetry** | SQLite traces + Telegram alerts via Makaclaw | Every decision is hashable and replayable |
| **Dashboard** | FastAPI + vanilla JS, 2s polling | Bloomberg aesthetic, every trade row links to its Arc tx |
| **Web framework** | FastAPI + uvicorn (async, single process) | Agent loop + dashboard share the event loop |

---

## Flow

A single signal goes through this exact sequence in under 3 seconds:

1. **`SignalClient`** probes the emitter — receives `402 Payment Required` with x402 v2 challenge in the `payment-required` header
2. Client decodes the challenge, signs an **EIP-3009** `transferWithAuthorization` for $0.001 USDC, encodes it base64 into the `x-payment` header
3. Client retries the GET — emitter forwards the signed envelope to the **facilitator's** `/verify` and `/settle` endpoints
4. Facilitator validates the signature, submits the auth on-chain to Arc Testnet's native USDC contract, returns the tx hash
5. Emitter returns the cascade signal + tx hash to the client
6. **`decide()`** evaluates the signal against portfolio + market state, builds a `ReasoningTrace` with the exact rule chain that fired
7. **`RiskManager.check_pretrade()`** can VETO the action (kill switch, emergency halt)
8. **`HLExecutor.execute()`** dispatches via the HL Python SDK using the trade-only API wallet
9. On close, `record_close()` updates the daily PnL ledger, fires Telegram alerts if thresholds are breached
10. Full trace + execution result is persisted to SQLite

---

## Repository layout

```
drip/
├── signal_client.py     # x402 v2 consumer — buys signals via EIP-3009
├── decision.py          # Pure decision engine (5 rules, no side effects)
├── executor.py          # Hyperliquid SDK wrapper, single execute(action) entry
├── risk.py              # Daily kill switch + liq protection + state persistence
├── reasoning.py         # ReasoningTrace dataclass + SQLite persistence
├── loop.py              # Main async agent + dashboard server (one event loop)
│
├── facilitator.py       # x402 v2 facilitator — submits EIP-3009 on Arc
├── mock_emitter.py      # x402 v2 emitter for local dev
├── cascade_sim.py       # Synthetic BTC signal generator
├── serve.py             # Runs facilitator + emitter on :8090 / :8091
│
├── cctp.py              # Circle CCTP V2 bridge — Arc → Arbitrum Sepolia
│
├── dashboard.py         # FastAPI /state + /cctp/trigger endpoint
├── static/index.html    # Bloomberg-aesthetic single-page dashboard
│
├── telegram_alerts.py   # Alert helper via existing Makaclaw bot
├── ARCHITECTURE.md      # Full design doc, RFB mapping, 14-day plan
└── .env.example         # Template for required environment variables
```

---

## Local development

### Prerequisites

- Python 3.10+
- Poetry
- WSL Ubuntu, Linux, or macOS (Windows-native untested)
- A Hyperliquid testnet master account + generated API wallet
- An Arc Testnet wallet funded with USDC via [faucet.circle.com](https://faucet.circle.com)

### Setup

```bash
git clone https://github.com/Makabeez/drip.git
cd drip
poetry install
cp .env.example .env
# Edit .env with your wallet keys + HL config
```

### Run the mock emitter + facilitator (terminal 1)

```bash
poetry run python serve.py
# Facilitator on :8090, emitter on :8091
```

### Run the agent + dashboard (terminal 2)

```bash
poetry run python loop.py
# Agent polls every 2s
# Dashboard on http://localhost:8086
```

### Open the dashboard

Navigate to `http://localhost:8086`. You'll see:

- **Header** — network, uptime, live status pill
- **Stat panels** — account value, daily PnL with kill-threshold gauge, position, risk state
- **Counters** — signals received, trades opened, trades closed, total settled USDC
- **Trade tape** — every executed trade with timestamps, sizes, leverage, TP/SL, clickable Arc tx links
- **Signal stream** — full decision log including holds, with confidence bars and reasons

---

## On-chain proof

Every signal payment is a real `transferWithAuthorization` call on Arc Testnet's USDC contract. One example from the first end-to-end test:

| Field | Value |
|-------|-------|
| Tx hash | [`0x583308ed6408c9709b8776765541b613c163eb27142d5e0ae637a176b5dc1688`](https://testnet.arcscan.app/tx/0x583308ed6408c9709b8776765541b613c163eb27142d5e0ae637a176b5dc1688) |
| Method | `transferWithAuthorization` |
| From (facilitator) | `0xE847Df51F83fda5663dB994268F2F07ec39BF7Bf` |
| Tokens transferred | `0x56...45be → 0xE8...F7Bf` for **0.001 USDC** |
| Confirmation time | **0.51 seconds** |
| Gas paid | `0.001904995245 USDC` |
| Status | ✅ Success |

Click the tx hash to verify on Arc explorer.

---

## Cross-chain settlement (Circle CCTP V2)

Drip extends past Arc with **Circle's Cross-Chain Transfer Protocol V2** for autonomous capital management. When Drip's Hyperliquid margin falls below threshold, the agent bridges USDC from its Arc Testnet operational wallet to its Arbitrum Sepolia settlement wallet (Hyperliquid's settlement chain) — without manual intervention, without exposed private keys, in seconds.

### The 4-step bridge flow

```
Arc Testnet                                                Arbitrum Sepolia
═════════════                                              ═════════════════

   Consumer                                                     HL Master
   wallet                                                        wallet
     │                                                              │
     │ ① approve()                                                   │
     │   USDC ── 1000 ──► TokenMessengerV2                          │
     │                                                              │
     │ ② depositForBurn()                                            │
     │   • burns USDC on Arc                                         │
     │   • emits MessageSent event with dest=ARBITRUM(3)            │
     │                                                              │
     │                                                              │
     ▼                                                              │
   ┌──────────────────────────────────────────┐                    │
   │ ③ Circle IRIS attestation service        │                    │
   │   • signs the burn event with Circle keys│                    │
   │   • returns (message, attestation) bytes │                    │
   │   • finality time: 8s–10min on testnet   │                    │
   └──────────────────────────────────────────┘                    │
                                                                    │
                                                                    │
                                                ④ receiveMessage()  │
                                                   USDC minted ◄────┘
                                                   to HL Master
```

### Live bridges on testnet (real funds, real attestations)

Drip has executed **two end-to-end Arc → Arbitrum Sepolia bridges** via the manual `/cctp/trigger` endpoint. All four contract calls are publicly verifiable:

#### Bridge #2 — Production-correct: Consumer → HL Master, 1.00 USDC, **14 seconds total**

| Step | Chain | Tx hash | Notes |
|------|-------|---------|-------|
| ① Approve | Arc Testnet | _skipped_ — allowance cached from prior bridge | Bridge code reuses existing allowance |
| ② Burn | Arc Testnet | [`0x24b4ccb7…5b9a4a`](https://testnet.arcscan.app/tx/0x24b4ccb7194f2b1a44c52d687ff436d4252171c9a0bd287927e01a3eb75b9a4a) | `depositForBurn` on TokenMessengerV2 |
| ③ Attestation | Circle IRIS | — | `messages[].status="complete"` in **8s** |
| ④ Mint | Arb Sepolia | [`0x0f2058ef…5a59a2`](https://sepolia.arbiscan.io/tx/0x0f2058ef61e3c59157d33211c21373c2918c0b9bd736fd87fc04f85af25a59a2) | `receiveMessage` on MessageTransmitterV2 → +1 USDC to HL Master |

#### Bridge #1 — Initial validation: self-bridge, 0.50 USDC, 643s total

| Step | Chain | Tx hash |
|------|-------|---------|
| ① Approve | Arc Testnet | [`0x8153d57b…eed9ca`](https://testnet.arcscan.app/tx/0x8153d57b4b08dabc3fce62006a32cad94f5b494fdf504a88ffb760f001eed9ca) |
| ② Burn | Arc Testnet | [`0xdedc0b69…e9a84c`](https://testnet.arcscan.app/tx/0xdedc0b692a2a804e9f2ac8a1330cc0f1d5d39adaea4a9813a06564d656e9a84c) |
| ④ Mint | Arb Sepolia | [`0x0e3cbd57…b84036`](https://sepolia.arbiscan.io/tx/0x0e3cbd5769e288cf53650502a3a20e55250e8cf1d7b8d3379c2cafba6bb84036) |

### Production path: Circle Crosschain Forwarding Service

Bridge #2's USDC mint arrives at HL Master's Arb Sepolia address (`0xa480…30B0`). To get from there into the Hyperliquid perp margin pool, production Drip uses [Circle's Crosschain Forwarding Service](https://developers.circle.com/cctp/concepts/forwarding-service), which is specifically designed for the Hyperliquid use case — it removes the attestation polling and one-click bridges from CCTP-enabled chains directly into HL's orderbook DEX deposit flow.

The Drip codebase implements the standard CCTP V2 path that any application can reuse; the Forwarding Service is the production-grade automation layer Circle hosts on top.

### CCTP integration details

- **`cctp.py`** — `CCTPBridge` class, ~530 lines, full 4-step flow with SQLite persistence
- **TokenMessengerV2:** [`0x8FE6B999Dc680CcFDD5Bf7EB0974218be2542DAA`](https://testnet.arcscan.app/address/0x8FE6B999Dc680CcFDD5Bf7EB0974218be2542DAA) (same on Arc + Arb Sepolia)
- **MessageTransmitterV2:** [`0xE737e5cEBEEBa77EFE34D4aa090756590b1CE275`](https://sepolia.arbiscan.io/address/0xE737e5cEBEEBa77EFE34D4aa090756590b1CE275)
- **Domain IDs:** Arc Testnet = 26, Arbitrum = 3
- **Standard transfer mode** (`minFinalityThreshold = 2000`) — Arc currently doesn't support Fast Transfer
- **Cooldown:** 60s between manual triggers (prevents button-mashing)
- **Trigger control:** dashboard button + `POST /cctp/trigger?amount_usdc=N`

### Circle stack breadth

Drip integrates **three independent Circle primitives** in a single autonomous agent:

| # | Primitive | Where it lives | Verification |
|---|-----------|----------------|--------------|
| 1 | **x402 v2 micropayments** | every signal request → 402 challenge → settle | trade tape rows linking to Arc tx |
| 2 | **EIP-3009 `transferWithAuthorization`** | self-hosted facilitator on Arc Testnet | facilitator at `0xE847…F7Bf` settles each payment |
| 3 | **CCTP V2 cross-chain** | `cctp.py` orchestrates approve→burn→attest→mint | bridge table above |

---

## Why this only works on Arc

| Property | Why it matters | Arc |
|----------|----------------|-----|
| **Sub-second finality** | $0.001 micropayments must clear faster than the trading signal expires | ✅ ~0.5s |
| **Native USDC as gas** | Agent doesn't need to source a native gas token | ✅ |
| **EIP-3009 on USDC** | Consumer signs once, facilitator pays gas — gasless for the buyer | ✅ |
| **Low gas cost** | Per-signal economics must clear gas | ✅ ~$0.002 |
| **EVM compatibility** | Reuse standard tooling (`web3.py`, `eth-account`) | ✅ |

---

## Configuration

All knobs live in `.env`. Key parameters:

| Variable | Default | Purpose |
|----------|---------|---------|
| `AGENT_POLL_INTERVAL` | `2.0` | Seconds between signal fetches |
| `CONF_THRESHOLD` | `0.60` | Minimum signal confidence to trade |
| `RISK_TP_PCT` | `0.0012` | Take-profit (+0.12% from entry) |
| `RISK_SL_PCT` | `0.0008` | Stop-loss (-0.08% from entry) |
| `RISK_TIME_STOP_SECONDS` | `60` | Force-close stale positions |
| `RISK_MAX_LEVERAGE` | `5` | Hard leverage cap |
| `RISK_DAILY_LOSS_PCT` | `0.02` | Daily kill switch threshold (-2% NAV) |
| `RISK_LIQ_MARGIN_RATIO` | `0.40` | Force-close if margin/account > 40% |
| `RISK_KELLY_FRACTION` | `0.25` | Quarter-Kelly sizing |
| `RISK_MAX_POSITION_PCT` | `0.05` | Max 5% NAV per position |
| `HL_NETWORK` | `testnet` | Switch to `mainnet` only when sized for it |
| `HL_SYMBOL` | `BTC` | v1 trades BTC-PERP only |

See `.env.example` for the full list with annotated comments.

---

## Roadmap

| Status | Item |
|--------|------|
| ✅ | x402 v2 facilitator + emitter on Arc Testnet |
| ✅ | Autonomous decision + execution loop on HL testnet |
| ✅ | Risk manager with kill switch + liq protection |
| ✅ | Bloomberg-aesthetic dashboard with live trade tape |
| ✅ | Public deployment at drip.baserep.xyz |
| ✅ | CCTP V2 top-up (Arc → HL margin refill on threshold, manual trigger + autonomous gated) |
| ⬜ | Paymaster integration (USDC-only gas on agent's Arc txs) |
| ⬜ | USYC idle margin sweep (yield on uninvested capital) |
| ⬜ | Reasoning-trace marketplace (Irys-pinned daily bundles) |
| ⬜ | Multi-symbol support (ETH, SOL alongside BTC) |
| ⬜ | Mainnet switch (after extended testnet uptime proof) |

---

## Attribution

Built by [@Makabeez](https://github.com/Makabeez) ([@GeiserJoe2](https://x.com/GeiserJoe2) on X) for the **Agora Agents Hackathon** — Canteen × Circle on Arc, May 11–25, 2026.

Lineage from prior work:
- [AlphaDrip](https://github.com/Makabeez/alphadrip) — pay-per-alpha signal API on Arc Testnet (April 2026)
- [PropRail](https://github.com/Makabeez/proprail) — USDC payout rail for prop firms (April 2026)
- [Scavenger](https://github.com/Makabeez/Scavenger-v2) — Pacifica liquidation sniper (April 2026)

---

## License

MIT — see [LICENSE](LICENSE).

Use it, fork it, ship something better.
