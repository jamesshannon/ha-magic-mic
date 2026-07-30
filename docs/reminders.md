# Reminders & Alarms by Voice

> Feature doc over the spine. Reminders and alarms are the **primary consumers of the
> durable delivery engine** defined in [`scheduling-model.md`](scheduling-model.md);
> this doc is thin: it records the two **presets**, the **create/write tool shape**, and
> the **recurrence** surface. All the architecture (durable store, trigger + watermark +
> catch-up, targeting resolver, content-free announce + pull-to-read acks, escalation →
> queue) lives in the spine. See also [`calendar.md`](calendar.md) (the create flow meets
> `create_event`) and [`todo.md`](todo.md) (firing = reminder, passive list = todo).

---

## TL;DR

- **Reminders and alarms share everything** — the durable substrate, the trigger layer,
  the ack/escalation/queue state machine. They differ only by **preset** (a
  user-invoked, legible profile — *not* inferred stakes):
  [the spine's per-feature table](scheduling-model.md#per-feature-presets-legible-not-inferred).
- **Both require persistence** (unlike timers) — a silently-dropped "take your
  medication" reminder or an overnight-update-eaten wake alarm destroys trust.
- **Build = one create/write tool** that normalizes the time, picks the store, and
  names it back; recurrence stays **simple** (one-shot / daily / weekly / interval).
- **A reminder is the degenerate field-filling of the ephemeral-automation shape**, not a
  separate authoring mechanism ([`ephemeral-automations.md`](ephemeral-automations.md)):
  `{trigger: time, condition: —, body: deliver(content)}`. `create_reminder` is a
  **legible front-door** over that one primitive (the common time→notify case), so the LLM
  never chooses "reminder vs. automation" — it fills fields. Delivery richness lives in the
  **body's `deliver` call** (announce/ack/escalate/snooze), keeping reminders rich without a
  bespoke action surface.
- **"Broadcast/intercom"** ("tell everyone dinner's ready") is a thin consumer of the
  same content-free announce — noted below, not a separate build.

---

## The two presets (per the spine)

Read off the person-vs-target geometry — the invoked feature encodes it legibly:

- **Reminder** — has **content**, person could be **anywhere** → **content-free announce
  + pull-to-read**, escalate **reach** (content-free broadcast to locate the person),
  **long/∞** catch-up grace (missed ones surface in the queue at interaction-start).
- **Alarm** — a **wake signal** with no content, person is **at the target but asleep** →
  **loud persistent tone**, escalate **intensity** on the target (broadcasting elsewhere
  can't wake a sleeper), **snooze is first-class**, **short** blare grace (don't blare a
  7 AM alarm at 10; surface "your alarm didn't fire" informationally instead).

Everything else — durable store, trigger/watermark/catch-up, the ack↔escalation↔queue
machine — is **identical and inherited** from the spine.

---

## The create / write tool

One tool, `create_reminder` (alarm = the same tool with the alarm preset — the LLM maps
"set an alarm for 7" → preset=alarm). **Determinism-in-tools** (§5.4): the LLM supplies
intent + natural language; the tool does the mechanical work.

- **LLM supplies:** the content (for reminders), a **natural time** ("in 20 minutes",
  "tomorrow at 9", "every weekday at 7"), and the implied **preset** (reminder vs alarm,
  from the phrasing).
- **Tool does deterministically:**
  1. **Normalize the time** → absolute datetime (or recurrence rule) via `dt_util` + the
     device/home timezone — *not* the model doing date math (same as `calendar.md`).
     **Localization boundary (§5.7):** the multilingual model resolves the natural-language
     phrase to structured form; the tool does only the deterministic tz/calendar math — it
     doesn't parse NL dates per-language.
  2. **Pick the store** by the user's visibility/sync intent (the spine's surprise
     principle): default = **native reminder store** (a `CalendarEntity` we own); if the
     user signals a shared/visible calendar ("put it on my work calendar"), route to
     `create_event` there. `ScheduledItem.store` is the seam — **one create flow writes
     native-or-real**, so this and calendar-write are **not separate builds**.
  3. **Apply the preset** (target floor + escalation profile + catch-up grace).
  4. **Name it back** (surprise principle): "Okay — I'll remind you to take the bins out
     tomorrow at 8 AM."

Generation cost: like any tool command, ≥2 generations (create → confirm); fine for a
non-urgent authoring action. Because the write is **behavioral** (it *will* interrupt
you later), lean **confirm-by-naming-back** rather than silent (mirrors the
memory/find-entities "behavioral write → confirm" stance).

---

## Recurrence (keep it simple)

Native recurrence covers the voice-common cases only — **one-shot**, **daily**,
**weekly** (incl. "weekdays"), **fixed interval** ("every 4 hours"). The LLM maps
phrasing → `{one_shot | daily | weekly(days) | interval(n)}`; the tool validates and
stores it (RRULE via `ical` where the store is a real calendar). **Punt complex RRULE**
(“every 2nd Tuesday”, end-dates, exceptions) and **sharing** to the real calendar path —
don't reimplement a calendar in the native store.

---

## Broadcast / intercom (thin consumer, noted for completeness)

"Tell everyone dinner's ready" / "announce bedtime" is **not** a scheduled item at all —
it's an **immediate** delivery. But it rides the *same* content-bearing announce
primitive the delivery engine already has (here the content *is* the message, delivered
now, to all/selected satellites). So it's a **thin consumer**, not a new subsystem: a
`broadcast` tool → delivery-engine announce with `when = now`, `target = all/named
areas`, `content = the message`. No ack/escalation/queue needed (fire-and-forget). Flag
only; not required for the reminders/alarms build.

## Delivery mechanism (per the spine)

Both presets emit through **`assist_satellite`'s service API**, not a bespoke handler
(see the spine's [satellite output primitive](scheduling-model.md#the-satellite-output-primitive-assist_satellite-not-register_handler)):
- **Reminder / broadcast (unsolicited)** → `announce(message, preannounce=True)` — the
  `preannounce` earcon is the "⟨ding⟩", and it **doesn't open the mic**, keeping the
  reminder ambient/ignorable. The "read it" pull is a *separate, later, user-initiated*
  turn — not an immediate mic-open.
- **Solicited / "read it now" / proactive nudge** → `start_conversation` (announce +
  open mic + seed the session). Requires the LLM agent — so the engaging path is
  cloud-only; the ambient `announce` still works offline-adjacent.
- **Alarm** → `announce` with an alarm `preannounce_media_id` / loop media, escalating
  *intensity* on the target (not the conversational path).

---

## Open items

- **Preset values to tune on real use / evals:** escalation timeouts + max rungs per
  preset; alarm blare volume/pattern; the reminder broadcast fan-out order.
- **Snooze defaults** (alarm = N minutes; reminder = "remind me again in…" parse).
- **Native store schema** — the `CalendarEntity` + `Store` + `ical` wiring
  (implementation-level; meets `calendar.md`'s create surface).
- **Alarm "missed" informational catch-up** wording/threshold (short blare-grace vs.
  surfacing "your 7 AM alarm didn't fire").

---

## Key references (ha-core)

- `homeassistant/components/calendar/__init__.py:224` — `create_event` (the real-calendar
  store path); `CalendarEntityFeature.CREATE_EVENT` gate.
- `homeassistant/components/calendar/trigger.py` — fire-on-event machinery the durable
  trigger reuses.
- `intent/timers.py:460,832` — the `conversation_command` delivery precedent.
- Design spine: [`scheduling-model.md`](scheduling-model.md) (delivery, presets, durable
  trigger, catch-up).
