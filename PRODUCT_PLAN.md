# Product Plan — LLM-Backed Voice Assistant for Home Assistant

> Working design document. Captures the architectural findings, decisions, and
> strategy from initial exploration. Living doc — expect to drill into individual
> sections and revise. Not a build spec yet.

---

## 0. Companion documents

Per-feature / per-topic deep-dives live in [`docs/`](docs/) so this plan stays an
overview. As we drill into a feature (memory, speaker identification, todo, …) it
gets its own file there. Current docs:

- [`docs/external-agents-openclaw.md`](docs/external-agents-openclaw.md) —
  build-vs-delegate decision re: OpenClaw / external agent platforms. **Not**
  building on it; the two delegate patterns (build-on ❌ vs thin front-end ✅)
  look identical but differ in who owns the agent loop.
- [`docs/voice-streaming.md`](docs/voice-streaming.md) — how the pipeline streams
  (STT in / LLM→TTS out, with a serialization point between); why latency is LLM
  **prefill**, not transport; and why §5.2 context-reduction is the real TTFT
  contribution. Phase-0 constraint: keep `supports_streaming` + delta emission.
- [`docs/evaluation.md`](docs/evaluation.md) — live **tracing** vs offline
  **evaluation** (related, distinct); the two trace systems reconciled; core has
  no LLM eval tooling (only building blocks); how to prove a prefill/caching
  change keeps quality (prompt-token equality) while improving TTFT/TTLT; eval
  harness design. **Two testing tiers:** (A) **deterministic subsystem tests** (Part G) —
  ordinary pytest, exact, CI-blocking, for the non-LLM machinery (scheduling substrate,
  delivery, undo, scorer, memory store); the durable-reminder **time/restart/DST simulation
  harness** (freezer + `async_fire_time_changed` + restore-cache — the property list in
  `scheduling-model.md` *is* the test spec; Calendar Trigger tests use the same pattern) is
  the highest-stakes piece; plus a performance/scale harness. (B) **probabilistic LLM eval**
  (Parts D–E). Reporting = an **outcome scorecard** (resolved-locally / LLM-correct /
  after-clarification / wrong / "don't understand" + tokens/gens/turns + local-vs-LLM split) —
  the same instrument as build-sequence's **value dashboard**; a **metric × scope** matrix
  (full hassil→LLM path vs LLM-only × correctness/turns/tokens/latency/**helpfulness**), with
  helpfulness on **LLM-as-judge**. **Prior art (Part H):** an ad-hoc HA community benchmark
  exists (proves demand + the "friendly name helps" thesis, but stands outside the pipeline),
  plus corpora (SMH-Bench/HomeBench/HomeFlow/VISTA) and OSS frameworks (DeepEval pytest-native
  is the dev-harness reuse candidate) → **reuse-vs-build** split (heavy framework for our dev
  harness, thin pytest+corpus+scorer for core). The harness is **feature-decoupled → a
  merge-first core contribution** (§7). Work-items: eval harness, deterministic+timing harness,
  trace enrichment, prior-art/reuse decision, frontend UI fixes.
- [`docs/build-sequence.md`](docs/build-sequence.md) — **order and proof, not design.** The
  build **prioritization** (three axes: necessary scaffolding/observability · features that
  *objectively prove* value via the harness · low-hanging magic) braided into **waves**, with
  per-item **component-vs-core** tags and thin-slice scope. Two principles: **measurement
  precedes optimization** (Wave 0 = walking skeleton + the instrument + a **baseline** run of
  the stock fork; every later change is a measured delta at fixed task-success) and **seam
  early / engine just-in-time**. Key reframe: the value-proving features *are* the §5.6
  primitives (prompt-context=tokens, `find_entities`=turns, learning=local-rate), so
  "primitives first" = "prove value first." The **value dashboard** (tokens/gens/turns/
  hassil-rate) = [`evaluation.md`](docs/evaluation.md) Part E's scorecard — one instrument.
  Reorders §8 (token/turn proof to the front; notebook-memory → cheap delight).
- [`docs/speaker-identification.md`](docs/speaker-identification.md) — voice-ID as
  an *input* to `resolve_user()` (§5.1). Model is easy/community-proven; core has
  none and there's **no** formal attempt/ADR (only unanswered Discussion #527);
  the six reasons; the graduated-assurance model (personalization ≠ binary auth)
  and the refined "never grants HA permissions" invariant; how confidence scores
  actually work (tuned cosine + top-1/top-2 margin, not a model probability); and
  a self-contained **adaptive-enrollment** note (valid idea; tracking-drift ≡
  poisoning; supervised re-anchor). Deferred to Phase 4.
- [`docs/music-playback.md`](docs/music-playback.md) — play-by-voice works via the
  native `HassMediaSearchAndPlay` intent (plays best result; needs Music Assistant
  as search backend); per-capability table; the UX principle **optimistic play,
  not clarify-first** (the `find_entities` disambiguation parallel *inverts* for a
  huge catalog); native search shipped (Voice Ch.10), assistant-grade gaps
  (what's-playing / like / queue) remain with no roadmap = our custom-tool space.
- [`docs/conversation-loop.md`](docs/conversation-loop.md) — "continued
  conversation" (mic reopens after a turn, skips wake word); default "?" heuristic
  is crude; VAD timeouts (~0.7 s silence / ~15 s cap) auto-close; our design =
  default-continue for Q&A + **deterministic stop after commands**, shorter
  follow-up timeout, and a **spurious-gate** on reopened turns (the piece that
  makes liberal reopening safe). Also the multi-turn `ChatLog` home. **Barge-in**
  (interrupt mid-response — "stop", or a new question over the top) lives **below the
  agent** (satellite + pipeline; the LLM never sees a "stop") → **inherited**; our job is
  keep-streaming (so long replies are cancelable) + stop-words stay **local**
  (`HassNevermind`, offline-safe).
- [`docs/ambient-noise.md`](docs/ambient-noise.md) — white/pink/brown noise (+
  eventual nature sounds) on endless repeat by voice. Own capability, **not** the
  music path (streams/generated, not library). Play direct from HA (no MA) via
  `play_media` + a component-served URL; endless via a **looping stream view**
  (not player-dependent `repeat`). Cloud stream URLs **won't merge to core** →
  local synthesis / small bundled noise files (precedent: `acknowledge.mp3`);
  nature sounds user-provided. `play_ambient(sound, target, duration?)` + sleep
  timer.
- [`docs/scheduling-model.md`](docs/scheduling-model.md) — **architecture spine**
  for all time-based features (timers/alarms/reminders/todos/calendar as one
  design space). Timers *do* carry payloads (`conversation_command`). HA already
  has the *trigger* half (Calendar Trigger, `create_event`, RRULE via `ical`); the
  missing universal piece is **Assist delivery** (announce/notify/command +
  snooze/ack). Delivery's **output primitive is `assist_satellite`'s service API**
  (`announce` + `preannounce` earcon for the ambient content-free case,
  `start_conversation` for solicited/proactive), not the low-level timer
  `register_handler` (§2.8); `SatelliteBusyError` is a *defer* state in the ack/escalation/
  queue machine. Store choice = user's visibility/sync intent (native-private
  default; real calendar only on explicit cue). Unifying move: native store
  implemented **as a `CalendarEntity`** = private store + optional view +
  trigger-source at once. Includes a **Triggering implementation** section:
  reuse HA's Calendar Trigger (interval-cursor + point-in-time alarm), which
  punts on missed-while-down → add a **persisted watermark** + **two-knob
  catch-up** (grace = time filter, collapse = recurrence filter) + watermark-last
  ordering + catch-up-is-informational. `timers.md`/`reminders.md` are thin docs
  over this.
- [`docs/ephemeral-automations.md`](docs/ephemeral-automations.md) — distinct FR:
  transient LLM-authored `{trigger, condition, action}` rules ("in 5 min **if** the
  door's still open," "**when** the laundry's done"). LLM authors at **creation**,
  deterministic eval at **fire** (not LLM-at-fire). Justified by **state triggers
  unlocking** event-driven rules a timer+LLM can't do (not cost/determinism).
  Reuse HA's engine (`async_initialize_triggers` + `condition.async_from_config`),
  **not** automation entities. Two trigger backends → one condition→delivery
  pipeline: time (our watermark) + state (HA's engine; watermark is time-only).
  LLM-at-fire survives only for non-compilable fuzzy conditions.
- [`docs/find-entities.md`](docs/find-entities.md) — fuzzy entity resolution →
  **canonical `entity_id`**, fixing the LLM path's **exact** name match
  (`_filter_by_name`, §2.4; `entity_id` accepted verbatim at `intent.py:428` = the
  seam). **Reframe:** the device-control fix is a **fuzzy fallback inside the match
  layer** (runs only *after* an exact NAME miss, behind an opt-in `fuzzy=` flag so
  the hassil path is untouched), **not** a mandatory front-loaded tool — proven by
  **generation-counting**: a tool call is never terminal (`stop_reason:tool_use` →
  HA loops `anthropic/entity.py:1201`), so any command is ≥2 model generations;
  in-match fuzzy hides inside the *one* `tool_use` (2 gens, = exact-match cost)
  while a mandatory `find_entities` costs **3 gens on every command**. Ambiguity →
  return candidates in the `tool_result`; the disambiguation round-trip works **out
  of the box** via continued-conversation + chat-session (`conversation_id`, 5-min
  TTL, `?`-reopen — the crude heuristic `conversation-loop.md` upgrades). Rejected
  lever: **terminal fire-and-forget intent** (= the hassil path; LLM path loops
  instead for contextual confirmation/failure/chaining). `find_entities`-**the-tool**
  survives only for **decoupled resolution** (ephemeral automations / reminders
  authoring `{trigger,condition,action}` for *later*, browsing). Shared primitive =
  **scorer + top-1/top-2 guard** (rapidfuzz `token_set_ratio`), **two consumers**.
  Disambiguation policy **inverts** music's optimistic-play. rapidfuzz = **new dep**
  (difflib fallback). Phase 0, **first core PR**.

- [`docs/prompt-context.md`](docs/prompt-context.md) — the **LLM I/O contract**
  (output half of the §5.6 prompt-context primitive; input/taxonomy half still to
  scope). No structured envelope today — a **hybrid**: actions = `tool_use`, spoken
  message = free-text stream, control signals = the "?" heuristic. **Generation
  model:** a `tool_use` is never terminal (`stop_reason:tool_use` → HA loops
  `anthropic/entity.py:1201/1250`), so any tool command is **≥2 generations**. A
  return-**struct** doesn't kill streaming (order `message` first + incremental
  parse) but costs parse-complexity; **typed blocks already separate channels
  out-of-band**, so prefer them (sentinel/JSONL are single-channel/local-model
  framing; JSONL+**grammar** is the local fix). The **one real 1-generation win** is
  a `terminal_intent` **struct field** (not a tool — a `tool_use` forces the
  round-trip) with **optimistic-execute + fallback-to-gen2-on-failure**; async
  tools (background task + stub result) save tool latency, not the generation.
  **Never** a `set_metadata` tool (wasted 3rd gen). Filler policy: no preamble for
  fast actions, **earcon** for ack, spoken filler only for slow tools. Shell is
  throwaway (§5.5) so a struct shell is fine **if** capabilities stay tool-shaped.
  **Input/prompt-budget half:** today's full-entity-roster dump (~8–20k tok for a
  500–1000-entity home, re-prefilled **per generation**) is a **crutch for
  exact-match** — redundant once resolution is fuzzy. Replace with **tier-1
  taxonomy skeleton** (bounded by structure) + **tier-2 request-conditioned name
  injection** (room-scoped via `device_id→area` — floor too coarse — ∩ domain-
  keyword + fuzzy-name; = `find_entities` run **proactively**, its *third
  consumer*; keyword+fuzzy not embeddings per §5.3; misses degrade to one lookup)
  + tier-3 retrieval (memory only). **Cache is within-conversation / within-
  generation-loop, not cross-conversation** → stable cached prefix (instructions +
  tools + skeleton) + uncacheable tail (names + history + memory); the **cold first
  utterance** is the uncached TTFT villain that pruning fixes. **Measure end-to-end**
  (tokens ×generations + TTFT/TTLT at fixed task-success; quality guard = task-
  success equality, *not* token equality); cache hit rate is free from Anthropic
  `usage` (`cache_read`/`cache_creation`). **Fleet/phone-home telemetry** (validates
  priors: room-locality, burstiness, home-size) is opt-in + content-free, lives in
  proving-ground/Nabu Casa, **not core**.

- [`docs/memory.md`](docs/memory.md) — the Phase-2 differentiator, **shrunk** to
  what real users ask for. Two products wear the name: *confidant* (scales with
  conversation frequency the tactical home lacks → inert) vs *substrate* (serves
  the tactical commands, needs no conversation). **Value model inverts** the
  chatbot's — memory **removes future turns** (clarification skipped/command
  shortened), not enables longer ones. Evidence from four shipping community
  artifacts clusters on the **notebook** (parking, wifi, keys, codes, pet name —
  explicit write + explicit recall, FTS); confidant framing is **vendor marketing
  only**, architecture repo is **silent** ([#1068](https://github.com/home-assistant/architecture/discussions/1068)),
  and home-mind's praised "memory" is really **deterministic context injection**
  (§5.2, already scoped). **Negative definition + route-to-structured rule:** memory
  = the *residual* (appointments→reminders, "call it X"→**alias**, units→settings,
  layout→§5.2). **Two feels-like-memory primitives kept distinct:** free-floating
  notebook (explicit recall) vs **entity-attached facts** (aliases + annotations,
  injected via §5.2 tier-2). **Aliases feel like memory but are a config write** —
  fix exact-match (§2.4) at the source, UI-editable, and **dissolve
  disambiguation-learning** (state it once, no multi-turn inference). **No silent
  inference** (disambiguation-learning + annotation auto-capture rejected as the
  same un-HA failure). **Annotations** ("100 ppm is normal here") = deferred corner
  case (not explicit-recall → needs entity-join injection; write-trigger hard;
  route-to-structured → threshold edit). Multi-user = personal/household scope on
  `resolve_user()` (§5.1). Phase 2 = **notebook only**.

- [`docs/learning.md`](docs/learning.md) — **the friction-resolution primitive, split out
  of memory.** The offer machinery ("recognize confusion → offer a durable fix → confirm →
  persist") isn't memory-specific: *memory* = user-writes/user-recalls (the notebook);
  *learning* = **friction-triggered offer + machine-triggered consumption** (an alias the
  matcher applies, a rewrite the pipeline applies — never "recalled"). **Shared thing is the
  offer engine, not storage** — each fix-sink owns its store (entity registry / **YAML** /
  FTS column; route-to-structured). Mechanism = a **`FrictionResolver` provider registry**
  surfaced via HA's dynamic tool-list gate (`async_get_tools`, §2.5) on a *friction*
  predicate — portable, and the core loop needn't know the resolver set. **Detector is
  typed** → routing to the right resolver is near-free (v1 needs no filter). New member =
  **command aliases** (phrase→phrase rewrite applied pre-agent): buys **routing stability
  under skill-growth** (pins a phrase to a route regardless of competitors; install-time
  **collision** is a proactive non-semantic detector) *and* can **move an utterance off the
  cloud path** (rewrite→local intent short-circuit, §2.9/offline). Verified `ha-core/`: HA
  has phrase→**action** (conversation triggers / `intent_script`, matched before intents,
  no LLM) but **not** phrase→phrase rewrite nor the offer-to-learn layer. Each resolver
  declares its **inverse** ([`undo.md`](docs/undo.md)); SKILL text biases hard to **silence**
  and is a **localization** gate (§5.7) for core.

- [`docs/calendar.md`](docs/calendar.md) — calendar as a capability: the existing
  read tool (`calendar_get_events`) + the **write** surface we add. Verified `ha-core/`
  CRUD asymmetry: **CREATE** is a proper service (`calendar.create_event`) broadly
  supported (Google/CalDAV/local); **UPDATE/DELETE are websocket-only** (no service) and
  thin — **UPDATE is `local_calendar`-only** (Google/CalDAV can't). **No create LLM tool
  exists today.** Scope: **CREATE v1** (thin Tool doing datetime-normalization +
  calendar-selection + name-it-back), **DELETE fast-follow** (needs a new
  `calendar.delete_event` service + event-resolution via read→fuzzy-over-summaries→
  disambiguate), **UPDATE punt**. Meets [`scheduling-model.md`](docs/scheduling-model.md)
  at `create_event`: the native reminder store is itself a `CalendarEntity` w/
  `CREATE_EVENT`, so one create flow writes native-or-real (`ScheduledItem.store` routes)
  — calendar-write and the reminder store are **not separate builds**.

- [`docs/todo.md`](docs/todo.md) — **thin.** Base todo is **already done** (add/
  complete/remove intents + `todo_get_items` read tool exposed today). Verified: the
  entity supports rich edits (UPDATE/MOVE/SET_DUE/DESCRIPTION) but only add/complete/
  remove are wired to intents; **todos never fire on due** (trigger is list-mutation).
  The one design point is settled upstream — **firing = reminder, passive list = todo**;
  a task the user wants listed *and* nudged is a **reminder with `store = todo(<list>)`**
  (native store owns firing; todo item is the visible copy, calendar pattern). Residual
  (low-priority): rich-edit intents, list-selection default.

- [`docs/weather.md`](docs/weather.md) — **current-conditions vs. forecast split is
  the whole story.** Current conditions already work (native `HassGetWeather` intent +
  `temperature`/`humidity` in the LLM attribute allowlist). **Forecasts are gapped on
  both paths**: they live behind the `weather.get_forecasts` **service** (not state,
  moved out of attributes 2023.9), `HassGetWeather` doesn't call it, there's no forecast
  intent/tool, and the Assist LLM API can't call arbitrary services. **Build = a
  `get_forecast` tool** wrapping the service with **deterministic date-range handling**
  (§5.4, like calendar): LLM passes "this weekend", tool resolves dates, picks
  daily/hourly, filters, returns structured periods the LLM **summarizes** (forecast is
  summarization-shaped → LLM-leaning, not a canned template). Overlaps web-search but the
  **local forecast tool wins** for home weather (grounded/structured/private).

- [`docs/web-search.md`](docs/web-search.md) — **surprising: on the cloud backend it's
  already built.** The stock `anthropic` component wires Anthropic's **server-side**
  `web_search` + `web_fetch` (`entity.py:1035–1074`, `server_tool_use`); our delivery
  **forks that shape → inherits it**. Server-side executes **mid-stream in one request**
  → **no HA loop round-trip** (generation-counting win vs a client-side tool's ≥2 gens).
  Real work is **(a) a portability seam** — server-side is Anthropic-coupled (lives in
  the delivery-engine, not an `llm.py` platform); a provider-agnostic/local path needs a
  **client-side tool** (SearXNG/Brave/Tavily) — the same **framing-per-backend** split as
  prompt-context — and **(b) enablement/defaults**: ships **off** (`const.py:51`),
  `max_uses=5`, `user_location` **hand-typed** → auto-fill from `hass.config`
  (determinism-in-tools). Prompt policy = **prefer local capabilities first** (esp.
  `get_forecast` over web_search for home weather).

- [`docs/reminders.md`](docs/reminders.md) — **thin, over the spine.** Reminders + alarms
  are the primary consumers of the durable delivery engine; they **share everything** and
  differ only by **preset** (user-invoked → legible, *not* inferred stakes). Escalation
  strategy = **person-vs-target geometry**: **timer** you're at it → none; **alarm** at it
  but asleep → escalate *intensity* (loud/persistent; broadcast can't wake a sleeper);
  **reminder** location unknown → escalate *reach* (content-free broadcast). Both **require
  persistence** (trust). Build = one **`create_reminder`** tool (normalize time → pick
  store via visibility-intent, `ScheduledItem.store` routes native-vs-real-calendar so this
  and calendar-write are **one build** → name it back; behavioral write → confirm).
  Recurrence stays **simple** (one-shot/daily/weekly/interval; punt complex RRULE +
  sharing to the real calendar). **Broadcast/intercom** = thin immediate-announce consumer,
  not a subsystem.

- [`docs/timers.md`](docs/timers.md) — **thin, descriptive.** Base timers **already ship
  and are LLM-exposed** (start/cancel/pause/adjust/status/named/multiple, gated on
  device-supports-timers). Ephemeral **by inheritance from core**, not conceptually — the
  **short-catch-up-grace corner** of the one model. v1 leaves them on core's path (they
  work; live-countdown/precision runtime is timer-specific); **durable-backed timer**
  (persist record + in-memory firing + short grace) is a cheap future migration.
  Sleep-timer is a *different* consumer (see `ambient-noise.md`).

- [`docs/offline.md`](docs/offline.md) — **cross-cutting.** The cloud-Claude-in-a-local-
  first-home tension: what works when the model is unreachable. **Two axes:**
  *runtime-offline* (a scheduled item must fire while the cloud is down) is **mostly solved
  by construction** — the LLM-authors-once / deterministic-runtime split means
  reminders/timers/ephemeral-automations **fire with no LLM** (distinct from the "HA was
  **down**" catch-up story); *request-offline* (a live utterance can't reach the cloud) is
  shrunk by `prefer_local_intents` (§2.9) to the residual. **Today (verified):** on API
  error the user hears the raw *"Anthropic API error: …"* (not the local no-match text),
  and there is **no retry/second local pass**. Build = a **second local pass on connection
  failure** (honor the deferred `GET_STATE`/`MEDIA_SEARCH_AND_PLAY` local match; conservative
  non-strict retry) + **legible degradation** (vendor-neutral message, transient-vs-auth,
  connection-health short-circuit). Discipline = keep pushing capability into
  compile-once/run-deterministic; runtime-LLM capabilities (web search, Q&A) are knowingly
  offline-fragile. Local-model fallback = speculative. **Whole-pipeline caveat:** a cloud
  LLM usually means cloud **STT+TTS** too — cloud STT down = *no transcript reaches
  hassil* (local rescue presupposes local STT); cloud TTS down = can't even speak the
  error. Biggest lever = a **fully-local pipeline** (Piper/Whisper); mitigations =
  **pre-cached system phrases** (TTS disk cache, in the chosen voice) + **pre-rendered
  reminder content at creation** + earcon-alert is TTS-independent. Firing when TTS is
  down risks a **partial-delivery trap** (ack'd but content un-playable) → *an ack that
  can't deliver isn't an ack; keep queued.*

- [`docs/security.md`](docs/security.md) — **cross-cutting, highest-severity gap.**
  **Indirect prompt injection:** untrusted text (web pages, calendar-invite titles,
  device `friendly_name`/`media_title`, foreign notes) enters context and may carry
  instructions the model follows — "unlock the door," "delete her appointments," exfil.
  **Saving grace:** this is *not* a general computer-use agent — sinks are **exposure-
  bounded intents** (§2.2–2.5) and determinism-in-tools (§5.4) caps the model's authority,
  so injection reaches only what we exposed, mostly reversible. **Stance = blast-radius
  control, not detection** (unsolved): L1 least-privilege exposure (don't expose
  locks/alarm by default), L2 high-consequence behind an **injection-independent gate**
  (confirm/PIN), L3 **taint model** (untrusted-in → restrict dangerous sinks; the
  principled form of the **`allow untrusted` toggle** — web retrieval off by default,
  split `web_search` vs `web_fetch`), L4 provenance-labeling, L5 optional **local guardrail
  classifier** (Prompt Guard / Llama Guard / NeMo *do* exist, local-runnable; Claude has no
  configurable guardrail product), L6 egress/**SSRF** hardening (server-side web_fetch = no
  LAN SSRF; client-side must block private IPs). **Honest:** no complete defense — minimize
  what a success can *do*. Same bright line as speaker-ID (exposure is the bound).

- [`docs/undo.md`](docs/undo.md) — **cross-cutting shared primitive** the §5.6 method
  surfaced late: "undo that / turn it back / forget that." **Deterministic undo, not
  LLM-reconstructed** — the LLM only *recognizes intent to undo*; the reversal is a
  **command-pattern journal** replay (each mutating tool records **its own inverse** at
  execute time; §5.4 applied to reversal). HA hands us the device-control case:
  `scene.create` **`snapshot_entities`** + `scene.apply` restore full prior state, and
  intents expose the affected set (`success_results`). Inverses: device = snapshot/restore;
  memory = delete/restore-prior; create-actions (reminder/calendar/todo/alias/automation) =
  delete-by-id; calendar delete = recreate-from-saved. **Recognize as a *local intent*
  (`HassUndo`-shaped)** — deterministic, offline-safe (a locally-handled action must be
  locally undoable, [`offline.md`](docs/offline.md)). Distinct from **`HassNevermind`**
  (abort in-progress), music "next" (re-query), and snooze. **Undo underwrites the
  *optimistic* execution paths** (terminal-intent fast path, optimistic memory/music) and
  **operationalizes [`security.md`](docs/security.md)'s reversibility argument.** Not
  everything is undoable (irreversible side-effects, world-moved-on, read-only) → decline
  legibly. v1 = single-level; "declare your inverse" becomes part of the capability
  contract (§5.5). No undo in core (verified).

_All planned topic docs now written._

---

## 1. Vision

Build an LLM-backed voice assistant layer for Home Assistant that is roughly
equivalent to Google Home / Alexa in capability, but local-first-friendly and
privacy-respecting in the HA tradition. Beyond device control, it should offer
"true assistant" features: long-term memory, time-based reminders, calendar
write access, and richer information retrieval (weather forecasts, web search).

**End-goal:** land the capabilities in HA **core**.
**Short-term:** iterate fast as a **custom component**, structured so migration
to core is as close to copy/paste as possible.

---

## 2. How HA Assist / LLM integration actually works (foundational findings)

These are the load-bearing facts the whole design rests on. File references are
into the local `ha-core/` clone.

### 2.1 Tools are provider-agnostic; the provider integration is a thin adapter
- A `Tool` is just `name` + `description` + `parameters` (voluptuous schema) +
  `async_call()`. Nothing provider-specific. (`homeassistant/helpers/llm.py:158`)
- The `anthropic` integration contains **no** tool definitions. At request time
  it reads `chat_log.llm_api.tools`, converts each via `voluptuous_openapi.convert()`
  into a JSON-Schema `input_schema` (`components/anthropic/entity.py:148`), and
  passes them as `model_args["tools"]`. When Claude returns a `tool_use`, it
  routes back through `chat_log.llm_api.async_call_tool`.
- **Implication:** any LLM integration (Anthropic, OpenAI, Google, Ollama) gets
  the same tools for free. Capabilities belong in the tool layer, not the adapter.

### 2.2 Entity control happens through *intents*, not entity IDs
- There is no `turn_off_lights` tool. Domains contribute `IntentTool` wrappers
  around HA intents (`HassTurnOn`, `HassLightSet`, …). `IntentTool.async_call`
  dispatches to `intent.async_handle`. (`helpers/llm.py:223`)
- `ActionTool` wraps a service/action directly; currently used for exposed
  **scripts**. (`helpers/llm.py:614`)
- The Assist API (`components/llm/__init__.py:94`) aggregates tools from every
  integration exposing an `llm.py` platform via `async_get_tools`
  (`LLMToolsPlatformProtocol`). Recent refactor — tools are contributed per
  integration, not hardcoded in one class.
- The exposed-entity list in the prompt is **context, not tools**. The
  `homeassistant` platform dumps a static YAML overview (names + aliases +
  area + domain, **no state**) and contributes `GetLiveContextTool` for
  on-demand state. (`components/homeassistant/llm.py:318`)

### 2.3 The LLM emits *constraints*, not entity lists — this reframes context cost
- Intent slot schema is `name` OR `area` OR `floor` (one string) + optional
  `domain` / `device_class` lists. (`helpers/intent.py:948`)
- "Turn off everything downstairs" = `HassTurnOff(floor="downstairs")`. The
  fan-out to N entities happens **server-side** in `async_match_targets`
  (`helpers/intent.py:510`). The model never emits 1000 entities.
- `name="all"` = "don't constrain by name" (`intent.py:1016`). A fully
  unconstrained target is **rejected by design** ("Service handler cannot target
  all devices", `intent.py:1048`) — whole-house sweeps must be scripts/scenes.
- **Consequence:** area/floor/domain commands need *zero* entity names — only the
  taxonomy (which floors/areas/domains/device-classes exist). That's small and
  bounded regardless of home size. Entity *names* only matter for device-specific
  targeting.

### 2.4 Name resolution in the LLM path is EXACT, not fuzzy
- `_filter_by_name` compares `name.strip().casefold()` against entity name +
  aliases — exact match, no edit distance. (`helpers/intent.py:413`, `436`)
- So if Claude emits "reading light" for an entity named "Reading Lamp", it
  **fails** (`MatchFailedReason.NAME`). Entity-id is accepted directly
  (`intent.py:428`).
- **hassil is NOT in the LLM path.** hassil is the local/default agent's
  sentence matcher. Its "fuzzy" matching (hassil 3.8, `fuzzy.py`) is an n-gram
  language-model score over the *carrier sentence*, with entity names matched
  **exactly** via a trie (`fuzzy.py:453`) — it does not edit-distance "reading
  lamp" ↔ "couch lamp"; both are distinct trie entries. It has an ambiguity
  guard: returns `None` if top-2 intents are within `MIN_DIFF_SCORE`.
- **Consequence:** in the LLM path *we* are the natural-language layer, and there
  is no fuzzy safety net downstream. Either the exact name must reach the model,
  or a lookup tool must return the canonical `entity_id`. → motivates
  `find_entities` (§5.2).

### 2.5 What's exposed to the LLM today (15 `llm.py` platforms)
| Group | Integration → tools |
|---|---|
| Device control (IntentTool) | `intent` (generic: TurnOn/Off, SetPosition, StopMoving, CancelAllTimers + 7 timer intents if device supports timers), `light` (LightSet), `fan`, `climate`, `humidifier`, `media_player` (9 tools), `vacuum`, `lawn_mower`, `todo` (add/complete/remove), `assist_satellite` (Broadcast only), `intent_script` (user-defined) |
| Read/context (custom Tool) | `homeassistant` (`GetLiveContextTool` + static dump), `calendar` (`calendar_get_events`, **read-only**), `todo` (`todo_get_items`) |
| Action-backed | `script` (each exposed script → `ActionTool`) |

- **Gating:** a tool only appears if there's ≥1 **exposed** entity in a relevant
  domain, checked per request (`intent/llm.py:65`). Timer tools require a
  timer-capable requesting device. Tool list is dynamic per instance/request.
- **"Exposes an intent" ≠ "exposes an LLM tool."** Many voice intents
  (`HassGetState`, weather, `HassNevermind`, …) are deliberately withheld from
  the LLM via an allowlist (`intent/llm.py:22`); the LLM reads state via
  `GetLiveContextTool` and converses instead.

### 2.6 Key gaps (where we add value)
- **Calendar is read-only** — no create/update/delete tool. Reminders that
  should land on a calendar need a write tool we build.
- **No long-term memory primitive** — entirely greenfield.
- **No first-class time-based reminder** — today it's a todo entity + automation
  blueprint hack.
- **No command aliasing or offer-to-learn** — HA binds a phrase to an *action*
  (conversation triggers / `intent_script`), but there's no phrase→**phrase** rewrite that
  re-enters the pipeline, and nothing *proposes* a fix when a phrase routes poorly. Both are
  the learning primitive ([`docs/learning.md`](docs/learning.md)).

### 2.7 Timers: HA owns the clock, the device is the alarm bell
- Countdown runs server-side (`asyncio.sleep` in a background task,
  `components/intent/timers.py:306`). Not forwarded to the ESP32.
- Gated on the device because a timer is localized: `async_device_supports_timers`
  = "has this device registered a timer event handler?" (`timers.py:487/493`).
  On fire, HA dispatches STARTED/UPDATED/FINISHED/CANCELLED to
  `self.handlers[device_id]` (`timers.py`); satellites (esphome, wyoming, voip,
  mobile_app) register handlers that ring/display.
- Deviceless escape hatch: a timer with a `conversation_command` instead of a
  device runs that command through the agent on fire (`timers.py:259`).
- **Reference pattern for reminders:** HA core owns schedule + state; a
  registered handler owns delivery. Timers are in-memory only — reminders need
  persistence across restarts (the gap to close).

### 2.8 The satellite output surface: `assist_satellite` announce / converse / ask
The modern, satellite-agnostic way to make a satellite *emit* something (the delivery
engine's output primitive, §5.6) is `assist_satellite`'s service API — richer than the
low-level timer `register_handler` pattern (§2.7):
- **`announce(message, preannounce=True)`** — earcon (`PREANNOUNCE_URL`) + TTS/media,
  **blocks until played**, **does not open the mic**, multi-target; raises
  `SatelliteBusyError` if the satellite is mid-interaction
  (`assist_satellite/entity.py:199`, service `__init__.py:72`). This is the delivery
  engine's content-free "⟨ding⟩ you have a reminder."
- **`start_conversation(start_message, extra_system_prompt)`** — announce **and** open
  the mic **and** seed the chat session so the LLM knows it spoke first (`entity.py:250`).
  The mechanism for **proactive/bidirectional** (Phase 4) and engaging reminders.
  **Requires an LLM conversation agent** (raises on the built-in agent, `:275`) → proactive
  is a cloud/LLM-path capability (an offline-degradation concern).
- **`ask_question(question, answers=[...])`** — announce, capture one STT utterance,
  match it **locally against a fixed answer set with no LLM** (pipeline truncated at STT,
  `entity.py:333,481`). *Not* used for LLM disambiguation (wrong shape); logged as a
  possible **generation-saving confirm-before-write** primitive (`docs/prompt-context.md`).

Consequence: the delivery engine adopts `announce`/`start_conversation` as its output
primitive; `SatelliteBusyError` is a first-class *defer* state in its ack/escalation/queue
machine. See [`docs/scheduling-model.md`](docs/scheduling-model.md).

### 2.9 Local-first routing: `prefer_local_intents` (hassil handles the common path)
A pipeline option (`assist_pipeline/pipeline.py:431`, **default `False`** — we recommend
**on**) that routes a command to the **local hassil matcher first** and only falls
through to the LLM on a miss. The exact semantics (verified) matter and are subtle:
- Engages when the agent is an **LLM with the CONTROL feature** (ours). hassil runs
  **strict** matching — exact wording + exposed entities only (`strict_intents_only=True`,
  `default_agent.py:1454`).
- The `_async_local_fallback_intent_filter` (`pipeline.py:138`) is an **exclusion**: a
  strict match to **`GET_STATE`** or **`MEDIA_SEARCH_AND_PLAY`** is **deferred to the
  LLM** (`default_agent.py:1455-1459`); **every other** strict match (TurnOn/Off,
  SetPosition, timers, …) is **handled locally** with its canned response. The two
  exclusions are deliberate: the LLM gives a richer `GET_STATE` answer and parses
  `media_class` far better (music-playback / weather).
- No match (or a soft error) → falls through to the LLM — where our fuzzy
  `find_entities` fallback catches wrong/approximate names (§5.2). **Composition:** exact
  name → local (fast/deterministic/offline); fuzzy name → LLM. The two layers don't
  overlap.
- **One bounded downside:** a hassil **false-positive pre-empts the LLM** (handled
  locally, never rescued). Strict matching + the `MIN_DIFF_SCORE` ambiguity guard (§2.4)
  make this rare — a good trade of edge-case flexibility for speed/determinism, but real.
- Locally-handled turns are **invisible** to the LLM-layer features (contextual
  confirmation, memory-offer gate, spurious-gate, multi-intent chaining) — aligned, since
  those target the friction cases that *don't* match locally.

**Strategic consequence (dual-payoff):** with prefer-local on, every **"helps-local"
intent we contribute** (`find_entities`-as-intent, calendar-write intent, what's-playing
intent, weather-forecast intent) doesn't only serve no-LLM users — it **removes that
command from the cloud path** for LLM users too (now local: faster, cheaper, works
offline). This reframes the scattered "helps local too" notes as one runtime-performance
strategy and reinforces the §7 "contribute intents first" sequencing. (Open tension: our
fuzzy fallback is currently LLM-path-only by design — see
[`docs/find-entities.md`](docs/find-entities.md) — vs. extending it into the local
matcher so pure-local benefits, which trades away determinism.)

---

## 3. Locked decisions

| Decision | Choice | Notes |
|---|---|---|
| v1 scope | **Skeleton first** | Working custom agent at parity with stock Anthropic, then layer capabilities. |
| Delivery vehicle | **Custom conversation integration** | Fork the `anthropic` component's shape (reuse its ~1300-line tool/streaming loop). |
| Model location | **Cloud Claude** | Best reasoning/tool-use; network round-trip already absorbs retrieval cost. |
| Multi-user | **Design for multi-user from day one** | Data model keyed by resolved `user_id` behind a resolver seam; voice-ID drops in later without migration. |
| Component type (short term) | **`custom_components/`** | For fast iteration, developed in this repo. |
| Component type (end goal) | **Core** | Via provider-agnostic capability platforms (§7). |

---

## 4. Demand landscape (what people actually ask for)

Ranked by how loudly the community asks (forums, HA blog, comparisons):

| Capability | Demand | Status today | Gap |
|---|---|---|---|
| Smarter weather (forecasts, arbitrary locations) | Highest | Basic current-conditions only | No forecast reasoning, no other-location |
| Web search / real-time facts | Very high | None native (MCP can) | Anthropic integration already supports a `web_search` tool — mostly wiring |
| Time-based reminders | High | Todo + automation hack | No first-class primitive; calendar read-only |
| Long-term memory / preferences | High | **None** | Greenfield — the differentiator |
| Multi-user / voice personalization | High | **None** (no voice-ID in Assist) | Needs speaker-ID + per-user context |
| Proactive / bidirectional | Growing | Emerging (Ask Question, daily summary) | No general memory-driven nudges |
| Todo / shopping list | Steady | Works (add/complete/remove/read) | UX rough; fine as base |
| Timers | Top-3 historically | Solid (named, multi, device-bound) | Ephemeral only |
| Traffic / commute | Moderate | None | External API + home/work locations |

HA's own direction: **local-first, privacy, opt-in AI**; they've bet on **MCP**
as the extension mechanism and leaned into AI through 2025 (AI Tasks, Suggest
with AI, streaming TTS).

---

## 5. Architecture principles & key mechanisms

### 5.1 Identity resolver (the load-bearing seam for multi-user)
- Requests carry **no speaker identity.** `ConversationInput` gives `context.user_id`,
  `device_id`, `satellite_id` (`components/conversation/models.py:22`) — for voice,
  `user_id` is the pipeline owner, not the speaker.
- Single chokepoint: `resolve_user(context, device_id, satellite_id) -> user_id`.
  v1 order: (1) `context.user_id` if it maps to a real Person; (2) configured
  device→owner mapping; (3) `"default"` household user.
- All capability data namespaced by that `user_id` from the first commit.
  Phase 4 voice-ID = a higher-priority branch in this one function.

### 5.2 Entity resolution strategy (context-window + exact-match fix)
Three tiers instead of dumping the full roster:
1. **Always inject a compact taxonomy skeleton** — floor→area tree + domains +
   device-classes present (+ optional counts). Small, bounded by home *structure*,
   not entity *count*. Grounds the model and anchors area/domain commands.
2. **Detail on demand + fuzzy resolution.** Fuzzy match over names/aliases
   (rapidfuzz token-set) with a top-1/top-2 ambiguity guard, **resolving to
   canonical `entity_id`** (sidesteps the exact-match failure of §2.4). **Two
   consumers of one scorer** (see [`docs/find-entities.md`](docs/find-entities.md)):
   (a) a **fuzzy fallback inside the intent match layer** — the actual
   device-control fix, free on the happy path (it hides inside the `tool_use` that
   was already firing); (b) a `find_entities(name?, area?, floor?, domain?,
   device_class?)` **tool** only for resolution *decoupled* from an immediate
   intent (ephemeral automations, reminders, browsing). *Not* a tool the model must
   call before every command — that would cost an extra model generation on every
   command. Plus states/attrs fetch (extend `GetLiveContextTool` pattern).
3. **RAG only for unbounded unstructured stores** — long-term memories/notes,
   retrieved in parallel (off the critical path). *Not* for entities.

> **This is also the primary latency lever.** Time-to-first-token is dominated by
> prefill over the static prompt (entity context + tool defs), not the user's
> words — so shrinking it cuts TTFT for every request, cloud and local, including
> the cold first utterance that prompt-caching can't help. See
> [`docs/voice-streaming.md`](docs/voice-streaming.md).

### 5.3 Graph vs hashmaps (settled analysis)
- **Containment hierarchy** (attr→entity→device→area→floor) is a fixed-depth,
  homogeneous tree. The registries are already indexed hashmaps with reverse
  indices. → **hashmap lookups + composed functions win, permanently.** A graph
  engine adds nothing.
- Multi-level fuzzy ("area MATCH x AND entity MATCH y") is fixed-depth → composed
  index lookups, not a query engine. You get the ergonomics without the engine.
- **When a graph structure earns its keep** (not before):
  - *Edge metadata + weighting* — spatial "near"/"other side of room" (needs
    position data HA doesn't model — the blocker is *ontology/edge data*, not the
    data structure).
  - *Graph algorithms* you don't want to hand-write (shortest path, components,
    cycles).
  - *Many heterogeneous edge types* queried in combination.
  - Note: single-edge variable-depth reachability (`via_device` transitive
    closure) is a **recursive function over the reverse index**, not a reason to
    adopt a graph engine.
- Vector/keyword search is **orthogonal**: it solves *reference resolution*
  (which node is "the reading lamp"), then you traverse. Hybrid = fuzzy-match
  seed nodes → graph/index expand.
- **Lowest-hanging fruit:** fuzzy indices over names/aliases per node type +
  a few composed search functions over the existing hashmaps. Also fixes §2.4.

### 5.4 Determinism-in-tools (closes the local/cloud gap AND improves cloud)
- LLMs (cloud included) are unreliable at date math, arithmetic, exhaustive
  filtering. Push all CPU-deterministic work into the tool/HA layer.
- Division of labor: **LLM decides *intent and orchestration*; the tool does
  anything deterministic.** E.g. `set_reminder` accepts forgiving input and
  resolves the absolute datetime itself (HA has tz/`now()`); `find_entities`
  does the fuzzy match, not the model.
- Payoff: weak local models become viable (shrinks the "effective usability"
  gap), cloud gets more reliable, and the tool degrades gracefully — which is a
  precondition for core acceptance (a Claude-only tool is a design smell).

### 5.5 Portability discipline (component → core = copy/paste)
- The real boundary is **dependency direction**, not file layout. Each capability
  module depends only on `hass`, `llm.LLMContext`, `ToolInput`, and HA helpers —
  **never** upward on the conversation shell or the Anthropic client.
- **Mirror core's contract now:** structure each capability as if it were already
  a core `llm.py` platform — a module exposing `async_get_tools(hass, llm_context,
  api_id) -> LLMTools` — even though the shell calls it directly for now.
- Package unit: **one capability = a `Tool` + its backing service** (a
  `Store`-backed class, a scheduler), pre-shaped as a core PR. Migration = drop
  the file at `components/<capability>/llm.py`, register the platform, delete the
  direct call.
- The conversation shell (Anthropic client, streaming, prompt assembly) is
  deliberately **throwaway**.

---

### 5.6 Shared primitives — the point of the broad exploration

**Why we scope a wide range of features before building any:** the recurring
*primitives* surface only when you lay many features side by side. Identifying and
**fully scoping each shared primitive once** lets the features become thin layers
over it — and tells us the high-leverage build order (lay the primitive that
several features sit on *first*).

Primitives identified so far, with their dependents:

| Shared primitive | Depended on by |
|---|---|
| **Delivery engine** (announce/notify/command + snooze/ack) | reminders, alarms, ambient sleep-timers, ephemeral automations, (existing) timers |
| **`find_entities`** (fuzzy → canonical `entity_id`) | device control (exact-match fix), music search/disambiguation, ephemeral-automation condition/target resolution, reminder targeting |
| **`{trigger, condition, action}` + HA condition/trigger helpers** | ephemeral automations, conditional reminders |
| **Scheduling/trigger substrate** (time watermark/catch-up + state `async_initialize_triggers`) | reminders, alarms, sleep-timers, ephemeral automations |
| **`resolve_user()` identity seam + user-keyed `Store`** | memory, reminders (per-user), calendar/todo scoping, personalization, speaker-ID (Phase 4) |
| **Prompt-context — I/O contract + taxonomy skeleton + retrieval** | entity context (TTFT, §5.2), memory injection, mic-open/meta-signals (conversation-loop), generation-count-aware output shaping. *Two halves:* output/I/O contract ([`docs/prompt-context.md`](docs/prompt-context.md), done) + input taxonomy/retrieval (§5.2, pending). |
| **Undo journal** (each mutating tool declares its inverse; deterministic replay) | device control (snapshot/restore), memory/alias writes, reminder/calendar/todo creates, calendar delete, ephemeral automations — and it underwrites every *optimistic* execution path + [`security.md`](docs/security.md)'s reversibility ([`docs/undo.md`](docs/undo.md)) |
| **Offer / learning engine** (detect friction → offer a durable fix → confirm → persist; `FrictionResolver` registry gated via `async_get_tools` §2.5) | entity aliases, **command aliases**, annotations, threshold edits, todo-default resolution — storage is per-sink (registry / YAML / FTS), only the *offer flow* is shared ([`docs/learning.md`](docs/learning.md)) |

**Build-order implication:** the **delivery engine**, **`find_entities`**, the
**scheduling/trigger substrate**, and the **identity + user-keyed store** each
underpin 3–5 features — so they are the foundations to lay first, before any one
feature is built end-to-end.

---

### 5.7 Localization discipline (core won't accept un-localizable)

A cross-cutting discipline, like §5.4/§5.5. HA is heavily international and its
localization pipeline is **strict** — `strings.json` → generated `translations/`
(`ha-core/CLAUDE.md`), and sentence intents are localized per-language in the
`home-assistant/intents` repo (`hassil==3.10.0`). **Anything with hardcoded English is a
core-merge blocker.** So: **language-agnostic where possible; dynamic/localizable where
not; never a baked-in English string.** Every path classifies into three buckets:

- **Agnostic by construction (keep):** canonical `entity_id`, structured intents, and
  area/floor/domain targeting are **IDs and structure, not words** (§2.3); earcons/chimes
  are language-neutral; the LLM is **multilingual** (so keep prompt/tool *text* in HA
  translations, not baked English, and let the model speak the user's language); our
  default-continue + **LLM stop-flag** replaces the punctuation "?" heuristic with a
  language-agnostic signal.
- **Must be built *dynamic/localizable* (fix):**
  - **Any local intent we contribute** (`find_entities`, calendar-write, `HassUndo`,
    what's-playing, forecast) ships **localized sentence templates** in
    `home-assistant/intents` — the hard core gate (a new intent isn't accepted otherwise).
  - **The §5.2 tier-2 keyword→domain booster** must be **derived from HA's *localized*
    device-class / domain strings** (e.g. `cover/strings.json` `entity_component`), **not a
    hardcoded English dict** — dynamic by construction (see
    [`docs/prompt-context.md`](docs/prompt-context.md)).
  - **Cached system phrases** (offline error, reminder prompt) come from `strings.json`
    and render per-language/voice ([`docs/offline.md`](docs/offline.md)), never literal
    English.
- **Model-owned language parsing (the boundary with §5.4):** understanding a
  **natural-language phrase** — "next Tuesday," "this weekend" — is language-dependent and
  is the **multilingual model's** job; the **tool** does only the **deterministic tz /
  calendar arithmetic** on the structured result. So a capability tool **never parses
  natural-language dates per-language** (that would be an un-localizable maintenance
  sink). This refines §5.4: the model resolves the *words*, the tool does the *math*.

**Caveat to validate:** the rapidfuzz scorer is character-level (Unicode-safe), but
`token_set_ratio` assumes **whitespace word boundaries** — CJK/Thai tokenization and
per-script threshold tuning need eval coverage ([`docs/find-entities.md`](docs/find-entities.md)).

Designing language-agnostic **now** avoids a rewrite at core-merge — the cheapest time to
honor this is before the English assumptions calcify.

---

## 6. Proposed file structure (custom component)

```
custom_components/<name>/
  __init__.py        # config entry setup, service registration
  config_flow.py     # API key + options (fork anthropic's)
  const.py
  conversation.py    # ConversationEntity — the agent shell (throwaway)
  entity.py          # chat loop, streaming, tool-call dispatch (fork anthropic)
  identity.py        # resolve_user() + device→owner config  [§5.1]
  store.py           # user-keyed Store helper (keying convention)
  capabilities/      # each = as-if-core llm.py platform [§5.5]
    entities.py      # find_entities              (Phase 0)
    memory.py        # WriteMemory/RecallMemory   (Phase 2)
    reminders.py     # SetReminder/... + scheduler (Phase 3)
    calendar_write.py# create/update events        (Phase 3)
    weather.py       # forecast tool               (Phase 1)
    websearch.py     # enable/ wire web search     (Phase 1)
```

### 6.1 Configuration & onboarding (built incrementally)

Standard HA pattern: a `config_flow` + `options_flow` (forking anthropic's), and options
**accrete per feature as they land** — no need to plan the page up front. Three
disciplines keep it from becoming a setup wall:

- **Config lives in *three* places — only one is ours.** (a) **Our options flow** — API
  key, model, `web_search`/`web_fetch` toggles, memory settings, capability enables. (b)
  **The Assist *pipeline* (HA-owned):** STT/TTS/wake **engine choice** (local-vs-cloud =
  the §offline resilience lever), `prefer_local_intents` (§2.9), continued-conversation /
  VAD timeouts (conversation-loop). (c) **Entity exposure** (HA-owned "Expose to Assist")
  = the security capability bound ([`docs/security.md`](docs/security.md) L1). We don't own
  (b)/(c) but **depend on them**, so the integration should **detect and surface guidance**
  ("for offline resilience, use local STT/TTS"; "prefer_local_intents is off"; conservative
  exposure for locks/alarm) rather than silently depend.
- **Infer, don't ask — on the page itself.** The "never interrogate" discipline
  (scheduling targeting, `user_location` auto-filled from `hass.config`, tz, area from the
  device registry) means **no config field for anything HA already knows.** This is what
  reconciles a settings page with the "zero-setup / magical" claims across the docs: those
  forbid *runtime* interrogation (asking mid-conversation), not a minimal global page.
- **Split by risk where it's a safety toggle.** `web_search` (snippets) and `web_fetch`
  (arbitrary attacker URL) are **separate** toggles, not one (security.md L3); memory,
  continued-conversation, and any fleet telemetry are **opt-in + disclosed**.

Named config surfaces the docs already imply (accrete as built): API key / model /
prompt-caching (anthropic fork); web_search + web_fetch + max_uses (auto-filled location);
memory enable + retention; device→owner map (§5.1); todo default lists; ambient
user-sound library; Voice-ID enrollment (Phase 4). Keep each **minimal and default-good.**

### 6.2 Integration topology — module vs. integration (design to the boundary, split JIT)

The file structure above is **one integration with modules**, not many integrations — a
deliberate choice, because HA's architecture *would* let us split further and we may
eventually want to. Two boundaries to keep distinct:

- **Module boundary** (what we build first): `capabilities/` shaped as as-if-core `llm.py`
  platforms, clean contracts, one config entry. Low overhead, fast iteration.
- **Integration boundary** (later): physically separate installable integrations (a
  `memory provider`, a `friction-resolver` provider, …), each its own manifest / config
  entry / lifecycle.

**HA already supports the modular vision at two levels.** (1) Voice is *already*
decomposed into separate integration domains — `stt` / `tts` / `wake_word` / `conversation`
/ `assist_pipeline` / `assist_satellite` — so the STT provider (Deepgram, …) is **not
ours**; we inherit it via the pipeline. (2) For LLM **tools**, the seam is native and
shipping: any integration can `llm.async_register_api()` (`helpers/llm.py:78`), and
`CONF_LLM_HASS_API` is a **list** the user multi-selects, merged via **`MergedAPI`**
(`llm.py:95`, *"a single APIInstance for one or more API ids, merging…"*). So a separate
provider integration can contribute tools to our agent with **no new mechanism.** The one
piece that *cannot* split is the **conversation agent itself** — a pipeline binds exactly
one agent, so Magic Mic's entity is necessarily a single integration.

**The discipline: design to the integration boundary now, split just-in-time.**

- Design every cross-capability interface **as if it already crosses an integration
  boundary** — registration + discovery, no private cross-imports, depend only on
  `hass`/`llm`/documented contracts (§5.5 dependency-direction, one step further). This
  *enforces* the contract, **is** the core-contribution shape, and makes a later split
  near-free. Register capabilities' tools via `async_register_api` even inside one
  integration, so promotion is moving code, not redesigning.
- **Don't** physically split in Waves 0–2 ([`docs/build-sequence.md`](docs/build-sequence.md)):
  each integration is real boilerplate + config surface, and the contracts aren't proven yet
  — premature splitting fights the rapid-iteration goal. **Promote** to a separate
  integration JIT, when a contract has stabilized *and* third-party pluggability has concrete
  value (memory provider, `FrictionResolver`, [`docs/learning.md`](docs/learning.md)).

**Multi-provider prompt budget is a proving-ground question, not a blocker.** Merging
several providers' APIs merges their tools *and* prompt text → more budget pressure (the
bloat [`docs/prompt-context.md`](docs/prompt-context.md) fights), so third-party providers
must honor the same dynamic `async_get_tools` gating — the discipline matters **more** with
providers in the mix. Crucially, this is exactly the kind of thing to **discover and
mitigate in Magic Mic (the throwaway proving ground) before the extension contract freezes
in core** — a mitigation like **realtime provider/tool filtering** (a quick relevance search
over tool descriptions to inject only the likely-relevant providers per request) is far
cheaper to explore now than to retrofit onto a frozen core seam. (Note: tool/provider
selection is a *different* filtering problem from entity resolution — tool descriptions are
free-text/semantic, so **embeddings may earn their keep here where §5.3 rejected them for
bounded/structured entities**; don't over-read §5.3 as forbidding embeddings everywhere.)

---

## 7. Governance / path-to-core strategy

- **Cloud is not the gate.** `anthropic`/`openai`/`google`/`ollama` conversation
  agents are already in core; Nabu Casa *sells* cloud LLM processing — cloud AI
  is aligned with, not against, the commercial model. Opt-in is necessary but
  already satisfied by precedent.
- **The real gate is architectural layering.** Core would reject a *monolithic*
  assistant that bundles memory/reminders/tools inside one provider integration.
  It accepts thin adapters + provider-agnostic capabilities (Assist API /
  `llm.py` / MCP).
- **This dissolves the "cloud outpaces local / exposes gaps" worry:** build the
  *capabilities* as provider-agnostic tools and **local models get them too**.
  Cloud vs local reduces to model quality (already accepted). Contributing shared
  capabilities reads as reinforcing the local-first platform, not prioritizing
  cloud.
- **Optics concern is dated:** HA leaned hard into AI in 2025. Their guardrail is
  "AI stays opt-in and never degrades the no-AI path" — which this design meets.
- **The intent contributions are dual-payoff (§2.9).** Because `prefer_local_intents`
  routes strict matches to hassil, every capability we land **as a local intent** (not
  just an LLM tool) helps no-AI users *and* removes that command from the cloud path for
  AI users (faster/cheaper/offline). This both strengthens the "never degrades the no-AI
  path" story and makes intent-first the performant choice, not just the polite one.
- **Modularity means core cherry-picks à la carte.** Because every capability is a
  self-contained, provider-agnostic module (§5.5/§5.6), core can adopt **any subset in any
  order** — and reject any one on principle at **zero cost to the rest.** Nothing is
  entangled; there is no all-or-nothing PR. The sequencing below is *our recommended* order,
  not a dependency chain core is bound to.
- **The eval / trace harness is the cleanest "merge-first" contribution.** It's
  **feature-decoupled** (depends on no capability) and **immediately useful to core's own
  Assist work** — the perennial "which model is best for Assist?" has no reproducible answer
  today; only ad-hoc community benchmarks exist, none integrated with the pipeline
  ([`docs/evaluation.md`](docs/evaluation.md) Parts C/F/H). So it can land **before or in
  parallel with** the capability PRs. This is active utility to the maintainers — a tool they
  can use for their own development — not a nudge to notice a gap.
- **Contribution sequencing (least → most controversial):**
  1. `find_entities` / fuzzy resolution — arguably a fix to the exact-match
     limitation; helps local most. First core PR.
  2. Calendar-write intents/tools — fills an obvious read/write asymmetry.
  3. Persistent reminders — new primitive; moderate discussion (delivery,
     persistence, overlap with timers/todo).
  4. Long-term memory — most opinionated (definition, retention, privacy,
     per-user). Land last, after proving the pattern in the component.
- **Localizability is a hard merge gate (§5.7).** Core rejects hardcoded English; new
  intents need localized sentences in `home-assistant/intents`, and any user-facing string
  goes through `strings.json`→translations. Design language-agnostic (or dynamic) from the
  start — retrofitting localization at merge time is a rewrite.
- Capabilities go through architecture discussion with the Assist/voice
  maintainers, not a surprise PR. (Note: OHF AI policy — no autonomous
  contributions; a human reviews/understands/submits every change.)

---

## 8. Phased roadmap

> This section is the *feature-value* phasing. For the **build order and how each step is
> *proved*** — the prioritization axes, the walking skeleton, the value dashboard, and where
> the test harnesses land — see [`docs/build-sequence.md`](docs/build-sequence.md), which
> braids scaffolding, value-proving primitives, and cheap magic into waves (and reorders some
> of the below: the token/turn proof moves to the front, notebook-memory drops to cheap delight).

- **Phase 0 — Skeleton.** Custom conversation integration on cloud Claude at
  parity with stock Anthropic (inherits device control via Assist API). Add
  `find_entities` (returns `entity_id`, ambiguity guard). Thread `resolve_user()`
  and user-keyed `Store` (empty) through the request. Establish the
  `capabilities/` `llm.py`-shaped contract.
- **Phase 1 — Information.** Weather-forecast tool, web-search (enable built-in),
  optional traffic. Highest demand, lowest structural risk.
- **Phase 2 — Memory.** `Store`-backed long-term memory with `WriteMemory` /
  `RecallMemory` + retrieval-into-prompt. The differentiator.
- **Phase 3 — Reminders.** Persistent scheduled reminders (durable across
  restarts) + calendar-write tool. Delivery via the registered-handler pattern.
- **Phase 4 — Proactive & multi-user.** Assistant-initiated nudges (via
  `assist_satellite.start_conversation` — announce + open mic + seed the session, §2.8;
  requires the LLM agent, so gated on connectivity); voice-ID → per-user context (only if
  chasing Alexa parity); **off-satellite delivery (phone push) + its actionable-notification
  ack** — gated on Voice-ID (push→which phone→which person→identity), parked in
  [`docs/scheduling-model.md`](docs/scheduling-model.md) so it's not lost.

---

## 9. Open questions / areas to dig into next

- Component name (placeholder `<name>` throughout).
- `find_entities`: exact signature, scoring (rapidfuzz token-set vs alternatives),
  ranking, area/domain filtering, ambiguity threshold, return shape.
- Taxonomy skeleton: exact format + real token counts on a large-home scenario;
  which entities (if any) to prune from tier-1 while keeping tool-reachable.
- Memory: data model (what *is* a memory), retention/expiry, retrieval method
  (embeddings vs keyword at HA scale), prompt-injection budget, per-user keying.
- Reminders: persistence model (survive restart), delivery channels (satellite
  announce / mobile push / conversation_command), overlap/interaction with
  timers and todo.
- Calendar-write: intent vs custom Tool; which calendar backends support writes.
- Identity: device→owner config UX; how far to design the schema for eventual
  voice-ID.
- The `ChatLog` / conversation tool-calling loop internals (before extending it).
```
