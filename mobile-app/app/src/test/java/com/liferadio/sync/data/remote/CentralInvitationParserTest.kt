package com.liferadio.sync.data.remote

import java.time.Instant
import java.util.Base64
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class CentralInvitationParserTest {
    private val now = Instant.parse("2026-08-01T00:00:00Z")

    @Test fun parsesAndroidEnrollmentInvitationWithoutPersistingSecret() {
        val parsed = CentralInvitationParser.parse(code("2026-08-02T00:00:00Z"), now)
        assertEquals("https://central.example.test", parsed.centralBaseUrl)
        assertEquals("仅上传本机数据", parsed.permissionLabel)
    }

    @Test fun rejectsExpiredAndNonHttpsInvitations() {
        assertThrows(IllegalArgumentException::class.java) {
            CentralInvitationParser.parse(code("2026-07-31T00:00:00Z"), now)
        }
        assertThrows(IllegalArgumentException::class.java) {
            CentralInvitationParser.parse(code("2026-08-02T00:00:00Z", "http://central.example.test"), now)
        }
    }

    private fun code(expiresAt: String, baseUrl: String = "https://central.example.test"): String {
        val json = """{"v":1,"invitation_id":"22222222-2222-4222-8222-222222222222","central_base_url":"$baseUrl","invitation_token":"${"x".repeat(32)}","scope":"upload","expires_at":"$expiresAt"}"""
        return "LR1." + Base64.getUrlEncoder().withoutPadding().encodeToString(json.toByteArray())
    }
}
