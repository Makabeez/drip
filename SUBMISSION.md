# Drip — Agora Submission Packet v2

> **Form:** https://forms.gle/ok3Gr9zhmHnApvK48
> **Deadline:** May 25 2026
>
> Field-by-field answers ready to copy-paste. Each section maps 1:1 to the actual form.

---

## Problem Statement *

**(What problem is your project solving? What is compelling about this problem?)**

AI trading agents today have zero economic accountability. An LLM prompted with "act as a trader" can claim to monitor 200 markets and make a thousand decisions a day — but none of those claims touch a counterparty, settle a transaction, or leave a verifiable trail. The agent costs nothing to run, so its outputs cost nothing.

The result is a flood of "AI trader" demos that can't be evaluated, can't be priced, and can't compose with each other. Markets don't believe in things that don't pay rent.

What's compelling is that the inverse is now actually buildable. Arc's sub-second finality and USDC-as-gas mean a trading agent can pay $0.005 per signal it consumes without the gas costs eroding the entire margin. EIP-3009 means the agent settles by signature without ever holding a gas token. CCTP V2 means the agent can move its own capital across chains when its margin runs low — no human in the loop.

For the first time, you can run a 24/7 trading agent where every signal is paid for, every trade is on-chain, every decision is hashed, and the agent's PnL is the only metric that matters. That's the substrate Drip is built for: an agentic market where signal sellers, traders, and risk providers price their work in real USDC, continuously, at the speed of the underlying market.

---

## Project Description

**(Describe what your project does, how it works, and what tech you used.)**

Drip is an autonomous Hyperliquid perps trading agent that buys its own signals on Arc Testnet via x402 micropayments, executes BTC-PERP trades on Hyperliquid testnet, manages its own risk with a daily kill switch and liquidation protection, and bridges its own capital across chains via Circle CCTP V2 when margin drops.

It targets RFB 01 (Perpetual Futures Trading Agent) — covering autonomous leverage decisions on Hyperliquid, dynamic SL/TP, liquidation protection, and cross-chain collateral movement.

**How it works.** Every 2 seconds the agent polls a self-hosted x402 v2 emitter for the latest BTC signal. The emitter returns HTTP 402 with a price quote ($0.005 USDC); the agent signs an EIP-3009 `transferWithAuthorization`; a self-hosted facilitator on Arc Testnet verifies and submits the payment on-chain in ~0.5 seconds; the emitter releases the signal. The signal flows into a decision engine that applies Kelly sizing, vol-scaled leverage (capped at 5x), and pyramid/close-opposite rules. A cross-cutting risk manager enforces a daily 5% kill switch and a 40% margin-utilization liquidation guard. Approved decisions execute on Hyperliquid via the Python SDK using a trade-only API wallet. Every decision is hashed and persisted in SQLite as a reasoning trace.

When account margin drops below threshold, the agent autonomously fires a CCTP V2 bridge: approve USDC on Arc → `depositForBurn` → poll Circle IRIS attestation → `receiveMessage` on Arbitrum Sepolia (Hyperliquid's settlement chain). Fastest end-to-end bridge: 14 seconds. The full bridge history is visible in the live dashboard with clickable tx links.

**Tech stack.** Python 3.12, FastAPI for the facilitator and dashboard, web3.py for Arc + Arbitrum interaction, eth-account for EIP-3009 signing, the Hyperliquid Python SDK for trade execution, SQLite for persistence (risk state, reasoning traces, bridge history), PM2 + systemd for 24/7 operation, Cloudflare tunnel for the public dashboard at drip.baserep.xyz. ~3,000 lines of Python across 14 modules, MIT licensed. **Three Circle primitives integrated:** USDC on Arc, EIP-3009 `transferWithAuthorization` via self-hosted x402 facilitator, and CCTP V2 cross-chain.

---

## Traction *

**(How many real people have tried the product? How much validation were you able to get from end users? Also include things like RTs / follows / stars here =))**

The "user" is the agent itself — Drip is built for the case where the autonomous agent IS the customer. Traction numbers as of submission time:

- **17h 26m+ continuous uptime** on a publicly accessible dashboard (drip.baserep.xyz), with PM2 + systemd autorestart and SQLite-persisted state across restarts
- **11,720+ paid signals processed** via x402 micropayments on Arc Testnet — every single payment is a real on-chain `transferWithAuthorization` tx, viewable on Arc Explorer
- **1,631 trades opened, 1,632 closed** on Hyperliquid BTC-PERP testnet — real fills via the API wallet, with real cumulative testnet notional ~$293k
- **2 end-to-end CCTP V2 bridges** completed Arc Testnet → Arbitrum Sepolia. Fastest: 14 seconds end-to-end (approve + burn + IRIS attest + mint). Both visible in the live dashboard with clickable tx links on Arc and Arbiscan
- **1 daily kill switch event** fired correctly at -5% NAV — proving the risk discipline actually works under live conditions, not just in code review
- **Live dashboard public** since May 19, accessible globally via Cloudflare tunnel

Public artifacts:
- GitHub: github.com/Makabeez/drip — public, MIT, ~10 commits showing the build progression
- X post (pinned): x.com/GeiserJoe2 — Day 6 announcement with dashboard screenshot, 3-Circle-primitives angle
- LinkedIn post: published earlier today (collaborator boost) with the same 3-primitives angle

The deeper validation is the code itself: it has run unattended for 17+ hours, processed five-figure transaction volume, survived its own kill switch tripping, and the dashboard is still serving from drip.baserep.xyz right now while you read this.

---

## Project Source Code *

https://github.com/Makabeez/drip

---

## Project Live

https://drip.baserep.xyz/

---

## Project Video Demo *

[TO FILL — Loom/YouTube link after D8 recording]

---

## Circle / Arc Feedback

**(What worked with Circle / Arc, and where can Circle / Arc improve as a product and resources? Specificity and quality of your answer might win you a feedback award!)**

### What worked

**USDC-as-gas on Arc is the actual game-changer.** Pay-per-signal at $0.005 doesn't economically make sense on any other L1 we've worked with. Volatile gas tokens force builders to over-charge to cover variance; flat USDC-denominated fees let us price the signal at half a cent and still have margin after settlement gas. We've shipped on Celo, Base, Pacifica, and Arc this year — Arc is the only one where per-action pricing this small actually pencils out.

**CCTP V2's IRIS attestation response is genuinely well-designed.** The `decodedMessageBody` field in the JSON saved us hours of debug time — we could see exactly what was being burned, to whom, and with what `mintRecipient` before ever submitting the destination tx. The same `TokenMessengerV2` address (`0x8FE6B999...`) across Arc Testnet and Arbitrum Sepolia is also a nice touch — felt very Stripe-API-like to have one constant work across chains.

**Sub-second finality is psychologically transformational.** When the agent settles a signal payment in 0.5s, the architecture stops thinking about "pending → confirmed → finalized" lifecycle and just treats every tx as immediately-real. That mental shift unlocks designs (like our every-2-second poll loop) that would be impossible on slower chains.

### Where we hit friction (the actually useful feedback)

1. **No public x402 facilitator supports Arc Testnet.** We had to ship our own (`facilitator.py`, MIT-licensed in our repo) just to settle our own payments. Coinbase's facilitator is Base-only; there's no Circle-hosted alternative on Arc. This is the single biggest unlock missing — every project trying to build pay-per-action on Arc has to either ship their own facilitator or use Base instead. **Suggestion:** Circle should host a public x402 v2 facilitator on Arc Testnet (and ultimately mainnet). Happy to contribute our implementation as a starting point.

2. **The IRIS V2 attestation API silently 404s on tx hashes missing the `0x` prefix.** Our first bridge polled for 5 minutes returning 404 because `Web3.to_bytes(...).hex()` in our web3.py version strips the prefix. The 404 gave no hint that the prefix was the issue. **Suggestion:** either accept both forms, or return a 400 with `"transactionHash must be 0x-prefixed"` to fail fast.

3. **CCTP V2 on Arc Testnet doesn't support Fast Transfer (only Standard, `minFinalityThreshold = 2000`).** Our attestation latency varied wildly between bridges — 8 seconds in one test, 643 seconds in another. For an agent making "low margin → bridge" decisions, this latency variance is hard to plan around. **Suggestion:** publish p50 / p95 / p99 finality times for Arc Testnet attestations so builders can size their margin buffers correctly. Even a static "expect 30-300s on testnet" callout in the docs would help.

4. **Arbitrum Sepolia rejects legacy `gasPrice` transactions when the base fee bumps mid-flight.** Our first `receiveMessage` call failed with `max fee per gas less than block base fee: maxFeePerGas: 20000000 baseFee: 20004000` — we were 4 wei short. Fixed by switching to EIP-1559 `maxFeePerGas` / `maxPriorityFeePerGas` with 2x headroom. **Suggestion:** the CCTP V2 docs / example code should explicitly recommend EIP-1559 type-2 transactions for `receiveMessage`. The Solidity reference is fine, but the JS/TS/Python integration examples should call this out.

5. **Arc Testnet faucet rate-limits are a real blocker during active development.** Multi-day cooldowns on Arc and dependent chains (we needed Arb Sepolia ETH for `receiveMessage` gas — Alchemy faucet has 24h cooldown). When you're iterating fast, this can stop work entirely. **Suggestion:** Circle should run its own Arc Testnet faucet with more generous limits for whitelisted builders during hackathons.

6. **"Unified Account" mode on Hyperliquid testnet UI breaks the Python SDK silently.** Not Circle's problem per se, but worth flagging for the CCTP-to-HL forwarding case: if a builder enables Unified Account mode in the HL UI, `marginSummary` returns `accountValue=0.0` and `assetPositions=[]` even when the UI shows balance. This took us hours to diagnose. The CCTP-to-HL forwarding service docs should call this out.

### What we'd love Circle to ship next

- **A hosted x402 v2 facilitator on Arc Testnet + mainnet** — #1 ask, would unlock dozens of pay-per-action projects
- **Circle Crosschain Forwarding Service on testnet** — currently mainnet-only; Drip's HL deposit step needs exactly this, but we can't demo it
- **Native Paymaster on Arc with one-line SDK integration** — would let agents skip the "facilitator pays gas" pattern entirely
- **A canonical reference implementation for an agent that manages its own multi-chain treasury** combining Gateway + CCTP + USYC — every builder is reinventing this; one official reference would compress development time materially

---

## General Feedback

**(What worked well? What didn't? What could the Canteen team improve for future hackathons?)**

### What worked well

- **The RFB format is excellent.** "Here are six concrete problems worth solving, but build whatever excites you" struck exactly the right balance between direction and freedom. RFB 01 mapped near-1:1 to what I wanted to build, which removed friction from "but is this a good fit?" second-guessing.
- **The judging rubric being published in advance** (30/30/20/20) let me prioritize correctly. I knew "traction" was as valuable as "innovation" and could plan two weeks accordingly instead of leaving the public deployment for the last day.
- **The Canteen + Arc Discord channels** had actual human energy and useful technical context. Quality > quantity of channels.
- **Async final judging (no live demo day required)** is a huge accessibility win. As a solo builder in Geneva on European time, not having to fly anywhere or attend a specific demo slot let me ship a better project.
- **Two weeks is the right duration** — long enough to ship something real, short enough to maintain momentum.

### What was hard

- **No public x402 facilitator on Arc made the entry barrier sharper than it needed to be.** Probably half the RFBs implicitly assume facilitator infrastructure is available — for pay-per-action work, you have to ship your own. Not a complaint, just a real cost.
- **The fragmentation between Arc docs, Circle docs, Hyperliquid docs, and CCTP V2 docs** meant a lot of context-switching. A "build an agent on Arc that trades on HL using x402 + CCTP" recipe would have shortcut a week of integration work for me. (And I bet most RFB 01 + RFB 05 builders.)
- **Faucet limits** as noted above — real friction on testnet iteration speed.

### Suggestions for next time

- **Publish a "x402 on Arc starter pack"** — a minimal client + facilitator template builders can clone instead of writing from scratch. Could be a hackathon repo, doesn't have to be production-grade.
- **Office hours from Circle / Arc engineers** during the build window (paired with the existing Discord) would let solo builders unblock faster. Even one hour twice a week would be massively valuable.
- **An "early traction" milestone at the halfway point** — encouraging projects to ship publicly before final submission would surface real engagement vs last-week scrambles. Could be lightweight: "post your live URL by Day 7 to be eligible for [small bonus]."
- **A "Circle stack breadth" sub-prize** — for projects that integrate 3+ Circle primitives. Would push more builders to actually use multiple products instead of one. (Drip would have won this one easily.)
- **An optional 1:1 with the Circle product team after submission**, even 15 minutes, for builders who provided detailed feedback. The form's $500 incentive is good; the actual feedback loop with the team would be even more valuable.

Overall: best-run hackathon I've participated in this year. The Heraclitus quotes were a nice touch.

---

## Quick prep checklist before hitting submit

- [ ] Verify drip.baserep.xyz is responding (curl /health)
- [ ] Verify github.com/Makabeez/drip latest commit is pushed
- [ ] Verify CCTP panel renders with at least one SUCCESS row (judges will click around)
- [ ] Demo video recorded + uploaded (max 3min, Loom/YouTube/Vimeo)
- [ ] Re-read this packet end-to-end (catch any factual drift)
- [ ] Submit ~24h before deadline for safety, then watch for any field errors
