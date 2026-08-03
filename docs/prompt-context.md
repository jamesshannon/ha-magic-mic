# Prompt-Context & the LLM I/O Contract

> Shared-primitive doc (PRODUCT_PLAN §5.6: "prompt-context / entity summary +
> retrieval"). Two halves, both here now: **the output/interaction contract**
> (§§ below: how the model returns speech, actions, meta-signals; the
> generation-count economics; verbal behavior) and **the input/prompt-budget
> design** (§"Prompt budget: request-conditioned context": what entity context we
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
  **entity summary** (floor→area→domain→device-class + counts) plus the user's
  own words plus `find_entities` for the tail is correctness-complete. The summary is
  bounded by home *structure*, not entity *count*.
- **Inject a small, request-conditioned name subset** on top of the summary:
  room-scoped (`device_id → area`; floor is too coarse) ∩ request-relevance
  (domain keywords + fuzzy-name match). This is **`find_entities` run *proactively*
  at prompt-build** — a *third consumer* of its scorer/guard primitive. Keyword +
  fuzzy, **not embeddings** (§5.3: entities are bounded/structured).
- **Cache is a within-conversation and within-command-loop lever, not a
  cross-conversation one.** Keep a stable cached prefix (instructions + tools +
  entity summary) with a breakpoint; the request-conditioned names + history + memories
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

**Proving-ground freedom vs core relevance.** We fork the `anthropic` component, so the
experiment may use a structured envelope if that is cleaner. Keep deterministic capability
logic independent of the Anthropic wire format, but do not pretend the shell or capability
files will copy into core unchanged. The useful outputs are measured behavior and explicit
contracts; core's current free-text + `tool_use` + `continue_conversation` seams determine
where an upstream implementation would ultimately belong.

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

Regardless of how yes/no is recognized, the **operation being confirmed is not reconstructed
from chat prose**. The shell first stores a normalized, immutable pending operation in the
ChatLog's conversation-scoped sidecar. The next answer only approves or rejects that record.
`ask_question` is one possible recognition front-end; an LLM-interpreted reply is another.
Both execute the same stored operation through the same policy check.

The main LLM still writes the confirmation question in v1. The immutable record prevents the
approval turn from changing the call, but it does not verify that the question faithfully
described the staged operation. Stronger rendering and step-up alternatives are deliberately
deferred in [`security.md`](security.md#deliberately-excluded-confirmation-mechanisms).

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
   inferable from "did a control intent fire this turn" — more reliable than trusting
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

## Response brevity & deterministic shaping

Verbosity is the top recurring complaint about voice assistants (Google especially: too
many words). The fix is **not** uniform terseness — that discards *necessary* speech — but
**modality scaled to what the user doesn't already know**, driven where possible by
deterministic signals rather than the model's discretion. Three separable failure modes,
needing different fixes: *uniform explicit confirmation* ("OK, turning on the living room
lights," every time → earcon-first, above); *chatty filler* ("Sure!", "Anything else?" →
prompt policy); *over-explaining the substantive answer* (the verbose content itself → the
policy here).

### The decision is a function of deterministic signals, not model mood

Earcon vs. terse-confirm vs. explain vs. answer is drivable by `(resolution-confidence,
tool-outcome, consequence-class)` — signals we already compute:

| Situation (deterministic signal) | Modality |
|---|---|
| Exact name / entity_id, tool succeeded, observable + reversible | **earcon**, no words |
| Our fuzzy layer fired with low top-1/top-2 margin ([`find-entities.md`](find-entities.md)) | **terse confirm** naming the resolved entity ("turning off the *kitchen* light") |
| Invisible or high-consequence action (setpoint, message, [`security.md`](security.md) tier) | **confirm** even on high confidence ("behavioral write → confirm") |
| Tool failed (matched-but-unavailable / no-match / service-error) | **spoken explanation** (below) |
| Query | **the value, terse** ("72", not a wrapped sentence) |

So verbosity is *computed*, not emergent — the reason we can beat Google. The LLM's freedom
is bounded to *wording* the failure/query, and even that is canned for common cases.

### Failure breaks the earcon path — and the break is deterministic

The earcon-only branch is **gated on actual success** (`success_results` non-empty, no
`unavailable` targets). An unreachable light is not a success, so it can never reach the
earcon branch — the signal forces speech. Interpretation is a **gradient**, not an LLM
guess: HA returns a **typed** outcome, so a **canned template per error-type** ("I couldn't
reach the living room light") is fast, offline-capable, and TTS-pre-cacheable
([`offline.md`](offline.md)); richer causal narration ("it may be powered off at the
switch") is the [`explainability.md`](explainability.md) pattern on top (structured record,
LLM *narrates* only, "can't tell why" a first-class value).

### Earcon-default is undo-safe — scaled by consequence

The irreducible hard case: the LLM resolves **in its head** ("the light by the couch" →
emits an exact `entity_id`), hiding the inference, so it looks like a perfect match and we'd
earcon. Two things close it: (a) for **observable + reversible** actions, earcon-default is
safe because a wrong result is visible and [`undo.md`](undo.md) catches it (optimism
underwritten by undo, doing double duty here); (b) for **invisible / high-consequence**
actions, confirm regardless. Consequence-class is deterministic (it's the domain/action).

> **Caveat — clean signals will be harder to get than the table implies.** The policy
> *assumes* crisp `(confidence, outcome, consequence)` values; each is fuzzier in practice:
> in-head LLM resolution is invisible by construction (no confidence to read), confidence
> calibration across domains/languages is unsettled ([`find-entities.md`](find-entities.md)),
> and consequence-class has genuine edge cases (is "set the thermostat to 90" high-consequence?).
> Treat the table as the *target* policy — expect the **signal-extraction** to be the real
> work when we build it, not the decision logic. Not worth digging deeper until then.

### The verbosity dial (a global default-good setting)

A single global **verbosity setting** (terse ⇄ conversational) — *not* runtime interrogation
(forbidden), the same class as the personality/prompt-template surface (PRODUCT_PLAN §6.1).
Default **terse-with-earcons**; dial up for users who want chat. It sets the prompt directive
*and* how aggressively the earcon path suppresses speech. Never inferred from behavior — a
rare explicit setting, default good.

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
  downstairs" = `HassTurnOff(floor=downstairs)`. The entity summary serves these fully.
- **Named commands: the user supplies the name.** The model doesn't need the roster
  to *know* the name — it needs the summary to know *there are lights in the
  living room* (so it can `find_entities(domain=light, area="Living Room")`), which
  the summary's counts provide.

So **entity summary + fuzzy resolution is correctness-complete without the roster.** What
the roster buys is only the *zero-lookup fast path* (the model already had the
exact name). We preserve that fast path selectively, below.

> **Pruning is prompt-only.** Everything exposed stays intent- and
> `find_entities`-reachable (exposure is unchanged). We're deciding *pre-loaded* vs
> *fetched*, never *reachable* vs *not*.

### The other budget: a hard tool-count cap (not tokens)

Token size is the *soft* budget above. There is also a **hard** one, easy to miss
because it isn't about tokens: **the total number of tools/scripts exposed to the
model cannot exceed 128** — HA's docs are explicit that going over "will cause the
conversation engine to fail with a hard limit error inherited from the underlying
LLM API" (`exposing_scripts_to_llms.markdown`). This is a *count* ceiling, and it's
a hard failure, not a slowdown.

It bites us specifically because our proxy is **additive**. The 128 is shared across
*everything* in the tool list: every exposed script (`ActionTool`, one per script) +
every capability tool we add (`find_entities`, calendar-write, reminders, memory,
weather, undo, `web_search`/`web_fetch`, any SKILL surfaced as a tool) + every tool
from a **merged** third-party provider (§6.2 `MergedAPI` multiplies the count). A
home that already runs near the cap on scripts alone can be pushed over by us.

**Consequence:** request-time capability selection is correctness machinery, not only
prompt-budget hygiene: it must keep a multi-provider, many-script home under 128 without
making valid capabilities unreachable. The filtering → Tool RAG → budget assembly →
discovery-fallback design lives in
[`capability-selection.md`](capability-selection.md). Selection is not the security or
intentionality boundary.

**Also hard-capped: description lengths.** The same API-inherited limits bound tool
authoring — roughly **1024 characters** per tool/script description and **128
characters** per field (parameter) description (`exposing_scripts_to_llms.markdown`;
exact numbers vary by provider). Capability tool descriptions and SKILL tool-facing
text ([`skills.md`](skills.md)) must fit; a long "when to call this" description gets
rejected or truncated. Keep descriptions dense, not long.

### Tier 1 — the always-injected entity summary

Floor→area→domain→device-class tree, with counts (e.g. "Living Room: 4 lights,
2 covers, 1 media_player"). Bounded by home **structure**, not entity count —
small and roughly constant regardless of home size. It grounds the model, anchors
all area/floor/domain commands (no names needed), and tells the model what exists
so it can `find_entities` the rest. This is the stable, cacheable anchor.

This is an **Assist API strategy**, not a rewrite of every HA LLM API prompt. API
preparation finds the selected `assist` contribution, replaces that member with
`EntitySummaryAssistAPI`, and leaves every other registered API unchanged before HA merges
them. The preparation result records whether the summary was actually applied. Tier-2 names
and the `find_entities` pairing rely on that effective result, not merely on a configuration
flag. An eventual core implementation should put this choice inside Assist prompt assembly;
conversation providers such as Claude and Ollama consume the resulting provider-neutral
`APIInstance` without owning the strategy.

### Tier 2 — request-conditioned name injection (the fast path, bounded)

On top of the summary, inject exact names for a **small, relevant** subset so the
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
  lookup, not a failure** (the summary still lists what exists). Recall-oriented.
- **The keyword map must be *localized*, not hardcoded English** (PRODUCT_PLAN §5.7).
  Derive it from HA's **localized device-class / domain strings** (e.g. `cover/strings.json`
  `entity_component` names) so it's dynamic per language — a hardcoded English dict both
  fails non-English homes and blocks core-merge. Because it's only a recall *booster*
  (misses degrade to a lookup), a thin/absent map in some language is graceful, not fatal.
  Matching reuses Hassil normalization and RapidFuzz's Unicode-aware processor. When the
  installed HA intents for a language set Hassil's `ignore_whitespace` option, a complete
  translated domain term may also match inside the whitespace-free utterance. Terms are not
  split into invented character n-grams.
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

#### As built (wave1-prompt-context)

`capabilities/prompt_context.py::select_request_names`, plus the shared
`entity_candidates.build_candidate` adapter and `async_domain_keyword_map`. The candidate
set is every exposed entity; where an entity sits sets how easily it qualifies (room scope
is a soft prior, Refinement B below, built rather than deferred):

Magic Mic's summary and request-name instructions are loaded from
the integration's `conversation` translation category for each request language. The same
typed string bundle builds the request's `find_entities` tool schema. This localizes only
Magic Mic-owned additions; prompts and tool text contributed by HA core remain upstream
concerns.

Both registry-derived prompt blocks use readable quoted records inside stable markers. A
localized instruction says that quoted values are data, not instructions. Newlines and other
control characters in registry values are normalized, each value is capped at 160 characters,
and each complete block is capped at 8,192 characters with an omission marker. The entity
summary emits one line per area/domain pair; request-name injection emits one
`name`/`entity_id` line per selected entity. Aliases remain relevance-scoring input and are
deliberately not copied into the prompt.

This is proportionate robustness and defense-in-depth, not a primary security boundary.
Area/floor names, aliases, and user-assigned entity names are administrator-controlled; an
owner deliberately injecting their own assistant already has greater authority. Integration-
or device-provided friendly names are the narrower indirect-input case. Formatting cannot
make them trustworthy, so tool policy and external-egress defaults remain the enforcement
boundaries ([`security.md`](security.md)).

- **In the requesting area** (device area inherited as HA does): admitted at
  `NAME_INJECTION_FLOOR` (55, at or below the resolution floor: a spurious inclusion wastes
  tokens, not a wrong action), with keyword widening (its domain being named floors it in
  even with no name match) and a `NAME_INJECTION_ROOM_BONUS` (10) added for ranking, so it
  sorts above an equal-scoring entity elsewhere. Widening is room-only because unbounded it
  would inject a whole domain (every light in the house).
- **Elsewhere in the house**: admitted only above the higher `NAME_INJECTION_HOUSE_FLOOR`
  (75), so an explicit reference ("turn off the kitchen ceiling light" from the living room)
  reaches its entity while an incidental one-token overlap does not.
- **No area at all** (typed input, no satellite): every entity scored at the normal floor
  with no widening, since there is no room to prefer.
- Name relevance is union `token_set_ratio` (the shared scorer). Top-N is
  `NAME_INJECTION_LIMIT` (10). The keyword map is derived from `entity_component`
  translations (each domain's display name plus its device-class names), so it is
  localized, not a hardcoded English dict. It is deliberately thin: canonical terms
  ("Light", "Blind"), not colloquial synonyms ("lamp", "music"), and misses degrade to
  fuzzy-name plus one lookup. The scorer's Hassil normalization is shared with
  `find_entities`; thresholds still require per-language corpus calibration rather than
  extrapolation from English.

Refinement A below was weighed during that build and deferred; it is recorded so it is not
re-derived from scratch. Revisit it when the eval says the fast path is leaving turns on
the table.

> **The first live measurement says the fast path is not being used at all** (see "Measured
> (Wave 1, first name-injection run)" under Measurement below). On the golden set, injected
> names were read once and changed nothing, because the stock intent tools resolve a spoken
> name themselves. This whole tier is flagged for a revisit against a realistic corpus
> before it is built on further; read that section before tuning Tier 2 or adopting
> Refinement A or Option 2.

#### Refinement A — domain-name decoration instead of keyword widening

An alternative to the separate keyword filter: append each entity's localized domain and
device-class name to its `Candidate` context (alongside area and floor), then let plain
`token_set_ratio` carry domain relevance. A blind in the kitchen scores against "close the
kitchen blinds" through its "Cover"/"Blind" tokens, with no utterance keyword-parse step.

- **For.** It subsumes widening for injection and grades it: a light that also matches on
  name outranks a bare domain match, instead of everything flat at the floor. It drops the
  `keyword_domains` utterance loop. Because the adapter is shared, it also helps
  `find_entities` fuzzy lookups that omit a structured `domain` (the "ask for a bedroom
  light, the entity is a bedroom lamp" case), which widening cannot reach: widening only
  fires at prompt-build.
- **Against.** The resolution consumers (`find_entities`, the match-layer fallback) filter
  domain **structurally** and should (PRODUCT_PLAN §1: exhaustive filtering is
  deterministic code's job). When the model passes `domain=light`, the candidate set is
  already all lights, so a uniform "Light" token on every document compresses the top-1/
  top-2 margin the ambiguity guard reads. That is the failure the IDF tie-break
  ([`find-entities.md`](find-entities.md)) was added to fight; IDF absorbs it for sets of
  five or more but not for a two-lamp office on the plain-union path, where decoration can
  turn a clean resolve into "ambiguous."
- **And** it blends the domain signal into the score, so the explicit "domain-only match"
  flag that lets us room-gate widening is gone; the no-room flood it prevents does not go
  away, it just gets harder to gate.
- **If revisited:** decorate in the shared `build_candidate`, drop injection's widening,
  and gate on the resolver micro-benchmark (the guardrail regimes, `false-resolve == 0`).
  If small homogeneous sets regress, scope decoration to the injection candidate build
  only (an `extra_context` arg), leaving the resolution consumers on structured domain.

#### Refinement B — soft room scope with a house-wide strong-match escape (built)

The reason room scope is a soft prior and not a hard gate. A hard gate drops an explicit
cross-room reference ("turn off the kitchen ceiling light" spoken from the living room): the
kitchen entity is not in the room's candidate set, so nothing is injected and the turn falls
to a lookup. The as-built admission (above) models it as three sources into one candidate
set instead: room fuzzy and room widening at the normal floor (the prior, admitted
generously), and house-wide fuzzy at a higher floor (strong, confident matches only, the
escape for utterances that name a specific entity elsewhere). A weak house-wide match is the
noise room-scope exists to suppress; only a specific reference clears the bar.

Two properties keep it bounded: room entities carry the ranking bonus so they sort above
equal-scoring house-wide ones, and top-N still caps the total. The cost is that the fuzzy
pass runs over all exposed entities, not just the room (still bounded hashmap/rapidfuzz
work, O(all) not O(room)). Note the escape cannot distinguish "kitchen ceiling light" from a
bare "ceiling light" that happens to match a same-named light in another room, since
`token_set_ratio` scores a name subset at 100 either way: both cross the house floor, so a
repeated generic name injects its siblings house-wide, bounded by top-N and out-ranked by
the room. Whether that recall is worth the tokens is an eval question; the thresholds
(`NAME_INJECTION_HOUSE_FLOOR`, `NAME_INJECTION_ROOM_BONUS`) are the tuning surface.

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
   ≥2-generations multiplier (and thus softens the summary's extra-lookup cost).
2. **Across turns within a conversation** — continued-conversation turns are
   seconds apart, inside the TTL; turn 1's prefix + accumulated history is warm for
   turn 2.

So structure the prompt for that:

- **Stable cached prefix** (cache breakpoint after it): instructions + tool schemas
  + **entity summary**. Doesn't change mid-conversation.
- **Volatile tail** (inherently uncacheable, and that's fine): **request-conditioned
  names** (change per turn by design) + history + tool results + retrieved
  memories. Small, so re-prefilling each turn is cheap.

Two nuances:

- **The cold first utterance is the only fully-uncached moment — and it's the
  TTFT villain.** Caching rescues gens 2+ and turns 2+, never the first response.
  That's *why* keeping the cold prompt small (entity summary + filtered names, not the
  8–20k roster) is the win caching structurally can't provide. Summary-first and
  intra-conversation caching point the **same** way.
- **Mind the cache minimum** (~1024 tokens on most models). A lean summary + tools
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
should inform the core conversation-trace enrichment path evaluation.md already lists
as a work-item; over-instrument the experimental shell freely, and
**disclose** what's captured (token/timing = benign; utterance/entity content or
anything leaving the box needs explicit opt-in).

#### Measured (Wave 1, first name-injection run)

`evals/harness/variant.py` ran the 25-case golden set through the testbed agent twice,
satellite in the living room, names off then on, model `claude-haiku-4-5`. Task-success
was equal (rescored through R23: 15 LLM-correct, 4 wrong, 6 unjudged in both arms).
Generations moved by −2 (47 → 45), but
`find_entities` was called zero times in either arm, and the two cases that shifted
(`implicit-cold`, `conditional-reminder`) sit outside the requesting room and carry no
injected name, so that −2 is run-to-run model variance, not injection (a first run showed
0). The stock intent tools (`HassTurnOn`/`HassTurnOff` and the rest) take a spoken `name`
and resolve it themselves, so the model never reaches for the entity_id lookup injection
exists to skip. The corpus has no command that forces one.

This stored artifact predates the R24 paired-order fix: it ran the complete names-off arm
before the names-on arm. Current runs pair arms per case, alternate order across cases, and
record per-case deltas. The historical result remains useful for the conclusion above
because the changed cases could not receive the feature, but its small aggregate resource
delta is not evidence of savings.

Option 1's cache cost showed up as designed: names-on created 42,707 cache tokens against
names-off's 5,273 (per-turn names bust the system-block cache) and read 46,034 fewer.
Output tokens fell 549.

So on this corpus the injection buys nothing in turns while adding cache churn, because
Tier 2's fast path is never taken. The one case that read an injected name (`set-volume`,
names-on passed `name="Living Room Speaker"`) resolved identically to names-off, which
passed `area=living room, domain=media_player`: the name was a second route to a target
the entity summary and satellite room already reached, not a new capability.

**What was and was not tested.** Three layers can carry entity data into the prompt: the
full roster (stock Static Context, in neither arm), the entity summary (Tier 1, in both
arms), and the Tier-2 names (the only thing toggled). This run measured Tier 2 and found it
near-valueless here; it did not test removing the summary, and the roster was absent from
both arms. The separate Wave 0 baseline ran the full roster and also scored 21/4, hinting
the roster's names went unused too, but under an area-less run with a different cache
regime, so that is a lead, not a match. A stock HA Assist with the roster removed would
resolve this same class of command, for the reason in the run summary above: the intent
tools and `GetLiveContext` do the resolution, not the prompt.

**Why this may generalize, and the revisit it earns.** The corpus caveat cuts less than it
looks. Its commands are the realistic ones: name the device, address a room, or ask about
state. The utterances where a pre-loaded name pays are the oblique ones ("the thing by the
couch", "it's stuffy in here"), and those are rarer in real speech than this design
assumed. If that holds, request-conditioned name injection (and perhaps pre-loaded entity
context in general) is dead weight in the common case, and the response is not Option 2 but
stepping back from prompt-loaded names toward lookup-only. This is one model (haiku), 25
curated cases, one home, so the effect is likely model-dependent: a weaker model may
fabricate names without a roster, a stronger one need it even less. It is enough to flag,
not to decide.

**Revisit the whole prompt-budget approach around 2026-09**, sooner if field or fleet data
lands. Before building further on name injection, re-measure against a larger, realistic
corpus that includes oblique references and any entity_id-only tools, across more than one
model. The immediate follow-up that would settle it: add corpus cases that force an
entity_id lookup (where injection can shave the `find_entities` round-trip). If those also
resolve without names, the roster is dead weight for capable models and Tier 2 should come
out; only if they do not does Option 2 (the cache-isolated second system block) become a
real question. Until then Tier 2 stays on by default (task-neutral, and its cost is cache
tokens, not turns), but it is a candidate for removal, not just tuning. Artifact:
`evals/results/wave1_name_injection.json`.

### Two tiers of observability (local vs fleet)

The above is **local, per-install** observability. Population questions—conversation-gap,
home-size, same-room targeting, lookup/disambiguation, and generation distributions—are
deployed-use telemetry, not corpus evaluation. The opt-in, content-free, locally aggregated
fleet design and its privacy constraints live in [`telemetry.md`](telemetry.md).

Until we have fleet data, the priors in this doc (room-scope, bursty traffic,
budget sizes) are **stated assumptions** — validate against whatever's available
(own install, synthetic large registries, external corpora like HomeBench, §Part E
of evaluation.md) and revisit the aggressiveness of pruning when real numbers land.

---

## Open questions

- **Do we adopt a struct-envelope shell, or stay free-text + typed blocks?** Lean
  typed blocks (HA-native and streams naturally); revisit the envelope only if
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
- **Input half** — entity-summary format + real token counts (§9).

---

## Key references

- `helpers/llm.py:50` — `DEFAULT_INSTRUCTIONS_PROMPT` (the minimal stock prompt)
- `anthropic/entity.py:1201/1250` — the tool loop (breaks on no unresponded results)
- `anthropic/entity.py:611-620` — typed block dispatch (`TextBlock` vs `ToolUseBlock`)
- `conversation/chat_log.py:376` — `unresponded_tool_results`
- `conversation/chat_log.py:448/461` — concurrent tool dispatch, awaited before gen2
- `conversation/chat_log.py:356` — the "?" mic-open heuristic
