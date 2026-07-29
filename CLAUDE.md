# CLAUDE.md

Guidance for AI coding agents working in this repository. Agents are the primary authors
here, directed by humans. Read this before making changes.

## What this project is

Magic Mic is an LLM-backed voice assistant that runs inside Home Assistant as a custom
integration (`custom_components/magic_mic`). Today it's an installable placeholder that
loads but does nothing; the capabilities are being built out. The end goal is to ship each
capability as a provider-agnostic Home Assistant primitive that can graduate into HA core.

- Read [`VISION.md`](VISION.md) for what it does and why, then
  [`PRODUCT_PLAN.md`](PRODUCT_PLAN.md) for the architecture. `PRODUCT_PLAN.md` is the source
  of truth.
- `PRODUCT_PLAN.md` §0 is the authoritative index of the design docs in [`docs/`](docs/).
  When a task touches a feature, read that feature's doc before writing code.
- Locked decisions are in `PRODUCT_PLAN.md` §3, architecture principles in §5, and the
  build order in [`docs/build-sequence.md`](docs/build-sequence.md).

## Core development principles

These are non-negotiable and shape almost every change. Full rationale in
`PRODUCT_PLAN.md` §5.

1. **The LLM decides intent and orchestration; deterministic code does the work.** Push all
   date math, arithmetic, exhaustive filtering, and fuzzy matching into tools and the HA
   layer, never into the model's head (§5.4). A tool that only works with a strong cloud
   model is a design smell.
2. **Capabilities are provider-agnostic and core-shaped.** Each capability is a
   self-contained module that depends only on `hass`, `llm.LLMContext`, `ToolInput`, and HA
   helpers, never upward on the conversation shell or the Anthropic client (§5.5). Structure
   it as if it were already a core `llm.py` platform (`async_get_tools(hass, llm_context,
   api_id) -> LLMTools`) so migration to core is close to copy/paste. The conversation shell
   is deliberately throwaway; the capabilities are the point.
3. **Multi-user from day one.** All capability data is namespaced by a resolved `user_id`
   from the first commit, behind a single `resolve_user(...)` seam (§5.1). Never key data on
   device or satellite directly; voice-ID drops into that one function later with no
   migration.
4. **Never degrade the no-AI path.** Where a capability can also land as a local HA intent,
   it should: that helps non-AI Assist users and moves the command off the cloud path
   (§2.9, §7). AI stays opt-in.
5. **Localizability is a hard requirement.** No hardcoded user-facing English. User strings
   go through `strings.json` and translations; new intents need localized sentences (§5.7).
   Retrofitting localization later is a rewrite.
6. **Prefer hashmap lookups and composed functions over new engines.** The containment
   hierarchy is a fixed-depth tree already indexed by HA's registries. Don't add a graph
   database or query engine to solve it (§5.3).
7. **Build in the neutral proxy layer; the provider is a testbed, not a dependency.** Magic
   Mic runs as a Testbed Proxy conversation agent (`magic_mic.testbed`) that wraps an internal
   provider agent (`magic_mic.internal.claude`) and interposes at the `llm.APIInstance` seam
   (tools, tool execution, `api_prompt`). Put Magic Mic logic in the proxy, not the provider.
   Claude is the demo backend because it's the easiest capable model to test against; there's
   no hard dependency on it, so don't let Claude-specific types or assumptions leak into the
   proxy or capabilities. Reach for the proxy seam first; modify `internal.claude` only when
   the HA↔LLM contract itself is what you need to change. See
   [`docs/testbed-proxy.md`](docs/testbed-proxy.md).

When a change would violate one of these, stop and raise it with the human rather than
working around it.

## Development environment

- **Python 3.14**, virtualenv at `.venv/` (already created; don't recreate it).
- **HA core is cloned at `references/core/`** (gitignored, full repo *including tests*). This
  is the authoritative local source: read it instead of fetching from GitHub. Especially
  `homeassistant/helpers/llm.py`, `components/conversation/`, and `components/anthropic/` (the
  shell this project mirrors); `tests/components/anthropic/` for test patterns (mock streams,
  fixtures); and `pyproject.toml` for the ruff config we mirror. The installed HA in
  `.venv/lib/python3.14/site-packages/homeassistant/` is the same source but without tests.
- The Home Assistant **developer docs** are cloned at `references/developers.home-assistant/`
  (gitignored). Consult them for conventions before guessing: test file structure, debugging,
  the integration quality scale. `references/developers.home-assistant/sidebars.js` indexes them.
- Tooling is in `.venv/bin/`: `ruff`, `pytest`, `hass`. Install test-only packages with
  `.venv/bin/pip install -r requirements_test.txt`.

Common commands, run from the repo root:

```
.venv/bin/ruff format .        # format
.venv/bin/ruff check .         # lint
.venv/bin/ruff check --fix .   # lint + autofix
.venv/bin/pytest               # run the test suite
```

Tests follow HA's layout: `tests/components/<domain>/` with `__init__.py`, `conftest.py`,
and `test_*.py` (mirrors core, so they move upstream unchanged). They run under
`pytest-homeassistant-custom-component`; the repo-root `conftest.py` grafts this repo's
`custom_components/` onto the loader path (the plugin otherwise shadows it), so run pytest
from the repo root. How to load the component into a live HA instance gets documented here as
that workflow settles.

## Code style

Base: the **Google Python Style Guide.** Where the **Home Assistant developer guidelines**
(https://developers.home-assistant.io/docs/development_guidelines/) differ, HA wins: this
code targets HA core, and ruff enforces HA's rules in CI.

Concrete rules (the HA specifics that override or sharpen Google's):

- **Formatting and linting are ruff**, configured by `ruff.toml`, which mirrors HA core's
  config (adapted to this repo's layout). Run `ruff format` and `ruff check --fix`; don't
  hand-format around them. Line length is 88 (the ruff/HA default, not Google's 80). Re-sync
  `ruff.toml` when the HA version in `.venv` is bumped.
- **No `from __future__ import annotations`.** HA is Python 3.14+, where PEP 649 defers
  annotation evaluation, so it's unnecessary; HA's ruff config bans it.
- **Full type hints** on every function. Type information lives in annotations, not
  docstrings. `assert`-based type narrowing only inside `TYPE_CHECKING` blocks.
- **Async by default** for I/O and HA entry points (`async_setup_entry`, and so on).
- **Logging uses lazy %-formatting, not f-strings:** `_LOGGER.debug("Connecting to %s",
  host)`. Everywhere else, prefer f-strings over `%` and `str.format()`. Log messages omit
  the component name (HA adds it) and don't end with a period. Never log secrets (keys,
  tokens, passwords). Default to `_LOGGER.debug`; reserve `.info` for genuinely user-facing
  events.
- **Comments are complete sentences ending in a period.** Docstrings follow Google style
  when extended detail helps; a one-line imperative docstring is fine for simple functions.
- **Alphabetize** constants and the contents of lists and dicts where practical (an HA
  convention).
- **Imports** grouped and ordered per HA's isort (`force-sort-within-sections`,
  `combine-as-imports`); `ruff check --fix` sorts them. Relative imports within the
  integration are allowed (as in core components); elsewhere prefer absolute.

## Working practices for agents

- **Read the relevant design doc before coding.** The architecture is meant to be argued
  with, not guessed at. If a doc and the code disagree, surface it.
- **Match the surrounding code** and HA core: naming, docstring style, and structure should
  read like the module you're editing.
- **Verify before claiming done.** At a minimum, `ruff check` and `ruff format --check` come
  back clean and `pytest` is green. Exercise new behavior rather than only typechecking it,
  and report failures honestly with the output.
- **Commit early and often:** after each coherent chunk whose checks pass, not at the end of
  a task. Frequent small commits are your rollback checkpoints when a change goes wrong, and
  they keep the working tree from becoming one unreviewable diff.
  - Work on a short-lived branch off `main`, one per task, and commit freely to it.
  - `main` is the published surface: HACS installs track it, so a pushed commit ships to
    every early tester. Keep `main` releasable. Pushing or merging to `main` is the
    outward-facing, human-gated step, not the commit.
  - Don't push to a remote or open a PR unless the human asks.
- **Upstream contributions are human-submitted.** Per the OHF AI policy there are no
  autonomous contributions to HA core: a human reviews, understands, and submits every
  upstream change (`PRODUCT_PLAN.md` §7). Your job is to prepare core-shaped, reviewable
  changes, not to file them.
- **Keep docs and code in sync.** If a change alters a decision recorded in
  `PRODUCT_PLAN.md` or a `docs/` file, update that doc in the same change.

## Writing prose

Project docs and READMEs are written to read like a human wrote them. Before writing or
editing any prose (README, VISION, design docs, commit messages, PR descriptions), consult
the `no-ai-slop` writing rules. The one that bites most often is em dashes: use commas,
colons, periods, or parentheses instead. Also avoid filler intensifiers, hype adjectives,
and templated section shapes, and end claims on a concrete fact.

## File map

```
custom_components/magic_mic/   the integration (placeholder shell today)
  __init__.py                  async_setup_entry / async_unload_entry
  config_flow.py               single-instance config flow
  const.py                     DOMAIN and constants
  manifest.json                domain, requirements, iot_class, version
  strings.json + translations/ user-facing strings (localized)
PRODUCT_PLAN.md                architecture and source of truth (§0 indexes docs/)
VISION.md                      the pitch: what it does, the standout moments
docs/                          one file per feature or topic (see PRODUCT_PLAN §0)
docs/build-sequence.md         build order, the walking skeleton, where tests land
```
