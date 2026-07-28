# Web Search by Voice

> Feature doc. The surprising finding: on the **cloud (Anthropic) backend, web search
> is essentially already built** — the stock `anthropic` component wires Anthropic's
> **server-side** `web_search` + `web_fetch`, and our delivery forks that component's
> shape. The real work is a **portability seam** and **enablement/defaults**, not a
> retrieval engine. See [`../PRODUCT_PLAN.md`](../PRODUCT_PLAN.md),
> [`prompt-context.md`](prompt-context.md) (generation-counting; framing-per-backend),
> and [`weather.md`](weather.md) (overlaps; the local forecast tool wins for home
> weather).

---

## TL;DR

- **No HA-native web search exists** (nor should there be — it's not a home-automation
  primitive). But the **`anthropic` component already ships server-side `web_search`
  and `web_fetch`** (`entity.py:1035–1074`, `server_tool_use`) behind config toggles.
  **We fork that component's shape → we inherit it.**
- **Server-side = a generation-counting win.** Anthropic executes the search
  **mid-stream inside one API request** — it does **not** cost an HA tool-loop
  round-trip the way a client-side search tool would (≥2 generations). Big latency
  argument on cloud.
- **The tension is portability.** Server-side web_search is **Anthropic-coupled**
  (`server_tool_use`, not a portable `llm.py` capability platform). It lives in the
  **delivery-engine / backend-adapter**, not as a capability module. A provider-
  agnostic / local path needs a **client-side search tool** (SearXNG local, or
  Brave/Tavily cloud) — the same **framing-per-backend** split as prompt-context.
- **Enablement + defaults** is the actual scope: it's **off by default**
  (`const.py:51`), `max_uses=5`, and `user_location` is **hand-typed** in the options
  flow — an obvious **determinism-in-tools** fix (auto-fill from `hass.config`).

---

## What already exists (cloud backend)

The stock `anthropic` conversation component wires **two server-side tools**:

- **`web_search`** — `WebSearchTool20250305Param` / `…20260209Param`
  (`entity.py:1035`), added to the request `tools` list; `max_uses` configurable;
  optional `user_location` (`{type:"approximate", city, region, country, timezone}`,
  `:1048`). Results come back as `web_search_tool_result` blocks **with citations**
  (`:241`).
- **`web_fetch`** — `WebFetchTool20250910Param` / `…20260209Param` (`:1058`), for
  reading a specific URL ("read me that article", follow a link). Interacts with
  `code_execution` (uses the newer fetch tool when code-exec is on).

Both are **`server_tool_use`** (`:436`) — **Anthropic runs them**, not HA. Config keys
(`const.py:20–28`): `CONF_WEB_SEARCH`, `CONF_WEB_FETCH`, `…_MAX_USES`,
`…_USER_LOCATION`, `…_CITY/REGION/COUNTRY/TIMEZONE`. **Defaults** (`const.py:51–53`):
`web_search=False`, `user_location=False`, `max_uses=5`.

**Implication:** for the cloud backend, "web search" is **not a build** — it's a
**toggle we inherit** when we fork the component. Which reframes the whole feature.

---

## Why server-side is the right default on cloud

| | Server-side (Anthropic) | Client-side HA tool |
|---|---|---|
| **Generations** | Runs **mid-stream, 1 request** — no HA loop round-trip | ≥2 generations (tool_use → tool_result → speak) |
| **Infra** | None; Anthropic hosts | Needs a provider + API key (Brave/Tavily) or self-host (SearXNG) |
| **Citations** | Built-in | Roll your own |
| **Freshness** | Anthropic-managed index | Provider-dependent |
| **Portability** | **Anthropic-only** (`server_tool_use`) | **Provider-agnostic** (any LLM backend) |
| **Privacy** | Query → Anthropic + web | Query → chosen provider (SearXNG can be local-ish) |

The generation-counting win (from [`prompt-context.md`](prompt-context.md)) is the
decisive one on cloud: search folds into the *same* streamed response instead of forcing
the HA loop to re-generate. So **default = server-side on the Anthropic backend.**

---

## The portability seam

Server-side web_search **breaks the dependency-direction rule** (§5.4: capability
modules depend only on `hass`/`llm`/HA helpers). It's a **backend feature**, so it
belongs in the **delivery-engine/backend-adapter layer**, *not* as an `llm.py` platform
— exactly where the `anthropic` fork already puts it. To keep "the assistant can search
the web" as a **capability** rather than an Anthropic quirk, mirror prompt-context's
**framing-per-backend**:

- **Cloud/Anthropic backend** → enable the **server-side** `web_search`/`web_fetch`
  (inherited).
- **Local / provider-agnostic backend** → a **client-side `web_search` tool**
  (`llm.py`-shaped) over a pluggable provider: **SearXNG** (self-hosted metasearch, the
  local-first-aligned choice) or a cloud API (Brave/Tavily). This path *is* a portable
  capability platform; it just costs the extra round-trip.

Both satisfy one capability contract ("the model may search the web / read a URL"); the
wiring differs by backend — same pattern as typed-blocks-vs-JSONL output framing.

---

## Enablement & defaults (the actual scope)

Since the mechanism is inherited, the design work is **when it's on and how it's
configured**:

- **On/off:** ships **off**. For an assistant-grade experience, **default on** (the
  "richer info retrieval" the plan calls for) — but disclose it (queries leave the home)
  and keep it a single toggle, honoring HA local-first + the AI policy.
- **Location auto-fill (determinism-in-tools):** today `user_location` is **typed by
  hand** in the options flow. Instead **derive it from `hass.config`**
  (latitude/longitude → city/region/country; `hass.config.time_zone`) so local-relevant
  searches ("pharmacies open now") work with **zero setup** — the same "never
  interrogate, infer from the home" stance as [`scheduling-model.md`](scheduling-model.md)
  targeting. Keep it behind the existing enable-location flag for privacy.
- **`max_uses`:** default 5 is fine; it bounds cost/latency per turn.
- **When to search (prompt policy):** **model-decides** — the tool is available, the
  model reaches for it on freshness/uncertainty (current events, "what year was that
  song made", live scores). Bias the prompt to **prefer local capabilities first** where
  they exist — notably **prefer the `get_forecast` tool over web_search for home
  weather** ([`weather.md`](weather.md)).

---

## Interaction with other docs

- **weather.md** — web_search *overlaps* weather but is **worse** for home weather
  (ungrounded, unstructured, external, less private). Prefer the local forecast tool;
  fall back to web_search only for out-of-area / no-integration cases.
- **prompt-context.md** — server-side search is the one tool that **doesn't** add an HA
  generation; the "prefer local first" bias keeps it from firing on the common path.
- **memory / calendar / reminders** — web_search is **read-only external retrieval**; it
  never writes home state, so it carries none of the confirm-before-write concerns those
  docs raise.
- **security.md** — web_search/web_fetch are the **widest untrusted-content ingress**
  (and `web_fetch` of an attacker URL is fully attacker-controlled). The off-by-default
  toggle here is the coarse form of [`security.md`](security.md)'s **taint model**;
  server-side vs client-side is also the **SSRF** boundary (server-side runs on
  Anthropic's infra → no LAN SSRF; a local/client-side fetch must block private hosts).

---

## Sources (ha-core refs)

- `homeassistant/components/anthropic/entity.py:1035–1074` — server-side `web_search` /
  `web_fetch` tool params; `:1048` `user_location`; `:436` `server_tool_use`; `:241`
  citations.
- `homeassistant/components/anthropic/const.py:20–28,51–53` — config keys + defaults
  (`web_search=False`, `max_uses=5`).
- `homeassistant/components/anthropic/config_flow.py:448` — options-flow exposure.
