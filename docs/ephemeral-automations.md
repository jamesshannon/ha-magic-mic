# Ephemeral (LLM-Authored) Automations

> Distinct FR, timer-adjacent but separate. Transient, LLM-authored
> `{trigger, condition, action}` rules: "remind me in 5 minutes **if** the door is
> still open," "tell me **when** the laundry's done," "turn off the heater in
> 30 min **unless** someone's in the garage." Builds on
> [`scheduling-model.md`](scheduling-model.md) (shares delivery + the time-trigger
> machinery); this doc is the conditional/event-driven layer on top.

---

## TL;DR

- A category of its own: transient `{trigger, condition, action}` rules the LLM
  authors from natural language — structurally *automations*, but ephemeral and
  voice-created.
- **LLM authors at *creation*** (parse NL → structured rule, resolve entities via
  `find_entities`); **deterministic evaluation at *fire*.** Not LLM-at-fire.
- **The real justification is capability, not cost/determinism.** State triggers
  ("when X happens") unlock event-driven rules that a timer + LLM-eval
  *fundamentally cannot do*. That — not efficiency — is why you build the
  structured engine.
- **Reuse HA's engine** (`async_initialize_triggers` + `condition.async_from_config`),
  **not automation entities.**
- **Two trigger backends, one `condition → delivery` pipeline:** *time* (our
  watermark/catch-up) + *state/event* (HA's trigger engine). The watermark is
  **time-only** and does not apply to state triggers.
- **LLM-at-fire survives only as the fallback** for genuinely fuzzy,
  non-compilable conditions.
- **This split is also the offline story.** Compile-at-creation → the rule **fires with
  no model at runtime**, so it works with the cloud unreachable; it's the archetype of
  [`offline.md`](offline.md)'s Layer 2 (LLM-at-fire is, by contrast, offline-fragile).
- **Ephemeral overrides ("lights to 100% for 15 minutes") are this engine, not a new
  feature.** An override is `snapshot → apply now → ephemeral automation that reverts the
  snapshot on a boundary trigger`. The boundary is just a trigger (time for "15 minutes,"
  sun/state for "until sunset" / "until I leave"), so it composes from the machinery here +
  [`undo.md`](undo.md)'s scene snapshot. No dedicated path in v1; see the worked case below.
- **Authoring is the v1 [`SKILL`](skills.md) consumer.** The `{trigger, condition, action}`
  grammar + entity-id discipline + examples the LLM needs to *author* a rule are too big for
  the base prompt and needed only rarely — and there's **no deterministic gate** ("start the
  dryer when the laundry's done" is an automation vs. reminder vs. one-shot is semantic). So
  it's the LLM-signaled class: a resident ~25-tok header, model pulls the body via
  `read_file` (skill-sandboxed). The pull costs one generation, but authoring is already multi-gen/deliberate,
  and **compile-once/run-deterministic amortizes it to zero at fire** — you pay to *build*
  the rule, never to run it. This is the single case that earns the SKILL registry in v1.

---

## The category (why it's a separate FR)

All of these are `{trigger, condition, action}` triples — i.e. automations — but
transient and voice-authored:

| Type | Example | Trigger | Cond | Action |
|---|---|---|---|---|
| Plain reminder | "remind me at 8" | time | — | notify |
| **Timed + condition** | "in 5 min if the door's still open" | time | state | notify |
| **State-triggered** | "tell me when the laundry's done" | state | — | notify |
| Timed + action (exists) | "lights off in 5 min" | time | — | command |
| State + action | "turn off the fan when I leave" | state | state | command |
| **Ephemeral override** | "lights to 100% for 15 min" | time *or* state | — | snapshot + apply now, revert on trigger |

Plain reminders/alarms live in `scheduling-model.md`; this doc owns the
**conditional and state-triggered** rows. It's a distinct FR because the *shape*
(a condition gate, a non-time trigger) differs enough that folding it into
"reminders" would muddle both.

---

## Author at creation, evaluate at fire — not LLM-at-fire

- **At creation**, the LLM parses "…if the door is still open" into a structured
  `{trigger, condition, action}`, resolving "the door" → `binary_sensor.front_door`
  via `find_entities`. Same NL→spec normalization as a reminder, plus a condition.
- **At fire**, evaluate **deterministically** with HA's own helpers (below). No
  LLM inference at fire time.

### On the cost/determinism argument (an honest concession)
Running the LLM *at fire* to do the if/then is tempting and **not as bad as it
first looks**: there's an LLM call at request time either way, so compile-at-
creation just makes that call do more (a harder parse) and skips the fire call.
For a **one-shot** conditional it's ≈ one harder call vs. two easier calls — a
wash. Two asymmetries remain, but they're narrow:
- **Recurring** tips it: compile = 1 call total; LLM-at-fire = 1 + N (per
  occurrence).
- **Where the non-determinism lands** is the durable difference: compile-at-
  creation resolves the fallible LLM judgment *while the user is present and can
  confirm* ("remind you if the **front** door is open") and is **reproducible**;
  LLM-at-fire puts that judgment in an **unattended** moment (silent misfire,
  non-reproducible).

So for *time*-triggered conditionals it's a genuine tradeoff, not a slam dunk —
which is why LLM-at-fire keeps a (narrow) home.

---

## Why build the structured engine: state triggers unlock a category

The decisive argument isn't cost. **"Notify me when the laundry's done" cannot be
done with timer + LLM-eval at all** — it's event-driven; there's no time to fire
an eval, and polling an LLM every N seconds is absurd. State-triggered rules exist
**only** via the structured approach (`async_track_state_change_event` / trigger
helpers).

So: build the structured `{trigger, condition, action}` engine **for what it
enables** (event-driven rules). Once it exists, time-conditionals ("if the door's
still open") ride along *deterministically for free* — you take them that way not
because you must, but because you already have the machinery.

---

## Reuse HA's engine, not automation entities

Structurally these are automations — so reuse the **helpers**, not the automation
**entity**:

- **Trigger** → `async_initialize_triggers` (`helpers/trigger.py:1824`) *is* the
  standard automation trigger engine, callable programmatically. It takes the same
  trigger config dicts as an automation's `trigger:` block (state, numeric_state,
  template, event, …) and runs your callback on fire. You are **not recreating
  state triggers** — you invoke the exact engine automations use, without a
  visible automation. (`async_track_state_change_event`, `helpers/event.py:304`,
  is the lower-level primitive.)
- **Condition** → `condition.async_from_config` (`helpers/condition.py:1330`)
  compiles a condition dict into a `ConditionCheckerType` callable evaluated
  against live state. Deterministic; covers state/numeric/template/time.
- **Action / delivery** → the `scheduling-model.md` delivery engine
  (announce/notify/command + snooze/ack).

**Not** auto-generated automation config entries: they clutter the user's
automation list (the surprise problem), dynamic create/delete is a heavyweight
lifecycle the subsystem isn't meant for, **and it still wouldn't give you time
catch-up.** So: the automation engine's *guts* over our own durable store.

---

## Two trigger backends, one pipeline

The engine has two trigger backends feeding one shared `condition → delivery`
pipeline. They need **different** infrastructure — do not unify them under the
watermark:

| | Time trigger | State / event trigger |
|---|---|---|
| Backend | Calendar-trigger + **watermark + catch-up** (ours) | `async_initialize_triggers` (HA's engine) |
| Why ours vs. theirs | **No** HA triggering fires for instants passed while down — automations, calendar trigger (`trigger.py:226`), and `async_initialize_triggers` all reset to `now` | HA fully provides event triggering |
| Missed-while-down | watermark + grace/collapse (see spine) | event is **lost**; at best a **one-time boot reconciliation** ("is the condition already satisfied on startup, and wasn't at creation?") — **not** a watermark |
| Recurrence | RRULE + watermark cursor | N/A (fires per event) |

The watermark/catch-up machinery is **time-specific**. State triggers are
event-driven callbacks — nothing to "catch up" in that sense.

---

## Lifecycle

- **Durable store** of `{trigger, condition, action}` specs (survives restart —
  same requirement as reminders).
- **Re-arm on startup**: time via the scheduling layer (watermark); state via
  `async_initialize_triggers` from the stored config.
- **One-shot self-remove** after firing.
- **State-triggered items need an expiry/timeout** — "tell me when the door
  closes" shouldn't linger for a month.
- **Boot reconciliation** for state rules whose condition became true during
  downtime (single current-state check, not a timeline replay).

---

## Worked case: ephemeral overrides ("100% for 15 minutes")

"Turn the lights to 100% for the next 15 minutes" (or "until sunset," or "until I leave")
is a **temporary override**. It's worth writing down because it *looks* like it wants its own
subsystem and doesn't: it's this engine plus a state snapshot, composed.

**Construction.** Three steps, all deterministic once authored:

1. **Snapshot** the target entities. `scene.create` with `snapshot_entities` captures the
   current state (all attributes) into a `from_service` scene at `scene.<id>`
   (`homeassistant/scene.py:271`). This is the same capture [`undo.md`](undo.md) uses.
2. **Apply** the override now (lights → 100%).
3. **Author a one-shot ephemeral automation** whose trigger is the boundary and whose action
   re-applies the snapshot: `{trigger: +15min OR sunset OR "I leave", action: scene.apply(scene.<id>) → scene.delete(scene.<id>)}`
   (`scene.py:231` apply, `:302` delete). The boundary is just a trigger, which is why this is
   a thin layer on the engine above: "for 15 minutes" is a time trigger, "until sunset" a sun
   trigger, "until I leave" a presence trigger. Same two backends, same pipeline.

Because the snapshot is baked into the compiled automation as a literal service call, the
revert is **compile-once/run-deterministic**: it fires with no model in the loop, so an
offline window can't strand the lights at 100% ([`offline.md`](offline.md) L2).

**Revert is literal and unconditional by default.** "For 15 minutes" means: restore the
captured state at t+15, whatever happened in between. Do **not** add a silent "unless the
world moved on" check. That would be invisible differential behavior the user never asked
for, exactly what `scheduling-model.md` rejects. If they want a guard they say so ("…unless
they're no longer at 15%"), and it lands in the `condition` slot of `{trigger, condition,
action}` where it's legible and user-authored. The default stays dumb, literal, predictable.

**The fragile mechanics belong in the authoring primitive, not in SKILL prose.** This is
determinism-in-tools one level down: the [`SKILL`](skills.md) teaches the *pattern and the
judgment* (recognize "do X for a while" → snapshot, apply, author a reverting one-shot); the
deterministic authoring step **guarantees** the parts an LLM hand-rolling YAML gets wrong
intermittently:

- **Ordering.** Snapshot *before* the override applies, or the scene captures 100% and the
  revert restores 100%. A silent, plausible-looking failure.
- **Scene lifecycle.** Deterministic `scene_id`; `scene.delete` on revert **and** on early
  cancel ("never mind, put them back now"), or orphan `scene.*` entities accumulate.
- **Restart durability.** `from_service` scenes are in-memory only (not written to
  `scenes.yaml`), so an HA restart mid-window loses the snapshot and the revert has nothing
  to apply. Either persist the captured state as data alongside the spec, or treat
  restart-during-window as a **bounded gap**, timer-style (`scheduling-model.md`
  short-grace). A substrate policy call, not per-request LLM reasoning.
- **Entity set.** Snapshot exactly the overridden entities (the `find_entities` set), not
  the whole room.

**No dedicated fast path in v1 — gate it on observability.** The general authoring path *is*
the override path; the SKILL teaching this pattern is all that's needed. Don't pre-build a
tailored "override" tool on a guess. Instead, **track ephemeral-automation creations by type**
(a column on the `evaluation.md` scorecard); if this snapshot-and-revert shape ever exceeds
~15% of creations, a lit path can be justified by data. A dedicated path only ever buys a
faster, more tailored version of a thing that already works; **correctness lives in the
mechanics above regardless of which path authors the rule.**

**Explainability sees two edges, not a span.** Each boundary of the override is an
independently attributable state change: "the assistant set it to 100% at 8:00 (at your
request)" and, at 8:15, "the assistant reverted an override." Both are normal
[`explainability.md`](explainability.md) cases (assistant-caused, top of the gradient). The
*pending* reversion is not a logged state change, so it isn't explainability's to report, and
we deliberately don't build a first-class "temporary state" object, so there's no span to
describe. "How much longer?" is a **schedule-introspection** query over pending triggers
(future), distinct from explainability (past); it's a substrate capability, not an
override-specific one, and only as good as the substrate's next-fire-time surface
(`scheduling-model.md`'s timer-runtime seam).

---

## The fuzzy-condition fallback (where LLM-at-fire survives)

Some conditions don't compile to a structured predicate — "if it **looks like
rain**," "if the room's still **messy**." Only *those* justify an LLM eval at
fire, accepting the unattended non-determinism because there's no structural
alternative.

**Rule of thumb:** compile to a structured condition whenever possible (the door
case is trivial); LLM-at-fire only when the condition genuinely can't be expressed
structurally.

---

## Related docs & references

- [`scheduling-model.md`](scheduling-model.md) — shared delivery engine +
  time-trigger (watermark) machinery; the ontology this sits within.
- [`find-entities.md`](find-entities.md) *(planned)* — entity resolution used to
  compile conditions.
- `helpers/trigger.py:1824` — `async_initialize_triggers` (programmatic automation
  triggers)
- `helpers/condition.py:1330` — `condition.async_from_config` (condition checker)
- `helpers/event.py:304` — `async_track_state_change_event`
- `calendar/trigger.py:226` — resets to `now` (no time catch-up → watermark is ours)
- [`undo.md`](undo.md) — the scene-snapshot capture reused by ephemeral overrides.
- [`explainability.md`](explainability.md) — why an override reads as two attributable edges,
  not a span.
- `homeassistant/scene.py:271/231/302` — `scene.create` (snapshot) / `apply` (revert) /
  `delete` (cleanup); `from_service` scenes are in-memory, not persisted to `scenes.yaml`.
