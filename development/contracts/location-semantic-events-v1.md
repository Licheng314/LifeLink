# Location semantic events v1

Android v2.9 changes new location collection from phone-side segments to immutable observations. Android v3.0 keeps that event model and makes the Android fix time authoritative for ordering and deduplication. Existing `location.sample` and `location.stay` events remain readable for backward compatibility.

## New Android observations

- Every fresh location with accepted accuracy creates one `location.observation` event with a stable UUID derived from the Android fix time, rounded coordinates, and provider.
- Every observation is `ready_to_sync=true` and enters the normal local outbox. It is removed from that outbox only after the central service returns its ID in `confirmed_event_ids`.
- Observations are immutable. A receiver deduplicates only the same `event_id`; it must not merge or overwrite a different observation ID.
- Coordinates are rounded to five decimal places in the synchronized payload. The phone retains the unrounded values in its local `location_samples` table.
- `place` is resolved on-device when available. Geocoding failure never blocks persistence or synchronization.
- Android no longer applies the 150 metre rule when creating synchronization events. PC consumers may derive stays or route segments from the complete observation sequence.

## Observation timing and replay

- Event `occurred_at` / legacy `timestamp` uses `location_time`, the timestamp attached to the Android `Location`. `observed_at` remains the later time when Life Link received and persisted the fix.
- Re-delivery of the same fix within the same second and rounded coordinates reuses the same event ID and is ignored as a true retransmission instead of creating another observation.
- Android rejects fixes older than ten minutes and fixes more than two minutes in the future. This prevents a service restart from turning a many-hours-old cached location into a new current observation.
- The foreground service requests high-accuracy fixes every five minutes with intentional callback batching disabled. If Android still returns several fresh fixes in one callback, each distinct fix is persisted in fix-time order.

## Motion window

- Each normally delivered observation carries a `motion_window` covering the period since the previous accepted observation, or since the tracking service started for the first observation.
- `trigger_count` counts threshold crossings from below to above, not every accelerometer reading above the threshold.
- `sensor_sample_count`, `threshold_mps2`, and `peak_delta_mps2` make sensitivity experiments auditable.
- Accelerometer telemetry is diagnostic context only. It does not reject a location, change the five-minute request interval, or decide whether an observation is synchronized.
- `accelerometer_available=false` and `sensor_sample_count=0` must be treated as unavailable evidence, not as proof that the phone was stationary.
- If Android replays several fixes in one callback, only the newest fix can receive the complete callback motion window. Earlier replayed fixes use unavailable motion evidence rather than duplicating one accelerometer window across multiple observations.

## Legacy segments

- `location.sample` and `location.stay` keep their existing update-by-revision semantics for old clients and already stored data.
- An active legacy segment keeps one `event_id`. Higher revisions may move `observed_until` / `latest_observed_at` forward and increase duration. Finalization may promote `location.sample` to `location.stay` and sets `is_active=false`; it cannot be reactivated or revised afterward, and its starting coordinates cannot be rewritten.
- On upgrade, Android may finalize one previously active segment at its last actual observation time. It creates no new phone-side segments afterward.
- PC consumers should preserve raw observations as the factual source and treat any derived segment as a reproducible view.

Complete addresses are personal-mode data. They should travel only over an authenticated HTTPS path to the user's central service. A public release must omit them by default and add a visible precision control plus explicit consent.
