# Magic Mic

**An LLM-backed voice assistant for Home Assistant that actually assists — and feels like
magic, built like clockwork.**

Smart speakers turn your lights on and off; Home Assistant already does that well. Magic
Mic adds the layer that makes something feel like a real assistant: reminders that find you
in whatever room you're in, a conditional automation compiled from a single sentence,
household facts it remembers, a system that learns how *you* talk, and a conversation that
flows instead of a one-shot command — all multi-user and local-first-friendly.

👉 **New here? Read [`VISION.md`](VISION.md)** — the short, example-driven tour of what this
feels like and why it's different.

---

## Status

**Early scaffold — installable, but it does nothing yet.** The design phase is complete (a
full architecture + deep-dive docs); the code is just beginning. What's in the repo today is
a **placeholder integration that installs via HACS and loads cleanly but provides no
capabilities.** Install it now to follow along and receive features automatically as they land.

If you're evaluating the *ideas*, everything is in the docs below. If you want to **install it
today** (and get updates as they're committed), see **[Install](#install-hacs)**.

The end state is a Home Assistant **custom component** (on cloud Claude to start), with every
capability shaped so it can graduate into Home Assistant **core** and reach everyone. The
shell is disposable on purpose; the capabilities are the point.

---

## Install (HACS)

> **Heads-up:** this currently installs a *placeholder that does nothing.* It exists so you
> can add it once and receive capabilities automatically as they're released — and so early
> testers can follow along.

1. In Home Assistant, open **HACS**.
2. Top-right **⋮ → Custom repositories**.
3. Repository `https://github.com/jamesshannon/ha-magic-mic`, type **Integration** → **Add**.
4. Find **Magic Mic** in HACS, **Download**, then **restart** Home Assistant.
5. **Settings → Devices & Services → Add Integration → Magic Mic** (nothing to configure yet).

Requires [HACS](https://hacs.xyz). Until a tagged release exists, HACS tracks the `main`
branch, so an update appears whenever new commits land.

---

## Start here

| Doc | What it's for |
|---|---|
| [`VISION.md`](VISION.md) | The pitch — what it feels like, the magic moments, why it's different. Read first. |
| [`PRODUCT_PLAN.md`](PRODUCT_PLAN.md) | The architecture and source of truth — how HA's Assist/LLM stack actually works, the locked decisions, the shared primitives, and the phased roadmap. |
| [`docs/build-sequence.md`](docs/build-sequence.md) | The build *order and proof* — prioritization axes, the walking skeleton, the value dashboard, and where the test harnesses land. |
| [`docs/`](docs/) | Per-feature and per-topic deep dives (map below). |

---

## Design docs

The [`docs/`](docs/) folder holds one file per feature or topic. `PRODUCT_PLAN.md` §0 is the
authoritative index; this is a reading-order map.

**Architecture spine & shared primitives** — the load-bearing mechanisms most features sit on:

- [`prompt-context.md`](docs/prompt-context.md) — the LLM I/O contract + the context/token
  budget (what goes into the prompt, and the generation-counting model that governs cost).
- [`find-entities.md`](docs/find-entities.md) — fuzzy entity resolution → canonical
  `entity_id`; the exact-match fix, and a primitive several features reuse.
- [`scheduling-model.md`](docs/scheduling-model.md) — the spine for every time-based feature
  (timers/alarms/reminders/todos/calendar as one model); delivery, escalation, catch-up.
- [`learning.md`](docs/learning.md) — the friction-resolution primitive: recognize confusion
  → offer a durable fix → persist. Home of aliases, command aliases, annotations.
- [`undo.md`](docs/undo.md) — deterministic reversal by journaling each tool's own inverse.
- [`conversation-loop.md`](docs/conversation-loop.md) — continued conversation / mic-open,
  barge-in, the multi-turn session.
- [`skills.md`](docs/skills.md) — gated instructional payloads: machinery-gated injection
  vs. a resident-header SKILL registry the model pulls from; the seam that carries
  instructions alongside tools and context.

**Capabilities & features:**

- [`memory.md`](docs/memory.md) — the notebook (wifi password, spare key, pet name).
- [`reminders.md`](docs/reminders.md) · [`timers.md`](docs/timers.md) ·
  [`calendar.md`](docs/calendar.md) · [`todo.md`](docs/todo.md) — the scheduling consumers.
- [`ephemeral-automations.md`](docs/ephemeral-automations.md) — a sentence → a real
  trigger/condition/action ("remind me in an hour if I haven't closed the door").
- [`weather.md`](docs/weather.md) · [`web-search.md`](docs/web-search.md) — information retrieval.
- [`music-playback.md`](docs/music-playback.md) · [`ambient-noise.md`](docs/ambient-noise.md)
  — audio: what's-playing / multi-room / follow, and noise/nature sounds.

**Cross-cutting concerns:**

- [`security.md`](docs/security.md) — prompt injection; blast-radius control, taint model.
- [`offline.md`](docs/offline.md) — graceful degradation when the cloud (or HA) is down.
- [`speaker-identification.md`](docs/speaker-identification.md) — voice-ID as an input to
  multi-user identity (Phase 4).
- [`voice-streaming.md`](docs/voice-streaming.md) — how the pipeline streams; where latency
  actually lives.
- [`evaluation.md`](docs/evaluation.md) — tracing vs. offline eval; the two testing tiers.

**Context / decisions:**

- [`external-agents-openclaw.md`](docs/external-agents-openclaw.md) — the build-vs-delegate
  decision (not building on an external agent platform).

---

## How it's meant to fit Home Assistant

- **A thin provider shell + provider-agnostic capabilities.** The conversation integration is
  a disposable adapter; the valuable parts (`find_entities`, calendar-write, reminders,
  memory, the learning engine) are built as core-shaped `llm.py`-style platforms so they can
  be contributed upstream and work for local models too.
- **Never degrades the no-AI path.** Every capability landed as a local *intent* also helps
  non-AI Assist users — and lets those commands run on-device without the cloud.
- **Local-first and multi-user by construction**, not as an afterthought.

See `PRODUCT_PLAN.md` §5 (principles), §7 (path-to-core), and §8 (roadmap) for the full story.

---

## Contributing / running

Very early — the integration loads but has no features, so there's nothing to *run* yet. The
most useful contributions right now:

- **Read a design doc above and poke holes in it** — the architecture is meant to be argued with.
- **File an [issue](https://github.com/jamesshannon/ha-magic-mic/issues)** with feedback, use
  cases, or interest in **beta testing / contributing**.

Setup, the dev/test harness, and PR guidance will land here as the first capabilities do.

> **Working name.** *Magic Mic* — a nod to the internal code name for the "keep the mic open"
> feature this assistant leans on, and to how it ought to feel. The component's technical
> domain will be something neutral; the name isn't load-bearing.
