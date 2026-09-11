package com.liferadio.sync.service

/**
 * Keeps the UI's collection state tied to an actual accepted fix rather than
 * merely to a foreground-service instance being alive.
 */
object LocationTrackingHealthPolicy {
    const val STALE_AFTER_MILLIS = 15 * 60 * 1000L

    fun isStale(enabled: Boolean, lastAcceptedAt: Long, now: Long): Boolean =
        enabled && (lastAcceptedAt <= 0L || now - lastAcceptedAt > STALE_AFTER_MILLIS)
}
