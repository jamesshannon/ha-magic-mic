# Todo / Lists

> **Thin doc.** Basic todo already works by voice; the design decisions live in
> [`scheduling-model.md`](scheduling-model.md) (the reminder×todo boundary,
> todo-as-store, "todos don't fire on due"). This doc just records the verified
> surface and the small residual. See PRODUCT_PLAN §2.5 / §4.

---

## TL;DR

- **Base case is done.** Add / complete / remove intents + a `todo_get_items` read
  tool are already exposed to the LLM (`todo/intent.py`, `todo/llm.py:32`). Nothing to
  build for "add milk to my shopping list," "what's on my list," "mark it done."
- **Todos never fire on due** (`todo/trigger.py` fires on list *mutation*, not
  due-time). So a **firing** task is a **reminder**, not a todo — decided in the spine.
- **Rich edits aren't exposed** — the entity supports UPDATE/MOVE/SET_DUE/DESCRIPTION
  (`TodoListEntityFeature`) but only add/complete/remove are wired to intents. Richer
  manipulation is a **low-priority fast-follow** (thin intents over existing methods).

---

## Verified surface (`ha-core/`)

| Capability | Exposed to LLM today? | Where |
|---|---|---|
| Add item | ✅ intent (list name **required**, **exact-match**) | `todo/intent.py` (`ListAddItem`) |
| Complete item | ✅ intent (same) | `ListCompleteItem` |
| Remove item | ✅ intent (same) | `ListRemoveItem` |
| Read items | ✅ tool `todo_get_items` (list = **enum** of real names) | `todo/llm.py:32` |
| Multiple lists | ✅ each list = a `TodoListEntity`; every op targets a **named** list | — |
| Update / rename / description | ❌ (entity supports it) | `TodoListEntityFeature.UPDATE/SET_DESCRIPTION` |
| Move / reorder | ❌ (entity supports it) | `MOVE_TODO_ITEM` |
| Set due date | ❌ (entity supports it) | `SET_DUE_DATE_ON_ITEM` / `..._DATETIME_ON_ITEM` |
| Fire on due date | ❌ **not a feature** — trigger is list-mutation only | `todo/trigger.py` |

---

## The one design point (already settled upstream)

**Firing vs. list is the reminder×todo boundary** ([`scheduling-model.md`](scheduling-model.md)):
- "Add milk to my list," "add a task" → **todo** (passive list; the base case above).
- "Remind me to call mom at 5" → **reminder** (fires; native store owns trigger+delivery).
- A task the user wants **both listed and nudged** → a `ScheduledItem` with todo
  placement and a linked external item UID. The `ScheduledItemStore` owns firing and
  delivery state (watermark/catch-up); the todo item is the **visible/synced copy**. Todos
  themselves never fire, so the nudge can't come from HA's todo trigger.

Route by the user's **intent to fire**, not the word — same normalization discipline
as the rest of the scheduling model.

---

## Residual (small, low-priority)

- **Rich-edit intents** (update/rename/move/set-due/description): each is a thin intent
  over an existing entity method + feature-flag gate — the calendar pattern. Build only
  if demand shows; §4 rates todo "works; UX rough; fine as base."
- **Multiple lists work, but list-targeting has two gaps.** Both intents and the read
  tool require targeting a **named** list; there is **no default/primary list** and **no
  content inference** ("milk" → shopping), so the LLM must name a list every call —
  even with a single list. And the **write intents resolve the list by exact match**
  (`async_match_targets` → §2.4 `_filter_by_name`), so "groceries" vs "Shopping List"
  fails just like a device name → **`find_entities`'s fuzzy match-layer fallback is the
  fix** (a list is an entity in `DOMAIN=todo`; see [`find-entities.md`](find-entities.md)).
  The read tool avoids this (list is a `vol.In` **enum** of real names).

### Default lists — scope at build time (proposed direction)
The net-new convenience worth designing: **two blessed default lists** — a generic
**tasks** list ("add to my list") and a **shopping** list ("add to my shopping list") —
mirroring how Google/Alexa special-case shopping (distinct mode: frequent adds,
in-store check-off, household-shared).

- **Resolve-then-create-if-absent**, not create-unconditionally: fuzzy-match the user's
  reference against *existing* lists first (reuse the `find_entities` scorer — a list is
  a `DOMAIN=todo` entity), auto-create **only** when nothing matches. Same
  don't-duplicate discipline as alias-collision / note-slots (a user with "Groceries"
  shouldn't get a second "Shopping List") — the scorer's **fourth consumer**.
- **Bound auto-creation to the two categories** (tasks, shopping); any other named list
  requires the user to have it or to say "make a list called X" — keeps lists from
  proliferating, keeps behavior predictable.
- **Lazy on first add** (not eager at setup) + **name it back** ("made you a shopping
  list and added milk" — surprise principle).
- **Ownership (build question):** own the defaults as our `TodoListEntity`s — parallel
  to the native reminder store being a `CalendarEntity` we own — rather than provisioning
  `local_todo` config entries (config-flow, awkward to auto-create); but *prefer an
  existing user list* on a fuzzy hit.
- **Keying:** shopping ≈ household, tasks ≈ per-user. An unidentified `"default"` caller can
  use the household shopping list but cannot acquire a pseudo-personal task list; personal
  tasks require a resolved person (§5.1).
- `SET_DUE_DATE` is the one edit that interacts with reminders — but per above, a
  due-date the user expects to *fire* should route to a reminder-on-a-list, not a bare
  todo due-date (which is inert).

---

## Key references

- `todo/intent.py` — add/complete/remove intent handlers
- `todo/llm.py:32` — `TodoGetItemsTool` (`todo_get_items`)
- `todo/const.py:35` — `TodoListEntityFeature` (CREATE/DELETE/UPDATE/MOVE/SET_DUE/DESC)
- `todo/trigger.py` — list-mutation trigger (**not** due-time)
- [`scheduling-model.md`](scheduling-model.md) — reminder×todo boundary, todo-as-store
