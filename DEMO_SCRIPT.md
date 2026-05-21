# Drip — Demo Video Script (D8 recording)

> **Target duration:** 2:30–3:00 (3:00 hard cap per brief)
> **Tool:** Loom free plan (5 min limit, fine) OR YouTube unlisted
> **Resolution:** 1920×1080 if possible, 720p acceptable (Loom free caps here)
> **Audio:** Plain mic + script-as-you-go is fine. Subtitles optional but recommended (`Add captions` button in Loom post-record).
>
> **Optimization target:** A judge with 30 seconds of attention should know what Drip is. A judge with 3 minutes of attention should know why it deserves to win.

---

## Pre-recording checklist (5 min before hitting record)

```bash
# 1. Drip is live and healthy
curl -s https://drip.baserep.xyz/health
# Expected: {"ok":true,"service":"drip-dashboard"}

# 2. Agent is trading (not in kill switch)
curl -s https://drip.baserep.xyz/state | python3 -c "
import sys, json
s = json.load(sys.stdin)
print('Kill tripped:', s['risk']['kill_switch_tripped'])
print('Signals:', s['counters']['signals_received'])
print('Trades:', s['counters']['trades_opened'])
print('Position:', s['position']['side'] if s['position'] else 'flat')
"
# Expected: Kill tripped: False, signals/trades incrementing

# 3. CCTP cooldown is clear (so the trigger button works for demo)
poetry run python -c "
import sqlite3, os, time
from dotenv import load_dotenv
load_dotenv()
db = sqlite3.connect(os.environ['SQLITE_PATH'])
last = db.execute('SELECT MAX(started_at_ms) FROM cctp_bridges').fetchone()[0]
if last:
    age = int(time.time() - last/1000)
    print(f'Last bridge: {age}s ago (cooldown is 60s)')
"

# 4. Pre-warm browser tabs:
#   - https://drip.baserep.xyz (main demo)
#   - https://github.com/Makabeez/drip (will show briefly at end)
#   - https://testnet.arcscan.app/ (will be opened by clicking a tx)

# 5. Close anything noisy (Telegram desktop, email, Slack)
# 6. Put phone on silent
# 7. Brief warmup: read the script through once before recording
```

---

## Shot list — second-by-second plan

### Opening (0:00 – 0:25) — Hook + what is Drip

**Visual:** Land on https://drip.baserep.xyz. Browser zoom level so the top 4 tiles + trade tape are visible without scrolling. **Don't scroll yet.**

**Script:**

> "This is Drip — an autonomous Hyperliquid perps trading agent that pays for its own signals on Arc.
>
> What you're seeing is live. The agent is right now buying BTC trading signals at half a cent each via x402 micropayments on Arc Testnet, executing on Hyperliquid, managing its own risk, and — when its margin runs low — bridging USDC across chains via Circle CCTP V2.
>
> Three Circle primitives, one continuously running agent."

**On-screen cues:**
- Point at uptime ticker in top right ("running for X hours")
- Point at signals/trades counters in the middle row

---

### Section 1: Live trading (0:25 – 1:00) — Traction proof

**Visual:** Hover over the trade tape. Click ONE Arc tx hash in the trade tape — it opens Arc explorer in a new tab. **Switch to the explorer tab briefly.**

**Script:**

> "Every row in this trade tape is a real Hyperliquid fill, with a real Arc transaction hash proving the signal was paid for.
>
> Let me click one of these — [click `0x...` link] — this opens Arc explorer, and you can see the actual `transferWithAuthorization` call on the USDC contract, 0.005 USDC moving from the agent to the signal seller. Confirmed in half a second.
>
> Right now there are over twelve thousand of these payments and sixteen hundred trades. The agent has been running unattended."

**Then switch back to dashboard.**

---

### Section 2: Risk discipline (1:00 – 1:30) — The "agent IS accountable" angle

**Visual:** Hover over the RISK tile at top right, then scroll down to show the signal stream and recent HOLD reasons.

**Script:**

> "The thing that makes this an agent and not just an automation is that it's economically accountable. Look at the risk panel — the agent has a hard 5% daily NAV kill switch and a 40% margin utilization liquidation guard.
>
> Yesterday, the daily kill switch fired correctly when noisy signals pushed PnL below the threshold. The agent stopped trading AND — critically — stopped paying for signals it couldn't use. Halting spending in lockstep with halting trading.
>
> In the signal stream you can see every decision the agent makes, including the HOLD reasons. 'low_confidence,' 'same_direction_open,' 'daily_kill_switch.' Every decision is hashable and persisted."

---

### Section 3: CCTP V2 live bridge (1:30 – 2:30) — The headline differentiator

**Visual:** Scroll down to the **CCTP BRIDGES panel**. Show the existing bridge rows briefly. Then click the **TRIGGER 1 USDC TOP-UP** button.

**Script (while clicking):**

> "Now here's the part most agents can't do. When the agent's margin on Hyperliquid runs low, it autonomously bridges USDC from its operational wallet on Arc to its settlement wallet on Arbitrum Sepolia — Hyperliquid's settlement chain.
>
> I'll trigger it now manually for the demo — [click button, accept confirm] —
>
> The agent fires the four-step Circle CCTP V2 flow: approve USDC on Arc, depositForBurn, poll Circle's IRIS attestation service, then receiveMessage on Arbitrum Sepolia. The whole thing typically takes about 15 seconds end-to-end on testnet."

**Wait for the BRIDGING → SUCCESS transition (~15-30s). If it's still running at 2:00, calmly say:**

> "While that completes, let me show you the source code briefly."

**Then switch to GitHub tab.** Show repo top, scroll to README's CCTP section, show the on-chain proof table.

**When bridge completes, switch back to dashboard:**

> "Done — the new row shows as SUCCESS. Click the mint tx link and you can verify on Arbiscan: 1 USDC just arrived at the agent's Arbitrum Sepolia address. No human in the loop. Real Circle CCTP V2. End-to-end."

---

### Closing (2:30 – 2:55) — Call to action

**Visual:** Back to dashboard top, then briefly show:
- The footer line: `DRIP · github.com/Makabeez · Agora Agents Hackathon · drip.baserep.xyz`

**Script:**

> "Drip is open source under MIT. The live URL drip.baserep.xyz is publicly accessible — click around, click the bridge trigger yourself if you want.
>
> Built solo for the Agora Agents Hackathon. Three Circle primitives, one autonomous agent. Thanks for watching."

---

## Recording tips

- **Pre-record once** as a throwaway to find pacing issues. Watch it back at 1.5x to catch dead air.
- **Speak slower than feels natural.** The brief says 3 min max — better to have 2:45 of clean audio than 3:00 of rushed.
- **Don't read the script verbatim** — keep it on a second monitor or sticky note, but speak conversationally. The script is a safety net, not a teleprompter.
- **If you stumble, pause for 3 seconds and re-deliver the sentence.** You can trim out the bad take in Loom's editor (the timeline scrubber lets you cut clips).
- **Avoid filler words.** "So...", "basically...", "you know..." add 10-15 seconds and dilute punch.
- **Don't apologize for anything on the dashboard** — no "this might look messy" or "the PnL is currently negative." Just show what's there with confidence.

---

## If something breaks during recording

**Scenario A: Bridge fails.** Highlight the past two SUCCESS rows in the panel instead. Pivot to "here are the previous bridges — same tx hashes are clickable on Arbiscan. The button you saw fires the same logic."

**Scenario B: Dashboard is empty (e.g. agent just restarted).** Skip the trade tape section. Spend more time on the architecture in the README (we'd switch to GitHub earlier).

**Scenario C: Kill switch is tripped during recording.** Lean into it — "and right here you can see the kill switch in its tripped state. The agent is correctly refusing to trade until tomorrow's UTC rollover. This is the risk discipline working as designed."

---

## Post-record checklist

- [ ] Watch the full recording back at 1x speed before publishing
- [ ] Trim opening dead air and closing dead air
- [ ] Add captions via Loom's auto-generate (judges may watch muted)
- [ ] Set video to public (NOT just "anyone with link" — Loom has a separate Public toggle)
- [ ] Copy share link, paste into SUBMISSION.md video field
- [ ] Watch the published version once to verify nothing's broken
- [ ] Update SUBMISSION.md with the video link
- [ ] Commit: `git commit -am "SUBMISSION: add demo video link"` + push

---

## Hard constraints (don't violate)

- **Never exceed 3:00.** Cut content if needed.
- **Never apologize.** Confidence is part of the demo.
- **Don't read out long tx hashes.** Say "this hash here" and let the screen do the talking.
- **Don't open private windows.** Use a clean browser profile or close Telegram/email tabs first.
- **Don't show your X profile or email address on screen** unless you want it public.
