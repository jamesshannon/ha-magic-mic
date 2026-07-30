# Offline / Cloud-Unreachable Degradation

> Cross-cutting concern. The plan locks **cloud Claude** (PRODUCT_PLAN §3) into a
> project whose whole ethos is **local-first**. This doc owns the question no
> per-feature doc does: what happens when the model is unreachable, and how much of
> the assistant keeps working anyway. Builds on `prefer_local_intents` (§2.9) and the
> `assist_satellite` delivery primitive (§2.8). Cross-refs
> [`scheduling-model.md`](scheduling-model.md), [`ephemeral-automations.md`](ephemeral-automations.md),
> [`find-entities.md`](find-entities.md), [`voice-streaming.md`](voice-streaming.md).

---

## TL;DR

- **Offline is a *whole-pipeline* property — STT and TTS, not just the LLM.** A cloud
  LLM usually means cloud STT + TTS too. **Cloud STT down → no transcript ever reaches
  hassil**, so even local device control (§2.9) dies — the local-first rescue *assumes
  local STT*. **Cloud TTS down → can't speak the answer, or even the error.** Resilience
  is a property of the whole pipeline; each stage's cloud-dependence counts independently.
- **Two axes, don't conflate them.** *Runtime-offline* = a scheduled thing must fire
  while the cloud is down (reminders/timers/automations). *Request-offline* = a live
  utterance can't reach the cloud. They have different answers; the design already
  mostly solves the first (with a TTS caveat below).
- **Today's behavior (verified).** Local-first runs hassil first (§2.9); on a miss the
  request goes to the LLM; on an API/connection error the Anthropic entity raises
  `HomeAssistantError`, `async_converse` catches it, and the user **hears the raw error
  string** ("Anthropic API error: …") — *not* the local "Sorry, I couldn't understand
  that." **No retry, no second local pass.** The conversation ends.
- **Blast radius is already small — because of `prefer_local_intents`.** The bulk of
  device control never needed the cloud, so request-offline only degrades the
  *residual* the local matcher couldn't handle. Offline-resilience ≈ *how capable the
  local intent layer is* → the same **dual-payoff** as §2.9/§7: every local intent we
  contribute is also an offline-resilience gain.
- **Runtime-offline is mostly solved by construction — the *trigger* is.** The
  **LLM-authors-once / deterministic-runtime** split means timers, alarms, reminders,
  and ephemeral automations **fire with zero LLM involvement.** But *delivery* still
  needs TTS to speak **content**: the content-free **earcon** ding is a media file (TTS-
  independent → resilient), while reading the reminder text needs TTS. So a reminder set
  yesterday still **dings** offline; whether it can be **read** depends on TTS (fixable by
  pre-rendering content at creation — below).
- **The residual that genuinely needs the model at runtime** (conversational Q&A, web
  search, forecast summarization, authoring *new* smart behaviors) can't be rescued —
  so degrade **legibly**: a clear "can't reach the assistant" message, transient-vs-auth
  distinguished, never a silent drop or a faked success.

---

## What HA does today (verified)

The request path and its failure mode:

1. **Local-first (§2.9).** With `prefer_local_intents` on, hassil strict-matches first;
   common device control is handled locally and **never touches the cloud** — immune to
   an outage.
2. **Miss → the LLM agent.** `recognize_intent` falls back to `conversation.async_converse`
   (`assist_pipeline/pipeline.py:1297`).
3. **API/connection failure → raise.** The Anthropic entity catches
   `anthropic.APIConnectionError` (WAN down), `AuthenticationError`, and other
   `AnthropicError`s and **raises `HomeAssistantError`** with a translated message,
   calling `coordinator.mark_connection_error()` (`anthropic/entity.py:1228-1248`).
4. **Caught into an error response.** `async_converse` catches `HomeAssistantError` and
   returns an `IntentResponse` with code `UNKNOWN` whose **speech is `str(err)`**
   (`conversation/agent_manager.py`, the `except HomeAssistantError` branch). So the
   user hears the integration's error string — e.g. *"Anthropic API error: Connection
   error."* (`anthropic/strings.json` → `api_error`).

**Two facts that shape the design:**
- The spoken output on outage is **not** the generic local no-match text
  (`_DEFAULT_ERROR_TEXT = "Sorry, I couldn't understand that"`,
  `default_agent.py:84`) — it's the specific, techy, vendor-naming API error. Poor UX;
  ours to improve in the shell.
- There is **no fallback after the LLM fails.** The local pass already happened *before*
  the LLM (and missed, which is why the LLM was called); nothing tries again. This is
  the gap where a **second local pass** (below) lives.

---

## Two axes (the load-bearing distinction)

| | **Runtime-offline** | **Request-offline** |
|---|---|---|
| Question | A scheduled item must fire while the cloud is down | A live utterance can't reach the cloud |
| Examples | reminder/alarm/timer fires; ephemeral automation's trigger hits | "turn on the lamp", "is the door open?", "set a reminder" |
| Status | **mostly solved by construction** (deterministic runtime) | mitigated by local-first + second pass; floored by a legible error |
| Note | *distinct from* "HA was **down**" (the watermark/catch-up story in `scheduling-model.md`) — here HA is **up**, only the cloud is unreachable, so the item fires normally | authoring a *new* scheduled item still needs the cloud |

Conflating these is the trap: `scheduling-model.md`'s catch-up handles HA **downtime**;
this doc handles the cloud being unreachable while HA runs. A reminder created last week
fires fine today with no internet — its runtime is entirely local.

---

## The pipeline is the unit of resilience (STT + TTS, not just the LLM)

A cloud LLM almost always ships with **cloud STT + TTS** (Nabu Casa Cloud is the common
bundle). Each stage fails independently, and the LLM analysis above silently assumed the
*other two* stages work. They may not:

| Stage | Local engine | Cloud engine | If the cloud engine is down |
|---|---|---|---|
| **STT** | Whisper / speech-to-phrase (Wyoming) | Nabu Casa Cloud | **No transcript at all** → nothing reaches hassil → even local device control (§2.9) is dead. Layers 0–1 **presuppose local STT.** |
| **Intent** | hassil (local) / our agent | cloud LLM | Covered above (local-first rescue). |
| **TTS** | Piper (Wyoming) | Nabu Casa Cloud | **Can't speak** the answer *or the error announcement.* You may be unable to even *tell* the user they're offline. |

**So the single biggest resilience lever is a fully-local pipeline** (local STT + hassil
+ local TTS, e.g. Whisper/speech-to-phrase + Piper): device control, scheduled firing,
and spoken responses all survive an outage. A cloud STT/TTS bundle trades resilience for
quality — a **legible** deployment tradeoff to surface, not to hide. This is also why
HA built **speech-to-phrase** (fast local STT tuned for intents): the local path only
pays off end-to-end if STT is local.

**Caveat — the two local STT choices are not interchangeable for us, and Speech-to-Phrase
is largely incompatible with the LLM assistant.** Speech-to-Phrase is a **close-ended**
model: it "transcribes what it knows" and, per HA's own docs, "open-ended items such as
shopping lists, naming a timer, and broadcasts are not usable out of the box"
(`voice_remote_local_assistant.markdown`). That's fine for hassil device control (fixed
sentence set) but it **structurally breaks the LLM path** — an arbitrary question, a
memory write, a free-form reminder body never gets transcribed, so nothing reaches our
agent. The STT that is *both* local-resilient *and* LLM-capable is **Whisper**, which is
open-ended but ~8 s per command on a Pi 4 (sub-second only on a NUC/beefier box). So the
"fully-local resilient pipeline" and "LLM assistant" are, below our layer, close to an
**either/or on modest hardware**: Speech-to-Phrase → fast, resilient, no LLM; Whisper →
LLM-capable and local but slow on a Pi; cloud STT → LLM-capable and fast but not
outage-resilient. This is a deployment tradeoff to **surface in onboarding guidance**
(§6.1 "detect and surface"), not something our code can resolve — a Speech-to-Phrase user
has effectively opted out of the open-ended half of the assistant.

### Cached / pre-rendered system phrases (so we can still speak)
Even with cloud TTS, a fixed set of **system phrases** can be made offline-playable,
because **HA's TTS has a disk cache** (`SpeechManager`, `use_file_cache` / `cache_dir`,
keyed by text + engine + language + voice). Two moves:

- **Pre-warm the cache while online** with the assistant's fixed phrases — the offline
  error ("I can't reach the assistant right now"), the reminder prompt ("you have a
  reminder — say 'read it'"), confirmations — synthesized **in the user's chosen voice +
  language** so the fallback sounds consistent (the reason to cache, not ship generic
  clips). Re-render when the voice/language changes (cache key changes → stale entries
  simply miss).
- **Earcons need no TTS.** The content-free announce ding (`PREANNOUNCE_URL`, a bundled
  media file, §2.8) plays regardless — so the *alert* half of delivery is inherently
  offline-resilient; only *spoken content* needs TTS. Another point in favor of
  content-free-announce + pull.

(Precedent for shipping/serving fixed audio: the bundled `acknowledge.mp3` and
`PREANNOUNCE_URL` static paths — but those are voice-neutral chimes; *phrases* want the
user's voice, hence cache-per-voice rather than bundle.)

---

## The mitigation layers (cheapest/highest-value first)

### Layer 0 — `prefer_local_intents` already shrinks the blast radius
The single biggest mitigation is free and already analyzed (§2.9): the common command
path is local, so an outage only touches the residual. **Investing in the local intent
layer is investing in offline resilience** — the dual-payoff of §2.9/§7 restated from the
availability angle.

### Layer 1 — a second local pass on connection failure (our shell's job)
Today nothing retries after the LLM errors. Our forked shell can, **only on a
connection/availability error** (not on a policy refusal or a genuine bad request):

- **(a) Honor the deferred-intent local match.** `GET_STATE` and `MEDIA_SEARCH_AND_PLAY`
  *did* strict-match locally but were handed to the LLM by policy (§2.9). On LLM failure,
  **fall back to the local canned result** instead of erroring — "is the door open?"
  answers offline. Highest-value rescue: it recovers exactly the intents local *can* do,
  that we only deferred for quality.
- **(b) Best-effort non-strict retry.** Re-run the local matcher with
  `strict_intents_only=False` (and, if we ever add it, the fuzzy `find_entities` tier in
  the local matcher — the open question in [`find-entities.md`](find-entities.md)). Only
  a *number* of requests are rescued and a wrong best-effort match is possible, but
  offline a legible guess can beat a flat error. Gate conservatively.

This is a **shell policy**, not a capability — portable-agnostic, lives in the throwaway
conversation loop (§5.5).

### Layer 2 — compile-once / run-deterministic = offline by construction
The strongest structural lever, and the answer to "what else can be done." The
**LLM-authors-at-creation / HA-evaluates-at-fire** split (already the spine of the
scheduling model and the whole thesis of [`ephemeral-automations.md`](ephemeral-automations.md))
means the *runtime* needs no model:

- Timers, alarms, reminders **fire** through the LLM-free delivery engine (announce +
  watermark/catch-up, §2.8/`scheduling-model.md`).
- Ephemeral automations are the archetype: "turn off the heater in 30 min unless
  someone's in the garage" → the LLM compiles `{trigger, condition, action}` **once**;
  HA's deterministic trigger/condition engine runs it **offline forever**.
- Corollary — **proactive needs the model at runtime** (`start_conversation`, §2.8/A1),
  so proactive *nudges* don't work offline; but the underlying scheduled **`announce`**
  is LLM-free, so the reminder still **dings** — it just can't do the conversational
  version. Concrete illustration of the split.

**Design pressure:** prefer capability shapes that compile to a deterministic runtime
over shapes that need the model at fire time (the same reason
`ephemeral-automations.md` compiles conditions instead of LLM-at-fire). Capabilities
that are inherently runtime-LLM (web search, world-knowledge Q&A, forecast
summarization, fuzzy LLM-at-fire conditions) are inherently offline-fragile — that's a
property to *acknowledge*, not fix.

#### The partial-delivery trap (firing when TTS is down)
The trigger is LLM-free, but reading dynamic **content** needs TTS — and with cached
system phrases (above) you can get a *partial* interaction that's worse than a clean
failure: earcon dings ✅ → cached "you have a reminder, hear it?" ✅ → user says "yes"
→ **ack recorded** → but the reminder's *actual text* was never cached → **can't play**.
The user ack'd a message they never heard. Two defenses:

- **Pre-render content at creation.** When a reminder is created (online), synthesize its
  content audio **then** and cache it (short text, cheap), so read-back survives a later
  TTS outage. Caveats: recurring/dynamic content and voice/language changes invalidate it
  (re-render on change).
- **An ack that can't deliver isn't an ack.** If the content can't be produced, **don't
  consume the ack** — treat un-playable content as a *delivery failure*, keep the item
  **queued**, and re-surface at interaction-start when TTS returns (the same
  never-silently-drop invariant as the reminder queue, `scheduling-model.md`). Better to
  re-notify than to mark-read-but-unheard.

### Layer 3 — legible degradation for the true residual
What can't be rescued (novel conversation, web search, generation) must fail **well**:

- **A clear, vendor-neutral message** — "I can't reach the assistant right now" beats
  "Anthropic API error: Connection error." Replace the raw `str(err)` in our shell. **But
  saying it needs TTS** — so if TTS is cloud, this phrase must be one of the **pre-cached
  system phrases** above, or the failure is silent (an earcon is the last-resort
  fallback).
- **Distinguish transient from terminal** — connection/5xx/overloaded → "try again in a
  moment"; auth → a config problem (reauth), not a retry.
- **Pre-empt with connection health.** The Anthropic coordinator already tracks
  connection state (`mark_connection_error`, and the entity's availability). If we *know*
  we're offline, skip the doomed cloud round-trip (and its latency) and go straight to
  best-effort-local + the clear message.
- **Never silently drop, never fake success** (the same trust invariant as the reminder
  queue).

### Layer 4 — local model fallback (speculative, flagged not planned)
The logical extreme: a small local model (e.g. Ollama) as an emergency backend for the
residual. High cost/complexity and out of v1 scope, but it's *enabled* by the
provider-agnostic capability discipline (§5.5, `web-search.md`'s framing-per-backend) —
capabilities already port to a local backend. Noted as the far end of the spectrum, not
a commitment.

---

## What to build (and what's already handled)

- **Already handled (no build):** runtime-offline *firing* for scheduled items
  (deterministic runtime) + earcon alert (TTS-independent); the Layer-0 shrink from
  `prefer_local_intents`.
- **Config/recommendation (biggest lever):** a **fully-local pipeline** (local STT + TTS)
  for resilience; surface the cloud-STT/TTS resilience tradeoff legibly.
- **Shell work (v1-cheap):** Layer 1 second-pass (honor deferred local matches on
  connection failure; conservative non-strict retry) + Layer 3 legible messaging
  (vendor-neutral, transient-vs-auth, optional connection-health short-circuit).
- **Cache work:** pre-warm **system phrases** (in the chosen voice/language) into the TTS
  disk cache; **pre-render reminder content at creation**; the "ack-that-can't-deliver →
  keep queued" policy.
- **Ongoing discipline:** Layer 2 — keep pushing capability into compile-once /
  run-deterministic; treat runtime-LLM capabilities as knowingly offline-fragile.
- **Deferred:** Layer 4 local-model fallback.

---

## Open questions

- **Second-pass trigger conditions** — exactly which error types warrant Layer 1
  (connection/5xx yes; refusal/bad-request no); how conservative the non-strict retry
  threshold is offline vs online.
- **Connection-health short-circuit** — is the Anthropic coordinator's state exposed
  cleanly enough to read pre-request, and does pre-empting hurt the case where the cloud
  recovered between the health signal and the utterance?
- **Message wording + i18n** — the vendor-neutral offline message and its translations;
  how much to say ("assistant offline" vs "no internet").
- **Fuzzy-in-local** — resolved jointly with the [`find-entities.md`](find-entities.md)
  open question (extending the fuzzy tier into the local matcher would also widen Layer 1b).
- **Does the second pass belong in core or the shell?** It's shell policy today; a
  `prefer_local_intents`-style "local fallback on agent error" could be a core
  contribution (helps every LLM agent, local-first-aligned).
- **Pre-render scope** — which system phrases to pre-cache; whether to pre-render *all*
  reminder content at creation or only when the pipeline uses a cloud TTS engine; how to
  detect "this TTS engine is cloud" to decide.
- **Ack-without-delivery policy** — keep-queued vs a degraded "you have a reminder I
  can't read yet"; interaction with snooze and the interaction-start queue.

---

## Key references

- `assist_pipeline/pipeline.py:1263-1297` — local-first attempt, then LLM fallback
- `anthropic/entity.py:1228-1248` — API/connection errors → `HomeAssistantError` +
  `mark_connection_error`
- `conversation/agent_manager.py` — `async_converse` catches `HomeAssistantError` → error
  `IntentResponse` (speech = `str(err)`)
- `anthropic/strings.json` — `api_error` / `api_authentication_error` messages
- `conversation/default_agent.py:84` — `_DEFAULT_ERROR_TEXT` (the local no-match text,
  *not* what an outage produces)
- `tts/__init__.py` — `SpeechManager` + `use_file_cache` / `cache_dir` (disk TTS cache,
  keyed by text+engine+language+voice); `PREANNOUNCE_URL` earcon (TTS-independent)
- Local engines: `piper` (TTS), `whisper` / speech-to-phrase (STT), `wyoming` (protocol)
- PRODUCT_PLAN §2.8 (delivery primitive), §2.9 (`prefer_local_intents`)
