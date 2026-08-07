"""The interposition seam: a decorator over `chat_log.llm_api`.

HA routes tool exposure (`.tools`), tool execution (`.async_call_tool`, run by the
ChatLog, not the provider) and the exposed-entity prompt (`.api_prompt`) through one
`llm.APIInstance`. Wrapping that single object interposes on all three,
provider-agnostically. See `docs/testbed-proxy.md`.

The wrapper is a real decorator: calls that pass policy delegate to the original API
instance, preserving custom ``async_call_tool()`` implementations. It presents a filtered
tool list to the model, but keeps the inner instance and its complete list for the
authoritative execution-time recheck.
"""

import asyncio
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from homeassistant.components.conversation import (
    ConversationTraceEventType,
    async_conversation_trace_append,
)
from homeassistant.helpers import intent, llm
from homeassistant.util.json import JsonObjectType

from ..capabilities.action_targets import (
    ArgumentResolution,
    annotate_entity_arguments,
    resolve_entity_arguments,
)
from ..capabilities.capability_selection import EnforcedSelection
from ..capabilities.localization import ConversationStrings
from ..capabilities.match_fallback import resolve_name_miss
from ..const import DOMAIN, LOGGER
from ..execution_result import get_undo_disposition, public_tool_result
from ..pending_operation import (
    PendingOperation,
    PendingOperationAlreadyStaged,
    async_stage_pending,
)
from ..session_state import SelectionTrace, ToolPolicyTrace
from ..tool_policy import (
    DEFAULT_TOOL_POLICY_REGISTRY,
    EffectClass,
    PolicySource,
    ToolPolicyContext,
    ToolPolicyDeniedError,
    ToolPolicyRegistry,
    evaluate_invocation,
    is_tool_exposed,
)
from ..undo import (
    LocalizedDescription,
    UndoDisposition,
    UndoUnavailable,
    UndoUnavailableReason,
    async_record_undo,
)

PENDING_OPERATION_LIFETIME = timedelta(seconds=30)

# A capability selector reduces a set of policy-exposed tool names to an EnforcedSelection
# for one turn (docs/capability-selection.md). It is a closure so the seam stays free of
# catalog and utterance knowledge: the entity binds those and passes only this callable.
CapabilitySelector = Callable[[frozenset[str]], EnforcedSelection]


class TestbedAPI(llm.APIInstance):
    """Policy-enforcing decorator over the original `APIInstance`.

    This is the neutral seam. Nothing provider-specific belongs here.
    """

    def __init__(
        self,
        inner: llm.APIInstance,
        policy_context: ToolPolicyContext,
        policy_registry: ToolPolicyRegistry,
        *,
        selector: CapabilitySelector | None = None,
        strings: ConversationStrings | None = None,
        entity_arguments: bool = True,
        entity_argument_hints: bool = True,
    ) -> None:
        """Initialize a filtered view while retaining the original executor.

        Tools pass two independent gates before the model sees them: tool-policy exposure
        (the security-relevant filter, always on) and, when ``selector`` is supplied,
        capability selection (a prompt/UX prune, gated on by the caller). Selection runs
        *after* exposure so it only ever ranks tools the request is already allowed to use.

        ``strings`` enables the match-layer fuzzy fallback (docs/find-entities.md Consumer 1):
        when present, an intent's exact name miss is retried through the shared resolver
        before the error reaches the model. ``None`` leaves execution unchanged.

        ``entity_arguments`` enables Consumer 3, resolving an ``entity_id``-typed tool argument
        before the call runs. False reproduces stock Home Assistant behavior, where the model's
        invented id reaches the service unchanged; the eval harness pairs the two arms to
        measure what the resolution is worth. ``entity_argument_hints`` additionally tells the
        model that resolution exists, by extending those fields' descriptions, so it can pass a
        spoken name instead of spending a lookup turn on an id. It applies only alongside
        ``entity_arguments``, since the hint describes what that resolution does.
        """
        self._inner = inner
        self._policy_context = policy_context
        self._policy_registry = policy_registry
        self._strings = strings
        self._entity_arguments = entity_arguments
        self._tool_call_tasks: set[asyncio.Task[Any]] = set()
        exposed_tools = [
            tool for tool in inner.tools if self._is_exposed(tool, record_trace=True)
        ]
        tools = inner.tools if len(exposed_tools) == len(inner.tools) else exposed_tools
        if selector is not None:
            tools = self._apply_selection(tools, selector)
        if entity_arguments and entity_argument_hints and strings is not None:
            tools = [annotate_entity_arguments(tool, strings) for tool in tools]
        super().__init__(
            api=inner.api,
            api_prompt=inner.api_prompt,
            custom_serializer=inner.custom_serializer,
            llm_context=inner.llm_context,
            tools=tools,
        )

    @classmethod
    def wrap(
        cls,
        inner: llm.APIInstance,
        policy_context: ToolPolicyContext,
        policy_registry: ToolPolicyRegistry = DEFAULT_TOOL_POLICY_REGISTRY,
        *,
        entity_argument_hints: bool = True,
        entity_arguments: bool = True,
        selector: CapabilitySelector | None = None,
        strings: ConversationStrings | None = None,
    ) -> "TestbedAPI":
        """Build the policy decorator around an existing API instance."""
        return cls(
            inner,
            policy_context,
            policy_registry,
            entity_argument_hints=entity_argument_hints,
            entity_arguments=entity_arguments,
            selector=selector,
            strings=strings,
        )

    def _apply_selection(
        self, tools: list[llm.Tool], selector: CapabilitySelector
    ) -> list[llm.Tool]:
        """Prune the policy-exposed roster to the selector's plan, and trace the decision.

        Selection can only tighten the catalogued portion of the roster: an uncatalogued
        tool is retained (docs "passthrough"), so the model never loses a tool selection
        cannot reason about. The compact `SelectionTrace` explains the resulting size, and
        the full plan (scores, omissions) is available on it for a richer trace.
        """
        outcome = selector(frozenset(tool.name for tool in tools))
        selected = [tool for tool in tools if tool.name in outcome.kept]
        self._policy_context.turn_metadata.selection = SelectionTrace(
            enforced=True,
            budget=outcome.budget,
            exposed_before=len(tools),
            exposed_after=len(selected),
            admitted=outcome.plan.admitted,
            dropped=outcome.dropped,
            passthrough=outcome.passthrough,
        )
        LOGGER.debug(
            "[testbed] capability_selection exposed=%d/%d dropped=%d passthrough=%d",
            len(selected),
            len(tools),
            len(outcome.dropped),
            len(outcome.passthrough),
        )
        return selected

    async def async_call_tool(self, tool_input: llm.ToolInput) -> JsonObjectType:
        """Track the HA-owned task, then execute through policy."""
        if task := asyncio.current_task():
            # ChatLog creates one task per streamed tool call. Retain completed tasks too:
            # an abnormal stream can stop before ChatLog awaits their result, and the
            # request boundary must retrieve any exception before identity is cleared.
            self._tool_call_tasks.add(task)
        return await self._async_call_tool(tool_input)

    async def async_cancel_and_drain_tool_calls(self) -> None:
        """Cancel unfinished tool calls and retrieve every tracked task result."""
        # ChatLog may have scheduled a task immediately before its stream failed, without
        # giving that task a timeslice to enter async_call_tool and register itself here.
        await asyncio.sleep(0)
        current = asyncio.current_task()
        tasks = tuple(task for task in self._tool_call_tasks if task is not current)
        self._tool_call_tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _async_call_tool(self, tool_input: llm.ToolInput) -> JsonObjectType:
        """Recheck policy, stage confirmation, or delegate the exact call."""
        LOGGER.debug("[testbed] tool_call %s", tool_input.tool_name)
        tool = self._find_inner_tool(tool_input.tool_name)
        if tool is None:
            self._trace_tool_call(tool_input)
            self._record_trace(
                allowed=False,
                consequence=None,
                policy_source=PolicySource.UNDECLARED,
                stage="execution",
                tool_name=tool_input.tool_name,
            )
            LOGGER.debug(
                "[testbed] tool_policy %s source=%s allowed=False",
                tool_input.tool_name,
                PolicySource.UNDECLARED,
            )
            raise ToolPolicyDeniedError(tool_input.tool_name)

        resolved = self._policy_registry.resolve(tool)
        exposed = is_tool_exposed(resolved, self._policy_context)

        # Resolve entity arguments before validating or evaluating policy
        # (docs/find-entities.md Consumer 3). A friendly name is not a valid entity_id, so
        # validating first would reject the input this exists to interpret, and policy must
        # judge the entity actually being acted on rather than the model's guess at its id.
        tool_args = tool_input.tool_args
        if (arguments := self._resolve_entity_arguments(tool, tool_args)) is not None:
            if arguments.tool_args is None:
                self._trace_tool_call(tool_input)
                return arguments.tool_result
            tool_args = arguments.tool_args

        normalized_input = llm.ToolInput(
            external=tool_input.external,
            id=tool_input.id,
            tool_args=tool.parameters(tool_args),
            tool_name=tool_input.tool_name,
        )
        decision = evaluate_invocation(
            resolved,
            normalized_input.tool_args,
            self._policy_context,
        )
        allowed = exposed and decision.allowed
        self._record_trace(
            allowed=allowed,
            consequence=decision.consequence,
            policy_source=decision.source,
            stage="execution",
            tool_name=tool.name,
        )
        LOGGER.debug(
            "[testbed] tool_policy %s source=%s allowed=%s consequence=%s",
            tool.name,
            decision.source,
            allowed,
            decision.consequence,
        )
        if not allowed:
            self._trace_tool_call(normalized_input)
            raise ToolPolicyDeniedError(tool.name)

        if decision.requires_confirmation:
            self._trace_tool_call(normalized_input)
            operation = PendingOperation.create(
                arguments=normalized_input.tool_args,
                consequence=decision.consequence,
                lifetime=PENDING_OPERATION_LIFETIME,
                principal=self._policy_context.principal,
                tool_name=tool.name,
            )
            try:
                async_stage_pending(self._policy_context.session_state, operation)
            except PendingOperationAlreadyStaged:
                self._record_trace(
                    allowed=False,
                    consequence=decision.consequence,
                    policy_source=decision.source,
                    stage="confirmation_conflict",
                    tool_name=tool.name,
                )
                LOGGER.debug(
                    "[testbed] confirmation conflict for %s; keeping first operation",
                    tool.name,
                )
                return {
                    "confirmation_required": False,
                    "error": "pending_operation_already_staged",
                    "success": False,
                    "tool_name": tool.name,
                }
            return {
                "confirmation_required": True,
                "success": False,
                "tool_name": tool.name,
            }

        # HA's base implementation traces for itself. A custom override may bypass it,
        # so the proof-of-concept proxy owns that path; wrapped overrides must not add a
        # second TOOL_CALL. Core should move this ownership above every executor.
        if type(self._inner).async_call_tool is not llm.APIInstance.async_call_tool:
            self._trace_tool_call(normalized_input)

        try:
            result = await self._inner.async_call_tool(normalized_input)
        except asyncio.CancelledError:
            self._record_effect(
                tool.name,
                decision.effect,
                disposition=None,
            )
            raise
        except intent.MatchFailedError as err:
            # Fuzzy fallback for an exact name miss (docs/find-entities.md Consumer 1):
            # resolve the name and retry with the canonical id, hand back candidates for
            # the model to ask about, or, when there is nothing to try, re-raise so ChatLog
            # surfaces HA's own error unchanged.
            fallback = self._resolve_name_miss(err)
            if fallback is None:
                self._record_effect(tool.name, decision.effect, disposition=None)
                raise
            if fallback.entity_id is None:
                # Ambiguous or not found: no action was taken, so record no effect.
                return fallback.tool_result
            try:
                result = await self._inner.async_call_tool(
                    self._with_name(normalized_input, fallback.entity_id)
                )
            except Exception:
                self._record_effect(tool.name, decision.effect, disposition=None)
                raise
        except Exception:
            self._record_effect(
                tool.name,
                decision.effect,
                disposition=None,
            )
            raise
        disposition = get_undo_disposition(result)
        self._record_effect(
            tool.name,
            decision.effect,
            disposition=disposition,
        )
        LOGGER.debug(
            "[testbed] tool_result %s effect=%s undo=%s",
            tool_input.tool_name,
            decision.effect,
            type(disposition).__name__ if disposition is not None else "missing",
        )
        return public_tool_result(result)

    def _resolve_entity_arguments(
        self, tool: llm.Tool, tool_args: dict[str, Any]
    ) -> ArgumentResolution | None:
        """Resolve a call's entity_id arguments, or None to use the model's args verbatim.

        Active only when Consumer 3 is enabled and the entity supplied request-language
        ``strings`` (the unresolved and ambiguous payloads are localized); ``None`` otherwise
        leaves the call exactly as HA would have made it, so a caller that does not opt in is
        unaffected.
        """
        if self._strings is None or not self._entity_arguments:
            return None
        resolution = resolve_entity_arguments(
            self.api.hass, self.llm_context, tool, tool_args, self._strings
        )
        if resolution is not None and resolution.tool_args is not None:
            rewritten = {
                field: value
                for field, value in resolution.tool_args.items()
                if tool_args.get(field) != value
            }
            if rewritten:
                self._trace_entity_arguments(tool.name, tool_args, rewritten)
                LOGGER.debug(
                    "[testbed] resolved entity arguments for %s: %s",
                    tool.name,
                    sorted(rewritten),
                )
        return resolution

    @staticmethod
    def _trace_entity_arguments(
        tool_name: str, supplied: dict[str, Any], rewritten: dict[str, Any]
    ) -> None:
        """Record what the model actually sent, before resolution overwrote it.

        The `TOOL_CALL` event carries the arguments *as executed*, which is right for
        auditing what happened to the home but erases the model's own value. Anything asking
        what the model produced (an eval classifying whether it passed a name or an id, a
        user asking why a different entity moved) cannot recover it from the call alone.
        """
        async_conversation_trace_append(
            ConversationTraceEventType.AGENT_DETAIL,
            {
                "entity_arguments": {
                    "resolved": {
                        field: rewritten[field] for field in sorted(rewritten)
                    },
                    "supplied": {
                        field: supplied.get(field) for field in sorted(rewritten)
                    },
                    "tool_name": tool_name,
                }
            },
        )

    def _resolve_name_miss(self, err: intent.MatchFailedError):
        """Fuzzy-resolve an intent's exact name miss, or None to re-raise the error.

        Active only when the entity supplied request-language ``strings`` (the fallback's
        candidate/not-found text is localized); ``None`` otherwise leaves execution exactly
        as HA's, so a caller that does not opt in is unaffected.
        """
        if self._strings is None:
            return None
        fallback = resolve_name_miss(
            self.api.hass, self.llm_context, err, self._strings
        )
        if fallback is not None and fallback.entity_id is not None:
            LOGGER.debug(
                "[testbed] fuzzy-resolved %s name -> %s",
                err.constraints.name,
                fallback.entity_id,
            )
        return fallback

    @staticmethod
    def _with_name(tool_input: llm.ToolInput, entity_id: str) -> llm.ToolInput:
        """Copy ``tool_input`` with its name replaced by a canonical entity_id.

        `_filter_by_name` matches a literal entity_id exactly (`intent.py:432`), so the
        retry targets the resolved entity by construction.
        """
        return llm.ToolInput(
            external=tool_input.external,
            id=tool_input.id,
            tool_args={**tool_input.tool_args, "name": entity_id},
            tool_name=tool_input.tool_name,
        )

    @staticmethod
    def _trace_tool_call(tool_input: llm.ToolInput) -> None:
        """Record a call when execution will not cross HA's base API method."""
        async_conversation_trace_append(
            ConversationTraceEventType.TOOL_CALL,
            {
                "tool_args": dict(tool_input.tool_args),
                "tool_name": tool_input.tool_name,
            },
        )

    def _record_effect(
        self,
        tool_name: str,
        effect: EffectClass,
        *,
        disposition: UndoDisposition | None,
    ) -> None:
        """Journal a declared outcome or conservatively block older undo."""
        if disposition is None:
            if effect is EffectClass.READ_ONLY:
                return
            disposition = UndoUnavailable(
                description=LocalizedDescription(
                    translation_domain=DOMAIN,
                    translation_key="undo_action_tool",
                    placeholders={"tool_name": tool_name},
                ),
                reason=UndoUnavailableReason.NOT_SUPPORTED,
            )

        metadata = self._policy_context.turn_metadata
        entry = async_record_undo(
            self._policy_context.session_state,
            disposition,
            metadata.turn_id,
        )
        if entry is not None:
            metadata.effects.append(entry)

    def _find_inner_tool(self, tool_name: str) -> llm.Tool | None:
        """Return the tool the inner HA executor would resolve first."""
        return next(
            (tool for tool in self._inner.tools if tool.name == tool_name), None
        )

    def _is_exposed(self, tool: llm.Tool, *, record_trace: bool) -> bool:
        """Evaluate and optionally trace pre-model availability."""
        resolved = self._policy_registry.resolve(tool)
        allowed = is_tool_exposed(resolved, self._policy_context)
        if record_trace:
            self._record_trace(
                allowed=allowed,
                consequence=None,
                policy_source=resolved.source,
                stage="exposure",
                tool_name=tool.name,
            )
        LOGGER.debug(
            "[testbed] tool_exposure %s source=%s allowed=%s",
            tool.name,
            resolved.source,
            allowed,
        )
        return allowed

    def _record_trace(
        self,
        *,
        allowed: bool,
        consequence: object | None,
        policy_source: object,
        stage: str,
        tool_name: str,
    ) -> None:
        """Append one compact decision to current turn metadata when available."""
        metadata = self._policy_context.turn_metadata
        metadata.tool_policy.append(
            ToolPolicyTrace(
                allowed=allowed,
                consequence=str(consequence) if consequence is not None else "",
                policy_source=str(policy_source),
                stage=stage,
                tool_name=tool_name,
            )
        )
