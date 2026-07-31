# Product Plan — LLM-Backed Voice Assistant for Home Assistant

> Working design document. Captures the architectural findings, decisions, and
> strategy from initial exploration. Living doc — expect to drill into individual
> sections and revise. Not a build spec yet.

---

## 0. Companion documents

Per-feature / per-topic deep-dives live in [`docs/`](docs/) so this plan stays an
overview. As we drill into a feature (memory, speaker identification, todo, …) it
gets its own file there. Current docs:

- [`docs/testbed-proxy.md`](docs/testbed-proxy.md): the **delivery vehicle**. A neutral
  **Testbed Proxy** conversation agent (`magic_mic.testbed`) wraps a near-upstream internal
  provider agent (`magic_mic.internal.claude`, a copy of the `anthropic` component) and
  interposes at the `llm.APIInstance` seam, where HA routes tool exposure (`.tools`), tool
  execution (`chat_log.llm_api.async_call_tool`, run by the `ChatLog`, not the provider), and
  the exposed-entity prompt (`.api_prompt`). One decorator over that object intercepts all
  three, provider-agnostically. Registering `internal.claude` too gives the unoptimized
  **baseline** for free (same backend, stock vs. wrapped, measure the delta). Claude is the
  demo provider because it's the easiest capable model to test against; **no hard dependency
  on Claude.** Escape hatch: edit `internal.claude` directly when the HA↔LLM contract itself
  is what needs changing (reach for the proxy first).
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
  plausible early upstream discussion/contribution** (§7), adapted to core if maintainers
  find it useful. Work-items: eval harness, deterministic+timing harness, trace enrichment,
  prior-art/reuse decision, frontend UI fixes.
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
  an *input* to `get_resolved_user()` (§5.1). Model is easy/community-proven; core has
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
  makes liberal reopening safe). Also the multi-turn `ChatLog` home: no parallel
  transcript; deterministic pending-operation/undo state is exposed through
  `MagicMicChatLog` and backed by a `conversation_id` sidecar. **Barge-in**
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
  default; real calendar only on explicit cue). Unifying move: one versioned
  **`ScheduledItemStore`** owns assistant scheduling/lifecycle state for reminders,
  alarms, scheduled commands, and ephemeral automations. The native scheduler reads this
  store directly; an optional `CalendarEntity` is a view/edit projection, not a firing
  dependency. Externally visible events get a UID-linked companion record rather than
  hiding assistant metadata in their descriptions. Includes a **Triggering
  implementation** section: reuse Calendar Trigger's interval-cursor + point-in-time
  alarm pattern, which
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
  LLM-at-fire survives only for non-compilable fuzzy conditions. **Ephemeral overrides**
  ("lights to 100% for 15 min") are a worked case of this engine, not a new feature =
  snapshot ([`undo.md`](docs/undo.md)) + apply + reverting one-shot on a boundary trigger;
  revert is **literal/unconditional** (no silent world-moved-on check), mechanics
  (snapshot-ordering, scene cleanup, restart-durability) live in the authoring primitive
  not SKILL prose, no dedicated path until observability shows the shape earns one.
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
  (difflib fallback). Phase 0; likely the first focused core-seam discussion once measured.

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
  fast actions, **earcon** for ack, spoken filler only for slow tools. The shell is
  experimental (§5.5), so a struct shell is fine if provider-specific framing stays
  out of deterministic capability logic.
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
  `usage` (`cache_read`/`cache_creation`). Population validation is owned by the
  opt-in, content-free deployed-use design in
  [`docs/telemetry.md`](docs/telemetry.md), not the eval corpus.

- [`docs/capability-selection.md`](docs/capability-selection.md) — the dedicated
  **prompt-time capability selection / Tool RAG design**. Compile a per-turn API:
  deterministic availability/identity filtering → high-recall relevance retrieval →
  dependency expansion + tool/instruction/context budgeting → model selection → execution
  recheck. Defines provider-neutral descriptors and `SelectionPlan`, two-level
  bundle/tool retrieval, bounded session affinity, unavailable-capability hints, discovery
  fallback, shadow-mode rollout, and recall@budget/task-success gates. Selection is not the
  security or spurious-speech boundary.

- [`docs/telemetry.md`](docs/telemetry.md) — **deployed product-outcome telemetry**, kept
  distinct from deterministic tests, per-run traces, and corpus evaluation. Owns
  content-free VISION-moment signals (attempt → complete/recover/reuse), population
  assumptions, local-first aggregation, explicit fleet opt-in, forbidden-data rules,
  denominators, rollout, retention, and deletion. These measures validate real-home use;
  they are not corpus acceptance criteria and never replace correctness gates.

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
  `get_resolved_user()` (§5.1); unidentified `"default"` callers are household-only and
  never acquire a pseudo-personal bucket. Phase 2 = **notebook only**.

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

- [`docs/skills.md`](docs/skills.md) — **gated instructional payloads.** Separates the
  **two mechanisms** that both "load instructions when relevant": **machinery-gated
  injection** (*we* see deterministic state → inject the block whole, zero extra gen —
  this is `learning.md`'s Resolve-Friction text) vs. a **SKILL registry** (~25-tok
  headers resident in the cached prefix, model pulls a body via **`read_file`** — named
  for the trained affordance but sandboxed to skills — one gen). Which one is set by whether a deterministic gate exists. **Gating classes by
  gate-owner:** (1) machinery-gated (us) (2) LLM-signaled (the model, via resident
  header — **ephemeral-automation authoring** is the v1 case, no deterministic gate)
  (3) provider-declared (a third party ships the gate → **over-inclusion incentive**,
  needs the §6.2 header-budget arbiter → **v2**). Publisher keyword gates are a **lossy
  semantic proxy**; resident-header + model-selects is paraphrase-robust and *is* the
  §6.2 filter (one filter, two payoffs). Pull tool is **named `read_file`** (trained
  affordance) but **sandboxed to skills** (authority = the resolver, not the name; §security). **v1 = the registry with one consumer (automation authoring)**;
  compile-once/run-deterministic means the pull-generation amortizes to zero at fire.

- [`docs/calendar.md`](docs/calendar.md) — calendar as a capability: the existing
  read tool (`calendar_get_events`) + the **write** surface we add. Verified `ha-core/`
  CRUD asymmetry: **CREATE** is a proper service (`calendar.create_event`) broadly
  supported (Google/CalDAV/local); **UPDATE/DELETE are websocket-only** (no service) and
  thin — **UPDATE is `local_calendar`-only** (Google/CalDAV can't). **No create LLM tool
  exists today.** Scope: **CREATE v1** (thin Tool doing datetime-normalization +
  calendar-selection + name-it-back), **DELETE fast-follow** (needs a new
  `calendar.delete_event` service + event-resolution via read→fuzzy-over-summaries→
  disambiguate), **UPDATE punt**. Meets [`scheduling-model.md`](docs/scheduling-model.md)
  at `create_event`: one create flow writes a native `ScheduledItem` or an external event
  plus UID-linked companion record. The native store has a `CalendarEntity` projection,
  so calendar-write and the reminder store are **not separate builds**.

- [`docs/todo.md`](docs/todo.md) — **thin.** Base todo is **already done** (add/
  complete/remove intents + `todo_get_items` read tool exposed today). Verified: the
  entity supports rich edits (UPDATE/MOVE/SET_DUE/DESCRIPTION) but only add/complete/
  remove are wired to intents; **todos never fire on due** (trigger is list-mutation).
  The one design point is settled upstream — **firing = reminder, passive list = todo**;
  a task the user wants listed *and* nudged is a `ScheduledItem` with todo placement and
  a linked item UID (the canonical store owns firing; todo is the visible copy). Residual
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
  persistence** (trust). Build = one **`create_reminder`** tool (normalize time → choose
  placement via visibility-intent; `ScheduledItemStore` owns native state or a companion
  record for a real-calendar event, so this and calendar-write are **one build** → name it
  back; behavioral write → confirm).
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
  locks/alarm by default), L2 consequence-aware voice confirmation backed by an immutable
  pending operation rather than model-reconstructed arguments, with an explicit
  non-malicious-model assumption, plus identity-gated tool exposure and execution, L3
  **taint model**
  (untrusted-in → restrict dangerous sinks; the
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

- [`docs/explainability.md`](docs/explainability.md) — **cross-cutting trust/debugging
  capability.** "Why is the thermostat at 67?" / "Why did the hallway light turn on?"
  **HA structurally enables this and Google/Alexa can't** (same "already-built, wrap it"
  shape as web-search): every `State` carries a `Context(user_id, parent_id, id)`
  (`core.py:1246`) and the **logbook** already resolves those into named "triggered by X"
  causes (`ContextAugmenter`, `logbook/processor.py`), while **recorder history** retains
  prior values *with attributes* (`get_significant_states`/`state_changes_during_period`,
  `include_start_time_state`/`no_attributes=False`). **Determinism-in-tools:** the tool
  emits a **structured causal record** (incl. **`unattributable` as a first-class value**);
  the LLM **only narrates** — anti-confabulation is the load-bearing guardrail ("not enough
  in the logs to say why" is a *returned value*, not a guess). **Three layers, two
  voice-free *and* LLM-free** (retrieval/resolution → interpretation → voice) → needs an
  **interpreter, not a mic** → strong §6.2 **split-JIT** candidate with non-Assist
  consumers (card, notification, REST). **Attribution gradient:** assistant-caused (undo
  journal, perfect) → automation/user (context chain) → device/cloud (opaque →
  unattributable). **State reversal is out of scope / functionally impossible** — *not* for
  missing data (history has prior values) but because attribution is partial and "undo"
  against a live/recurring external cause is ill-posed; only the **assistant-caused** half
  survives, and that's [`undo.md`](docs/undo.md)'s journal. Value = low-freq/high-trust
  (like undo); **Wave 3–4** (needs recorder). No such feature in core (verified).

_All planned topic docs now written._

---

## 1. Vision

Build an LLM-backed voice assistant layer for Home Assistant that is roughly
equivalent to Google Home / Alexa in capability, but local-first-friendly and
privacy-respecting in the HA tradition. Beyond device control, it should offer
"true assistant" features: long-term memory, time-based reminders, calendar
write access, and richer information retrieval (weather forecasts, web search).

**End-goal:** land the capabilities in HA **core**.
**Short-term:** iterate fast as a cohesive **custom-component proving ground**. Keep
provider dependencies isolated and contracts explicit so the findings translate cleanly,
without pretending that core adoption will be a source-file copy.

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
| Delivery vehicle | **Testbed Proxy conversation agent** | Neutral proxy (`magic_mic.testbed`) wraps a near-upstream internal provider agent (`magic_mic.internal.claude`), interposing at the `llm.APIInstance` seam. All Magic Mic logic lives in the proxy. See [`docs/testbed-proxy.md`](docs/testbed-proxy.md). |
| LLM provider | **Claude is the testbed, not a dependency** | Easiest capable model to demo against (contributors have keys; less setup than local Ollama; fair prebuilt baseline). A dependency on a *Claude-class* model may be unavoidable; a dependency on *Claude specifically* is not. Swap = swap the inner agent, not the proxy. |
| Model location | **Cloud Claude to start** | Best reasoning/tool-use; network round-trip already absorbs retrieval cost. Provider-agnostic capabilities mean local models get the same tools (§7). |
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
- Requests carry **no reliable speaker identity.** `ConversationInput` gives `context.user_id`,
  `device_id`, `satellite_id` (`components/conversation/models.py:22`). `context.user_id` is
  trustworthy for **text** (the logged-in user), but for **voice** it's the **pipeline owner,
  not the speaker** (identical no matter who talks).
- Core also drops the request origin before calling a conversation agent. Both direct text
  requests and STT output arrive as `ConversationInput.text`; `device_id` and `satellite_id`
  are not reliable source classifiers. The request adapter must therefore pass an explicit
  `text`, `voice`, or `unknown` source into identity resolution. Only explicit `text` may
  trust `context.user_id`; `unknown` fails closed. Do not infer source from device fields.
- **Identity and data scope are separate facts.** The resolver reports whether this request
  has a real person and which scope keys it may use. An unidentified voice caller resolves to
  the `"default"` principal: **unknown person, household scope only.** `"default"` is not a
  synthetic personal user. A recognized or authenticated person may use household scope plus
  their own personal scope. Therefore an unknown caller can ask for a household fact but is
  denied "my calendar," personal memory, personal todo, and other person-scoped operations.
- **`get_resolved_user(...)` is the uniform accessor.** Capability tools, intents, and policy
  code call it and never care whether the turn came from a live mic or a deferred trigger. It
  is synchronous, cheap, deterministic, and idempotent; it only reads the result already
  established for the request and falls back to the unidentified principal if none exists.
  Source belongs to the upstream `async_resolve_user(...)` operation, not this accessor.
  Resolution preserves both the resolved person (if any) and the household-only fallback
  instead of representing `"default"` as an ordinary `user_id`. It considers cheap signals in
  order: (a) an established speaker or deferred owner; (b) `context.user_id` if the source is
  explicitly text and it maps to a real, active, non-system HA user; (c) a configured
  device→owner mapping for voice; (d) the unidentified `"default"` principal.
- **Populating the resolved user is trigger-specific and happens once, upstream** (never inside
  the accessor):
  - **immediate voice:** a speaker-ID stage (Phase 4) at/after STT, where the audio is, matches an
    enrolled profile ([`docs/speaker-identification.md`](docs/speaker-identification.md));
  - **immediate text:** `context.user_id`;
  - **deferred** (a reminder / ephemeral automation firing): the trigger **replays the `user_id`
    it persisted at capture time** and does **not** re-resolve (no audio; the side channel is long
    gone). See [`docs/scheduling-model.md`](docs/scheduling-model.md) and
    [`docs/ephemeral-automations.md`](docs/ephemeral-automations.md).

  Both populators share one **"resolve and establish principal → run → clear"** lifecycle.
  The accessor performs only the middle read.
- **The handoff is a side channel keyed by request identity, not a `Context` attribute.**
  `Context` is slotted (`__slots__`; no arbitrary fields) and its `user_id` is HA's **auth**
  identity, which must not be overwritten with a speaker (**personalization-not-auth**,
  [`docs/security.md`](docs/security.md)). So the populator writes `{request → user_id}` into
  per-request session state (`hass.data`), and the accessor (reached by tools via `llm_context`)
  reads it.
- **Capture-time, not execution-time.** A deferred action that touches person-scoped data
  snapshots the resolved person onto its artifact at creation and scopes by the stored value at
  fire. An unidentified caller cannot author an action that later reads personal data. A
  household-scoped reminder may still capture the `"default"` principal because its body is
  household data, not a personal lookup.
- All capability data records an explicit **household or personal** scope from the first
  commit. Phase 4 voice-ID is the **upstream populator**, not logic inside each capability, so
  it drops in without changing capability call sites. There is no pre-Voice-ID
  `"default"`-personal bucket to migrate or strand.

#### 5.1.1 ChatLog-centered live interaction state

`ChatLog` remains the source of truth for the live transcript, tool calls, tool results, and
model-visible history. Do **not** build a parallel interaction transcript. Most one- and
two-turn flows, including open-ended entity disambiguation, need nothing beyond the
`ChatLog`: Claude sees the candidates and prior question in history and interprets the next
reply.

Some state must be deterministic rather than reconstructed from prose. Store only that state,
and choose its home by lifetime:

- **Turn:** resolved principal, origin device, trust/provenance markers, and effects produced
  during the turn.
- **Conversation session:** an immutable pending operation awaiting confirmation,
  disambiguation focus only when a capability needs a deterministic constraint, and the
  bounded undo journal.
- **Durable capability store:** reminders, memories, scheduled rules, and delivery/ack state.

Magic Mic already extends HA's `ChatLog` as `MagicMicChatLog`. Build on that interface, with
one implementation constraint: HA uses `dataclasses.replace()` to clone a `ChatLog` between
turns, so arbitrary subclass `__dict__` state does not survive. Session-scoped state therefore
lives in a sidecar keyed by `conversation_id`, exposed through `MagicMicChatLog` properties,
and cleaned up with the HA chat session. This is storage behind the existing object, not a
second interaction model.

Policy is enforced in code at two points:

1. **Exposure gate:** omit personal or sensitive tools when the resolved principal lacks the
   required assurance. The model should not see unusable tools.
2. **Execution gate:** recheck the same policy in `async_call_tool` before performing the
   action. Exposure is an optimization and UX boundary; execution is the security boundary.

For a confirmation-sensitive action, persist the normalized tool name and immutable arguments
as a pending operation with principal, consequence class, and expiry. A later "yes" approves
that stored operation; the model does not reconstruct or alter the operation after approval.
Pending state constrains the referent, not the whole next turn: "actually no, turn off the
lights" may reject the pending action and issue a new command. Open-ended clarifications
remain model-in-loop through `ChatLog`.

In the POC, the main LLM writes the spoken confirmation question. The immutable record binds
approval to the operation that was staged, preventing argument reconstruction, stale
approval, and replay; it does **not** prove that the model described that operation honestly.
This confirmation mechanism is not a defense against a malicious or fully prompt-injected
model. Tool-owned localized previews, an isolated renderer model, and structured step-up
confirmation are deferred alternatives, not v1 requirements. The threat-model boundary and
future options are recorded in [`docs/security.md`](docs/security.md).

#### 5.1.2 Execution gateway (extend the intent chokepoint)

HA already has the right center: local hassil execution and LLM `IntentTool` execution both
reach `intent.async_handle()`. Do not build another intent engine. Extend that path with a
small execution contract, and make custom capability tools use the same contract:

- principal + origin (`hassil | llm | deferred`) + normalized invocation in;
- normal response + effects + optional `UndoAction` out;
- policy, effect capture, and optional compensation around execution.

Do **not** make gateway-wide idempotency a foundation requirement. In the ordinary
synchronous Assist path there is no general retry protocol to deduplicate, and repeated
identical commands are routinely intentional: two toggles, two broadcasts, or two timers.
A hassil/LLM double-route is a pipeline bug to prevent structurally. Exactly-once behavior
after an ambiguous downstream timeout is impossible unless that downstream operation
supports an idempotency key. Add narrow replay guards only where a real replay boundary
exists—for example, consuming a pending confirmation once, consuming an undo entry once, or
claiming one persisted scheduled occurrence once.

The acting intent or capability owns compensation because only it knows what changed. It
returns a provider-neutral, inspectable `UndoAction` descriptor, not a closure. The proving
ground does **not** need to retrofit every HA intent: instrument a small representative set
of Magic Mic/demo intents, mark every other action explicitly un-undoable, and use the result
to propose the core intent contract. Broad coverage comes only after that contract lands in
core.

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

### 5.5 Proving-ground portability (preserve findings, not file shapes)

Magic Mic is one cohesive experiment in how the Assist stack could work, not a bag of
already-finished integrations waiting to be copied into core. Several of its important
results cross current boundaries—ChatLog session state, intent execution/effects,
prompt-time capability selection, identity scope, and durable scheduling—so upstreaming
them will require design with core maintainers and changes in the appropriate existing
subsystems.

Portability therefore means:

- isolate the Anthropic transport/client at the conversation adapter;
- keep deterministic domain logic and persisted schemas provider-neutral;
- give shared state and execution contracts explicit types and tests;
- use normal HA helpers, lifecycle, localization, and storage conventions;
- record the measurements and failure cases that justify each proposed core seam.

It does **not** mean inventing a standalone integration or `llm.py` platform for every
feature, registering internal modules as fake providers, or mirroring a speculative future
core layout. Use ordinary internal modules where they make the proving ground understandable.
Extract an extension boundary only when the experiment demonstrates that independent
providers are genuinely needed or core maintainers select that architecture.

The expected upstream artifact is a design argument plus focused tests and implementation
slices adapted to core—not unchanged Magic Mic source files. Clean dependency direction
still reduces that adaptation cost; faux copy/paste readiness does not.

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
| **Versioned `ScheduledItemStore`** (canonical trigger/condition/body + scope + lifecycle; native projection or external UID companion) | reminders, alarms, scheduled commands, ephemeral automations, restart/catch-up reconciliation |
| **`get_resolved_user()` identity seam + explicitly scoped Store** (`"default"` = unidentified, household-only; a real person unlocks personal scope) | memory, reminders, calendar/todo scoping, personalization, speaker-ID (Phase 4) |
| **ChatLog-centered session state** (conversation-ID sidecar for pending operations + undo; no parallel transcript) | deterministic confirmations, undo, constrained disambiguation, per-turn identity/provenance/effect trace |
| **Execution gateway over `intent.async_handle()` + capability-tool adapter** (policy, effects, optional `UndoAction`) | hassil intents, LLM `IntentTool`s, custom tools, deferred bodies; selective demo coverage before core adoption |
| **Prompt-context — I/O contract + taxonomy skeleton + retrieval** | entity context (TTFT, §5.2), memory injection, mic-open/meta-signals (conversation-loop), generation-count-aware output shaping. *Two halves:* output/I/O contract ([`docs/prompt-context.md`](docs/prompt-context.md), done) + input taxonomy/retrieval (§5.2, pending). |
| **Undo journal** (each mutating tool declares its inverse; deterministic replay) | device control (snapshot/restore), memory/alias writes, reminder/calendar/todo creates, calendar delete, ephemeral automations — and it underwrites every *optimistic* execution path + [`security.md`](docs/security.md)'s reversibility ([`docs/undo.md`](docs/undo.md)) |
| **Offer / learning engine** (detect friction → offer a durable fix → confirm → persist; `FrictionResolver` registry gated via `async_get_tools` §2.5) | entity aliases, **command aliases**, annotations, threshold edits, todo-default resolution — storage is per-sink (registry / YAML / FTS), only the *offer flow* is shared ([`docs/learning.md`](docs/learning.md)) |
| **Dynamic prompt assembly + capability selection** — one `SelectionPlan` budgets **tools, context data, and instructions** through availability filtering, Tool RAG, dependency expansion, and fallback | tool gating, tier-2 context injection, the offer engine, multi-provider APIs, and the SKILL registry ([`docs/capability-selection.md`](docs/capability-selection.md), [`docs/skills.md`](docs/skills.md)) |

**Build-order implication:** the **delivery engine**, **`find_entities`**, the
**scheduling/trigger substrate**, and the **identity + explicitly scoped store** each
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
    That contribution has **two coupled halves**, not one: `sentences/<lang>/` (how the
    command is *said*) **and** `responses/<lang>/` (how the result is *spoken back*), plus
    reuse of the shared `_common.yaml` `expansion_rules` for area/floor/domain carriers
    (`builtin_sentences.markdown`). A localized response definition is part of the gate, not
    an afterthought.
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

### 5.8 LLM-path coverage is a superset of hassil (proven, not assumed)

`prefer_local_intents` (§2.9) routes the common commands to hassil, and the offline story
([`docs/offline.md`](docs/offline.md)) leans on the local matcher as a fallback. Both are
**optimizations layered under our path — neither discharges our obligation to cover the
command ourselves.** The rule: **anything hassil can do, our LLM path must also do, plus
more.** We never ship a capability gap on the reasoning that "hassil catches it," because:

- **Local routing is user-toggleable and situational.** A persona/never-break-character
  user turns `prefer_local_intents` **off** (every turn goes to the LLM,
  `assist_create_open_ai_personality.markdown`); a cloud-STT deployment may not have the
  local matcher in the resilient position at all. If hassil is our coverage, those users
  lose table stakes.
- **hassil is an optimization for the happy path, not a floor under ours.** When it
  pre-empts us it's *faster*; when it's absent or off, we must be **complete on our own** —
  the LLM path standing alone has to reach at least parity with the built-in sentence set
  before it adds anything.
- **This is the concrete meaning of "never degrade the no-AI path" inverted:** the AI path
  must not silently *under*-cover relative to no-AI. Table stakes (turn on/off, set,
  covers, media transport, timers, list add, date/time, weather current-conditions,
  nevermind/undo) are **our** responsibility to hit, then exceed.

**Proven, not assumed.** The parity claim is an eval gate, not a design assertion: the
hassil **built-in sentence set** (`builtin_sentences.markdown`) plus the per-language
`home-assistant/intents` test sentences, run **through our path at LLM-only scope**, are a
**required regression corpus** — our path must reach ≥ parity on them before any
value-add counts ([`docs/evaluation.md`](docs/evaluation.md) Part E). Treat a parity miss
as a blocking regression, the same as a broken tool.

---

## 6. Proposed file structure (custom component)

```
custom_components/<name>/
  __init__.py        # config entry setup, service registration
  config_flow.py     # API key + options (fork anthropic's)
  const.py
  conversation.py    # ConversationEntity — experimental agent shell
  entity.py          # chat loop, streaming, tool-call dispatch (fork anthropic)
  chat_log.py        # ChatLog extension + conversation-ID session-state access [§5.1.1]
  identity.py        # resolved principal/scope + device→owner config [§5.1]
  store.py           # explicitly household/personal Store helper
  capabilities/      # provider-neutral internal domain/tool modules [§5.5]
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
  continued-conversation, and any fleet telemetry are **opt-in + disclosed**
  ([`docs/telemetry.md`](docs/telemetry.md)).

Named config surfaces the docs already imply (accrete as built): API key / model /
prompt-caching (anthropic fork); web_search + web_fetch + max_uses (auto-filled location);
memory enable + retention; device→owner map (§5.1); todo default lists; ambient
user-sound library; **response verbosity dial (terse ⇄ conversational; default terse — the
verbosity-complaint lever, [`docs/prompt-context.md`](docs/prompt-context.md))**; Voice-ID
enrollment (Phase 4). Keep each **minimal and default-good.**

### 6.2 Integration topology and capability selection

The file structure above is **one integration with modules**, not many integrations — a
deliberate choice. Module boundaries keep the proving ground legible and testable; they do
not predict how core will package the result. A later provider/extension boundary is a
separate design decision, justified only by demonstrated third-party or
independent-lifecycle needs.

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

Do not register Magic Mic's internal modules as separate `llm` APIs merely to simulate
portability. Use `async_register_api` when testing a genuinely independent provider;
otherwise plain internal composition is the more honest experiment. If a core proposal needs
a provider registry, the proving ground should supply evidence for its contract rather than
silently presupposing it.

Prompt-time **capability selection** means deterministic availability filtering followed by
relevance retrieval (**Tool RAG**) and budget assembly; *routing* is reserved for actual
path/executor dispatch. The complete catalog, descriptor/plan contracts, continuity,
fallback, and rollout design live in
[`docs/capability-selection.md`](docs/capability-selection.md).

**Multi-provider prompt budget is a proving-ground question, not a blocker.** Merging
several providers' APIs merges their tools *and* prompt text → more budget pressure (the
bloat [`docs/prompt-context.md`](docs/prompt-context.md) fights), so third-party providers
must honor the same dynamic `async_get_tools` gating — the discipline matters **more** with
providers in the mix. And there is a **hard** ceiling underneath the soft token one: the
total tool/script count exposed to the model **cannot exceed 128** (an API-inherited hard
failure, not a slowdown; see [`docs/prompt-context.md`](docs/prompt-context.md)) — merging
providers is exactly what pushes a many-script home over it, so the gate/filter is
cap-avoidance, not only cost. Crucially, this is exactly the kind of thing to **discover and
mitigate in Magic Mic (the throwaway proving ground) before the extension contract freezes
in core**. Tool/provider retrieval is a **relevance and budget** mechanism, not the security
boundary; execution policy still owns identity, consequence, and confirmation
([`docs/capability-selection.md`](docs/capability-selection.md)).
The same selection seam governs **provider-published SKILLs** (instruction payloads, not just tools —
[`docs/skills.md`](docs/skills.md) v2): a resident ~25-tok header per third-party skill has
the *same* incentive-to-over-include and cost-scales-with-installed-add-ons shape, so the
relevance retriever over descriptions is **one arbiter for both** payload types. SKILL headers
are why this must be settled here before the contract freezes — v2 provider skills are
publishing **+** this selection mechanism, never publishing alone.

---

## 7. Governance / path-to-core strategy

- **Cloud is not the gate.** `anthropic`/`openai`/`google`/`ollama` conversation
  agents are already in core; Nabu Casa *sells* cloud LLM processing — cloud AI
  is aligned with, not against, the commercial model. Opt-in is necessary but
  already satisfied by precedent.
- **The real gate is architectural evidence.** Magic Mic may be cohesive while proving the
  experience. Core adoption will place successful pieces into existing Assist,
  conversation, intent, scheduling, and integration seams—or add a new seam where the
  evidence warrants it. We should not prejudge that packaging by manufacturing many
  integrations inside the POC.
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
- **Do not promise à-la-carte source modules.** The user-visible capabilities share genuine
  foundations: session state, identity/scope, execution policy/effects, selection, and
  scheduling. Core can adopt findings incrementally, but some changes will necessarily
  establish a shared seam before feature slices can follow. The POC should make those
  dependencies explicit rather than conceal them behind nominally standalone modules.
- **The eval / trace work may still be an early contribution**, because it is useful beyond
  Magic Mic and can validate later Assist changes. Treat that as a hypothesis for maintainer
  discussion, not a requirement that the experimental harness itself land unchanged
  ([`docs/evaluation.md`](docs/evaluation.md) Parts C/F/H).
- **Likely discussion/adoption sequence (least → most controversial), subject to core
  architecture review:**
  1. `find_entities` / fuzzy resolution — arguably a fix to the exact-match
     limitation; helps local most.
  2. Calendar-write intents/tools — fills an obvious read/write asymmetry.
  3. Persistent reminders — new primitive; moderate discussion (delivery,
     persistence, overlap with timers/todo).
  4. Long-term memory — most opinionated (definition, retention, privacy,
     per-user). Discuss last, after proving the pattern in the component.
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
  `find_entities` (returns `entity_id`, ambiguity guard). Thread `get_resolved_user()`
  and the explicitly scoped `Store` (empty) through the request. The unidentified
  `"default"` principal is household-only, never a pseudo-personal user. Establish clean
  provider-neutral internal contracts without speculative per-capability core packaging.
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
