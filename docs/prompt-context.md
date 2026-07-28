# Prompt-Context & the LLM I/O Contract

> Shared-primitive doc (PRODUCT_PLAN §5.6: "prompt-context / taxonomy skeleton +
> retrieval"). Two halves, both here now: **the output/interaction contract**
> (§§ below — how the model returns speech, actions, meta-signals; the
> generation-count economics; verbal behavior) and **the input/prompt-budget
> design** (§"Prompt budget: request-conditioned context" — what entity context we
> inject, the cache model, and how we measure it). Grounded in the `anthropic`
> conversation loop and `helpers/llm.py`. Cross-refs
> [`voice-streaming.md`](voice-streaming.md) (streaming/TTFT latency),
> [`conversation-loop.md`](conversation-loop.md) (mic-open is one meta-signal
> instance), [`find-entities.md`](find-entities.md) (the scorer reused for
> injection), and [`evaluation.md`](evaluation.md) (how the budget change is
> measured / regression-gated).

---

## TL;DR

- **There is no structured response envelope today — it's a hybrid.** Actions are
  structured (`tool_use` blocks); the spoken message is **free text** streamed to
  TTS; control signals (mic-open) are a **heuristic** ("?" at the end,
  `chat_log.py:356`). Three fields, three different mechanisms.
- **A `tool_use` is never terminal.** Emitting one ends the generation
  (`stop_reason: "tool_use"`); the model can't know the result yet, so the spoken
  confirmation is always a **second generation**. HA loops until no unresponded
  tool results (`anthropic/entity.py:1201/1250`). So: pure chat = 1 generation;
  **any tool-using command = ≥2 generations.**
- **A return-struct is defensible, not free.** Streaming isn't lost (order
  `message` first + incrementally parse the streamed JSON), but you pay
  parse-complexity + a single committed output shape. The cleaner win on our stack
  is that Anthropic **already** separates channels via typed blocks — text deltas
  vs `tool_use` — so meta rides as blocks, not sniffed out of prose.
- **The one true single-generation optimization is a struct *field*, not a tool.**
  A `terminal_intent` field HA executes itself (speak the model's optimistic
  `message` on success, drop to gen2 only on failure) saves the second generation
  *because no `tool_use` was emitted*. A tool call structurally can't — it forces
  the round-trip.
- **Never a dedicated `set_metadata` tool** — that forces a wasted 3rd generation.
  Meta rides on the final response, or on a tool already being called, or is
  **deterministically inferred** (e.g. mic-open from "did a command fire").
- **Verbal behavior needs prompt policy the default lacks.** The stock prompt says
  nothing about fillers; a "checking…" before a sub-second command is worse than
  silence. Policy: no preamble for fast actions; an **earcon** (`acknowledge.mp3`)
  for acknowledgment; a spoken filler only for genuinely slow tools.
- **Per backend:** cloud Claude → native typed blocks; local text-only model →
  **grammar-constrained JSONL** (structure *and* enforced validity), the honest
  §5.4 story (force determinism locally, don't hope for it).

**Prompt budget (input half):**

- **Today's default is the full entity roster** (`homeassistant/llm.py` dumps every
  exposed entity's names/domain/areas) — scales with entity count (~8–20k tokens
  for a 500–1000-entity home), and it's re-prefilled **per generation**.
- **The roster is a crutch for exact-match.** Once resolution is fuzzy
  (match-layer fallback + `find_entities`), it's redundant for correctness — the
  **taxonomy skeleton** (floor→area→domain→device-class + counts) plus the user's
  own words plus `find_entities` for the tail is correctness-complete. Skeleton is
  bounded by home *structure*, not entity *count*.
- **Inject a small, request-conditioned name subset** on top of the skeleton:
  room-scoped (`device_id → area`; floor is too coarse) ∩ request-relevance
  (domain keywords + fuzzy-name match). This is **`find_entities` run *proactively*
  at prompt-build** — a *third consumer* of its scorer/guard primitive. Keyword +
  fuzzy, **not embeddings** (§5.3: entities are bounded/structured).
- **Cache is a within-conversation and within-command-loop lever, not a
  cross-conversation one.** Keep a stable cached prefix (instructions + tools +
  skeleton) with a breakpoint; the request-conditioned names + history + memories
  live in the uncacheable tail. The **cold first utterance** is the only fully
  uncached moment and the TTFT villain → keeping it small is the win caching can't
  provide.
- **Measure it, both ways:** the change trades tokens for generations, so the
  honest scorecard is **end-to-end** (total tokens across *all* generations +
  TTFT/TTLT) at **fixed task-success**. Quality-neutrality here = *task-success
  equality*, not *token equality*. Cache effectiveness is directly observable from
  Anthropic's `usage` counters (`cache_read` / `cache_creation`). See
  [`evaluation.md`](evaluation.md).

---

## The output contract today

The stock voice prompt is minimal (`helpers/llm.py:50`):

> "You are a voice assistant for Home Assistant. Answer questions about the world
> truthfully. Answer in plain text. Keep it simple and to the point."

No structure, no filler/latency guidance. The model's output is a stream of typed
content blocks — `TextBlock`s and `ToolUseBlock`s, delivered as distinct events
(`anthropic/entity.py:611-620`). So the three things a designer might want as one
struct are actually three mechanisms:

| Desired field | How it works today |
|---|---|
| `terminal_intent` / action | **Structured** — a `tool_use` block (IntentTool). Already have it. |
| `message` (spoken) | **Free text**, streamed block-by-block to TTS. |
| `leave_mic_open` | **Heuristic** — derived from the message ending in "?" (`chat_log.py:356`), *not* model-emitted. |

---

## The generation model (foundational)

A `tool_use` block is **never terminal**. When the model emits one the generation
ends with `stop_reason: "tool_use"` — it has yielded to get the result, and *at
that instant it doesn't know the outcome* (matched? service raised?), so it
cannot also speak a confirmation. HA enforces this: `_async_handle_chat_log` loops
`for _iteration in range(MAX_TOOL_ITERATIONS)` (`anthropic/entity.py:1201`) and
breaks only when `not chat_log.unresponded_tool_results` (`:1250`);
`unresponded_tool_results` is true whenever the last block is a `tool_result`
(`chat_log.py:376`). Consequences:

- **Pure chat, no tool → 1 generation** (`stop_reason: end_turn`).
- **Any tool-using command → ≥2 generations**: gen1 emits `tool_use`; the loop
  runs the tool, appends `tool_result`; gen2 sees it and speaks. The confirmation
  is *always* a separate generation from the tool call. (The model may stream
  preamble text *before* the `tool_use` in gen1 — "Sure, one sec" — but that's
  pre-result narration, not confirmation.)

This count is the yardstick for every optimization below. Each model generation is
a full API round-trip whose latency is TTFT prefill + generation
([`voice-streaming.md`](voice-streaming.md)); shaving one is worth real UX.

---

## Structured envelope vs free-text + tool_use

**Correcting a tempting overstatement:** a return-struct does *not* forfeit
streaming. Anthropic streams a tool/struct's input as partial-JSON deltas
(`input_json_delta`), so with `message` ordered **first** you can incrementally
parse and feed its string to TTS token-by-token, exactly like free text — trailing
meta fields arrive after the spoken part. The real cost of an envelope is
**incremental-JSON-parse complexity** + **committing to one output shape**, not
lost streaming.

But on our stack the envelope mostly solves a problem the API already solved.
**Typed blocks separate channels out-of-band for free** — text deltas → TTS,
`tool_use` → actions/meta — with no false-positive risk, no escaping, no prompt
fragility, and the model *trained* to use `tool_use`. So prefer typed blocks;
reach for in-band framing only when you have a single text channel:

| Framing | When it fits | Watch-outs |
|---|---|---|
| **Typed blocks** (text vs `tool_use`) | Cloud Claude / any structured-streaming API | none material — this is the grain |
| **`{` sentinel** → stop TTS, parse rest | last-resort single text channel | false positives (speech contains `{`); trailing-only; prompt-fragile control token |
| **JSONL** (one JSON object per line) | single channel, want interleaving | per-line buffering (mostly *overlaps* TTS clause-chunking, so cheap **if lines ≈ sentences**); needs valid-JSONL discipline |
| **JSONL + grammar (GBNF)** | **local text-only model** | the fragility fix: structure *and* enforced validity |

**Shell freedom vs core portability.** The conversation shell is deliberately
throwaway (§5.5) — we fork the `anthropic` component anyway — so we're free to make
*our* shell a structured-envelope shell if it's cleaner for us. Two constraints:
(1) portable **capabilities stay tool-shaped** (`find_entities` et al. depend on
`hass`/`llm`, never on our envelope); (2) core's conversation contract is
free-text + `tool_use` + the `continue_conversation` heuristic, so a struct
*loop* won't copy/paste into core — but that loop was always throwaway; the
capabilities port regardless.

---

## The terminal-intent fast path (the one real single-generation win)

The only way to truly collapse a simple command to **one** generation is a struct
**field**, *not* a tool call — because a `tool_use` forces the round-trip by
protocol, always, whereas a field HA reads and executes itself does not:

> Model's final generation emits `{message, terminal_intent}` **optimistically** →
> HA fires the intent → **success: speak `message`, done (1 generation)** →
> **failure: fall back to gen2** with the error so the model recovers.

HA can verify what the model can't (did the service call raise?) — the same bar
the hassil path already uses for its canned "Turned on the light." Happy path = 1
generation; error path = 2 (no worse than today). It's an **opt-in fast path** the
model selects for *simple, single, terminal* commands; it uses the normal tool
loop when it must chain intents or needs the result to answer.

**Why a tool can't do this job — and the async-tool distinction.** "Fire-and-forget,
ignore the return" isn't in the protocol: every `tool_use` id requires a
`tool_result` before the model continues (HA gathers tool calls concurrently but
**awaits all** before gen2 — `chat_log.py:448/461`). You can *simulate* async by
having a tool's `async_call` spawn the real work as a background task
(`hass.async_create_task`) and return an immediate **stub** result — but that only
saves the tool's **wall-clock latency**, not the second generation (gen2 still
needs its own round-trip). The generation boundary survives any `tool_use`. So the
thing you can't get from an async tool (escaping gen2) is exactly what the
struct-field buys.

**Trade:** the field's `message` is committed *before* HA knows success — hence the
optimistic-with-fallback handshake. Reserve it for commands whose only realistic
failure is "couldn't do it," where falling back to gen2 is fine. Optimism here (and the
optimistic memory/music writes elsewhere) is underwritten by **deterministic undo**
([`undo.md`](undo.md)) — "act now, cheaply reverse" only works because reversal is real.

### Candidate lever: `ask_question` for confirm-before-write (deterministic, no gen)

A separate single-generation lever, logged here so it isn't re-derived. **Behavioral
writes** (unlock the door, add an alias, a behavioral memory) must **confirm first**
([`memory.md`](memory.md), [`find-entities.md`](find-entities.md)) — and the naïve
confirm costs *two* extra generations: gen2 speaks "shall I?", the mic reopens, gen3
interprets "yes." Core's **`assist_satellite.ask_question`** (`entity.py:333`) collapses
that: it announces the question, captures one STT utterance, and matches it **locally
against a fixed answer set (`[yes, no]`) with no LLM** (pipeline truncated at STT,
`:481-484`) → a deterministic confirmation at **zero extra generations**, and its hassil
answer-match inherits HA's language coverage (unlike our English keyword maps).

**Why it's only a candidate, not adopted:** it inverts control — **HA drives** the
question mid-turn, so weaving an `ask_question` call into the middle of the LLM tool-loop
is novel, non-trivial control-flow (the loop expects the *model* to drive). And it only
fits **closed-set** confirmations; anything needing the model to interpret an open answer
stays LLM-in-loop (this is exactly why it's the wrong tool for disambiguation —
[`conversation-loop.md`](conversation-loop.md)). Evaluate against the plain
gen2-reopens-gen3 path once we can measure the confirm latency.

---

## Meta-signals in general

Beyond mic-open, expect more back-channel signals (confidence vs guessing,
"multi-part answer — keep listening," detected sentiment). The delivery rule:

1. **Deterministically infer whatever you can.** Mic-open after a command is
   inferable from "did a control intent fire this turn" — more robust than trusting
   the model to set a field ([`conversation-loop.md`](conversation-loop.md) §1).
2. **Struct field on the final response for model-only signals** — things with no
   deterministic source. Costs no extra generation (the final turn happens anyway).
3. **Never a dedicated `set_metadata` tool.** A separate meta tool_use forces a
   whole extra generation (call → ack → generate again) — the wasteful 3-generation
   pattern. If it must go through a tool, ride it as **extra args on a tool already
   being called**, never its own round-trip.

---

## Verbal behavior: filler & acknowledgment

The model *can* emit status text before a `tool_use` (gen1 preamble, streamed to
TTS): "Let me check…" [tool] → gen2 "It's 72°." But the default prompt says nothing
about *when*, and a filler before a sub-second `HassTurnOn` is worse than silence
(two utterances + a gap for something instant). Policy — prompt-driven, since the
model can't reliably know a tool's latency:

- **Fast device actions → no preamble.** Fire the intent; gen2 confirms.
- **Plausibly-slow tools (web search, forecast, external APIs) → one short filler.**
- **Prefer a non-verbal earcon for acknowledgment.** HA already ships
  `acknowledge.mp3` (cited in [`ambient-noise.md`](ambient-noise.md)); a "got it"
  chime at tool-fire decouples *"I heard you"* from *"here's the result"* — covering
  perceived latency without a spoken gen1 filler or the two-utterance awkwardness.

> **Two different earcons — don't conflate them.** This tool-fire "got it" chime plays
> **during an active response turn** and is our own to wire (there's no core primitive
> for "play a sound mid-TTS"). It is *not* the same as `assist_satellite`'s **`preannounce`**
> earcon, which prefixes an **announcement** (`announce` / `start_conversation`) and is
> the "⟨ding⟩" of the delivery engine's content-free reminder
> ([`scheduling-model.md`](scheduling-model.md)). Same idea (a chime), two distinct
> moments and two distinct mechanisms.

---

## Prompt budget: request-conditioned context (input half)

The *input* half of the primitive: what we put in the prompt on the way in. It's
the primary TTFT lever ([`voice-streaming.md`](voice-streaming.md)) because — per
the generation model above — the static context is re-prefilled on *every*
generation, so its size is paid ×(generation count) per command.

### The baseline and why it's mostly tax

Today the `homeassistant` platform injects the **full exposed-entity roster** as
"Static Context" — `yaml_util.dump(list(async_get_exposed_entities(...).values()))`
= names + aliases + domain + areas for every exposed entity (no state, no
`entity_id`). It scales **linearly with entity count**: ~15 tokens/entity → roughly
**8–20k tokens for a 500–1000-entity home**, re-prefilled per generation.

The roster's only job is to let the model map a spoken name → an exact registry
name *without a tool*. That's a **crutch for the exact-match resolver** (§2.4).
Once we have fuzzy resolution (the match-layer fallback + `find_entities`,
[`find-entities.md`](find-entities.md)), the roster is redundant for correctness.
Two things it was never needed for:

- **Area/floor/domain commands need zero names** (§2.3): "turn off everything
  downstairs" = `HassTurnOff(floor=downstairs)`. The skeleton serves these fully.
- **Named commands: the user supplies the name.** The model doesn't need the roster
  to *know* the name — it needs the skeleton to know *there are lights in the
  living room* (so it can `find_entities(domain=light, area="Living Room")`), which
  the skeleton's counts provide.

So **skeleton + fuzzy resolution is correctness-complete without the roster.** What
the roster buys is only the *zero-lookup fast path* (the model already had the
exact name). We preserve that fast path selectively, below.

> **Pruning is prompt-only.** Everything exposed stays intent- and
> `find_entities`-reachable (exposure is unchanged). We're deciding *pre-loaded* vs
> *fetched*, never *reachable* vs *not*.

### Tier 1 — the always-injected taxonomy skeleton

Floor→area→domain→device-class tree, with counts (e.g. "Living Room: 4 lights,
2 covers, 1 media_player"). Bounded by home **structure**, not entity count —
small and roughly constant regardless of home size. It grounds the model, anchors
all area/floor/domain commands (no names needed), and tells the model what exists
so it can `find_entities` the rest. This is the stable, cacheable anchor.

### Tier 2 — request-conditioned name injection (the fast path, bounded)

On top of the skeleton, inject exact names for a **small, relevant** subset so the
common case stays zero-lookup. Two filters, layered:

1. **Structural prior — room scope.** Entities in the requesting area
   (`device_id → area`; we already thread `preferred_area_id/floor_id`). **Floor is
   too coarse** — a 3-floor home is ~⅓ of all entities per floor, still a bomb.
   Most voice commands target the room you're in, so room-scope is the right grain.
2. **Request-relevance filter.** Narrow the room set by the utterance: **domain
   keywords** ("lamp/light" → `light`, "lock" → `lock`, "blinds/garage" → `cover`,
   "music/volume" → `media_player`) **plus fuzzy-name match** of request tokens
   against entity names/aliases. Take top-N.

**This is `find_entities` run proactively at prompt-build** — the *third consumer*
of its scorer + structured-filter primitive (after the match-layer fallback and
the reactive tool). Same code, run earlier, against the room-scoped candidate set.

Design constraints:

- **Keyword→domain is a recall *booster*, not the filter.** It misses implicit
  references ("it's dark in here" → lights) and names without the domain word
  ("turn on the Christmas tree"). So: domain-keyword widens, fuzzy-name catches the
  rest, room-scope is the backstop, and **a miss degrades to one `find_entities`
  lookup, not a failure** (the skeleton still lists what exists). Recall-oriented.
- **The keyword map must be *localized*, not hardcoded English** (PRODUCT_PLAN §5.7).
  Derive it from HA's **localized device-class / domain strings** (e.g. `cover/strings.json`
  `entity_component` names) so it's dynamic per language — a hardcoded English dict both
  fails non-English homes and blocks core-merge. Because it's only a recall *booster*
  (misses degrade to a lookup), a thin/absent map in some language is graceful, not fatal.
- **Keyword + fuzzy, not embeddings.** §5.3 settled it: entities are bounded and
  structured, so composed hashmap lookups + fuzzy over the room set suffice; vector
  search is for the *unbounded unstructured* stores (memory), not entities. Resist
  accreting embedding infra here.
- **Anaphora is history's job, not injection's.** "Turn *it* off" has no entity
  keyword; the referent is in the `ChatLog`, so injection and history complement
  rather than overlap.
- **Cost of conditioning:** entity-context assembly moves *into* the request path
  (can't prebuild at startup, since it depends on the utterance). But it's
  deterministic hashmap work (§5.3, sub-ms) — negligible next to one generation.

### Tier 3 — retrieval (unbounded stores only)

Long-term memories/notes via retrieval into the prompt — off the critical path,
parallel, budgeted (top-k + token cap). This is the *only* place embeddings are
warranted (§5.2 tier-3). Lives in the volatile tail (below), never the cached
prefix.

### The cache model (within-conversation, not across)

Caching is a real lever at two **sub-conversation** scales, and mostly wasted
across conversations (bursty traffic, 5-min TTL → the next conversation is cold):

1. **Within one command's generation loop** — gen2's prefix overlaps gen1's almost
   entirely, so gen2/gen3 re-read at cache-read rates. This *softens* the
   ≥2-generations multiplier (and thus softens the skeleton's extra-lookup cost).
2. **Across turns within a conversation** — continued-conversation turns are
   seconds apart, inside the TTL; turn 1's prefix + accumulated history is warm for
   turn 2.

So structure the prompt for that:

- **Stable cached prefix** (cache breakpoint after it): instructions + tool schemas
  + **taxonomy skeleton**. Doesn't change mid-conversation.
- **Volatile tail** (inherently uncacheable, and that's fine): **request-conditioned
  names** (change per turn by design) + history + tool results + retrieved
  memories. Small, so re-prefilling each turn is cheap.

Two nuances:

- **The cold first utterance is the only fully-uncached moment — and it's the
  TTFT villain.** Caching rescues gens 2+ and turns 2+, never the first response.
  That's *why* keeping the cold prompt small (skeleton + filtered names, not the
  8–20k roster) is the win caching structurally can't provide. Skeleton-first and
  intra-conversation caching point the **same** way.
- **Mind the cache minimum** (~1024 tokens on most models). A lean skeleton + tools
  may sit near it; if so, the within-conversation cache value comes mostly from
  *accumulated history*, not the static prefix.

### Measurement (see [`evaluation.md`](evaluation.md))

This change **trades tokens for generations** (a pruned prompt can add a
`find_entities` round-trip). So the metric must be **end-to-end per turn**, or
you'll "save tokens" while adding latency:

- **Cost:** total tokens summed across *all* generations in the turn — not the
  static prompt in isolation.
- **Latency:** TTFT + TTLT for the whole turn, including any added lookup.
- **Held fixed:** task-success rate. Quality-neutrality here is **task-success
  equality** (does it resolve the right entity / act correctly as often?), *not*
  the token-equality guard used for a pure caching change — we're deliberately
  changing tokens.
- **Regression gate:** a labeled `utterance → expected entity/action` set, run in
  CI under full-roster vs pruned, catches a recall drop the moment it appears.
- **Cache metrics for free:** Anthropic's `usage` object (captured in the stream
  handler — `entity.py` stashes `message.usage`) returns `input_tokens`,
  `output_tokens`, `cache_creation_input_tokens`, and `cache_read_input_tokens`
  per request — so cache hit rate is *directly measured*, letting us empirically
  confirm "cross-conversation caching is wasted" rather than assume it.

Enduring instrumentation (per-generation tokens/TTFT, resolution correctness)
should ride the core conversation-trace enrichment path evaluation.md already lists
as a work-item, so it migrates; over-instrument the throwaway shell freely, and
**disclose** what's captured (token/timing = benign; utterance/entity content or
anything leaving the box needs explicit opt-in).

### Two tiers of observability (local vs fleet)

The above is **local, per-install** observability — the trace/`usage` counters on
one box. Distinct from that is **fleet / phone-home aggregate telemetry**: the
population-level statistic you *cannot* get from one install — e.g. *"only 3% of
conversations start within 5 min of a prior conversation, across 10k users and 3M
conversations."* That specific number would **empirically settle the cache model
above** (is cross-conversation caching actually wasted?) instead of us assuming it.

Fleet telemetry is uniquely how you validate the **empirical priors this whole
design rests on** — none provable from a single install:

| Design assumption | Fleet metric that validates it |
|---|---|
| "Cross-conversation cache is wasted" (cache model) | inter-conversation gap distribution vs the 5-min TTL |
| "Most commands target the room you're in" (Tier-2 room scope) | % of resolutions where target ∈ requesting area |
| Token-budget estimates (~8–20k for 500–1000 entities) | exposed-entity count distribution; entities/area |
| Fast-path vs lookup economics | `find_entities` miss rate, disambiguation rate, generation-count distribution |

**But it cuts against HA's grain and does not ship in core.** Core is local-first
and doesn't phone home; the only precedent is the **opt-in `analytics` integration**
(anonymous, coarse — integrations/entity-count buckets), and it's deliberately
minimal. So fleet telemetry lives in the **proving-ground component and/or Nabu
Casa Cloud** (which already processes cloud conversations and is the natural
aggregation point) — never in the migratable capabilities, and always **opt-in +
disclosed + content-free** (timing/token/cache-hit/home-size-bucket/same-room-bool;
**never** utterances, entity names, or memories). Treat it as a design-validation
instrument for *us*, not a feature of the shipped capability.

Until we have fleet data, the priors in this doc (room-scope, bursty traffic,
budget sizes) are **stated assumptions** — validate against whatever's available
(own install, synthetic large registries, external corpora like HomeBench, §Part E
of evaluation.md) and revisit the aggressiveness of pruning when real numbers land.

---

## Open questions

- **Do we adopt a struct-envelope shell, or stay free-text + typed blocks?** Lean
  typed blocks (core-shaped, streams natively); revisit the envelope only if
  meta-signal volume grows.
- **Terminal-intent fast path** — worth the shell complexity (dual output shape +
  optimism/verify handshake) for the 1-generation win on simple commands? Measure
  gen2 latency first; if it's small under prompt-caching, the win may not justify
  the complexity.
- **Local JSONL+grammar path** — when (if) we support local models, is
  grammar-constrained JSONL the framing, and does it share a scorer/parse layer
  with the cloud typed-block path?
- **Filler policy** — encode as prompt text vs a small deterministic rule (earcon
  always at tool-fire, spoken filler gated on a tool "slow" flag we set per tool)?
- **Input half** — taxonomy-skeleton format + real token counts (§9).

---

## Key references

- `helpers/llm.py:50` — `DEFAULT_INSTRUCTIONS_PROMPT` (the minimal stock prompt)
- `anthropic/entity.py:1201/1250` — the tool loop (breaks on no unresponded results)
- `anthropic/entity.py:611-620` — typed block dispatch (`TextBlock` vs `ToolUseBlock`)
- `conversation/chat_log.py:376` — `unresponded_tool_results`
- `conversation/chat_log.py:448/461` — concurrent tool dispatch, awaited before gen2
- `conversation/chat_log.py:356` — the "?" mic-open heuristic
