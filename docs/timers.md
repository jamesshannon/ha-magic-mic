# Timers by Voice

> **Thin doc.** Countdown timers **already work by voice in HA today**; the design
> decisions live in [`scheduling-model.md`](scheduling-model.md) (the ontology, the
> [per-feature presets](scheduling-model.md#per-feature-presets-legible-not-inferred),
> and the ["short-grace corner" timer note](scheduling-model.md#timers-are-the-short-grace-corner-of-this-same-model-not-conceptually-ephemeral)).
> This doc just records the verified state + the boundary with the delivery engine.

---

## TL;DR

- **Base timers ship and are LLM-exposed today** — full intent set, gated on the device
  supporting timers. Nothing to build for v1.
- Timers are **ephemeral in core today** (`async_track_point_in_time`, in-memory,
  device-local) — a property **inherited from core's implementation, not a conceptual
  truth** (see the spine's timer note). They're the **short-catch-up-grace corner** of
  the one scheduling model.
- **Deliberate non-goals for v1:** durable persistence and cross-device follow. A
  **durable-backed timer** (persist the record, keep in-memory firing, short grace) is a
  cheap future migration, not a rebuild.

---

## What works today (verified)

| Capability | Status | Where |
|---|---|---|
| Start / cancel timer | ✅ intent | `intent/timers.py`, `TIMER_INTENTS` |
| Pause / unpause | ✅ intent | `intent/timers.py` |
| Increase / decrease | ✅ intent | `intent/timers.py` |
| "How much time left?" (status) | ✅ intent | `intent/timers.py` |
| Multiple + **named** timers ("pasta timer") | ✅ | `intent/timers.py` |
| LLM exposure | ✅ **gated on device supporting timers** | `intent/llm.py:31` (`TIMER_INTENTS`), `async_device_supports_timers` |
| Firing | device-local ding **or** `conversation_command` payload | `intent/timers.py:460,832` |
| Persistence | ❌ **ephemeral** (in-memory) | `helpers/event.py:1420` |

The deviceless `conversation_command` timer is the existing **"scheduled command"**
precedent that [`conversation-loop.md`](conversation-loop.md) and the delivery engine
build on.

---

## Preset (per the spine)

Timer = the profile `{target = requesting device (bound), delivery = local repeating
ding, escalation = none (stays local), ack = "stop"/dismiss, catch-up grace = short,
recurrence = none, persistence = ephemeral-today}`. The distinguishing choices:

- **Escalation = none.** A pizza timer must **not** ding every room — you're next to the
  device you set it on. (Contrast alarm = escalate intensity; reminder = escalate reach.)
- **Catch-up grace = short.** "Your 10-minute pasta timer!" three hours post-reboot is
  *wrong*, not helpful — drop a stale timer rather than fire it late. This short grace is
  the *only* thing that conceptually distinguishes a timer from a reminder in the model.

---

## Boundary with the delivery engine

Timers **could** become a delivery-engine consumer (they're an expressible point in the
model), but v1 leaves them on core's shipped path because (1) they already work + are
VUI-integrated and (2) the live-countdown / second-precision / pause-resume runtime is
timer-specific and core already owns it. The clean future split is **persist the record,
keep in-memory firing** — not moving timers onto the durable trigger loop.

**Sleep-timer is a *different* consumer:** "play rain for 30 minutes" is a scheduled
**stop action**, handled in [`ambient-noise.md`](ambient-noise.md), not a countdown-timer
intent.

---

## Residual (low priority)

- Optional: back the core timer with lazy persistence for restart-survival of long
  in-flight timers (the "durable-backed timer" migration) — only if the payoff appears.
- Everything else (start/stop/pause/adjust/status/named/multiple) is **done**.
