"""Tests for provider-neutral tool policy classification."""

from collections.abc import Mapping
from typing import Any

import pytest

from custom_components.magic_mic.identity import (
    UNIDENTIFIED_PRINCIPAL,
    DataScope,
    ResolvedPrincipal,
)
from custom_components.magic_mic.pending_operation import ConsequenceClass
from custom_components.magic_mic.session_state import MagicMicSessionState
from custom_components.magic_mic.tool_policy import (
    CallPolicy,
    EffectClass,
    ExposurePolicy,
    PolicySource,
    StaticToolPolicy,
    ToolPolicy,
    ToolPolicyContext,
    ToolPolicyRegistry,
    evaluate_invocation,
    is_tool_exposed,
    tool_policy,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.util.json import JsonObjectType


class FixtureTool(llm.Tool):
    """Minimal tool used only for policy resolution."""

    name = "fixture"

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Return an unused fixture result."""
        return {}


class FixtureSubclassTool(FixtureTool):
    """Tool subclass used to verify family policy lookup."""


class ArgumentPolicy(ToolPolicy):
    """Require personal scope only when arguments request it."""

    def exposure_policy(self, context: ToolPolicyContext) -> ExposurePolicy:
        """Keep the mixed-scope tool visible to household callers."""
        return ExposurePolicy(required_scope=DataScope.HOUSEHOLD)

    def classify_call(
        self,
        arguments: Mapping[str, Any],
        context: ToolPolicyContext,
    ) -> CallPolicy:
        """Classify the normalized scope argument."""
        return CallPolicy(
            consequence=ConsequenceClass.CONFIRM_ON_CONTINUATION,
            required_scope=(
                DataScope.PERSONAL
                if arguments.get("scope") == "personal"
                else DataScope.HOUSEHOLD
            ),
        )


def _context(
    principal: ResolvedPrincipal = UNIDENTIFIED_PRINCIPAL,
    *,
    is_continuation: bool = False,
    minimum_consequence: ConsequenceClass = ConsequenceClass.LOW,
) -> ToolPolicyContext:
    """Build deterministic policy context for a fixture request."""
    session_state = MagicMicSessionState()
    turn_metadata = session_state.async_begin_turn(
        "turn-1",
        is_continuation=is_continuation,
        principal=principal,
    )
    return ToolPolicyContext(
        is_continuation=is_continuation,
        minimum_consequence=minimum_consequence,
        principal=principal,
        session_state=session_state,
        turn_metadata=turn_metadata,
    )


def test_unclassified_tools_remain_explicitly_permissive() -> None:
    """The POC preserves pass-through while retaining unclassified provenance."""
    resolved = ToolPolicyRegistry().resolve(FixtureTool())

    assert resolved.source is PolicySource.UNCLASSIFIED
    assert resolved.policy is None
    assert is_tool_exposed(resolved, _context())
    decision = evaluate_invocation(resolved, {}, _context())
    assert decision.allowed
    assert decision.consequence is ConsequenceClass.LOW
    assert decision.effect is EffectClass.UNKNOWN
    assert not decision.requires_confirmation


def test_declared_policy_takes_precedence_over_legacy_registry() -> None:
    """A tool can migrate out of the central registry without changing consumers."""
    declared = StaticToolPolicy(required_scope=DataScope.PERSONAL)

    @tool_policy(declared)
    class DeclaredTool(FixtureTool):
        """Fixture with policy supplied beside its implementation."""

    registry = ToolPolicyRegistry()
    registry.register_type(
        FixtureTool,
        StaticToolPolicy(required_scope=DataScope.HOUSEHOLD),
    )

    resolved = registry.resolve(DeclaredTool())

    assert resolved.source is PolicySource.DECLARED
    assert resolved.policy is declared


def test_namespaced_tool_resolves_underlying_declared_policy() -> None:
    """HA aggregation does not hide policy declared by an owned tool."""
    declared = StaticToolPolicy(effect=EffectClass.READ_ONLY)

    @tool_policy(declared)
    class DeclaredTool(FixtureTool):
        """Fixture with policy attached before HA wraps it."""

    resolved = ToolPolicyRegistry().resolve(
        llm.NamespacedTool("assist", DeclaredTool())
    )

    assert resolved.source is PolicySource.DECLARED
    assert resolved.policy is declared


def test_namespaced_tool_resolves_underlying_exact_legacy_policy() -> None:
    """An exact compatibility entry uses the original type and name."""
    exact = StaticToolPolicy(required_scope=DataScope.PERSONAL)
    registry = ToolPolicyRegistry()
    registry.register_exact(FixtureTool, "fixture", exact)

    resolved = registry.resolve(llm.NamespacedTool("assist", FixtureTool()))

    assert resolved.source is PolicySource.LEGACY_EXACT
    assert resolved.policy is exact


def test_namespaced_tool_resolves_underlying_family_policy() -> None:
    """A family compatibility entry follows the original tool hierarchy."""
    family = StaticToolPolicy(required_scope=DataScope.HOUSEHOLD)
    registry = ToolPolicyRegistry()
    registry.register_type(FixtureTool, family)

    resolved = registry.resolve(llm.NamespacedTool("assist", FixtureSubclassTool()))

    assert resolved.source is PolicySource.LEGACY_TYPE
    assert resolved.policy is family


def test_static_policy_declares_effect_class() -> None:
    """Effect classification travels with invocation policy."""
    registry = ToolPolicyRegistry()
    registry.register_type(
        FixtureTool,
        StaticToolPolicy(effect=EffectClass.READ_ONLY),
    )

    decision = evaluate_invocation(registry.resolve(FixtureTool()), {}, _context())

    assert decision.effect is EffectClass.READ_ONLY


def test_legacy_exact_policy_takes_precedence_over_family_policy() -> None:
    """Specific known tools can override a broad legacy family classifier."""
    exact = StaticToolPolicy(required_scope=DataScope.PERSONAL)
    registry = ToolPolicyRegistry()
    registry.register_exact(FixtureTool, "fixture", exact)
    registry.register_type(FixtureTool, StaticToolPolicy())

    resolved = registry.resolve(FixtureTool())

    assert resolved.source is PolicySource.LEGACY_EXACT
    assert resolved.policy is exact


def test_legacy_family_policy_applies_through_subclasses() -> None:
    """A registered family policy follows the tool type's method resolution order."""
    family = StaticToolPolicy(required_scope=DataScope.HOUSEHOLD)
    registry = ToolPolicyRegistry()
    registry.register_type(FixtureTool, family)

    resolved = registry.resolve(FixtureSubclassTool())

    assert resolved.source is PolicySource.LEGACY_TYPE
    assert resolved.policy is family


def test_duplicate_registrations_and_decorators_are_rejected() -> None:
    """Policy replacement must be explicit instead of depending on import order."""
    registry = ToolPolicyRegistry()
    registry.register_exact(FixtureTool, "fixture", StaticToolPolicy())
    registry.register_type(FixtureTool, StaticToolPolicy())

    with pytest.raises(ValueError):
        registry.register_exact(FixtureTool, "fixture", StaticToolPolicy())
    with pytest.raises(ValueError):
        registry.register_type(FixtureTool, StaticToolPolicy())

    @tool_policy(StaticToolPolicy())
    class DecoratedTool(FixtureTool):
        """Fixture that already has a declared policy."""

    with pytest.raises(ValueError):
        tool_policy(StaticToolPolicy())(DecoratedTool)


def test_personal_scope_filters_before_exposure() -> None:
    """Personal tools are hidden from unidentified household callers."""
    registry = ToolPolicyRegistry()
    registry.register_type(
        FixtureTool,
        StaticToolPolicy(required_scope=DataScope.PERSONAL),
    )
    resolved = registry.resolve(FixtureTool())

    assert not is_tool_exposed(resolved, _context())
    assert is_tool_exposed(
        resolved,
        _context(ResolvedPrincipal(user_id="person-1")),
    )


def test_argument_policy_rechecks_scope_at_invocation() -> None:
    """A mixed-scope tool can expose broadly and reject a personal invocation."""
    registry = ToolPolicyRegistry()
    registry.register_type(FixtureTool, ArgumentPolicy())
    resolved = registry.resolve(FixtureTool())
    context = _context()

    assert is_tool_exposed(resolved, context)
    assert evaluate_invocation(resolved, {"scope": "household"}, context).allowed
    assert not evaluate_invocation(resolved, {"scope": "personal"}, context).allowed


def test_consequence_escalates_but_never_lowers() -> None:
    """Continuation and request signals can only raise the deterministic base."""
    registry = ToolPolicyRegistry()
    registry.register_type(FixtureTool, ArgumentPolicy())
    resolved = registry.resolve(FixtureTool())

    ordinary = evaluate_invocation(resolved, {}, _context())
    continuation = evaluate_invocation(
        resolved,
        {},
        _context(is_continuation=True),
    )
    raised = evaluate_invocation(
        resolved,
        {},
        _context(minimum_consequence=ConsequenceClass.ALWAYS_CONFIRM),
    )

    assert ordinary.consequence is ConsequenceClass.CONFIRM_ON_CONTINUATION
    assert not ordinary.requires_confirmation
    assert continuation.requires_confirmation
    assert raised.consequence is ConsequenceClass.ALWAYS_CONFIRM
    assert raised.requires_confirmation
