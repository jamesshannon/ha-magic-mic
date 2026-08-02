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
| Replace the entity roster with a taxonomy skeleton (§5.2) | rewrite `.api_prompt` |
| Trace every tool call and result | log inside `.async_call_tool` |
| Enforce identity/consequence policy | omit unavailable tools in `.tools`, recheck in `.async_call_tool` |
| Capture deterministic effects | journal private undo outcomes; barrier possible mutations with no outcome |
| Shadow/enforce capability selection | compute a `SelectionPlan`; compare with or replace `.tools`, prompt instructions, and context |

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

- **Prompt rewriting is not a day-one hook.** `async_provide_llm_data` may fold `api_prompt`
  into the ChatLog's system content, so the clean roster-to-skeleton rewrite (§5.2) likely
  wants the testbed to supply its own `llm.API` rather than post-edit an assembled string.
  Deferred to Wave 1.
- **Two prompt levers, not one.** The entity roster is `api_prompt` (via the llm_api seam);
  the base system prompt is the inner agent's `CONF_PROMPT` config, reached separately.
- **Keep `internal.claude` near upstream.** Trim only genuinely dead weight. Unused Claude
  features (extended thinking, citations, server-side code-execution tools, files) cost
  nothing at runtime, and keeping them preserves clean upstream diffs.
- **Provider swap.** Replacing Claude means adding `magic_mic.internal.<provider>` and
  pointing the testbed at it; the proxy, the seam, and the capabilities don't move. The one
  provider-specific hook outside the inner agent is the instrumented client for raw I/O.
