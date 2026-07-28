# Long-Term Memory

> The Phase-2 differentiator (PRODUCT_PLAN §8) — and the most over-scoped word in
> the plan. This doc's job is to **shrink** "memory" to what real users actually
> ask for and what survives the interaction profile, then route everything else to
> a more structured home. Grounded in shipping community artifacts, not the
> confidant vision. Cross-refs: [`find-entities.md`](find-entities.md),
> [`prompt-context.md`](prompt-context.md) §5.2, [`scheduling-model.md`](scheduling-model.md),
> [`ephemeral-automations.md`](ephemeral-automations.md), [`learning.md`](learning.md).
>
> **Scope note:** the *offer machinery* worked out below (recognize friction → offer a
> durable fix → confirm → persist) turned out **not to be memory-specific** — it's been
> factored into [`learning.md`](learning.md) as a cross-feature primitive (the offer engine +
> a `FrictionResolver` registry) shared by aliases, **command aliases**, annotations, and
> threshold edits. This doc keeps the **notebook** (user-writes / user-recalls) and the
> alias/annotation *specifics*; treat learning.md as the spine for the offer flow itself.
> The alias offer here is one `FrictionResolver`; command aliases are its new sibling.

---

## TL;DR

- **Two products wear the name "memory."** *Memory-as-confidant* (knows you,
  personalizes conversation) scales with **conversation frequency** — which a
  tactical device-control home doesn't have, so it sits inert. *Memory-as-substrate*
  serves the tactical commands themselves and needs no conversation to pay off.
  Scope the substrate; defer the confidant.
- **Value model inverts the chatbot's.** In a chatbot, memory *enables* longer
  sessions. In a tactical assistant, memory's job is to **remove future turns** —
  every stored fact is a clarification skipped, a command shortened, a correction
  not repeated. Latency/turn-count is the enemy (per prompt-context); memory is a
  lever on *that*.
- **The real, demanded feature is the notebook.** Every shipping community artifact
  clusters on user-authored world-facts with explicit write + explicit recall
  (parking spot, wifi password, spare keys, codes, pet's name). Highest demand,
  **lowest** build risk, cleanest core story. (Earlier ranking had this backwards.)
- **Negative definition + route-to-structured rule.** Memory-the-store holds only
  what has **no more structured home**. Appointments → reminders/calendar;
  "call it X" / disambiguation → **aliases**; units/default-room → settings; home
  layout / device capabilities → **deterministic prompt-context injection** (§5.2,
  already scoped). Memory is the *residual*.
- **Two "feels-like-memory" primitives, kept distinct:** (1) **free-floating
  notebook** (explicit recall, FTS) and (2) **entity-attached facts** (aliases +
  annotations), surfaced by **reactive enrichment of entity tool-results** (splice
  metadata onto the entities a tool call already returned), *not* proactive
  injection. Aliases are the strong, real member; annotations a deferred corner case.
- **Aliases mostly escape the retrieval problem; annotations don't.** Aliases have a
  **deterministic consumer** — the intent matcher checks aliases server-side at match
  time (§2.4), so "the tree" resolves with **zero prompt injection**. Annotations
  have **only** an LLM consumer (nothing deterministic can use "100 ppm is normal"),
  so they *must* reach the prompt → the tool-result join is their retrieval path.
  That asymmetry is why aliases are architecturally better than annotations.
- **Offers are governed by the shared engine** ([`learning.md`](learning.md)): inference
  may *offer*, only confirmation *writes*; silent auto-write and cross-conversation
  adaptation stay rejected. This doc keeps only the memory-specific detectors (below); the
  invariant, gating, and economics live there.

---

## What people actually ask for (evidence, not the vision doc)

Four shipping artifacts, and their concrete examples cluster hard:

| Source | What it actually stores |
|---|---|
| [Voice Assistant Memory blueprint](https://community.home-assistant.io/t/voice-assistant-memory/856199) | car parking spot ("basement 3 near the elevator"), wifi password, spare-keys location, phone numbers, birthday — with a **TTL** ("10 years or forever") |
| [ha-ai-memory](https://github.com/Riscue/ha-ai-memory) | "user is allergic to peanuts", "garage door code is 1234"; **private vs shared** scope; semantic recall |
| [Voice Assistant Long-term Memory blueprint](https://community.home-assistant.io/t/voice-assistant-long-term-memory/935090) | parking, wifi, lost items, appointments, cat's name |
| [home-mind](https://github.com/hoornet/home-mind) | sensor baselines ("100 ppm NOx is normal here"), corrections, device nicknames, **Home Layout Index**, **Device Capability Index** |

**Reads from the evidence:**
- The dominant, load-bearing feature is the **notebook** — user-authored world-facts,
  explicit write + explicit recall. Not personalization.
- The **confidant / personalization** framing appears only in *vendor marketing*
  (CarMem, Charlie Mnemonic, Perplexity), **not** in HA community asks.
- **Architecture repo is silent.** The foundational LLM-API discussion
  ([architecture #1068](https://github.com/home-assistant/architecture/discussions/1068))
  has no memory/personalization design — greenfield, and no prior maintainer
  position to align to. Reinforces "memory lands last" (§7).
- **home-mind's most-praised "memory" isn't memory.** Its Home Layout Index and
  Device Capability Index are **deterministic config precomputed and injected** into
  the prompt. The thing that reads as "the AI remembers my home" is really good
  context injection — i.e. the §5.2 taxonomy-skeleton work **already scoped**. A
  chunk of perceived memory value is delivered by prompt-context, not by a store.

---

## The taxonomy (four kinds, by writer × reader)

| Kind | Written by | Read by | Fit / verdict |
|---|---|---|---|
| **Notes (world-facts)** | User, explicitly | User, explicitly | **The feature.** Real, safe, in Phase 2. |
| **Entity-attached facts** | User, explicitly | Silently, at prompt-build | Aliases = real (also fixes §2.4). Annotations = deferred corner case. |
| **Profile preferences** | User / offered | Silently shapes behavior | Mostly **inert without a conversation/automation surface**; several already structured. Deferred. |
| **Learned/inferred defaults** | Inferred from history | Silently shapes behavior | **Don't build.** Collapses into an explicit preference or an alias write. |

---

## Route-to-structured — the rule that keeps memory shippable

Before writing a memory, ask **"does this have a more structured home?"** Memory is
the residual. This is what keeps the store from becoming a worse-than-config
dumping ground (the failure mode that makes it un-landable in core, §7).

| User says | Belongs in | Not memory because |
|---|---|---|
| "remind me of my dentist appointment" | reminders / calendar ([`scheduling-model.md`](scheduling-model.md)) | it has a fire time + delivery |
| "call the office tree 'the tree'" | **alias** on the entity | fixes exact-match at the source (§2.4), editable in UI, deterministic |
| "use Celsius" / default room | settings / exposure | core already models it |
| "which floor is the lamp on" | §5.2 taxonomy-skeleton injection | deterministic, per-request |
| "our cat's name is Fluffy" / "wifi is X" | **memory (notebook)** | genuinely unstructured, no other home |

---

## Aliases feel like memory (log this)

To a user, "remember to call the office tree 'the tree'" **is** a memory — but the
right implementation is an **alias write**, not a memory-store row:

- It makes HA's **exact** name match (§2.4, `_filter_by_name`, `intent.py:413`)
  succeed *at the source* — no fuzzy fallback, no retrieval, no `find_entities`
  round-trip on the happy path.
- It's **deterministic and UI-editable** (aliases are first-class registry data),
  where a memory blob is neither.
- It **dissolves disambiguation-learning**: "you always mean the office tree"
  doesn't need multi-turn pattern detection — the user just *states* it once, and it
  persists as an alias. Inference-as-a-feature evaporates; the value is free once
  aliases are **voice-writable**.

**Implication:** a valuable slice of "memory" is actually **voice-writable
alias/exposure config**. Build that as the entity-attached path; it's arguably a
config feature, not a memory feature, and lands far more easily than a store.

**Write surface (checked `ha-core/`):** no voice/LLM/intent path exists (grep of
`*/intent.py` + `helpers/intent.py` for alias-add = nothing); only a websocket
command (`config/entity_registry`, the UI). But the abstraction is clean and cheap:
`entity_registry.async_update_entity(entity_id, aliases=list[AliasEntry])`
(`entity_registry.py:1959`), **full-list REPLACE** → add = read-modify-write
(`current = entry.aliases or [COMPUTED_NAME]; update(aliases=[*current, new])`;
preserve the `COMPUTED_NAME` sentinel = entity's own name stays matchable,
`:93`/`:587`). Aliases feed **both** resolution paths — exact `_filter_by_name` and
the fuzzy fallback (`async_get_entity_aliases:573`). So an `add_alias` tool is ~5
lines; the cleaner core path is an **alias-add intent** (helps local too, like
`find_entities`). **Not a design-changing unknown — the alias slice is cheap.**

### Near-misses & dedup — the offer-gate already handles it

- **The gate IS the dedup mechanism.** Reaching the "remember that?" offer means fuzzy
  matching *failed* to resolve the utterance (that's what fired the disambiguation
  gate) → by construction the string is **outside** the target's current match basin
  → adding it is **net-new coverage, not a duplicate.** The LLM should **not** scan
  the alias list to prune near-misses.
- **Self-damping, not runaway.** Each alias expands both the exact and fuzzy basins,
  so the *next* near-miss ("desk lite" after "desk light" is stored) now resolves and
  never triggers an offer. The set grows to cover real phrasing variance, then stops.
  A "15 near-dup aliases" runaway only happens if the fuzzy scorer is **mis-tuned** —
  the lever is the **scorer threshold**, not dedup logic.
- **The one real write-time guard = CROSS-ENTITY COLLISION** (string already resolves
  to a *different* entity → ambiguity). The **tool's** job, deterministic (§5.4):
  reuse `find_entities`' scorer + top-1/top-2 guard over the registry — **exact-match
  to another entity → refuse + surface** the conflict; **fuzzy above the ambiguity
  threshold → confirm**; **matches many entities or a bare domain/area token → refuse**
  (would shadow broad/area commands); **never silently add** (behavioral → confirm-
  first). Not the LLM's reasoning.
- **Scope forced HOUSEHOLD-GLOBAL:** registry aliases are per-entity, **not** per-user
  → alias-as-memory is inherently *household* scope (fine — user A's "the tree" + user
  B's "the plant" both resolve). **Personal** aliases would need a separate per-user
  resolution layer, not the registry. Partially answers the scope open-question.
- **No TTL** (unlike notebook notes): registry aliases persist until edited →
  staleness/re-targeting is a lifecycle concern; bias offers to **specific** strings,
  away from generic terms ("the light") that shadow area/broad commands.

---

## Annotations — deferred corner case (worked example)

"100 ppm is normal here." Tempting, but it fails the notebook mold and is a corner
case on three axes:

1. **Not explicit-recall.** Nobody asks "what's the bathroom CO2 baseline?" It must
   reach the LLM **exactly when it reasons about that sensor**. Retrieval mechanism =
   **reactive enrichment of the entity tool-result** (see below), **not** the
   free-floating notebook (which serves explicit query). This part is *solved and
   cheap* — it is no longer the reason to defer.
2. **Write trigger is hard.** Options: (a) agent mid-turn recognizes "this should be
   remembered" and asks — error-prone, a correctness liability; (b) out-of-band
   analysis → auto-memory or a write-queue — infrastructure for a corner case. Both
   are the **silent-inference failure mode** we reject for disambiguation. Only sane
   trigger = **explicit user tag** ("remember that for next time"), same discipline
   as the notebook.
3. **Route-to-structured bites.** If any threshold/automation exists (e.g. a 50 ppm
   alert), "100 is normal" is a **threshold edit** — structured, deterministic. The
   annotation is only the *residual*: a truly-unstructured baseline with no
   automation behind it.

The canonical flow — "which CO2 sensors are abnormal?" → tool calls → list → user:
"the bathroom's fine, 100 is typical there" → "remember that" → written to entity
metadata → injected next time — is **a cool moment but a genuine corner case.**

**Verdict:** out of Phase 2 — but the deferral reason has **narrowed**. Retrieval is
solved and cheap (below); the only remaining gates are the **write-trigger**
(explicit "remember that" is the sole sane option) and **thin demand**. Status:
*mechanism-ready, gated on write-side + demand* — no longer a hard corner case.

---

## Reactive enrichment — the retrieval mechanism for entity-attached facts

Entity-attached facts surface by **enriching entity tool-results**, not by proactive
prompt injection: when the LLM calls `GetLiveContextTool` / `find_entities` and the
layer is about to return state for N entities, it looks up their attached metadata
(annotations, capability facts) and **splices it into the result** before returning.

- **Generation-free.** It rides a tool call that was *already* happening. Annotations
  only matter for read/reasoning queries, which inherently fetch state (≥2-gen flow
  already) — so enrichment adds **zero** tool calls and zero generations.
- **Pay-per-use.** Costs tokens only when an annotation-bearing entity is actually
  fetched — vs proactive §5.2 injection, which pays on every request in the
  uncacheable tail for entities that *might* be relevant. Reactive strictly beats
  proactive here on the end-to-end metric ([`prompt-context.md`](prompt-context.md)).
- **Seam already exists.** Tool results route back through
  `chat_log.llm_api.async_call_tool` (§2.1), which our layer owns — enrichment is a
  wrapper on that return path.
- **Generalizes past memory.** Same shape as home-mind's Device Capability Index
  ("this light supports color temp"). "Reactive enrichment of entity tool-results"
  is a small **reusable primitive** that belongs next to prompt-context, not buried
  in memory. (Aliases-for-*resolution* don't use it — they're consumed server-side by
  the matcher; only the LLM-facing uses of entity-attached facts do.)

---

## Write-trigger — the memory-specific detectors

The write side, not retrieval, is the hard part — and it's an instance of the offer engine.
**The general machinery lives in [`learning.md`](learning.md):** the invariant (inference
*offers*, only confirmation *writes*), the friction/silence economics (offer only when saved
turns > the asked turn; over-offering is the failure), the deterministic **eligibility gate**
(HA's `async_get_tools` §2.5, so the tool is absent on the common path and rides gen-2 of an
already-≥2-gen command), the guardrails (≤1/turn, suppress-after-decline via `HassNevermind`),
and the confirm-policy rule (behavioral fix → confirm-first; inert fix → optimistic). This
section keeps only what's **specific to memory's three fixes** — i.e. each resolver's *signal*:

- **Notebook** is barely "learning" — its writes/recalls are **user-driven**, not
  friction-driven: an **explicit keyword** ("remember", "note that", "where did I put…")
  injects the write/recall tool immediately, even turn 1 (keyword pre-gate, same shape as
  tier-2's domain-keyword booster). A pure note is **inert** → **optimistic-write-and-mention**
  ("noted — say 'forget that' to undo"); that "forget that" is an **undo instance** (delete
  the row / restore the prior slot value → [`undo.md`](undo.md)), not confirm-first. Payoff
  gate: real durable value (wifi password) over trivial one-offs; bias to the **structured**
  write (alias > blob).
- **Alias** — the signal is **HA-owned and crisp**: not "any tool ran" (reads fire
  constantly) but **a disambiguation actually occurred** — exact-match missed → fuzzy fallback
  fired, or candidates were returned and the user picked (we own the match layer,
  [`find-entities.md`](find-entities.md)). Strong deterministic signal, mild semantic urgency
  → high-precision / low-recall, default silence. Confirm-first (behavioral).
- **Annotation** — the signal is **semantic / LLM-owned** (the user *contradicted a judgment
  the model made* — "no, 100 is normal here"); HA can't see it mechanically, so **there is no
  crisp deterministic gate.** Coarse necessary-condition only: `turn>1` ∧ a state-read occurred
  (bias to sensor/numeric domains) ∧ not-declined — never turn-1. The *correction* is
  recognized by the LLM, not HA. Weak deterministic signal but *strong* semantic urgency (an
  explicit correction means the current non-memory produced a **wrong output**) → can afford
  **higher recall**; the correction is its own payoff proof. Second path: the user
  **volunteering** a baseline ("reads high, it's normal") = an explicit entity-attached write.

---

## Multi-user / scope (intersects `resolve_user()`)

ha-ai-memory ships a **private (per-agent) vs common (shared household)** split.
Mapped onto our seam (§5.1): memories are keyed by resolved `user_id`, with an
explicit **shared/household** scope as a first-class option (wifi password, garage
code, cat's name are household facts, not personal ones). "Private" here means
per-**user**, not per-agent. This is the `resolve_user()` + user-keyed `Store`
primitive (§5.6) doing double duty.

**Settled:** LLM **infers** scope from content (pronoun / shared-resource cues),
**default personal** on ambiguity; recall = caller's *personal ∪ household*. See
data-model notes below.

---

## Retrieval

- **Notebook:** keyword / FTS at HA scale (the blueprints literally use SQLite FTS).
  Embeddings only buy fuzzy-phrasing recall ("where's the car" ↔ stored "parking
  spot"); a tier-3 add, off the critical path (§5.2), not a v1 requirement.
- **Entity-attached facts:** no query — injected with the entity via §5.2 tier-2.
- Confidant/biographical: the only kind wanting semantic RAG — and it's the kind
  being deferred. So "memory = store + embeddings" is the **wrong monolith**; the
  valuable memory is small, structured-adjacent, and injected.

---

## Data model notes (to firm up)

- **Slot-keyed, overwrite-by-default** (settled). Notes are `key → value`, not a log:
  "where's my car" is a single slot — new parking **replaces** old. The LLM proposes
  a *subject* key ("car location", "wifi"); the **tool** fuzzy-matches it against the
  user's existing slots (**scorer's Nth consumer**, §5.4) so re-phrasings ("where I
  parked" vs "car location") collapse to one overwrite, not a duplicate. Distinct
  subjects coexist as distinct slots. True append/list notes are **not v1** (and are
  profile-prefs anyway, deferred).
- **Scope: inferred from content, default personal on ambiguity** (settled). Pronoun /
  shared-resource cues decide ("my dentist" → personal; "the wifi" → household);
  default **personal** when unclear (cross-user leak > non-share). Recall = caller's
  *personal ∪ household*, keyed via `resolve_user()` (§5.1); pre-voice-ID collapses to
  the default/household user, but keyed day one. (Aliases stay forced-household.)
- **TTL: LLM sets per slot-kind, default none** (settled). Volatile subjects (parking)
  → short default; durable (wifi/codes/names) → none; default no-expiry when unsure.
  Cleanup = lazy-on-read + optional periodic sweep.
- **Tool shape: one notebook family** `remember` / `recall` / `forget`, scope is a
  *param* not separate tools, injected per the offer-gating rules. Aliases =
  separate `add_alias`/intent against a different store (the registry).
- Fields (draft): `content`, `slot` (subject key, fuzzy-resolved), `scope`
  (personal→`user_id` | household), `ttl?`, `created_at`. Retrieval = FTS over
  `content` + fuzzy over `slot`.

---

## Open questions

Design is closed out; what remains is **implementation-level**, to settle when built:

- Slot-key derivation prompt + the fuzzy overwrite-vs-new threshold (reuses the scorer).
- Collision-check thresholds (share `find_entities`' top-1/top-2 tuning).
- FTS backend choice (SQLite FTS per the blueprints) + when/if embeddings earn their
  keep for fuzzy-phrasing recall (tier-3, off critical path).
- Offer-gating heuristics need real-conversation tuning (per-kind recall/precision).
- Deferred, revisit only with a triggering surface: profile-preference store,
  annotations (entity-attached), confidant/biographical + semantic RAG.
