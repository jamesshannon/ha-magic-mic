# Evaluation & Tracing for Assist / LLM

> Meta-feature doc. Two related-but-distinct concerns: **live tracing**
> (single-run observability, dev + prod) and **evaluation** (offline, systematic,
> across a corpus). They share instrumentation but differ in purpose. Covers what
> core provides, the gaps, and how to prove a change (e.g. prefill/caching) keeps
> quality while improving TTFT/TTLT. See [`voice-streaming.md`](voice-streaming.md)
> for the latency model and [`../PRODUCT_PLAN.md`](../PRODUCT_PLAN.md) §5.2.

Core has **no** LLM eval / benchmark / training tooling **integrated with Assist** — only
building blocks (tracing + a timestamped event stream + pytest/snapshot). Ad-hoc community
benchmarks and academic corpora exist but stand *outside* the pipeline (**Part H**). The
harness described below — corpus + runner + scorer + trace enrichment — would be a genuine
contribution, and (being **feature-decoupled**) one core could merge **first**, independent
of any Magic Mic capability (§7).

**Testing splits into two tiers** — keep them distinct: **(A) deterministic subsystem
tests** for the non-LLM machinery (scheduling, delivery, undo, scorer, memory store) —
ordinary pytest, exact, CI-blocking (**Part G**); **(B) probabilistic LLM-behavior eval**
— sampled, judged, threshold-gated (**Parts D–E**). Parts A–F below were originally written
for (B); Part G adds (A), which the highest-trust-stakes code (durable reminders) most
needs.

---

## Part A — Tracing vs. evaluation (and the state of live tracing)

**Tracing** = live, single-run observability. "What happened on *this*
interaction, and how long did each step take." Reactive debugging, in dev and in
prod. **Evaluation** = offline, systematic. "Across a corpus, is quality
maintained and latency improved." Proactive / CI. They overlap on *quality* and
share the same instrumentation, but eval is built by *aggregating and scoring*
what tracing already emits per run.

### The two trace systems (don't conflate them)

They live at **different layers**. The conversation trace covers one
conversation-*agent* turn; the pipeline run debug covers the whole voice pipeline
(of which the agent is one stage).

| | **Conversation trace** (`conversation/trace.py`) | **Pipeline run debug** (`assist_pipeline`) |
|---|---|---|
| **Scope** | One conversation-agent turn (the "intent"/LLM step) | Whole pipeline: wake → STT → **intent** → TTS |
| **Created by** | `async_converse()` wraps each turn in a `ConversationTrace` (`agent_manager.py:119`) | A `PipelineRun` per pipeline execution |
| **Records** | `ASYNC_PROCESS` (input), `AGENT_DETAIL` (chat-log detail), `TOOL_CALL` (each tool/intent the agent invoked). Events carry a `timestamp`; only these 3 types. | Per-stage `PipelineEvent`s w/ `timestamp` (`pipeline.py:410`): `STT_VAD_START/END`, `INTENT_START`, `INTENT_PROGRESS` (one per LLM delta), `INTENT_END`, `TTS_START/END` |
| **Storage** | In-memory `_recent_traces` (LimitedSizeDict); `async_get_traces()` | `pipeline_data.pipeline_debug[pipeline_id][run_id]`, LimitedSizeDict (`pipeline.py:611`) |
| **Consumers** | **Tests only** — no websocket/HTTP/UI in core. Only callers of `async_get_traces()` are integration tests (e.g. `tests/components/ollama/test_conversation.py`, `google_generative_ai`). | **Websocket API** (`websocket_run` / `websocket_list_runs` / `websocket_get_run`) → the frontend **Assist-debug UI** |
| **Answers** | *What did the agent **decide**?* (which tools, what args) — LM/intent level | *What happened end-to-end, and how long did each **stage** take?* — the source of "1.2 s STT" |

**Key facts that keep coming up:**
- **The conversation trace has no UI.** It is an instrumentation/testing hook.
  Nothing user-facing reads it; its consumers are integration tests asserting the
  agent called the right tools. → it is the natural hook for our eval harness's
  **action-level scoring** (Part E).
- **The pipeline run debug *is* the UI system** — websocket-backed, rendered in
  Settings → Voice assistants → pipeline → Debug.

### Relationship & nesting

- The conversation trace is conceptually **nested inside** the pipeline's intent
  stage: a pipeline run's intent step calls `async_converse`, which spins up a
  conversation trace.
- **But a conversation can happen with no pipeline.** Text input — the Assist chat
  sidebar, the `conversation.process` service, the REST/WS conversation API,
  automations — calls `async_converse` directly, so there is a conversation trace
  and **no** pipeline run (no STT/TTS stages exist). The conversation trace is the
  *only* trace for non-voice interactions.
- **They are not stitched together.** The pipeline debug UI shows the intent stage
  as one block of time plus the final result/deltas; it does **not** surface the
  conversation trace's `TOOL_CALL` detail. So the richest agent-level info (which
  tools ran, with what args, in what order) has *no* UI surface at all.

### Where live tracing could use work (a distinct contribution area)

- **UI is buggy.** (Lives in the `frontend` repo, not core — so partly outside a
  core PR, but real.)
- **Timing is stage-granular, not turn-internal.** The pipeline sees the whole
  LLM ("intent") stage as one duration. But for an *agentic* turn that's a loop:
  LLM round 1 → tool → LLM round 2 → … → final. Stage timing can't tell you "the
  2nd LLM round-trip took 3 s" or "`find_entities` took 800 ms."
- **Conversation trace has no first-class LLM-request or TTFT events.** You can
  infer gaps between `TOOL_CALL` timestamps, but per-round-trip TTFT and per-tool
  duration aren't structured.
- **The two systems aren't stitched into one timeline.** You can't see, in a
  single view: `STT 1.2s → LLM r1 (TTFT 0.8s, end 1.5s) → find_entities 0.3s →
  LLM r2 … → TTS`. And the agent-level tool detail isn't in the UI at all
  (previous point).

**Why this matters for us:** attributing latency *within* an agentic turn is
exactly what our TTFT/context-reduction work needs to debug. Enriching the
conversation trace with per-round-trip / per-tool timing (LLM-request
start/end + TTFT events) is the single instrumentation upgrade that serves
**both** live tracing and offline eval. Build once, feed both.

---

## Part B — What core provides (building blocks)

1. **Conversation trace** — *what the agent decided* (tool calls + args). Great
   for "what happened," coarse on timing (Part A).
2. **Pipeline event stream w/ timestamps** — the **latency substrate**. From it:
   - **TTFT** = timestamp of first `INTENT_PROGRESS` with content − `INTENT_START`
   - **TTLT** = `INTENT_END` / TTS end − `STT_END`
   - per-stage durations (what the UI shows).
   Nothing aggregates or reports it; raw events only.
3. **pytest + syrupy snapshots** (`tests/components/conversation/`, `.ambr`) —
   deterministic regression, but the **model is mocked**, so it tests plumbing
   (prompt assembly, tool dispatch), not model quality.
4. **`home-assistant/intents`** (external repo) — a large gold-master corpus
   (sentence → expected intent + slots) with CI. Real eval harness, but for the
   **local hassil template agent**, not LLM quality/latency.

---

## Part C — What's missing (the opportunity)

- **No LLM quality eval** — nothing runs utterances against a *live* model and
  scores "right tool, right args, right entity."
- **No latency benchmark** — timestamps emitted, never aggregated into TTFT/TTLT
  across a suite or compared across configs.
- **No A/B / regression harness** for agent changes (prompt, prefill, context
  reduction).
- **No training/fine-tuning tooling.**

---

## Part D — How to prove a prefill/caching change

Two halves; one is easier than expected.

### Latency (TTFT/TTLT) — aggregate the event stream
Run a fixed corpus through `assist_pipeline`, harvest event timestamps, compute
TTFT/TTLT per run (formulas in Part B), compare warm-prefill vs cold-prefill over
N runs, report distributions (p50/p95). All hooks exist; you're aggregating.

### Quality-invariance — mostly *provable*, not just measurable
Prompt caching / KV-prefill reuse is **output-preserving by construction**:
caching the KV state of *the same token sequence* yields identical logits (modulo
float nondeterminism). So the strongest test is **asserting the assembled prompt
token sequence is byte-identical with and without the optimization** — that
proves you didn't reorder/mutate the prompt, which is the only way caching *can*
change output. A sampled behavioral eval is then just the backstop.

For the behavioral backstop, **score at the tool-call/action level, not the
prose level**: "did it emit `HassTurnOff(area=kitchen)`?" is deterministically
checkable; free-text answers need LLM-as-judge or semantic similarity over
sampled runs with pass-rate thresholds. LLM output is non-deterministic, so
gold-master-exact-*text* fails, but gold-master-expected-*actions* works.

### Two kinds of change need two different quality guards

The token-identity argument above only holds for **output-preserving** changes
(caching, KV reuse). **Context-reduction changes** (the §5.2 / `prompt-context.md`
entity-injection work — full roster → skeleton + request-conditioned names) are
**not** output-preserving: they *deliberately* change the prompt tokens, so
token-identity can't be the guard. Their guard is **task-success equality** —
does the agent resolve the right entity / emit the right action *as often* with
the pruned context — measured on the labeled corpus (Part E), full-roster vs pruned,
as a CI regression gate.

And their **cost metric must be end-to-end, not per-prompt.** Pruning trades prompt
tokens for *generations* (a skeleton can add a `find_entities` round-trip). Measure
**total tokens summed across all generations in the turn** + TTFT/TTLT — not the
static prompt in isolation — or you'll report a token "win" while adding a
round-trip that costs both latency and tokens. (This is why Part A's per-round-trip
timing enrichment is load-bearing: without it you can't see the added generation.)

**Cache metrics come for free** from the provider `usage` object — Anthropic
returns `cache_creation_input_tokens` / `cache_read_input_tokens` per request
(HA's `anthropic/entity.py` already captures `message.usage`), so cache hit rate
is directly observable per run, no extra instrumentation. *(Population-level
questions — "how often is a conversation warm across the fleet?" — are **fleet
telemetry**, out of scope for this harness and for core; see
[`prompt-context.md`](prompt-context.md) §"Two tiers of observability.")*

---

## Part E — Eval harness design (what to build)

Two independent knobs frame the whole harness — set per run:

- **Scope:** the **full hassil→LLM path** (what the user actually experiences — includes
  local-vs-LLM routing + end-to-end latency) vs **LLM-only** (isolate the model's decision,
  to attribute a regression). Same corpus, different entry point.
- **Metric:** *tool/action correctness*, *task-completion*, **turns-to-completion**,
  **tokens** (summed across generations), *latency* (TTFT/TTLT), and **helpfulness**. The
  last separates an *assistant* from a command parser and is the only one needing
  **LLM-as-judge** — so it's sampled + threshold-gated, never CI-blocking.

- **Corpus — two shapes:**
  - *Single-turn:* `(utterance [, context: device/area/exposed-entities] → expected
    action(s): tool + args / entity resolution [, answer predicate])`.
  - *Multi-turn conversation:* a scripted dialogue → expected **trajectory** (turns to
    completion, disambiguation handled) — needed for the "learning/memory removes turns"
    thesis and for barge-in / continued-conversation. A **user-simulator** (VISTA-style,
    Part H) can drive these so we don't hand-script every branch.

  Seed from real usage + adapt external corpora (**SMH-Bench**, **HomeBench**, **HomeFlow**)
  mapped onto our entities/intents (Part H).
- **Runner:** execute each case at the chosen scope, capturing both the conversation trace
  (actions) and pipeline events (timing).
- **Scoring:**
  - *Action correctness* — exact/normalized match on tool + args; entity-resolution
    correctness (did `find_entities` return the intended `entity_id`?). Deterministic.
  - *Task completion / turns* — did the dialogue reach the goal, and in how many turns.
  - *Helpfulness / answer quality* — **LLM-as-judge** (G-Eval-style rubric) / semantic
    similarity, sampled, pass-rate thresholds. Distinct from correctness: a response can emit
    the right tool yet be unhelpful, or answer well with no tool at all.
  - *Latency* — TTFT/TTLT distributions from the event stream.
- **Reporting — the outcome SCORECARD (this *is* the build-sequence "value dashboard",
  [`build-sequence.md`](build-sequence.md) / [`../PRODUCT_PLAN.md`](../PRODUCT_PLAN.md) §8).**
  Don't just pass/fail each case — report the **distribution** across the corpus and its
  **delta across changes**:

  | Bucket | Meaning |
  |---|---|
  | resolved locally (hassil) | handled on-device, no LLM — the §2.9 win |
  | resolved by LLM, correct | right action / answer via the model |
  | resolved after clarification | correct, but cost N extra turns |
  | wrong action | did the wrong thing |
  | unresolved / "I don't understand" | no useful outcome |

  Plus per-corpus **tokens / generations / turns** and the **local-vs-LLM split**. The
  headline claim is a *movement* in this table — e.g. hassil 20→50, "don't understand" 30→5,
  turns↓, tokens↓ — at **fixed or rising task-success**, never a resource win bought with a
  quality loss (Part D). Per-config comparison + CI regression gates on the deterministic
  buckets; helpfulness sampled and threshold-gated.

---

## Part G — Deterministic subsystem & timing tests (the non-LLM machinery)

Everything above (Parts A–E) targets **probabilistic LLM behavior** — sampled, judged,
threshold-gated. But **most of what we build is deterministic** (the scheduling substrate,
delivery state machine, undo journal, `find_entities` scorer, memory store, local-first
routing, offline second-pass), and deterministic code gets **ordinary, exact,
CI-blocking unit/integration tests** — no model, no sampling. These are a **distinct tier**
from the eval harness, and every piece of functionality gets full coverage here.

> **Two testing tiers, don't conflate:** (A) **deterministic subsystem tests** — pytest,
> exact assertions, model-free, pass/fail in CI (this Part); (B) **LLM behavior eval** —
> sampled, judged, threshold-gated (Parts D–E). A change touching both needs both.

### The scheduling substrate is the highest-stakes deterministic surface
"A silently-dropped 'take your medication' reminder destroys trust"
([`scheduling-model.md`](scheduling-model.md)) — so the watermark / two-knob catch-up /
durable-restart logic needs **exhaustive** deterministic tests. Conveniently, that doc's
**"Robustness: gap-free timeline coverage" + "Ordering replaces atomicity" + catch-up
sections are already a test spec** — turn each stated property into a case:

| Property (from scheduling-model.md) | Test |
|---|---|
| **No double-fire** across re-ticks/edits/restart | fire once, replay ticks / reload store → assert single delivery per occurrence (watermark dedup) |
| **No silent miss** (at-least-once) | crash between deliver and watermark-persist → assert **redundant** re-fire, never a drop |
| **Catch-up grace** (drop stale) | simulate a multi-day outage → occurrences older than *N* skip-but-advance |
| **Catch-up collapse** (don't-replay recurrence) | short outage spanning several occurrences → **at most the latest** fires, not the series (the "July 2, July 3…" machine-gun) |
| **Clock jumps / DST / NTP** | forward leap = one big span (grace-filtered); backward jump leaves watermark ahead of `now` → nothing re-fires |
| **Timer vs reminder grace** | timer missed-while-down = drop (short grace); reminder = surface (long/∞) |
| **Catch-up is informational** | caught-up item fires *inform*, never re-runs a payload/command |

### The harness: HA's time-simulation toolkit (already proven)
No new framework — HA ships exactly what durable-time testing needs, and the **Calendar
Trigger tests use this pattern already** (`tests/components/calendar/test_trigger.py:258-267`),
which is precisely the machinery our durable trigger reuses:
- **`freezer` / `FrozenDateTimeFactory`** (pytest-freezer + `freezegun`) — `move_to(t)`
  sets the wall clock deterministically (DST/jump scenarios).
- **`async_fire_time_changed(hass, t)`** (`tests/common.py:504`) — drives the
  time-tracking callbacks (`async_track_point_in_time` / `_time_interval`) at simulated
  time; no real waiting.
- **Restart/persistence:** `mock_restore_cache` / `async_mock_restore_state_shutdown_restart`
  (`tests/common.py:1327/1381`) + config-entry reload → simulate reboot and assert the
  watermark survives and dedups.

### Every other subsystem, too
- **`find_entities` scorer** — a labeled `(query, candidates) → expected ranking + guard
  decision` set (decisive / ambiguous / floor); the ambiguity-threshold regression gate
  ([`find-entities.md`](find-entities.md)).
- **Undo journal** — action → recorded inverse → replay → assert state restored
  exactly (scene snapshot round-trip); world-moved-on → confirm/decline ([`undo.md`](undo.md)).
- **Delivery state machine** — mock the `assist_satellite` entity; assert
  announce/escalate/queue transitions, `SatelliteBusyError` → defer, ack paths ([`scheduling-model.md`](scheduling-model.md)).
- **Memory store** — slot overwrite / fuzzy-collapse / cross-entity alias collision / TTL
  expiry ([`memory.md`](memory.md)).
- **Local-first routing & offline** — the §2.9 exclusion set (`GET_STATE` /
  `MEDIA_SEARCH_AND_PLAY` defer, others local); the offline second-pass on a simulated
  connection error ([`offline.md`](offline.md)).
- **Injection red-team** — adversarial calendar titles / device names / web content as a
  regression gate ([`security.md`](security.md)).

### Performance / timing harness (distinct from correctness)
Separate from pass/fail correctness: **scale and throughput** — N reminders scheduled, a
large catch-up backlog completing promptly on restart, the scorer over a large registry —
measured against thresholds (may live outside the CI gate, like the latency harness). This
is the subsystem analogue of Part D's LLM TTFT/TTLT (which stays the measure for the
model path). Together: **correctness is exact and CI-blocking; performance is measured and
threshold-gated.**

### Conventions (core-contribution-shaped)
Follow HA's test conventions so these land upstream with the capabilities (§7):
`pytest` + `uv run pytest`, **syrupy** `.ambr` snapshots for structured outputs,
type-annotated fixtures, `pytest.mark.parametrize` over duplicated bodies (per
`ha-core/CLAUDE.md`). Tests ship **with** each capability, not after.

---

## Part F — Why this is worth more than a one-off script

- It **doesn't exist in core** and the ecosystem visibly needs it — the perennial
  "which model is best for Assist?" has no reproducible answer today.
- It's the **measurement rig for our §5.2 thesis**: you can't credibly claim
  "smaller prompt, same quality, better TTFT" without exactly this.
- **Synergy with tracing (Part A):** enriched per-round-trip / per-tool timing
  instrumentation feeds both live debugging *and* the eval harness. One
  instrumentation investment, two payoffs.

### Distinct work-items this doc implies
1. **Eval harness** (Parts D–E) — corpus + runner + action-level scoring +
   TTFT/TTLT harvest.
2. **Tracing enrichment** (Part A) — per-LLM-request + TTFT trace events;
   unify conversation-trace and pipeline-run into one timeline. Serves both.
3. **Deterministic subsystem + timing tests** (Part G) — exhaustive pytest coverage of the
   non-LLM machinery, led by the scheduling substrate's time/restart/DST simulation
   (freezer + `async_fire_time_changed` + restore-cache); ships with each capability.
4. **Prior-art / reuse decision** (Part H) — pick the framework (if any) backing the *dev*
   harness and fix the thin/portable boundary for the core contribution.
5. *(frontend, out of core scope)* — fix the Assist-debug UI bugs.

---

## Part H — Prior art & reuse vs. build

We are not the first to score Assist, and shouldn't rebuild what exists. The landscape as of
2026, and what we take from each:

- **HA-specific, but ad-hoc.** A community benchmark
  ([ha-voiceagent-llm-benchmark](https://github.com/Drizzt321/ha-voiceagent-llm-benchmark))
  scores *local* LLMs on intent accuracy (≈74.8% best) and — notably — found **"always use
  the friendly name" improves every model 5–24 points**, direct empirical support for the
  §5.2 / `find_entities` naming thesis. But it's a **standalone script, not integrated with
  the Assist pipeline or core** — exactly the gap (Parts C, F) and a **demand signal**, not a
  competitor. `home-assistant/intents` stays the gold-master for the *hassil* agent
  (Part B.4); nothing equivalent exists for the LLM/agent path.
- **Academic corpora / methodology to adapt (not adopt whole):** SMH-Bench, HomeBench (case
  corpora), **HomeFlow** (verifiable-simulation "data flywheel" for generating cases),
  **VISTA** (user-simulation for multi-turn agent eval → drives the conversation corpus,
  Part E).
- **General OSS frameworks:** **DeepEval** (Apache-2.0, **pytest-native** — matches HA
  convention; metrics map near 1:1: *Tool Correctness* ≈ action scoring, *Task Completion* ≈
  task-success, *G-Eval/DAG* ≈ helpfulness LLM-as-judge), **OpenAI Evals** (registry /
  benchmark runner), **promptfoo** (red-team focus → the injection regression gate,
  [`security.md`](security.md) / Part G).

**The reuse-vs-build split (an explicit open decision).** Use a **DeepEval-shaped framework
for *our* dev / iteration harness** — velocity, LLM-as-judge and trace views for free, VISTA
user-sim for conversation cases. Keep the **core-contributed** piece **thin and
convention-pure** (pytest + syrupy, minimal deps): core won't merge a heavy eval dependency,
but it *will* merge the **corpus format**, the **trace enrichment** (Part A), and the
**scorer**. Same "throwaway shell / portable capabilities" line (§5.5) applied to tooling —
not a compromise. The boundary is a build-time decision, not a design blocker.
