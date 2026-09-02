package com.liferadio.sync.data.model

/** Current state of the central synchronization loop. */
data class SyncStatus(
    val isSyncing: Boolean = false,
    val lastSyncTime: Long? = null,
    val pendingEvents: Int = 0,
    val errorMessage: String? = null,
    val lastSyncResult: String? = null
)
