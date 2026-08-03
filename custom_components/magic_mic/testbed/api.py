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
from datetime import timedelta
from typing import Any

from homeassistant.helpers import llm
from homeassistant.util.json import JsonObjectType

from ..const import DOMAIN, LOGGER
from ..execution_result import get_undo_disposition, public_tool_result
from ..pending_operation import (
    PendingOperation,
    PendingOperationAlreadyStaged,
    async_stage_pending,
)
from ..session_state import ToolPolicyTrace
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


class TestbedAPI(llm.APIInstance):
    """Policy-enforcing decorator over the original `APIInstance`.

    This is the neutral seam. Nothing provider-specific belongs here.
    """

    def __init__(
        self,
        inner: llm.APIInstance,
        policy_context: ToolPolicyContext,
        policy_registry: ToolPolicyRegistry,
    ) -> None:
        """Initialize a filtered view while retaining the original executor."""
        self._inner = inner
        self._policy_context = policy_context
        self._policy_registry = policy_registry
        self._tool_call_tasks: set[asyncio.Task[Any]] = set()
        exposed_tools = [
            tool for tool in inner.tools if self._is_exposed(tool, record_trace=True)
        ]
        tools = inner.tools if len(exposed_tools) == len(inner.tools) else exposed_tools
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
    ) -> "TestbedAPI":
        """Build the policy decorator around an existing API instance."""
        return cls(inner, policy_context, policy_registry)

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
        normalized_input = llm.ToolInput(
            external=tool_input.external,
            id=tool_input.id,
            tool_args=tool.parameters(tool_input.tool_args),
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
            raise ToolPolicyDeniedError(tool.name)

        if decision.requires_confirmation:
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

        try:
            result = await self._inner.async_call_tool(normalized_input)
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
