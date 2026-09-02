package com.liferadio.sync.data.model

import com.squareup.moshi.Moshi
import com.squareup.moshi.kotlin.reflect.KotlinJsonAdapterFactory
import org.junit.Assert.assertEquals
import org.junit.Test

class EventBackgroundMoshiTest {
    @Test fun `parses all real time item fields`() {
        val json = """{"business_date":"2026-08-16","generated_at":"2026-08-16T10:00:00Z","background_summary":{"wish":{"title":"心愿","items":[]},"device_and_apps":{"title":"设备","items":[]},"blacklist":{"title":"黑名单","items":[]},"location_and_activity":{"title":"位置","items":[]}},"ai_understanding":{"title":"AI 理解说明","items":[{"item_key":"one","text":"说明"}],"timezone":"Asia/Shanghai","real_time_valid_for_minutes":15},"real_time_items":[{"kind":"current_location","observed_at":"2026-08-16T10:00:00Z","is_stale":true,"include_in_ai":false,"device_id":"android-1","display_text":"上次位于家"}]}"""
        val value = Moshi.Builder().add(KotlinJsonAdapterFactory()).build().adapter(EventBackgroundResponse::class.java).fromJson(json)!!
        assertEquals(1, value.realTimeItems.size)
        assertEquals("current_location", value.realTimeItems.single().kind)
        assertEquals("android-1", value.realTimeItems.single().deviceId)
    }
}
