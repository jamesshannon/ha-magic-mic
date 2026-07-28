# Explainability

> "Why is my thermostat at 67 when I set it to 65 this morning?" "Why did the hallway
> light just turn on?" This is a trust-and-debugging capability: tell the user what
> *caused* a state they didn't expect. It gets confused with state reversal ("turn back on
> whatever I just turned off"), which is a different problem and out of scope; the last
> section says why. Grounded in released HA (`homeassistant==2026.7.4`, read from
> `.venv/.../site-packages/homeassistant/`). Related: [`undo.md`](undo.md),
> [`prompt-context.md`](prompt-context.md), [`security.md`](security.md); `PRODUCT_PLAN.md`
> §5.4 (determinism-in-tools) and §6.2 (integration topology).

---

## TL;DR

- HA can answer "why" and the Alexa/Google class can't, for a concrete reason: HA keeps a
  causal graph of your home. Every `State` carries a `Context(user_id, parent_id, id)`
  (`core.py:1246`), and the logbook already turns those contexts into readable "triggered
  by X" lines (`ContextAugmenter`, `logbook/processor.py`). We wrap data HA already stores.
- The tool does the lookup; the model only reads out the result. It queries the logbook and
  recorder history for the entity, resolves the context chain to a cause, and returns a
  structured record. When there's no recorded cause it returns `unattributable`, and the
  prompt treats "there isn't enough in the logs to say why" as a valid answer. That single
  rule is what keeps the model from inventing a reason. "Why is it at 67" invites a
  confident fabrication, and a feature that fabricates causes destroys the trust it exists
  to build.
- Two of the three layers touch neither voice nor the model. Retrieval and cause-resolution
  are ordinary `hass`-and-recorder code with their own consumers: a dashboard card, a
  notification, a REST call. Only the narration layer needs a language model. That makes
  explainability a good §6.2 split-JIT candidate.
- Reversal from logs is impossible, and not for the reason it first looks. The prior value
  *is* in the recorder, so missing data isn't the blocker. Attribution is often partial,
  and writing an old value back fights whatever is still causing the new one. Only
  assistant-caused reversal survives, and [`undo.md`](undo.md) already covers that.
- Low frequency, high trust. People ask "why" right when they're about to stop trusting the
  system, so a grounded answer pays off out of proportion to how often it runs. Build in
  Wave 3-4; it needs the recorder. Nothing in core does this today.

---

## Two stores, not one

The worry that "the logs might not have enough" is worth taking seriously, but it points at
the wrong axis. Explainability draws on two separate stores, and it needs both.

The logbook answers *what and who*. `ContextAugmenter` (`components/logbook/processor.py`)
takes a state change's context and resolves it to a named cause, filling in `CONTEXT_NAME`,
`CONTEXT_SOURCE`, `CONTEXT_EVENT_TYPE`, `CONTEXT_DOMAIN`, `CONTEXT_ENTITY_ID`, and
`CONTEXT_USER_ID`, with an `EntityNameCache` mapping ids to friendly names. "The Hallway
Motion automation turned it on" is a line HA already writes.

The recorder answers *what the values were*. `get_significant_states` and
`state_changes_during_period` (`components/recorder/history/__init__.py:124/454`) return
full `State` objects. Two defaults carry the weight: `include_start_time_state=True` hands
back the state at the start of the window, which is the prior value, and `no_attributes=False`
keeps attributes. So "brightness was 80% at 3:00, then 12% at 3:05" is fully recoverable,
dimmer level included.

The values are precise and available. What goes missing sometimes is the cause, and that
gap is the whole design. The gradient below handles it. It isn't a reason the feature can't
work.

## Three layers

The user's framing was "a separate concept Assist can call, one that doesn't need voice."
Split out, there are three layers, and only the top one touches the model or a microphone.

1. Retrieval and cause-resolution. Deterministic, no model, no mic. Walk the context chain
   (`context.parent_id` points at the cause) and pull the recorder timeline, then emit one
   record: entity, time, prior and new value, cause (an automation, a user, a script, or
   `unattributable`), and a confidence. Plain `hass`-and-recorder code, with consumers that
   never involve Assist: a "why did this happen?" button on a card, a notification action,
   a REST endpoint.
2. Narration. The model turns that record into a spoken sentence. This is the only sense in
   which the feature needs Assist: it needs something to read the record, not something to
   hear the question. A UI could render the same record as a chain with no model at all.
3. Voice. One delivery surface over layer 2, registering the tool through
   `llm.async_register_api` like any other provider.

Because layer 1 stands on its own and already has non-voice consumers, explainability is a
strong candidate to start inside Magic Mic and graduate to its own provider integration
once a second consumer earns the boundary (§6.2).

## The attribution gradient

How honest the tool can be depends on where the change came from. `unattributable` is a real
answer, not a failure.

| Source of the change | What HA knows | Spoken answer |
|---|---|---|
| The assistant itself | Exact: [`undo.md`](undo.md)'s journal recorded the action and its reason | "I set it, when you asked me to at 8:04." |
| An automation, script, or user action in HA | The context chain names it (`CONTEXT_NAME` / `CONTEXT_SOURCE`) | "Your Away Mode automation set it to 67 at 3pm." |
| The device, its cloud, or a physical control | Nothing: the value changed with no HA context attached | "It went to 67 at 3pm, but nothing's recorded about why. Probably the thermostat's own schedule." |

The tool walks down this gradient and reports where it landed. `unattributable` is a value
the code returns, not a story the model tells, which is what keeps the narration honest.

## Why reversal is out of scope

Split "turn back on whatever I just turned off" into two cases.

If the assistant made the change, keep it. That's [`undo.md`](undo.md)'s journal, which
records each action's inverse as it runs: bounded, deterministic, cheap.

If anything else made the change and you're reconstructing it from logs, it can't be done,
and the reason isn't the obvious one. The prior value sits in the recorder; missing data
isn't the problem. Two other things are.

Attribution is often partial. A flipped wall switch leaves no context at all. One observed
change can be the sum of a switch, an automation, and a trigger. Often you can't even say
what to reverse.

And reversal argues with a live cause. Suppose you do know the light was at 80%. Writing 80%
back doesn't restore the past, it fights the present: the physical dimmer now sits at 12%,
so the hardware and the digital state disagree, or the automation that dimmed it fires again
and undoes your undo. You aren't reversing an event, you're wrestling a cause you don't
control, and the operation has no clean meaning.

Put this in scope explicitly, so nobody reopens it expecting better logging to fix it.
Better logging wouldn't.

## Build notes

One tool, `explain_state`. It takes an entity reference (resolved with
[`find-entities.md`](find-entities.md)'s scorer, another consumer of it) and an optional
time hint like "just now" or "this morning" (normalized the §5.4 way). It returns the
causal record; the model reads it out. `unattributable` comes back as an ordinary result,
not an error.

The acceptance test is confabulation, and it belongs in [`evaluation.md`](evaluation.md).
The golden set has to include changes with no HA context, and passing means the assistant
says it doesn't know rather than guessing. That's the one path where this feature makes
things worse instead of better.

Two generations are baked in: fetch, then narrate. Fine for an informational query that
never sits on the hot path, same as forecast.

One accountability wrinkle, covered in [`security.md`](security.md): answering "why" reads
history and `CONTEXT_USER_ID`, so it can surface who did something. In a shared house, scope
answers to the caller's exposed entities, and decide whether to name the user or only say
that a user acted.

The retrieval-and-resolution half is a clean helper over logbook and recorder that doesn't
depend on voice or a particular model. That makes it plausible to contribute upstream, where
it would help core's own Assist, in the same merge-first category as the eval and trace
harness (§7). The narration stays in the component.
