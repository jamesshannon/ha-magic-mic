# Testbed Proxy: the conversation-agent shell

> The *delivery vehicle* the capabilities run in. This is the shell PRODUCT_PLAN §3 (delivery
> vehicle) and §5.5 (portability) refer to, and the thing Wave 0 in
> [`build-sequence.md`](build-sequence.md) stands up first. Cross-refs:
> [`evaluation.md`](evaluation.md) (the trace/eval instrument this enables),
> [`prompt-context.md`](prompt-context.md) and [`find-entities.md`](find-entities.md) (the
> first manipulations that ride the seam).

## Why a proxy, and why Claude

The goal is a conversation agent whose code we control, so we can do two things a core
`llm.py` plugin cannot: instrument the model-call boundary for tracing and evaluation, and
manipulate what the model sees (tools, prompt) and how its tool calls are handled before any
of it reaches an unmodified provider. A plugin sits inside the contract; the testbed sits
around it, which is what lets it measure and reshape the contract itself.

Claude is the provider for one reason: it's the easiest capable model to demonstrate
against. Most contributors have an Anthropic API key, it's the maintainer's usual provider,
and forking the existing `anthropic` component is less setup than a local Ollama container
while giving a fair, already-built comparison baseline. **There is no hard dependency on
Claude.** The proxy and every capability are provider-agnostic. A dependency on a
Claude-class model may prove unavoidable, but a dependency on Claude specifically is not.
Swapping in another provider means swapping the inner agent, not the proxy.

## The two pieces

- **`magic_mic.internal.claude`**: a near-upstream copy of HA's `anthropic` component,
  registered as its own conversation agent. It does the Claude wire protocol (request
  shaping, streaming decode, the tool/streaming loop) and nothing Magic-Mic-specific. It's
  kept close to upstream on purpose, so it stays diffable and updatable, and it doubles as
  the **unoptimized baseline** the value dashboard measures against
  ([`build-sequence.md`](build-sequence.md) Wave 0).
- **`magic_mic.testbed`**: the neutral proxy conversation agent. It owns the ChatLog setup,
  interposes at the contract seam, delegates the model call and loop to the inner agent, and
  carries all Magic Mic logic (tracing, tool filtering and replacement, prompt shaping). This
  is the agent a user selects.

Both run the same Claude backend: `internal.claude` stock, `testbed` wrapped. The eval
harness runs a golden set against both and reports the **delta**, which is exactly the
"measured change vs. the stock fork, at fixed task success" model in
[`evaluation.md`](evaluation.md) and [`build-sequence.md`](build-sequence.md).

The embedded provider shares this repository's config entry, so its credential lifecycle is
necessarily hosted by `MagicMicConfigFlow` in the proving ground. When the provider
coordinator confirms that its API key has been rejected, HA starts the standard administrator
reauthentication flow; a validated replacement updates and reloads that entry. This is demo
provider packaging, not proxy behavior. `magic_mic.testbed` does not catch or rewrite the
provider's HA-native error. With a separately installed provider, that provider's integration
owns the same lifecycle.

## The seam: `chat_log.llm_api`

HA routes three things through one object, the `llm.APIInstance` stored at
`chat_log.llm_api` (`homeassistant/helpers/llm.py`, class `APIInstance`):

- **Tool exposure** to the model: `chat_log.llm_api.tools`.
- **Tool execution**: the `ChatLog` itself calls `chat_log.llm_api.async_call_tool(...)`
  (`components/conversation/chat_log.py`, inside `async_add_delta_content_stream` and
  `async_add_assistant_content`), not the provider agent. The provider only streams
  `tool_use` blocks into the log; the log executes them.
- **The exposed-entity prompt**: `chat_log.llm_api.api_prompt`, built by
  `AssistAPI._async_get_api_prompt`.

Because both tool exposure and tool execution flow through that single object, one decorator
around it intercepts everything, provider-agnostically. The provider agent needs no awareness
of it.

The stock agent's handler (`components/anthropic/conversation.py`,
`_async_handle_message`) does two separable steps:

```python
await chat_log.async_provide_llm_data(...)   # sets chat_log.llm_api
await self._async_handle_chat_log(chat_log)  # the model + tool loop
```

The testbed slots between them. The real call also supplies a provider-neutral policy
context containing the resolved principal and ChatLog-backed session state:

```python
await chat_log.async_provide_llm_data(...)                 # 1. real Assist API setup
turn_metadata = session_state.async_begin_turn(turn_id)
chat_log.llm_api = TestbedAPI.wrap(                        # 2. decorate/filter/intercept
    chat_log.llm_api,
    ToolPolicyContext(
        principal=principal,
        session_state=session_state,
        turn_metadata=turn_metadata,
    ),
)
await self._async_handle_chat_log(chat_log)                # 3. provider wire protocol + loop
return conversation.async_get_result_from_chat_log(user_input, chat_log)
```

Step 3 calls the inherited provider loop directly, not the provider's
`_async_handle_message` (which would re-run step 1 and discard the wrap). That inherited loop
is the deliberate coupling between the testbed conversation entity and the internal provider
shell.

`TestbedAPI` retains the complete inner instance rather than copying its fields and later
calling the base `APIInstance` implementation. It presents a filtered `.tools` view, then
delegates every allowed call to `inner.async_call_tool()`. This preserves custom API
execution behavior and gives execution policy access to the original tool list for stale or
direct-call checks. A call not present in that original advertised list is rejected rather
than delegated to a potentially dynamic inner executor. Policy resolution and confirmation
staging are specified in [`tool-policy.md`](tool-policy.md).

## What the seam gives us

| Manipulation | Lever on the wrapped `APIInstance` |
|---|---|
| Filter tools before the model sees them | present a subset in `.tools` |
| Replace a tool's schema | swap the `Tool` in `.tools` |
| Redirect a tool call (`find_entities` to the fuzzy resolver) | intercept in `.async_call_tool`, fall through for the rest |
| Replace Assist's entity roster with a bounded entity summary (§5.2) | prepare the selected `assist` API before prompt composition |
| Trace every tool call and result | retain payloads in the ChatLog/conversation trace; write only classifications to the proxy debug log |
| Enforce identity/consequence policy | omit unavailable tools in `.tools`, recheck in `.async_call_tool` |
| Capture deterministic effects | journal private undo outcomes; barrier possible mutations with no outcome |
| Shadow/enforce capability selection | compute a `SelectionPlan`; compare with or replace `.tools`, prompt instructions, and context |

The interception boundary has a driven conversation test, not only `TestbedAPI` unit tests.
A mocked provider emits `HassTurnOn` as `tool_use`: the baseline follows stock Assist
execution, while the testbed crosses the decorator, strips private undo metadata before the
provider follow-up, and journals it internally. A second case emits the same tool after policy
has hidden it and verifies execution-time denial, no device effect, and a follow-up generation
that receives the structured error. This test protects the ChatLog task and serialization
lifecycle around the neutral seam.

Token, generation, and turn counts for the value dashboard come from inspecting the ChatLog
at the neutral layer. For raw model I/O (exact request and response bytes, token usage) the
testbed injects an instrumented Anthropic client into the inner agent, so the loop code stays
unmodified.

Capability selection first runs in shadow mode at this seam: compute and trace the proposed
per-turn tool/instruction/context set while leaving the wrapped API unchanged. Once the
recall and task-success gates pass, the same plan becomes authoritative. See
[`capability-selection.md`](capability-selection.md).

`MagicMicChatLog` remains the live-interaction interface. Deterministic values that must
survive between turns, such as a pending confirmed operation or the bounded undo journal,
are exposed through it but backed by a `conversation_id`-keyed sidecar. HA clones the
dataclass between turns, so arbitrary subclass instance values are turn-local.

Prompt personalization is also established at this interface. Before prompt composition,
the testbed gives `MagicMicChatLog` the display name of the resolved principal, or `None` for
an unidentified caller. Its prompt-template override substitutes that value for core's
authorization-derived `user_name` while passing the original `LLMContext` through unchanged.
Tools and authorization therefore retain the HA `Context`; the model is not told that a
voice pipeline owner is the current speaker.

Policy decisions are appended to the exact `TurnMetadata` captured for the request, with the
stage, tool, policy source, allow/deny result, and consequence. The session sidecar retains
metadata by turn ID rather than exposing one replaceable current-turn pointer, so overlapping
turns cannot redirect delayed effects. Tool arguments are not copied into that trace. A
policy source of `unclassified` currently preserves pass-through behavior; it is visible
debt, not an implicit safe classification. Its effect class defaults to `unknown`, so it
installs an un-undoable barrier after execution unless the result explicitly reports no
mutation or an inverse. Undo payloads are object attributes outside the public result mapping
and are not sent back to the model.

## The escape hatch: modifying `internal.claude`

The proxy is the default because it's the honest test of what core can already do through the
existing contract. But that contract is also a thing we're allowed to change, and some
demonstrations require it. When the seam can't reach something, edit `internal.claude`
directly, deliberately and minimally:

- to prototype a change to the HA↔LLM contract itself (a new field, a different way tools or
  context get assembled),
- to emit additional trace metrics from inside the loop,
- to handle a case where the provider pulls context straight from an HA module instead of
  going through `chat_log.llm_api`, where the proxy has nothing to intercept.

The rule is order, not prohibition: **reach for the proxy first; drop into `internal.claude`
when the contract itself is what's in the way.** A change made there to demonstrate a
possibility becomes the evidence for the matching core proposal (§7).

## Boundaries and known nuances

- **Entity-summary composition is Assist-scoped.** Before `async_provide_llm_data` folds API
  prompts into the ChatLog, the testbed replaces only the selected `assist` contribution
  with `EntitySummaryAssistAPI`. Other registered APIs retain their prompts and tools and
  merge normally. Preparation reports `entity_summary_applied`; Tier-2 names depend on that
  effective result. This is the composition contract to preserve when the feature moves to
  core, where it belongs in Assist prompt construction rather than a provider adapter.
- **Two prompt levers, not one.** The entity roster is `api_prompt` (via the llm_api seam);
  the base system prompt is the inner agent's `CONF_PROMPT` config, reached separately.
- **Keep `internal.claude` near upstream.** Trim only genuinely dead weight. Unused Claude
  features (extended thinking, citations, server-side code-execution tools, files) cost
  nothing at runtime, and keeping them preserves clean upstream diffs.
- **Provider swap.** Replacing Claude means adding `magic_mic.internal.<provider>` and
  pointing the testbed at it; the proxy, the seam, and the capabilities don't move. The one
  provider-specific hook outside the inner agent is the instrumented client for raw I/O.

## Home Assistant dependency upgrades

`internal.claude` tracks the Anthropic component shipped by the Home Assistant release in
the project's Python environment. It does not track the head of the `core` development
branch. The current provider baseline is Home Assistant `2026.7.4`; the installed package in
`.venv` is authoritative for runtime interfaces. `references/core` may be newer and is not a
valid source for a provider refresh unless it is checked out at the matching release.
HACS requires `2026.7.0`, the first patch release in that exercised monthly line. The minimum
is not a claim of compatibility with older lines. This repository does not yet contain a CI
workflow or pinned Home Assistant test dependency, so `.venv` remains the actual exercised
baseline until CI is added; when that changes, the HACS minimum follows the earliest tested
monthly line.

### Pinning the compatibility baseline in CI

Use an ordinary Python CI job for the component suite. A Home Assistant container is not
needed: these tests import the released `homeassistant` Python package and run under
`pytest-homeassistant-custom-component`, just as the local `.venv` does. Containers are useful
later for full installation or Supervisor/OS smoke tests, but they make the unit-test
dependency less explicit and do not replace the package-version pin.

Add a small CI requirements file containing the exact baseline, initially
`homeassistant==2026.7.4`, plus `-r requirements_test.txt`. The workflow should select Python
3.14, install that file in one resolver invocation, then run Ruff and pytest. Installing the
requirements together matters: a later unpinned test helper must not silently replace the HA
pin. The existing metadata test derives `2026.7.0` from the HA package executing the suite and
checks `hacs.json`, so changing the CI pin to a new monthly line fails until the HACS minimum
moves with it.

One exact-version job is the first useful gate. If the project later promises compatibility
across multiple HA lines, add a matrix whose oldest lane installs the advertised minimum
itself and whose newest lane is the provider-copy baseline. Do not use a floating `latest`
lane as compatibility evidence; it can supplement, but not replace, the pinned lanes.

A Home Assistant package upgrade and provider refresh are one coherent maintenance change:

1. Upgrade the Home Assistant dependency intentionally and record the resolved release
   version. Do not let an incidental refresh of test dependencies silently choose the
   provider baseline.
2. Compare every copied Anthropic module with the same released version. Use the installed
   package for runtime source and a matching release checkout when upstream tests or history
   are needed.
3. Refresh the internal copy, then reapply and review only Magic Mic's deliberate changes:
   local config-entry adaptation, generation-record instrumentation, testbed delegation
   hooks, and explicitly documented provider options.
4. Review changes to HA conversation, LLM, config-entry, coordinator, and tool APIs together.
   A mechanical provider diff is insufficient when the release changes a shared core seam.
5. Run the provider and proxy unit tests, the full project suite, and the fixed eval corpus.
   Update this section's baseline version in the same commit.

This cadence accepts that the fork will sometimes lag unreleased provider fixes. A fix may be
backported deliberately when it affects Magic Mic, but that is a reviewed compatibility
change against the installed release, not routine synchronization with development `core`.
