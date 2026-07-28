# Weather by Voice

> Feature doc. State of voice weather in HA, the **current-conditions vs. forecast
> split** (the whole story), and the one thing we build: a **forecast tool** wrapping
> `weather.get_forecasts`. See [`../PRODUCT_PLAN.md`](../PRODUCT_PLAN.md),
> [`web-search.md`](web-search.md) (the overlapping-but-worse generic path), and
> [`calendar.md`](calendar.md) (same determinism-in-tools date handling).

---

## TL;DR

- **Current conditions already work** on both paths — there's a native
  `HassGetWeather` intent (local), and for the LLM the current condition +
  `temperature`/`humidity` ride in via the exposed-entity attribute allowlist / state
  injection. No build needed.
- **Forecasts are the gap, and the whole build.** Forecasts are **not state** — they
  live behind the `weather.get_forecasts` **service** (moved out of attributes in
  2023.9). `HassGetWeather` **doesn't call it**, there's **no forecast intent**, and
  **no forecast LLM tool** — and the Assist LLM API can't call arbitrary services. So
  "will it rain this weekend / what's the high Friday" is **unreachable** today.
- **The build = a `get_forecast` tool** wrapping the service, with **deterministic
  date-range handling** (§5.4): LLM passes a natural range, the tool resolves dates,
  picks daily-vs-hourly, filters periods, returns structured results the LLM
  summarizes.
- Forecast leans **LLM-summarization**, not canned templates — which is why it's a
  *tool*, not just a core intent contribution.
- **No geography.** Both paths operate on weather **entities** (one per configured
  location), not places — "what's the weather in Rome?" works only if a Rome weather
  entity exists. Entity-miss on a place is the **routing seam to `web_search`**.

---

## How it works today

### Current conditions — covered
- **Native intent `HassGetWeather`** (`weather/intent.py:18`, `const.py:80`). Slots:
  optional `name` (which weather entity). It matches a `weather` entity and returns its
  **state** (`async_set_states(matched_states=[…])`) — the default agent renders a
  spoken response from that. Note it returns *state only* — **no forecast**.
- **LLM path**: the weather entity's **state is the current condition**
  (sunny/rainy/…), and `temperature` / `humidity` are in the exposed-entity
  **`interesting_attributes` allowlist** (`homeassistant/llm.py:77–91`) → already
  surfaced via `GetLiveContextTool` / request-relevant state injection. So "what's the
  weather / temperature / is it humid" answers in-context, **no dedicated tool**.
  (Caveat: the weather entity must be **exposed** to Assist for the LLM to see it.)

### Forecasts — not reachable
- Forecasts are fetched via the **`weather.get_forecasts` service**
  (`weather/__init__.py:213`, `SERVICE_GET_FORECASTS`), gated on the
  `WeatherEntityFeature.FORECAST_DAILY` / `FORECAST_HOURLY` / `FORECAST_TWICE_DAILY`
  features. Required field `type` ∈ {`daily`,`hourly`,`twice_daily`}
  (`services.yaml:19`). The `ATTR_FORECAST_*` keys (`condition`, `temperature`,
  `templow`, `precipitation`, `precipitation_probability`, `wind_speed`, `datetime`, …
  `__init__.py:107–126`) are **per-period service output, not state attributes**.
- `HassGetWeather` **does not** call this service; **no other intent** does; **no
  `weather/llm.py`** exists (unlike light/climate/todo/… which register their intents
  as LLM tools). And `LLM_INTENTS` — the curated allowlist of intents exposed to the
  model (`intent/llm.py:22`) — is **just** TurnOn/TurnOff/CancelAllTimers/SetPosition/
  StopMoving (+ timers); **`HassGetWeather` isn't even in it.**
- The Assist LLM API exposes **intents-as-tools + GetLiveContext + per-domain tools**,
  **not arbitrary `hass.services.async_call`**. So the model has **no way to reach**
  `get_forecasts`. Forecast is genuinely gapped on **both** paths.

---

## The build: a `get_forecast` tool

A thin capability tool wrapping `weather.get_forecasts` — same shape as the calendar
and (future) reminder tools, and the same **determinism-in-tools** discipline (§5.4:
the LLM decides *intent*, the tool does the date-math/selection/filtering):

- **Inputs (LLM-supplied):** target weather entity (default = the home's primary /
  only exposed weather entity — resolve, don't ask), a **natural time reference**
  ("tomorrow", "this weekend", "Friday", "next few days", "tonight").
- **Tool does deterministically:**
  1. **Resolve the reference to a concrete date/time range** (HA `dt_util` + the
     device/home timezone — *not* the model doing date math; identical to
     `calendar.md`'s datetime-normalization).
  2. **Pick `type`**: intraday / "tonight" / "this afternoon" → `hourly` (or
     `twice_daily`); multi-day → `daily`. Honor entity feature support; degrade
     `hourly`→`daily` if hourly unsupported.
  3. Call `get_forecasts`, **filter to the resolved range**, and return a compact
     structured list (per-period condition, high/low, precip probability, wind).
- **LLM does:** **summarize** the structured result into speech ("rain likely Saturday
  afternoon, clearing Sunday, highs near 18"). This is why forecast is **LLM-leaning**
  — the value is *summarization/synthesis of structured data*, which a canned
  hassil-template response can't do well. Current-conditions can be canned; forecast
  can't.

### Generation cost
Like every tool-using command, a forecast query is **≥2 generations** (gen1 = tool_use,
gen2 = speak the summary) — the generation-counting reality from
[`prompt-context.md`](prompt-context.md). That's inherent (the model can't summarize a
forecast it hasn't fetched) and fine; forecast is an *informational* ask where a beat of
latency is acceptable, and the mic stays open for follow-ups
([`conversation-loop.md`](conversation-loop.md) informational→CONTINUE).

### Path to core
Contributing a **forecast intent** to core would help the local path too (helps-local
DNA, like find_entities / calendar). But because forecast is summarization-shaped, the
*local* rendering is inherently weaker (template over N periods) — so the tool is the
primary artifact and a core intent is a secondary, lower-value contribution. Sequence
after calendar-write.

---

## Locations: entities, not geography ("what's the weather in Rome?")

**Neither path knows any geography.** `HassGetWeather`'s only slot is `name`, matched
against **weather-entity names/areas** (`async_match_targets`, `domains=[weather]`);
`get_forecasts` targets an **entity**. There is **no lat/lon/place field and no
geocoding** anywhere in the component. A weather integration instance is **pinned to one
configured location** (typically the home). So the `name` slot means *"which of my
configured weather entities,"* **not** *"which place on Earth."*

⇒ **"What's the weather in Rome?" resolves only if a weather entity named/aliased
"Rome" exists and is exposed.** Multi-location users must add a **separate weather
integration instance per city** (each becomes its own entity). Most homes have exactly
one (their home), so bare "what's the weather" = the home entity.

**This is the routing seam to web-search.** For a place-qualified ask:
1. Resolve the place against **exposed weather entities** (the find_entities scorer —
   its Nth consumer: does a matching weather entity exist?).
2. **Hit** → use the local `get_forecast` / current-conditions path (below).
3. **Miss** → fall back to **`web_search`** ([`web-search.md`](web-search.md)) — the
   only way to answer weather for a location the home has no entity for.

### What web_search weather actually delivers (forecast-grade, not sensor-grade)
`web_search` runs a **live query at request time** (not training-cutoff-cached), but
against a **third-party provider's index** — and it returns **cited prose excerpts**,
not structured data or the live page. Two properties matter, and the *second* is the
real one:

- **Index freshness = adaptive re-crawl, not a fixed cycle.** No one recrawls the whole
  (web-scale) index on a short cadence; providers prioritize re-crawl by
  change-rate × traffic, so **popular weather pages sit in the frequently-recrawled
  bucket** (≈minutes–hours) while the long tail lags. So for a **major city**, forecast
  text is usually fresh enough. (Exact provider/cadence isn't publicly pinned down —
  don't hard-code assumptions.)
- **The dominant failure mode is snippet *extraction*, not crawl age.** Search APIs
  return **organic snippets**, generally **not** the consumer answer-box/weather-widget
  (which is a near-real-time data partnership, usually not API-exposed). And weather
  pages are **JS-rendered** — the live numbers are injected client-side and often
  **aren't in the crawled snippet at all** (or show placeholder/cached values). This
  breaks *regardless* of how fresh the crawl is.

Net by ask: **forecasts** for a well-known place ("high in Rome tomorrow", "rain this
weekend") usually carry through and are what place-qualified asks mean → **acceptable**;
**instantaneous current conditions** / exact numbers → **unreliable**, but a rare voice
ask.

**If the soft spot ever matters, the fix is a structured weather API — not `web_fetch`.**
`web_fetch` of a JS weather page hits the *same* extraction problem. The reliable
weather-anywhere answer is a **client-side weather-API tool** (Open-Meteo / met.no +
geocoding) returning exact, current, *structured* data by contract — the reason to
prefer it over `web_search` is **not** "search is stale" but that **scraping prose off
dynamic weather pages is structurally unreliable**. Net-new infra + a provider dep, so
**not v1**; v1's entity-miss fallback stays **forecast-grade**, which fits the ask.

## Weather vs. web-search (they overlap — the local tool wins *when an entity exists*)

Anthropic's server-side `web_search` *could* answer any "weather in X" from the open
web. **Prefer the local `get_forecast` tool whenever a matching weather entity exists**:
it's grounded in the **user's own configured provider and exact location**, is
**structured** (no scraping), costs **no external search**, and keeps the query
**private**. Fall back to web-search precisely on the **entity-miss** case above (no
integration for that place, or out-of-area asks like "weather in Tokyo right now").
State this preference in the prompt so the model doesn't reach for web-search on
home-weather questions — but *does* reach for it when the place isn't a configured
entity.

---

## Sources (ha-core refs)

- `homeassistant/components/weather/intent.py` — `GetWeatherIntent` (state only).
- `homeassistant/components/weather/__init__.py:213` — `get_forecasts` service;
  `:107–126` `ATTR_FORECAST_*`; `const.py:31` `WeatherEntityFeature`.
- `homeassistant/components/weather/services.yaml:19` — `get_forecasts` schema (`type`).
- `homeassistant/components/homeassistant/llm.py:77–91` — `interesting_attributes`
  (temperature/humidity surfaced; **no forecast**).
- `homeassistant/components/intent/llm.py:22` — `LLM_INTENTS` (weather **absent**).
