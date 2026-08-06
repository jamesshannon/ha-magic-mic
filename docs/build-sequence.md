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
can emit the same outcome contract. The durable-store seam now uses locked row mutations over
validated, capability-namespaced JSON records; whole-scope replacement is not exposed. Wave
1 may continue; the documented undo and storage-backend boundaries remain deliberate
follow-on work, not foundation blockers.

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
| **Tokens** (+ cache_read/creation) | `entity.py` `usage` — already emitted | prompt-context §5.2 (roster dump → entity summary + conditioned names) |
| **Generations / request** | the chat loop (count tool_use round-trips) | `terminal_intent` field, server-side web_search, fewer disambig loops |
| **Model TTFT / round duration** | provider-round timing extension: `GenerationRecord.ttft_ms` / `duration_ms` per round, aggregated per run into p50/p95 by `Scorecard.latency` | prompt-context, capability selection, fewer tool loops |
| **Turns / task** | multi-turn text trajectory driver (planned with first clarification) | `find_entities`, learning (aliases remove clarification) |
| **Hassil-intervention rate** (% resolved locally) | `evals/harness/local_first.py`: faithful prefer-local path (recognize + CONTROL filter), off-cloud rate as routed-locally count | `prefer_local` ON, contributed intents, aliases, command aliases |
| **Voice TTFT/TTLT + spoken duration** | controlled pipeline plus labelled real-engine profile (planned with first pipeline-owned feature) | prompt/context latency, streaming, TTS, local-first routing |

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
roster), rescored through R23: **15 correct / 4 wrong / 6 unjudged / 0 unresolved**, 44
generations. Delivered beyond the plan:
the eval's fixture home is backed by executable entities plus a mocked satellite (so timers
and the full tool roster run headless), and the scorer takes `any_of` acceptable outcomes to
absorb LLM non-determinism. The stored baseline was captured with `web_search` and
`web_fetch` enabled; current installs default both provider options off.

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
- **[C]** Expose Claude's native `web_search` and `web_fetch` as independent provider
  options. They default off; enabling one causes Claude to include that server-side tool.

*Proves:* the harness runs; you have a baseline. *Component-only; nothing to contribute yet.*

### Wave 1 — Prove the thesis
*Axis 2, the core bet — and it's cheap to reach.*

**Entry gate:** complete the pre-Wave-1 foundation checklist in [`../TODO.md`](../TODO.md).
This freezes the identity/scope contract, ChatLog session-state seam, and two-stage tool policy
before more capabilities depend on the earlier placeholder interfaces.

- **[C]** **prompt-context §5.2** — entity summary + request-conditioned name injection,
  retire the roster dump ([`prompt-context.md`](prompt-context.md)). → measure **Δtokens**;
  the provider-round timing substrate (per-round TTFT/duration, aggregated to p50/p95 by
  `Scorecard.latency`) is in place, so a **TTFT** claim now needs an A/B across configs.
- **[C]** **Capability-selection shadow mode** — build the provider-neutral catalog,
  deterministic availability filter, relevance retriever, dependency/budget assembler, and
  selection trace, but do not remove tools yet. Compare the proposed per-turn API with the
  tool actually used by the full-roster baseline. Enforce selection only after
  recall@budget and end-to-end task-success gates pass
  ([`capability-selection.md`](capability-selection.md)).
- **[C] / possible core seam** **`find_entities`**: both consumers built
  ([`find-entities.md`](find-entities.md)). Consumer 2 (the decoupled lookup tool) ships in
  `capabilities/entities.py`, wired into the testbed roster. Consumer 1 (the fuzzy fallback
  in the match layer) ships as `capabilities/match_fallback.py`, interposed at the proxy's
  tool executor: an intent's exact NAME miss is fuzzy-resolved and retried with the canonical
  `entity_id`, or returns candidates for the model to ask about. This is the component-side
  evidence for the proposed core match-layer change (behind an opt-in `fuzzy` constraint);
  use its measurements to motivate that discussion with core maintainers. Direct resolution
  uses the single-turn runner; the scripted multi-turn driver already gates **Δturns** and
  disambiguation-recovery claims.
- **[HA]** Recommend **`prefer_local_intents` ON** (§2.9), which the 2026-08-05 verdict in the
  exit checklist does. The gate was that the combined local-first text driver record both the
  HASSIL intervention and the final action before the recommendation, so that
  **Δhassil-intervention rate** could be read without hiding fallback failures; `local_first.py`
  does both. Every other driver keeps running the off configuration, which is what proves the
  model still handles the commands HASSIL would have taken
  ([`evaluation.md`](evaluation.md#both-routing-configurations-stay-covered-an-invariant-not-a-coincidence)).
- **[C] Testing gate (tool interception):** driven conversation tests now feed a provider
  `tool_use` through the complete baseline and testbed loops. They prove stock baseline
  execution, allowed proxy execution, private undo-outcome stripping, execution-time denial
  of a hidden tool, and provider follow-up after both success and denial. Keep this boundary
  test when adding tool replacement or routing such as `find_entities`; unit seam tests around
  `TestbedAPI` do not cover the ChatLog/provider lifecycle. See
  [`testbed-proxy.md`](testbed-proxy.md). Fault-injection cases also require that a tool
  scheduled immediately before provider failure, a tool already blocked when the stream
  fails, and a tool running when the outer request is cancelled are all cancelled and joined
  before resolved identity is cleared, with no late work. A cancelled possible mutation must
  record its conservative undo barrier during that cleanup, whether cancellation arrives
  before or after the external effect point.
- **[C] Testing gate (live comparisons):** use one paired pass over affected cases during
  development, with alternating arm order and per-case deltas. Escalate changed or
  decision-relevant cases to three trials. Run the full corpus for broad behavior changes
  and locked wave or release artifacts; repeat the full corpus only for a broad go/no-go
  decision that targeted cases cannot represent
  ([`evaluation.md`](evaluation.md#live-comparison-cadence)).

**Exit checklist (as of 2026-08-05).** The capabilities are built; the wave closes on the
*measurement* gates, not the code. "Built" means the mechanism ships and is tested; "proven"
means the go/no-go read is recorded. Two flips are held off on purpose until their gate passes.

Built and closed:

- [x] **Foundation entry gate** cleared: the pre-Wave-1 checklist landed and `TODO.md` is
  retired (identity/scope contract, ChatLog session-state seam, two-stage tool policy frozen).
- [x] **`find_entities`, both consumers** ([`find-entities.md`](find-entities.md)): Consumer 1
  (match-layer fallback) and Consumer 2 (lookup tool) share one resolve step with a
  requesting-room preference; the resolver micro-benchmark pins a `decoy` / `near-miss`
  caution regime at zero false-resolves.
- [x] **Timing substrate**: per-round TTFT/duration aggregate to run-level p50/p95, so a TTFT
  claim is now an A/B, not a guess.
- [x] **Testing gate (tool interception)**: driven baseline/testbed conversation tests plus
  fault-injection (cancel-and-join, conservative undo barrier) are in place.

Open gates (each blocks the wave's go/no-go):

- [x] **prompt-context Δtokens verdict**: closed 2026-08-05, negative for Tier 2.
  `DEFAULT_NAME_INJECTION` is now **off**. The A/B was decisive in the unexpected direction:
  1.45x total spend against summary-only (1.73x prompt-side) at identical task success, because
  per-turn names sit inside the single cached system block and re-prefill it every turn. Two
  structural findings say a better corpus would not have changed it. No tool in the Wave 1
  roster consumes an `entity_id`, so the `find_entities` round-trip the tier exists to skip is
  never taken; and the selector keys on name overlap plus domain keywords, so it goes silent on
  the oblique references the tier was justified by. The mechanism, the config option, and the
  tests stay; the re-test trigger is the first `entity_id`-consuming tool, in Wave 3
  ([`prompt-context.md`](prompt-context.md)). Tier 1, the entity summary, is unaffected and
  stays on. Note what this does *not* close: the summary-versus-roster-dump saving is still
  unmeasured, since both arms ran with the summary applied.
- [ ] **Capability-selection enforcement flip**. Catalog, availability filter, retriever,
  budget assembler, trace, and gated enforcement are built, but `DEFAULT_CAPABILITY_SELECTION`
  is off. The task-success gate on the golden set is a PASS (0 regressions, exposure 31→17); the
  recall@budget gate is what holds the flip. On the widened 50-tool script corpus (36 cases,
  2026-08-05), budget-8 recall is 100% in-vocabulary but 53% out-of-vocabulary, the misses being
  6 name-only (unconfigured) scripts plus 3 true synonym gaps
  ([`capability-selection.md`](capability-selection.md)). Recall does not move with budget: the
  miss list is identical at 8, 12, 16, 24, and 50, so it is a retrieval floor and a wider budget
  buys nothing. A second reason holds the flag independently of any recall number, and it is the
  one that decides Wave 1: the demo catalog's retrieval documents are English, which
  "Localization" forbids from gating a live request. Measured on 2026-08-05, an English catalog
  scoring German utterances leaves 16 of 76 cases with nothing but the two resident reads. Both
  fixes are now scoped and carried to Wave 2 rather than attempted here.
- [x] **`prefer_local_intents` recommendation [HA]**: closed 2026-08-05, recommend it **on**.
  There is no flag in this repo to flip. It is a Home Assistant pipeline setting
  (`assist_pipeline/pipeline.py:431`) that nothing in the integration reads, so the gate closes
  on a recorded verdict plus the README install step, not on a code change. The
  Δhassil-intervention read: 14/25 turns off-cloud, 22/25 routing agreement, and zero
  regressions traceable to routing. The one local "wrong action" (`turn-off-all-lights`) is the
  room-scoped behavior a 2026-08-04 ruling deemed correct against a stale whole-home
  expectation, and it fails the same way on the LLM path. None of the three routing
  disagreements is hassil taking a turn it should not have, so the false-positive pre-emption
  §2.9 warns about did not appear. Two consequences carried forward rather than closed here:
  the artifact reports three arg-bearing local wins UNJUDGED, all three of which close on the
  next run under the 2026-08-05 changes (`start-timer` and `add-shopping-item` judged from
  their durable effects, `set-bedroom-brightness` converted to state scoring), leaving no
  UNJUDGED local win ([`evaluation.md`](evaluation.md)); and locally handled mutations do not reach the undo
  journal, which makes `HassUndo`-as-local-intent a prerequisite of the undo claim
  ([`undo.md`](undo.md)). The deferred room-scoped rewrite of `turn-off-all-lights` is
  independent of this verdict and still open.
- [ ] **Wave 1 go/no-go recorded**. The locked artifact that states the token / turn / local
  verdict, the Wave 1 analogue of `wave0_baseline.json`. All three reads above are now in, so
  this is the last open gate. Two of the three closed negative or no-op for the shipped
  defaults (Tier 2 off, enforcement off) and the third is a setting we do not own, which the
  artifact has to state plainly rather than dress up. The token half also has to say what is
  still unmeasured: the entity summary versus HA's roster dump, since both name-injection arms
  ran with the summary applied.

*Proves:* the token/turn/local claims — the **go/no-go** on the design's central bet.

### Wave 2 — Bank cheap magic + first learning + upstream evidence
*Axis 3 + axis 2 dual-payoff.*

- **[C]** Weather forecast tool ([`weather.md`](weather.md)); what's-playing local intent
  ([`music-playback.md`](music-playback.md)); **notebook memory** `remember`/`recall`/`forget`
  ([`memory.md`](memory.md)) — demanded, low-risk delight (Store seam already threaded).
- **[C]** **Learning v1** — the offer engine + two resolvers: `add_alias` (rides the
  `find_entities` friction signal) + the **command-alias** resolver ([`learning.md`](learning.md)).
  → measure **Δhassil-rate + Δturns + utterances-moved-off-cloud**.
- **Testing gate:** learning's offer, accept/decline, and subsequent reuse run through the
  multi-turn text trajectory driver. The command-alias local-rate claim also runs through
  the combined local-first driver. Offer acceptance by itself is not task success.
- Prepare the `find_entities` results, tests, and proposed match-layer seam for maintainer
  discussion (§7). The eval/trace work can be discussed independently if it proves broadly
  useful; neither is assumed to land unchanged.

**Capability selection, carried from Wave 1.** The Wave 1 read is recorded, not acted on:
enforcement stays off, and these are the four items that would let the flip be reconsidered,
in cost order. Items 1 and 2 are the ones with a measured number behind them.

1. **[C] Move the localized-document builder into the component**
   ([`capability-selection.md`](capability-selection.md) "Localized retrieval documents").
   `evals/harness/localized_catalog.py` derives bundle documents from
   `home_assistant_intents`, which is already a dependency. English recall rises 95% → 100%
   at budget 8 on the Wave 0 set; German rises 30% → 100% on 76 held-out utterances, against
   an authored English catalog that leaves 16 of those 76 with nothing but the resident
   reads. Retires the localization blocker, which was the flag's second and independent
   reason for being off.
2. **[C] Unify the referent index** ([`find-entities.md`](find-entities.md) "The shared
   referent core"). One ranked lookup over entities, scripts, and scenes with one signal set
   (name, aliases, description, area), two layers on top: caution-regime resolution, and
   recall-oriented exposure. Today the same script is ranked by two scorers that disagree on
   `focus_mode`. Entity descriptions join the resolver's signals here. **When scripts enter
   the index, re-check Tier-2 name injection against it**
   ([`prompt-context.md`](prompt-context.md) "What the selector cannot reach"): scripts are
   the one referent class absent from both the prompt roster and the entity summary, so a
   selector that can reach them is a different proposition from the one measured in Wave 1.
   That is a selector question, and it is separate from the tier's own re-test trigger,
   which is a roster carrying an `entity_id` consumer in Wave 3.
3. **[C] Miss recovery through that index** ([`capability-selection.md`](capability-selection.md)
   "Miss recovery"). A capped, filterable `search` returning enough metadata to choose
   (name, area, description, parameter names), not a name-only teaser that costs ten turns of
   probing. Parameterless referents already execute through `HassTurnOn` inside the frozen
   roster, so this ships without any loop change. First measure whether the existing
   `find_entities` name-alternatives path already recovers the hidden-script class; that
   needs a corpus, not code.
4. **[C] Residency and budget policy for abstract bundles** (timers, calendar, weather). The
   one class with no referent to rank, and the only place a catalog-shaped retriever is still
   the right tool. Residency is earned from the traces, per Stage 4.

**Upstream evidence, not an upstream ask.** Hydrating a parameterized referent's schema needs
the exposed tool set to change between generations in one turn, and it cannot:
`internal/claude/entity.py` builds `model_args` once before the iteration loop and never
recomputes `model_args["tools"]`. Do not open that conversation on the strength of the code
read alone. Log each turn that hits the wall, then implement per-iteration tool recomputation
in the fork we control and show it recovers the request. A count plus a working implementation
is the ask; a design argument is not. Also unknown and worth measuring: what share of real
homes' scripts declare fields at all, since the parameterless path needs no change.

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
- **Testing gate:** store, trigger, catch-up, and delivery-state work can land under
  deterministic tests. A reminder is not complete as a voice experience until the controlled
  pipeline driver covers announce, microphone reopening, pull-to-read, and acknowledgement.
- **Possible core work later:** calendar-write and reminder/scheduling seams, after the
  proving ground establishes their contracts (§7).
- **[C] Re-measure Tier-2 name injection here, because this is where its trigger fires.**
  Conditional reminders and scheduled commands are the first tools to take an entity as
  *data*, so this wave produces the first roster containing an `entity_id` consumer. Tier 2
  went off by default in Wave 1 measuring 1.45x spend for no benefit on a roster where every
  tool resolved a spoken name itself ([`prompt-context.md`](prompt-context.md) "The verdict,
  and the trigger that reopens it"). Build Option 2, the cache-isolated second system block,
  in the same change so the arm under test costs about 8% rather than 45%, and fix the
  selector's blind spot on oblique references first or the A/B measures the wrong thing.

*Proves:* the headline VISION demos.

### Wave 4 — Proactive & multi-user
*Phase 4, deferred.*

- **[C]/[HA]** `assist_satellite.start_conversation` nudges; voice-ID → per-user context;
  off-satellite push + actionable-notification ack ([`scheduling-model.md`](scheduling-model.md),
  [`speaker-identification.md`](speaker-identification.md)). Long-term memory remains the
  most opinionated candidate for eventual core discussion.
- **Testing gate:** proactive conversation, continued-conversation behavior, and satellite
  acknowledgement require the controlled voice-pipeline driver. Streaming cancellation must
  pass there before claiming barge-in support. Any real latency threshold is reported with
  the selected STT/TTS engines and hardware, not as a generic model result.

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
