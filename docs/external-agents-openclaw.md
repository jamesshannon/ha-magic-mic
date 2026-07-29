# External Agent Platforms (OpenClaw): Build vs. Delegate

> Topic/decision doc. Captures the discussion on whether to delegate memory /
> conversation / orchestration to an external agent platform (OpenClaw) instead
> of building HA-native capabilities. See [`../PRODUCT_PLAN.md`](../PRODUCT_PLAN.md).

---

## Context

Considered connecting the HA agent to **OpenClaw** (formerly Clawdbot/Moltbot),
a self-hosted AI agent orchestration layer, to get memory / conversation /
"thinking" "for free" rather than building them natively.

**What OpenClaw is:** a self-hosted orchestration layer wiring together
inference, retrieval, **persistent memory**, and execution into a coherent
agent. Messaging-app front-ends (WhatsApp/Telegram/Discord), reads files,
manages calendar, watches GitHub, executes system commands. BYO-LLM (works best
with Claude; also OpenAI/Google/local Ollama). Has an HA Supervisor add-on. The
existing HA↔OpenClaw link is the **reverse** direction — OpenClaw controlling
HA — not an Assist→OpenClaw connector.

---

## Clarification: "thinking" is not something OpenClaw provides

"Memory and thinking" split apart:
- **Thinking** (extended reasoning) is a **model** capability — Claude's extended
  thinking, already exposed by the `anthropic` integration. You get it by
  choosing the model, not by adopting an orchestrator.
- OpenClaw would only give **memory + retrieval + agent-loop orchestration**. And
  we already own a good tool loop (the forked Anthropic `entity.py`).

So the only real "for free" is a memory store + conversation loop.

---

## The two delegate patterns (only one is bad)

### Pattern A — Build our ambitious HA-native assistant *on top of* OpenClaw ❌
Rejected. You'd try to keep HA's tool/context layer authoritative *while* an
external orchestrator owns the loop. Consequences:
- **Ugly device-control topology.** HA Assist → OpenClaw (LLM decides) → back
  into HA's API to actuate → OpenClaw → Assist response. Two hops through HA;
  device control bypasses the Assist API, losing exposed-entity gating, intent
  resolution, and `LLMContext` (device_id/area) — so "turn on the lights" no
  longer resolves to the room you're speaking in.
- **Identity/area context doesn't cross the boundary.** `get_resolved_user()`,
  `preferred_area_id`, per-device context are HA-native; you'd rebuild or lose
  them.
- **Memory becomes un-composable and un-portable.** It's OpenClaw's model — can't
  shape it to HA per-user/entity/area semantics, can't expose it as a shared
  `RecallMemory` tool, **can't migrate to core.** This is the killer: an
  Assist→OpenClaw bridge is fundamentally un-core-able and forecloses the entire
  path-to-core strategy (PRODUCT_PLAN §7).
- **Latency + trust surface.** Extra orchestration hops in a latency-critical
  voice pipeline; a large always-on agent (file/command/email access) adopted
  just to get a memory store. Scope mismatch.

### Pattern B — HA Voice hardware as a thin front-end to an OpenClaw you already run ✅
Legitimate, clean, and a real gap someone should fill (just **not our product**).
- Clean because you **fully cede to OpenClaw as top-level.** HA Voice = commodity
  ears + mouth: satellite → STT → forward text to OpenClaw → OpenClaw does
  everything (incl. controlling HA via its existing reverse integration) → text
  back → TTS. **Single clear owner**, so no contention over the tool layer. The
  two-hop weirdness of Pattern A only exists if you *also* want HA's Assist
  authoritative; drop that and it's coherent.
- **Trivial to build:** a `ConversationEntity` whose `_async_handle_message`
  forwards `user_input.text` (+ `device_id`/area as context) to OpenClaw's API
  and returns the reply. HA made the conversation agent pluggable for exactly
  this. ~a day of work.
- **Tradeoff:** you lose HA-native resolution (`preferred_area_id`, exposed-entity
  gating, `find_entities`) unless the bridge forwards `device_id`/area and
  OpenClaw's HA-control side consumes it. Acceptable to most people already
  invested in OpenClaw.

---

## Why no Assist→OpenClaw connector exists

OpenClaw's posture is **top-level agent that owns the user relationship**
(messaging-first). Its natural topology is *OpenClaw calls HA*, not *HA calls
OpenClaw*. Being a sub-agent behind Assist is a demotion it hasn't prioritized —
so nobody's built the (easy) bridge. Not a technical barrier; a positioning one.

---

## Where OpenClaw is still useful to us

- **Reference implementation.** Study its memory layer when designing Phase 2
  (persistence, retrieval, injection). Borrow *ideas*, not the dependency.
- **MCP middle-path (only as a shortcut, not a foundation).** If we ever want a
  *specific* OpenClaw capability without ceding the loop: our HA-owned agent
  calls it as an **MCP tool**, or queries an OpenClaw-exposed MCP memory server.
  HA already bet on MCP for this. Still inherits OpenClaw's memory abstractions,
  so foundation-no, shortcut-maybe.

---

## Decision & positioning takeaway

- **Do not build on OpenClaw (Pattern A).** It trades away the two things the
  long-term vision rests on: owning the tool/context layer, and a portable,
  core-bound capability set. Great for a "make my house smart this weekend" goal;
  a dead end for the platform we're building.
- **Pattern B is fine but is a different product** for a different segment
  (people already running a general personal-agent who want voice hardware).
- **This clarifies our differentiation:** our product serves people who want the
  assistant **deeply HA-native** (per-user/per-area context, provider-agnostic
  tools, no external orchestrator, a path into core). The bridge's existence
  would *validate* the split rather than compete: HA Voice as universal voice
  I/O, with a spectrum of agents behind it from "thin bridge to my existing
  agent" (Pattern B) to "deeply-integrated native assistant" (ours).
- ⚠️ **Pattern A and Pattern B look superficially identical** ("connect HA to
  OpenClaw") — do not conflate them later. The difference is *who owns the agent
  loop and the tool layer.*
