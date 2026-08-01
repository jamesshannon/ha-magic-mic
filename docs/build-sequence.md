# Build Sequence & Prioritization

> **Order and proof, not design.** The 20+ topic docs say *what* to build and *why*; this
> says *in what order* and *how we prove each step earns its place*. Companion to
> [`../PRODUCT_PLAN.md`](../PRODUCT_PLAN.md) §8 (feature phasing), §5.6 (shared primitives),
> and §7 (path-to-core). Cross-refs: [`evaluation.md`](evaluation.md) (the measurement rig),
> [`prompt-context.md`](prompt-context.md), [`find-entities.md`](find-entities.md),
> [`learning.md`](learning.md), [`scheduling-model.md`](scheduling-model.md).

> **Progress (2026-07-29):** **Wave 0 is complete** and merged to `main` (see the Wave 0
> section for the exit number). **Wave 1 — Prove the thesis** is next.

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
retrofit — `get_resolved_user()`, the explicitly scoped `Store`), but build the *engine* only
when its first consumer lands (the delivery engine waits for reminders). `find_entities` is
the exception that comes early — it already has a Wave-1 consumer *and* four downstream ones.

Two seams need hardening before more Wave-1 behavior lands. First, `"default"` means an
unidentified caller with household scope only, not a synthetic personal user. Second,
`MagicMicChatLog` remains the live-interaction object, with deterministic session state
(pending operation + undo journal) behind a `conversation_id`-keyed sidecar because HA clones
the dataclass between turns. The blocking implementation checklist is
[`../TODO.md`](../TODO.md).

Foundation sections 1-5 now implement identity/scope, the ChatLog sidecar, immutable pending
operations, the two-stage tool-policy kernel, and the bounded undo seam. The policy layer
deliberately preserves unclassified tools while tracing them; registry coverage and a
fail-closed unknown default are later deployment gates, not claims made by this POC. Undo is
likewise selective: possible mutations without typed metadata become explicit barriers, and
locally handled hassil mutations remain outside the claim until the core intent chokepoint
can emit the same outcome contract. The final full-suite gate completed on 2026-08-01 with
formatting, lint, and all 146 tests passing. Wave 1 may
continue; the documented undo coverage boundaries remain deliberate follow-on work, not a
foundation blocker.

**Convergence worth naming:** the features that prove value (axis 2) are almost exactly the
§5.6 shared primitives — prompt-context moves tokens, `find_entities` moves turns and feeds
local routing, learning moves local-rate and turns. "Primitives first" and "prove value
first" are the *same* instruction, so they don't compete.

**Build one proving ground with honest internal boundaries.** Modules separate concerns and
keep provider transport out of deterministic logic; they are not rehearsals for one-module-
per-core-integration or promises of copy/paste migration. Do not add registration/discovery
machinery until the experiment has a real independent provider. What moves upstream is the
evidence, behavior, tests, schemas, and proposed seam, adapted with core maintainers. Tool RAG
and multi-provider pressure are things to measure here before proposing a core capability-
selection contract (PRODUCT_PLAN §6.2).

**Feature scoping remains just-in-time.** The flagship feature docs contain explicit
“Build-time scoping gate” sections for interaction-specific semantics, failure behavior, and
acceptance cases. Those questions must close before that feature's implementation slice, but
they are not reasons to block unrelated Wave 1 work or invent more shared foundations now.

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

This is the development/corpus scorecard, not deployed product telemetry. Whether people
attempt, complete, ignore, recover from, and reuse the VISION moments in real homes is a
separate post-deployment measurement layer defined in
[`telemetry.md`](telemetry.md).

---

## Proving ground vs. core (what each is for)

Build and measure the coherent experience in the component. Core work begins only when a
finding requires an existing core seam or maintainers agree that the evidence justifies a
new one:

- The **fuzzy fallback inside the intent match layer** (§2.4) — as an *LLM tool* `find_entities`
  lives in the component; as the *match-layer fix* that helps the local path it's a core change.
- **Localized sentences** for any contributed intent (`home-assistant/intents`, §5.7).
- The **STT/TTS/pipeline/satellite-firmware layer** — barge-in/stop-words, `prefer_local_intents`,
  the `assist_satellite` output surface. *Wire, don't build.*

Do not block component iteration on a core PR, but do not describe upstreaming as an
à-la-carte file-copy track either. Shared foundations may need to land before individual
features. Tags below describe where experimentation or a prerequisite change occurs, not a
preselected PR/package boundary.

---

## The waves

Each wave carries something from all three axes, so there's always a measured result *and*
something demonstrable. Tags: **[C]** component · **[core]** needs a core change/PR ·
**[HA]** HA-owned toggle we depend on.

### Wave 0 — Skeleton + instrument + baseline ✅ complete (2026-07-29)
*Axis 1; the instrument for axis 2.*

**Status: complete.** Every bullet below landed. The locked baseline is
`evals/results/wave0_baseline.json` (`claude-haiku-4-5`, `prefer_local` OFF, pre-magic
roster): **21 correct / 4 wrong / 0 unresolved**, 44 generations. Delivered beyond the plan:
the eval's fixture home is backed by executable entities plus a mocked satellite (so timers
and the full tool roster run headless), the scorer takes `any_of` acceptable outcomes to
absorb LLM non-determinism, and `web_search`/`web_fetch` ship on with `user_location` off
(privacy-first, config-supplied).

- **[C]** Stand up the **Testbed Proxy** ([`testbed-proxy.md`](testbed-proxy.md)):
  `magic_mic.internal.claude` (near-upstream copy of the `anthropic` component, registered as
  its own agent = the **baseline**) + `magic_mic.testbed` (neutral proxy that wraps
  `chat_log.llm_api` and delegates the inherited provider loop). At Wave 0 the wrapper was
  **pass-through**: identical behavior to the baseline, but with the trace hook and
  tool-interception seam in place. The foundation pass now filters and rechecks classified
  tools while delegating allowed calls to the original API instance. It inherits device
  control, streaming, and (Claude-specific, optional) **server-side web_search**
  ([`web-search.md`](web-search.md)).
- **[C]** Thread `get_resolved_user()` + explicitly scoped `Store` **empty** through the
  request (§5.1); establish provider-neutral internal contracts without speculative
  per-capability integration scaffolding (§5.5).
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

**Entry gate:** complete the pre-Wave-1 foundation checklist in [`../TODO.md`](../TODO.md).
This freezes the identity/scope contract, ChatLog session-state seam, and two-stage tool policy
before more capabilities depend on the earlier placeholder interfaces.

- **[C]** **prompt-context §5.2** — taxonomy skeleton + request-conditioned name injection,
  retire the roster dump ([`prompt-context.md`](prompt-context.md)). → measure **Δtokens / TTFT**.
- **[C]** **Capability-selection shadow mode** — build the provider-neutral catalog,
  deterministic availability filter, relevance retriever, dependency/budget assembler, and
  selection trace, but do not remove tools yet. Compare the proposed per-turn API with the
  tool actually used by the full-roster baseline. Enforce selection only after
  recall@budget and end-to-end task-success gates pass
  ([`capability-selection.md`](capability-selection.md)).
- **[C] / possible core seam** **`find_entities`** fuzzy in-match fallback — component tool
  first; use its measurements to motivate a match-layer discussion with core maintainers
  ([`find-entities.md`](find-entities.md)).
  → measure **Δturns** (disambiguation success).
- **[HA]** Flip **`prefer_local_intents` ON** (§2.9) → measure **Δhassil-intervention rate**.
- **Testing gate (tool interception):** the Wave 0 equivalence test covers the *pass-through*
  proxy only. **Before any tool filtering / replacement / interception is committed** (the
  wrapped `TestbedAPI.async_call_tool` routing, e.g. `find_entities` → the fuzzy resolver), add
  a conversation-turn test driving a `tool_use` response that asserts the interception: the
  baseline executes the stock tool; the testbed routes/rewrites it. See
  [`testbed-proxy.md`](testbed-proxy.md).

*Proves:* the token/turn/local claims — the **go/no-go** on the design's central bet.

### Wave 2 — Bank cheap magic + first learning + upstream evidence
*Axis 3 + axis 2 dual-payoff.*

- **[C]** Weather forecast tool ([`weather.md`](weather.md)); what's-playing local intent
  ([`music-playback.md`](music-playback.md)); **notebook memory** `remember`/`recall`/`forget`
  ([`memory.md`](memory.md)) — demanded, low-risk delight (Store seam already threaded).
- **[C]** **Learning v1** — the offer engine + two resolvers: `add_alias` (rides the
  `find_entities` friction signal) + the **command-alias** resolver ([`learning.md`](learning.md)).
  → measure **Δhassil-rate + Δturns + utterances-moved-off-cloud**.
- Prepare the `find_entities` results, tests, and proposed match-layer seam for maintainer
  discussion (§7). The eval/trace work can be discussed independently if it proves broadly
  useful; neither is assumed to land unchanged.

*Proves:* learning moves the metrics and produces evidence suitable for upstream design
discussion.

### Wave 3 — The heavy magic (scheduling spine)
*Axis 3 high-value, heaviest infra — the VISION Tier-1 hooks.*

- **[C]** **`ScheduledItemStore` first, then the delivery engine + scheduling substrate**
  (the 4–5-consumer primitives, [`scheduling-model.md`](scheduling-model.md)). Reminders,
  alarms, scheduled commands, and ephemeral automations must not grow separate durable
  schemas.
- **[C]** Reminders (content-free announce + pull-to-read), **conditional reminders**
  (ephemeral-automations — the "remind me in an hour if I haven't closed the door" hook),
  calendar-write.
- **Testing gate:** the **time/restart/DST simulation harness** ([`evaluation.md`](evaluation.md)
  Part G) becomes required here — the highest-trust-stakes deterministic surface.
- **Possible core work later:** calendar-write and reminder/scheduling seams, after the
  proving ground establishes their contracts (§7).

*Proves:* the headline VISION demos.

### Wave 4 — Proactive & multi-user
*Phase 4, deferred.*

- **[C]/[HA]** `assist_satellite.start_conversation` nudges; voice-ID → per-user context;
  off-satellite push + actionable-notification ack ([`scheduling-model.md`](scheduling-model.md),
  [`speaker-identification.md`](speaker-identification.md)). Long-term memory remains the
  most opinionated candidate for eventual core discussion.

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
- When to take the measured `find_entities` result and proposed match-layer seam to core
  maintainers; the component experiment unblocks Wave 1 without presupposing PR shape.
- Reuse-vs-build for the dev harness (DeepEval-shaped vs. hand-rolled) — [`evaluation.md`](evaluation.md)
  Part H; a Wave-0 decision but not a blocker.
- Where the offer/learning engine module sits relative to `capabilities/` (it gates *other*
  capabilities' fixes — [`learning.md`](learning.md)).
