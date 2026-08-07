# Fuzzy Entity Resolution → Canonical `entity_id`

> Shared-primitive doc (PRODUCT_PLAN §5.2, §5.6). Fixes the LLM path's **exact-match**
> name-resolution failure (§2.4). **Key reframe (see §"Where fuzzy belongs"):** the
> device-control fix is a **fuzzy fallback inside the intent match layer**, *not* a
> mandatory front-loaded `find_entities` tool. `find_entities`-the-tool survives for
> a narrower job: **resolution decoupled from immediate execution** (ephemeral
> automations, reminders, browsing). Both share one **scorer + ambiguity-guard**
> primitive. Depended on by device control, music search
> ([`music-playback.md`](music-playback.md), where disambiguation *inverts*),
> ephemeral automations ([`ephemeral-automations.md`](ephemeral-automations.md)),
> reminder targeting ([`scheduling-model.md`](scheduling-model.md)). First core PR (§7).

---

## TL;DR

- **The bug:** in the LLM path, name resolution is **exact**. `_filter_by_name`
  compares `name.strip().casefold()` against name + aliases, no edit distance
  (`helpers/intent.py:419/436`). "reading light" for "Reading Lamp" →
  `MatchFailedReason.NAME`. hassil's fuzzy matcher is **not** in this path (§2.4);
  *we* are the natural-language layer with no downstream safety net.
- **The seam:** `_filter_by_name` short-circuits and accepts a literal `entity_id`
  (`intent.py:428`). So a canonical `entity_id` in the model's hands makes any
  downstream targeting exact-by-construction.
- **The reframe (this doc's core finding):** don't force the model to call a
  lookup tool before every command. Put **fuzzy match as a fallback inside the
  match layer** — after exact fails. On the happy path it costs **nothing** (it
  hides inside the single `tool_use` that was already happening); the mandatory-
  tool approach costs an extra model generation on *every* command. See the
  generation count in §"Why in-match wins."
- **Disambiguation is a conditional round-trip, and it already works.** A tool
  call is never terminal — the model always gets a follow-up generation with the
  `tool_result`. On ambiguity we return the candidate list; the model asks a
  question; HA's **continued-conversation + chat-session** machinery reopens the
  mic and replays history. No new infrastructure (§"The round-trip").
- **`find_entities`-the-tool** is scoped to **decoupled resolution**: authoring
  `{trigger, condition, action}` for *later*, reminder targeting, "what do I have
  in the garage" browsing — where the model needs `entity_id`s as *data*, with no
  intent firing to piggyback on.
- **Shared primitive** (§5.6) = the **scorer + top-1/top-2 ambiguity guard**
  (rapidfuzz `token_set_ratio` + margin), with **three resolution consumers** here:
  the match-layer fallback (1), the tool (2), and entity arguments on script tools
  (3). Only one of the three is a tool. The scorer has further consumers outside
  resolution, each numbered locally in its own doc: proactive name injection at
  prompt-build ([`prompt-context.md`](prompt-context.md) Tier 2) and capability
  selection's miss recovery (§"The shared referent core").
- **Script tools were a second, uncovered failure path.** The match-layer fallback
  catches `MatchFailedError`, which only *intent* tools raise. An exposed script
  whose field is an `EntitySelector` asks the model for an `entity_id` that no
  prompt ever supplied, and the invented id went straight to the service call.
  Consumer 3 closes it, **exact-first**: fuzzy suggests candidates there, it never
  resolves, because the input is an id the model synthesized rather than the user's
  words. See §"The `ActionTool` selector asymmetry" and
  [`core-deltas.md`](core-deltas.md) CD1.
- **Reuse `async_match_targets`** for all *structured* filtering; its only gap is
  the exact name match. rapidfuzz is a **new** HA dep (difflib fallback).

---

## The bug, precisely

Two chokepoints match names **exactly**:

1. **Intent targeting** — `HassTurnOn(name="reading light")` runs
   `async_match_targets`; `_filter_by_name` keeps a candidate only if
   `_normalize_name(candidate_name) == name_norm` over name/aliases
   (`intent.py:419/436`); `_normalize_name` = `strip().casefold()` (`:413`). One
   wrong word fails the command.
2. **`GetLiveContextTool`** — its `name` filter *also* funnels through
   `async_match_targets` (`homeassistant/llm.py:271`). `allow_duplicate_names=True`
   returns both "AC"s, but the match underneath is still **exact** — so the model
   can't even *look up* an approximate name today.

The one forgiving input is a literal `entity_id` (`intent.py:428`) — the seam.

Why this is LLM-specific (§2.4): hassil's "fuzzy" is an n-gram score over the
*carrier sentence* with entity names matched exactly via a trie, and hassil isn't
in the LLM tool path anyway. The local agent's leniency does not cover us.

And a third chokepoint that matches no name at all, below.

---

## The `ActionTool` selector asymmetry

The two chokepoints above are *name* matching that is too strict. Exposed **scripts**
fail earlier than that: they never get a name to match.

`ActionTool.async_call` converts registry references from names to ids before the
service call, for exactly two selector types (`helpers/llm.py:1011-1035`, HA 2026.7.4):
`AreaSelector` through `intent.find_areas`, `FloorSelector` through `find_floors`. So a
script field declared as an area takes the *name* "Kitchen" from the model and receives
`area_id` at execution. Entity fields get none of that. `EntitySelector` serializes to
`{"type": "string", "format": "entity_id"}` (`helpers/llm.py:816`) and the model's string
goes to `hass.services.async_call` unchanged, while the prompt contains no entity ids
(§2.5). Areas and floors are name-in and id-resolved; entities are id-in and unresolved.

Two consequences that matter for this doc:

- **Consumer 1 does not cover it.** The match-layer fallback hangs off
  `intent.MatchFailedError`, which only intent tools raise. A script handed a
  fabricated id never reaches `async_match_targets`, so there is no miss to catch and
  the service call quietly targets nothing. This is a real hole in Magic Mic today, not
  only in core.
- **An unguessable `entity_id` was never the underlying problem.** `_filter_by_name`
  compares against the friendly name, never the id, so `light.office_lamp_a1b2c3`
  already matches "Office Lamp". What breaks is a tool whose *parameter* is an id.

**Consumer 3** (below) is the response: resolve `EntitySelector` arguments by name at the
proxy before execution. Upstream, the fix is to give entity fields the same conversion
area fields already get, which is a smaller change than any of the alternatives (prompt-
side ids, a mandatory lookup, or a resolution action every script author must call). The
citations, the `IndexError` in core's own area conversion not to copy, and the contract
tests that catch a core fix live in [`core-deltas.md`](core-deltas.md) CD1 and CD3.

---

## Where fuzzy belongs (the reframe)

The instinct is to add a `find_entities` tool the model calls to resolve a name →
`entity_id` before acting. That works, but it **taxes every interaction** — even
when the user was exactly right — because it inserts a mandatory extra step. The
better fix drops fuzzy *into the resolution layer* as a **fallback after exact
match fails**. To see why, you have to count model generations.

### How the tool-use loop actually works

A `tool_use` block is **never terminal**. When the model emits one, the generation
ends with `stop_reason: "tool_use"` — it has yielded to get the result, and *at
that instant it doesn't know the outcome* (matched? service failed?), so it cannot
also speak a confirmation. HA enforces this: `_async_handle_chat_log` loops
`for _iteration in range(MAX_TOOL_ITERATIONS)` (`anthropic/entity.py:1201`) and
breaks only when `not chat_log.unresponded_tool_results` (`:1250`);
`unresponded_tool_results` is true whenever the last block is a `tool_result`
(`chat_log.py:376`). So:

- **Pure chat, no tool** → **1 generation** (`stop_reason: end_turn`).
- **Any tool-using command** → **≥2 generations**: gen1 emits `tool_use`; the loop
  runs the tool and appends `tool_result`; gen2 sees it and speaks. The spoken
  confirmation is *always* a separate generation from the tool call. (The model
  may stream preamble text — "Sure, one sec" — before the `tool_use` in gen1, but
  the real confirmation is gen2.)

### Why in-match wins — count the generations

Because gen2 happens *regardless*, where fuzzy lives decides the total cost:

| Approach | Happy / decisive case | Ambiguous case |
|---|---|---|
| **Exact match (today)** | gen1 `tool_use` → gen2 speak = **2** | fails; gen2 blindly re-guesses |
| **Fuzzy in match layer** | gen1 `tool_use` *(fuzzy resolves inside the tool call)* → gen2 speak = **2** | gen1 → candidates returned → gen2 asks/picks |
| **Mandatory `find_entities` first** | gen1 `find_entities` → gen2 `tool_use` → gen3 speak = **3, always** | 3+ |

Row 2, happy case is the point: **the fuzzy match hides inside the one `tool_use`
that was already going to happen**, so a fuzzy hit costs the *same 2 generations
as an exact hit*. The mandatory-tool row pays a third generation on **every**
command, exact or not. That third generation is pure latency tax (TTFT prefill +
generation, per [`voice-streaming.md`](voice-streaming.md)) on the *common* case
to serve the *uncommon* one.

### Rejected lever: the "terminal fire-and-forget command"

One could imagine the model emitting a *terminal* intent — fire-and-forget, no
gen2 — and HA speaking the intent's own canned `speech` (`"Turned on the light"`).
**This is exactly how the local/hassil path already works** (recognize → fire →
speak canned response, zero model loop; that's why it's fast). The LLM path
*could* do it too — `IntentResponse` carries `speech` — but deliberately doesn't:
it loops so gen2 can give a **contextual** confirmation ("dimmed the kitchen light
to 30%"), **report failures** it couldn't know at emit time, and **chain multiple
intents** from one utterance. So: a real latency lever, considered and rejected
for those reasons. Noted here so we don't re-derive it — the in-match fuzzy
fallback captures the latency win (2 generations) *without* giving up gen2's
confirmation.

---

## The round-trip (disambiguation works out of the box)

The ambiguous case needs the model to come back and choose. That round-trip is not
new infrastructure — it's HA's continued-conversation + chat-session machinery:

- **A match failure is a `tool_result`, not a crash.** `HassTurnOn` raises
  `MatchFailedError` (`intent.py:1059`), which bubbles through `async_handle`
  (`:144`); `ChatLog` catches it and feeds `{"error": ..., "error_text": ...}`
  back as the `tool_result` (`chat_log.py:462-465`). The model's *next* generation
  (gen2, which happens anyway) sees it. Today that payload is bare; our
  improvement is to make it **rich** — the scored candidate list with each
  `entity_id` + area.
- **History persists across turns — the "LLM session."** `helpers/chat_session.py:28`
  — `CONVERSATION_TIMEOUT = timedelta(minutes=5)`, a session keyed by
  `conversation_id`. The `ChatLog` is stored in `all_chat_logs[conversation_id]`
  and **reused** next turn (`chat_log.py:105`). So a follow-up replays the whole
  thread: original request → `tool_use` → `tool_result` (our candidates) →
  assistant's "which one?" → user's answer → correct intent. General HA helper,
  not LLM-specific.
- **Mic reopens without wake word.** `ChatLog.continue_conversation`
  (`chat_log.py:356`) is true iff the last assistant message ends with `?` (or
  Greek `;` / Chinese `？`). The pipeline reads it (`pipeline.py:1346`), stashes
  `continue_conversation_agent`, and routes the next utterance straight to that
  agent (`pipeline.py:1045`).

**So the disambiguation loop is free**, provided the "which one?" reply ends in
"?". That trailing-"?" gate is the crude heuristic [`conversation-loop.md`](conversation-loop.md)
flags: a disambiguation happens to fit it, but "Couch lamp, or reading lamp."
(no "?") would fail to reopen. The `conversation-loop.md` upgrade (default-continue
+ deterministic-stop + spurious-gate) makes "reopen for a disambiguation prompt"
intentional rather than punctuation-dependent. This reframe **relies on** that
upgrade rather than duplicating it.

**As built: a scripted driver proves the round-trip.** `evals/harness/trajectory.py`
drives a multi-turn conversation through the real testbed agent with the provider's
per-generation output scripted (no live model), threading the `conversation_id` across
turns. The driven test (`tests/components/magic_mic/test_trajectory.py`) exercises the
whole loop: turn 1 calls `find_entities`, gets an ambiguous shortlist, asks, and actuates
nothing; turn 2 in the same conversation resolves and turns on exactly the chosen light;
and the acting generation's request replays turn 1's exchange. A corpus of these
trajectories (`evals/corpus/wave1_disambiguation.yaml`) runs through the driver and scores
recovery, misfire (the unsafe wrong-entity case), and turns to completion: two-way and
three-way ambiguities recover, and a direct command and a search-with-alternatives both
complete on the first turn. This is the scripted trajectory the build sequence requires
before any Δturns or disambiguation-recovery claim ([`build-sequence.md`](build-sequence.md),
[`evaluation.md`](evaluation.md)). It proves the machinery deterministically; the emergent
Δturns number (a *live* model choosing when to ask over these same worlds) is a separate,
model-dependent run.

**The live run exists, and disambiguation recovers at +1 turn.** `evals/harness/trajectory_live.py`
drives the same corpus worlds and utterances through the real testbed proxy with a key,
letting the model generate its own tool calls (no scripting). It stands each case up on its
own HA instance so a prior world's entities cannot leak into the next case's `find_entities`
search, and it stops the moment the world reaches the case's goal state, so the turn it
completes on is the emergent turn-to-complete. Scoring is world-based, and getting that
right mattered: the first attempt keyed completion off the action tool appearing in the
trace and scored the two ambiguous cases as MISFIRED, because haiku opens with a failed
`HassTurnOn(name="lamp")` (two lamps, nothing resolves) and then asks. That failed call
changes nothing, so counting it as "acted" both mis-scored the turn and cut off the
follow-up that resolves it. Keying completion off the world state instead lets the recovery
play out. The first pass exposed a second problem in the corpus itself, invisible to the
scripted harness: a case named one light exactly "Bedroom Light", so "turn on the bedroom
light" was an exact match rather than an ambiguity, and an oblique "I want to read" case had
no follow-up turn and too strict an end state. With those fixed and the corpus broadened to
seven cases across lights, fans, and covers and both a name-token and a same-name-in-two-rooms
ambiguity, the run (2026-08, `claude-haiku-4-5`, artifact
`evals/results/wave1_disambiguation_live.json`) recovered all five ambiguous cases and
completed both direct commands: an ambiguous request ends turn 1 as a question with the world
untouched, and the follow-up ("the bedside one", "the tower one", "the office one") actuates
exactly the right entity on turn 2, while a direct command completes on turn 1. So haiku asks
before acting on a real ambiguity, and disambiguation costs one extra turn (mean 1.71 across
the seven cases: five at 2, two at 1). World-based scoring earned its keep here: the blinds
case opened via `HassSetPosition` rather than the `HassTurnOn` the corpus scripted, and it
scored as a clean recovery anyway because completion is judged on the world reaching its goal,
not on which tool fired. The run also confirmed HA drops the deprecated `HassOpenCover` /
`HassCloseCover` from the LLM roster (they are in `AssistAPI.IGNORE_INTENTS`), so covers open
and close through the on/off handler.

---

## Design

> **What the Consumer numbers mean.** The numbered consumers below are the **resolution**
> sites: a referent string arrives, and something is about to act on the target it names.
> They share the scorer *and* the ambiguity policy. The scorer's other consumers do a
> different job and are **named, not numbered**, in their own docs: proactive name
> injection at prompt-build ([`prompt-context.md`](prompt-context.md) Tier 2) assembles a
> prompt, and capability selection's miss recovery (§"The shared referent core") decides
> tool exposure. Both want recall where resolution wants caution, which is why they are a
> separate list rather than Consumers 4 and 5.

### Consumer 1 — fuzzy fallback in the match layer (device control)

The fix for the exact-match bug. Flow:

1. Exact match runs as today (`_filter_by_name`).
2. **Only on `MatchFailedReason.NAME`**, run fuzzy over the already-filtered,
   exposed candidate set (domain/area/exposure filters have already narrowed it).
   Score each candidate's names/aliases (`async_get_entity_aliases`,
   `intent.py:1478`) with rapidfuzz `token_set_ratio`; take the best per candidate.
3. Apply the ambiguity guard (below):
   - **Decisive** single winner → resolve to its `entity_id` and proceed. gen2
     speaks a normal confirmation; the user can correct. (Matches Alexa/Google;
     the fuzz is invisible to the model.)
   - **Ambiguous / none confident** → return the scored candidate list as the
     `tool_result` so gen2 can ask.

Two policy notes:
- **Actions warrant caution.** Auto-resolving a *physical* action on a fuzzy guess
  is riskier than a read. Thresholds must be conservative for action intents —
  when close, return candidates rather than act. (This is the music inversion,
  below.)
- **Shared with hassil.** `async_match_targets` also serves the hassil path (which
  rarely misses — it emits trie-exact names). Gate the fallback behind an opt-in
  constraint (e.g. `fuzzy=True` set by the LLM adapter) so we don't silently
  change global matching behavior. Keeps the core change conservative/reviewable.
- **Happy path untouched** — fuzzy only runs *after* an exact NAME miss.

**As built** (`capabilities/match_fallback.py` `resolve_name_miss`, wired at the testbed
proxy's tool executor in `testbed/api.py`). This is the component-side evidence for the
core change: rather than touch core `async_match_targets`, the proxy catches the
`MatchFailedError(NAME)` an intent raises and resolves before it reaches the model. It
re-runs the structured filters without the name to recover the exposed candidate set, scores
the failed name through the shared `resolve_candidates`, and either retries the same intent
with the winner's canonical `entity_id` (the `_filter_by_name` entity_id seam, so it rides
the one `tool_use` with no extra generation) or hands back a localized candidate list with no
action taken. A non-NAME failure, a missing name, or no configured assistant re-raises the
original error unchanged. The opt-in is the request-language `strings` the entity threads
through `TestbedAPI.wrap` (absent = the fallback is off and execution is exactly HA's), which
also keeps the model-facing ambiguous/not-found text localized (en + es).

Two deltas from the design above, both deliberate and tracked. The **`fuzzy=True`
constraint** on `async_match_targets` is not the plumbing used: the proxy interposes at tool
execution instead, so the strict hassil path is untouched without a new core flag, and the
`fuzzy=True` seam remains the shape to propose when the change moves to core. The
**action-caution thresholds** are not yet tightened: a decisive fuzzy hit auto-acts on the
shared `ACCEPT`/`MARGIN` and the user corrects (the Alexa/Google parity the design cites),
with action-specific tightening left to the open threshold question. Scope: the fallback
fires on a NAME *miss* only; a `DUPLICATE_NAME` (two entities sharing one exact name) is a
different reason and is left to HA's own handling.

**Room is a preference, not a filter.** When no area is spoken, the fuzzy re-match runs
house-wide, mirroring HA core's `_filter_by_name` (which matches the name across all exposed
entities *before* any area logic): a uniquely named device resolves from any room, so "turn on
the floor lamp" from the hallway finds the den's floor lamp. The requesting room comes from
context (the satellite `device_id` on the `LLMContext`, the way core injects a soft
`preferred_area_id`) and is applied only to break a genuine tie: when the house-wide match is
ambiguous, `_prefer_area` keeps the in-room candidates and re-applies the same accept/margin
guard, so context can settle a tie decisively but never force a fuzzy physical action. A
spoken `area`/`floor` is honored as a hard scope, exactly as core would, so "the reading light
in the kitchen" does not cross rooms.

This room-preference is not a property of the match-layer fallback; it lives in the shared
resolve step (`entity_candidates.resolve_name_over_states`) that both consumers call, so
`find_entities` breaks the same tie the same way. "The reading light" resolves to the same
device whether it arrives as a device-control name-miss (Consumer 1) or a `find_entities` call
(Consumer 2), and neither ever filters out a uniquely named cross-room device. `find_entities`
supplies the preference only when its `area`/`floor` arg is absent; a spoken area is already the
hard filter, and the no-name structured list ("what lights are on?") has no name to break, so it
is never silently narrowed to the requesting room.

This trusts the prompt over the model: the system prompt tells the model to pass `area`/`floor`
only when the user names a location, not to echo its own room onto a named request (the room is
supplied deterministically from context, as core does by stripping `preferred_area_id` from the
model and injecting the device's area). If the model ignores that and echoes its room, the
echoed area scopes the search and a device elsewhere misses, exactly as core would miss on a
supplied-but-wrong area. That is left as a prompt-adherence signal for the evals to catch rather
than something the match layer papers over with an echo-detection heuristic, which was tried and
removed: it cannot tell an echo from a user who genuinely names their own room, and guessing
there mis-scopes a real request.

**As built (live).** `evals/harness/fuzzy_fallback.py` drives the fuzzy corpus
(`evals/corpus/wave1_fuzzy_fallback.yaml`, device names deliberately more formal than the
spoken phrasing) through the live testbed agent with the entity summary on and name injection
off, so the model never sees the exact roster and must resolve a spoken name. Each case sets
the room the request comes from, so the corpus pairs same-room and different-room variants of a
request and puts two similarly named devices ("Sofa Reading Light", "Bedside Reading Light") in
different rooms. On claude-haiku-4-5: 7/7 correct, no wrong-target actuation, and **zero turns
echoed the requesting room** as an area, so the prompt guidance held. What each path did:

- "turn on the floor lamp" resolved the den lamp house-wide from a device-less hallway (the
  original cross-room bug) and, unchanged, from the den itself.
- "turn on the reading light" from the living room resolved the living-room one by the room
  tiebreak; the same words from the hallway, with no room to break the tie, asked.
- "the reading light in the bedroom" honored the spoken room and turned on the bedroom one.
- the near-threshold "under cabinet lights" went through `find_entities` (Consumer 2); "close
  the office shade" resolved by area + device-class slots, so the fuzzy layer never ran.

The run reports the resolution path per case and flags any turn that passed its own room as an
area; here that count was zero, and the two turns that did pass an area (a spoken "bedroom",
the "office" shade) were correctly not counted as echoes
(`evals/results/wave1_fuzzy_fallback.json`). The room-preference, spoken-scope, and echo-flag
branches are pinned by keyless unit tests
(`tests/components/magic_mic/test_match_fallback.py`, `test_fuzzy_fallback.py`).

### Consumer 2 — `find_entities` tool (decoupled resolution)

Justified where resolution is **separated from an intent that's firing now**, so
there's no in-match fallback to ride:

- Ephemeral automations / reminders authoring `{trigger, condition, action}` that
  reference `entity_id`s to fire **later** ([`ephemeral-automations.md`](ephemeral-automations.md),
  [`scheduling-model.md`](scheduling-model.md)).
- Conditions, targeting non-intent capabilities, and browsing ("what lights are in
  the garage?").

Signature:

```
find_entities(
    name:         str | list | None  # fuzzy — the scored field (one string, or alternatives to OR)
    area:         str | None      # structured (HA resolves, alias-aware)
    floor:        str | None      # structured
    domain:       str | list | None
    device_class: str | list | None
    state:        str | None
    limit:        int = 5
) -> { success, results: [ {entity_id, name, area, floor?, domain, state?, score} ], ambiguous?: bool }
```

Implementation reuses HA: call `async_match_targets` with everything *except*
`name` (structured filters + `assistant=` exposure, `allow_duplicate_names=True`)
to get the valid candidate set, then hand the matched states to the same shared
`resolve_name_over_states` Consumer 1 uses, so the scorer, the accept/margin guard, and the
requesting-room tiebreak are one implementation, not two that can drift. If `name` is absent
it's a pure structured list ("the kitchen lights") — returning `entity_id`s, which is what
`GetLiveContext` can't do today, and with no name to score the room preference does not apply.

### Name alternatives (OR without a query language)

`name` takes one string or a list of alternatives, and the list is an OR: each alternative
is scored independently and a candidate keeps its best, so broadening a search with synonyms
cannot dilute a strong single-term hit. This exists because the opposite, a model pooling
synonyms into one string, measurably backfires. `token_set_ratio` charges the unmatched words
against the one that hit, so "focus concentration" against a `Focus Mode` script scores 51
(under the floor, nothing resolves) where "focus" alone scores 100. LLMs reach for boolean
`OR` naturally, but a bag-of-tokens scorer treats extra tokens as dilution, closer to AND, so
the naive attempt fails silently.

Structured alternatives fix it without teaching the scorer a query language: the model emits a
list, nothing parses an operator, and it stays language-neutral (no localized `OR` to strip).
The scorer is reused unchanged, called once per alternative with the best kept per candidate,
and the IDF tie-break likewise takes each candidate's best alternative. Alternatives are also
what makes miss recovery work: the model rewrites "help me concentrate" into
`["focus", "concentration", "deep work"]`, and a better query against the same index is the
whole mechanism ([`capability-selection.md`](capability-selection.md) "Miss recovery").
Whether acting on the result then confirms is the confidence gate in
[`tool-policy.md`](tool-policy.md).

The tool is constructed per request from Magic Mic's `conversation` translation category.
Its description and every parameter description therefore use the request language with
HA's normal English fallback. Failures return a stable machine code (`invalid_area`,
`invalid_floor`, or `assistant_not_configured`) plus a localized `error_text`; capability
code never builds model-facing English errors.

### Consumer 3 — entity arguments on script tools

The response to the selector asymmetry above, and the only consumer that runs **before**
execution rather than after a failure or ahead of the turn. An exposed script whose field
is an `EntitySelector` receives whatever string the model produced; nothing validates it,
and an unknown id makes the service call a no-op.

Shape: at the proxy's tool-execution seam, walk the tool's parameter schema for
`EntitySelector` fields and resolve each value before handing the call to the inner API
instance.

**This consumer is exact-first, and fuzzy is the last rung, not the mechanism.** That is a
deliberate split from Consumer 1, for a reason worth stating plainly, because the two look
like the same problem:

> Consumer 1's input is **the user's own words**, sitting in a slot the model filled from
> the utterance, reached only after HA's exact match already failed. Consumer 3's input is
> **an identifier the model synthesized**. Nobody said "light.office_lamp"; the model built
> it from a name it saw. Fuzzy-matching that string resolves a *guess* to a real device, so
> the failure mode is not "wrong device, user corrects" but "the model invented a plausible
> id and we actuated whatever was nearest it." Scripts also skew more behavioral and less
> reversible than a `HassTurnOn`, so the [`undo.md`](undo.md) safety net that makes
> optimism affordable elsewhere is thinner here.

So the ladder, first rung that hits wins:

1. **Already a live entity_id → pass through untouched.** `hass.states.get(value) is not
   None`. No matching runs. This is also the backward-compatibility guarantee: every call
   that works today is byte-identical afterwards, because we only ever rewrite a value that
   would otherwise have targeted nothing.
2. **Exact name or alias match**, exposure-filtered, the same comparison
   `_filter_by_name` makes. Catches the model that passed "Office Lamp" instead of an id.
3. **De-slug, then exact.** `light.office_lamp` → domain `light`, tokens "office lamp",
   matched exactly against names/aliases within that domain. **This is the rung that
   actually fixes the reported bug**, and it is easy to miss: the string in the field is
   shaped like an id, not a name, so rungs 2 and 4 both under-perform on it. The model
   slugified a friendly name; we un-slugify it.
4. **Nothing matched: fuzzy suggests, it never resolves.** The scorer runs over the same
   scoped set and its ranked candidates go back as the `tool_result` for the model to
   choose between. The id-shaped value is scored alongside its de-slugged form (the OR-list
   the scorer already supports) so "light.office_lamp" is not judged as literal text.

> **This replaced a raised-threshold fuzzy rung during the build, and it is the better
> shape.** The original plan was fuzzy-that-acts, tuned conservatively and default-off. But
> a threshold high enough to be safe against a synthesized id resolves almost nothing, and
> the version that is default-off is dead code. Suggest-only gets the same recall with *no*
> false-resolve risk at any threshold, and the extra generation it costs is the one the
> model was going to spend asking anyway. The resolution path is now exact end to end: the
> scorer cannot pick a target here, only offer one.

So **ambiguity never acts**, and there is no threshold to get wrong. A script is a
behavioral write in the [`tool-policy.md`](tool-policy.md) sense far more often than a
`HassTurnOn` is.

**As built:** `capabilities/action_targets.py::resolve_entity_arguments`, called from the
proxy's tool-execution seam (`testbed/api.py`) *before* argument validation and policy
evaluation. Both orderings are deliberate. Magic Mic normalizes arguments through
`tool.parameters(...)`, and `EntitySelector.__call__` runs `cv.entity_id_or_uuid`, so
validating first would reject a friendly name before resolution ever saw it (core does not
validate at all, CD4, which is a difference worth knowing rather than relying on). And tool
policy has to judge the entity actually being acted on, not the model's guess at its id.

Rungs 2 and 3 call `intent.async_match_targets` with a `name` constraint, so exposure
filtering, alias handling, and duplicate-name disambiguation toward the requesting room are
core's semantics rather than a second implementation. A duplicate name core cannot settle is
not a match; it falls through to the candidate list and the model asks.

Three details that decide whether the slice is correct rather than merely plausible:

- **Honor the selector's own config as structured filters.** `EntitySelectorConfig` carries
  `domain`, `device_class`, `include_entities`, and `exclude_entities`
  (`helpers/selector.py:176`, `:992`). The author already narrowed the field; a resolver
  that ignores that is throwing away free precision and can resolve outside what the script
  will accept.
- **`multiple: true` makes the value a list.** Resolve each member independently. Decide
  explicitly what a partial result means: one ambiguous member should not silently execute
  the other three.
- **Exposure still applies.** Candidates come from the assistant-exposed set, so this can
  never become a path to a hidden entity.

**Why the interception can run at all:** nothing on the LLM path validates tool arguments
against the selector schema. `EntitySelector.__call__` would reject "Office Lamp" through
`cv.entity_id_or_uuid`, but it is never called: `APIInstance.async_call_tool` dispatches
straight to `tool.async_call` (`helpers/llm.py:242-260`) despite its docstring saying it
validates. Consumer 3 depends on that gap, which is why it is pinned as
[`core-deltas.md`](core-deltas.md) CD4.

Do not mirror core's area conversion literally: `list(intent.find_areas(...))[0]` raises
`IndexError` on a miss ([`core-deltas.md`](core-deltas.md) CD3). A resolution failure is a
`tool_result` the model can act on, not an exception.

#### Scope: `EntitySelector` now, `TargetSelector` as its own slice

A script field can be declared `selector: target:` instead of `selector: entity:`. That
serializes to `cv.TARGET_FIELDS` (`helpers/llm.py:885` → `helpers/config_validation.py:1310`):
a dict with optional `entity_id`, `device_id`, `area_id`, `floor_id`, and `label_id`, each a
list. `ActionTool.async_call` type-checks for `AreaSelector` and `FloorSelector` validators,
and a `TargetSelector` is neither, so **nothing inside a target dict is resolved** — including
its `area_id` and `floor_id`, which *do* get name-resolved when they are standalone fields.
A target field is therefore a strictly larger hole than CD1, not the same one in a different
wrapper.

We start with scalar `EntitySelector` anyway:

- Different value shape. One string (or a list) versus a nested dict of five list-valued
  keys, each needing its own registry and its own miss policy.
- Different failure semantics. "Ambiguous" is answerable for one field; for a target dict
  where one of five members is ambiguous and the rest resolved, the right behavior is a
  design question, not an implementation detail.
- Different upstream patch. The `area_id`-inside-target gap is its own finding and its own
  core fix, so bundling it muddies the CD1 contribution.

Get one slice right with tests, then take targets with their own ledger entry.

### The shared primitive — scorer + ambiguity guard

One function, two call sites. `score(query, candidates) -> ranked[(entity_id, score)]`
plus the guard:

- **Floor:** drop `s < ~60` (stops "the thermostat" matching a light at 30).
- **Decisive:** `s1 - s2 >= MARGIN` (~15) **and** `s1 >= ACCEPT` (~75) → confident
  single result.
- **Ambiguous:** top cluster within `MARGIN` → return all (≤ `limit`),
  `ambiguous=true`.

Same top-1/top-2 margin logic as hassil's `MIN_DIFF_SCORE` (§2.4) and speaker-ID's
cosine margin ([`speaker-identification.md`](speaker-identification.md)) — a
recurring pattern worth factoring. Thresholds are **starting guesses**; tune on
the eval harness ([`evaluation.md`](evaluation.md)) and expose as `const.py`
constants.

> **Why `token_set_ratio`.** Order/duplicate-insensitive, rewards shared tokens:
> "reading light" ↔ "Reading Lamp" scores high on the shared token; "kitchen
> ceiling" ↔ "Ceiling Light Kitchen" isn't punished for word order. Plain
> Levenshtein over-penalizes reorder/length. Blend with `partial_ratio` if
> substring hits ("lamp" → "Reading Lamp") matter — tune against evals.

**As built** (`fuzzy.resolve_candidates`, the entry point all consumers call): a
two-stage pipeline, tuned against the model-free resolver micro-benchmark
([`evaluation.md`](evaluation.md) Part G, `evals/corpus/resolution/`).

1. **Descriptive-document union.** Score `token_set_ratio` over each candidate's
   documents — **one per name/alias**, each with **area + floor** appended — and keep
   the best. Per-alias (not one joined blob) so a query can't match "reading" from one
   alias and "lamp" from another; **location on each** so a query can span fields
   ("kitchen light" → a "Ceiling Light" in the Kitchen) — this is how **area matching**
   lands without a fuzzy area matcher. A bare area token still can't resolve alone (a
   subset match needs *all* query tokens present), so location strengthens a name match
   without every kitchen entity tying at 100 on "kitchen".

   > **Per-alias is a conservative default, not a settled best practice** (`Candidate`).
   > The counter-case: complementary aliases ("Reading Light" + "Nook Lamp") where
   > "reading lamp" arguably *should* hit the entity — per-alias gives that up so it
   > can't manufacture a spurious cross-alias match (the kind that can false-resolve
   > rather than ask). The seed has neither case; revisit if complementary aliases show
   > up. Flip by joining the names into one document.
2. **IDF tie-break, regime-gated.** Only when stage 1 leaves an above-floor cluster
   ambiguous *and* the candidate set is large enough to estimate term rarity
   (`FUZZY_IDF_MIN_CANDIDATES`), re-rank that cluster by IDF-weighted coverage:
   down-weight tokens common across the set (a shared "light", or the area token
   inside an `area=`-filtered set — TF-IDF's sweet spot) so the discriminating token
   decides. **Union is always the floor**: IDF can break a tie but never demote a
   union result, so tiny homes (where df can't estimate rarity) and recall are never
   sacrificed. Pure IDF and a `max(union, idf)` blend were both measured and rejected
   (pure IDF regressed small homes and recall; `max` can't suppress a distractor).

Out of scope for weighting and tracked separately: **synonyms** ("light" ↔ "lamp",
the one benchmark case IDF can't close), phonetic matching, and `preferred_area_id`
bias.

---

## The shared referent core (the exposure-layer consumer, and the boundary it draws)

Consumers 1 and 2 already share one resolve step. A third arrived from the other direction:
capability selection's miss recovery needs to answer "what in this home could the user have
meant", and that is the same question with a different threshold
([`capability-selection.md`](capability-selection.md) "Miss recovery").

The thing being searched is a **referent**: anything the household names and can act on.
Entities, scripts, and scenes are all referents. The split that matters is not entity versus
script, which is a Home Assistant implementation detail the speaker never sees, but referent
versus abstract capability. A light named "Party" and a script named "Party Mode" are the
same kind of thing to a person; a countdown timer is not, because no timer exists to be named
until one is made.

Today the same script is ranked by two scorers with different inputs:

| | Signals | Scorer | Decides |
|---|---|---|---|
| `capability_selection.action_descriptor` | name, aliases, **description**, area | IDF-weighted lexical over the pooled document | pre-turn tool exposure |
| `entity_candidates.resolve_name_over_states` | name, aliases, area/floor context | `token_set_ratio` plus the ambiguity guard | in-turn resolution |

They disagree on real cases. Capability selection drops `focus_mode` for "help me
concentrate" at budget 8 (`wave1_scripts_selection_shadow.json`), while `find_entities`
resolves the name list `["concentration", "focus", "zone"]` to `script.focus_mode`
(`test_find_entities.py`). The subsystem holding *more* signal, the one that reads the
description, is the one that loses it. Two indexes over the same objects will keep drifting,
so the core is shared and the layers differ:

- **Core.** One ranked lookup over referents, one signal set (name, aliases, description,
  area), one score scale. Entity descriptions feed it too, which the resolver does not read
  today and should.
- **Resolution layer** (`find_entities`, the match fallback). Hard filters, the caution
  regime that holds false-resolves at zero on the decoy and near-miss benchmark, returns a
  resolved target or a small candidate set. Optimized for "pick one and act".
- **Exposure layer** (selection, miss recovery). Same index, recall-oriented threshold, no
  relevance floor at the last-chance end, returns compact headers with enough metadata to
  choose. Optimized for "do not lose the capability".

Two thresholds over one index is the point, and it has to stay explicit. Importing the
resolution layer's caution into an exposure decision would suppress exactly the marginal
candidate exposure is meant to keep.

The consumers keep separate cost models even with a shared core. An entity name costs a few
prompt tokens; a tool costs a full schema and a slot against the provider's 128-tool ceiling.
Same ranking, different budgets.

---

## Reused HA machinery (not rebuilt)

`async_match_targets` (`intent.py:510`) already does domain / state / area / floor
/ **exposure** (`async_should_expose`) / device-class / feature filtering and
**duplicate-name dedup** via `MatchTargetsPreferences(area_id, floor_id)` (`:665`).
`async_get_exposed_entities` (`homeassistant/llm.py:68`) already assembles the
per-entity `names` / `domain` / `areas` (+ optional state/attrs) we search and
return. **The only missing capability is fuzzy name scoring** — everything else is
a function call.

---

## Why not "optimistic best-match" like music

[`music-playback.md`](music-playback.md) argues **optimistic play, not
clarify-first** — a huge catalog would clarify on nearly every request. Entity
resolution **inverts** this: the set is small/bounded/known, and a wrong device is
a *visible physical action* (wrong light, wrong lock), sometimes unsafe to undo.
So here the cost-benefit favors the **ambiguity guard / clarify-when-close** stance
— *especially* for action intents (Consumer 1). Same primitive (fuzzy + top-1/top-2
margin), opposite policy knob. Stated explicitly so the two docs don't look
contradictory.

The **`decoy` and `near-miss` regimes** in the resolver benchmark
(`evals/corpus/resolution/`) are the guardrail on this stance. A decoy home puts a
confusingly similar sibling beside each target (two door locks, a Garage Light next to a
Garage Door); a near-miss home names a device that does not exist, so the nearest candidate
is a decoy to refuse. Pushing the scorer more decisive to win shared-word cases (a lower
`ACCEPT` or margin) is exactly what would turn these ask/none outcomes into a confident
wrong action, so the CI gate holds them at zero false-resolves regardless of tuning. The
three "* light" near-miss queries straddle the thresholds by construction, landing above the
margin, over the floor but under `ACCEPT`, and under the floor, so the corpus also records
where each guard boundary currently sits.

---

## `GetLiveContextTool` — fold in vs alongside

`GetLiveContext` reads **state** for reasoning; its name filter is **exact** and it
returns no `entity_id`. Two paths:
1. **Alongside** — add `find_entities` (returns `entity_id`), leave `GetLiveContext`
   for state-reading. Simplest; slight surface overlap. Component starts here.
2. **Fold** fuzzy name matching *into* `GetLiveContext` and add `entity_id` to its
   output — fewer tools, reads as *fixing* the exact-match limitation, which is the
   cleaner core-PR framing (§7).

Lean: **alongside in the component** (iterate on scoring/return shape freely),
decide fold-vs-not before the core PR. Note that the *device-control* fix
(Consumer 1) is independent of this — it lives in the match layer, not in either
tool.

---

## Evaluation gate

The deterministic resolver corpus gates ranking, acceptance, ambiguity, and localization
thresholds. The single-turn LLM runner gates direct resolution and wrong-target effects; it
is `evals/harness/fuzzy_fallback.py` over `wave1_fuzzy_fallback.yaml`, which state-scores each
case (so a wrong-target resolution actuates the wrong device and fails) and classifies which
resolution path each turn took (match-layer fallback, `find_entities`, structured slots, or
asked).

**Consumer 3 has its own driver and corpus**, because no other case forces an `entity_id` to
exist: `evals/harness/entity_id_tools.py` over `wave1_entity_id_tools.yaml`, whose targets are
exposed scripts with entity-selector parameters. It runs each case **paired**, resolution off
(stock Home Assistant, where an invented id targets nothing) and on, alternating arm order, and
state-scores both, so the delta is what the feature is worth rather than a claim about it. It
also classifies how the model filled the argument: a live id (it got there itself), an
id-shaped value naming nothing (CD1's bug in the act), or a spoken name (it never tried to
produce an id). The corpus's load-bearing property is that no object id is derivable from its
friendly name, which `test_entity_id_tools.py` asserts, since a guessable fixture would let
both arms pass and measure nothing.

A scripted multi-turn trajectory is required before claiming disambiguation success or
fewer turns: it must carry the same `conversation_id`, answer the candidate question, permit a
correction or unrelated replacement command, and score the final world state. Direct fuzzy
resolution may land before that driver; the clarification claim may not.

---

## Dependency: rapidfuzz

**Not** currently a HA dependency (confirmed: no hit in `requirements_all.txt` or
the tree) — the one friction point for the core PR. Options: **bundle rapidfuzz**
(MIT C-extension, fast, ubiquitous; a wheel to vendor); **stdlib
`difflib.SequenceMatcher`** (zero deps, but no token-set semantics, slower);
**hassil** (ships already, but its fuzzy path is n-gram + exact trie, not a
drop-in for entity-name edit distance). Recommendation: rapidfuzz in the component
now; for core, justify the dep or ship a difflib token-set fallback behind the same
`score()` interface so the dep is optional. Keep the scorer behind one function so
swapping is trivial.

---

## Portability shape (§5.5)

The tool ships as `capabilities/entities.py` exposing `async_get_tools(hass,
llm_context, api_id) -> LLMTools`, depending only on `hass`, `llm.LLMContext` /
`ToolInput`, and HA helpers — never on the conversation shell or the Anthropic
client. The **match-layer fallback** (Consumer 1) is a change to
`intent.async_match_targets` (or a wrapper) behind an opt-in flag — a core-side
change, framed as fixing the exact-match limitation, helps local models most,
first contribution (§7, §8). Both land in **Phase 0** and unblock every feature
that needs to name a device.

---

## Open questions

- **Auto-resolve threshold for actions** — the decisive `ACCEPT`/`MARGIN` values;
  conservative enough that a wrong physical action is rare. Tune on evals. This
  threshold is the deterministic half of the confidence-and-confirmation model in
  [`tool-policy.md`](tool-policy.md) ("Confidence, severity, and the confirmation
  gate"): the score-plus-margin band above which a fuzzy action resolves without
  asking, and below which it returns candidates for the model to adjudicate. A low
  score routes to the model; it does not by itself force a confirmation.
- **`fuzzy=True` plumbing** — how the LLM adapter sets the opt-in constraint
  without touching the hassil path; whether it's a `MatchTargetsConstraints` field
  or a wrapper.
- **Extend fuzzy into the local matcher?** The fallback is currently **LLM-path-only**
  by design (keep the strict hassil path deterministic/conservative for the core PR). But
  with `prefer_local_intents` on (PRODUCT_PLAN §2.9), a wrong/approximate name **strict-
  misses locally and falls through to the LLM** — so pure-local / offline users get *no*
  fuzzy resolution. Whether to also give the **local** matcher a fuzzy tier (so
  pure-local benefits, and more commands stay off the cloud) is a real open question — it
  trades away the determinism that motivated gating. Revisit alongside the offline story
  ([`offline.md`](offline.md) Layer 1b).
- **Fold vs alongside** `GetLiveContext` — decide before the core PR.
- **rapidfuzz vs difflib fallback** for core.
- **Fuzzy area/floor?** Start structured (areas are few, taxonomy-injected);
  add only if evals show misses.
- **Compound queries** ("the lamp in the reading nook") — push structure to the
  model (`name` + `area`) vs. accept a blob and factor it. Prefer the model slots.
- **Localization of the scorer (§5.7)** — the implementation reuses Hassil's Unicode NFC
  normalization and RapidFuzz's Unicode-aware processor. Accented Latin and Cyrillic survive
  both union scoring and the IDF tie-break. HA and Hassil do not expose a general linguistic
  tokenizer for fuzzy search; Hassil handles configured no-whitespace languages in its
  grammar matcher instead. Do not invent segmentation here. Add labeled per-language
  resolution corpora and choose any future language-specific scorer or tokenizer from
  measured failures. The existing English-derived action thresholds are not presumed
  portable.
- **`preferred_area_id` / `preferred_floor_id`** — thread known area from prior
  turns as a ranking bias, not just a hard filter.
