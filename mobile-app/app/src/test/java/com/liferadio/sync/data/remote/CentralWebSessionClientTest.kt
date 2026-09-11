package com.liferadio.sync.data.remote

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class CentralWebSessionClientTest {
    @Test
    fun acceptsOnlyHttpsBootstrapUrls() {
        assertTrue(
            CentralWebSessionClient.isValidWebUrl(
                "https://central.example.test/#lifelink_bootstrap=test-token"
            )
        )
        assertFalse(CentralWebSessionClient.isValidWebUrl("http://central.example.test/#lifelink_bootstrap=test-token"))
        assertFalse(CentralWebSessionClient.isValidWebUrl("https://central.example.test/"))
        assertFalse(CentralWebSessionClient.isValidWebUrl(null))
    }
}
