"""Identity resolution seam: the per-user scope key for a request.

`get_resolved_user()` is a uniform *accessor*: capability tools call it to get the
`user_id` to scope by, and never care whether the turn came from a live mic or a
deferred trigger firing. It is cheap, deterministic, and idempotent, and never runs the
expensive speaker-ID work.

*Populating* the resolved user is trigger-specific and happens once, upstream:

- immediate voice: a speaker-ID stage (Phase 4) at/after STT, where the audio is;
- immediate text: `context.user_id` (the logged-in user);
- deferred: the reminder/automation trigger replays the `user_id` it persisted at
  capture time (never re-resolved at fire).

The handoff is a side channel keyed by request identity, not a `Context` attribute:
`Context` is slotted (no arbitrary fields) and its `user_id` is HA's *auth* identity,
which must not be overwritten with a speaker (personalization-not-auth). See
PRODUCT_PLAN §5.1 and docs/speaker-identification.md.
"""

from homeassistant.core import Context, HomeAssistant

DEFAULT_USER_ID = "default"


async def get_resolved_user(
    hass: HomeAssistant,
    context: Context,
    device_id: str | None = None,
    satellite_id: str | None = None,
) -> str:
    """Return the `user_id` scope key for a request. Never returns `None`.

    Reads signals in priority order; runs no speaker-ID here:

    1. A resolution already established upstream for this request (from session state):
       the speaker-ID stage for voice, or a deferred trigger replaying the persisted
       owner. Not wired yet (Wave 0 has no populator).
    2. `context.user_id` if it maps to a real, active, non-system HA user. Trustworthy
       for text; for voice this is the pipeline owner, so a source-aware ordering will
       later rank device->owner above it.
    3. A configured device->owner mapping (`device_id` / `satellite_id`), not built yet.
    4. The `"default"` household bucket.
    """
    user_id = context.user_id
    if user_id:
        user = await hass.auth.async_get_user(user_id)
        if user is not None and user.is_active and not user.system_generated:
            return user_id
    return DEFAULT_USER_ID
