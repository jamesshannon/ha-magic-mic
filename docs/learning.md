# Learning (the friction-resolution primitive)

> Split out of [`memory.md`](memory.md): the machinery that lets the assistant
> **recognize friction and offer a durable, deterministic fix** is not memory-specific —
> it's a cross-feature primitive that several capabilities plug into. This doc defines that
> primitive (the **offer engine** + a **`FrictionResolver` provider registry**) and the
> family of fixes that ride it — entity aliases, **command aliases** (new), annotations,
> threshold edits, todo-default resolution. Cross-refs: [`memory.md`](memory.md),
> [`find-entities.md`](find-entities.md), [`undo.md`](undo.md),
> [`conversation-loop.md`](conversation-loop.md), [`prompt-context.md`](prompt-context.md),
> [`todo.md`](todo.md), PRODUCT_PLAN §2.5 (dynamic tool lists), §2.9 (`prefer_local`),
> §5.4 (determinism-in-tools).

---

## TL;DR

- **"Learning" ≠ "memory."** They got lumped because both *feel* like the assistant
  remembering. The line is **who triggers the write and who consumes it**: *memory* =
  user-triggered write, user-triggered recall (the notebook — wifi password, spare key);
  *learning* = **friction-triggered offer, machine-triggered consumption** (an alias the
  matcher applies, a command rewrite the pipeline applies — the user never "recalls" it,
  it just works next time). This doc owns the second.
- **One pattern, stated once:** *recognize confusion (often algorithmically) → offer
  something that mitigates it next time → on confirmation, persist it where a deterministic
  consumer will apply it.* Every friction-driven fix is an instance.
- **The shared thing is the offer engine, NOT storage.** Each fix-sink owns its own store,
  chosen by what's structurally natural: entity aliases → the **entity registry** (HA's);
  command aliases → a **human-editable YAML table** (the HA config idiom — the learned path
  and a hand-authored path write the same file); annotations → an **FTS column keyed by
  `entity_id`**. This *is* `memory.md`'s route-to-structured rule: the fix's structured
  home is the point.
- **Mechanism = a `FrictionResolver` provider registry**, surfaced into the prompt via
  HA's existing **dynamic tool-list gate** (`async_get_tools`, §2.5) pointed at a *friction
  predicate* instead of "exposed-entity-exists." The core loop never has to know the set of
  resolvers — new ones register a tool + SKILL text + sink. This is itself a small instance
  of the whole project's north star (a plug-in seam for new assistant behaviors).
- **Designed as a cross-integration seam.** The registry is built so a resolver could live in
  a *separate integration* — any third party can provide a `FrictionResolver`, the way any
  integration can `llm.async_register_api()` today (the native precedent — PRODUCT_PLAN §6.2).
  We build it **module-first, split just-in-time**: the contract is written as if it already
  crosses an integration boundary (registration + discovery, no private imports), so promoting
  it to a standalone provider integration later is moving code, not redesigning.
- **The detector is typed, so the filter is nearly free.** The signal that *detected* the
  friction already names the relevant resolver (match-miss → `add_entity_alias`; repair-turn
  → `add_command_alias`; model-contradiction → `add_annotation`). Loading *all* resolvers is
  just the fallback for genuinely ambiguous friction. v1 needs no separate filter component.
- **Bias hard to silence.** Over-offering is the failure mode (`memory.md`); the SKILL text
  defaults to saying nothing and offers only on genuine, high-value, likely-to-recur
  friction. ≤1 offer/turn, suppress-after-decline.

---

## The offer engine (the reusable spine)

`memory.md` already built this for aliases/annotations; here it is factored out, feature-agnostic:

```
friction detected
  → eligibility gate      (cheap, deterministic; keeps the offer machinery
                           OFF the common-path prompt — §2.5 async_get_tools)
  → proposer              (LLM decides whether it's worth it + phrases the offer)
  → confirmation          (behavioral fix → confirm-first; inert fix → optimistic-and-mention)
  → fix-sink.apply()      (writes to the sink's OWN store)
  → suppression / self-damping   (≤1/turn, suppress-after-decline; each fix widens
                                   the basin so the same friction stops recurring)
```

Two properties carried over from `memory.md` that make it cheap enough to be always-on:

- **It rides an already-expensive request.** Friction only *exists* after a tool has fired,
  so the offer materializes on **gen-2's tool-list reissue** (HA re-issues tools each loop
  generation, §2.1 / [`prompt-context.md`](prompt-context.md)) — not a fresh round-trip. The
  resolvers simply appear in the tool list of a command that was already going to be ≥2
  generations. Marginal cost on the expensive path, absent on the cheap one.
- **HA decides eligibility; the LLM decides content.** §5.4 determinism-in-tools applied to
  prompt assembly. The soft judgment ("is *this* worth offering?") runs only inside a
  window HA has already narrowed → high precision without a spurious-call surface on every
  request.

## The `FrictionResolver` plugin contract

A resolver is the unit third parties / future features register. It declares five things:

| Field | What it is |
|---|---|
| **signal** | the friction it answers to (match-miss, repair-turn, model-contradiction, install-collision) — this is also its routing key |
| **tool + SKILL text** | the fix tool the LLM calls, plus the "when to use me / default to silence" prose injected on eligibility. **This is the *machinery-gated* SKILL** ([`skills.md`](skills.md)) — HA sees the friction and injects the block **whole**, zero extra generation; it is *not* the resident-header `load_skill` registry. The etiquette half (silence-bias, ≤1/turn, confirm-policy, undo-mention) is **one shared block** across resolvers; a resolver adds only a tiny tool-specific note. (Resolves the shared-vs-per-resolver open question below.) |
| **sink + store** | where the fix is written — and it owns that store (registry / YAML / FTS column) |
| **confirm-policy** | *behavioral* fix (changes future resolution → confirm-first) vs *inert* fix (changes nothing → optimistic-and-mention) |
| **inverse** | its undo, per [`undo.md`](undo.md)'s "declare your inverse" — `add_command_alias` is reversible like every other write; learning is not the one un-undoable thing |

The registry composes the "friction turn" from whichever resolvers the fired signal selects.

## The fix family

| Fix | Signal (detector) | Consumer | Store | Confirm |
|---|---|---|---|---|
| **Entity alias** | exact-match miss → fuzzy fallback fired (HA-owned, crisp) | intent matcher, server-side (§2.4) | entity registry | confirm-first |
| **Command alias** (new) | repair-turn / rephrase / undo-after / install-collision (LLM-owned + structural) | pre-agent rewrite (this doc) | **YAML table** | confirm-first |
| **Annotation** | user contradicts a model judgment (LLM-owned, semantic) | LLM, via reactive tool-result enrichment (`memory.md`) | FTS column + `entity_id` | confirm-first |
| **Threshold / setting edit** | correction that has a structured home | the automation/setting itself | HA config | confirm-first |
| **Todo default-list** | list reference doesn't resolve | resolve-then-create (`todo.md`) | todo entity | optimistic |

Detectors split by **who owns the signal**, and the precision/recall stance follows
(straight from `memory.md`): **HA-owned** signals (entity alias) → precise gate, LLM does
little, high-precision/low-recall/default-silence; **LLM-owned** signals (annotation,
command alias) → no crisp deterministic gate, coarse necessary-condition only, but the
*correction is its own proof of payoff* so leaning on the model with higher recall is safe.

---

## Command aliases (the new member)

A command alias rewrites a whole utterance to one the assistant already handles reliably —
`"what should I wear tomorrow?"` → `"what's the forecast tomorrow morning?"`,
`"goodbye"` → `"turn off all the lights"` — applied **before** the model runs.

### Two things it buys (beyond "LLM makes deterministic commands")

1. **Routing stability under ambiguity and growth.** Even a perfect model routes less
   reliably as capabilities accrue — more tools/skills = more "which one did they mean"
   surface (the tool-bloat concern in [`prompt-context.md`](prompt-context.md)). A
   "wardrobe picker" skill that collides with `"what should I wear tomorrow?"` doesn't make
   the model dumber; it makes routing *ambiguous*. An alias pins the phrase to a route
   **regardless of how many competitors exist** — so its value *grows with capability*, the
   opposite of a toy. This also gives a **proactive, non-semantic detector**: two skills
   claiming overlapping phrasings is an **install-time collision**, observable with no
   "confusion" ever occurring.
2. **It can move an utterance off the cloud path entirely.** If the rewrite target is a
   *locally-matchable* command, the utterance short-circuits to hassil and **never touches
   the LLM** — faster, cheaper, private, offline-capable. This is the §2.9 / offline
   dual-payoff made concrete: a command alias is a lever that relocates work from cloud to
   local. (If the target still needs the model — e.g. the forecast tool — you keep the
   *reliability* win but not the local win.)

### What HA has today (checked `ha-core/`)

HA has the *effect* — phrase → action, handled locally, no LLM — via **conversation
sentence triggers**:

```yaml
trigger:
  - platform: conversation      # homeassistant/components/conversation/trigger.py
    command: ["goodbye", "good bye"]
action:
  - service: light.turn_off
    target: { entity_id: all }
```

In `default_agent.py` these are matched **before** built-in intents
(`async_recognize_sentence_trigger` at `_async_handle_message`, ~L443, *then* intents at
~L458) and handled entirely by hassil — the LLM never sees them. `intent_script` is the same
family (phrase → custom intent → action + response). The sentences are real hassil templates
(slots/wildcards), not literals.

**What HA lacks — and it's exactly the value here:**
- **Phrase → phrase rewrite.** HA binds a phrase to an *action/script*, never to *another
  natural-language command* that **re-enters the pipeline**. The indirection — reuse the
  assistant's own understanding of the target phrase instead of hand-wiring a script that
  duplicates `weather.get_forecasts` + TTS — doesn't exist.
- **The offer-to-learn layer.** Nothing proposes the alias when a phrase routes poorly.

### Placement

An HA pipeline picks **one** conversation agent, so there's no clean pre-agent hook. The
natural home is **at the assistant's own entry**: apply the rewrite table first; if the
result matches a local intent, hand it to the local matcher directly (the `prefer_local`
dance we already do); otherwise proceed to the model. Self-contained, no pipeline surgery.

### Detector

Command-alias friction is **LLM-owned / semantic** (annotation-class), because a mis-route
is often *intermittent and silent* — the system may not know it was wrong. Usable signals:
**repair turns** ("no, I meant…", an immediate rephrase, an undo-right-after) plus the
structural **install-collision** signal above. So command aliases = *annotation-class
detection* with *alias-class consumption* (a deterministic pre-pipeline rewrite). The
framework already has a home for that hybrid.

### The manual path is first-class

Because the store is human-editable YAML, power users author aliases by hand without ever
tripping the detector. The learned path and the manual path converge on the same file —
which is exactly the HA-community-friendly "pin it down when the magic is flaky" escape
hatch, not a black box.

---

## Path to core & localization

- The registry is HA's own `async_get_tools` gate with a different predicate → **portable /
  core-shaped**, not shell-special (§5.5).
- Each resolver's **SKILL text is user-facing**, so anything core-bound goes through
  `strings.json` → translations, never baked English (§5.7). Same gate as any contributed
  intent.
- Contribution-wise: entity aliases ride the alias-add intent already argued in `memory.md`;
  command aliases are more novel (core's current answer is "write a conversation trigger"),
  so the likely first contribution is the **offer-to-learn** behavior over HA's *existing*
  fix surfaces, with the pure rewrite table proposed separately.

---

## v1 scope & open questions

- **v1:** the offer engine + registry with **two resolvers** — `add_entity_alias` (already
  cheap per `memory.md`) and `add_command_alias` (YAML store + agent-entry rewrite + local
  short-circuit). Typed-detector routing; no separate filter. Annotations/thresholds stay
  deferred (`memory.md`).
- **Open (implementation-level):**
  - Command-alias detector tuning — which repair signals fire an offer, install-collision
    threshold (reuse the `find_entities` scorer over registered phrasings).
  - Rewrite-table match: exact vs. fuzzy on the incoming utterance (fuzzy risks
    over-capture; likely exact/near-exact only, scorer as guard).
  - SKILL-text calibration for silence-bias (**resolved: shared block, not per-resolver** —
    see the contract table / [`skills.md`](skills.md); remaining work is wording, not shape).
  - Whether the offer engine and its registry live beside `capabilities/` or as their own
    module (they gate *other* capabilities' fixes).
