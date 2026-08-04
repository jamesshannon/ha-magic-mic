# Capability Selection

> The prompt-time system that turns the full installed capability catalog into a small,
> relevant, authorized API for one turn. This is the home for availability filtering,
> relevance retrieval (**Tool RAG**), prompt/tool budgeting, continuity, miss recovery, and
> selection evaluation.

---

## TL;DR

Compile a per-turn API:

1. Enumerate the installed capability catalog.
2. Deterministically filter capabilities the request cannot use.
3. Retrieve a high-recall relevant subset from the utterance and conversation state.
4. Expand dependencies and fit tools, instructions, and context into hard budgets.
5. Let the model select and orchestrate within that exposed subset.
6. Recheck authorization and consequence policy at execution.
7. Recover explicitly from retrieval misses instead of silently claiming incapability.

Start with structural signals + BM25/text retrieval, session affinity, dependency expansion,
and a discovery fallback. Run it in shadow mode against the full-tool baseline before it can
remove tools from real requests. Do not begin with a separate routing LLM or an embedding
dependency.

---

## Terminology

These words name different stages:

- **Capability selection** — the complete prompt-time process that decides what tools,
  instructions, and capability context the model receives.
- **Availability/policy filtering** — hard, deterministic exclusion of disabled,
  unconfigured, unauthorized, or inapplicable capabilities.
- **Relevance retrieval / Tool RAG** — ranking and selecting likely-relevant capabilities
  from the remaining catalog.
- **Model selection** — the model choosing a tool from the exposed set.
- **Routing/dispatch** — sending an already-selected request down a real execution path:
  hassil versus LLM, provider dispatch, or tool/intent execution.

Retrieval has not routed the request. It has reduced the menu. “Filter” alone is also too
narrow for ranked semantic retrieval, so **capability selection** is the umbrella term.

Local-first hassil routing happens upstream of this design. If hassil completes the request,
there is no LLM tool-selection problem. This document governs the LLM turn after that route
falls through.

---

## Goals

- Keep prompt tokens and exposed tool count bounded as integrations, scripts, and skills
  grow.
- Stay below the provider/API hard tool-count ceiling (currently 128 for the relevant HA
  path).
- Reduce tool-choice hallucinations and irrelevant provider instructions.
- Preserve task success: omission of the correct capability is more expensive than exposing
  one extra plausible capability.
- Enforce identity and configuration availability before model exposure, then enforce again
  at execution.
- Support short contextual follow-ups such as “and Thursday?” without requiring the user to
  repeat the domain.
- Make every selection explainable in traces and recover safely from misses.
- Select tools, instruction/SKILL payloads, and capability-specific context through one
  coherent plan.

## Non-goals

- This is **not** the security boundary. Execution policy remains authoritative.
- It is not a spurious-speech detector. A television transcript containing a real command
  may retrieve the corresponding tool correctly.
- It does not infer whether the user intended to repeat a command.
- It does not replace the model's orchestration. It limits the model's available API.
- It does not require every internal Magic Mic module to become a provider/integration.
- It does not promise perfect one-pass recall; a bounded miss-recovery path is part of the
  design.

---

## Capability catalog

Each selectable capability publishes compact retrieval metadata separately from its full
tool schema:

```text
CapabilityDescriptor {
  id: "calendar"
  selection_text:
    "Read upcoming events, create calendar events, answer schedule questions"
  examples:
    - "What do I have tomorrow?"
    - "Put dinner on my calendar"
  domains: ["calendar"]
  tools:
    - get_calendar_events
    - create_calendar_event
  instructions: ["calendar_usage"]
  context_loaders: ["calendar_defaults"]
  requirements:
    - calendar integration configured
  dependencies:
    - datetime_normalization
}
```

`selection_text` and examples are retrieval documents, not always-injected prompt text.
They may be richer than the tool description without charging every request for those
tokens.

Descriptors should be provider-neutral. A real independent provider may register them, but
Magic Mic's own modules can contribute them through ordinary internal composition; do not
manufacture provider registrations merely to imitate a hypothetical core layout.

Execution policy is associated with each tool, not expanded into this retrieval descriptor.
The selection system projects the tool's pre-model availability into its filter, while the
tool policy retains argument-dependent classification and execution authority. One calendar
bundle may contain unrestricted metadata lookup, personal event reads, and destructive
writes with different policy. See [`tool-policy.md`](tool-policy.md).

### Bundles and tools

The first selection unit is a **capability bundle**, not an isolated tool. A bundle keeps
required instructions, context, and dependencies together. Large bundles then support a
second selection stage over individual tools.

Examples:

- A calendar-read request need not expose calendar deletion.
- A calendar-delete request needs read/event-resolution plus the delete operation and its
  confirmation policy.
- Ephemeral-automation authoring brings its authoring instructions and `find_entities`
  dependency.
- A large collection of scripts/`ActionTool`s needs individual retrieval by name,
  description, domain, and area even when the general device-control bundle is selected.

The catalog declares dependency closure and “expose together” groups so the model does not
need to understand internal wiring.

---

## Selection inputs

Selection uses only request/session information already available before the model
generation:

- complete current STT/text utterance;
- recent `ChatLog` turns and tool results;
- current conversation focus and recently used capabilities;
- immutable pending-operation or clarification referent, if present;
- resolved principal and assurance/scope;
- requesting device, area, and other cheap HA context;
- installed integrations, exposed entities, enabled features, and provider support;
- tool-count and token budgets.

Do not copy transcript content into another interaction model. Session affinity and pending
state are small derived inputs exposed through the ChatLog-centered session state.

---

## Stage 1 — deterministic availability filtering

Filter before relevance retrieval:

- capability or feature is disabled;
- required integration, entity, service, or provider support is absent;
- administrator has disabled the operation;
- resolved principal lacks the required household/personal scope;
- the capability is invalid in the current operating mode;
- a provider cannot represent or execute the tool contract.

Retrieval cannot restore a filtered capability.

### Explain an unavailable capability without exposing its tool

Completely hiding an unavailable feature can produce a misleading “I can't do that.” When
the utterance is relevant to a known but unavailable capability, the selection plan may
inject a compact, non-actionable reason:

> Personal calendar access requires an identified user.

The executable schema remains absent. Reasons must avoid leaking sensitive configuration;
an administrative disable or hidden integration may warrant a generic “not available for
this request.”

Consequence policy is usually **not** an availability filter. A tool that requires
confirmation can be exposed, selected, normalized, and staged. The execution policy decides
whether it runs.

The current policy kernel already performs this availability stage for classified tools at
the `TestbedAPI` seam. Unclassified tools remain available in the POC for compatibility and
are traced as such. Capability-selection shadow mode must report that status rather than
treating an unknown policy as evidence that a tool is unrestricted.

---

## Stage 2 — relevance retrieval (Tool RAG)

Rank the filtered catalog using a high-recall hybrid:

- exact and fuzzy entity-name matches;
- explicit domains, verbs, and structured request signals;
- BM25/text similarity over `selection_text` and examples;
- requesting-area relevance for physical-device operations;
- recent capability/session affinity;
- pending referent affinity;
- prompt and tool-count cost.

Conceptually:

```text
score =
    text relevance
  + entity/domain evidence
  + recent-session affinity
  + pending-referent affinity
  - exposure cost
```

This is not a calibrated risk probability and should not be presented as one. The score is
only a ranking device.

Start with lexical/structural retrieval because it is cheap, local, explainable, and easy to
evaluate. Add embeddings only if measured misses—especially paraphrase or multilingual
misses—justify their dependency and operational cost. A separate LLM classifier is also
deferred: it adds a generation and moves the classification error rather than removing it.

### Optimize for recall

A false positive usually costs some prompt tokens. A false negative makes a valid task
impossible or invokes the fallback. Therefore the initial threshold should favor recall,
with budget pressure handled by ranking and assembly rather than aggressive early pruning.

For multi-intent utterances, preserve result diversity instead of filling the budget with
near-duplicate tools from the first high-scoring bundle.

### Localization

Keyword-only English routing would violate the project's localization discipline.
Retrieval documents and examples should use localized HA strings where available, and the
evaluation corpus must include supported languages before lexical gates become authoritative.
Embeddings or another multilingual retriever may earn their keep here if localized lexical
retrieval cannot maintain recall.

---

## Stage 3 — conversation continuity

The latest utterance is insufficient:

> “What is on my calendar tomorrow?”
>
> “And Thursday?”

Use bounded session affinity:

- retain a recently used capability for one or two follow-up turns;
- strongly retain the capability tied to an unresolved clarification or pending referent;
- drop affinity when the user clearly changes subject;
- never allow affinity to override current authorization or availability.

This is retrieval help, not an “established intent” whitelist. The user may correct or
replace the pending topic:

> “Actually, no—turn off my lights.”

The new request can supersede the previous focus and select a different bundle.

---

## Stage 4 — budget assembly

The assembler takes ranked bundles and produces one `SelectionPlan` under:

- the hard tool-count ceiling;
- a configurable tool-schema token budget;
- an instruction/SKILL budget;
- a capability-context budget;
- reserved space for required resident/fallback machinery.

```text
SelectionPlan {
  tools
  instructions
  context_loaders
  unavailable_hints
  dependency_expansions
  omitted_candidates
  reasons_and_scores
  tool_count
  estimated_prompt_tokens
}
```

Dependencies are expanded before final admission. A capability cannot be admitted in a
half-functional form merely because its primary tool ranked highly.

Some tiny, frequently used capabilities may eventually remain resident, but residency must
be earned through traces and evaluation. “Common” by intuition is not enough, especially
when generated scripts and multi-provider APIs compete for the same 128 slots.

Selection covers all three dynamic payload types:

- executable tools;
- capability instructions/SKILLs;
- capability-specific context.

They need separate budgets but one plan, because selecting a tool without the instructions
needed to use it—or injecting instructions for an absent tool—is incoherent.

---

## Stage 5 — model choice and execution

The model receives the assembled API and chooses/orchestrates within it. The normal
execution gateway then:

- resolves the selected tool implementation;
- rechecks identity, authorization, and consequence policy;
- normalizes/stages confirmation-sensitive operations;
- records effects and an optional `UndoAction`.

Tool exposure is a prompt/UX boundary. The execution check is the security boundary and
covers stale, malformed, or directly constructed calls.

The implemented proxy delegates allowed calls to the original `APIInstance`, preserving
custom executors. Confirmation-sensitive calls instead stage the exact immutable operation.
The legacy registry and the current permissive unknown-tool boundary are specified in
[`tool-policy.md`](tool-policy.md).

---

## Miss recovery

Retrieval misses are inevitable. The assistant must not silently translate a miss into “I
cannot do that.”

### Preferred fallback: discovery without execution

Keep a small non-executing discovery capability available:

```text
discover_capabilities(query)
```

It searches the complete **already availability-filtered** catalog and returns compact
capability headers. Choosing a result marks that bundle for exposure on the next generation.
The fallback costs an extra generation only on a miss and cannot itself execute an arbitrary
hidden operation.

Validate in the proving ground that HA's tool set can expand safely between generations in
one turn. If the current seam cannot, that is evidence for a core selection/loading seam.
Do not work around it with a broad `invoke_anything(name, arguments)` tool; that would erase
schemas, authorization visibility, and useful model constraints.

### Optional automatic expanded retry

An automatic retry may be evaluated only when the first generation:

- made no tool call;
- produced no side effect;
- appears to be declining or asking for a missing capability.

It must never replay an effectful generation. Explicit discovery is preferred because it is
traceable and easier to reason about.

---

## Selection trace and observability

Every turn records:

- catalog size before filtering;
- hard-filtered capabilities and safe reason codes;
- retrieved candidates, scores, and signal contributions;
- dependency expansions;
- final tools/instructions/context;
- tool count and estimated/actual prompt tokens;
- whether the chosen tool was initially selected;
- discovery or expanded-retry use;
- final task outcome and generations/latency.

This trace belongs with the existing conversation/evaluation trace, not in the user-visible
transcript. It should answer:

- Why was this tool present?
- Why was that capability absent?
- Was it filtered, below the retrieval cutoff, or excluded by budget?
- Did conversation affinity affect the result?
- Did fallback recover the miss?

---

## Evaluation and rollout

### Shadow mode first

Before capability selection can remove real tools, run it beside the current full-tool
baseline:

1. Compute the `SelectionPlan` without applying it.
2. Record which tool the full-roster model actually used.
3. Check whether that tool and its dependencies would have been exposed.
4. Compare projected tool count/tokens with observed task success.

This produces retrieval recall and savings without breaking requests.

### Metrics

- capability recall@budget;
- tool + dependency recall@budget;
- task-success delta against the full-tool baseline;
- unauthorized executable-tool exposure (**must remain zero**);
- prompt tokens and exposed tool count;
- selection latency;
- extra generations and latency caused by discovery;
- false “unavailable” answers;
- follow-up continuity success;
- multi-intent coverage;
- miss rate by language, provider, and capability.

The golden set needs positive, negative, and adversarial cases:

- direct requests and paraphrases;
- short contextual follow-ups;
- explicit topic changes after a pending clarification;
- multiple intents in one utterance;
- unavailable personal capability requests;
- homes with many scripts and merged providers;
- capability descriptions with overlapping vocabulary;
- Tool RAG retrieval of a command quoted in spurious media speech (proving selection is not
  the intentionality boundary);
- discovery recovery after a forced retrieval miss.

### Gates

- Deterministic tests cover availability filtering, dependency closure, budget enforcement,
  session-affinity expiry, and safe reason injection.
- Probabilistic/e2e evaluation covers relevance recall, model task completion, and recovery.
- Enforced selection ships only after shadow recall meets a defined threshold at the target
  budget and task success does not regress beyond the scorecard tolerance.

---

## Recommended v1

1. Provider-neutral `CapabilityDescriptor` catalog.
2. Deterministic configuration and identity filtering.
3. Capability-level BM25/text retrieval over descriptions and examples.
4. Structural boosts from entity/domain matches and recent conversation focus.
5. Dependency expansion and simple tool/token budgets.
6. Selection traces.
7. Shadow-mode comparison against the full-tool baseline.
8. A bounded, non-executing discovery fallback.
9. Enforcement only after evaluation.

Deferred until evidence demands them:

- embeddings/vector index;
- separate LLM router/classifier;
- learned ranking;
- elaborate marginal-value optimization;
- third-party provider publication contract;
- permanent resident-set tuning;
- automatic retry beyond the narrow effect-free case.

---

## As-built: Wave 1 shadow mode

The first slice landed in `custom_components/magic_mic/capabilities/capability_selection.py`
plus the offline harness `evals/harness/selection_shadow.py`. It implements the
deterministic spine (steps 1 through 6 of Recommended v1) and the shadow comparison (step
7), and stops short of enforcement (step 9), discovery (step 8), and conversation
continuity (Stage 3), which land with their first live consumers.

What exists:

- `CapabilityDescriptor` / `Catalog`: a provider-neutral demo catalog of twelve bundles
  over the HA Assist intent tools plus `find_entities`, indexed by id and by tool so a
  used tool maps back to its owning bundle. `GetLiveContext` and `GetDateTime` are marked
  `resident`.
- `available_descriptors` (Stage 1): intersects declared tools with the tools the running
  system actually exposes, so an absent integration drops out and a partly-exposed bundle
  is projected to its runnable subset. Availability is grounded in the live roster, not a
  separate requirements engine.
- `rank_descriptors` (Stage 2): high-recall lexical retrieval, the shared Unicode-aware
  fuzzy scorer over each bundle's `selection_text` and examples, best-per-bundle, plus a
  structural boost when the request names a declared domain.
- `assemble_plan` (Stage 4): residents first and unconditional, then high score to low
  under a tool-count budget with dependency closure, so nothing is admitted half-
  functional. Below-floor, budget-displaced, and unavailable bundles all carry into the
  plan's `omitted` list with a safe reason, so the trace explains every catalog entry.
- `selection_shadow`: reads a scored baseline artifact (which already records each case's
  utterance and the tools the model called), recomputes the plan across a budget sweep, and
  reports exact case- and tool-level recall@budget plus the tool saving.

### First shadow finding (2026-08, `wave0_baseline`)

Nineteen of twenty-five cases called a tool. Recall against the demo catalog:

| Budget | Case recall | Tool recall | Avg tools exposed |
|---|---|---|---|
| 6  | 79% | 80% | 6 |
| 8  | 89% | 90% | 8 |
| 10 | 89% | 90% | 10 |
| 24 (full) | 100% | 100% | 24 |

Two things to read from this, and one caveat:

1. **The retriever is not ready to enforce.** At a tight budget the misses are ranking
   errors, not budget starvation: for "turn off the living room lamp" the `climate` and
   `volume` bundles outrank `device_control`, because `token_set_ratio` over short
   descriptor documents rewards an incidental shared word ("the", "room"). Lexical
   retrieval was the right cheap starting point, but the shadow number says the next work
   is scorer quality (structural/domain evidence weighted above bare text overlap, or the
   IDF variant the fuzzy scorer already carries), measured back through this same harness,
   before selection may remove a real tool.
2. **The instrument works and is honest.** It surfaces the exact miss and the bundle that
   displaced the right one, and it separates a catalog gap (an uncatalogued used tool) from
   a ranking miss so a gap cannot hide as low recall.
3. **The saving is catalog-relative and understates reality.** The baseline artifact does
   not record the full per-turn roster HA exposed, so the harness uses the twelve-bundle
   catalog (24 tools) as the denominator. A real home exposes more (generated scripts,
   scenes, merged providers), where the same recall would buy a larger saving. Measuring
   the true exposed roster needs a live shadow run that records `inner.tools` per turn;
   that is the follow-up, and it does not change the recall numbers above.

This mirrors the prompt-context lesson: build the measurement first, let it tell you
whether the optimization pays before you ship it. The gate to enforcement stays where
Recommended v1 put it, a recall threshold at the target budget with no task-success
regression, now with a harness that can report the number.

---

## Worked examples

### Contextual calendar follow-up

```text
Turn 1: "What do I have tomorrow?"

filter:
  calendar available for resolved person
retrieve:
  calendar-read strong
assemble:
  calendar read + datetime normalization

Turn 2: "And Thursday?"

retrieve:
  weak lexical signal
  strong recent calendar affinity
assemble:
  calendar read retained
```

### Unidentified personal request

```text
Request: "Read my calendar"

filter:
  executable personal-calendar tools removed
selection:
  unavailable hint selected because request is calendar-relevant
response:
  explain that personal calendar access requires an identified user
```

### Conditional reminder

```text
Request: "Remind me tomorrow morning if the garage door is still open"

retrieve:
  ScheduledItem authoring
  ephemeral-automation instructions
dependency expansion:
  find_entities
omit:
  weather, memory, unrelated scripts
execution:
  model fills the bounded spec
  deterministic code resolves time/entity and persists ScheduledItem
```

### Retrieval miss

```text
Request: valid but unusual paraphrase
initial selection: misses the needed capability
model: calls discover_capabilities with a compact query
discovery: returns filtered matching headers
next generation: selected bundle is exposed
trace: records miss, recovery, extra generation, and final outcome
```

---

## Related docs

- [`prompt-context.md`](prompt-context.md) — overall prompt/I/O budget and context assembly.
- [`security.md`](security.md) — two-stage exposure/execution policy and consequence gates.
- [`tool-policy.md`](tool-policy.md) — implemented policy object, registry, and proxy
  enforcement contract.
- [`conversation-loop.md`](conversation-loop.md) — continuation state and spurious detection.
- [`skills.md`](skills.md) — instruction payloads selected alongside tools.
- [`testbed-proxy.md`](testbed-proxy.md) — proving-ground interception seam.
- [`evaluation.md`](evaluation.md) — scorecard, traces, and deterministic/probabilistic gates.
- [`telemetry.md`](telemetry.md) — long-tail deployed selection misses, discovery use, and
  real-home tool-count distributions after shadow/corpus gates pass.
- [`find-entities.md`](find-entities.md) — entity-level retrieval/resolution, a separate
  structured problem from capability selection.
