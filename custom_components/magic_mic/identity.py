"""Identity resolution seam: map a request to a stable per-user scope key.

`resolve_user()` does *retrieval only*: it is cheap, deterministic, and idempotent,
reading already-available signals and returning a `user_id`. It never runs the
expensive/stateful speaker-ID work; that is a separate upstream stage (Phase 4) that
writes its result into the request context, which this function then reads. See
PRODUCT_PLAN §5.1 and docs/speaker-identification.md.
"""

from homeassistant.core import Context, HomeAssistant

DEFAULT_USER_ID = "default"


async def resolve_user(
    hass: HomeAssistant,
    context: Context,
    device_id: str | None = None,
    satellite_id: str | None = None,
) -> str:
    """Return the `user_id` scope key for a request.

    Reads cheap signals in priority order; runs no speaker-ID here:

    1. The upstream voice-ID signal, once it exists (Phase 4), is read first from
       context. (Not present yet.)
    2. `context.user_id` if it maps to a real, active, non-system Home Assistant user.
    3. A configured device->owner mapping (`device_id` / `satellite_id`), not built yet.
    4. The `"default"` household bucket. Never returns `None`.
    """
    user_id = context.user_id
    if user_id:
        user = await hass.auth.async_get_user(user_id)
        if user is not None and user.is_active and not user.system_generated:
            return user_id
    return DEFAULT_USER_ID
