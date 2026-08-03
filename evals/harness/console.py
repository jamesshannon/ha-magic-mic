"""An interactive CLI to drive one utterance at a time and inspect the whole turn.

This is the *live-tracing* sibling of the offline corpus runner (`baseline.py`): the
terminal equivalent of Home Assistant's "chat with the assistant" box plus its debug-trace
tool, for hand-testing during a wave. It stands up the same headless HA and fixture world
the evals use, then lets you type utterances and watch what happens:

- the **HASSIL** probe (the local, keyless path) and whether it handled the turn, or
- the **LLM** turn: every model round's request, the tools sent, the tool calls made, the
  durable effects, token/cache cost, and the spoken answer.

Two knobs, both live-toggleable, mirroring `docs/evaluation.md` Part E:

- **Scope** (``:hassil on|off``): the full hassil→LLM path (probe local first; if it
  resolves, stop, exactly as ``prefer_local`` would) vs LLM-only (skip straight to the
  model). Start LLM-only with ``--skip-hassil``.
- **Agent** (``:agent baseline|testbed``): the stock provider agent vs the Magic Mic proxy.

The outgoing request (system prompt, tools, messages, and the cache boundary) is captured
harness-side by wrapping the provider client's ``messages.create`` for the turn: it sees the
fully composed payload, including the ``cache_control``-marked **durable** system block and
the **volatile** message list. Nothing in ``custom_components/`` changes.

The fixture world is **not** reset between turns (unlike the eval runner): state accumulates,
so "turn it on" then "now turn it off" behave, and a follow-up like "no, I meant the dining
room" works because the conversation carries a stable id. ``:reset`` restores the world and
``:new`` starts a fresh conversation.

Run it with a live key (from the environment or a project-root ``.env``):

    ANTHROPIC_API_KEY=sk-... .venv/bin/python -m evals.harness.console
    .venv/bin/python -m evals.harness.console --skip-hassil --agent baseline
    .venv/bin/python -m evals.harness.console --web-search -u "what happened today?"
    .venv/bin/python -m evals.harness.console -u "turn on the kitchen light" -u "now off"
"""

import argparse
import asyncio
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
import copy
from dataclasses import dataclass, field
import json
import logging
import sys
from time import perf_counter
from typing import Any

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_test_home_assistant,
)

import custom_components
from homeassistant import loader
from homeassistant.components import conversation
from homeassistant.components.conversation.trace import async_get_traces
from homeassistant.const import CONF_API_KEY
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar, entity_registry as er, intent

from .baseline import REPO_ROOT, load_api_key

# Running as a plain script bypasses the repo-root conftest, so graft this repo's
# `custom_components/` onto the package search path exactly as it does; otherwise HA's
# loader reports "Integration not found". Must precede the magic_mic imports below.
_REPO_CC = str(REPO_ROOT / "custom_components")
if _REPO_CC not in custom_components.__path__:
    custom_components.__path__.insert(0, _REPO_CC)

from custom_components.magic_mic.const import DOMAIN  # noqa: E402
from custom_components.magic_mic.internal.claude.const import (  # noqa: E402
    CONF_WEB_FETCH,
    CONF_WEB_SEARCH,
)

from .backing import (  # noqa: E402
    ExecutableWorld,
    Satellite,
    build_executable_world,
    register_satellite,
)
from .corpus import World, load_corpus  # noqa: E402
from .effects import ObservedEffect, effect_cursor, effects_since  # noqa: E402
from .routing import LocalOutcome  # noqa: E402
from .runner import _observe_from_trace  # noqa: E402
from .scoring import ToolCall  # noqa: E402
from .world import async_setup_local_agent  # noqa: E402

_AGENT_SUFFIXES = {"baseline": "_claude_baseline", "testbed": "_testbed"}

# Tokens (from --here or :here) that place the satellite in no room at all.
_NOWHERE = {"clear", "none", "nowhere", "unset"}


def _normalize_room(token: str) -> str:
    """Normalize a room token to a corpus area key (``Dining Room`` → ``dining_room``)."""
    return token.strip().lower().replace(" ", "_")


# ── Terminal styling (no dependency; degrades to plain text when not a tty) ──────────


class _Style:
    """Minimal ANSI helpers, disabled when stdout is not a terminal."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def cyan(self, text: str) -> str:
        return self._wrap("36", text)

    def green(self, text: str) -> str:
        return self._wrap("32", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)

    def red(self, text: str) -> str:
        return self._wrap("31", text)


# ── Request capture (the harness-side patch) ────────────────────────────────────────


@dataclass
class CapturedRequest:
    """One model round's outgoing request, reduced to what's worth inspecting."""

    system: str
    system_cached: bool
    tool_names: tuple[str, ...]
    deferred_tools: tuple[str, ...]
    messages: tuple[dict[str, Any], ...]
    elapsed_ms: float | None = None

    @classmethod
    def from_kwargs(cls, kwargs: dict[str, Any]) -> "CapturedRequest":
        """Reduce a copied ``messages.create`` payload to a display record."""
        system = kwargs.get("system")
        system_text = ""
        system_cached = False
        if isinstance(system, str):
            system_text = system
        elif isinstance(system, list):
            parts = []
            for block in system:
                parts.append(block.get("text", ""))
                if block.get("cache_control"):
                    system_cached = True
            system_text = "".join(parts)

        tool_names: list[str] = []
        deferred: list[str] = []
        for tool in kwargs.get("tools") or []:
            name = tool.get("name") or tool.get("type", "?")
            tool_names.append(name)
            if tool.get("defer_loading"):
                deferred.append(name)

        return cls(
            system=system_text,
            system_cached=system_cached or bool(kwargs.get("cache_control")),
            tool_names=tuple(tool_names),
            deferred_tools=tuple(deferred),
            messages=tuple(kwargs.get("messages") or ()),
        )


@dataclass
class _Capture:
    """Accumulates the requests seen during one turn."""

    requests: list[CapturedRequest] = field(default_factory=list)


class _TimedStream:
    """Wrap the provider's response stream to time a whole model round.

    ``messages.create(stream=True)`` returns before any tokens arrive; the generation happens
    as the provider consumes the stream. Wrapping the stream lets us stop the clock when it's
    exhausted, so the recorded time is the round's real latency (request + generation), not
    just the call setup. The provider only iterates the stream, so proxying ``__aiter__`` /
    ``__anext__`` is enough.
    """

    def __init__(self, stream: Any, on_done: Callable[[], None]) -> None:
        self._stream = stream
        self._on_done = on_done
        self._iterator: AsyncIterator[Any] | None = None

    def __aiter__(self) -> "_TimedStream":
        self._iterator = self._stream.__aiter__()
        return self

    async def __anext__(self) -> Any:
        assert self._iterator is not None
        try:
            return await self._iterator.__anext__()
        except StopAsyncIteration:
            self._on_done()
            raise


@asynccontextmanager
async def capture_requests(client: Any) -> AsyncIterator[_Capture]:
    """Wrap the provider client's ``messages.create`` to snapshot each round's payload.

    The provider builds the full request (system, tools, messages, cache markers) and hands
    it to ``client.messages.create`` once per model round. Snapshotting a deep copy there is
    the whole "what actually went to the LLM": the composed prompt is not on the conversation
    trace, so this patch is how the CLI sees it. The copy matters because the provider reuses
    and extends the ``messages`` list across rounds in place. The returned stream is wrapped
    so the recorded ``elapsed_ms`` spans the full round, not just the call.
    """
    capture = _Capture()
    original = client.messages.create

    async def _wrapped(**kwargs: Any) -> Any:
        request = CapturedRequest.from_kwargs(copy.deepcopy(kwargs))
        capture.requests.append(request)
        start = perf_counter()

        def _done() -> None:
            request.elapsed_ms = (perf_counter() - start) * 1000

        return _TimedStream(await original(**kwargs), _done)

    client.messages.create = _wrapped
    try:
        yield capture
    finally:
        client.messages.create = original


@contextmanager
def capture_service_failures() -> Iterator[list[str]]:
    """Capture handled intent service failures for the turn, without the traceback.

    When an intent matches an entity whose service isn't supported (e.g. the fixture
    thermostat has no ``climate.turn_off``), intent handling logs the failure with a full
    traceback via ``_LOGGER.exception`` and then falls back to a normal response
    (`helpers/intent.py`). This filter pulls the exception off that one record, keeps it as a
    concise ``module.Class: message`` note for the console to show, and drops the record so
    the traceback never reaches the terminal. Other intent errors pass through untouched.
    """
    notes: list[str] = []
    logger = logging.getLogger("homeassistant.helpers.intent")

    class _Capture(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            if not record.getMessage().startswith("Service call failed for "):
                return True
            exc = record.exc_info[1] if record.exc_info else None
            if exc is not None:
                cls = type(exc)
                try:
                    detail = str(exc)
                except Exception:  # noqa: BLE001 - never let formatting fail the turn
                    detail = repr(exc)
                notes.append(f"{cls.__module__}.{cls.__qualname__}: {detail}")
            else:
                notes.append(record.getMessage())
            return False

    log_filter = _Capture()
    logger.addFilter(log_filter)
    try:
        yield notes
    finally:
        logger.removeFilter(log_filter)


# ── Standing up the world ───────────────────────────────────────────────────────────


@dataclass
class Session:
    """Everything the REPL needs to drive turns."""

    hass: HomeAssistant
    entry: MockConfigEntry
    world: ExecutableWorld
    agents: dict[str, str]
    client: Any
    satellite: Satellite
    area_ids: dict[str, str]


def _agent_id(hass: HomeAssistant, entry: MockConfigEntry, suffix: str) -> str:
    """Return the entity id of the agent registered under ``suffix``."""
    ent_reg = er.async_get(hass)
    unique_id = f"{entry.entry_id}{suffix}"
    for entity in ent_reg.entities.values():
        if entity.platform == DOMAIN and entity.unique_id == unique_id:
            return entity.entity_id
    raise RuntimeError(f"agent {unique_id!r} not registered")


def _area_ids(hass: HomeAssistant, world: World) -> dict[str, str]:
    """Map each corpus area key to the id it registered under (built by the world)."""
    area_reg = ar.async_get(hass)
    return {
        key: area_reg.async_get_or_create(key.replace("_", " ")).id
        for key in world.areas
    }


def _default_area(world: World) -> str | None:
    """Pick the room the console's satellite starts in (living room, else the first)."""
    if "living_room" in world.areas:
        return "living_room"
    return world.areas[0] if world.areas else None


def _start_area_id(
    here: str | None, world: World, area_ids: dict[str, str]
) -> str | None:
    """Resolve the ``--here`` argument (or the default) to a starting area id.

    ``None`` means "not given", so use the default room; ``nowhere`` (and friends) place the
    satellite in no room; anything else matches a corpus area by key or spoken name, falling
    back to no room when it names nothing.
    """
    if here is None:
        default = _default_area(world)
        return area_ids.get(default) if default else None
    norm = _normalize_room(here)
    if norm in _NOWHERE:
        return None
    return area_ids.get(norm)


async def stand_up(
    hass: HomeAssistant,
    api_key: str,
    *,
    here: str | None,
    web_fetch: bool,
    web_search: bool,
) -> Session:
    """Set up the local core, the live integration, and the fixture world.

    Mirrors `baseline.stand_up_agent`, but keeps a handle to both agents and the client so
    the REPL can switch agents and wrap the client per turn, and places the voice satellite in
    a room (``here``, a corpus area key; default the living room) so a bare "the lights"
    resolves to it. The config-entry setup makes a real ``models.list`` call, so a bad key
    fails loudly here before any turn runs.
    """
    hass.data.pop(loader.DATA_CUSTOM_COMPONENTS, None)
    await async_setup_local_agent(hass)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_API_KEY: api_key},
        options={
            CONF_WEB_FETCH: web_fetch,
            CONF_WEB_SEARCH: web_search,
        },
    )
    entry.add_to_hass(hass)
    if not await hass.config_entries.async_setup(entry.entry_id):
        raise RuntimeError("integration failed to set up (check the key is live)")
    await hass.async_block_till_done()

    corpus_world = load_corpus().world
    world = await build_executable_world(hass, corpus_world)
    area_ids = _area_ids(hass, corpus_world)
    satellite = register_satellite(
        hass, area_id=_start_area_id(here, corpus_world, area_ids)
    )
    return Session(
        hass=hass,
        entry=entry,
        world=world,
        agents={
            name: _agent_id(hass, entry, suffix)
            for name, suffix in _AGENT_SUFFIXES.items()
        },
        client=entry.runtime_data.client,
        satellite=satellite,
        area_ids=area_ids,
    )


# ── Driving a turn ──────────────────────────────────────────────────────────────────


@dataclass
class TurnResult:
    """The observable result of one utterance."""

    local: LocalOutcome | None
    handled_locally: bool
    requests: tuple[CapturedRequest, ...]
    tools: tuple[ToolCall, ...]
    generations: list[dict[str, int]]
    speech: str
    error: str | None
    conversation_id: str | None
    hassil_ms: float | None = None
    service_failures: tuple[str, ...] = ()
    effects: tuple[ObservedEffect, ...] = ()


async def _probe_local(
    hass: HomeAssistant,
    utterance: str,
    conversation_id: str | None,
    device_id: str | None,
) -> tuple[LocalOutcome, str | None]:
    """Run one utterance through the default (HASSIL) agent and reduce the response.

    Returns the outcome and the id HA assigned the conversation, so the caller can thread it
    on to the LLM (same session) and into the next turn. ``device_id`` is the satellite, so a
    bare "the lights" resolves against its room (HASSIL injects it as ``preferred_area_id``).
    """
    try:
        result = await conversation.async_converse(
            hass,
            utterance,
            conversation_id,
            Context(),
            agent_id=None,
            device_id=device_id,
        )
    except HomeAssistantError as err:
        # HASSIL matched a sentence but executing it raised (e.g. a fixture entity that
        # doesn't support the mapped service raises ServiceNotSupported). Surface the
        # exception text as an error outcome rather than letting the traceback escape; the
        # turn then falls through to the LLM, since an errored local path isn't "resolved".
        return (
            LocalOutcome(
                response_type=intent.IntentResponseType.ERROR.value,
                error_code=intent.IntentResponseErrorCode.FAILED_TO_HANDLE.value,
                speech=str(err),
            ),
            conversation_id,
        )
    response = result.response
    speech = ""
    if response.speech:
        speech = response.speech.get("plain", {}).get("speech", "")
    return (
        LocalOutcome(
            response_type=response.response_type.value,
            error_code=response.error_code.value if response.error_code else None,
            speech=speech,
        ),
        result.conversation_id,
    )


async def drive_turn(
    session: Session,
    utterance: str,
    *,
    agent: str,
    hassil: bool,
    conversation_id: str | None,
) -> TurnResult:
    """Drive one utterance, optionally via HASSIL first, then the LLM if needed.

    With ``hassil`` on the local path is tried first; if it *resolves*, the turn stops there
    (the ``prefer_local`` win) and the LLM never runs. Otherwise, or with ``hassil`` off, the
    utterance goes to the LLM agent and the round-by-round request is captured.

    ``conversation_id`` threads multi-turn history: pass ``None`` for a fresh conversation and
    reuse the returned ``TurnResult.conversation_id`` on the next turn (HA rejects a
    self-minted ULID it didn't issue, so the id must come from a prior turn's result).
    """
    device_id = session.satellite.device_id
    local: LocalOutcome | None = None
    current_id = conversation_id
    hassil_ms: float | None = None
    effects_at_start = effect_cursor(session.hass)
    # A handled service failure on either path (the thermostat rejecting climate.turn_off) is
    # logged with a traceback deep in HA; capture it as a concise note for the whole turn.
    with capture_service_failures() as failures:
        if hassil:
            start = perf_counter()
            local, current_id = await _probe_local(
                session.hass, utterance, current_id, device_id
            )
            hassil_ms = (perf_counter() - start) * 1000
            if local.resolved:
                return TurnResult(
                    local=local,
                    handled_locally=True,
                    requests=(),
                    tools=(),
                    generations=[],
                    speech=local.speech,
                    error=None,
                    conversation_id=current_id,
                    hassil_ms=hassil_ms,
                    service_failures=tuple(failures),
                    effects=effects_since(session.hass, effects_at_start),
                )

        error: str | None = None
        speech = ""
        tools: tuple[ToolCall, ...] = ()
        generations: list[dict[str, int]] = []
        async with capture_requests(session.client) as capture:
            try:
                result = await conversation.async_converse(
                    session.hass,
                    utterance,
                    current_id,
                    Context(),
                    agent_id=session.agents[agent],
                    device_id=device_id,
                )
            except HomeAssistantError as err:
                error = str(err)
            else:
                current_id = result.conversation_id
                response = result.response
                if response.speech:
                    speech = response.speech.get("plain", {}).get("speech", "")
                if response.response_type is intent.IntentResponseType.ERROR:
                    error = speech or response.error_code.value
                tools, generations = _observe_from_trace(
                    async_get_traces()[-1].as_dict()["events"]
                )

        return TurnResult(
            local=local,
            handled_locally=False,
            requests=tuple(capture.requests),
            tools=tools,
            generations=generations,
            speech=speech,
            error=error,
            conversation_id=current_id,
            hassil_ms=hassil_ms,
            service_failures=tuple(failures),
            effects=effects_since(session.hass, effects_at_start),
        )


# ── Rendering ───────────────────────────────────────────────────────────────────────


def _summarize_message(message: dict[str, Any], style: _Style) -> list[str]:
    """Render one volatile message as one or more indented lines."""
    role = message.get("role", "?")
    content = message.get("content")
    lines: list[str] = []
    prefix = style.dim(f"  [{role}]")
    if isinstance(content, str):
        lines.append(f"{prefix} {_clip(content)}")
        return lines
    for block in content or []:
        btype = block.get("type")
        if btype == "text":
            lines.append(f"{prefix} {_clip(block.get('text', ''))}")
        elif btype == "tool_use":
            lines.append(
                f"{prefix} {style.yellow('→ ' + block.get('name', '?'))} "
                f"{_clip(str(block.get('input', {})))}"
            )
        elif btype == "server_tool_use":
            lines.append(f"{prefix} {style.yellow('⇒ ' + block.get('name', '?'))}")
        elif btype == "tool_result":
            lines.append(
                f"{prefix} {style.dim('← result ' + _clip(str(block.get('content', ''))))}"
            )
        elif btype == "thinking":
            lines.append(f"{prefix} {style.dim('[thinking]')}")
        else:
            lines.append(f"{prefix} {style.dim('[' + str(btype) + ']')}")
    return lines


def _clip(text: str, limit: int = 160) -> str:
    """Collapse whitespace and clip a string for a one-line summary."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _fmt_ms(ms: float | None) -> str:
    """Format an elapsed time for the trace (ms under a second, else seconds)."""
    if ms is None:
        return "?"
    return f"{ms:.0f} ms" if ms < 1000 else f"{ms / 1000:.2f} s"


# Inline a JSON node up to this width; wider nodes expand one key/item per line.
_INLINE_WIDTH = 80


def _flat(value: Any) -> bool:
    """True when ``value`` has no list holding more than one item, at any depth.

    This is the "collapsible" test: such a subtree is small and regular enough to read on
    one line (a dict of scalars, a single-item list, and so on).
    """
    if isinstance(value, dict):
        return all(_flat(child) for child in value.values())
    if isinstance(value, list):
        return len(value) <= 1 and (not value or _flat(value[0]))
    return True


def _compact_json(value: Any, indent: int = 0) -> str:
    """Pretty-print JSON, but keep flat, short subtrees on a single line.

    A node collapses to one line when it holds no multi-item list (`_flat`) and its compact
    form fits `_INLINE_WIDTH`; otherwise it expands with one key or item per line. This trims
    the whitespace of a full indent while still breaking up the parts that are actually big.
    """
    compact = json.dumps(value, sort_keys=True, ensure_ascii=False)
    if not isinstance(value, (dict, list)) or (
        _flat(value) and len(compact) <= _INLINE_WIDTH
    ):
        return compact

    pad = " " * (indent + 2)
    close = " " * indent
    if isinstance(value, dict):
        rows = [
            f"{pad}{json.dumps(key, ensure_ascii=False)}: "
            f"{_compact_json(child, indent + 2)}"
            for key, child in sorted(value.items())
        ]
        return "{\n" + ",\n".join(rows) + f"\n{close}}}"
    rows = [f"{pad}{_compact_json(item, indent + 2)}" for item in value]
    return "[\n" + ",\n".join(rows) + f"\n{close}]"


def _format_value(value: Any) -> str:
    """Render a tool input/result in full, compact where it can be (see `_compact_json`)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return value
    try:
        return _compact_json(value)
    except (TypeError, ValueError):
        return str(value)


def _tool_exchanges(
    requests: Sequence[CapturedRequest],
) -> list[tuple[str, Any, Any]]:
    """Pair each tool call with its result across the captured rounds.

    The provider echoes a round's ``tool_use`` and the following ``tool_result`` into the
    *next* request's messages, so scanning every captured request recovers the full call →
    result exchange (in first-seen order). This is the source for the full result the summary
    line clips.
    """
    uses: dict[str, tuple[str, Any]] = {}
    results: dict[str, Any] = {}
    order: list[str] = []
    for request in requests:
        for message in request.messages:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") == "tool_use":
                    tool_id = block.get("id")
                    if tool_id not in uses:
                        uses[tool_id] = (block.get("name", "?"), block.get("input", {}))
                        order.append(tool_id)
                elif block.get("type") == "tool_result":
                    results[block.get("tool_use_id")] = block.get("content", "")
    return [(uses[t][0], uses[t][1], results.get(t)) for t in order]


def _render_kv(label: str, value: Any, style: _Style) -> list[str]:
    """Render a labelled value block, indenting any wrapped JSON under the label."""
    parts = _format_value(value).splitlines() or [""]
    return [
        f"      {style.dim(label + ':')} {parts[0]}",
        *(f"        {line}" for line in parts[1:]),
    ]


def render_turn(result: TurnResult, style: _Style, *, verbose: bool) -> str:
    """Render a driven turn: local outcome, per-round requests, tool calls, cost, answer."""
    out: list[str] = []

    if result.local is not None:
        tag = style.green("resolved") if result.local.resolved else style.dim("passed")
        recog = "recognized" if result.local.recognized else "no-match"
        timing = (
            style.dim(f"  [{_fmt_ms(result.hassil_ms)}]")
            if result.hassil_ms is not None
            else ""
        )
        out.append(
            f"{style.bold('HASSIL')}  {tag} ({recog})"
            + (f"  {_clip(result.local.speech)}" if result.local.speech else "")
            + timing
        )

    out.extend(
        style.yellow("SERVICE FAILED") + f"  {note}" for note in result.service_failures
    )

    if result.handled_locally:
        return "\n".join(out)

    if not result.requests:
        out.append(style.dim("(no LLM request captured)"))

    for index, request in enumerate(result.requests, start=1):
        out.append("")
        header = f"── round {index}/{len(result.requests)} ──"
        if request.elapsed_ms is not None:
            header += f"  {_fmt_ms(request.elapsed_ms)}"
        out.append(style.cyan(header))
        if index == 1:
            marker = " (cache_control: ephemeral)" if request.system_cached else ""
            out.append(
                style.bold(f"DURABLE · system prompt [{len(request.system)} chars]")
                + style.dim(marker)
            )
            out.append(request.system if verbose else _indent(request.system))
        else:
            out.append(style.dim("DURABLE · system prompt unchanged (cached)"))

        out.append(style.bold(f"VOLATILE · messages [{len(request.messages)}]"))
        for message in request.messages:
            out.extend(_summarize_message(message, style))

        tool_line = f"TOOLS SENT [{len(request.tool_names)}]"
        if request.deferred_tools:
            tool_line += f" ({len(request.deferred_tools)} deferred)"
        out.append(style.bold(tool_line))
        out.append(style.dim("  " + ", ".join(request.tool_names)))

    exchanges = _tool_exchanges(result.requests)
    if exchanges:
        out.append("")
        out.append(style.bold("TOOL CALLS"))
        for name, tool_input, tool_result in exchanges:
            out.append(f"  {style.yellow('→ ' + name)}")
            out.extend(_render_kv("input", tool_input, style))
            if tool_result is not None:
                out.extend(_render_kv("result", tool_result, style))
    elif result.tools:
        # No exchange was captured (e.g. a call still open at the iteration cap); fall back
        # to the trace's call list, which has the name and args but not the result.
        out.append("")
        out.append(style.bold("TOOL CALLS"))
        out.extend(
            f"  {style.yellow('→ ' + call.name)} {_clip(str(call.args))}"
            for call in result.tools
        )

    if result.effects:
        out.append("")
        out.append(style.bold("DURABLE EFFECTS"))
        for effect in result.effects:
            out.append(f"  {style.yellow('→ ' + effect.kind)}")
            out.extend(_render_kv("data", effect.data, style))

    if result.generations:
        totals = {
            key: sum(g[key] for g in result.generations)
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_creation_tokens",
            )
        }
        total_ms = sum(r.elapsed_ms or 0 for r in result.requests)
        out.append("")
        out.append(
            style.bold(f"COST  {len(result.generations)} round(s)")
            + style.dim(
                f"  in {totals['input_tokens']} / out {totals['output_tokens']}"
                f"  cache read {totals['cache_read_tokens']} / create {totals['cache_creation_tokens']}"
                f"  time {_fmt_ms(total_ms)}"
            )
        )

    if result.error:
        out.append(style.red(f"ERROR  {result.error}"))
    elif result.speech:
        out.append(style.green("SPEECH") + f"  {result.speech}")
    return "\n".join(out)


def _indent(text: str, prefix: str = "  ") -> str:
    """Indent every line of ``text`` (for the non-verbose system-prompt block)."""
    return "\n".join(prefix + line for line in text.splitlines())


def _render_world(session: Session, style: _Style) -> str:
    """Render the fixture world as a table ordered by area (satellite included).

    Every row is area · entity id · name · state, so the satellite reads consistently with the
    entities: it sits in its room's group with ``(satellite)`` in the entity column.
    """
    hass = session.hass
    ent_reg = er.async_get(hass)
    area_reg = ar.async_get(hass)

    def area_of(entity_id: str) -> str | None:
        entry = ent_reg.async_get(entity_id)
        if entry and entry.area_id and (area := area_reg.async_get_area(entry.area_id)):
            return area.name
        return None

    # (area, entity id, name, state); the satellite is a row like any other.
    rows: list[tuple[str | None, str, str, str]] = [
        (session.satellite.area_name(hass), "(satellite)", "Voice Satellite", "")
    ]
    for state in hass.states.async_all():
        if state.domain in ("conversation", "person", "zone"):
            continue
        rows.append(
            (area_of(state.entity_id), state.entity_id, state.name, state.state)
        )
    # Order by area (unplaced last), then entity id within the area.
    rows.sort(key=lambda r: (r[0] is None, r[0] or "", r[1]))

    headers = ("AREA", "ENTITY", "NAME", "STATE")
    cells = [(area or "—", entity, name, st) for area, entity, name, st in rows]
    widths = [
        max(len(header), *(len(row[col]) for row in cells))
        for col, header in enumerate(headers)
    ]
    # One colour per column, so the satellite row matches the entity rows.
    painters = (style.bold, str, style.dim, style.cyan)

    def line(values: tuple[str, ...], paint: bool) -> str:
        parts = [
            (painters[col] if paint else style.bold)(value.ljust(widths[col]))
            for col, value in enumerate(values)
        ]
        return "  " + "  ".join(parts).rstrip()

    return "\n".join([line(headers, paint=False), *(line(row, True) for row in cells)])


# ── REPL ────────────────────────────────────────────────────────────────────────────

_HELP = """\
Type an utterance to drive a turn. Meta-commands:
  :hassil [on|off]   toggle (or show) the HASSIL-first path
  :agent [name]      switch agent (baseline | testbed)
  :new               start a fresh conversation (keeps the world)
  :reset             restore the fixture world and start a fresh conversation
  :here [room]       move the satellite to a room (or 'nowhere'); show it with no arg
  :world             list the satellite's room and current entity states
  :req               dump the last turn's raw requests (full system + messages)
  :tools             list the tools sent on the last turn
  :verbose           toggle full system-prompt printing
  :help              show this
  :quit / :q         exit\
"""


@dataclass
class ReplState:
    """Mutable REPL settings that meta-commands change."""

    agent: str
    hassil: bool
    verbose: bool
    conversation_id: str | None = None
    last: TurnResult | None = None


async def _handle_command(
    session: Session, state: ReplState, style: _Style, line: str
) -> bool:
    """Handle a ``:`` meta-command. Return False to exit the REPL."""
    parts = line.split()
    cmd, args = parts[0], parts[1:]
    if cmd in (":quit", ":q"):
        return False
    if cmd == ":help":
        print(_HELP)
    elif cmd == ":hassil":
        if args:
            state.hassil = args[0].lower() in ("on", "true", "1", "yes")
        print(style.dim(f"hassil {'on' if state.hassil else 'off'}"))
    elif cmd == ":agent":
        if args and args[0] in session.agents:
            state.agent = args[0]
        elif args:
            print(style.red(f"unknown agent {args[0]!r} (baseline | testbed)"))
        print(style.dim(f"agent {state.agent}"))
    elif cmd == ":new":
        state.conversation_id = None
        print(style.dim("new conversation"))
    elif cmd == ":reset":
        await session.world.reset(session.hass)
        state.conversation_id = None
        print(style.dim("world reset; new conversation"))
    elif cmd == ":here":
        if args:
            norm = _normalize_room(" ".join(args))
            if norm in _NOWHERE:
                session.satellite.move_to(session.hass, None)
            elif norm in session.area_ids:
                session.satellite.move_to(session.hass, session.area_ids[norm])
            else:
                print(
                    style.red(f"unknown room {' '.join(args)!r}; ")
                    + style.dim("rooms: " + ", ".join(sorted(session.area_ids)))
                )
        room = session.satellite.area_name(session.hass)
        print(style.dim(f"satellite @ {room or 'nowhere'}"))
    elif cmd == ":world":
        print(_render_world(session, style))
    elif cmd == ":verbose":
        state.verbose = not state.verbose
        print(style.dim(f"verbose {'on' if state.verbose else 'off'}"))
    elif cmd == ":req":
        if state.last and state.last.requests:
            for index, request in enumerate(state.last.requests, start=1):
                print(style.cyan(f"── round {index} system ──"))
                print(request.system)
                print(style.cyan(f"── round {index} messages ──"))
                for message in request.messages:
                    print(message)
        else:
            print(style.dim("(no captured request yet)"))
    elif cmd == ":tools":
        if state.last and state.last.requests:
            print("\n".join(f"  {name}" for name in state.last.requests[-1].tool_names))
        else:
            print(style.dim("(no captured request yet)"))
    else:
        print(style.red(f"unknown command {cmd!r}; :help for the list"))
    return True


async def repl(session: Session, state: ReplState, style: _Style) -> None:
    """Read utterances, drive turns, print traces, until EOF or :quit."""
    print(style.dim(_HELP))
    print(
        style.dim(f"\nagent={state.agent}  hassil={'on' if state.hassil else 'off'}\n")
    )
    loop = asyncio.get_running_loop()
    while True:
        try:
            line = await loop.run_in_executor(None, lambda: input(style.bold("» ")))
        except (EOFError, KeyboardInterrupt):
            print()
            return
        line = line.strip()
        if not line:
            continue
        if line.startswith(":"):
            if not await _handle_command(session, state, style, line):
                return
            continue
        state.last = await drive_turn(
            session,
            line,
            agent=state.agent,
            hassil=state.hassil,
            conversation_id=state.conversation_id,
        )
        state.conversation_id = state.last.conversation_id
        print(render_turn(state.last, style, verbose=state.verbose))
        print()


# ── Entry point ─────────────────────────────────────────────────────────────────────


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m evals.harness.console",
        description="Interactive CLI to drive utterances and inspect the turn.",
    )
    parser.add_argument(
        "--agent",
        choices=sorted(_AGENT_SUFFIXES),
        default="testbed",
        help="which agent to drive (default: testbed)",
    )
    parser.add_argument(
        "--skip-hassil",
        action="store_true",
        help="start in LLM-only scope (no HASSIL preemption)",
    )
    parser.add_argument(
        "-u",
        "--utterance",
        action="append",
        dest="utterances",
        metavar="TEXT",
        help="drive this utterance and exit (repeatable; skips the REPL)",
    )
    parser.add_argument(
        "--here",
        metavar="ROOM",
        help="room the satellite starts in, e.g. 'dining_room' (default: living room; "
        "'nowhere' for no location). Move it live with :here",
    )
    parser.add_argument(
        "--web-search",
        action="store_true",
        help="enable Claude's native web search for this console session",
    )
    parser.add_argument(
        "--web-fetch",
        action="store_true",
        help="enable Claude's native web fetch for this console session",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="disable ANSI styling",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print the full system prompt on every round",
    )
    return parser.parse_args(argv)


async def main(argv: Sequence[str] | None = None) -> None:
    """Stand up the world and either run one-shot utterances or the REPL."""
    args = _parse_args(argv)
    style = _Style(enabled=not args.no_color and sys.stdout.isatty())
    api_key = load_api_key()

    async with async_test_home_assistant() as hass:
        session = await stand_up(
            hass,
            api_key,
            here=args.here,
            web_fetch=args.web_fetch,
            web_search=args.web_search,
        )
        state = ReplState(
            agent=args.agent,
            hassil=not args.skip_hassil,
            verbose=args.verbose,
        )
        if args.utterances:
            for utterance in args.utterances:
                print(style.bold(f"» {utterance}"))
                state.last = await drive_turn(
                    session,
                    utterance,
                    agent=state.agent,
                    hassil=state.hassil,
                    conversation_id=state.conversation_id,
                )
                state.conversation_id = state.last.conversation_id
                print(render_turn(state.last, style, verbose=state.verbose))
                print()
        else:
            await repl(session, state, style)


if __name__ == "__main__":
    asyncio.run(main())
