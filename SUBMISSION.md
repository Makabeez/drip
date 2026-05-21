# Drip — Agora Agents Hackathon Submission Packet

> **Form:** https://forms.gle/ok3Gr9zhmHnApvK48
> **Deadline:** May 25 2026 (asynchronous review after deadline)
> **RFB targeted:** RFB 01 — Perpetual Futures Trading Agent
>
> Copy-paste each section into the corresponding form field.

---

## Project name

**Drip** — Autonomous Hyperliquid perps agent that pays for its own signals

---

## Short tagline (one line)

An autonomous perp trading agent that buys its signals on Arc via x402 micropayments and bridges its own capital cross-chain via CCTP V2 — three Circle primitives in one continuously running agent.

---

## Project description (longer)

Most "AI trading agents" are LLM-prompted toys with no economic accountability. Drip flips the model: the signal layer charges the agent layer, the agent's PnL becomes the signal's quality benchmark, and every decision leaves a USDC-denominated trail on Arc.

The agent runs 24/7 on Hyperliquid testnet (BTC perps), polling a self-hosted x402 v2 emitter every 2 seconds. Each signal costs $0.005 USDC, paid via EIP-3009 `transferWithAuthorization` and settled by a self-hosted facilitator on Arc Testnet — no public facilitator supports Arc yet, so Drip ships its own.

When a signal is purchased, the agent runs it through a decision engine (Kelly-sized position, vol-scaled leverage capped at 5x, kill switch + liq protection cross-cutting), then executes via the Hyperliquid Python SDK using a trade-only API wallet. Every decision is hashed and persisted as a reasoning trace.

When margin runs low, the agent autonomously bridges USDC from Arc Testnet (its operational chain) to Arbitrum Sepolia (Hyperliquid's settlement chain) via Circle CCTP V2 — full approve → burn → IRIS attest → mint flow, real on-chain in ~14 seconds.

The whole loop is unattended. Live dashboard streams state every 2s with on-chain proof links for every signal payment and bridge transaction.

---

## RFB(s) targeted

**RFB 01 — Perpetual Futures Trading Agent** (primary, near-1:1 fit)

Concrete mapping:
- 24/7 monitoring of Hyperliquid BTC-PERP ✅
- Split-second decisions on leverage (Kelly + vol-scaled, max 5x) ✅
- Liquidation protection (margin > 40% → kill switch, deleverage) ✅
- Cross-chain collateral movement (CCTP V2 Arc → Arb Sepolia) ✅
- Risk management framework with dynamic leverage adjustment ✅
- Live dashboard at drip.baserep.xyz ✅

---

## GitHub repo

https://github.com/Makabeez/drip

(MIT licensed, ~6 commits telling the build progression, every .py file documented inline.)

---

## Live product link

https://drip.baserep.xyz

Live dashboard — Bloomberg-aesthetic, dark theme, auto-refreshes every 2 seconds. Shows account value, daily PnL, position, risk state, full trade tape with clickable Arc explorer links, signal stream with confidence bars, and a CCTP BRIDGES panel with a "TRIGGER 1 USDC TOP-UP" button judges can click to fire a live bridge.

---

## Video demo

[TO RECORD ON D8 — placeholder. ~3 min Loom/YouTube walkthrough.]

Shot list (planned):
1. Land on dashboard, point out 4 top tiles (account, PnL, position, risk)
2. Highlight trade tape — click an Arc tx → it opens on explorer
3. Highlight signal stream + confidence bars
4. Scroll to CCTP BRIDGES panel
5. Click TRIGGER 1 USDC TOP-UP → confirm dialog → watch bridge complete in ~15s
6. New row appears with status SUCCESS + clickable mint tx on Arbiscan
7. Zoom out: this is what 3 Circle primitives in 1 agent looks like

---

## Traction metrics (the form will ask)

**As of May 21, 2026, 16:30 UTC:**

- **17h 26m continuous uptime** of the autonomous agent (across PM2 restarts, state persisted in SQLite)
- **11,720 paid signals processed** via x402 v2 micropayments on Arc Testnet
- **1,631 trades opened** on Hyperliquid BTC-PERP
- **1,632 trades closed** with realized PnL
- **293,000+ USDC cumulative notional traded** (testnet)
- **2 end-to-end CCTP V2 bridges** Arc Testnet → Arbitrum Sepolia, fastest 14s end-to-end
- **1 daily kill switch event** correctly fired at -5% NAV threshold (risk discipline proven)
- **Live URL accessible** at drip.baserep.xyz (Cloudflare tunnel on PM2-managed WSL VPS)

User problem we're building for: **AI trading agents today have zero economic accountability**. They prompt-engineer their way through markets without ever touching a counterparty or settling a transaction. Drip is the opposite: every signal is paid for, every trade is on-chain, every decision is hashable, and the agent's PnL is the only metric that matters. We're building toward a future where agentic commerce is the substrate, not a feature.

---

## Circle Product Feedback (the $500 bonus field)

### Circle products used in Drip

| Product | How we used it | Why we chose it |
|---|---|---|
| **USDC on Arc Testnet** | Native settlement for every signal payment ($0.005 each) and as the chain's native gas | The "gas in USDC" property is the entire premise of pay-per-signal economics. No other L1 makes per-signal pricing make sense. |
| **EIP-3009 `transferWithAuthorization`** | Every x402 v2 micropayment uses a signed authorization, submitted by our self-hosted facilitator | The agent signs once and never holds gas tokens — facilitator pays gas, agent settles in USDC. Removes the "agent needs to source gas" problem entirely. |
| **CCTP V2 (Cross-Chain Transfer Protocol)** | Bridges USDC from Arc Testnet (operational chain) to Arbitrum Sepolia (Hyperliquid's settlement chain) when margin drops | The agent shouldn't need a human to top up its margin. CCTP V2 lets it move its own capital cross-chain autonomously. |

### What worked well

- **USDC-as-gas on Arc** — sub-second finality and predictable dollar-denominated fees made the pay-per-signal economics actually viable. We could not have built this on any other L1.
- **CCTP V2 attestation flow** — the IRIS API returned `status: complete` in 8 seconds on one of our bridges, with the full decoded message body. The attestation JSON includes a `decodedMessageBody` field that's a delight to debug against.
- **Same contract addresses across testnets** — `TokenMessengerV2` at `0x8FE6B999...` works identically on Arc Testnet and Arbitrum Sepolia. Made the integration almost trivial once we had the constants right.

### Friction points (the actually useful feedback)

1. **No public x402 facilitator supports Arc Testnet yet.** We had to ship our own. This is the single biggest blocker for anyone wanting to build pay-per-action on Arc — Coinbase's facilitator is Base-only, and there's no Circle-hosted alternative. **Suggestion:** Circle should run a hosted x402 facilitator on Arc, or publish an MIT-licensed reference one that supports the V2 schema. (We'd happily contribute ours, see `facilitator.py` in our repo.)

2. **IRIS V2 attestation API silently 404s on tx hashes without `0x` prefix.** Our first bridge spent 5 minutes polling because `Web3.to_bytes(...).hex()` returns the hash without the prefix in our version of web3.py, and the IRIS endpoint requires it. The 404 response gives no hint that the prefix is the issue. **Suggestion:** either accept both prefix forms, or return a 400 with a hint like `"transactionHash must be 0x-prefixed"`.

3. **Arc Testnet faucet rate-limits sharply.** During testing we needed Arb Sepolia ETH for `receiveMessage` gas and bumped into multi-day cooldowns on Alchemy's faucet. Not Circle's problem directly, but the CCTP V2 docs could note "you also need destination-chain gas to call `receiveMessage`" up-front. We almost missed it.

4. **Arc Testnet base fee fluctuates fast enough to reject legacy `gasPrice`-style transactions.** We had to switch our `_step_mint` call to EIP-1559 `maxFeePerGas` / `maxPriorityFeePerGas` with 2x headroom after seeing `max fee per gas less than block base fee` errors mid-flight. **Suggestion:** the Arc docs should explicitly recommend EIP-1559 type-2 transactions for anything touching the bridge.

5. **CCTP V2 doesn't support Fast Transfer from Arc Testnet** (only Standard, `minFinalityThreshold = 2000`). This means attestation latency is variable — we saw 8 seconds in one test, 643 seconds in another. For an agent making "low margin → bridge" decisions, this latency variance is hard to plan around. **Suggestion:** publish the typical p50/p95/p99 finality times for Arc Testnet attestations so builders can size their margin buffers correctly.

### What we'd like Circle to ship next

- **Hosted x402 facilitator on Arc** (#1 ask, would unlock dozens of projects)
- **Circle Crosschain Forwarding Service availability for testnet** — the production-grade automation that abstracts away attestation polling is exactly what Drip's HL deposit step would use, but it's mainnet-only today
- **Native Paymaster on Arc** with one-line SDK integration — would let agents skip even the "facilitator pays gas" pattern entirely
- **A reference implementation for "agent managing its own multi-chain treasury"** combining Gateway + CCTP + USYC. Right now builders cobble these together; one canonical reference would compress development time materially.

---

## Team

**Joe Geiser** ([@GeiserJoe2](https://x.com/GeiserJoe2), [Makabeez](https://github.com/Makabeez) on GitHub) — solo builder, based in Geneva, Switzerland. Former proprietary trader, now building onchain. Active hackathon participant; recent projects include AlphaDrip (lablab.ai Agentic Economy on Arc), PropRail (USDC payout rail for prop firms), and Scavenger (Pacifica liquidation sniper).

---

## License

MIT — see [LICENSE](https://github.com/Makabeez/drip/blob/main/LICENSE) in the repo.

Use it, fork it, ship something better.
