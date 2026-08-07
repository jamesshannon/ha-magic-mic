# Evaluation & Tracing for Assist / LLM

> Meta-feature doc. Two related-but-distinct concerns: **live tracing**
> (single-run observability, dev + prod) and **evaluation** (offline, systematic,
> across a corpus). They share instrumentation but differ in purpose. Covers what
> core provides, the gaps, and how to prove a change (e.g. prefill/caching) keeps
> quality while improving TTFT/TTLT. See [`voice-streaming.md`](voice-streaming.md)
> for the latency model, [`telemetry.md`](telemetry.md) for deployed product-outcome
> measurement, and [`../PRODUCT_PLAN.md`](../PRODUCT_PLAN.md) §5.2.

Core has **no** LLM eval / benchmark / training tooling **integrated with Assist** — only
building blocks (tracing + a timestamped event stream + pytest/snapshot). Ad-hoc community
benchmarks and academic corpora exist but stand *outside* the pipeline (**Part H**). The
harness described below — corpus + runner + scorer + trace enrichment — would be a genuine
contribution candidate because it is feature-decoupled. Treat its upstream shape and timing
as a maintainer discussion, not an assumption that the proving-ground harness lands first or
unchanged (§7).

**Testing splits into two broad tiers**: **(A) deterministic subsystem tests** for the
non-LLM machinery (scheduling, delivery, undo, scorer, memory store), ordinary pytest,
exact and CI-blocking (**Part G**); **(B) probabilistic behavior evaluation**, sampled and
threshold-gated (**Parts D–E**). Tier B has several drivers rather than one ever-growing
runner: single-turn text, local-first text, multi-turn text, and the voice pipeline. The
layered plan and feature gates are defined below. Parts A–F were originally written for
(B); Part G adds (A), which the highest-trust-stakes code (durable reminders) most needs.

---

## Part A — Tracing vs. evaluation (and the state of live tracing)

**Tracing** = live, single-run observability. "What happened on *this*
interaction, and how long did each step take." Reactive debugging, in dev and in
prod. **Evaluation** = offline, systematic. "Across a corpus, is quality
maintained and latency improved." Proactive / CI. They overlap on *quality* and
share the same instrumentation, but eval is built by *aggregating and scoring*
what tracing already emits per run.

**Deployed-use telemetry is a third concern.** It asks whether people in real homes attempt,
complete, recover from, and reuse the VISION interactions over time. A fixed corpus cannot
measure adoption, ignore/cancel behavior, repeat use, or population distributions. Those
scenario outcome signals live in [`telemetry.md`](telemetry.md), not in the corpus scorecard.

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

Payloads are part of the trace contract. A useful agent trace must preserve the utterance,
normalized tool arguments, tool results, and ordering needed to explain and score what
happened. Standard Python logs are different: they are routinely copied, aggregated, and
retained outside the trace lifecycle, so Magic Mic logs only tool names and classifications
there. Before payload-bearing traces gain broader UI, persistence, or export, they need the
access, retention, selective-redaction, and encryption-at-rest decisions in
[`security.md`](security.md#diagnostic-trace-privacy).

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
- **Partial latency benchmark** — per-round model TTFT and duration are recorded per case
  (the provider-round timing extension: `GenerationRecord.ttft_ms` / `duration_ms`, carried
  into every live artifact) and the run rolls them up into p50/p95 over the model-driven
  turns (`Scorecard.latency`, in the `latency_ms` block of the baseline artifact). Still
  open: comparing those distributions across configs (an A/B), and whole-pipeline voice TTLT,
  which needs the controlled pipeline profile. The model number is network-inclusive and
  excludes tool-execution time between rounds, so it is not wall-clock turn latency.
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
entity-injection work — full roster → entity summary + request-conditioned names) are
**not** output-preserving: they *deliberately* change the prompt tokens, so
token-identity can't be the guard. Their guard is **task-success equality** —
does the agent resolve the right entity / emit the right action *as often* with
the pruned context — measured on the labeled corpus (Part E), full-roster vs pruned,
as a CI regression gate.

And their **cost metric must be end-to-end, not per-prompt.** Pruning trades prompt
tokens for *generations* (the summary can add a `find_entities` round-trip). Measure
**total tokens summed across all generations in the turn** + TTFT/TTLT — not the
static prompt in isolation — or you'll report a token "win" while adding a
round-trip that costs both latency and tokens. (This is why Part A's per-round-trip
timing enrichment is load-bearing: without it you can't see the added generation.)

**Cache metrics come for free** from the provider `usage` object — Anthropic
returns `cache_creation_input_tokens` / `cache_read_input_tokens` per request
(HA's `anthropic/entity.py` already captures `message.usage`), so cache hit rate
is directly observable per run, no extra instrumentation. Population-level questions such
as “how often is a conversation warm across the fleet?” belong to
[`telemetry.md`](telemetry.md), not this harness.

---

## Part E — Eval harness design (what to build)

Two independent knobs frame the whole harness — set per run:

- **Scope:** the **full hassil→LLM path** (what the user actually experiences — includes
  local-vs-LLM routing + end-to-end latency) vs **LLM-only** (isolate the model's decision,
  to attribute a regression). Same corpus, different entry point.
- **Metric:** *tool/action correctness*, *task-completion*, **turns-to-completion**,
  **tokens** (summed across generations), *latency* (TTFT/TTLT), **response brevity** (the
  verbosity complaint, [`prompt-context.md`](prompt-context.md)), and
  **helpfulness**. The last separates an *assistant* from a command parser and is the only
  one needing **LLM-as-judge** — so it's sampled + threshold-gated, never CI-blocking.

- **Corpus — two shapes:**
  - *Single-turn:* `(utterance [, context: device/area/exposed-entities] → expected
    action(s): tool + args / entity resolution [, answer predicate])`.
    Provider-specific behavior under test is declared per case as setup, such as
    `provider_options: {web_search: true}`. Unspecified web tools remain off, so a retrieval
    experiment cannot silently change ordinary device-control cases.
  - *Multi-turn conversation:* a scripted dialogue → expected **trajectory** (turns to
    completion, disambiguation handled) — needed for the "learning/memory removes turns"
    thesis and for barge-in / continued-conversation. A **user-simulator** (VISTA-style,
    Part H) can drive these so we don't hand-script every branch.

  Seed from real usage + adapt external corpora (**SMH-Bench**, **HomeBench**, **HomeFlow**)
  mapped onto our entities/intents (Part H).
  - *hassil-parity set (required, non-optional):* the built-in **starter sentences**
    (`builtin_sentences.markdown`) + the per-language `home-assistant/intents` test
    sentences, mapped onto our entities. Run at **LLM-only scope** (not the hassil→LLM
    path — routing to hassil would hide an LLM-path gap behind the local matcher). This is
    the operational form of PRODUCT_PLAN §5.8: **our path must reach ≥ parity with hassil
    before any value-add counts.** A parity miss is a **blocking** regression, scored in
    the deterministic buckets below, not a helpfulness sample. All these utterances are
    caught by hassil when local routing is on, so their value is specifically as the
    *LLM-path* regression set — not a `home-assistant/intents` contribution.
- **Runner:** execute each case at the chosen scope, capturing both the conversation trace
  (actions) and pipeline events (timing).
- **Scoring:**
  - *Action correctness* — exact/normalized match on tool + args; entity-resolution
    correctness (did `find_entities` return the intended `entity_id`?). Deterministic.
    Required calls must appear in order, named argument values match exactly after
    conservative text normalization, and undeclared extra calls fail. Read-only setup calls
    are allowed only when the outcome declares them as supporting tools. State-scored cases
    likewise declare a permitted-tool roster, so reaching the right state cannot hide an
    unrelated call. A separate fixture effect ledger records durable or external results
    that do not appear in entity state (currently timers and todo rows); those effects are
    explicit outcome predicates and undeclared effects fail.
    Tool-name matching is brittle when several tools reach the same outcome (the Wave 0
    baseline scored four cases "wrong" for using `HassLightSet` over the guessed tool while
    acting correctly). For device-control cases, prefer **state-diff scoring** as a
    complementary signal: snapshot entity state, run the turn, assert only the declared
    `expect_changes` differ (borrowed from home-assistant-datasets, Part H). Keep tool-call
    scoring for query / answer / clarification cases, where no state changes to observe.
  - *Task completion / turns* — did the dialogue reach the goal, and in how many turns.
  - *Helpfulness / answer quality* — **LLM-as-judge** (G-Eval-style rubric) / semantic
    similarity, sampled, pass-rate thresholds. Distinct from correctness: a response can emit
    the right tool yet be unhelpful, or answer well with no tool at all.
  - *Latency* — TTFT/TTLT distributions from the event stream.
  - *Response brevity* — length of the spoken response, in **spoken-seconds** (the honest
    unit: it's the TTLT the user actually feels; words/chars/tokens are proxies). **Two
    regimes, because most of the corpus has no local baseline:**
    - *hassil-comparable cases* — score as **excess-or-shortfall vs. the per-case baseline**
      (the hassil canned response / deterministic template for that command). This isolates
      the *unnecessary* verbosity — the actual complaint — and is directly comparable across
      hassil-vs-LLM and across model/prompt updates.
    - *LLM-only cases* — genuinely model-only requests ("turn off the light in the room where
      I cook food," conversational Q&A) have **no hassil response to compare to**, and these
      are a large and growing share. Track **absolute** length and its **drift across config
      versions over time** (this model/prompt vs. the last), not against a phantom baseline.
    Necessary verbosity (disambiguation, failure explanation) is **expected, not penalized** —
    it surfaces as *justified* excess tied to the low-confidence / failure outcome buckets,
    so the metric separates "verbose because it had to be" from "verbose for no reason."
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
  | unjudged (no success predicate) | observed response cannot prove task success |

  Plus per-corpus **tokens / generations / turns** and the **local-vs-LLM split**. The
  headline claim is a *movement* in this table — e.g. hassil 20→50, "don't understand" 30→5,
  turns↓, tokens↓ — at **fixed or rising task-success**, never a resource win bought with a
  quality loss (Part D). Live comparisons use paired cases with alternating arm order and
  retain each pair's outcome and resource delta, rather than relying only on corpus totals.
  Helpfulness remains sampled and threshold-gated.

### Live comparison cadence

Use the smallest run that can answer the question:

- A code change with no effect on prompts, routing, provider configuration, tool exposure,
  or scoring needs deterministic tests, not a keyed model run.
- During behavioral development, run one paired pass over the affected cases and a few
  adjacent cases. This is the normal fast feedback loop.
- If a changed result or small resource delta would affect a design decision, run that
  targeted set for three paired trials. Accept a small improvement only when its direction
  repeats and task success does not fall. Three trials do not justify a confidence interval.
- Run one full-corpus paired pass when a prompt, model, provider configuration, tool roster,
  routing policy, or scoring contract changes broadly, and when refreshing a locked
  artifact at a wave or release milestone.
- Repeat the full corpus for three paired trials only for a broad go/no-go decision where a
  targeted set cannot represent the affected behavior. Safety failures are never averaged
  away; one unintended tool, state change, or durable effect requires investigation.

The paired runner alternates `off→on` and `on→off` by case. Additional trials reverse each
case's order again. Artifacts retain per-case order, correctness, buckets, and deltas, plus
trial totals. Exact output-token counts are expected to move; cache input and creation/read
counts are cache-regime evidence, not stable quality metrics.

### Layered harness plan and feature gates

Do not make every corpus case traverse STT, the model, tools, and TTS. Preserve the current
text runner as the fast agent-behavior tier, then add a new driver when a feature crosses a
boundary that the existing tier cannot observe. Drivers may reuse corpus cases and scoring,
but each artifact must name its scope and must not report a metric its driver cannot measure.

| Layer | Driver and evidence | Status | Features and claims gated by it |
|---|---|---|---|
| **Deterministic subsystem** | Model-free pytest over policy, stores, state machines, matching, time, restart, and failure paths. Exact and CI-blocking. | Exists; expands with each capability. | Every capability. In particular: identity/tool policy, scheduling, delivery state, undo executors, memory, aliases, capability selection, and the continuation consequence policy. |
| **Single-turn LLM-only text** | `async_converse` against the fixture home. Scores tools, arguments, state, durable effects, answers, generations, and provider tokens. | Exists. This is all the current live artifacts prove. | Prompt and tool changes, capability-selection recall, direct `find_entities` resolution, one-turn memory/query/write, calendar/weather/web tools, and action correctness. It does **not** prove local routing, clarification recovery, or voice latency. |
| **Agent timing extension** | Add provider request start, first content delta, final delta, and tool duration to the text trace. Reports per-generation model TTFT and duration, not whole-pipeline voice latency. | **Exists (per round):** the provider-round timing extension clocks each generation from request start to its first content delta (TTFT) and to its final delta (round duration), as `GenerationRecord.ttft_ms` / `duration_ms`, carried into `ObservedTurn` and every live artifact per case, and rolled up per run into p50/p95 over the model-driven turns (`Scorecard.latency`, the `latency_ms` artifact block). Remaining: per-tool execution duration, and comparing the distributions across configs (an A/B). | Prompt-context, capability-selection, and `find_entities` may claim fewer tokens or generations today, and lower model TTFT once a run compares the aggregated distributions across configs. |
| **Local-first text** | Drive the actual HASSIL→LLM decision path and record which agent handled the request, fallback behavior, final action, and total conversation-stage duration. Run the same cases LLM-only when attribution is needed. | **Exists:** `evals/harness/local_first.py` reproduces the pipeline's prefer-local decision faithfully: recognize through the default agent's `async_recognize_intent(strict_intents_only)`, execute through `async_handle_intents`, and apply HA's CONTROL fallback filter (`HassGetState`, `HassMediaSearchAndPlay` defer to the model), so only a local miss or a deferred intent reaches the LLM. The off-cloud rate is the real routed-locally count, not a raw HASSIL-match over-count. Correctness is judged where the local path allows it (world diff for state-scored, speech for answer, a declared durable effect for an arg-bearing action, name for a no-arg intent) and reported UNJUDGED otherwise, so a passing number never credits a local win this harness cannot verify. Recognized slots are recorded for diagnosis and scored by nothing, since HA and the `home-assistant/intents` repo already test template binding against the outcome it produces. | `prefer_local_intents`, new local intents, command aliases claimed to move speech off-cloud, and the offline second local pass. An arg-bearing action with no observable effect stays unverified until its case is converted to state scoring, so a wrong-arg local regression is caught only where an effect or a state change carries the argument. |
| **Multi-turn text trajectory** | Script user turns over one `conversation_id`; retain ChatLog state and score the final outcome, turns to completion, clarification, correction, cancellation, and intermediate side effects. Start with authored branches; add a user simulator only if scale warrants it. | **Exists (scripted + live):** `evals/harness/trajectory.py` drives turns over one `conversation_id`, `evals/corpus/wave1_disambiguation.yaml` scores recovery, misfire, and turns to completion (world-based, so scripted and live score identically), and `evals/harness/trajectory_live.py` drives the same worlds against a real model for the emergent turn count (stops when the world reaches the goal, one HA instance per case). The live pass (2026-08, `claude-haiku-4-5`, seven cases across lights/fans/covers and name/area ambiguity) recovered all five ambiguous cases at 2 turns and completed both direct commands in 1 (mean 1.71): haiku asks before acting on a real ambiguity, and disambiguation costs +1 turn. | `find_entities` ambiguity recovery, immutable confirmation including “no, do X instead,” session undo, learning offers and acceptance, memory correction, and any claim that a feature removes clarification turns. This makes `resolved after clarification` reachable. |
| **Controlled voice pipeline** | Feed deterministic STT results through `assist_pipeline` with controlled/mock STT and TTS boundaries. Capture pipeline events, continued-conversation flags, cancellation, spoken output, and delivery/ack transitions. Use real engines or hardware only for a separately labelled performance profile. | Add before a pipeline-owned interaction is called complete. | Continued conversation and its spurious gate, conversation-ID reuse across reopened microphones, streaming cancellation and barge-in, whole-pipeline offline behavior, proactive `start_conversation`, and reminder announce/pull-to-read/ack flows. Spoken duration and absolute end-to-end TTFT/TTLT require the labelled real-engine profile. |

### Both routing configurations stay covered (an invariant, not a coincidence)

Users run with `prefer_local_intents` on or off, and a command that HASSIL captures on one
box reaches the model on the next. Both paths have to keep working, so both are measured.

The split falls out of which entry point a driver calls. `runner.observe_turn` addresses the
agent by id (`conversation.async_converse(..., agent_id=...)`), which skips `assist_pipeline`
entirely, so `prefer_local_intents` is never consulted; `baseline.py`, `fuzzy_fallback.py`,
`selection_gate.py`, `variant.py`, and `trajectory.py` all run through it. Prefer-local-off is
therefore the default configuration for every driver except `local_first.py`, which is the
only one that reproduces the on configuration. Each artifact records which it was in
`run.prefer_local`.

Three properties keep this honest, and they are worth stating because none is self-evident
from reading one driver:

- **The corpus carries the basics on purpose.** 17 of the 25 Wave 0 cases are labelled
  `routing_truth: local`, meaning HASSIL covers them today, and every one of them runs the
  model path in the baseline. Without that population the LLM path could break on "turn on
  the kitchen light" and no run would notice. `test_corpus.py` pins a floor on it so the
  population cannot erode silently as corpora grow.
- **The two scopes judge different things, and the corpus knows it.** `expected` scores the
  local path and `expected_llm` the model path (`Case.expected_for`), because core's Assist
  API drops intents like `HassGetCurrentTime` in favor of `GetDateTime`. `current-time` is the
  clean example: `HassGetCurrentTime` locally, `GetDateTime` for the model, same answer. The
  divergence is also a warning sign. When the two scopes disagree about *grounding* rather
  than tool name, as `set-bedroom-brightness` did with `area` locally against `name` for the
  model, it usually means neither expectation is load-bearing and the case wants state
  scoring.
- **Comparing the two artifacts is a manual step.** Nothing diffs `wave0_baseline.json`
  against `wave1_local_first.json` per case, so a divergence where one path is right and the
  other wrong is only visible to a reader who lines them up. Do it at each wave close.

The gap this leaves is on the local side, not the model side, and it is narrower than the
missing arg schema makes it look. A locally routed action never exposes its arguments, but a
**declared durable effect observes them downstream**: `start-timer` records
`timer.started {seconds: 600}` and `add-shopping-item` records
`todo.item_created {summary: milk}` on the local path exactly as on the model path, so a
HASSIL regression starting a 10-minute timer for "set a timer for 5 minutes" is caught.

The one case with arguments and no observable effect, `set-bedroom-brightness`, was converted
to `expect_changes` the same day. The fix there was not an arg convention but the one
`set-volume` already took: 30% is brightness 76 of 255 in the attributes, which is the
argument, observed. The 2026-08-06 re-run confirms it end to end, with
`unjudged_local: 0` against the previous 3 and **routing identical case for case**, so the
scoring got stricter without the off-cloud claim moving.

The residual is a shape rather than a case: an arg-bearing local action whose arguments leave
neither a state change nor a durable effect. Nothing in the Wave 0 set is one. A future
capability that only talks to an external service would be, and it should declare an
`ExpectedEffect` at that boundary rather than reach for slot scoring.

**Slot values are recorded, never scored.** `LocalRouting.slots` carries the post-resolution
slots of a local win into the artifact so a surprising route is diagnosable without a repro,
and nothing asserts on them. Home Assistant already tests that its sentences bind the right
slots and that those slots reach the right service call, correlating both halves in a single
test (`tests/components/conversation/test_default_agent.py` asserts `slots["area"]` and the
resulting calls for "turn on the lights" from a room-bound satellite), and the
`home-assistant/intents` repo tests per-language template binding for the sentences it owns.
Asserting slots here would restate an upstream guarantee against a weaker fixture, and for an
intent we contribute the sentence tests belong upstream in that repo's format, where they can
actually be submitted.

The layers are gates on claims, not gates on unrelated implementation. For example, the
`ScheduledItemStore` and reminder catch-up machinery can land with exhaustive deterministic
tests before a voice-pipeline driver exists. The reminder feature is not complete as a voice
experience until announce, microphone reopening, content delivery, and acknowledgement have
passed the controlled pipeline layer. Likewise, direct fuzzy resolution can land before the
multi-turn driver, but ambiguity recovery cannot.

Build the layers just in time:

1. Agent timing and local-first text now exist (`GenerationRecord` timing aggregated by
   `Scorecard.latency`; `evals/harness/local_first.py` for the routing path), so the
   prompt-latency and local-routing claims have a substrate. What remains for each is a
   config A/B: comparing distributions or off-cloud rates across a change, not new plumbing.
2. Add multi-turn text when `find_entities` first asks a clarification, or before pending
   confirmation/undo/learning becomes user-reachable if that arrives first.
3. Add the controlled voice-pipeline driver with continued conversation or the first
   satellite delivery flow. Extend it for cancellation before claiming barge-in, and for
   real TTS output before using spoken duration as an acceptance metric.

No layer substitutes for deployed telemetry. Corpus trajectories can prove that a scripted
recovery works; they cannot prove how often people attempt, abandon, or reuse it in homes.

**Grow the corpus with each feature, not in a big exercise later.** Now that the trajectory
machinery exists (loader, driver, world-based scorer, per-case standup, scorecard), a new
case is a few lines of YAML, so the case for a one-off "corpus sprint" later is weak: it
would re-pay the ramp-up and leave everything shipped in between without regression coverage.
The rule that keeps incremental growth honest is to add cases with the feature that makes the
claim, not ahead of it: cases authored before a feature's data contract settles get rewritten
(the prompt-context work already burned effort that way). The two layers have different
economics, and only one is expensive, which is what makes this low-risk: the scripted corpus
(`test_trajectory_corpus.py`) runs free in CI every commit, so grow it eagerly and treat each
case as permanent regression protection; the live sweep (`trajectory_live.py`) costs a key
and minutes, so run it in batches at milestones, not per commit. A bounded near-term breadth
pass is worth it only for a headline claim whose corpus is visibly thin (as the +1-turn
disambiguation claim was at three cases, now seven across lights, fans, and covers); it is
not a standing "exercise".

### Open hypotheses and corpus gaps (the test backlog)

The harness above is the instrument; this is the running list of what to point it at. Each
row is an empirical claim the design currently rests on but has not confirmed, paired with
the corpus or realism gap that keeps it open. It is a tracker, not a buildout: the corpus and
drivers grow just in time (above), so the point is to record the hypothesis and the test that
would settle it now, while the test is still cheap to state and the evidence is not yet
available to build against. A feature doc that flags a "revisit" adds a row and keeps its
depth; this table is the index. It complements the design-assumption table in
[`prompt-context.md`](prompt-context.md) (assumptions only deployed telemetry can settle):
rows here are the ones a corpus or harness change can test without waiting for fleet data.

| Hypothesis (as held today) | Owning doc | What would test it | Corpus / realism gap it needs | Status |
|---|---|---|---|---|
| Prompt-loaded entity names go unused: the stock intent tools resolve a spoken name/area/domain slot themselves, so injected names (and perhaps the whole roster) are dead weight in the common case. | [`prompt-context.md`](prompt-context.md) | An A/B on a roster that contains an `entity_id`-consuming tool, scored for whether names shave the `find_entities` round-trip, with Option 2's cache-isolated block in place and across more than one model. | **The roster gap is closed:** `evals/corpus/wave1_entity_id_tools.yaml` exposes scripts whose parameters are entity selectors, so an id consumer no longer waits on Wave 3, and `entity_id_tools.py --names` runs both arms with injection on. What remains: Option 2's cache-isolated block, a second model, and the oblique-reference blind spot (the selector goes silent there, so a harder corpus alone would measure the blind spot rather than the tier). | Open, default off. 2026-08-05: 1.45x total spend at identical task success; the selector cannot reach the oblique case the tier was justified by. The Wave 3 trigger is superseded: the corpus exists now, so re-measure on it rather than waiting for the scheduling substrate. |
| Entity-argument resolution earns its keep: a tool that takes an `entity_id` is unusable without it, because the model invents a plausible id that targets nothing. | [`find-entities.md`](find-entities.md) Consumer 3, [`core-deltas.md`](core-deltas.md) CD1 | `evals/harness/entity_id_tools.py` runs each case twice, resolution off (stock HA) and on, alternating arm order, and scores by resulting state. It also classifies how the model filled the argument: a live id, an invented one, or a spoken name. | The corpus is 6 cases on one home and one model; script tools are the only id consumer in it, so it does not yet say whether the same holds for the Wave 3 scheduling substrate's own id arguments. | Open, unrun (needs a key). The arms are proven to differ deterministically (`test_entity_id_tools.py::test_the_two_arms_differ_on_this_fixture`); what is unmeasured is how often a real model lands in each argument-source class. |
| Most commands target the room the user is in (the Tier-2 room-scope prior). | [`prompt-context.md`](prompt-context.md) | Share of resolutions whose target sits in the requesting area, over a multi-room corpus. | Corpus is single-home and does not vary the requesting room; best settled by fleet data. | Open. |
| The curated golden set over-cleans results: every command names its target or is an area / domain / state case, so a "names don't help, routing is easy" reading may be a corpus artifact. | this doc (Part E) | Grow the corpus toward a realistic utterance distribution and re-run every standing comparison. | A realistic corpus (oblique references, misheard names, `entity_id`-only tools, multi-device homes), seeded from real usage and adapted external corpora (Part H). | Open, cross-cutting. Bounds the confidence of every feature's live conclusion. |
| Lexical Tool-RAG keeps enough recall to enforce a tool budget: ranking bundles from the utterance finds the needed capability at a small budget. | [`capability-selection.md`](capability-selection.md) | The shadow harness (`selection_shadow`) recomputes the plan across a budget sweep and scores whether the used tool survived, reported by phrasing regime, over a real per-turn roster. | Golden set is 26 cases on a 24-tool catalog. The 50-tool script corpus adds scale and an in/out-of-vocabulary split; still needs the live per-turn roster and non-English cases. | Open. IDF scorer reaches 95% at budget 8 on Wave 0. On the 50-tool script home (36 cases, 2026-08-05): in-vocabulary recall 100% (14 cases), out-of-vocabulary recall 53% (10/19) at budget 8 while hiding ~43 tools. The earlier 88% was an all-aliased set; widening the out-of-vocabulary set to include name-only scripts drops it, and the misses split into 6 unconfigured (name-only) scripts and 3 true synonym gaps. Task-success enforcement gate on the golden set is a separate PASS (0 regressions); the recall number is what still holds the flip. |
| Enforcing the plan on a live request does not regress task success. | [`capability-selection.md`](capability-selection.md) | The gate harness (`selection_gate`) drives the golden set through the testbed agent twice per case, full roster vs enforced at a binding budget, arm order alternating, and scores task-success non-regression, attributing any regression to a tool the full arm used being dropped. | Golden set is 26 clean English cases; one trial; one model; budget 8 barely stresses recall on this corpus. The recorded gate artifact ran at 25 cases, before the bare-lights split. | Open (one gate cleared). 2026-08: task success identical case-for-case (17/25 both arms), zero regressions, roster pruned 31 -> 17.1 tools with no outcome change. Still blocked from enforcing by the English catalog (localization) and corpus breadth. |
| Lexical retrieval is enough; embeddings are not needed yet. | [`capability-selection.md`](capability-selection.md) | Grow a harder out-of-vocabulary set (different speakers, synonyms, non-English), add Unicode stemming, and measure the gap that survives cooperative aliases; escalate to embeddings or miss recovery only for that residual. | The out-of-vocabulary set is now 19 cases across aliased and name-only scripts; still needs a multilingual set before the escalation call is trustworthy. | Open. The 53% out-of-vocabulary recall (2026-08-05) separates into two classes: name-only scripts a lexical retriever cannot reach at all (a configuration gap, not a retrieval one) and true synonym gaps on configured scripts ("caffeine" -> Coffee Time, "treadmill" -> Workout, "help me concentrate" -> Focus Mode). Only the second class, not the aggregate, is what would motivate embeddings. Recall is flat across budgets 8 through 50, so it is a retrieval floor, not budget pressure. |
| Retrieval documents derived from `home_assistant_intents` localize the catalog without hand translation, and cost nothing in English. | [`capability-selection.md`](capability-selection.md) | `evals/harness/localized_catalog.py` builds bundle documents by rendering the shipped HASSIL templates, then scores derived against authored on both English corpora and on held-out utterances in a second language. Model-free. | Held out by sentence but only 9% of held-out tokens are novel, so it measures vocabulary coverage, not generalization. One non-English language. No out-of-vocabulary regime in that language. | Open, direction strong. 2026-08-05: English Wave 0 recall 95% -> 100% at budget 8; script corpus unchanged at 8 and 12; German 30% -> 100% over 76 cases, where the authored English catalog leaves 16 of 76 with residents only and every one of its hits traces to a loanword or the word "in". |
| A parameterless referent is recoverable without changing the exposed tool set; only a referent with arguments needs mid-turn hydration. | [`capability-selection.md`](capability-selection.md) | Drive hidden-script cases through the live agent and check whether `find_entities` name alternatives plus `HassTurnOn` complete the request, then count how often a turn wants a parameterized referent it cannot hydrate. | No corpus of oblique script requests with a hidden roster; no data on what share of real scripts declare fields. | Open. The roster is verified frozen per turn (`_get_model_args` runs once before the loop), so the hydration gap is real; the question is how often it binds. |

Maintenance: when a feature lands a finding that depends on the corpus growing, or on data we
do not have yet, add a row here and link it from the owning doc, so the assumption is tracked
in one place instead of re-derived per feature.

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
| **One canonical store** | create reminder/alarm/scheduled command/rule → reload → assert each restores through the same versioned `ScheduledItem` schema and lifecycle API |
| **Projection, not dual truth** | edit/cancel native projection and externally linked calendar event → assert deterministic reconciliation with the canonical/companion record |
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
  ([`find-entities.md`](find-entities.md)). Report false accepts, clarifications, and misses
  by language/script, with accented Latin, Cyrillic, and no-whitespace-script fixtures in
  the corpus. Unit tests prove those strings survive normalization; they do not establish
  that one English-tuned threshold is safe in every language.
- **Undo journal** — action → recorded inverse → replay → assert state restored
  exactly (scene snapshot round-trip); world-moved-on → confirm/decline ([`undo.md`](undo.md)).
- **ChatLog session state** — state survives HA's between-turn `dataclasses.replace()`,
  expires with the chat session, never duplicates transcript content, isolates concurrent
  conversation IDs, and keeps delayed effects attributed to the originating turn when two
  turns overlap within one conversation.
- **Identity/scope policy** — unidentified `"default"` callers can use household scope but
  cannot create or read personal records; recognized/authenticated users see household plus
  only their own personal records.
- **Tool policy + confirmation** — unavailable personal tools are absent from `.tools` and
  rejected again at execution; "yes" executes the immutable stored operation, while expiry,
  principal change, altered arguments, and "no" cannot execute it.
- **Capability selection** — deterministic availability filtering, dependency closure,
  tool/token budget enforcement, and affinity expiry; then shadow/e2e
  capability-and-tool recall@budget, task-success delta, unauthorized exposure (zero),
  discovery recovery, follow-up continuity, multi-intent coverage, and miss rate by language
  and provider ([`capability-selection.md`](capability-selection.md)).
- **Continuation false-action suite** — television/podcast dialogue containing quoted
  commands, cross-talk, valid but unrelated speech, explicit corrections ("actually no,
  turn up the heat"), and real follow-up commands. Run across the intended strong cloud
  model and weaker/local candidates. Measure false accepts and false rejects for the default
  one-pass spurious judgment; compare the optional no-tools classification pass only if the
  one-pass false-action rate is unacceptable, including its added generation and latency.
- **Continuation consequence policy** — continuation origin promotes only the declared demo
  operations, model sensitivity may raise but never lower the base tier, and an unrelated
  new command can reject/supersede a pending operation rather than being trapped by it.
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

- **HA-specific and integrated (the closest prior art).**
  [home-assistant-datasets](https://github.com/allenporter/home-assistant-datasets) (Allen
  Porter, a core-adjacent maintainer) runs synthetic homes through any HA conversation
  integration (openai / google / ollama / anthropic) via pytest and scores the result. Four
  things it does that we should learn from, and one it does not do that is our whole reason to
  exist:
  - **State-diff scoring, not tool-name matching.** It sets up entity state, runs the
    utterance, then diffs the post-action states against a declared `expect_changes` (with an
    `ignore_changes` list for derived attributes like a cover's `is_closed`). Pass = no
    undeclared diff. This scores the *outcome in the world*, so it is immune to the model
    picking an equally-valid tool. Our tool-call scorer hit exactly that failure in the Wave 0
    baseline: four cases scored "wrong action" only because the model used `HassLightSet` /
    `HassSetPosition` / `HassSetVolume` / `HassListAddItem` instead of the guessed tool while
    doing the right thing. `any_of` is our patch for that brittleness; state-diff removes the
    need for it on device-control cases. **We should adopt state-diff as a complementary
    correctness signal** (see Part E), keeping tool-call scoring for query / answer /
    clarification / generation-count cases where there is no state change to observe.
  - **`synthetic_home` fixture format + component.** A declarative `_fixtures.yaml` inventory
    (areas / devices / entities with attributes) loaded by the
    [synthetic-home](https://github.com/allenporter/synthetic-home) custom component, with
    homes across locales (`home1-us`, `dom1-pl`, `home2-ru`, `home5-cn`, `home7-dk`). This is
    the maintained equivalent of our bespoke `evals/harness/backing.py`. Adopting it is a
    larger, dependency-bearing call (Part H reuse note below); the format is worth converging
    toward regardless.
  - **A ready `intents` dataset** derived from HA's own NLP unit tests, plus `assist` (voice
    corner cases) and `assist-mini` (small models). A source of `local`-routing cases we
    hand-authored 17 of.
  - **collect / eval as two pytest phases.** `collect` scrapes model outputs to disk; `eval`
    scores them separately, so re-scoring after a scorer change costs no model tokens. Our
    `baseline.py` couples the run and the score. Token stats come from the same conversation
    trace `AGENT_DETAIL` hook we use, banked in a `TokenStatsBank`; there is also a
    leaderboard, CSV reports, and cost reporting we have not built.
  - **What it does not do:** it runs the LLM agent only. It has no concept of the local
    HASSIL path, so no local-vs-LLM routing split and no hassil-intervention rate. That
    measurement is the `prefer_local` thesis (§2.9) and the reason our corpus carries
    `routing_truth` and runs at two scopes. Its token stats compare *models*; our scorecard
    tracks a *movement across configs of one agent* at fixed task-success. So Wave 0 rebuilt
    some scaffolding (a fixture home, trace token-capture, a timed-agent wrapper) that already
    existed here, but the routing scorecard that Wave 0 exists to produce is genuinely absent
    from it.
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
user-sim for conversation cases. Keep corpus, trace, and scorer concepts separable enough to
discuss with core maintainers, but do not pre-build a pretend "core version." If core wants
some of this work, adapt a thin pytest-native slice to its conventions at that point. The
boundary is an upstream design decision, not a reason to constrain the proving-ground
harness now.

**What we adopt from home-assistant-datasets, and what we defer.** Split by cost and risk,
because the two borrowings are independent:

- **State-diff scoring: adopted.** Landed as `harness/statediff.py`: a case may carry `setup`
  (pre-turn state to stage), `expect_changes` (the post-turn state to assert), and
  `ignore_changes`, and when it does, correctness is the resulting world state, not the tool
  called. Declared entities are checked on state plus named attributes; every other exposed
  entity on state alone, so a wrong-target side effect is caught without tripping on a derived
  attribute (a light's `color_mode` following its state off). The snapshot is scoped to
  entities exposed to the agent, so the conversation entity's own timestamp does not trip a
  phantom diff. Nine device-control cases migrated: `close-blinds` dropped its `any_of`,
  `set-volume` dropped its name-vs-area `expected_llm`, and the four previously-falsified tool
  predictions became moot. The routing split, generation count, and token totals are
  untouched; only the correctness signal changed, and only where a state change exists to
  observe. A tenth followed on 2026-08-05: `set-bedroom-brightness` looked like it needed tool
  precision, but the percentage is what the attribute records (30% is brightness 76 of 255),
  and its two tool expectations disagreed on the grounding slot. A case with a loose end state
  (`implicit-cold`) stays tool-scored. The baseline artifact was re-run keyed on 2026-08-06 so
  its scoring basis matches; it now reads 19 LLM-correct / 3 wrong / 4 unjudged over 26 cases,
  and the two
  predictions the stale artifact carried (the `weather` fixture defect, and the effect
  telemetry that would judge `start-timer` and `add-shopping-item`) both held (see
  `evals/README.md`).
- **`synthetic_home`: adopted at the format level only, not the component.** The three levels
  of coupling: (1) the inventory *format*, our own parser, no dependency; (2) the PyPI library
  `synthetic-home` (Apache-2.0, `requires_python >=3.13`) as a pinned test-only dep behind an
  adapter, our own instantiation; (3) the `home-assistant-synthetic-home` custom component
  (MIT), loaded into the test HA. We take Level 1 and refuse Level 3. Level 3 is the costly,
  risky path: it is not a pip package (PYTHONPATH or a `custom_components` symlink), it does
  not replace `world.py`'s local-agent setup or the satellite timer-device shim, and it hands
  entity instantiation to a third party, which is exactly where we need control (our backing
  exposes `HassLightSet` only when a light advertises brightness; state-diff needs the service
  to actually actuate). `backing.py` keeps building the world; the `expect_changes` / `setup`
  case format is the piece we converged toward. Level 2 stays available if we later want to
  track allenporter's evolving datasets directly; if taken, the dep goes in a test-only
  requirements file (never `manifest.json`, never `custom_components/`), pinned exactly and
  treated as a baseline input, isolated behind one adapter module, with any vendored dataset
  files frozen and attributed.
