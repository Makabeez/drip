# Drip — Autonomous HL Perps Agent on Arc

> AlphaDrip v2. The signal API now has an autonomous customer: itself.

**Hackathon:** Agora Agents Hackathon (Canteen × Circle on Arc)
**Window:** May 11 → May 25, 2026
**Built by:** Joe (@GeiserJoe2 · github.com/Makabeez)
**Predecessor:** AlphaDrip (April 2026) — pay-per-alpha signal API on Arc Testnet, 163 on-chain txs in 326s demo

---

## Thesis

Most "AI trading agents" are LLM-prompted toys with no economic accountability. Drip flips the model: the **signal layer charges the agent layer** via on-chain micropayments, the agent's PnL becomes the signal's quality benchmark, and every decision the agent makes leaves a USDC-denominated trail on Arc. Pay-per-signal at $0.003/signal only works on Arc — sub-second finality, ~$0.0019 gas, native USDC settlement.

This is RFB 01 (Perpetual Futures Trading Agent) executed against Joe's actual domain: a former prop trader's risk discipline, HL native fluency, and a working signal API already shipped.

---

## RFB mapping

| RFB | Hit? | How |
|-----|------|-----|
| **01 — Perpetual Futures Trading Agent** | ✅ Primary | Autonomous HL perps execution, dynamic leverage, liquidation protection, funding-rate-aware |
| **05 — Cross-Platform Arbitrage Agent** | Secondary | CCTP-routed margin top-up from Arc to HL when opportunity > threshold |
| **06 — Social Trading Intelligence** | Stretch | Optional public signal feed registered as a Polymarket-style builder (research item #2 in Agora brief) |

---

## Judging axis fit

| Axis | Weight | Score plan |
|------|--------|------------|
| Agentic sophistication | 30% | Three autonomous decisions: signal subscription, position sizing, risk management. Full autonomy after launch. |
| Traction | 30% | Live agent during judging window. Real PnL. Real on-chain settlement of every signal payment. Demo wallet on Arc Testnet shows continuous tx stream. |
| Circle tool usage | 20% | USDC, CCTP, Wallets, Contracts, Paymaster, USYC. 6/7 primitives. |
| Innovation | 20% | Agent-as-its-own-customer pattern. Pay-per-signal economics impossible on any other chain. |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          DRIP — AGENT LAYER                              │
│                       (NEW: this hackathon)                              │
│                                                                          │
│   ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐   │
│   │  Signal Client   │──▶│  Decision Engine │──▶│  HL Executor     │   │
│   │  · HTTP 402 pay  │   │  · Confidence    │   │  · py SDK        │   │
│   │  · EIP-3009 sign │   │  · Kelly sizing  │   │  · API wallet    │   │
│   │  · USDC on Arc   │   │  · Vol regime    │   │  · Min $10 coll  │   │
│   └──────────────────┘   └──────────────────┘   └──────────────────┘   │
│           │                       │                       │             │
│           ▼                       ▼                       ▼             │
│   ┌──────────────────────────────────────────────────────────────────┐ │
│   │              Risk Manager (cross-cutting)                         │ │
│   │  · Liquidation protection (auto-deleverage @ 3x margin buffer)   │ │
│   │  · Daily loss kill switch (-2% NAV → halt 24h)                   │ │
│   │  · USYC sweep when idle (margin > 2x req → park excess yield)    │ │
│   │  · CCTP top-up from Arc when HL margin < threshold               │ │
│   └──────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────┘
            ▲                                                  │
            │ HTTP 402 / signal                                │ orders
            │                                                  ▼
┌───────────┴────────────┐                          ┌───────────────────┐
│  ALPHADRIP — EMITTER   │                          │   HYPERLIQUID     │
│  (REUSED: already live)│                          │   (perp DEX)      │
│                        │                          │                   │
│  · Cascade engine      │                          │  · BTC-PERP       │
│  · Express /signals    │                          │  · API wallet     │
│  · EIP-3009 relayer    │                          │  · WS user fills  │
│  · Arc Testnet USDC    │                          │                   │
└────────────────────────┘                          └───────────────────┘
            │                                                  ▲
            │ tx hash                                          │
            ▼                                                  │
┌─────────────────────────────────────────────────────────────────────────┐
│                          ARC TESTNET                                     │
│                                                                          │
│  USDC (0x3600...) ──── transferWithAuthorization ────▶ Seller wallet    │
│                                                        0x9747B4B2F4...   │
│  USYC ◀──── idle margin sweep ────                                       │
│                                                                          │
│  Paymaster: all agent gas paid in USDC, no native token                  │
└─────────────────────────────────────────────────────────────────────────┘
                                    ▲
                                    │ CCTP V2
                                    │ (margin top-up)
                                    ▼
                          ┌──────────────────────┐
                          │   HL ARBITRUM L2     │
                          │   USDC margin acct   │
                          └──────────────────────┘
```

---

## Component breakdown

### 1. Signal Client (`agent/signal_client.py`) — ~150 LOC

Subscribes to AlphaDrip emitter via HTTP 402. On `402 Payment Required`:
1. Parse `accepts` array for Arc Testnet USDC option
2. Sign EIP-3009 `transferWithAuthorization` ($0.003 USDC, payTo = AlphaDrip seller wallet)
3. POST signal request with `payment-signature` header
4. Receive `{ signal: {...}, tx_hash: "0x..." }`
5. Cache signal until next cascade fires

**Reused from AlphaDrip:** wire format, payment header spec, seller wallet address.
**New:** Python port (AlphaDrip consumer was TypeScript CLI). The agent needs Python for HL SDK fluency.

### 2. Decision Engine (`agent/decision.py`) — ~200 LOC

Pure function: `(signal, portfolio_state, market_state) → action`.

```python
@dataclass
class Action:
    side: Literal["long", "short", "close", "hold"]
    size_usd: float
    leverage: float  # 1.0 to 5.0 (conservative; deliberately not 10x+)
    reasoning: dict  # logged for trace, hashable, demo-ready
```

Decision rules (deliberately simple, deliberately interpretable):

- **Signal confidence < 0.6** → hold
- **Position already open same direction** → hold (no pyramiding for v1)
- **Position open opposite direction** → close, wait one signal, then enter
- **Position size** → fractional Kelly: `0.25 × edge / variance`, capped at 5% of NAV
- **Leverage** → inversely scaled with 1h realized vol: low vol = 3x, high vol = 1.5x
- **Stop-loss** → 0.8% below entry (matches your hl-signal-bot proven config)
- **Take-profit** → 1.2% above entry
- **Time stop** → 60s max hold (cascade signals are short-horizon)

**Joe's edge:** this isn't ChatGPT-flavored decision logic. The TP 0.10%, SL 0.08%, 60s timeout heuristics are lifted from your battle-tested hl-signal-bot live-mode config. Judges who probe the parameters will see real experience.

### 3. HL Executor (`agent/executor.py`) — ~180 LOC

Wraps `hyperliquid-python-sdk`. Uses **API wallet** (not master key) — trade-only permission, no withdrawal risk.

```python
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

exchange = Exchange(
    wallet=api_wallet,
    base_url=constants.MAINNET_API_URL,  # or TESTNET for dev
    account_address=MASTER_ACCOUNT_ADDRESS,
)

# Place market order with reduce-only flag respected
result = exchange.market_open(
    name="BTC",
    is_buy=action.side == "long",
    sz=size_btc,
    px=None,  # market
    slippage=0.005,
)
```

Subscribes to user fills WebSocket for instant position confirmation. Reuses your AlphaDrip cascade-engine WS connection pattern.

### 4. Risk Manager (`agent/risk.py`) — ~250 LOC

Runs every 2 seconds independent of signal cadence:

- **Liquidation protection.** Pulls `clearinghouseState` from HL Info API. If `marginUsed / accountValue > 0.4` → auto-deleverage (close 50% of largest position).
- **Daily kill switch.** Persists session NAV in SQLite. If today's drawdown ≤ -2% → halt all signal consumption for 24h. Telegram alert via your existing Makaclaw bot.
- **USYC sweep.** If `freeMargin > 2 × maxOpenMargin` for 5 consecutive checks → withdraw excess to Arc, swap to USYC for yield between trades.
- **CCTP top-up.** If HL margin drops below operating threshold AND Arc USDC balance > top-up size → trigger CCTP V2 burn-and-mint from Arc to Arbitrum.

The kill switch + USYC sweep are what separate this from generic agent demos. Most teams won't bother with idle-capital yield; you've been thinking about this since the November funding-rate-arb work.

### 5. Dashboard (`web/`) — Vercel + Cloudflare Tunnel

**Reused from AlphaDrip:** Bloomberg aesthetic, dark theme, mono fonts, terminal feel.
**New panels:**
- **Live tape** — every signal received, payment tx hash, decision, execution result
- **Position monitor** — current HL exposure, unrealized PnL, margin health
- **Vault status** — USYC parked balance, last sweep, total interest earned
- **Risk gauges** — distance to liquidation, daily PnL vs kill threshold, time-since-last-trade

Frontend pulls from a `/state` endpoint exposed by the agent process. Cloudflare Tunnel routes external traffic to the agent on your home VPS (`protocol: http2` per your existing config).

---

## Circle stack usage

| Primitive | Where used | Why it matters |
|-----------|------------|----------------|
| **USDC** | Every signal payment | Native settlement currency on Arc |
| **EIP-3009** (USDC method) | `transferWithAuthorization` | Gasless signal payment, no approve+transfer dance |
| **CCTP V2** | Arc → HL margin top-up | Cross-chain capital mobility for the agent |
| **Wallets** | API wallet for HL, seller wallet on Arc | Scoped permissions, no key exfiltration risk |
| **Contracts** | AlphaDrip relayer (existing) | EIP-3009 dispatcher already deployed |
| **Paymaster** | Agent's Arc transactions | USDC-denominated gas, no native token to source |
| **USYC** | Idle margin parking | Yield between trades, *most-overlooked primitive in the stack* |

**6/7 primitives.** Only thing skipped is App Kit (we're not building consumer flows).

---

## 14-day execution plan

### Phase 1 — Foundations (May 12–14, 3 days)

- [ ] **D1** Fork AlphaDrip repo to `Makabeez/drip`. Strip the TypeScript consumer CLI. Keep emitter, relayer, contracts intact.
- [ ] **D1** Stand up Python project: `poetry init`, add `hyperliquid-python-sdk`, `web3`, `httpx`, `eth-account`, `sqlmodel`.
- [ ] **D2** Port AlphaDrip consumer CLI from TS → Python (`signal_client.py`). Verify it still extracts 402 challenge + signs EIP-3009 + receives signals from your live emitter.
- [ ] **D2** Set up HL **testnet** API wallet. Fund with test USDC. Place a manual test order via `examples/basic_order.py` from the SDK.
- [ ] **D3** Decision engine v0: hardcoded rules from `hl-signal-bot` proven config. Unit-tested in isolation with fake signals.

### Phase 2 — Agent loop (May 15–17, 3 days)

- [ ] **D4** End-to-end agent loop: signal received → decision → testnet HL order placed. Log every step to SQLite.
- [ ] **D5** Risk manager v1: liquidation protection + daily kill switch. Force-trigger both in tests.
- [ ] **D6** Telegram integration — wire Risk Manager alerts into Makaclaw bot (already running on droplet).

### Phase 3 — Circle stack expansion (May 18–20, 3 days)

- [ ] **D7** CCTP V2 top-up flow. Use Circle Bridge Kit (you have prior PropRail integration). Demo: Arc → Arbitrum cross-chain margin refill.
- [ ] **D8** USYC sweep. Park 70% of idle margin in USYC. Sweep logic + emergency unwind path.
- [ ] **D9** Paymaster integration for agent's Arc-side transactions. All gas paid in USDC.

### Phase 4 — Mainnet + UX (May 21–22, 2 days)

- [ ] **D10** Switch HL executor to **mainnet**. Start with $50 USDC margin only. Confirm fills with real funds.
- [ ] **D10** Bloomberg-aesthetic dashboard live at `drip.baserep.xyz` (Vercel + Cloudflare Tunnel).
- [ ] **D11** Live tape, position monitor, vault status, risk gauges all wired to `/state` endpoint.

### Phase 5 — Submission (May 23–25, 3 days)

- [ ] **D12** Run agent live for 24h. Capture screenshots of real PnL, real txs on Arc explorer, real HL fills.
- [ ] **D13** README in Zama style (banner, shields, ASCII arch, sections). Demo video (3 min max, no audio needed, terminal + dashboard + HL fills + Arc explorer split-screen).
- [ ] **D14** Submit on Canteen platform. Ping Elton Tay (Circle DevRel) on Discord — "shipped v2 on Arc, here's the writeup." Post thread on X tagging `@CanteenApp` `@circle` `@HyperliquidX`.

---

## File delivery norms (from session ops)

- All new files written to WSL via shell heredoc, not notepad
- `pwd` before every destructive op
- Solidity + MQL5 stays Claude-routed per LLM Router rules
- Python infra can route to DeepSeek for non-sensitive enrichment, but **agent core stays Claude** (wallet logic + risk = mainnet logic = Claude-only by your privacy rule)
- Working directory: `C:\Github\drip\` on Windows; `/home/vps/drip/` on droplet for the running agent
- PM2 process name: `drip-agent` (mirror your hugo-copier and poly-news-trader patterns)

---

## What this is NOT

- Not a multi-user product. Single autonomous agent, single operator. Traction = volume of decisions, not user count.
- Not a copy-trading platform. Drip's decisions are its own; it doesn't mirror a leader.
- Not 10x leverage chaos. Max 5x by design. Judges should see prop-firm-grade risk discipline, not gambling.
- Not LLM-decisioned. Decision engine is pure Python heuristics. The "AI" is in the autonomy and adaptiveness, not in calling Claude for every trade. (Optional: add a `reasoning_trace` field that captures *why* every decision was made — research item #1 in the Agora brief, hashable trace for future "reasoning marketplace" tie-in.)

---

## Open questions (for Joe to answer before D1)

1. **Mainnet capital allocation.** $50 is safe; $100–200 makes PnL more demo-able. What's the appetite?
2. **Symbol focus.** BTC-PERP (deepest liquidity, matches AlphaDrip emitter) vs SOL-PERP (you ran Scavenger here, faster cascades) vs both?
3. **Reasoning trace.** Add it for innovation points (Agora research item #1 hook), or skip to ship faster?
4. **Telegram alerts.** Wire to Makaclaw or new dedicated bot? Makaclaw is simpler but mixes audit streams.

---

## Stretch goals (only if D1–D11 finish early)

- **Reasoning trace marketplace hook.** Hash every decision trace, pin to Irys, hash-on-Arc. Sets up next hackathon's narrative.
- **Public signal mirror endpoint.** Anyone can subscribe to Drip's own signals (it becomes a signal *emitter* in addition to consumer). Register as a Polymarket-style builder for fee accrual.
- **Multi-symbol expansion.** SOL, ETH alongside BTC. Diversification + more demo material.

---

*Drip · Agora Agents Hackathon · May 11–25, 2026*
*github.com/Makabeez/drip · drip.baserep.xyz (target)*
