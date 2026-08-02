"""Provider-neutral tool exposure and invocation policy.

Tools owned by Magic Mic may declare a policy directly, usually with
``@tool_policy(...)``. Existing Home Assistant and third-party tools cannot yet do
that, so ``ToolPolicyRegistry`` supplies the same contract externally. The registry
is migration machinery, not a blocklist: until core tools publish stable policy and
provenance metadata, it is the authoritative policy source for classified legacy
tools.

Policy evaluation is deliberately split in two. ``exposure_policy()`` uses facts
available before a model generation, while ``classify_call()`` may inspect normalized
arguments immediately before execution. The latter is required for broad tools whose
consequence depends on the requested domain or operation.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm

from .const import DOMAIN
from .identity import DataScope, ResolvedPrincipal
from .pending_operation import ConsequenceClass
from .session_state import MagicMicSessionState, TurnMetadata

_POLICY_ATTRIBUTE = "magic_mic_tool_policy"
_ToolT = TypeVar("_ToolT", bound=type[llm.Tool])


class PolicySource(StrEnum):
    """Where the effective policy for a tool came from."""

    DECLARED = "declared"
    LEGACY_EXACT = "legacy_exact"
    LEGACY_TYPE = "legacy_type"
    UNCLASSIFIED = "unclassified"


class EffectClass(StrEnum):
    """Whether a successful invocation can change durable state."""

    MUTATING = "mutating"
    READ_ONLY = "read_only"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ToolPolicyContext:
    """Deterministic request facts shared by both policy stages."""

    principal: ResolvedPrincipal
    session_state: MagicMicSessionState
    turn_metadata: TurnMetadata
    is_continuation: bool = False
    minimum_consequence: ConsequenceClass = ConsequenceClass.LOW


@dataclass(frozen=True, slots=True)
class ExposurePolicy:
    """Requirements knowable before tool arguments exist."""

    required_scope: DataScope | None = None


@dataclass(frozen=True, slots=True)
class CallPolicy:
    """Requirements for one normalized invocation."""

    consequence: ConsequenceClass = ConsequenceClass.LOW
    effect: EffectClass = EffectClass.UNKNOWN
    required_scope: DataScope | None = None


@dataclass(frozen=True, slots=True)
class InvocationDecision:
    """Evaluated authorization and confirmation requirements for one call."""

    allowed: bool
    consequence: ConsequenceClass
    effect: EffectClass
    requires_confirmation: bool
    required_scope: DataScope | None
    source: PolicySource


class ToolPolicyDeniedError(HomeAssistantError):
    """A tool invocation failed the deterministic execution policy."""

    def __init__(self, tool_name: str) -> None:
        """Initialize a localizable denial without leaking policy details."""
        self.tool_name = tool_name
        super().__init__(
            translation_domain=DOMAIN,
            translation_key="tool_not_available",
            translation_placeholders={"tool_name": tool_name},
        )


class ToolPolicy(ABC):
    """Classify one tool before exposure and again before invocation."""

    @abstractmethod
    def exposure_policy(self, context: ToolPolicyContext) -> ExposurePolicy:
        """Return requirements available before a model sees the tool."""

    @abstractmethod
    def classify_call(
        self,
        arguments: Mapping[str, Any],
        context: ToolPolicyContext,
    ) -> CallPolicy:
        """Return requirements for normalized invocation arguments."""


@dataclass(frozen=True, slots=True)
class ResolvedToolPolicy:
    """A tool policy plus the source that supplied it."""

    policy: ToolPolicy | None
    source: PolicySource


@dataclass(frozen=True, slots=True)
class StaticToolPolicy(ToolPolicy):
    """Apply one scope and base consequence to every invocation of a tool."""

    consequence: ConsequenceClass = ConsequenceClass.LOW
    effect: EffectClass = EffectClass.UNKNOWN
    required_scope: DataScope | None = None

    def exposure_policy(self, context: ToolPolicyContext) -> ExposurePolicy:
        """Return the static pre-exposure requirement."""
        return ExposurePolicy(required_scope=self.required_scope)

    def classify_call(
        self,
        arguments: Mapping[str, Any],
        context: ToolPolicyContext,
    ) -> CallPolicy:
        """Return the static invocation requirement."""
        return CallPolicy(
            consequence=self.consequence,
            effect=self.effect,
            required_scope=self.required_scope,
        )


class ToolPolicyRegistry:
    """Resolve policies for declared and otherwise-unannotated tools.

    Exact legacy registrations use both the concrete tool type and tool name. This
    avoids pretending that the flattened HA API currently provides a stable global
    tool identity. Type registrations are useful only for families with genuinely
    uniform policy; argument-dependent families should register a classifier.
    """

    def __init__(self) -> None:
        """Initialize an empty legacy-policy registry."""
        self._exact: dict[tuple[type[llm.Tool], str], ToolPolicy] = {}
        self._types: dict[type[llm.Tool], ToolPolicy] = {}

    def register_exact(
        self,
        tool_type: type[llm.Tool],
        tool_name: str,
        policy: ToolPolicy,
    ) -> None:
        """Register a policy for one legacy concrete type and tool name."""
        key = (tool_type, tool_name)
        if key in self._exact:
            raise ValueError(
                f"Policy already registered for {tool_type.__name__}.{tool_name}"
            )
        self._exact[key] = policy

    def register_type(
        self,
        tool_type: type[llm.Tool],
        policy: ToolPolicy,
    ) -> None:
        """Register a fallback policy for a legacy tool family."""
        if tool_type in self._types:
            raise ValueError(f"Policy already registered for {tool_type.__name__}")
        self._types[tool_type] = policy

    def resolve(self, tool: llm.Tool) -> ResolvedToolPolicy:
        """Resolve declared policy, exact legacy policy, then type policy."""
        if (declared := getattr(tool, _POLICY_ATTRIBUTE, None)) is not None:
            if not isinstance(declared, ToolPolicy):
                raise TypeError(
                    f"{type(tool).__name__}.{_POLICY_ATTRIBUTE} must be a ToolPolicy"
                )
            return ResolvedToolPolicy(declared, PolicySource.DECLARED)

        tool_type = type(tool)
        if (exact := self._exact.get((tool_type, tool.name))) is not None:
            return ResolvedToolPolicy(exact, PolicySource.LEGACY_EXACT)

        for candidate_type in tool_type.__mro__:
            if (family := self._types.get(candidate_type)) is not None:
                return ResolvedToolPolicy(family, PolicySource.LEGACY_TYPE)

        return ResolvedToolPolicy(None, PolicySource.UNCLASSIFIED)


def tool_policy(policy: ToolPolicy) -> Callable[[_ToolT], _ToolT]:
    """Attach a policy to a Magic Mic-owned tool class."""

    def decorate(tool_type: _ToolT) -> _ToolT:
        if _POLICY_ATTRIBUTE in tool_type.__dict__:
            raise ValueError(f"{tool_type.__name__} already declares a tool policy")
        setattr(tool_type, _POLICY_ATTRIBUTE, policy)
        return tool_type

    return decorate


def is_tool_exposed(
    resolved: ResolvedToolPolicy,
    context: ToolPolicyContext,
) -> bool:
    """Return whether a tool is available to the current principal."""
    if resolved.policy is None:
        return True
    required_scope = resolved.policy.exposure_policy(context).required_scope
    return required_scope is None or context.principal.can_access(required_scope)


def evaluate_invocation(
    resolved: ResolvedToolPolicy,
    arguments: Mapping[str, Any],
    context: ToolPolicyContext,
) -> InvocationDecision:
    """Evaluate scope and effective consequence immediately before execution."""
    call_policy = (
        resolved.policy.classify_call(arguments, context)
        if resolved.policy is not None
        else CallPolicy()
    )
    consequence = max_consequence(
        call_policy.consequence,
        context.minimum_consequence,
    )
    required_scope = call_policy.required_scope
    allowed = required_scope is None or context.principal.can_access(required_scope)
    return InvocationDecision(
        allowed=allowed,
        consequence=consequence,
        effect=call_policy.effect,
        required_scope=required_scope,
        requires_confirmation=_requires_confirmation(consequence, context),
        source=resolved.source,
    )


def max_consequence(
    first: ConsequenceClass,
    second: ConsequenceClass,
) -> ConsequenceClass:
    """Return the stricter ordinal consequence class."""
    order = {
        ConsequenceClass.LOW: 0,
        ConsequenceClass.CONFIRM_ON_CONTINUATION: 1,
        ConsequenceClass.ALWAYS_CONFIRM: 2,
    }
    return first if order[first] >= order[second] else second


def _requires_confirmation(
    consequence: ConsequenceClass,
    context: ToolPolicyContext,
) -> bool:
    """Return whether the effective consequence requires confirmation now."""
    return consequence is ConsequenceClass.ALWAYS_CONFIRM or (
        consequence is ConsequenceClass.CONFIRM_ON_CONTINUATION
        and context.is_continuation
    )


DEFAULT_TOOL_POLICY_REGISTRY = ToolPolicyRegistry()


__all__ = [
    "DEFAULT_TOOL_POLICY_REGISTRY",
    "CallPolicy",
    "EffectClass",
    "ExposurePolicy",
    "InvocationDecision",
    "PolicySource",
    "ResolvedToolPolicy",
    "StaticToolPolicy",
    "ToolPolicy",
    "ToolPolicyContext",
    "ToolPolicyDeniedError",
    "ToolPolicyRegistry",
    "evaluate_invocation",
    "is_tool_exposed",
    "max_consequence",
    "tool_policy",
]
