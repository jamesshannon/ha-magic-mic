# Tool Policy

> The deterministic contract that decides whether an LLM tool is visible, whether one
> concrete call is authorized, and whether that call executes or becomes an immutable
> pending operation. Capability relevance and Tool RAG live in
> [`capability-selection.md`](capability-selection.md); this document owns the execution
> policy beneath them.

## Current implementation

Section 4 of the pre-Wave-1 foundation is implemented in
`custom_components/magic_mic/tool_policy.py` and the `TestbedAPI` decorator.

The kernel has four parts:

1. `ToolPolicy` provides two deterministic methods:
   - `exposure_policy(context)` returns requirements knowable before arguments exist;
   - `classify_call(arguments, context)` returns the requirements for one normalized call.
2. `StaticToolPolicy` covers tools whose scope, consequence, and effect class never vary by
   argument.
3. `@tool_policy(...)` lets a Magic Mic-owned tool publish its policy beside its
   implementation.
4. `ToolPolicyRegistry` supplies policies for existing HA and third-party tools that cannot
   declare the contract themselves.

Both policy methods receive a provider-neutral `ToolPolicyContext`: the resolved principal,
conversation-scoped `MagicMicSessionState`, the exact request-local `TurnMetadata`,
continuation origin, and an optional minimum consequence raised by other deterministic
signals. The evaluator produces immutable exposure and invocation decisions. It does not
execute tools.

The consequence vocabulary remains deliberately ordinal:

- `low`: execute immediately after authorization;
- `confirm_on_continuation`: execute after a normal wake-word turn, but stage on a
  wake-word-free continuation;
- `always_confirm`: stage on every turn.

A request-level signal may raise the declared consequence but cannot lower it. The current
live request adapter does not yet receive continuation origin from HA, so production turns
currently use `is_continuation=False`. Deterministic tests establish the policy behavior;
the upstream continuation side channel remains future work.

Invocation classification also carries an effect class:

- `read_only`: no journal entry is needed unless the tool explicitly reports otherwise;
- `mutating`: the result must declare `UndoAction`, `UndoUnavailable`, or `NoMutation`;
- `unknown`: compatibility default for unclassified/legacy calls, treated conservatively
  as a possible mutation after execution.

Effect class is execution metadata, not consequence. A low-consequence light command may
mutate; a sensitive calendar read may remain read-only.

## Two-stage enforcement

`TestbedAPI` is a real decorator around the original `llm.APIInstance`:

1. At construction, it resolves each tool policy and exposes only tools whose pre-model
   requirements pass.
2. At `async_call_tool()`, it resolves the exact tool from the inner API's complete tool
   list, applies that tool's declared parameter schema once, repeats the exposure check,
   classifies the normalized arguments, and checks scope again. A name absent from that
   advertised list is denied before delegation, even when a custom inner executor would
   accept it dynamically.
3. An allowed low-consequence call delegates the same normalized `ToolInput` to the original
   API instance. This preserves custom `APIInstance.async_call_tool()` implementations
   instead of bypassing them through the base HA executor, while preventing later coercion or
   default insertion from changing the operation after policy evaluation.
4. A confirmation-sensitive call does not invoke the inner API. It freezes the exact tool
   name, arguments, principal, effective consequence, and a 30-second expiry into the
   session's `PendingOperation`, then returns a structured `confirmation_required` tool
   result. Construction validates nested keys and leaves against the JSON value domain,
   rejects non-finite numbers and circular containers, and owns the resulting immutable
   copy. HA eager-starts calls in emitted order and staging has no await, so the first such
   call claims the single pending slot. Later calls return a structured
   `pending_operation_already_staged` conflict, leave the first record unchanged, and do no
   work. The main LLM writes the spoken question; tools do not provide previews in v1.
5. At delegated execution's terminal boundary, the proxy records private undo outcome
   metadata. A completed mutating or unknown call without it becomes a `not_supported`
   journal barrier; read-only and explicit no-op outcomes do not shadow the latest mutation.
   A raised possible mutation also creates a barrier because a partial effect cannot be ruled
   out. Cancellation of a possible mutation records the same barrier before propagating
   unchanged.

Scope denial raises a typed, localizable `ToolPolicyDeniedError`. Exposure and execution
decisions record the tool name, policy source, stage, outcome, and consequence in the exact
turn metadata carried by the request. They never discover a "current" turn through mutable
conversation state. An absent tool records policy source `undeclared`; an advertised tool
without policy metadata records `unclassified`. The record contains no tool arguments.

This provides the stale/direct-call defense even though that path is rare today. More
importantly, it fixes the contract before restricted tools and selection machinery depend on
the proxy's earlier pass-through shape.

## Policy ownership

Policy is not one large passive `CapabilityDescriptor`. The contracts have different
lifetimes and grains:

```text
Capability
├── retrieval text, examples, and aliases
├── bundle membership
├── instructions, context loaders, and dependencies
└── tools
    ├── schema and executor
    └── ToolPolicy
        ├── exposure_policy(context)
        └── classify_call(arguments, context) → scope, consequence, effect
```

Calendar reading and calendar deletion belong to one retrieval bundle but have different
execution policies. A generic intent or service tool may also need argument-dependent
classification, such as household light control versus lock actuation. That behavior belongs
in a `ToolPolicy` implementation, not a growing collection of optional descriptor fields.

Decorators are authoring syntax, not the runtime abstraction. A simple owned tool can attach
a `StaticToolPolicy` with `@tool_policy(...)`; a complex tool can attach its own `ToolPolicy`
object. Existing tools still require the registry because Magic Mic does not own their class
definitions.

## The legacy registry is first-class

Until HA integrations publish policy metadata, most existing tools are legacy tools from
Magic Mic's point of view. The registry will therefore be the primary source for a large part
of the installed catalog, not a small compatibility overlay and not a blocklist.

Resolution precedence is:

1. policy declared by the tool;
2. legacy registration for the concrete tool type plus its name;
3. legacy registration for a tool family/type;
4. explicit `unclassified` result.

When HA combines multiple selected APIs, it presents each member tool as an
`llm.NamespacedTool`. Policy resolution follows that wrapper to the original member tool, so
declarations and legacy registrations still use the member's type and unprefixed name. The
namespaced wrapper remains the external identity for model exposure, execution lookup,
tracing, argument normalization, and delegation. This keeps policy ownership stable without
changing HA's merged-API calling convention.

Exact registrations include both type and name because HA currently flattens contributions
from every LLM tools platform into `APIInstance.tools` and discards the source integration.
Names alone are not stable identities and may collide. Broad type registrations are only
correct for families with uniform behavior; a generic family such as `IntentTool` will
eventually need a classifier over the normalized intent/domain/arguments or finer exact
entries.

The default registry intentionally contains no pretend-complete Intent x Domain matrix yet.
Representative static and argument-dependent policies prove the contract in tests. Policies
should be added alongside each real restricted capability and through an explicit inventory
of the core tool catalog, with regression tests that detect renamed or newly unclassified
tools.

## Unclassified tools and the security claim

In the POC, an unclassified tool remains exposed and executes exactly as it did before the
policy layer. Existing core tools therefore keep their current invocation behavior. Every
such policy decision is labeled `unclassified` in the turn trace, and its effect defaults to
`unknown`, so successful execution without undo metadata creates a conservative barrier.
Magic Mic's `find_entities` declares `read_only`; it remains otherwise unrestricted.

This is a compatibility choice, not a secure default. While unclassified tools are
permissive, Magic Mic must not claim that its policy registry forms a closed capability
security boundary. Before that claim or broad public deployment, one of these must happen:

- every installed tool is classified through a declaration or registry entry; or
- unclassified tools default unavailable, with an explicit administrator compatibility
  override.

The likely core-shaped fix is a stable tool identity and provenance contract, for example an
integration, capability, and operation ID supplied when tools are aggregated. Integrations
could then own their inherent classifications while a central evaluator applies household
configuration and request facts.

## Long-term configuration layers

Keep four sources separate:

1. **Tool declaration:** inherent scope, base consequence, effect class, and argument classifier. Owned by
   the tool integration or, during migration, the legacy registry.
2. **Installation configuration:** capability enablement, entity/domain restrictions,
   network permission, shared-speaker privacy, and administrator confirmation overrides.
3. **Request context:** resolved principal, device, continuation origin, operating mode, and
   provenance-derived escalation.
4. **Evaluation result:** the per-turn exposure decision and per-call invocation decision.

Ordinary configuration should move policy toward less authority: disable a tool, narrow its
audience, or raise confirmation. It should not silently lower a tool's inherent consequence
or personal-data requirement. An advanced unsafe override may be considered later, but must
be distinguishable from normal configuration.

The first administrator UI should expose stable concepts rather than every internal field:

- enable or disable a capability;
- restrict a capability to identified users or future HA user groups;
- restrict entity/domain access;
- require confirmation;
- allow external-network use;
- allow personal results to be spoken on shared devices.

The evaluator can later compose these restrictions without changing the `ToolPolicy` methods
or the `TestbedAPI` enforcement points.

## Confidence, severity, and the confirmation gate (design, not yet built)

Confirmation is decided by two independent inputs. **Severity** is modeled today: the ordinal
consequence a tool declares (`low`, `confirm_on_continuation`, `always_confirm`). **Match
confidence**, how sure we are this action is what the request meant, is not modeled yet. This
section records the model settled in design so the eventual implementation does not relitigate
it. Nothing here is built. The seams it plugs into already exist: the request-level consequence
escalation ("an optional minimum consequence raised by other deterministic signals" in
`ToolPolicyContext`, and "provenance-derived escalation" in configuration layer 3 above), and
the `find_entities` action auto-resolve threshold ([`find-entities.md`](find-entities.md)).

### Two scores, opposite goals, do not conflate them

There are two numbers in play and they are not the same score.

- **Exposure relevance** (`SELECTION_RELEVANCE_FLOOR`, currently `1.0` on the ranker's
  IDF-coverage scale of 0 to 100) decides which tools enter the prompt. Its job is **recall**:
  when in doubt, expose the tool and let the model decide. The floor is deliberately permissive;
  the tool budget does the real pruning ([`capability-selection.md`](capability-selection.md)).
- **Match confidence** (the `find_entities` fuzzy score: `token_set_ratio` plus a top-1/top-2
  margin) decides whether a fuzzy action resolves on its own. Its job is **precision** on a
  consequential act.

The same closeness pulls these two in opposite directions. A tight margin between two exposure
candidates is an argument to expose *both* and defer to the model. A tight margin on an action
resolve is an argument to *not* fire on a guess. Reusing the exposure floor as a confirmation
signal would be a category error.

### The match score is a floor, not a ceiling

A deterministic match score earns trust asymmetrically.

- **A strong score with a clear margin is a positive license.** It is safe to resolve and act,
  reproducibly, and it is the one case that also works on the no-AI path where there is no model
  to lean on.
- **A weak score is an abstention, not a veto.** It means lexical matching cannot see the
  connection; the call belongs to the model. A weak score never, on its own, forces a
  confirmation.

Worked example. "Help me concentrate" or "go into my zero-in mode" against a `Focus Mode`
script scores low lexically: almost no token overlap. The semantic fit is still obvious, and
the model resolves it. Faulting the assistant for not confirming there would punish it for the
scorer's blindness, not for real doubt. So low lexical score is the wrong trigger for a
confirmation.

**Margin routes; it is not a confirm knob.** A tight margin means the deterministic scorer
cannot discriminate, so the right move is to raise the model into the loop (return the candidate
list, expose both), not to lower execution confidence. Once the model has adjudicated on
meaning, the margin has done its job. If the scorer ranks candidate A at 0.99 and B at 0.85 and
the model picks B, that only happens on the path where the tool returned a list and deferred;
the model chose B for a reason the lexical score could not see, which is principle 1 (the LLM
decides intent and orchestration; deterministic code does the work) working as intended, and
the A-over-B margin is irrelevant to whether B should confirm.

### What actually triggers a confirmation

Two things, and neither is "the score was low":

1. **Severity** (deterministic, tool-declared or registry-supplied, administrator-overridable).
   A high-consequence action confirms regardless of how confident anyone is. This is the
   reproducible backstop, and it is the mechanism already implemented above. Confidence can
   never lower it.
2. **Ambiguity the model itself recognizes** (the middle band). Two actions both plausibly fit
   and it cannot tell, or it is genuinely reaching. Only the model can see semantic fit, and
   deciding "confirm or just act" is orchestration, which principle 1 assigns to the model.

So the gate is not a flat severity-by-confidence matrix. It has three inputs: the **absolute
score** licenses a deterministic act at the high end and abstains at the low end; the **margin**
routes between a deterministic resolve and deferring to the model; and **who adjudicated**
decides whether a low lexical score is a problem (a bare deterministic resolve on a weak, tight
match is not allowed) or a non-issue (a model that deliberately chose a semantically strong
action is confident by construction). Severity sits over all of it as a floor that confidence
cannot move.

### Wiring to the existing seams

- The confidence signal feeds the **request-level escalation** already in the contract: it can
  raise a call's effective consequence (`low` toward `confirm`), never lower it.
- The deterministic half lives in the **`find_entities` action auto-resolve threshold**
  ([`find-entities.md`](find-entities.md)): the score-plus-margin band above which a fuzzy
  action resolves without asking, and below which it returns candidates for the model.
- **Provenance is the cleanest confidence input.** A direct call to an exposed tool the model
  named is high confidence by construction: the tool made budget and the model chose it. A pick
  made after a `find_entities` round-trip is where graded confidence actually lives, because that
  path is the only one carrying a purpose-built match score and margin.
- The **discovery fallback** ([`capability-selection.md`](capability-selection.md)) is where
  low-lexical-score semantic matches get a runtime home: a script beyond the tool budget is still
  reachable through `find_entities(query=..., domain="script")`, so the ranker does not have to be
  clairvoyant. This is the alternative to embedding the whole catalog.

### Reproducibility and the no-AI path

Letting the model own the ambiguity confirmation is softer than a threshold and risks a "works
only on a strong model" smell (principle 1). The guard is severity as the deterministic floor:
a wrong low-severity guess (`Focus Mode` fires when `Reading Mode` was meant) is cheap and
undoable ([`undo.md`](undo.md)), so leaning on model judgment there is acceptable, while anything
genuinely consequential is pinned to confirm by severity independent of confidence. The no-AI and
local path does not get the semantic-ambiguity layer at all; it relies on HASSIL exact match plus
the deterministic auto-resolve threshold, which is correct because it has no model to consult.

Principle 1 is "the LLM decides intent and orchestration; deterministic code does the work"
(`PRODUCT_PLAN.md` section 5.4, `CLAUDE.md` core principle 1).

## Next implementation steps

- Inventory the actual HA/core and bundled third-party tool catalog before a restricted
  capability relies on complete coverage.
- Add stable policy identities or a provenance-preserving aggregation seam in the proving
  ground, then use the evidence to shape a core proposal.
- Add policy entries with each real capability rather than guessing the entire matrix now.
- Thread live continuation origin into `ToolPolicyContext`; do not infer it from transcript
  history, `device_id`, or `conversation_id`.
- Wire local yes/no handling to consume the pending operation, re-run policy using the
  approval-turn principal, and delegate the exact stored arguments once.
- Define the administrator override model after the first settings require it.
- Implement the confidence input to the confirmation gate (see "Confidence, severity, and the
  confirmation gate"): derive it from match provenance and the `find_entities` score/margin, feed
  it through the existing request-level escalation, and keep severity as the floor it cannot
  lower.
- Change the unknown-tool default from permissive to unavailable only when classification
  coverage and compatibility behavior are ready for that enforcement gate.

## Tests that define the current contract

Deterministic tests cover:

- declared, exact-legacy, family-legacy, and unclassified resolution;
- personal-scope filtering and argument-dependent execution denial;
- schema coercion before argument-dependent policy and exact normalized delegation;
- delegation to an arbitrary inner `APIInstance.async_call_tool()` override;
- typed/localizable rejection without inner execution;
- ordinary versus continuation behavior for `confirm_on_continuation`;
- immutable staging for continuation and `always_confirm` operations;
- ordered multi-call staging where the first operation survives and later calls return
  structured conflicts without aborting the ChatLog;
- read-only/no-op versus mutating/unknown journaling behavior and private result metadata;
- policy trace records and unchanged pass-through for unclassified tools.

These are seam tests. They do not establish that the present empty default legacy registry
classifies the HA ecosystem.
