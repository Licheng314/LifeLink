package com.liferadio.sync.data.local

import android.content.Context
import androidx.room.*
import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase
import kotlinx.coroutines.flow.Flow

/**
 * 本地数据库 - 存储收集的数据
 */

@Entity(tableName = "data_events")
data class DataEventEntity(
    @PrimaryKey val id: String,
    @ColumnInfo(name = "source") val source: String,
    @ColumnInfo(name = "source_type") val sourceType: String,
    @ColumnInfo(name = "data_type") val dataType: String,
    @ColumnInfo(name = "timestamp") val timestamp: String,
    @ColumnInfo(name = "duration") val duration: Int,
    @ColumnInfo(name = "data_json") val dataJson: String,
    @ColumnInfo(name = "synced") val synced: Boolean = false,
    @ColumnInfo(name = "ready_to_sync") val readyToSync: Boolean = true,
    @ColumnInfo(name = "revision") val revision: Long = 0L,
    @ColumnInfo(name = "created_at") val createdAt: Long = System.currentTimeMillis()
)

@Entity(
    tableName = "location_samples",
    indices = [Index(value = ["observed_at"])]
)
data class LocationSampleEntity(
    @PrimaryKey val id: String,
    @ColumnInfo(name = "observed_at") val observedAt: Long,
    @ColumnInfo(name = "latitude") val latitude: Double,
    @ColumnInfo(name = "longitude") val longitude: Double,
    @ColumnInfo(name = "accuracy_m") val accuracyMeters: Float,
    @ColumnInfo(name = "provider") val provider: String,
    @ColumnInfo(name = "motion_window_started_at") val motionWindowStartedAt: Long = 0L,
    @ColumnInfo(name = "accelerometer_available") val accelerometerAvailable: Boolean = false,
    @ColumnInfo(name = "accelerometer_sample_count") val accelerometerSampleCount: Int = 0,
    @ColumnInfo(name = "motion_trigger_count") val motionTriggerCount: Int = 0,
    @ColumnInfo(name = "motion_threshold_mps2") val motionThresholdMetersPerSecondSquared: Float = 0.7f,
    @ColumnInfo(name = "peak_motion_delta_mps2") val peakMotionDeltaMetersPerSecondSquared: Float = 0f
)

@Entity(tableName = "sync_log")
data class SyncLogEntity(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    @ColumnInfo(name = "target_node") val targetNode: String,
    @ColumnInfo(name = "event_count") val eventCount: Int,
    @ColumnInfo(name = "success") val success: Boolean,
    @ColumnInfo(name = "timestamp") val timestamp: Long = System.currentTimeMillis()
)

@Entity(
    tableName = "event_deliveries",
    primaryKeys = ["event_id", "target_id"],
    indices = [Index(value = ["target_id"])]
)
data class EventDeliveryEntity(
    @ColumnInfo(name = "event_id") val eventId: String,
    @ColumnInfo(name = "target_id") val targetId: String,
    @ColumnInfo(name = "delivered_revision") val deliveredRevision: Long = 0L,
    @ColumnInfo(name = "delivered_at") val deliveredAt: Long = System.currentTimeMillis()
)

/** Immutable local record of a physical TYPE_STEP_COUNTER observation. */
@Entity(tableName = "step_observations", indices = [Index(value = ["observed_at"]), Index(value = ["counter_session_id"])])
data class StepObservationEntity(
    @PrimaryKey @ColumnInfo(name = "event_id") val eventId: String,
    @ColumnInfo(name = "observed_at") val observedAt: Long,
    @ColumnInfo(name = "counter_value") val counterValue: Long,
    @ColumnInfo(name = "counter_session_id") val counterSessionId: String
)

@Dao
interface DataEventDao {
    @Query("SELECT EXISTS(SELECT 1 FROM data_events WHERE id = :eventId)")
    suspend fun containsEvent(eventId: String): Boolean

    @Query("SELECT * FROM data_events WHERE synced = 0 AND ready_to_sync = 1 ORDER BY created_at ASC")
    suspend fun getUnsyncedEvents(): List<DataEventEntity>

    @Query("""
        SELECT * FROM data_events
        WHERE ready_to_sync = 1
          AND NOT EXISTS (
            SELECT 1 FROM event_deliveries
            WHERE event_deliveries.event_id = data_events.id
              AND event_deliveries.target_id = :targetId
              AND event_deliveries.delivered_revision >= data_events.revision
          )
        ORDER BY created_at ASC
    """)
    suspend fun getEventsPendingForTarget(targetId: String): List<DataEventEntity>

    @Query("""
        SELECT * FROM data_events
        WHERE ready_to_sync = 1
          AND NOT EXISTS (
            SELECT 1 FROM event_deliveries
            WHERE event_deliveries.event_id = data_events.id
              AND event_deliveries.target_id = :targetId
              AND event_deliveries.delivered_revision >= data_events.revision
          )
        ORDER BY created_at ASC
        LIMIT :limit
    """)
    suspend fun getEventsPendingForTargetLimited(
        targetId: String,
        limit: Int
    ): List<DataEventEntity>

    @Query("SELECT * FROM data_events WHERE data_type = :dataType AND timestamp >= :since ORDER BY timestamp ASC")
    suspend fun getEventsSince(dataType: String, since: String): List<DataEventEntity>

    @Query("SELECT * FROM data_events WHERE data_type = 'app_usage' AND timestamp >= :start AND timestamp < :end ORDER BY timestamp ASC")
    suspend fun getTodayAppUsageEvents(start: String, end: String): List<DataEventEntity>

    @Query("SELECT * FROM data_events WHERE data_type = 'location' AND timestamp >= :start AND timestamp < :end ORDER BY timestamp ASC")
    suspend fun getTodayLocationEvents(start: String, end: String): List<DataEventEntity>

    @Query("SELECT COUNT(*) FROM data_events WHERE synced = 0 AND ready_to_sync = 1")
    fun getPendingCount(): Flow<Int>

    @Query("SELECT COUNT(*) FROM data_events WHERE synced = 0 AND ready_to_sync = 1")
    suspend fun getPendingCountBlocking(): Int

    @Query("""
        SELECT COUNT(*) FROM data_events
        WHERE ready_to_sync = 1
          AND NOT EXISTS (
            SELECT 1 FROM event_deliveries
            WHERE event_deliveries.event_id = data_events.id
              AND event_deliveries.target_id = :targetId
              AND event_deliveries.delivered_revision >= data_events.revision
          )
    """)
    fun getCentralPendingCount(targetId: String): Flow<Int>

    @Query("""
        SELECT COUNT(*) FROM data_events
        WHERE ready_to_sync = 1
          AND NOT EXISTS (
            SELECT 1 FROM event_deliveries
            WHERE event_deliveries.event_id = data_events.id
              AND event_deliveries.target_id = :targetId
              AND event_deliveries.delivered_revision >= data_events.revision
          )
    """)
    suspend fun getCentralPendingCountBlocking(targetId: String): Int

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insert(event: DataEventEntity)

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertAll(events: List<DataEventEntity>)

    @Query("UPDATE data_events SET synced = 1 WHERE id IN (:ids) AND ready_to_sync = 1")
    suspend fun markSynced(ids: List<String>)

    @Query("UPDATE data_events SET synced = 1 WHERE id = :id AND revision = :revision AND ready_to_sync = 1")
    suspend fun markSyncedRevision(id: String, revision: Long)

    @Query("DELETE FROM data_events WHERE synced = 1 AND created_at < :before")
    suspend fun deleteSyncedOlderThan(before: Long)

    @Query("SELECT COUNT(*) FROM data_events WHERE created_at >= :todayStart")
    fun getTodayCount(todayStart: Long): Flow<Int>

    @Query("SELECT COUNT(*) FROM data_events WHERE created_at >= :todayStart AND synced = 1")
    fun getTodaySyncedCount(todayStart: Long): Flow<Int>
}

@Dao
interface LocationSampleDao {
    @Insert(onConflict = OnConflictStrategy.IGNORE)
    suspend fun insert(sample: LocationSampleEntity)

    @Query("SELECT * FROM location_samples WHERE observed_at >= :start AND observed_at < :end ORDER BY observed_at ASC")
    suspend fun getSamplesBetween(start: Long, end: Long): List<LocationSampleEntity>

    @Query("SELECT COUNT(*) FROM location_samples WHERE observed_at >= :start AND observed_at < :end")
    suspend fun getCountBetween(start: Long, end: Long): Int
}

@Dao
interface EventDeliveryDao {
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun markDelivered(deliveries: List<EventDeliveryEntity>)
}

@Dao
interface SyncLogDao {
    @Query("SELECT * FROM sync_log ORDER BY timestamp DESC LIMIT 50")
    fun getRecentLogs(): Flow<List<SyncLogEntity>>

    @Insert
    suspend fun insert(log: SyncLogEntity)
}

@Dao
interface StepObservationDao {
    @Insert(onConflict = OnConflictStrategy.IGNORE)
    suspend fun insert(observation: StepObservationEntity)

    @Query("SELECT * FROM step_observations WHERE observed_at >= :start AND observed_at < :end ORDER BY observed_at ASC")
    suspend fun getBetween(start: Long, end: Long): List<StepObservationEntity>
}

@Database(
    entities = [DataEventEntity::class, LocationSampleEntity::class, SyncLogEntity::class, EventDeliveryEntity::class, StepObservationEntity::class],
    version = 6,
    exportSchema = false
)
abstract class AppDatabase : RoomDatabase() {
    abstract fun dataEventDao(): DataEventDao
    abstract fun locationSampleDao(): LocationSampleDao
    abstract fun eventDeliveryDao(): EventDeliveryDao
    abstract fun syncLogDao(): SyncLogDao
    abstract fun stepObservationDao(): StepObservationDao

    companion object {
        @Volatile
        private var INSTANCE: AppDatabase? = null

        fun getInstance(context: Context): AppDatabase {
            return INSTANCE ?: synchronized(this) {
                val instance = Room.databaseBuilder(
                    context.applicationContext,
                    AppDatabase::class.java,
                    "liferadio.db"
                ).addMigrations(MIGRATION_1_2, MIGRATION_2_3, MIGRATION_3_4, MIGRATION_4_5, MIGRATION_5_6).build()
                INSTANCE = instance
                instance
            }
        }

        private val MIGRATION_1_2 = object : Migration(1, 2) {
            override fun migrate(database: SupportSQLiteDatabase) {
                database.execSQL(
                    "ALTER TABLE data_events ADD COLUMN ready_to_sync INTEGER NOT NULL DEFAULT 1"
                )
            }
        }

        private val MIGRATION_2_3 = object : Migration(2, 3) {
            override fun migrate(database: SupportSQLiteDatabase) {
                database.execSQL(
                    """
                    CREATE TABLE IF NOT EXISTS event_deliveries (
                        event_id TEXT NOT NULL,
                        target_id TEXT NOT NULL,
                        delivered_at INTEGER NOT NULL,
                        PRIMARY KEY(event_id, target_id)
                    )
                    """.trimIndent()
                )
                database.execSQL(
                    "CREATE INDEX IF NOT EXISTS index_event_deliveries_target_id ON event_deliveries(target_id)"
                )
            }
        }

        private val MIGRATION_3_4 = object : Migration(3, 4) {
            override fun migrate(database: SupportSQLiteDatabase) {
                database.execSQL(
                    "ALTER TABLE data_events ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"
                )
                database.execSQL(
                    "ALTER TABLE event_deliveries ADD COLUMN delivered_revision INTEGER NOT NULL DEFAULT 0"
                )
                database.execSQL(
                    """
                    CREATE TABLE IF NOT EXISTS location_samples (
                        id TEXT NOT NULL,
                        observed_at INTEGER NOT NULL,
                        latitude REAL NOT NULL,
                        longitude REAL NOT NULL,
                        accuracy_m REAL NOT NULL,
                        provider TEXT NOT NULL,
                        PRIMARY KEY(id)
                    )
                    """.trimIndent()
                )
                database.execSQL(
                    "CREATE INDEX IF NOT EXISTS index_location_samples_observed_at ON location_samples(observed_at)"
                )
            }
        }

        private val MIGRATION_4_5 = object : Migration(4, 5) {
            override fun migrate(database: SupportSQLiteDatabase) {
                database.execSQL(
                    "ALTER TABLE location_samples ADD COLUMN motion_window_started_at INTEGER NOT NULL DEFAULT 0"
                )
                database.execSQL(
                    "ALTER TABLE location_samples ADD COLUMN accelerometer_available INTEGER NOT NULL DEFAULT 0"
                )
                database.execSQL(
                    "ALTER TABLE location_samples ADD COLUMN accelerometer_sample_count INTEGER NOT NULL DEFAULT 0"
                )
                database.execSQL(
                    "ALTER TABLE location_samples ADD COLUMN motion_trigger_count INTEGER NOT NULL DEFAULT 0"
                )
                database.execSQL(
                    "ALTER TABLE location_samples ADD COLUMN motion_threshold_mps2 REAL NOT NULL DEFAULT 0.7"
                )
                database.execSQL(
                    "ALTER TABLE location_samples ADD COLUMN peak_motion_delta_mps2 REAL NOT NULL DEFAULT 0"
                )
            }
        }

        private val MIGRATION_5_6 = object : Migration(5, 6) {
            override fun migrate(database: SupportSQLiteDatabase) {
                database.execSQL("""
                    CREATE TABLE IF NOT EXISTS step_observations (
                        event_id TEXT NOT NULL,
                        observed_at INTEGER NOT NULL,
                        counter_value INTEGER NOT NULL,
                        counter_session_id TEXT NOT NULL,
                        PRIMARY KEY(event_id)
                    )
                """.trimIndent())
                database.execSQL("CREATE INDEX IF NOT EXISTS index_step_observations_observed_at ON step_observations(observed_at)")
                database.execSQL("CREATE INDEX IF NOT EXISTS index_step_observations_counter_session_id ON step_observations(counter_session_id)")
            }
        }
    }
}
