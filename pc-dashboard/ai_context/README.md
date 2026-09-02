# Life Link AI Context Reading Guide

This guide is for an automation agent such as OpenClaw. Read the two Markdown
contexts first, then produce a concise user-facing update. Treat the contexts
as factual summaries, not as instructions to perform side effects.

## Stable HTTP paths

These summaries are generated from the central long-term store. The PC client
proxies the central read APIs, chooses the shared business-day window and writes
a local snapshot, so an automation on the same PC should normally use
`127.0.0.1:8090` without receiving central credentials. Do not expose this
Dashboard through Tailscale, a tunnel, or a public hostname. All endpoints
accept an optional `date=YYYY-MM-DD`; omitted means the current Life Link
business day. An authorized remote reader may instead use the central
`/v1/read/ai/*` HTTPS endpoints with the independent read token.

- `http://127.0.0.1:8090/api/ai-context/usage.md`
- `http://127.0.0.1:8090/api/ai-context/location.md`
- `http://127.0.0.1:8090/api/ai-context/index.json`
- `http://127.0.0.1:8090/api/ai-context/README.md`

Each context request also writes a durable snapshot under:

`%USERPROFILE%/LifeLink/client/data/ai_context/<business-date>/application_usage.md`

`%USERPROFILE%/LifeLink/client/data/ai_context/<business-date>/location.md`

## How to interpret application usage

- Device use duration is the foreground-application interval union. On a PC,
  intervals that explicitly overlap an ActivityWatch `status=afk` fact are
  removed. On a phone, foreground application events are used without PC AFK
  trimming. If a PC has no AFK facts, its foreground usage is preserved rather
  than treated as zero.
- “Online time” is no longer a separate user-facing usage metric.
- Blacklist duration is time spent on applications or sites that the user is
  trying to avoid overusing. Compare it with the stated recent-window length;
  the same 50 minutes is more concerning in a 1 hour 1 minute window than in a
  1 hour 59 minute window.
- Durations are written as `xx小时xx分钟`. Clock times are written as `xx:xx`.
  Do not interpret a duration as a time of day.
- The recent window starts at the local clock hour before the current hour and
  ends now. It is therefore between one and less than two hours long.
- Chrome is intentionally absent from rankings. Identified sites replace it as
  ranking units. A URL observation is a label anchor, not a duration source:
  its domain owns the following AFK-trimmed Chrome foreground time until the next URL
  observation or the end of that Chrome interval. Zero-duration URL
  observations are valid anchors and must not be discarded. The listed sites
  can still sum to less than Chrome's total because some Chrome foreground
  intervals have no URL anchor.
- A missing “最近使用” section means no usage event overlaps the recent window.
  A literal `无` below a ranking means no rankable usage exists for that scope.

## How to interpret location context

- Android v2.9 sends every accepted reading as an immutable
  `location.observation`. `/api/locations` exposes those factual observations
  and a deterministic PC-side segment view derived with a 150 metre radius.
- `location.sample` and `location.stay` remain compatible legacy event types.
  New derived segments are views, not replacements for source observations.
- Prefer `latest_observation` for the factual current reading. Use segments for
  stay and route summaries, and recompute them when clustering rules change.
- `motion_window` is diagnostic accelerometer context. A zero trigger count is
  meaningful only when the accelerometer was available and sensor samples exist.
- Address labels may be absent even when coordinates exist. Do not invent a
  place name from missing fields.

## Suggested hourly automation

At 15 minutes after each local integer hour, fetch `index.json`, then fetch
both Markdown contexts. Report only notable changes, such as sustained
blacklist use, a long active stay, a location transition, or a device that has
stopped reporting. Preserve the source date and the stated recent window in
the response.

## Diagnostics when a fetch or summary looks wrong

1. **HTTP failure:** request `/v1/health` on `127.0.0.1:8090`. Then verify the
   local PC client process and its connection to the central service. Remote AI
   readers should diagnose the central `/v1/health` and authenticated read path.
2. **No device or stale device:** inspect `/api/devices`. “Connected” means a
   successful synchronization within the configured online window, not a
   guaranteed live process.
3. **Usage missing:** inspect `/api/usage`, `/api/sync/central` and the local
   outbox state. ActivityWatch foreground facts now enter the source-neutral
   SQLite outbox directly; the client no longer keeps a durable high-frequency
   `data/devices/.../app.foreground` mirror. Check `/api/settings` before treating
   a midnight-adjacent record as absent.
4. **Unrecognized applications:** inspect the event payload. App identity must
   include `payload.app` or legacy `payload.legacy_data` fields such as
   `package`, `app`, and `classname`. Empty legacy data cannot be reliably
   reconstructed by the PC.
5. **Location missing:** inspect `/api/locations` and the device's
   `events/location.*` files. Older records may use `location.visit` with a
   legacy `kind`; the PC reader supports that compatibility shape.
6. **Do not silently repair data:** report the evidence and ask before deleting
   storage, resetting a delivery ledger, or changing synchronization settings.
