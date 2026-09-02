package com.liferadio.sync

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import android.os.Build
import com.liferadio.sync.service.SyncService

class LifeRadioApp : Application() {

    companion object {
        const val CHANNEL_ID = "life_radio_sync"
        const val IMPORTANT_EVENTS_CHANNEL_ID = "life_radio_important_events"
    }

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        startSyncService()
    }

    private fun startSyncService() {
        SyncService.start(this)
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "Life Link",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "Data synchronization service"
            }
            val notificationManager = getSystemService(NotificationManager::class.java)
            notificationManager.createNotificationChannel(channel)
            notificationManager.createNotificationChannel(
                NotificationChannel(
                    IMPORTANT_EVENTS_CHANNEL_ID,
                    "事件提醒",
                    NotificationManager.IMPORTANCE_HIGH
                ).apply {
                    description = "Life Link 检测到中央的重要或普通事件时提醒"
                }
            )
        }
    }
}
