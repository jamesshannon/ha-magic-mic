# Undo / Correction

> Cross-cutting **shared primitive** the §5.6 method surfaced late: "undo that," "turn it
> back," "never mind, put it how it was," "forget that." Invoked piecemeal across
> [`memory.md`](memory.md) ("say 'forget that' to undo"), [`music-playback.md`](music-playback.md)
> ("no, the other one / next"), [`prompt-context.md`](prompt-context.md) (optimistic-
> execute + fallback), [`find-entities.md`](find-entities.md) — designed nowhere. It's the
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
  time, records **its own inverse** into an **undo journal** keyed to the turn. "Undo" =
  pop the last entry and run the inverse. The tool that did the action declares how to
  reverse it — never a generic reasoner.
- **HA already has the hard part for device control:** `scene.create` with
  `snapshot_entities` + `scene.apply` captures and restores full entity state (incl.
  attributes) deterministically (`homeassistant/scene.py:81,120`); intents expose exactly
  which entities were affected (`success_results`/`failed_results`, `intent.py:1095/1347`).
  Snapshot the targeted set *before* the action, restore on undo.
- **Recognize undo as a *local intent* where possible** (deterministic, fast, and it
  **works offline** — you turned the lights off locally, "turn them back" must too,
  [`offline.md`](offline.md)). Fall back to an LLM-recognized `undo` only for phrasings
  hassil misses.
- **Not everything is undoable.** Irreversible side-effects (a sent message, a purchase,
  a spoken announcement), a world that **moved on** since the action, and read-only ops
  (web_search) have no clean inverse — decline **legibly**.
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
journal**: each mutating tool, when it runs, returns an *inverse action* + the data needed
to run it; the shell stores it; "undo" executes it.

---

## The undo journal (what each capability records)

Per mutating action, store `{turn_id, timestamp, description, inverse}` where `inverse` is
capability-specific and **produced by the acting tool**:

| Action | What's recorded | Deterministic inverse |
|---|---|---|
| **Device control** (TurnOn/Off, LightSet, SetPosition, …) | pre-action **state snapshot** of the *targeted* entities (`success_results` → entity_ids → `scene`-style snapshot) | restore the snapshot (`scene.apply` / re-issue the entities' prior states + attributes) |
| **Memory note (create)** | the written slot + row id | delete the row |
| **Memory note (overwrite)** | the **prior** slot value | restore prior value |
| **Alias add** | the added alias string + entity | remove that alias (registry read-modify-write, [`memory.md`](memory.md)) |
| **Reminder / calendar / todo create** | the created **id/UID** | delete by id |
| **Calendar delete** (destructive) | the **full deleted event** payload | recreate it |
| **Ephemeral automation create** | the rule id | remove the rule |
| **Music play** | — | *correction*, not state-undo (below) |

The snapshot for device control is the load-bearing case, and HA hands it to us:
`scene.create` with `snapshot_entities` grabs the current state+attributes of a set of
entities into a re-applyable scene; the intent's `success_results` tells us the set. So
"undo" of "dim the kitchen to 30%" restores the *exact* prior brightness/color/on-state —
no model reasoning, no lossy inverse.

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
- **Time-boxed.** Undo is meaningful only for a short window (the conversation / a few
  minutes). The journal is bounded and recent; "undo" refers to the **last** action (or
  last turn), not arbitrary history.

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

Stated once here so those docs can lean on "undoable" without each re-deriving it.

---

## Scope & phasing

- **v1 = single-level undo** of the **last mutating action/turn** (what assistants
  actually offer). One journal slot, replayed. Covers "undo that."
- **Later = a bounded stack** ("undo the last three"), if demand shows — same journal, a
  few entries deep.
- **Build order:** the **device-control** inverse (snapshot/restore) is the highest-value
  and is mostly HA plumbing; the **create-actions** inverse (delete-by-id) rides each
  capability's own build (reminder/calendar/todo/memory already record ids). So undo is
  cheap **if** each mutating tool returns its inverse at build time — make "declare your
  inverse" part of the capability contract (§5.5), not a bolt-on.

---

## Open questions

- **Undo granularity** — last *action* vs last *turn* (a turn may chain several intents);
  does "undo" reverse all of the last turn's mutations or just the last one?
- **Snapshot cost** — snapshot every targeted set pre-action (cheap, but per-command
  overhead) vs only when an action looks undoable/high-value; TTL on the journal.
- **World-moved-on policy** — restore-anyway vs confirm vs decline when an entity changed
  since; how to detect cheaply (state last_changed vs the action timestamp).
- **Local `HassUndo` vs LLM tool** — is the local intent enough coverage, and does the
  journal live somewhere the local path can reach it (it must, for offline undo)?
- **Declare-your-inverse contract** — the exact shape a capability returns so the shell can
  journal + replay uniformly (an `inverse` action dict? a callable? a service+data?).

---

## Key references

- `homeassistant/components/homeassistant/scene.py:81,120,231` — `snapshot_entities` /
  `SERVICE_APPLY` (deterministic state capture + restore)
- `homeassistant/helpers/intent.py:1095,1147,1347` — `success_results` / `failed_results`
  / `IntentResponseTarget` (which entities an intent affected)
- §2.5 — `HassNevermind` (abort-in-progress, distinct from undo)
- PRODUCT_PLAN §5.4 (determinism-in-tools — the reason reversal is deterministic), §5.5
  (capability contract — where "declare your inverse" belongs)
