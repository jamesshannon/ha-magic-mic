# Music / Media Playback by Voice

> Feature doc. State of voice-controlled music in HA, what works vs. what
> doesn't, the key UX principle (**optimistic play, not clarify-first**), and how
> it fits our LLM agent. See [`../PRODUCT_PLAN.md`](../PRODUCT_PLAN.md) and
> [`find-entities.md`](find-entities.md) (the disambiguation parallel that
> *inverts* here).

---

## TL;DR

- Basic **play-by-voice works** via the native `HassMediaSearchAndPlay` intent
  (search → play best result) — but effectively **requires Music Assistant** as
  the search backend, and it **plays the first result with no disambiguation**.
- The LLM path is the *strong* path: the model populates `media_class`
  (song/artist/album/playlist) from natural language far better than hassil.
- **UX principle: optimistic best-match play + cheap correction, NOT
  clarify-first.** For a huge catalog, clarifying would fire on nearly every
  request. This is the opposite of the entity-disambiguation pattern — the
  cost-benefit inverts.
- **Assistant-grade gaps remain and have no public roadmap:** "what's playing,"
  "like/favorite," queue control, "play something similar." These are our
  custom-tool opportunities.

---

## How it works today

Two paths, often conflated in commentary:

### 1. Native core intent — `HassMediaSearchAndPlay`
`media_player/intent.py:278`. Slots: required `search_query` (free text) +
optional `media_class` (`artist`/`album`/`playlist`/`track`/… from the
`MediaClass` enum) + name/area/floor for the target player. It:
1. matches a target `media_player` requiring the `SEARCH_MEDIA` + `PLAY_MEDIA`
   features,
2. calls the `search_media` service,
3. **plays `results[0]`** (the first result).

Exposed to the LLM as a tool (`media_player/llm.py`). Two catches:
- **Backend must implement `search_media`** — in practice ≈ **Music Assistant**;
  the stock Spotify integration's player historically does not.
- **First-result-only — no disambiguation** (by design; see UX principle).

### 2. Music Assistant blueprints
The `music-assistant/voice-support` repo. MA's docs still say *"there is no
built-in support in HA or MA for initiating music playback by voice"* and steer
users to custom blueprints/intent-scripts (local **or** LLM-based recognition).
This lags the native intent (below) but is what MA officially recommends, partly
because the native intent's *local* sentence coverage is thin and MA blueprints
handle MA-specific queue behavior.

### History
MA became a **native HA integration in 2024.12**. HA added `search_media` + the
**Search-and-Play intent**, announced in **Voice Chapter 10 (June 2025)**,
specifically because "central pieces were missing... such as the ability to
search a media library."

---

## Per-capability support

| Ask | Supported? | How / caveat |
|---|---|---|
| Play a **song** | Yes | `search_query` + `media_class=track`; quality = backend search; plays `results[0]` |
| Play a **playlist** (bounded) | **Yes — best case** | `media_class=playlist`; named library items → reliable |
| Play an **artist** (unbounded, fuzzy) | Yes, weakest | `media_class=artist`; fuzzy match **delegated to provider**; "play Coldplay" fine, obscure/ambiguous → wrong result, no disambiguation |
| Play **album / radio / similar** | Partly | `media_class=album`; MA's *"Don't stop the music"* auto-plays similar after the queue ends |
| **"What's playing?"** | **LLM: yes today / local: no intent** | `media_title`/`media_artist`/`media_album_name` are in the exposed-entity attribute allowlist (`homeassistant/llm.py:88–90`) → already surfaced via `GetLiveContextTool`/state injection, **no new tool needed**. Local (no-LLM) agent has **no `HassMediaWhatsPlaying` sentence intent** → can't answer. See §"What's-playing" below |
| **"Like / favorite this"** | **No** | No intent/tool; MA has favorite *services*, not voice-exposed |
| **Queue control** | **Mostly no** | No voice intent for queue manipulation |

**Fuzzy matching is not HA's** — it's delegated to the provider/MA search, and HA
plays `results[0]`. No confidence, no "did you mean," no ranking control at the
intent level.

**Spotify specifically:** decent *via Music Assistant* (Spotify as an MA
provider; MA 2.4 added Spotify Connect). Weak via the *native* Spotify
integration directly — hence community projects (SpotifyPlus,
`spotify-voice-assistant`).

---

## "What's playing?" / "what song is this?"

**The LLM path already works — no new tool.** `media_title`, `media_artist`,
`media_album_name` are in the exposed-entity **attribute allowlist**
(`homeassistant/llm.py:77–91`), so the currently-playing track is already handed to
the model via `GetLiveContextTool` (and via request-relevant state injection — this
is a clean tier-2 case: a media query pulls the active player's state into context).
The follow-up chain is free from the same mechanism:

> "what's playing?" → *"Roygbiv by Boards of Canada"* → "cool, what year was that
> made?" → LLM answers from world-knowledge (later: the web-search tool).

The mic staying open for that follow-up is exactly `conversation-loop.md`'s
**informational-Q&A → CONTINUE** branch. So for our agent the whole interaction is
already covered.

**The net-new is the *local* path** — your Hassil instinct. There's no
`HassMediaWhatsPlaying` sentence intent, so an **offline / no-LLM** Assist can't
answer turn 1. Worth adding, and it's **core-contribution-shaped** (same "helps
local too" DNA as the find_entities / calendar intents): resolve the active
`media_player` → read `media_title`/`media_artist` → speak a canned *"X by Y."*
Ceiling to keep in mind: the *follow-up* ("what year") is inherently **un-local**
(world-knowledge), so once it goes conversational you're on the LLM path regardless
— the local intent's value is bounded to the standalone turn-1 factual read.

**Two meanings of "what song is this?"** (don't conflate):
- **(a) what's playing on my speaker** — a state read; handled as above.
- **(b) what's this ambient audio I'm hearing** — Shazam-style acoustic
  fingerprinting of mic input. HA does **not** do this; needs an external
  audio-recognition service (AudD / ACRCloud). Different, much harder capability —
  **out of scope**, not part of this intent.

## Multi-room: transfer / expand — a real gap, and it *inverts* the UX principle

Moving or spreading audio across speakers ("play this in the living room," "move
the living room music here," "play it in here too") is **not voice-reachable in HA
today**, and it's missing from the gap list above.

- **Service plumbing exists:** `media_player.join` / `unjoin`
  (`media_player/__init__.py:384,469`), a `group_members` state attribute (`:797`),
  and the `GROUPING` feature flag; Music Assistant adds queue-transfer on top.
- **But there is no intent for it.** The full media intent set is Pause, Unpause,
  Mute/Unmute, SearchAndPlay, SetVolumeRelative (`media_player/intent.py`) — nothing
  for join/unjoin/transfer. **No intent ⇒ no local voice path ⇒ nothing inherited as
  an LLM tool** via the Assist API. Same class as the *what's-playing / favorite /
  queue* gaps → a **custom-tool opportunity**. Two sub-cases:
  - **Transfer/move** — source stops, target starts (`unjoin` source, `join`/play on
    target, or MA queue-transfer).
  - **Expand** ("...in here *too*") — `join` this player into the current group.

**This inverts the doc's core UX principle.** Catalog search is optimistic
best-match because ambiguity is the norm over a huge set; but the *operands here are
speakers* — a small, exact, named set. So resolving the **target player** is
`find_entities`-shaped (**clarify-when-close**), even while the **content** half
stays optimistic. One request, two policies: exact-match the speaker, best-match the
music.

### Music-follow ("play what's playing wherever I go") — out of scope, but proven
Presence-driven music-follow exists **only as community DIY** (Spotifynd, the
ESPresense **"Room Music Follow"** blueprints, **"Group Sonos based on presence"**) —
nothing in core. Each is the same assembly: **room presence → `media_player`
grouping/transfer.** We do **not** build it. Its targeting half is the *same
primitive* as reminder delivery-follow, scoped as the **presence → target-set
resolver** in [`scheduling-model.md`](scheduling-model.md); music-follow is a future
*consumer* of that resolver (payload = the transfer action above), and the voice
surface is just a trigger phrase ("follow me"). Listed here only so the pointer
exists.

## UX principle: optimistic play, not clarify-first

The `find_entities` "return candidates → disambiguate" pattern **does not apply
here** — the cost-benefit inverts:

| | Entities | Music |
|---|---|---|
| Set size | Small | Huge (Spotify-scale) |
| Ambiguity | Rare | The **norm** (most titles collide; even artist-only) |
| Cost of wrong pick | Annoying, clearly not asked | Trivially recoverable ("no, the other one" / "next") |
| → Right default | Occasional upfront disambiguation OK | **Optimistic best-match + cheap correction** |

Clarifying music would fire on nearly every request — death by a thousand "which
one did you mean?" Google/Alexa **deliberately don't clarify**: they play the
best match on a strong popularity/personalization prior and rely on post-hoc
correction. (That post-hoc "no, the other one / next" is a **domain re-query**, *not* the
deterministic state-restore of [`undo.md`](undo.md) — same "act now, correct cheaply" bet,
different mechanism; the two are kept distinct there.) HA's Search-and-Play agrees — it plays the best result with **no
disambiguation mechanism at all**, by design.

**The "global search without immediately playing" feature request is NOT about
per-play disambiguation** — it's a **browse/queue** workflow (search to build a
queue, browse a library, avoid auto-blasting audio). Nobody's asking for a
clarify-turn on every play.

---

## Design for our LLM agent

- **Inherit the fused tool free.** Via the Assist API we get
  `HassMediaSearchAndPlay`; the LLM populates `media_class` from phrasing ("play
  the *workout playlist*" → `playlist`) better than hassil templates. Basic
  play-by-voice works out of the box **if the user runs Music Assistant** — the
  one hard dependency to flag.
- **Default = fused, optimistic, one call.** `HassMediaSearchAndPlay` is a single
  tool call that plays the best result. Do **not** default to a
  search-then-disambiguate flow.
- **Search-read tool is a *narrow* addition, not the default.** A separate
  "search returns candidates" tool costs an **extra LLM round-trip** (search →
  pick → play = 2 calls vs 1). Reserve it for:
  - **browse / queue building** (the actual feature request),
  - **LLM-side smarter ranking than `results[0]`** without a user turn (prefer
    popular, honor implied `media_class`, catch an explicit preference like "the
    acoustic version"),
  - genuine clarification only when the user *signaled* they care **and** it's
    truly tied.
- **Custom-tool gaps (assistant-grade):** `what_is_playing` (read state),
  `favorite_current` / "like this" (call MA's favorite service), queue control.
  These are where we add value beyond stock.

---

## Roadmap reality

- **The "horizon" item already shipped** — native library search + the
  Search-and-Play intent (Voice Chapter 10, June 2025; MA native in 2024.12).
  Older "on the horizon" commentary predates it.
- **No public roadmap for the assistant-grade gaps** (what's-playing / like /
  queue / smarter disambiguation). Voice Chapter 10's only forward caveat is
  **language coverage**.
- **Inconsistency to be aware of:** MA's own voice doc still says "no built-in
  support" and recommends blueprints, out of step with core now shipping the
  intent — because the native intent's local sentence coverage and MA-specific
  queue behavior still favor blueprints for now.

---

## Sources

- [Voice Chapter 10 — HA blog (Search-and-Play intent)](https://www.home-assistant.io/blog/2025/06/25/voice-chapter-10/)
- [Music Assistant — Voice Control docs](https://www.music-assistant.io/integration/voice/)
- [Music Assistant's next big hit (2.4 voice) — MA blog](https://www.music-assistant.io/blog/2025/03/05/music-assistants-next-big-hit/)
- [Enhanced MA voice control: global search *without* immediately playing — HA Community](https://community.home-assistant.io/t/enhanced-music-assistant-voice-control-with-global-music-search-without-immediately-playing/912548)
- [`music-assistant/voice-support` blueprints (GitHub)](https://github.com/music-assistant/voice-support)
