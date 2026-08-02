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
  (rapidfuzz `token_set_ratio` + margin), with **two consumers**: the match-layer
  fallback and the tool. One of them is not a tool.
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

---

## Design

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
    name:         str | None      # fuzzy — the scored field
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
to get the valid candidate set, then apply the **same scorer + guard** as
Consumer 1. If `name` is absent it's a pure structured list ("the kitchen lights")
— returning `entity_id`s, which is what `GetLiveContext` can't do today.

The tool is constructed per request from Magic Mic's `conversation` translation category.
Its description and every parameter description therefore use the request language with
HA's normal English fallback. Failures return a stable machine code (`invalid_area`,
`invalid_floor`, or `assistant_not_configured`) plus a localized `error_text`; capability
code never builds model-facing English errors.

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
  conservative enough that a wrong physical action is rare. Tune on evals.
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
