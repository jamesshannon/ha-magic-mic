# Build Sequence & Prioritization

> **Order and proof, not design.** The 20+ topic docs say *what* to build and *why*; this
> says *in what order* and *how we prove each step earns its place*. Companion to
> [`../PRODUCT_PLAN.md`](../PRODUCT_PLAN.md) §8 (feature phasing), §5.6 (shared primitives),
> and §7 (path-to-core). Cross-refs: [`evaluation.md`](evaluation.md) (the measurement rig),
> [`prompt-context.md`](prompt-context.md), [`find-entities.md`](find-entities.md),
> [`learning.md`](learning.md), [`scheduling-model.md`](scheduling-model.md).

---

## The three prioritization axes

Most features are *subjectively* awesome — that's not in question. We prioritize for a
**combination** of:

1. **Necessary scaffolding / testing / observability** — the component shell, the identity
   and store seams, and (load-bearing) the **measurement instrument**.
2. **Features that objectively *prove* value** — the ones that move the headline numbers the
   harness reports: **tokens saved, generations prevented, turns prevented, hassil-intervention
   rate increased**. Subjective delight is real, but the harness is how we prove we're making
   things *faster, or at least not slower*.
3. **Low-hanging "magic"** — cheap, high-delight features (some literally free by inheritance).

There is **no strict dependency order** beyond scaffolding and a few obvious prerequisites,
and **no feature must be 100% complete before the next** — each enters as a **thin vertical
slice** (its metric-moving or delight-delivering core) with the long tail explicitly deferred.

---

## Two organizing principles

**Measurement precedes optimization.** Axis 2 says "saved / prevented / increased" — against
what? Against the **stock anthropic fork, unoptimized**. So the instrument that captures the
four numbers, plus **one baseline run**, is the real product of Wave 0. Every later change is
then a *measured delta*, at **fixed or rising task-success** (never a resource win bought with
a quality loss — [`evaluation.md`](evaluation.md) Part D). The eval **scorecard** *is* this
dashboard; build one instrument, not two.

**Seam early, engine just-in-time.** Thread a primitive's *seam* on day one (cheap, avoids a
retrofit — `resolve_user()`, the user-keyed `Store`), but build the *engine* only when its
first consumer lands (the delivery engine waits for reminders). `find_entities` is the
exception that comes early — it already has a Wave-1 consumer *and* four downstream ones.

**Convergence worth naming:** the features that prove value (axis 2) are almost exactly the
§5.6 shared primitives — prompt-context moves tokens, `find_entities` moves turns and feeds
local routing, learning moves local-rate and turns. "Primitives first" and "prove value
first" are the *same* instruction, so they don't compete.

**Design to the integration boundary, split just-in-time.** Build as **one integration with
modules**, not many integrations — but write every cross-capability contract *as if* it
already crosses an integration boundary (registration + discovery, no private cross-imports;
register tools via `async_register_api`). This enforces the contract, is the core-contribution
shape, and makes a later split near-free — while avoiding per-integration boilerplate before
contracts are proven. Promote a module to a standalone integration (a memory provider, a
`FrictionResolver` provider) **JIT**, once its contract stabilizes and third-party
pluggability has concrete value (PRODUCT_PLAN §6.2). Corollary: multi-provider prompt-budget
pressure is a thing to **discover and mitigate here** (realtime provider filtering, §6.2)
*before* the extension contract freezes in core — the proving-ground payoff.

---

## The value dashboard (= [`evaluation.md`](evaluation.md) Part E scorecard)

| Metric | Instrumented at | Moved by |
|---|---|---|
| **Tokens** (+ cache_read/creation) | `entity.py` `usage` — already emitted | prompt-context §5.2 (roster dump → skeleton + conditioned names) |
| **Generations / request** | the chat loop (count tool_use round-trips) | `terminal_intent` field, server-side web_search, fewer disambig loops |
| **Turns / task** | task-level trace | `find_entities`, learning (aliases remove clarification) |
| **Hassil-intervention rate** (% resolved locally) | pipeline / `prefer_local` path | `prefer_local` ON, contributed intents, aliases, command aliases |

Reported as the **outcome scorecard** (resolved-locally / LLM-correct / after-clarification /
wrong / "don't understand"), tracked as a *movement* across changes.

---

## Component vs. core (what goes where)

The line falls out of the architecture: **LLM-path capability → component; no-AI local-path
help + the sub-agent pipeline layer → core.** The set that genuinely *needs* core is small:

- The **fuzzy fallback inside the intent match layer** (§2.4) — as an *LLM tool* `find_entities`
  lives in the component; as the *match-layer fix* that helps the local path it's a core change.
- **Localized sentences** for any contributed intent (`home-assistant/intents`, §5.7).
- The **STT/TTS/pipeline/satellite-firmware layer** — barge-in/stop-words, `prefer_local_intents`,
  the `assist_satellite` output surface. *Wire, don't build.*

Everything else builds in the component, shaped like core `llm.py` platforms. **Never block
component iteration on a core PR** — contributions (§7) are a lagging, à-la-carte track.

---

## The waves

Each wave carries something from all three axes, so there's always a measured result *and*
something demonstrable. Tags: **[C]** component · **[core]** needs a core change/PR ·
**[HA]** HA-owned toggle we depend on.

### Wave 0 — Skeleton + instrument + baseline
*Axis 1; the instrument for axis 2.*

- **[C]** Stand up the **Testbed Proxy** ([`testbed-proxy.md`](testbed-proxy.md)):
  `magic_mic.internal.claude` (near-upstream copy of the `anthropic` component, registered as
  its own agent = the **baseline**) + `magic_mic.testbed` (neutral proxy that wraps
  `chat_log.llm_api` and delegates the loop to the inner agent). At Wave 0 the wrapper is
  **pass-through**: identical behavior to the baseline, but with the trace hook and
  tool-interception seam in place. Inherits device control, streaming, and (Claude-specific,
  optional) **server-side web_search** ([`web-search.md`](web-search.md)).
- **[C]** Thread `resolve_user()` + user-keyed `Store` **empty** through the request (§5.1);
  establish the `capabilities/` `llm.py`-shaped contract (§5.5).
- **[C]** Tier-A pytest scaffold + the **Tier-B golden-set runner** (seed cases from
  `VISION.md`'s transcripts) + the **value dashboard** (capture `usage` tokens/cache; count
  generations in the chat loop; record local-vs-LLM routing; trace turns).
- **Run the baseline** — stock full-roster prompt, `prefer_local` OFF. *This is the number
  everything is measured against.*
- **[C]** Bank the free magic: enable `web_search`/`web_fetch` + auto-fill `user_location`
  from `hass.config`.

*Proves:* the harness runs; you have a baseline. *Component-only; nothing to contribute yet.*

### Wave 1 — Prove the thesis
*Axis 2, the core bet — and it's cheap to reach.*

- **[C]** **prompt-context §5.2** — taxonomy skeleton + request-conditioned name injection,
  retire the roster dump ([`prompt-context.md`](prompt-context.md)). → measure **Δtokens / TTFT**.
- **[C] / [core]** **`find_entities`** fuzzy in-match fallback — component tool first; the
  match-layer version opens the first **[core]** track ([`find-entities.md`](find-entities.md)).
  → measure **Δturns** (disambiguation success).
- **[HA]** Flip **`prefer_local_intents` ON** (§2.9) → measure **Δhassil-intervention rate**.
- **Testing gate (tool interception):** the Wave 0 equivalence test covers the *pass-through*
  proxy only. **Before any tool filtering / replacement / interception is committed** (the
  wrapped `TestbedAPI.async_call_tool` routing, e.g. `find_entities` → the fuzzy resolver), add
  a conversation-turn test driving a `tool_use` response that asserts the interception: the
  baseline executes the stock tool; the testbed routes/rewrites it. See
  [`testbed-proxy.md`](testbed-proxy.md).

*Proves:* the token/turn/local claims — the **go/no-go** on the design's central bet.

### Wave 2 — Bank cheap magic + first learning + first contribution
*Axis 3 + axis 2 dual-payoff.*

- **[C]** Weather forecast tool ([`weather.md`](weather.md)); what's-playing local intent
  ([`music-playback.md`](music-playback.md)); **notebook memory** `remember`/`recall`/`forget`
  ([`memory.md`](memory.md)) — demanded, low-risk delight (Store seam already threaded).
- **[C]** **Learning v1** — the offer engine + two resolvers: `add_alias` (rides the
  `find_entities` friction signal) + the **command-alias** resolver ([`learning.md`](learning.md)).
  → measure **Δhassil-rate + Δturns + utterances-moved-off-cloud**.
- **[core]** **First capability PR:** `find_entities` / fuzzy resolution (least controversial,
  §7). The eval/trace harness is **[core] merge-first**, available to land any time from here.

*Proves:* learning moves the metrics; the contribution pipeline works.

### Wave 3 — The heavy magic (scheduling spine)
*Axis 3 high-value, heaviest infra — the VISION Tier-1 hooks.*

- **[C]** **Delivery engine + scheduling substrate** (the 4–5-consumer primitives,
  [`scheduling-model.md`](scheduling-model.md)).
- **[C]** Reminders (content-free announce + pull-to-read), **conditional reminders**
  (ephemeral-automations — the "remind me in an hour if I haven't closed the door" hook),
  calendar-write.
- **Testing gate:** the **time/restart/DST simulation harness** ([`evaluation.md`](evaluation.md)
  Part G) becomes required here — the highest-trust-stakes deterministic surface.
- **[core]** later: calendar-write, then reminders (§7 order).

*Proves:* the headline VISION demos.

### Wave 4 — Proactive & multi-user
*Phase 4, deferred.*

- **[C]/[HA]** `assist_satellite.start_conversation` nudges; voice-ID → per-user context;
  off-satellite push + actionable-notification ack ([`scheduling-model.md`](scheduling-model.md),
  [`speaker-identification.md`](speaker-identification.md)). **[core]** long-term memory is the
  last, most-opinionated contribution.

---

## What this reorders vs. PRODUCT_PLAN §8

§8 is *feature-value* phasing; this braid corrects two things it under-weighted:

- It pulls the **token/turn/local proof** (prompt-context, `find_entities`, learning) to the
  front as the thing that de-risks the whole bet — §8 tucked these into "Phase 0 skeleton" prose.
- It drops **notebook-memory** from "Phase 2 differentiator" to "cheap delight in Wave 2"
  (memory.md argues it's demanded-but-modest for a tactical home), and keeps the heavy
  scheduling magic last because it's the most infra for the demo payoff.

Component scaffolding really is light — you inherit anthropic's `config_flow`/`entity`/streaming;
the work is the capabilities + the harness, not the shell.

---

## Open sequencing questions

- Exact Wave-0 dashboard scope: how much trace enrichment (Part A) is needed before the
  baseline is trustworthy vs. added incrementally.
- Whether `find_entities`' **[core]** match-layer PR leads or trails its component tool (the
  component tool unblocks Wave 1; the core PR can follow once the pattern is proven).
- Reuse-vs-build for the dev harness (DeepEval-shaped vs. hand-rolled) — [`evaluation.md`](evaluation.md)
  Part H; a Wave-0 decision but not a blocker.
- Where the offer/learning engine module sits relative to `capabilities/` (it gates *other*
  capabilities' fixes — [`learning.md`](learning.md)).
