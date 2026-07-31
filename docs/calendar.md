# Calendar: Read & Write

> Calendar as a capability: the existing **read** tool, and the **write** surface we
> add (create/update/delete events). The *scheduling* side of calendars
> (reminders/alarms landing in a native store, the calendar-**trigger**, delivery) is
> [`scheduling-model.md`](scheduling-model.md); this doc is the **event CRUD tool**
> that the "add it to my calendar" branch of that model calls. Grounded in
> `ha-core/`. Cross-refs: [`find-entities.md`](find-entities.md) (disambiguation
> pattern), [`prompt-context.md`](prompt-context.md).

---

## TL;DR

- **Read exists, write is greenfield.** `calendar_get_events` is exposed to the LLM
  today (read-only, §2.5); there is **no** create/update/delete LLM tool (confirmed —
  grep of `llm.py` empty). Filling the write half is the whole job.
- **Sharp CRUD asymmetry in core** (verified inventory below): **CREATE** is a proper
  entity **service** and broadly supported (Google/CalDAV/local); **UPDATE/DELETE are
  websocket-only** (no service to call) and thinly supported — **UPDATE is
  `local_calendar`-only**; Google/CalDAV cannot update through HA at all.
- **Scope: CREATE v1, DELETE fast-follow, UPDATE punt.**
  - v1 = a thin Tool over `calendar.create_event` — covers the dominant "add X to my
    calendar," works on the real backends people use.
  - DELETE = needs a **new `calendar.delete_event` service** (clean core PR filling
    the §2.6 read/write asymmetry) + **event-resolution** (read → fuzzy-over-summaries
    → disambiguate).
  - UPDATE = out of scope (local-only; delete+recreate loses attendees/identity).
- **Shared create path with reminders.** Per [`scheduling-model.md`](scheduling-model.md)
  native `ScheduledItem`s are projected through a `CalendarEntity` declaring
  `CREATE_EVENT`. The same create flow chooses native placement or creates a real event plus
  a UID-linked companion `ScheduledItem` for assistant delivery state. Calendar-write and
  the reminder store are **not separate builds.**

---

## Verified inventory (`ha-core/`)

| | CREATE | UPDATE | DELETE |
|---|---|---|---|
| **local_calendar** | ✅ | ✅ | ✅ |
| **Google** | ✅ | ❌ | ✅ |
| **CalDAV** | ✅ | ❌ | ❌ |
| **rachio** (niche irrigation) | — | — | ✅ |
| **Service surface** | ✅ `calendar.create_event` | ❌ websocket-only | ❌ websocket-only |

- **Feature flags:** `CalendarEntityFeature.{CREATE,UPDATE,DELETE}_EVENT`
  (`calendar/const.py:31`). Entity methods `async_create_event` / `async_update_event`
  / `async_delete_event` (`calendar/__init__.py:766–779`), default `NotImplementedError`.
- **CREATE is a service:** `calendar.create_event`, registered as an entity service
  gated on `CREATE_EVENT` (`__init__.py:330`, schema `:225`). → cleanly wrappable as an
  LLM tool.
- **UPDATE/DELETE are websocket-only:** `handle_calendar_event_*` handlers with
  `connection` + `POLICY_CONTROL` checks (`__init__.py:884–990`). **No service exists**
  even where the backend implements the method — to offer them you either call the
  entity method directly (bypasses the websocket permission layer) or **contribute the
  missing services** (preferred; core-worthy).
- **Read tool today:** `calendar_get_events` (read-only), plus the `calendar.get_events`
  service (`SupportsResponse.ONLY`, `:337`).

---

## Design

### CREATE (v1)
A custom Tool over `calendar.create_event` (not a raw `ActionTool`) so it can:
- **Require a resolved person for personal calendars.** The unidentified `"default"`
  principal has household scope only, so personal calendar tools are absent from its tool
  list and rejected again at execution. A future explicitly household calendar may opt into
  household access; no calendar silently becomes one.
- **Normalize datetime deterministically** — "next Tuesday 3pm," "tomorrow morning" →
  absolute ISO/duration in the tool, not the model (determinism-in-tools §5.4; HA owns
  tz/`now()`). **Localization boundary (§5.7):** *understanding the phrase* is
  language-dependent → the **multilingual model** resolves the natural-language reference
  to a structured form (relative offset / partial date); the **tool** does only the
  deterministic **tz / calendar arithmetic** on that. The tool must **not** parse
  natural-language dates per-language (un-localizable). Same split for the weather-forecast
  and reminder time inputs.
- **Select the target calendar** — gate to `CREATE_EVENT`-capable entities; default to
  the single writable one, else a configured/primary calendar. **Name it back** ("added
  to your Google calendar") — the surprise principle from
  [`scheduling-model.md`](scheduling-model.md): surface *where it went*, no hidden side
  effect.
- Core path: contribute as an **intent** so the local agent gets it too (same logic as
  `find_entities` / create-service exposure).

### DELETE (fast-follow)
Two prerequisites: a **new `calendar.delete_event` service** (mirror create), and
**event-resolution** — call `calendar_get_events` (read) to find the target and its
UID, fuzzy-match the user's phrase ("my dentist appointment") against event **summaries**
with a top-1/top-2 disambiguation guard. This is the disambiguation pattern again, but
over **read-results**, not the registry — events aren't registry entities, so it's
fuzzy-over-fetched-events, not the entity scorer. Delete by UID.

> **Security note:** event summaries/descriptions are **untrusted** (anyone who can send
> an invite writes them), and delete feeds them to the model *and* performs a destructive
> action — a prime injection vector. Normalize and store the exact delete as an immutable
> pending operation, have the model name the resolved event in the confirmation, and execute
> only that stored operation after approval. This prevents argument reconstruction but assumes
> the model describes the event honestly; it is not an injection-independent gate
> ([`security.md`](security.md) L2). Treat the summaries as data, not instructions (L4).

### UPDATE (out of scope)
Only `local_calendar` supports it; Google/CalDAV can't update through HA. Modeling it as
delete+recreate loses attendees/identity and is risky. Punt; if ever built, it's
local-only or waits on upstream backend support.

---

## Where this sits vs. the scheduling model

The two are distinct but meet at `create_event`:
- **`scheduling-model.md`** owns the *native* store (reminders/alarms), the durable
  **trigger** (watermark/catch-up), and **delivery** — the "keep it private, fire it at
  me" machinery.
- **This doc** owns the *event CRUD tool* — the "put it on my real, shared, synced
  calendar" action, invoked only on the explicit "add it to my calendar" cue (the
  surprise principle keeps it opt-in).
- They share the **create path**: native store = a `CalendarEntity` with `CREATE_EVENT`;
  real calendars = `CalendarEntity`s with `CREATE_EVENT`. One create flow, `store` field
  routes native vs real.

---

## Open questions

- Calendar-selection default when several writable calendars exist (primary flag? config?).
- `create_event` param coverage (all-day vs timed, recurrence/RRULE, attendees,
  location) — how much to expose vs keep simple.
- Whether to land DELETE's missing service as a standalone core PR before wiring the tool.
- Event-resolution ranking for delete (summary-only vs summary+time+location).

---

## Key references

- `calendar/const.py:31` — `CalendarEntityFeature` flags
- `calendar/__init__.py:330` — `create_event` entity service; `:225` schema
- `calendar/__init__.py:766–779` — `async_create/update/delete_event` methods
- `calendar/__init__.py:884–990` — websocket-only create/delete/update handlers
- Write-capable platforms: `google`, `caldav`, `local_calendar` (`.../*/calendar.py`)
