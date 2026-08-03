# Voice Pipeline Streaming & Latency

> Meta-feature doc. How HA's Assist pipeline streams audio/text through
> STT → LLM → TTS, where the latency actually lives, and what is (and isn't)
> worth contributing to core. See [`../PRODUCT_PLAN.md`](../PRODUCT_PLAN.md) §5.2
> (entity-context reduction is the primary latency lever).

Traced against the local clone: `ha-core/homeassistant/components/assist_pipeline/pipeline.py`
(2259 lines) and `.../anthropic/entity.py`.

---

## TL;DR

- Audio → STT is **streamed** (chunk-by-chunk during capture; VAD closes the
  stream). It is NOT buffered-then-sent.
- STT → LLM is a **real serialization point** — the LLM gets the full transcript.
  This is inherent and correct; do not try to stream around it.
- LLM → TTS **is streamed** (token deltas → TTS), past a char-count threshold.
  This changed in 2025 (the "9–13× streaming TTS" work).
- **Latency at the STT→LLM junction is LLM prefill (time-to-first-token),
  dominated by the big static prompt — not the user's words.** The real
  performance lever is shrinking that prompt (PRODUCT_PLAN §5.2), which is a
  TTFT feature, not just a token-cost one.

---

## The three stages

```
Audio ──(streamed, chunk-by-chunk; VAD closes stream)──▶ STT ──▶ [full transcript]
                                                                       │  ← serialization point
                                                                       ▼
                                              LLM ──(token deltas)──▶ TTS ──▶ audio out
                                                    (streamed once past a char threshold)
```

Streaming at both ends; one unavoidable sync point in the middle.

### Stage 1: Audio → STT is **streamed**, not buffered-then-sent

The common belief ("HA waits for VAD to end, then sends the entire audio to
STT") is wrong. The STT engine is fed a live async generator of audio chunks
*during capture*:

- `stt_provider.async_process_audio_stream(...)` receives `_speech_to_text_stream`,
  which `yield`s each chunk as it arrives (`pipeline.py:959`, `:1003`).
- VAD runs **per chunk**; when it detects end-of-speech silence it `break`s and
  closes the stream (`pipeline.py:1017`). VAD decides *when to stop streaming* —
  it does not gate a bulk upload.

The grain of truth: HA consumes a **single final transcript** from that stream
(the `stt` provider interface returns one `SpeechResult` at stream end). Audio
streams in live, but the *text* pops out once, at end-of-speech — because you
generally can't act on half an utterance.

> Engine nuance: even though HA streams chunks in, a *batch* STT engine (e.g.
> plain Whisper) buffers them and transcribes in one pass at VAD end, so the
> transcript still appears only after you stop speaking. A *streaming* STT engine
> processes incrementally and returns the final transcript faster. This is an
> engine property, not a pipeline fix.

### Stage 2: STT → LLM is a **real serialization point**

The conversation agent is invoked with the complete transcript:
`ConversationInput(text=intent_input)` (`pipeline.py:1210`). This is the one
genuine "wait for the previous stage to finish" junction, and it's inherent —
intent recognition / the LLM prompt need the whole utterance, and the LLM won't
emit its first token until it's read the full prompt anyway. It does not stream
partial transcripts into the LLM, and that's a reasonable design, not a missing
feature. (See "Latency" below for why streaming here wouldn't help.)

### Stage 3: LLM → TTS **is streamed**

If the TTS engine supports streaming input *and* the conversation agent
advertises streaming, the pipeline wires the LLM's token deltas straight into
TTS:

- Sets up a `tts_input_stream` queue when `tts_stream.supports_streaming_input`
  (`pipeline.py:1122`).
- A `chat_log_delta_listener` catches assistant-content deltas as the LLM
  produces them and pushes them into the queue (`pipeline.py:1128`, `:1154`).
- TTS synthesizes from that generator incrementally via
  `async_set_message_stream` (`pipeline.py:1208`) — so it starts speaking before
  the LLM finishes.

**Threshold nuance:** streaming to TTS doesn't start on the first token. There's
a threshold (`pipeline.py:1156–1179`): it buffers until it's seen
`STREAM_RESPONSE_CHARS` characters (a "this will be long" signal) or a
tool-call-after-text, *then* flips to streaming and flushes the buffered parts as
the first chunk. The reason (comment at `:1161`): **streamed responses aren't
cached**, so short replies are kept whole (cacheable, spoken as one message) and
only long ones get the streaming path.

### What this means for our project

We largely get Stage-3 streaming **for free**. Because we're forking the
Anthropic conversation entity — which sets `_attr_supports_streaming = True` and
emits chat-log deltas — we inherit output streaming as long as we preserve that
delta-emitting behavior in the chat loop and don't collapse the response into a
single blob before returning.

**Constraint for the Phase 0 skeleton:** keep `supports_streaming = True` and
keep emitting deltas; don't buffer the whole response internally. **This is also the
barge-in constraint** — a response buffered into one blob can't be cleanly cut off
mid-sentence when the user interrupts ("stop"); streaming is what makes a long reply
cancelable ([`conversation-loop.md`](conversation-loop.md) §Barge-in). The user's TTS
engine must support streaming input (else it cleanly falls back to
speak-after-complete). The only latency we can't stream away is the Stage-2
handoff plus the LLM's time-to-first-token — which is exactly why the
streaming-TTS threshold exists: to hide long-generation latency behind early
speech.

---

## Latency: it's prefill, not transport

Verified: the Anthropic integration **already** does the obvious cloud win — it
wraps the system prompt (which contains the static entity context) in
`cache_control: ephemeral` (`entity.py:963–972`), gated by `CONF_PROMPT_CACHING`.
So prefix caching exists. That tells you where the real latency is, and it isn't
in transport.

### Streaming the transcript into the LLM is a dead end

Three reasons, and they compound:

1. **Prefill needs the whole utterance.** An LLM request is *prefill* (process
   all prompt tokens → build KV cache → first token) then *decode* (generate).
   You can't prefill a sentence you haven't finished receiving in a way that
   helps, because intent depends on the tail.
2. **Partial transcripts are unstable.** "turn on the…" → "turn off the…". Any
   speculative prefill on a partial would get invalidated constantly. And HA's
   STT interface returns a *single final* `SpeechResult` anyway — there's no
   partial-hypothesis stream to consume even if you wanted to.
3. **The utterance is a rounding error in the token count.** "Turn off the
   kitchen lights" is ~6 tokens against a prompt of thousands (system + entity
   context + tool defs + history). Prefilling it early saves almost nothing.

Tokenization is the same story: it's sub-millisecond and never the bottleneck.
The expensive thing is the *forward pass over the prompt tokens* (prefill), not
turning text into tokens.

### Where the latency actually lives — and what's contributable

Time-to-first-token ≈ prefill time ≈ **dominated by the big static prefix**, not
the user's words. So the real levers are about the *static prompt*:

**1. Make the prompt smaller — the biggest, most universal win.** Every token cut
from the entity context and tool defs cuts prefill for *every* request, cloud and
local, first utterance included (caching doesn't help a cold prompt). This is
exactly the entity-summary + `find_entities` work in PRODUCT_PLAN §5.2. So our
own context-reduction design **is** the performance contribution to core — it's
not just a token-cost thing, it's a TTFT thing, and it has the broadest benefit.

**2. Warm the prefix *before* the transcript arrives — real but harder, mostly a
local-model story.** The static prefix is known before the user even speaks. In
principle you could kick off prefill of it at wake-word detection, overlapping
with the ~1–3 s of speech, so the KV cache is warm when the transcript lands.
Honest caveats:

- **Cloud:** low value. Anthropic's cache is *reactive* (populated by request N,
  reused by N+1 within the ~5-min TTL) — it already covers multi-turn and does
  NOT help the cold first utterance. You *could* fire a cache-priming request at
  wake-word to warm it for the first turn, but that's an extra billed request for
  a marginal gain. Probably not worth landing generically.
- **Local:** genuinely compelling. On a Pi/local model, prefilling thousands of
  prompt tokens is slow and dominates TTFT. Overlapping that prefill with the
  user speaking could hide seconds. But it needs the inference backend to expose
  "prefill without generating," and HA's `conversation` abstraction has no hook
  for it today — so it's a deeper architectural change, not a small PR.

### A third lever: skip the LLM entirely (`prefer_local_intents`)

Distinct from prompt-shrinking: with `prefer_local_intents` on (PRODUCT_PLAN §2.9), a
strict hassil match (the bulk — TurnOn/Off, timers, etc.) is handled **locally with no
LLM round-trip at all** → *zero* prefill/TTFT for the common command. Only misses and the
two deferred intents (`GET_STATE`, `MEDIA_SEARCH_AND_PLAY`) pay the LLM latency. So the
fastest path isn't a smaller prompt — it's **no prompt**, and every "helps-local" intent
we contribute widens that fast path (dual-payoff, §2.9). Prompt-shrinking then governs the
*residual* that genuinely needs the model.

### Verdict

- Streaming STT→LLM: **no gain, correctly.** Don't chase it.
- The junction's latency is LLM prefill; the contributable win is **shrinking the
  static prompt** (already planned, §5.2), plus — more speculatively —
  **overlapped prefill for local models** (needs new plumbing HA doesn't have).
- **The bulk of commands can skip the LLM outright** via `prefer_local_intents` — the
  cheapest latency of all (§2.9).
- Cloud prefix caching is already handled; it just doesn't help cold starts.

**Pitch framing for a core contribution:** "the entity-context reduction isn't
only about token cost — it's the primary TTFT lever, and unlike prompt caching it
helps the first utterance and local models too." Stronger than a streaming
change, and it's work we're doing anyway.

## Evaluation gate

Unit and conversation tests must prove that provider deltas remain incremental and that
cancellation joins in-flight generation/tool work. Provider-round timing in the text harness
is sufficient for a model TTFT claim. A voice-experience claim requires the controlled
pipeline driver to observe first intent progress, TTS start/end, and cancellation while TTS
is active. Absolute end-to-end latency and spoken-duration thresholds must name the STT/TTS
engines, hardware, cache state, and satellite profile used; mock-engine timings prove wiring,
not user-perceived performance.
