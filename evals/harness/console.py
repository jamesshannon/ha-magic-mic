"""An interactive CLI to drive one utterance at a time and inspect the whole turn.

This is the *live-tracing* sibling of the offline corpus runner (`baseline.py`): the
terminal equivalent of Home Assistant's "chat with the assistant" box plus its debug-trace
tool, for hand-testing during a wave. It stands up the same headless HA and fixture world
the evals use, then lets you type utterances and watch what happens:

- the **HASSIL** probe (the local, keyless path) and whether it handled the turn, or
- the **LLM** turn: every model round's request, the tools sent, the tool calls made, the
  token/cache cost, and the spoken answer.

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
    .venv/bin/python -m evals.harness.console -u "turn on the kitchen light" -u "now off"
"""

import argparse
import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, nullcontext
import copy
from dataclasses import dataclass, field
import sys
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
from homeassistant.helpers import entity_registry as er, intent

from .baseline import REPO_ROOT, load_api_key, pin_pre_magic_roster

# Running as a plain script bypasses the repo-root conftest, so graft this repo's
# `custom_components/` onto the package search path exactly as it does; otherwise HA's
# loader reports "Integration not found". Must precede the magic_mic imports below.
_REPO_CC = str(REPO_ROOT / "custom_components")
if _REPO_CC not in custom_components.__path__:
    custom_components.__path__.insert(0, _REPO_CC)

from custom_components.magic_mic.const import DOMAIN  # noqa: E402

from .backing import (  # noqa: E402
    EVAL_DEVICE_ID,
    ExecutableWorld,
    build_executable_world,
    register_timer_device,
)
from .corpus import load_corpus  # noqa: E402
from .routing import LocalOutcome  # noqa: E402
from .runner import _observe_from_trace  # noqa: E402
from .scoring import ToolCall  # noqa: E402
from .world import async_setup_local_agent  # noqa: E402

_AGENT_SUFFIXES = {"baseline": "_claude_baseline", "testbed": "_testbed"}


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


@asynccontextmanager
async def capture_requests(client: Any) -> AsyncIterator[_Capture]:
    """Wrap the provider client's ``messages.create`` to snapshot each round's payload.

    The provider builds the full request (system, tools, messages, cache markers) and hands
    it to ``client.messages.create`` once per model round. Snapshotting a deep copy there is
    the whole "what actually went to the LLM": the composed prompt is not on the conversation
    trace, so this patch is how the CLI sees it. The copy matters because the provider reuses
    and extends the ``messages`` list across rounds in place.
    """
    capture = _Capture()
    original = client.messages.create

    async def _wrapped(**kwargs: Any) -> Any:
        capture.requests.append(CapturedRequest.from_kwargs(copy.deepcopy(kwargs)))
        return await original(**kwargs)

    client.messages.create = _wrapped
    try:
        yield capture
    finally:
        client.messages.create = original


# ── Standing up the world ───────────────────────────────────────────────────────────


@dataclass
class Session:
    """Everything the REPL needs to drive turns."""

    hass: HomeAssistant
    entry: MockConfigEntry
    world: ExecutableWorld
    agents: dict[str, str]
    client: Any


def _agent_id(hass: HomeAssistant, entry: MockConfigEntry, suffix: str) -> str:
    """Return the entity id of the agent registered under ``suffix``."""
    ent_reg = er.async_get(hass)
    unique_id = f"{entry.entry_id}{suffix}"
    for entity in ent_reg.entities.values():
        if entity.platform == DOMAIN and entity.unique_id == unique_id:
            return entity.entity_id
    raise RuntimeError(f"agent {unique_id!r} not registered")


async def stand_up(hass: HomeAssistant, api_key: str) -> Session:
    """Set up the local core, the live integration, and the fixture world.

    Mirrors `baseline.stand_up_agent`, but keeps a handle to both agents and the client so
    the REPL can switch agents and wrap the client per turn. The config-entry setup makes a
    real ``models.list`` call, so a bad key fails loudly here before any turn runs.
    """
    hass.data.pop(loader.DATA_CUSTOM_COMPONENTS, None)
    await async_setup_local_agent(hass)

    entry = MockConfigEntry(domain=DOMAIN, data={CONF_API_KEY: api_key})
    entry.add_to_hass(hass)
    if not await hass.config_entries.async_setup(entry.entry_id):
        raise RuntimeError("integration failed to set up (check the key is live)")
    await hass.async_block_till_done()

    world = await build_executable_world(hass, load_corpus().world)
    register_timer_device(hass)
    return Session(
        hass=hass,
        entry=entry,
        world=world,
        agents={
            name: _agent_id(hass, entry, suffix)
            for name, suffix in _AGENT_SUFFIXES.items()
        },
        client=entry.runtime_data.client,
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


async def _probe_local(
    hass: HomeAssistant, utterance: str, conversation_id: str | None
) -> tuple[LocalOutcome, str]:
    """Run one utterance through the default (HASSIL) agent and reduce the response.

    Returns the outcome and the id HA assigned the conversation, so the caller can thread it
    on to the LLM (same session) and into the next turn.
    """
    result = await conversation.async_converse(
        hass, utterance, conversation_id, Context(), agent_id=None
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
    local: LocalOutcome | None = None
    current_id = conversation_id
    if hassil:
        local, current_id = await _probe_local(session.hass, utterance, current_id)
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
                device_id=EVAL_DEVICE_ID,
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


def render_turn(result: TurnResult, style: _Style, *, verbose: bool) -> str:
    """Render a driven turn: local outcome, per-round requests, tool calls, cost, answer."""
    out: list[str] = []

    if result.local is not None:
        tag = style.green("resolved") if result.local.resolved else style.dim("passed")
        recog = "recognized" if result.local.recognized else "no-match"
        out.append(
            f"{style.bold('HASSIL')}  {tag} ({recog})"
            + (f"  {_clip(result.local.speech)}" if result.local.speech else "")
        )
    if result.handled_locally:
        return "\n".join(out)

    if not result.requests:
        out.append(style.dim("(no LLM request captured)"))

    for index, request in enumerate(result.requests, start=1):
        out.append("")
        out.append(style.cyan(f"── round {index}/{len(result.requests)} ──"))
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

    out.append("")
    if result.tools:
        out.append(style.bold("TOOL CALLS"))
        out.extend(
            f"  {style.yellow(call.name)} {_clip(str(call.args))}"
            for call in result.tools
        )

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
        out.append(
            style.bold(f"COST  {len(result.generations)} round(s)")
            + style.dim(
                f"  in {totals['input_tokens']} / out {totals['output_tokens']}"
                f"  cache read {totals['cache_read_tokens']} / create {totals['cache_creation_tokens']}"
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
    """List the current fixture entity states."""
    lines = [style.bold("WORLD")]
    for state in sorted(session.hass.states.async_all(), key=lambda s: s.entity_id):
        if state.domain in ("conversation", "person", "zone"):
            continue
        lines.append(f"  {state.entity_id} = {style.cyan(state.state)}")
    return "\n".join(lines)


# ── REPL ────────────────────────────────────────────────────────────────────────────

_HELP = """\
Type an utterance to drive a turn. Meta-commands:
  :hassil [on|off]   toggle (or show) the HASSIL-first path
  :agent [name]      switch agent (baseline | testbed)
  :new               start a fresh conversation (keeps the world)
  :reset             restore the fixture world and start a fresh conversation
  :world             list current entity states
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
        "--pin-baseline",
        action="store_true",
        help="disable the shipped web tools (the locked pre-magic reference)",
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
        with pin_pre_magic_roster() if args.pin_baseline else nullcontext():
            session = await stand_up(hass, api_key)
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
