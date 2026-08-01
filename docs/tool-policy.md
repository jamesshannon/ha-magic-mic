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
2. `StaticToolPolicy` covers tools whose scope and consequence never vary by argument.
3. `@tool_policy(...)` lets a Magic Mic-owned tool publish its policy beside its
   implementation.
4. `ToolPolicyRegistry` supplies policies for existing HA and third-party tools that cannot
   declare the contract themselves.

Both policy methods receive a provider-neutral `ToolPolicyContext`: the resolved principal,
`MagicMicSessionState`, continuation origin, and an optional minimum consequence raised by
other deterministic signals. The evaluator produces immutable exposure and invocation
decisions. It does not execute tools.

The consequence vocabulary remains deliberately ordinal:

- `low`: execute immediately after authorization;
- `confirm_on_continuation`: execute after a normal wake-word turn, but stage on a
  wake-word-free continuation;
- `always_confirm`: stage on every turn.

A request-level signal may raise the declared consequence but cannot lower it. The current
live request adapter does not yet receive continuation origin from HA, so production turns
currently use `is_continuation=False`. Deterministic tests establish the policy behavior;
the upstream continuation side channel remains future work.

## Two-stage enforcement

`TestbedAPI` is a real decorator around the original `llm.APIInstance`:

1. At construction, it resolves each tool policy and exposes only tools whose pre-model
   requirements pass.
2. At `async_call_tool()`, it resolves the exact tool from the inner API's complete tool
   list, repeats the exposure check, classifies the normalized arguments, and checks scope
   again.
3. An allowed low-consequence call delegates to the original API instance. This preserves
   custom `APIInstance.async_call_tool()` implementations instead of bypassing them through
   the base HA executor.
4. A confirmation-sensitive call does not invoke the inner API. It freezes the exact tool
   name, arguments, principal, effective consequence, and a 30-second expiry into the
   session's `PendingOperation`, then returns a structured `confirmation_required` tool
   result. The main LLM writes the spoken question; tools do not provide previews in v1.

Scope denial raises a typed, localizable `ToolPolicyDeniedError`. Exposure and execution
decisions record the tool name, policy source, stage, outcome, and consequence in current
turn metadata. The record contains no tool arguments.

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
        └── classify_call(arguments, context)
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
policy layer. `find_entities` and the existing core tools therefore keep their current
behavior. Every such decision is labeled `unclassified` in the turn trace.

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

1. **Tool declaration:** inherent scope, base consequence, and argument classifier. Owned by
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
- Change the unknown-tool default from permissive to unavailable only when classification
  coverage and compatibility behavior are ready for that enforcement gate.

## Tests that define the current contract

Deterministic tests cover:

- declared, exact-legacy, family-legacy, and unclassified resolution;
- personal-scope filtering and argument-dependent execution denial;
- delegation to an arbitrary inner `APIInstance.async_call_tool()` override;
- typed/localizable rejection without inner execution;
- ordinary versus continuation behavior for `confirm_on_continuation`;
- immutable staging for continuation and `always_confirm` operations;
- policy trace records and unchanged pass-through for unclassified tools.

These are seam tests. They do not establish that the present empty default legacy registry
classifies the HA ecosystem.
