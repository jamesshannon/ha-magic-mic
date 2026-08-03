# Conversation Loop: Continued Conversation, Turn Reopening & Multi-turn

> Feature/mechanics doc. How HA keeps the mic open for follow-ups ("continued
> conversation"), the timeouts that close it, and our design for free-flowing
> multi-turn ("Magic Mike"-style) without drowning in false captures.
> See [`voice-streaming.md`](voice-streaming.md) and
> [`../PRODUCT_PLAN.md`](../PRODUCT_PLAN.md).

---

## TL;DR

- HA supports leaving the mic open after a turn — **"continued conversation."**
  The agent sets a flag; the satellite reopens the mic for the next turn,
  **skipping the wake word**, preserving `conversation_id`.
- **Default trigger is crude:** reopen iff the assistant's reply ends with a
  question mark. So declarative answers ("Washington DC") won't reopen — the
  natural follow-up dies.
- **It auto-closes:** trailing-silence (~0.7 s) ends a spoken turn; an overall
  ~15 s timeout closes a silent one. Both configurable.
- **Our design:** default-**continue** for informational turns (LLM flags *stop*),
  deterministic **stop** after device-control turns, a **shorter follow-up
  timeout**, and a **spurious-gate** on reopened turns so ambient/TV audio is
  rejected instead of interpreted. The spurious-gate is what makes liberal
  reopening safe.
- **Barge-in** (interrupt the assistant mid-response — "stop", or a new question over the
  top) lives **below the agent**: the satellite + pipeline handle it, the LLM never sees a
  "stop". We **inherit** it; our job is to keep streaming (so a long reply is cancelable)
  and keep stop-words **local** (`HassNevermind`, offline-safe). See §Barge-in.

---

## The mechanism (continued conversation)

Three steps across the stack:

1. **Agent signals it** — `ConversationResult.continue_conversation`
   (`conversation/models.py:80`), derived from `ChatLog.continue_conversation`
   (`chat_log.py:356`).
2. **Pipeline remembers** — on a result with `continue_conversation=True`, the
   pipeline stores the agent id (`pipeline.py:1346` → `continue_conversation_agent`;
   field at `pipeline.py:2232`). On the next run, `prepare_recognize_intent`
   (`pipeline.py:1045`) sees it, forces the same agent, and proceeds
   intent-only.
3. **Satellite reopens the mic** — the flag is passed to the device
   (`esphome/assist_satellite.py:384`). The satellite reopens the microphone and
   starts the next pipeline **at STT, skipping wake word**, reusing the same
   `conversation_id` so history/context carries.

### Default trigger: the "?" heuristic (crude)
`ChatLog.continue_conversation` returns `True` **iff the assistant's last message
ends with `?` / `;` (Greek) / `？` (Chinese)** (`chat_log.py:356`). That's the
whole rule. Zero-config and fine for explicit questions, but it can't see the
conversational middle ground: an informational *answer* ("Washington DC") is a
statement, so it won't reopen, even though "what's the population?" is a natural
next turn. A stock agent (and ours, via `async_get_result_from_chat_log`,
`util.py:45`) inherits this heuristic unless it sets the flag explicitly.

### Timeouts that close the mic
From `VoiceCommandSegmenter` (`vad.py:73`), all configurable fields:
- **`silence_seconds = 0.7`** — trailing silence after speech ends the turn.
- **`timeout_seconds = 15.0`** — overall cap; a reopened-but-silent mic closes
  at 15 s.
- (`speech_seconds = 0.3`, `reset_seconds = 1.0` tune command-start detection.)

So a declined follow-up simply times out and closes — the safety property that
makes liberal reopening viable.

### Characteristics / limits
- **Single-turn window that chains** — reopens for *one* more turn; if that
  turn's result also sets the flag, it chains again. Not a persistent open mic.
- **Device-dependent** — the satellite must honor the flag (esphome / Voice PE
  do); same opt-in shape as timer handlers.
- **No always-on mode** — there is deliberately no wake-word-free always-listening
  mode (privacy).

---

## Design for our agent

### The vision
Free-flowing multi-turn: "What's the capital of the US?" → "Washington DC" →
*[mic reopens]* → "Nice, what's the population?" — without re-waking. The "?"
heuristic can't do this because the answer is declarative.

### 1. Default-continue, with a deterministic stop after commands
A positive-signal model (reopen only on a detected cue) systematically
*under*-continues — "Washington DC" reads as terminal. Inverting the default
(assume continue; LLM flags **stop**) fixes the unpredictable long tail
("population?", "how far from NYC?").

**But a single global default-continue is wrong for the majority of traffic**,
because of a turn-type asymmetry:

| Turn type | Natural default | Why |
|---|---|---|
| Device command ("turn off the lights") | **Stop** | Task closed; nobody follows up conversationally; this is the *bulk* of voice traffic |
| Informational / Q&A ("Washington DC") | **Continue** | Follow-ups are common |

Global default-continue exposes the *common* case (commands) to the failure mode:
if the LLM ever forgets to flag stop, the mic hangs open after "turn off the
lights" — into the TV. So:

- **Device-control turn → deterministic stop.** You already know the turn invoked
  a control intent and ended without a question — a stop signal that
  needs no LLM judgment. Cheap, reliable, covers the majority.
- **Informational turn → default continue**, LLM flags stop only on clear
  terminal cues ("thanks, that's all").

This puts default-continue where it belongs (conversation) without betting the
common command case on the LLM remembering to stop.

*Mechanism note — how the stop/continue signal is actually carried.* Inverting the
default requires a **reliable per-turn channel** for the signal; the "?" heuristic
was free, this isn't. But the two turn-types get it from **different sources**, and
that's the point:

- **Command turn → deterministic, no model channel needed.** "Did a control intent
  fire this turn (a `tool_use` for an IntentTool) and end without a question?" is
  computable in the shell — a *stop* with **zero** LLM judgment. This is
  the mic-open case of the general rule "infer what you can deterministically"
  ([`prompt-context.md`](prompt-context.md) §Meta-signals). More reliable than
  trusting the model to remember a flag, and it covers the *bulk* of traffic.
- **Informational turn → a genuine model-only signal.** "The user seems done"
  ("thanks, that's all") has no deterministic source, so the LLM must emit it. This
  is the signal that needs the per-turn channel — delivered as a **struct field on
  the final response / typed block, not a dedicated `set_metadata` tool** (which
  would force a wasted extra generation) and not the punctuation heuristic. See
  [`prompt-context.md`](prompt-context.md) (output contract) for how that channel
  is shaped and why a tool call is the wrong carrier.

So `leave_mic_open` is **not one mechanism**: it's deterministic inference for
commands + a model-emitted field for the conversational tail. Avoid modeling it as
a single boolean the LLM always sets.

### 2. Shorter follow-up timeout
The 15 s default is too long for a *follow-up* window — a declined follow-up
should close fast (Google/Alexa use ~8 s). Since `timeout_seconds` is per-run
configurable, use a **shorter timeout (≈5–8 s)** on continuation turns so silence
(or TV audio) closes quickly rather than holding an open mic for 15 s.

### 3. Spurious-gate on reopened turns (the piece that makes #1 safe)
Liberal reopening's #1 downside is **false capture** — ambient speech / TV /
cross-talk fires a spurious STT→LLM turn. VAD only detects *speech*, not
*relevance*. The LLM, with the conversation in context, can judge **plausible
continuation of this thread** — "call now while operators are standing by" isn't
one, even though it's a valid imperative in isolation.

**Prime continuation turns with:** *"This may be a spurious capture (background
audio, TV, someone else). If it isn't a plausible continuation of the
conversation, return 'spurious'."* Refinements:
- **Only on continuation turns**, not wake-word turns (wake-word = intentional;
  try to interpret those).
- **Judge against the prior turn**, not in isolation (that's what rejects the
  valid-but-implausible imperative).
- **On "spurious": discard silently** — no TTS, no action, and **do not append to
  the chat log / memory** (a spurious capture in context degrades the next real
  turn). Then close (or one short re-listen).
- **Accept the failure mode:** occasional over-rejection of an oddly-phrased real
  follow-up → user re-asks (same friction as a timeout). Net strongly positive.

**The main model performs this judgment in v1.** It receives the complete STT transcript
plus prior turns, which is the richest text context available for distinguishing a real
follow-up from a quoted command in a television scene. A smaller continuation classifier is
not assumed to be better, and same-speaker audio is supporting evidence rather than proof of
intent.

**Fallback to measure if false accepts are too high:** a no-tools classification generation.
Call the same capable model with the transcript and history but no actionable tools, require
`intentional | spurious | uncertain`, and only expose tools after `intentional`. This makes
the gate explicit, measurable, and able to fail closed on `uncertain`; it does not inherently
make the model's judgment more accurate and costs another generation on every continuation.
Do not adopt it without the adversarial eval showing that the latency buys a meaningful
false-action reduction.

Pending interaction state narrows **referents**, not the user's permitted intent. After
"which lamp?" the user may answer "the den one," but may also say "I didn't say lamp, turn up
the heat." After a confirmation they may say "actually no, turn off my lights." The model
may reject/supersede the pending operation and issue a new command in the same turn. The
special reminder case stays precise because "read it" resolves through the pending-reminder
operation; it must not become a generic request to read calendar or memory data.

**Consequence-aware confirmation is the bounded response to a false accept.** Tools/intents
may carry a small ordinal consequence policy (`low | confirm-on-continuation |
always-confirm`), and continuation origin can promote the required confirmation tier. The
gateway then stages the exact operation and asks "did I hear you right?" before execution.
Model metadata or sensitive-data provenance may only raise the deterministic base tier,
never lower it. A calibrated numeric Intent × Domain risk matrix is not a v1 requirement;
prove the hook on a few representative operations first.

### The synthesis
#3 makes #1 safe. Default-continue is only tolerable if the resulting false
captures are filtered — the spurious-gate is that filter. They're a **pair**:
*reopen liberally (default-continue + deterministic stop after commands) because
follow-up turns are spurious-gated.* Without the gate, aggressive reopening
drowns in TV-triggered turns; with it, you get the free-flowing conversation and
a graceful reject for noise.

### Privacy
Given HA's base, make continued-conversation **opt-in / configurable**. The VAD
auto-close makes it tolerable; the LLM judgment (turn-type default + spurious
gate) is what makes it feel *smart* rather than creepy-always-on.

---

## Why clarifications stay LLM-in-loop (not `assist_satellite.ask_question`)

Core ships a closed-set Q&A primitive — `assist_satellite.ask_question` — that announces
a question, captures **one** STT utterance, and matches it **locally against a fixed
answer set with no LLM** (truncates the pipeline at STT; `assist_satellite/entity.py:333,
481-484`). It's tempting for disambiguation ("which lamp — reading or couch?"), but it's
the **wrong tool** for it: our clarify round-trip
([`find-entities.md`](find-entities.md)) is deliberately **open and contextual** — the
model reasons over scored candidates + history and must accept "the tall one in the
corner," not just an enumerated choice. `ask_question` can't do that. So clarifications
ride the **continue-conversation + spurious-gate** path above, keeping the LLM in the
loop, not `ask_question`.

`ask_question` is **not discarded** — it's logged in [`prompt-context.md`](prompt-context.md)
as a candidate **generation-saving confirm-before-write** primitive (a deterministic
yes/no needs no LLM and no extra generation). That's a different job from open-ended
disambiguation.

## Barge-in (interrupting the assistant mid-response)

The user must be able to **cut off the assistant while it's talking** — halt a long
reminder / weather forecast / a blaring alarm ("stop"), or ask something new over the top
("wait — what's the population?"). Two facts settle the design:

- **Barge-in lives *below* the conversation agent — we inherit it, we don't build it.**
  Wake-word (or a button) during playback is detected by the **satellite firmware**
  (esphome / Voice PE), which stops the audio and starts a fresh pipeline; HA cancels the
  in-flight run (`_cancel_running_pipeline`, `assist_satellite/entity.py:547`). **The
  Anthropic/LLM agent never sees a "stop"** — the interrupt is an audio/pipeline event.
  Our forked agent's only obligation is to **not break it** (below).
- **"Stop" is a *local, deterministic* intent, not an LLM turn.** `HassNevermind`
  (`intent/__init__.py:410`, `INTENT_NEVERMIND`) is a local hassil intent and is
  **deliberately withheld from the LLM** (§2.5) — so "stop / nevermind / cancel" resolves
  on the local path, instantly, and **works offline** (a blaring alarm must be stoppable
  with no WAN — [`offline.md`](offline.md)). This is `prefer_local_intents` (§2.9) doing
  exactly its job.

### Two kinds of barge-in
1. **Stop-barge-in (halt).** The wake-word interrupt itself stops the playback; the
   utterance, if any, resolves **locally** — "stop/nevermind" → `HassNevermind`; "stop the
   timer" → timer-cancel; an **alarm** → *dismiss* (the delivery engine's ack transition,
   [`scheduling-model.md`](scheduling-model.md)). No LLM, no cloud.
2. **Replace-barge-in (new request).** "What's the population?" is just a **normal new
   turn** through the pipeline → the LLM like any query. No special handling; inherited.

### Our responsibilities (what must not break)
- **Keep streaming so a long response is cancelable mid-stream.** A response buffered into
  one blob can't be cleanly cut; the `supports_streaming` + delta-emission constraint
  ([`voice-streaming.md`](voice-streaming.md)) is *also* the barge-in constraint. When the
  pipeline task is cancelled, our generation task must cancel cleanly (no uninterruptible
  work).
- **Stop-words stay local** (above) — never route "stop" to the cloud.
- **Barge-in ≠ spurious.** A barge-in is **wake-word-gated → intentional**, so it is
  *interpreted*, not spurious-gated — consistent with "only spurious-gate continuation
  turns, not wake-word turns" (§3). The two mechanisms compose.

### The "user only heard half" subtlety (noted, low priority)
Interrupting TTS at token *N* means the user heard only the first *N* characters, but the
`ChatLog` records the **full generated response** → the model's history diverges from what
the user actually heard, so a follow-up ("the second one?") could confuse it. **Fix if it
ever matters:** record how much was actually flushed to TTS (we already count deltas,
[`voice-streaming.md`](voice-streaming.md)) and **truncate/annotate the assistant turn in
the chat log at the interruption point.** Edge case — logged so it isn't lost.

### Device dependence & privacy
Barge-in requires a satellite that supports **wake-word-during-playback** (a device/
firmware capability — Voice PE / capable esphome; same opt-in-by-capability shape as timer
handlers and `announce`). There is deliberately **no always-on stop-word** without it
(privacy: interruption stays wake-word-gated). Devices without the capability → wait it
out or use a button.

## Multi-turn context (ChatLog)

Continued conversation reuses the same `conversation_id`, so the `ChatLog`
persists across turns — history and any injected context carry into the
follow-up, which is what makes clarify/agentic flows (reminder confirmation, the
rare music disambiguation) work. The chat loop also emits streaming deltas
(see [`voice-streaming.md`](voice-streaming.md)); a follow-up turn must preserve
that delta/streaming behavior.

**Build on `ChatLog`; do not mirror it.** Most short interactions need only its transcript
and tool results. The model can interpret "the den one" from a candidate-bearing tool result
or an ordinary "yes" from the preceding question without another conversation object.
Deterministic state is added only when reconstructing from prose would be unsafe:

- an immutable pending operation for a consequence-gated confirmation;
- a bounded undo journal;
- per-turn principal, provenance, and effect metadata.

`MagicMicChatLog` exposes that state, but conversation-lifetime values live in a
`conversation_id`-keyed sidecar. HA clones the `ChatLog` dataclass between turns with
`dataclasses.replace()`, so values stored only in the subclass instance dictionary would be
lost. Durable reminder, memory, rule, and delivery state stays in each capability's store,
not in the chat session.

## Evaluation gate

Use two layers. Scripted multi-turn text trajectories first prove history, corrections,
pending-operation supersession, and spurious classification without introducing STT or TTS
variance. The feature is not complete until the controlled Assist pipeline proves that the
microphone actually reopens without a wake word, preserves `conversation_id`, closes on the
declared timeout/stop conditions, and discards a spurious capture without speech or effects.
Barge-in additionally requires cancellation during streamed output and proof that local stop
handling wins. Real-device timing and acoustic behavior are a separately labelled profile,
not a prerequisite for every text-behavior run.

---

## Key references

- [`telemetry.md`](telemetry.md) — content-free deployed-use measures for actual follow-up
  rate, session length, correction proxies, and optional classifier cost.
- `conversation/models.py:80` — `ConversationResult.continue_conversation`
- `conversation/chat_log.py:356` — the "?" heuristic
- `conversation/util.py:45` — result derives the flag from the chat log
- `assist_pipeline/pipeline.py:1045,1346,2232` — pipeline propagation
- `assist_pipeline/vad.py:73` — `VoiceCommandSegmenter` timeouts
- `esphome/assist_satellite.py:384` — flag passed to the device
