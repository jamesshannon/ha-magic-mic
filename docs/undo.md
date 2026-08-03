# Undo / Correction

> Cross-cutting **shared primitive** the §5.6 method surfaced late: "undo that," "turn it
> back," "never mind, put it how it was," "forget that." Invoked piecemeal across
> [`memory.md`](memory.md) ("say 'forget that' to undo"), [`music-playback.md`](music-playback.md)
> ("no, the other one / next"), [`prompt-context.md`](prompt-context.md) (optimistic-
> execute + fallback), and [`find-entities.md`](find-entities.md), yet designed nowhere. It's the
> safety net that makes every *optimistic* execution path tolerable, so it deserves to be
> factored once. Grounded in `ha-core/`. Cross-refs [`scheduling-model.md`](scheduling-model.md),
> [`offline.md`](offline.md), [`security.md`](security.md).

---

## TL;DR

- **Deterministic undo, not LLM-reconstructed undo.** The LLM's *only* job is to
  **recognize the intent to undo**; the **reversal itself is deterministic** — the shell
  replays a pre-recorded inverse. Making the model *reason out* how to undo what it did is
  exactly the §5.4 determinism-in-tools anti-pattern (unreliable, and it can't see prior
  state). This is §5.4 applied to reversal.
- **Command pattern / compensating action.** Every *mutating* capability, at execute
  time, returns an explicit outcome: `UndoAction`, `UndoUnavailable`, or `NoMutation`.
  "Undo" inspects the **latest mutation** and either runs its inverse once or declines.
  It never skips an unsupported action to undo something older. The function that did the
  action builds the compensation because it knows what changed; the gateway only records
  and dispatches it.
- **HA already has the hard part for device control:** HA's state-reproduction helper can
  restore captured state plus attributes without creating a persistent scene entity, and
  intents expose which targets succeeded. Snapshot candidates *before* the action, filter
  the inverse to `success_results` afterward, and optionally record expected post-action
  state so replay can decline if the world moved on.
- **Recognize undo as a *local intent* where possible** (deterministic, fast, and it
  **works offline** — you turned the lights off locally, "turn them back" must too,
  [`offline.md`](offline.md)). Fall back to an LLM-recognized `undo` only for phrasings
  hassil misses.
- **Not everything is undoable.** Irreversible side-effects (a sent message, a purchase,
  a spoken announcement), a world that **moved on** since the action, and read-only ops
  (web_search) have no clean inverse — decline **legibly**.
- **This journal covers only *assistant-caused* actions.** Reversing a change the assistant
  didn't make (a physical switch, an automation) by reconstructing it from logs is a
  different, **functionally impossible** problem — see [`explainability.md`](explainability.md):
  not for missing data (history has prior values) but because attribution is partial and
  "undo" against a live/recurring external cause is ill-posed.
- **No undo exists in core** (verified) — greenfield.

---

## The principle: recognize-with-LLM, reverse-deterministically

The division of labor is the same as everywhere else in the design:

- **LLM decides intent** — "undo that" / "put it back" / "never mind" / "forget that" all
  map to *reverse the last thing you did*. Fuzzy, natural-language, model-friendly.
- **The tool/shell does the reversal** — deterministically, from a **recorded inverse**,
  because the model can't reliably reconstruct prior state or the exact created id, and
  guessing is dangerous (an action already happened).

So undo is **not** "ask the model to emit the opposite command." It's a **command-pattern
journal**: an undo-capable intent or tool returns an inspectable, provider-neutral
`UndoAction` descriptor plus the data needed to run it; the execution gateway stores it;
"undo" executes it. A lambda describes the ownership intuition, but it is not the contract:
a closure cannot be serialized, traced, or moved cleanly into core.

---

## The undo journal (what each capability records)

Per possible mutation, store `{execution_id, turn_id, created_at, expires_at, description,
status, disposition}`. An undoable disposition also contains an authorization binding and
an immutable, provider-neutral `InverseOperation`; the acting tool or intent produces it.
Inverse arguments are carried out of band from the public tool-result mapping and therefore
are not serialized into model context. Descriptor construction validates all nested keys and
leaves as JSON, including finite-number and circular-reference checks, before retaining an
immutable owned copy. Python annotations alone are not the boundary.

There are three successful execution outcomes:

- `NoMutation`: read/no-op; do not add a journal entry or shadow an older mutation.
- `UndoAction`: a localized description, household or exact personal-owner binding, and an
  inspectable inverse descriptor.
- `UndoUnavailable`: an explicit barrier classified as `impossible`, `prohibited`, or
  `not_supported`.

An unclassified or mutating tool that supplies no disposition is conservatively recorded as
`not_supported`. A tool exception also records that barrier because a partial effect may
already have happened. This fail-closed rule prevents "undo" from silently targeting an
older, unrelated action.

| Action | What's recorded | Deterministic inverse |
|---|---|---|
| **Device control** (TurnOn/Off, LightSet, SetPosition, …) | pre-action state+attribute snapshot, filtered after execution to successful entity IDs | HA state reproduction for those exact entities |
| **Memory note (create)** | the written slot + row id | delete the row |
| **Memory note (overwrite)** | the **prior** slot value | restore prior value |
| **Alias add** | the added alias string + entity | remove that alias (registry read-modify-write, [`memory.md`](memory.md)) |
| **Reminder / calendar / todo create** | the created **id/UID** | delete by id |
| **Calendar delete** (destructive) | the **full deleted event** payload | recreate it |
| **Ephemeral automation create** | the rule id (+ each fired body tool's own journal entry) | remove the rule; **already-fired effects reverse via their body tools' inverses** |
| **Music play** | — | *correction*, not state-undo (below) |

The snapshot for device control is the load-bearing generic case. `UndoHelper` captures
state and attributes before execution only for domains the caller explicitly marks safe.
After execution, the intent's successful target IDs select the captured subset. Replay uses
HA state reproduction directly; it does not create a scene entity. When expected
post-action states are supplied, an exact state/attribute mismatch consumes the replay as a
failure rather than overwriting a changed world. Locks are therefore not made undoable just
because their state is technically reproducible.

Snapshot restoration and an inverse call to the same intent are two reusable strategies,
not universal inference. Created records normally use a capability-specific delete-by-ID
inverse. Undoing deletion requires enough owned data to restore the aggregate: for Magic
Mic stores, prefer tombstones or an explicit aggregate snapshot so child/relationship data
is restored transactionally. The generic undo layer must never guess foreign-key semantics.
Recorder history is likewise not an inverse source: it may contain old values, but lacks
reliable assistant attribution and domain-specific compensation semantics.

**Storage location:** the bounded live journal is conversation-session state exposed through
`MagicMicChatLog`, not content appended to the transcript and not another interaction
object. Its backing store is keyed by `conversation_id` because HA clones the `ChatLog`
dataclass between turns; a value placed only in the subclass instance dictionary would be
lost. The implemented replay lifetime is two minutes and session-only. Later, if user
evidence requires undo across chat-session expiry or restart, promote the bounded journal
to a short-TTL integration store without changing the capability inverse contract.

## Implemented foundation contract

The pre-Wave-1 foundation now provides:

- immutable `UndoAction`, `UndoUnavailable`, `NoMutation`, and `InverseOperation`
  descriptors;
- a bounded journal in `MagicMicSessionState`, with single-use states (`available`,
  `executing`, `undone`, `failed`, `expired`, `unavailable`), execution IDs, turn IDs, and
  a two-minute expiry;
- authorization on replay using the resolved household/personal principal while preserving
  the separate HA `Context` used by intent/service authorization;
- an executor registry, inverse-intent executor, and opt-in state-snapshot helper/executor;
- private result metadata for direct tools and HA `IntentResponseDict` results;
- effect classification (`read_only`, `mutating`, `unknown`) at the proxy seam, with
  conservative barriers for possible mutations lacking metadata.

Replay claims an entry before execution. Success and failure both prevent a second replay;
an authorization denial does not consume it. Repeating the original live command remains
valid because the gateway performs no semantic deduplication.

This is the seam, not the complete user feature. There is not yet a `HassUndo` sentence
intent or LLM fallback tool, no production Magic Mic mutating capability yet consumes the
helper, and the core intent catalog has not been retrofitted.

---

## Recognizing "undo" — prefer a local intent

Two ways to catch the intent, and the more deterministic one is better (and offline-safe):

1. **Local intent (preferred)** — a `HassUndo`-shaped sentence intent ("undo", "undo
   that", "turn it back", "put it back", "never mind that"). Deterministic, zero-latency,
   and — critically — **works when the cloud is down**: a locally-handled action
   (`prefer_local_intents`, §2.9) must be locally undoable, or offline undo breaks. This is
   a **helps-local core contribution** (same DNA as find_entities / calendar intents,
   §7). The intent just triggers "run the last journal inverse."
2. **LLM-recognized `undo` tool (fallback)** — for phrasings hassil misses ("no no, I
   liked it how it was") the model recognizes intent-to-undo and calls a deterministic
   `undo` tool. Still no reasoning about *how* — the tool owns that.

Either way the reversal is the journal replay; recognition is the only fuzzy part.

---

## Don't conflate four different "take it back" gestures

| Gesture | Meaning | Mechanism |
|---|---|---|
| **Undo** (this doc) | reverse a **completed** action | journal inverse replay |
| **`HassNevermind`** | abort an **in-progress** interaction (cancel, no action taken) | existing core intent (§2.5); nothing to reverse |
| **Music correction** | "no, the other one / next" over a **huge catalog** | play the next candidate ([`music-playback.md`](music-playback.md)) — a domain re-query, not a state restore |
| **Snooze** | defer a delivered reminder | ack + reschedule ([`scheduling-model.md`](scheduling-model.md)) |

Undo is specifically *reverse the last completed mutation*. `HassNevermind` is "stop, I
hadn't finished asking." Keeping them distinct avoids a muddled single mechanism.

---

## What's undoable — and what isn't

- **Cleanly undoable:** device state (snapshot/restore), memory/alias (delete/restore),
  create-actions (delete created), automation create (remove).
- **Costly but possible:** calendar *delete* (recreate from the saved payload — keep it in
  the journal precisely so delete stays reversible; ties to [`security.md`](security.md)
  L2, since delete is high-consequence).
- **Not undoable — decline legibly:**
  - **Irreversible side-effects** — a `conversation_command`/script that sent a message,
    made a purchase, ran an external action; a **spoken announcement** already played; a
    door that was unlocked (state re-lockable, but the security event happened).
  - **Read-only ops** — web_search/web_fetch, a Q&A answer: nothing to reverse.
  - **The world moved on.** If a targeted entity **changed since** the action (someone else
    hit the switch, an automation fired), restoring the old snapshot may be *wrong*. Undo
    should check "is this entity still as I left it?" and, if not, **confirm or decline**
    rather than blindly restore — same "don't act on a stale world" instinct as reminder
    catch-up ([`scheduling-model.md`](scheduling-model.md)).
- **Time-boxed.** Undo is meaningful only for a short window. Foundation replay expires in
  two minutes; "undo" refers to the latest individual mutation, not a prior supported item,
  a whole multi-tool turn, or arbitrary history.

---

## Why this is a load-bearing primitive (the optimism it underwrites)

Several docs choose **optimistic execution + cheap correction** over clarify-first. Undo
is what *makes that safe*, so it's a dependency of all of them:

- `prompt-context.md`'s **terminal-intent fast path** (optimistic-execute, speak
  before confirming success) — tolerable because a wrong result is undoable.
- `memory.md`'s **optimistic-write-and-mention** ("noted — say 'forget that'") — literally
  an undo instance; factor its "forget that" into this primitive (the memory tool records
  the inverse; "forget that" = undo scoped to the last memory write).
- `music-playback.md`'s **optimistic best-match play** — its correction is domain-specific
  ("next"), but it's the same "act now, cheaply reverse" bet.
- `security.md`'s reversibility argument — "most sinks are reversible, so a successful
  injection can't do irreversible harm" is only *true* if reversal is real. Undo
  **operationalizes** that claim (and pairs with the L2 confirm-gate on the *ir*reversible
  ones). Undo only ever reverses the assistant's **own recent** actions, so it isn't a new
  injection sink.

- `ephemeral-automations.md`'s **ephemeral overrides** ("lights to 100% for 15 min") reuse
  the same state-snapshot/reproduction mechanism as their revert target, applied on a
  boundary trigger rather than on an "undo" utterance.

Stated once here so those docs can lean on "undoable" without each re-deriving it.

---

## Scope & phasing

- **v1 = selective, single-level undo** of the latest individual mutating action.
  Instrument a few representative Magic Mic/demo intents and capability tools to prove the
  contract. All other intents report that the action cannot be undone until their core
  handlers adopt `UndoAction`; do not fork the whole intent catalog for the POC.
- **Later = a bounded stack** ("undo the last three"), if demand shows — same journal, a
  few entries deep.
- **Build order:** the **device-control** inverse (snapshot/restore) is the highest-value
  and is mostly HA plumbing; the **create-actions** inverse (delete-by-id) rides each
  capability's own build (reminder/calendar/todo/memory already record ids). So undo is
  cheap **if** each mutating tool returns its inverse at build time — make "declare your
  inverse" part of the capability contract (§5.5), not a bolt-on.

---

## Remaining direction

- Add a local `HassUndo`-shaped intent plus an LLM fallback tool that both invoke the same
  journal replay. Neither may ask the model to reconstruct an inverse.
- Intercept locally handled hassil mutations. Today they can bypass `TestbedAPI`, so the POC
  must not claim them as undoable. The core-shaped destination is outcome metadata on
  `intent.async_handle()` / `IntentResponse`, captured by the shared execution gateway.
- Instrument a few representative Magic Mic/demo mutations as they are built: inverse
  intent, state snapshot, custom create/delete compensation, and explicit impossible or
  prohibited outcomes. Do not fork the full core intent catalog for the demonstration.
- Decide per capability whether exact post-state checking is sufficient or whether a
  localized confirmation/decline policy is needed for world-moved-on cases. Foundation
  snapshot replay intentionally declines.
- Register and expose the default executor set when the first user-facing undo entry point
  lands; persistence beyond the two-minute session window remains evidence-driven.

## Evaluation gate

Each inverse first needs exact deterministic action→journal→replay coverage, including
expiry, single-use consumption, barriers, and world-moved-on refusal. Before voice or text
"undo that" is called complete, a multi-turn trajectory must perform the original mutation
and undo in one conversation, verify the final state/effect ledger, and prove that an expired
or unsupported latest action cannot fall through to an older entry. Locally handled actions
remain outside the claim until the local-first driver shows their outcomes enter the journal.

---

## Key references

- `homeassistant/helpers/state.py` — `async_reproduce_state` (deterministic state restore
  without a scene entity)
- `homeassistant/helpers/intent.py:1095,1147,1347` — `success_results` / `failed_results`
  / `IntentResponseTarget` (which entities an intent affected)
- §2.5 — `HassNevermind` (abort-in-progress, distinct from undo)
- PRODUCT_PLAN §5.4 (determinism-in-tools — the reason reversal is deterministic), §5.5
  (capability contract — where "declare your inverse" belongs)
