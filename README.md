# <img src="custom_components/magic_mic/brand/icon.png" alt="Magic Mic Home Logo" width="45"> Magic Mic

**An LLM-backed voice assistant for Home Assistant that does more than switch devices on
and off.**

Smart speakers turn your lights on and off, and Home Assistant already does that well.
Magic Mic adds the parts that make something feel like a real assistant: reminders that
reach you in whatever room you're in, a conditional automation built from one sentence,
household facts it remembers, phrasing it learns from you, and a conversation that
continues instead of ending after a single command. It's multi-user throughout and runs
locally where it can.

👉 **New here? Read [`VISION.md`](VISION.md)** for a short, example-driven tour of what it
does and how it differs.

---

## For home users

Magic Mic installs as a Home Assistant conversation agent: add it, point your Assist
pipeline at it, and enter your Claude API key. It's beta, and it updates itself as
capabilities land.

**Working today**

- Conversational device control through Assist. Back-and-forth instead of one command per
  press; target things by room, floor, or name.
- Web search, answered inline in the same conversation.
- A compact summary of your home replaces the full device list sent to the model each
  turn, so token cost scales with your layout (floors, areas, domains) rather than your
  device count. A 200-device home stops re-sending 200 names on every turn.
- Automatic updates. HACS tracks `main`, so new capabilities appear as they're committed,
  no reinstall.

**Coming soon**

- Memory: tell it a fact ("the wifi password is hunter2-galaxy"), ask for it back later.
- Reminders that find you in whatever room you're in, including conditional ones ("remind
  me in an hour if I haven't closed the garage").

As each capability ships it moves from "Coming soon" up into "Working today."

---

## Install (HACS)

> **Heads-up:** this installs a **baseline LLM assistant** (device control + web search) that
> needs a Claude API key; the differentiated capabilities are still landing. Add it once to
> get them automatically as they ship, and to follow along.

1. In Home Assistant, open **HACS**.
2. Top-right **⋮ → Custom repositories**.
3. Repository `https://github.com/jamesshannon/ha-magic-mic`, type **Integration** → **Add**.
4. Find **Magic Mic** in HACS, **Download**, then **restart** Home Assistant.
5. **Settings → Devices & Services → Add Integration → Magic Mic**, then enter your Claude
   API key. Select the **Magic Mic** conversation agent in your Assist pipeline to use it.

Requires Home Assistant `2026.7.0` or newer and [HACS](https://hacs.xyz). Until a tagged
release exists, HACS tracks the `main` branch, so an update appears whenever new commits
land.

---

## Start here

| Doc | What it's for |
|---|---|
| [`VISION.md`](VISION.md) | The pitch: what it does, the standout moments, how it differs. Read first. |
| [`PRODUCT_PLAN.md`](PRODUCT_PLAN.md) | Architecture and source of truth: how HA's Assist/LLM stack works, the locked decisions, the shared primitives, and the phased roadmap. |
| [`docs/build-sequence.md`](docs/build-sequence.md) | Build order and proof: prioritization axes, the walking skeleton, the value dashboard, and where the test harnesses land. |
| [`docs/`](docs/) | Per-feature and per-topic deep dives (map below). |

---

## Design docs

The [`docs/`](docs/) folder holds one file per feature or topic. `PRODUCT_PLAN.md` §0 is the
authoritative index; this is a reading-order map.

**Architecture spine and shared primitives** (the load-bearing mechanisms most features sit
on):

- [`prompt-context.md`](docs/prompt-context.md): the LLM I/O contract and the context/token
  budget (what goes into the prompt, and the generation-counting model that governs cost).
- [`find-entities.md`](docs/find-entities.md): fuzzy entity resolution to a canonical
  `entity_id`; the exact-match fix, and a primitive several features reuse.
- [`scheduling-model.md`](docs/scheduling-model.md): the spine for every time-based feature
  (timers, alarms, reminders, todos, and calendar as one model); delivery, escalation, catch-up.
- [`learning.md`](docs/learning.md): the friction-resolution primitive: recognize confusion,
  offer a durable fix, persist it. Home of aliases, command aliases, annotations.
- [`undo.md`](docs/undo.md): deterministic, single-use reversal from typed outcomes and
  capability-owned inverses, with explicit barriers for unsupported mutations.
- [`conversation-loop.md`](docs/conversation-loop.md): continued conversation / mic-open,
  barge-in, the multi-turn session.
- [`skills.md`](docs/skills.md): gated instructional payloads: machinery-gated injection
  vs. a resident-header SKILL registry the model pulls from; the seam that carries
  instructions alongside tools and context.

**Capabilities and features:**

- [`memory.md`](docs/memory.md): the notebook (wifi password, spare key, pet name).
- [`reminders.md`](docs/reminders.md) · [`timers.md`](docs/timers.md) ·
  [`calendar.md`](docs/calendar.md) · [`todo.md`](docs/todo.md): the scheduling consumers.
- [`ephemeral-automations.md`](docs/ephemeral-automations.md): a sentence becomes a real
  trigger/condition/action ("remind me in an hour if I haven't closed the door").
- [`weather.md`](docs/weather.md) · [`web-search.md`](docs/web-search.md): information retrieval.
- [`music-playback.md`](docs/music-playback.md) · [`ambient-noise.md`](docs/ambient-noise.md):
  audio: what's-playing / multi-room / follow, and noise/nature sounds.

**Cross-cutting concerns:**

- [`explainability.md`](docs/explainability.md): "why is it at 67 / why did the light turn
  on?" — narrate the cause from logbook + history; state reversal is out of scope.
- [`security.md`](docs/security.md): prompt injection; blast-radius control, taint model.
- [`offline.md`](docs/offline.md): graceful degradation when the cloud (or HA) is down.
- [`speaker-identification.md`](docs/speaker-identification.md): voice-ID as an input to
  multi-user identity (Phase 4).
- [`voice-streaming.md`](docs/voice-streaming.md): how the pipeline streams; where latency
  lives.
- [`evaluation.md`](docs/evaluation.md): tracing vs. offline eval; the two testing tiers.

**Context and decisions:**

- [`external-agents-openclaw.md`](docs/external-agents-openclaw.md): the build-vs-delegate
  decision (why this isn't built on an external agent platform).

---

## How it's meant to fit Home Assistant

- **A thin provider shell plus provider-agnostic capabilities.** The conversation
  integration is a disposable adapter. The parts that matter (`find_entities`,
  calendar-write, reminders, memory, the learning engine) are built as core-shaped
  `llm.py`-style platforms, so they can be contributed upstream and work for local models too.
- **Doesn't degrade the no-AI path.** Every capability that lands as a local *intent* also
  helps non-AI Assist users, and lets those commands run on-device without the cloud.
- **Local-first and multi-user by construction.** Keyed per person and cloud-optional from
  the start, not bolted on afterward.

See `PRODUCT_PLAN.md` §5 (principles), §7 (path-to-core), and §8 (roadmap) for the full story.

---

## Contributing / running

Early days: it installs and runs as an LLM voice agent (see [For home
users](#for-home-users)), but the differentiated capabilities are still landing. The most
useful contributions right now:

- **Read a design doc above and poke holes in it.** The architecture is meant to be argued with.
- **File an [issue](https://github.com/jamesshannon/ha-magic-mic/issues)** with feedback, use
  cases, or interest in **beta testing / contributing**.

Setup, the dev/test harness, and PR guidance will land here as the first capabilities do.
