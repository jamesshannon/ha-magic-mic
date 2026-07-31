# Ambient Noise / White Noise (Feature Request)

> Distinct FR. Play white/pink/brown noise (and eventually nature sounds) through
> speakers, on endless repeat, by voice, locally, without depending on Music
> Assistant or cloud streams. See [`music-playback.md`](music-playback.md) (this
> is deliberately *not* part of the music path) and
> [`../PRODUCT_PLAN.md`](../PRODUCT_PLAN.md).

---

## TL;DR

- **HA has no first-class ambient/sleep-sounds feature** (Alexa/Google do).
  Nothing ambient/white-noise/sleep-sound exists in the tree.
- **This is its own capability, not a music-search special case** — it wants
  *streams/generated audio*, not library items, so it must not route through the
  music-search path.
- **Play it directly from HA, no MA:** `media_player.play_media` to any
  `PLAY_MEDIA` player, with the audio served from a component-exposed URL.
- **Endless without a 12 h file:** a small **looping stream view** (radio-style)
  is player-agnostic and doubles as the URL exposer; `repeat=one` is the
  fallback but is player-dependent.
- **Local-first / core-viable:** ship tiny loopable noise files (precedent:
  `acknowledge.mp3`) or synthesize noise. **Cloud stream URLs won't merge to
  core** (rot, trust, local-first). Nature sounds = user-provided / separate
  download.

---

## State in HA

- **No ambient/sleep-sound concept anywhere** in the codebase (Alexa's and
  Google's curated sleep-sound libraries with loop + sleep timer have no HA
  equivalent).
- Primitives exist:
  - `media_player.play_media` accepts an arbitrary URL/stream
    (`async_process_play_media_url`, `media_content_id`).
  - `REPEAT_SET` feature — `RepeatMode` off/one/all (`media_player/const.py:205`).
  - **`repeat` is not a voice intent** (media_player intents are only
    next/pause/prev/search-play/mute/volume) — so "loop this" isn't natively
    voice-accessible.

## Governance: cloud streams won't merge to core

A core feature that ships/depends on a **curated list of cloud stream URLs**
wouldn't be accepted: URLs rot, services shut down (support burden core doesn't
control), plus trust/privacy of third-party streams — and it cuts against
local-first.

Important distinction: **HA happily *plays* cloud streams** (TuneIn, radio) — the
objection isn't streaming, it's **core taking responsibility for specific
third-party content**. User-*provided* streams = fine; core-*bundled* URLs =
pushback. So the mechanism (play + loop a stream) is fine; curated cloud
*content* is the problem.

→ Cloud streams are acceptable for our **custom component** (fast, includes nature
sounds), but the **core-candidate** backend must be local.

---

## The local-first design (core-viable)

**White noise is the ideal case because it's local by nature:**

- **Synthesize** it — white/pink/brown noise are filtered random signals (white =
  random samples; pink = 1/f; brown = integrated white). Endless by construction,
  no file, no cloud.
- **Or ship tiny loopable files** — simpler, and there's direct precedent:
  `assist_pipeline` bundles `acknowledge.mp3`; `assist_satellite`/`esphome`
  register static paths to serve their own audio. So shipping small audio in a
  voice component is an established pattern.

Either dissolves all three concerns at once: no 12 h file, no repeat-logic worry,
no cloud.

**Nature sounds (rain/ocean/fan/campfire) are the harder case** — not
synthesizable, need recorded audio. Keep them **user-provided or a separate
optional download**, not bundled in core (binary size / licensing). The tool
interface is identical whether the source is synthesized, bundled, user-provided,
or a cloud stream — so start with bundled noise and grow the catalog without
touching the contract.

---

## Playing it directly from HA (no MA)

`media_player.play_media` works on any entity with the `PLAY_MEDIA` feature
(Chromecast, Sonos, DLNA, ESPHome players, VLC…). MA is just one such player, not
required.

The player fetches the audio over HTTP, so HA must **expose it at a URL**:

1. **Component static path / custom view** — register bundled files
   (`async_register_static_paths` + `StaticPathConfig`, `http/__init__.py:65`).
   Cleanest for a fixed set of shipped files; what `assist_satellite` does.
2. **media_source** — resolves `media_source://…` to a signed URL; better for a
   *browseable* library (e.g. user-provided nature sounds).

Then `async_process_play_media_url` (`media_player/browse_media.py:34`) produces
the **absolute, signed URL** the player fetches (`async_sign_path` + `get_url`).

**Gotcha:** the URL must be reachable **by the player device** on the LAN, so
`get_url` must resolve to an address the speaker can hit — the classic "cast
device can't reach HA" footgun.

### Endless loop — use a looping stream view, not `repeat`

- **`repeat=one`** — set the player to loop. **Player-dependent** (many simple
  ESPHome/cast players don't implement `REPEAT`), and not voice-exposed. Not
  universal.
- **Looping stream view (recommended)** — a small `HomeAssistantView` (or aiohttp
  response) that reads the gapless-loop file and yields it on repeat forever. The
  player connects like internet radio and plays until stopped. **Player-agnostic,
  needs no `REPEAT` support, and doubles as the URL exposer** — one piece that
  serves *and* loops.

So: **ship small gapless-loop noise files + serve via a looping stream view +
`play_media` to any speaker.** No MA, endless, no 12 h file, fully local, works on
any player.

---

## Design for our agent

- **`play_ambient(sound, target, duration?)`** tool — distinct from music.
  Curated/deterministic set of sounds (deterministic-in-the-tool principle), each
  mapped to a bundled loop file / synth generator / (custom-component-only) cloud
  stream.
- **Not routed through music search** — it's streams/generated audio, not library
  items. Keeping it separate avoids polluting the music library *and* the
  music-search logic. This is why it's its own FR/doc.
- **Pair with a sleep timer** ("play rain for 30 minutes") — reuse the
  timer/reminder subsystem: start playback + schedule a stop. This is the piece
  that makes it feel like a real assistant feature (Alexa/Google always have it).
- **Stop/pause already work** via existing media_player intents (it's a normal
  playback session); ducks for announcements like any media.
- **Catalog path:** bundled white/pink/brown noise now → nature sounds
  user-provided or separate download later. Tool contract stable across all
  backends.

### Custom-component-now vs core-candidate (factoring)
- **v1 (our component):** may use cloud streams (fast, includes nature sounds).
- **Core candidate:** local synthesis or small bundled noise files; nature sounds
  user-provided.
- **Keep the `play_ambient` interface stable**; swap only the backend. Same voice
  UX, same sleep-timer pairing, different source.

---

## Build-time scoping gate

Before shipping ambient playback, settle and test:

- **Player compatibility:** codec/container support, authenticated HA media URLs, range
  requests, buffering, and loop behavior across representative players.
- **Gapless reality:** measure audible loop seams and long-run stability; do not call a
  repeated short file “gapless” based only on API behavior.
- **Session ownership:** identify the playback session the assistant started so duration
  expiry or “stop the rain” does not stop unrelated music that replaced it.
- **Restart/expiry:** define what happens to a scheduled stop across HA restart and how stale
  sleep stops are discarded.
- **Announcement interaction:** test duck/resume and `assist_satellite` announcements while
  the loop is active, including players that restart the stream from the beginning.
- **Asset policy:** document licenses, bundle/download size, user-provided file validation,
  and the no-cloud guarantee for every sound advertised as local.

These are implementation-time acceptance gates for the ambient-noise slice. Any real-home
compatibility/use telemetry follows the privacy and aggregation rules in
[`telemetry.md`](telemetry.md).

---

## Key references

- `media_player/const.py:205` — `REPEAT_SET` / `RepeatMode`
- `media_player/browse_media.py:34` — `async_process_play_media_url` (signed URL)
- `http/__init__.py:65` — `StaticPathConfig` / `async_register_static_paths`
- Precedent: `assist_pipeline/acknowledge.mp3`; `assist_satellite` static paths
- No ambient/white-noise concept exists in core (verified by search)
