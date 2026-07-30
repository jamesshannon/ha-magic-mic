# Scheduling Model: Timers, Alarms, Reminders, Todos, Calendar

> Architecture spine for all time-based features. The concepts overlap heavily
> and share a substrate; this doc defines the shared model so `timers.md`,
> `reminders.md`, and the sleep-timer in [`ambient-noise.md`](ambient-noise.md)
> can be thin feature docs over it. See [`../PRODUCT_PLAN.md`](../PRODUCT_PLAN.md).

---

## TL;DR

- Timer / alarm / reminder / todo / calendar are **points in one design space**
  (trigger-time + payload + delivery + lifecycle), not unrelated systems.
- **Correction:** timers are *not* payload-less — `HassStartTimer` carries a
  `conversation_command` run on finish. "Turn off the lights in 5 minutes" is a
  timer.
- **HA already has most of the *trigger* half:** a **Calendar Trigger** (fire on
  event start/end ± offset), `calendar.create_event` (backend-gated), RRULE, and
  local persistence via `ical`. The **missing universal piece is *delivery***
  (announce/notify/run-command + snooze/ack), which is source-agnostic.
- **Store choice is driven by the user's visibility/sync expectation, not
  technical convenience** — private native store by default; the *real* calendar
  only on explicit "on my calendar" (the surprise principle).
- **Unifying design:** implement the native store *as a `CalendarEntity`* → it's
  the private store, the optional visible view, *and* a calendar-trigger source at
  once. The one delivery engine then fires over native + real calendars uniformly.

---

## The ontology

All five are the same skeleton — **(trigger time / recurrence) + (payload) +
(delivery) + (lifecycle)** — differing on a few axes:

| Axis | Timer | Alarm | Reminder | Todo | Calendar |
|---|---|---|---|---|---|
| Time model | relative | absolute wall-clock | either | due-date (optional) | absolute + duration |
| Recurrence | one-shot | usually recurring | either | usually one-shot | recurring |
| Payload | optional command | none (ding) | content | content | content |
| On fire | ding / run cmd | ding/announce | notify content | (mark due) | pre-event notify |
| Durability | ephemeral-ok | **required** | **required** | **required** | **required** |
| Sync/shared | local | local | local | often synced | synced |
| Completion | fire-and-forget | fire-and-forget | fire-and-forget | **completable** | RSVP |

The concepts are **VUI presets** over the shared model. Keep them distinct at the
VUI/FR level (users expect different behavior, and even Google conflates
timer/alarm) — but share the substrate underneath.

---

## Correction: timers carry a payload

`HassStartTimer` has a `conversation_command` slot (`intent/timers.py:832`); on
finish, `_timer_finished` runs it via `async_converse` (`timers.py:460`):

```python
if timer.conversation_command:
    async_converse(hass, timer.conversation_command, ...)   # run the command
elif timer.device_id in self.handlers:
    self.handlers[timer.device_id](FINISHED, timer)          # or ding the device
```

So a timer is already a **relative-time scheduled action**, not just a countdown —
which is why the timer/reminder line blurs. The existing `TimerManager` is
ephemeral + Assist-only + relative-time; reminders differ on *durability +
time-model + delivery-type*.

---

## What HA already has (verified inventory)

| Capability | Status | Where |
|---|---|---|
| Countdown timer subsystem | Exists; **ephemeral**, Assist-only; device-ding or `conversation_command` | `intent/timers.py` |
| Device delivery-handler pattern | Exists (`register_handler` → satellite ring/notify) | `intent/timers.py` |
| Ephemeral scheduling primitive | `async_track_point_in_time` (in-memory) | `helpers/event.py:1420` |
| **Calendar Trigger** (fire on event start/end ± offset) | **Exists** | `calendar/trigger.py` |
| Calendar **write** (`create_event`) | Exists, backend-gated (`CalendarEntityFeature.CREATE_EVENT`) | `calendar/__init__.py:224` |
| RRULE / recurrence | Exists (validation in platform; engine in `ical`) | `calendar/__init__.py:195`; `local_calendar` (`ical==14.0.1`) |
| Local persistence | `ical` + ICS store | `local_calendar` (`.storage/*.ics`) |
| Todo due-dates | Exists, feature-flagged | `todo/__init__.py` (`ATTR_DUE_DATE/DUETIME`) |
| Todo trigger | Exists but fires on **list changes** (added/removed/completed), **NOT due-time** | `todo/trigger.py` |

**Key takeaway:** HA already has the *trigger* half (calendar trigger), the
*write* half (`create_event`), and *recurrence + persistence* (`ical`). Todos do
**not** fire on due. There is **no durable, dynamically-created scheduled-action
store**, and **no Assist delivery layer** on top of triggers.

---

## The central insight: store ⊥ trigger; the missing piece is delivery

- HA can already *trigger* on calendar events. What it can't do is turn a trigger
  into **Assist-style delivery** — announce on the right satellite, notify the
  right user, run a command, with snooze/ack. **That delivery layer is the new
  universal piece, and it is source-agnostic.**
- **Persistence stakes** differ by concept: a **hard correctness requirement** for
  reminders/alarms — the time-horizon spans HA's monthly updates / reboots, and a
  silently-dropped "take your medication" reminder destroys trust (a reminder you can't
  trust is worse than none). For a countdown timer it's a **plus, not a requirement**
  (a 45-min oven timer surviving a 2-min restart is *good*), so it doesn't *force* the
  substrate — but see the timer note below: "ephemeral" is not a conceptual property of
  timers, it's inherited from core's current implementation.

### Timers are the short-grace corner of this same model (not conceptually ephemeral)
The ontology already lists **timer as one point in this design space**, so the substrate
should *generalize* to it, not exclude it. What actually distinguishes a timer is not
"can't/shouldn't persist" but a **different catch-up policy**, which the two-knob
catch-up ([below](#catch-up--re-execute-safety)) already parameterizes:

| Missed while HA was down | Policy |
|---|---|
| **Reminder** | **must surface** (dropping it destroys trust) → grace = long/∞ |
| **Timer** | **must *not* fire** ("your 10-min pasta timer!" 3 h post-reboot is wrong) → grace = **short** |

So a timer is just a consumer with `{target = requesting device, escalation = none,
recurrence = none, catch-up-grace = short, persistence = lazy backup}` — it falls out of
the model. **Why v1 still leaves timers on the shipped core path** (not a conceptual
exclusion): (1) core timers **already work and are VUI-integrated** — reminders are the
gap, and rebuilding a shipped subsystem is cost/surface for a nice-to-have; (2) the
timer *firing runtime* is genuinely timer-specific — second-precision + a **live
countdown** + pause/resume/"how much left?" is a stateful in-memory object, so the clean
split is **persist the record, keep in-memory firing**, not move timers onto the durable
trigger loop wholesale. ⇒ Design the substrate to *express* timers (don't exclude them);
ship v1 with timers on core; a **durable-backed timer** (persist record + in-memory
firing + short grace) is a cheap **future migration**, not a rebuild.

---

## Canonical model + normalization

The LLM normalizes fuzzy surface language into a canonical spec and routes by the
**spec**, not the word the user used:

```
ScheduledItem {
  trigger:  relative | absolute | recurring(RRULE)
  when:     ISO datetime / duration / RRULE
  payload:  content (to speak) | command (to run) | none (ding)
  delivery: satellite-announce | notify(user) | run-command
  store:    native | calendar(<id>) | todo(<list>)
  lifecycle: pending | delivered(k) | snoozed | acked | queued | cancelled
}
```

- **Don't error on mismatched words.** "Timer for 7:30am" (absolute) → make an
  alarm; "alarm for 5 minutes" (relative) → make a timer. This is an **LLM
  advantage** over hassil's rigid word→intent mapping.
- **Name it back** in the response ("Alarm set for 7:30 AM") — soft-corrects the
  vocabulary without friction, and surfaces *where it went* so there's no hidden
  side effect.

---

## Store choice = the user's visibility/sync intent (the surprise principle)

The user doesn't care about the *subsystem* for **behavior**, but very much cares
about **side effects** (visibility, sync, sharing). Route storage accordingly:

- **Default: native, local, private store** — reminders/alarms land here,
  invisible to the user's real calendar/todo. No surprise.
- **Real calendar — only on explicit cue** ("add it to my calendar"): synced,
  shared, on their actual schedule, as they asked.
- **Todo — only for genuinely task-like items on explicit cue** ("add to my
  list"): completable, on a list.

A recurring "take medicine daily" silently appearing on the user's shared Google
Calendar is a real expectation violation — so **native/private is the default**,
calendar/todo are opt-in targets.

---

## The unifying design: delivery engine over `CalendarEntity` sources

Reuse the **library, not the entity**. "Reuse the calendar" concretely means
reuse `ical` (RRULE + ICS persistence) — *not* puppeting a hidden calendar
entity, because:
- there is **no true "system-invisible" flag** (`hidden_by` still surfaces in the
  Calendar panel; `enabled_default=False` turns the entity *off*);
- calendar events can't hold payload/delivery/snooze/fired-state without
  overloading `description`;
- it's abstraction abuse maintainers would reject.

### Architecture
1. **Delivery engine** (new, universal): due-time → announce / notify / run-command
   + snooze/ack. Source-agnostic. The **satellite output primitive is
   `assist_satellite`'s service API** (`announce` / `start_conversation`), *not* the
   lower-level timer `register_handler` pattern — see
   [Satellite output primitive](#the-satellite-output-primitive-assist_satellite-not-register_handler).
2. **Trigger**: reuse the existing **Calendar Trigger** machinery (fire on event
   start ± offset) by making every store a `CalendarEntity`.
3. **Native store** = `Store` (JSON) for one-shot + `ical` for recurrence,
   **exposed as a `CalendarEntity`**. That one object is simultaneously:
   - the private source of truth (owns payload/delivery/snooze/fired-state),
   - the **optional visible view** (flip a visibility flag → renders in Calendar),
   - a **calendar-trigger source** (fires through the same path as real calendars).
4. **Real calendars** are already `CalendarEntity`s → `create_event` for the
   explicit "on my calendar" case; the same engine fires on them.

So native reminders and "on-my-calendar" reminders fire through **one** trigger +
delivery path.

### Scoping (reminders ≠ every calendar event)
The engine must fire only on **reminder-tagged events / opted-in calendars**, not
every event on the user's calendar — otherwise the assistant announces every
meeting. This is why Google keeps *Reminders* distinct from *Calendar events* even
though both live in the calendar: reminders fire proactively; events notify per
their own settings. Preserve that distinction.

**Ownership is captured at creation.** "Remind **me**…" resolves the owner `user_id` via
`get_resolved_user()` at capture and stores it on the reminder (§5.1). Firing scopes by that
stored value (whose calendar, whose personal note) and **never re-resolves identity at fire**
(there's no speaker then). *Whose* reminder (scoping) and *where* to deliver it
(targeting/escalation below) are independent outputs of that one capture.

### Todo's role
Todos **don't fire on due** (the trigger is list-mutation). So *firing-things are
reminders, not todos*; a todo-with-due-date stays a **passive list** and is an
opt-in target for task-like reminders the user wants on a list — never a firing
mechanism.

---

## Delivery: targeting, escalation & ack

The delivery engine is shared across timers/alarms/reminders — they differ only by
**default target + escalation profile**, not mechanism (timers are already
device-bound via `register_handler`; alarms want the longest escalation).

### Modality follows origin (voice-in → voice-out)
Two orthogonal delivery axes: **which device** (the targeting resolver below) and **which
channel/modality** (spoken vs. off-satellite text/notify). The default on the second is
**the response is delivered in the modality the request arrived in.** A voice-initiated
query — "tell me my summary," "what's on my calendar today" — is **spoken back on the
satellite**, never silently turned into a phone text; text/notify delivery is the
**opt-out**, taken only when the user expresses it ("text me the summary"). Same
never-interrogate rule as targeting: accept a volunteered channel, never demand one, never
branch on an invisible preference.

**Why this is also the safe first use of `get_resolved_user()`.** Off-satellite text
delivery needs identity, but only for **destination routing** (which person's notify
target), *not* for reading personal data — a much lower-stakes personalization than
calendar/PII access ([`docs/security.md`](docs/security.md)'s personalization-not-auth
line). So "text me my summary" is a clean early identity consumer: a wrong resolution
sends a benign summary to the wrong phone, it doesn't leak a protected read. The
**proactive/solicited daily summary** (the `assist_daily_summary.markdown` blueprint
pattern — conversation agent summarizes weather + calendar, ships via `notify`) is the
canonical case: valuable, but its text-push half stays **gated on user-resolution** and is
a Phase-4 proactive candidate, not a shipped default automation.

**What the "summary" payload actually is (payload ⊥ invocation).** The summary itself is
not a bespoke feature — it's a **rich-prompt command alias** (a routine:
[`learning.md`](learning.md) "rich-prompt target"), i.e. a saved prompt like *"give me a
spoken brief: today's weather, agenda before noon, reminders due today, under four
sentences."* That one payload is reachable through **multiple invocation front-doors**: a
**phrase** ("good morning" / "tell me my summary" → the alias fires), a **time** (7am → the
scheduling substrate fires the *same* rewrite target as a deferred `conversation_command`),
or a dashboard button. So "good morning" and a 7am auto-brief are the **same routine, two
invocations** — not a skill vs. an automation. This is the general shape for time-triggered
LLM behaviors here: the schedule carries a **rewrite-target payload**, and delivery-modality
(above) + targeting (below) decide how the result comes back.

### Magical vs. kludgy = FRICTION, not error
**Kludgy is the *effort of getting it right*, not the risk of getting it wrong.** A
reminder request that triggers setup questions ("you're James — should this follow
you? what if you don't ack? send it to your phone?") is kludgy *even when every answer
is correct*; pre-set **preferences are kludgy too** (setup is friction). Magical = the
right thing happens with **zero imposed friction.** Engine rule: **never interrogate at
creation, never require setup, never branch on an invisible inferred setting.** Reach
good behavior via fixed legible defaults + *pull-based* interaction at fire time.
Accept user-*volunteered* targeting ("remind me on the kitchen speaker"); never
*demand* it.

### Targeting is a SILENT degrade-to-floor resolver
Climb toward "smart" only on signals available **as a byproduct of the home** (an
occupancy sensor already installed, Voice-ID once trained) — never by asking, never on
feature-specific setup. The instant a rung would require interrogation or config,
**don't; silently use the floor** (graceful-degradation DNA of `find_entities` — a
miss degrades, never fails and never asks).

| Rung | Signal it needs | Silent & zero-setup? |
|---|---|---|
| **Fire on requesting device** (floor) | `device_id` in request | ✅ |
| **"remind me on [device]"** | user *volunteers* it | ✅ volunteered, not asked |
| **Escalate to broadcast on non-ack** | nothing | ✅ **reliability without identity** |
| **Presence (fire where they are)** | area occupancy sensor | ✅ if the sensor exists |
| **Device-owner → owner's push** | device→owner config (§5.1) | ⚠️ one-time setup |
| **Room-level follow** | BT beacon | ⚠️ rare hardware |
| **Full personal / multi-channel** | **Voice-ID** | ⚠️ one-time training, then silent |

Key insight: **escalate-to-broadcast buys *reliability* without *identity*** — you
needn't know where the person is if you eventually ping everywhere. The floor
(requesting device + escalate-on-non-ack) needs zero identity **and zero questions**,
ships now. Without Voice-ID a voice-set reminder is inherently semi-public and personal
channels stay gated (the *one* right phone needs identity — same `get_resolved_user()` seam
as memory scope) — a silent capability limit, **not** a reason to ask. The mechanics of
those gated **phone-push** rungs (and their off-satellite ack) are **parked** to Phase 4
below — see [Deferred: off-satellite ack](#deferred-off-satellite-ack-phone-push--phase-4-voice-id-gated).

#### This resolver is a SHARED primitive, not a reminders concern
The presence/BT-beacon rungs above are really a **generic "where is the person → target
set" resolver**; reminder delivery is just its *first* consumer. Its **second, already
proven, consumer is music-follow** ("play what's playing wherever I go"). The HA
community has shipped music-follow repeatedly — **Spotifynd**, the ESPresense-driven
**"Room Music Follow"** blueprints, **"Group Sonos based on presence"** — and every one
is the *same two-layer assembly*: `room presence (ESPresense / motion / BT beacon)` →
`payload action`. There is **no reusable "follow" concept in HA to import** (nothing in
core; each project re-assembles the two layers by hand) — which is exactly why the
resolver belongs *here* as a primitive, with the payload swapped per consumer:

| Consumer | Targeting layer (shared) | Payload action |
|---|---|---|
| **Reminder-follow** | presence → target-set | content-free announce + pull-to-read |
| **Music-follow** | presence → target-set | `media_player` grouping/transfer (`join`/`unjoin`) |

So reminder-follow and music-follow are **siblings**, not one reusing the other — both
sit on this resolver. Voice's role in both is trivial: a trigger phrase ("follow me" /
"begin follow mode") flips the resolver on. Music-follow itself stays **out of scope**
(see [`music-playback.md`](music-playback.md)); the community's demand for it just
**validates** building the resolver as a first-class, payload-agnostic primitive rather
than a reminders-only branch.

### Content-free announce + pull-to-read (= ack): one interaction, four wins
Default delivery is **content-free**: "⟨ding⟩ you have a reminder." The user **pulls**
the content — "read it" — and *that pull is the acknowledgement.*
- **Solves privacy without guessing** — nothing is read aloud until asked → no
  `privacy` field, no inference, no bystander leak.
- **Cleaner ack** than "Reminder to water the flowers — do you acknowledge?" (clunky,
  *and* already leaked the content).
- **Makes broadcast escalation non-intrusive.** "⟨ding⟩ a reminder" everywhere is a
  **notification** (ambient, exposes nothing, addressed to no one); "⟨ding⟩ water the
  plants" everywhere is a **message forced on the room.** That's *why* content-free
  feels lighter — it's the right register for an unsolicited interruption.

### The satellite output primitive (`assist_satellite`, not `register_handler`)
The engine emits sound on a satellite through **`assist_satellite`'s service API**, not
the timer subsystem's lower-level `register_handler`. On the merits it fits the delivery
model better:

- **`announce(message, preannounce=True)`** (`assist_satellite/entity.py:199`,
  service `__init__.py:72`) = the **content-free announce** exactly: the `preannounce`
  earcon (default `PREANNOUNCE_URL`) **is** the "⟨ding⟩", and it plays a message *without
  opening the mic* — which is precisely why the reminder stays **ambient/ignorable**
  (mic-open would demand engagement). It's a **multi-target entity service**, so
  broadcast escalation is free.
- **It blocks until the announcement finishes playing**, which is what lets escalation
  **sequence** deterministically (announce on the requesting device → await → escalate to
  broadcast) rather than racing overlapping audio.
- **`SatelliteBusyError`** (raised if the satellite is mid-interaction,
  `entity.py:231/289`) is a **real delivery condition that feeds the state machine
  below** — a busy target isn't a failure, it's a *defer/re-try-or-queue*.
- **`start_conversation(start_message, extra_system_prompt)`** (`entity.py:250`, service
  `__init__.py:89`) is the *other* mode — announce **and** open the mic **and** seed the
  chat session so the LLM knows it spoke first. Reserved for **solicited/engaging**
  delivery (a nudge you should answer) and Phase-4 proactive, **not** the default
  unsolicited reminder. It **requires an LLM conversation agent** (raises on the built-in
  agent, `entity.py:275`) — so proactive delivery is a cloud/LLM-path capability (a
  degrade-when-offline concern, see PRODUCT_PLAN roadmap).
- **Not `ask_question` for the ack.** `ask_question` (`entity.py:333`) truncates the
  pipeline at STT and matches the answer **locally against a fixed answer set (no LLM)** —
  a closed-set primitive. It does *not* fit the open, contextual pull-to-read ack or LLM
  disambiguation (see [`conversation-loop.md`](conversation-loop.md)); it's logged
  separately as a possible **generation-saving confirm-before-write** primitive
  ([`prompt-context.md`](prompt-context.md)).

### NO inferred stakes (legibility)
No LLM-inferred "stakes/priority" governing escalation. "Stakes" isn't a concept users
hold, and **behavior that differs on an invisible setting is unexplainable** — nobody
reasons "ah, lower stakes" when one reminder escalates and another doesn't. All
reminders behave the same. (Same rule as memory: no silent inference that changes
behavior.) Any differentiation must be *user-stated and legible*, never inferred.

### Ack ↔ escalation ↔ queue (one state machine)
`pending → deliver(k, content-free) → [pull/ack → done] | [timeout → escalate k+1,
still content-free] | [max → queued]`. **Snooze = ack + reschedule.** Universal for
user-facing deliveries; a **run-command** payload has nothing to pull → auto-completes.
- **Terminal is a QUEUE** — not silence (silent-miss = trust-killer) and not endless
  dinging. Timed-out-unacked reminders **and** missed-while-down occurrences (catch-up)
  funnel into the same queue.
- **Surfaced at interaction-start:** "you have 2 reminders" opens the next interaction;
  the user pulls them. Better than naive resume-dinging-after-reboot — batched,
  non-intrusive, still no silent miss.
- **A busy target is a state, not a failure.** `announce` raising `SatelliteBusyError`
  (the target is mid-interaction) is a **defer** transition — re-try shortly or fold the
  item into the queue — never a drop. Same "no silent miss" invariant as timeout.

### Deferred: off-satellite ack (phone push) — Phase 4, Voice-ID-gated
**Parked, not designed — recorded so the insight isn't lost when we get here.** The
Phase-3 floor delivers to **satellites** and acks via **pull-to-read** (the
`assist_satellite` path above). Delivering to a **phone** is a *higher targeting rung*
already marked ⚠️ in the ladder, and it's gated on a dependency chain:

> push to a phone → *which* phone → *which person* → **identity** → **Voice-ID (Phase 4)**.

Pre-Voice-ID, the only way there is the **device→owner config** (§5.1) — a one-time-setup
interim that maps the requesting device to an owner with a registered `mobile_app`; the
*zero-setup, recognize-who's-speaking* version needs Voice-ID. So phone push rides the
same Phase-4 timeline as Voice-ID, and there's no reason to design its mechanics now.

**The one insight to preserve: ack is channel-specific, but the state machine is one.**
On-satellite ack = pull-to-read (a *voice* interaction). Off-satellite ack ≠ that — a
phone uses HA's **`notify` + `mobile_app` actionable notifications**: the ack signal is an
**action-button callback** (and tag-based **clear/update** for dismiss/escalate), *not* a
spoken pull. So when Phase 4 arrives, the work is **routing a second ack ingress
(actionable-notification callbacks) into the *same* ack↔escalation↔queue machine** — not a
new subsystem. And, as with the satellite API (§2.8), **the primitive already exists**:
`notify` (`SERVICE_NOTIFY`, `ATTR_TARGET`, `ATTR_TAG`) + `mobile_app`
(`CLEAR_NOTIFICATION`, notification-action events) — wire it in, don't rebuild it.

### Per-feature presets (legible, not inferred)
The three VUI features are **three fixed profiles over the one delivery engine.** This
does **not** violate "no inferred stakes": the differentiation is which feature the user
**invoked** ("set an alarm" vs "remind me" vs "set a timer") — **user-stated and
legible**, the exact escape hatch the rule allows. Within a feature, every instance
behaves identically.

The organizing insight: **escalation strategy = where the person is assumed to be
relative to the target**, which the invoked feature encodes.

| Axis | **Timer** | **Alarm** | **Reminder** |
|---|---|---|---|
| Person vs. target | **at it** | **at it, asleep** | **unknown location** |
| Has content | label only | none (wake signal) | **yes** (the thing) |
| Default target (floor) | requesting device (bound) | requesting device (bedroom) | requesting device → climb ladder |
| Delivery | local repeating ding | **loud persistent wake tone** | **content-free announce + pull-to-read** |
| Escalation | **none** (stays local) | escalate **intensity** on target (louder/persistent); broadcast only as last resort | escalate **reach** — content-free **broadcast** (locate the person) |
| Ack | "stop" / dismiss | dismiss / **snooze** | **pull-to-read** = ack / snooze |
| Catch-up grace | **short** (drop stale) | **short** blare + surface "missed" informationally | **long/∞** (queue at interaction-start) |
| Recurrence | none | **common** (daily/weekday) | sometimes |
| Persistence | ephemeral today ([timer note](#timers-are-the-short-grace-corner-of-this-same-model-not-conceptually-ephemeral)) | **required** | **required** |

Why the escalation row differs is not stakes but **geometry**: timer → you're next to the
device, no need to escalate; alarm → you're at the device but unconscious, so escalate
*intensity* (broadcasting to the kitchen can't wake a sleeper); reminder → you could be
anywhere, so escalate *reach* by spreading the content-free ding. Alarm's intrusiveness is
**consented-to** (you asked to be woken), which is why loud-persistent is the right
register there but content-free-quiet is right for an unsolicited reminder.

### Build
Floor ships Phase 3: requesting-device + **content-free announce** + **pull-to-read
acks** + bounded content-free escalation + **queue surfaced at interaction-start**. No
inferred `stakes`/`privacy` fields. Targeting = a silent resolver `(requesting_device,
item, byproduct-signals) → target set`; presence (consume existing HA
`person`/`device_tracker`/occupancy, read-only), device-owner, Voice-ID, BT-beacons
slot in as higher-confidence inputs **without ever asking**. Ladder maps onto the
roadmap: **floor = Phase 3, presence = optional silent enrichment, personal/multi-
channel = post-Voice-ID Phase 4.** Magical is a later rung on the same structure.

---

## Triggering implementation

How the delivery engine actually decides *when* to fire, for durable +
user-editable + recurring items.

### Reference: HA's Calendar Trigger already does the hard part — reuse it
It's a **hybrid** (coarse re-fetch + precise alarm), not a per-second poll or a
bare `sleep()`:
- **Interval cursor** `[start, end)` advancing **adjacent + non-overlapping**
  (`Timespan.next_upcoming`, `calendar/trigger.py:119`).
- **Periodic refresh** (`async_track_time_interval`, `:233`) re-fetches the
  upcoming window into a queue → handles **edits**, **bounded lookahead**, and
  **recurrence** (RRULE expanded via `async_get_events`).
- **Point-in-time alarm** for the *next* queued event only
  (`async_track_point_in_time`, `:255`) → precise; only the coarse refresh polls.
- Dispatch drains all events `<= now` (`:276`); refresh flushes stragglers
  (`:290`).

### The gap it punts on (why we can't use it as-is for reminders)
On attach the cursor is set to `now` and only looks **forward**
(`calendar/trigger.py:226`): **missed-while-down events never fire, no persisted
watermark, no cross-restart dedup.** Fine for automations; fatal for reminders
("take your medication," missed because HA updated).

### What a durable engine adds
1. **Persisted per-item watermark** (`processed_through`) — *not* per-occurrence
   flags:
   - **No double-fire:** never fire an occurrence `<= watermark`; idempotent
     across re-ticks, edits, restarts.
   - **Bounded storage for infinite recurrence:** store RRULE + one cursor, not a
     boolean per day. (The "per-day did-it-fire flag" collapses into one monotonic
     cursor.)
2. **Startup catch-up with TWO knobs** (resolves the false-negative /
   false-positive tension):
   - **Grace (time filter):** drop occurrences older than *N*; skip-but-advance.
     Handles the multi-day, off-on-vacation outage.
   - **Collapse / don't-replay (recurrence filter):** a recurring item fires **at
     most the latest** in-grace occurrence, never the series. Handles a short
     outage spanning several occurrences.
   - Together they kill the "take your July 2nd dose; next, July 3rd…" machine-gun
     — grace for long outages, collapse for short ones.
3. **Watermark lives in *our* store, keyed by `(item/event-UID, occurrence)`** —
   never on the (external, sync-clobbered) calendar event. Past edits (moving an
   occurrence before the watermark) don't re-fire.

### Robustness: gap-free timeline coverage
The half-open adjacent interval means every instant belongs to **exactly one
span**, so one mechanism absorbs the whole "time is unreliable" family:
- **jitter** — a tick 5 s late; the next span sweeps up the stragglers.
- **downtime** — the persisted watermark extends coverage across the gap.
- **clock jumps / DST / NTP** — a forward leap is just a big span (filtered by
  grace); a backward jump leaves the watermark ahead of `now`, so nothing
  re-fires.

> **This section is a test spec.** Every property below (no-double-fire, no-silent-miss,
> grace, collapse, clock-jump/DST) is a deterministic case in the scheduling-substrate
> harness ([`evaluation.md`](evaluation.md) Part G) — driven by `freezer` +
> `async_fire_time_changed` + restore-cache, the same pattern the Calendar Trigger tests
> already use. Highest-trust-stakes code → exhaustive coverage.

### Ordering replaces atomicity
Doesn't need true atomicity. **Persist the watermark *last*, after delivery is
dispatched.** The only crash window then produces a **redundant re-fire**
(annoying), never a **silent miss** (dangerous). Prefer at-least-once + the
watermark's dedup.

### Catch-up ≠ re-execute (safety)
A caught-up occurrence is an **informational** delivery ("you missed your 8 PM
dose"), **not** a re-issued action ("take it now"). Replaying a missed dose — or a
`conversation_command` payload — as an *action* can be harmful. Encode it:
catch-up fires *inform*; they do not re-run payloads/commands.

### Poll vs. alarm
The hybrid (reuse Calendar Trigger) is preferred and already written; a 1 s poll
is a valid simpler alternative at HA scale. **The watermark is the load-bearing
correctness piece regardless of cadence** — polling frequency only affects
latency, not correctness.

---

## What this means to build

- **New:** the delivery engine (announce/notify/command + snooze/ack) — with a
  **silent degrade-to-floor targeting resolver**, **content-free announce + pull-to-read
  acks**, **bounded content-free escalation → queue-at-interaction-start**, and no
  inferred stakes/privacy (see Delivery section); the native
  reminder/alarm store implemented as a `CalendarEntity` (Store + `ical`); and the
  **durable trigger layer** (persisted watermark + two-knob catch-up) over the
  calendar-trigger machinery.
- **Reuse:** Calendar Trigger (fire on event start ± offset), `create_event`
  (explicit calendar path), `ical` (RRULE + ICS persistence), and the
  **`assist_satellite` service API** (`announce` + `preannounce` earcon for the
  content-free ambient case; `start_conversation` for solicited/proactive) as the
  satellite output primitive — the timer `register_handler` pattern is the lower-level
  fallback only.
- **Keep native recurrence simple** (daily/weekly/interval covers most voice
  cases); punt complex RRULE / sharing to the real calendar.
- **VUI stays separate** (timer/alarm/reminder distinct features), substrate
  shared.

### Related docs
- [`timers.md`](timers.md) *(planned)* — core's countdown/command timer (ephemeral
  *today*, by inheritance from core, not conceptually); the short-grace corner of this
  model. Thin, references the timer note above.
- [`reminders.md`](reminders.md) *(planned)* — durable reminders/alarms as the
  primary consumer of this model.
- [`ambient-noise.md`](ambient-noise.md) — sleep-timer is a delivery-engine
  consumer ("play rain 30 min" = scheduled stop).
- [`conversation-loop.md`](conversation-loop.md) — deviceless `conversation_command`
  timer is the existing "scheduled command" precedent.
- [`offline.md`](offline.md) — the deterministic runtime here is **LLM-free**, so
  scheduled items **fire with the cloud unreachable** ("runtime-offline"). Distinct from
  the **HA-was-down** catch-up above: there HA is off; there the cloud is off but HA runs.

---

## Key references

- `intent/timers.py:460,832` — timer `conversation_command` payload + fire
- `assist_satellite/entity.py:199,250,333` — `announce` / `start_conversation` /
  `ask_question` (delivery output primitive; `SatelliteBusyError` at `:231,289`)
- `assist_satellite/__init__.py:72,89,107` — the three services; `PREANNOUNCE_URL` earcon
- `calendar/trigger.py` — Calendar Trigger (fire on event start/end ± offset)
- `calendar/__init__.py:195,224` — RRULE validation; `create_event`
- `local_calendar` (`ical==14.0.1`, `.storage/*.ics`) — persistence + recurrence
- `todo/__init__.py` — due-date fields; `todo/trigger.py` — list-mutation trigger
- `helpers/event.py:1420` — `async_track_point_in_time` (ephemeral)
