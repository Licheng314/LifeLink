package com.liferadio.sync.data.local

import android.content.Context
import android.location.Address
import android.location.Geocoder
import android.location.Location
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.util.Locale

data class ResolvedPlace(
    val country: String? = null,
    val adminArea: String? = null,
    val city: String? = null,
    val district: String? = null,
    val roadOrPoi: String? = null,
    val displayLabel: String? = null,
    val fullAddress: String? = null,
    val precision: String = "coordinates",
    val resolvedAt: Long = System.currentTimeMillis()
)

class LocationPlaceResolver(context: Context) {

    private val applicationContext = context.applicationContext

    suspend fun resolve(location: Location, resolvedAt: Long): ResolvedPlace? = withContext(Dispatchers.IO) {
        if (!Geocoder.isPresent()) return@withContext null

        val address = runCatching {
            @Suppress("DEPRECATION")
            Geocoder(applicationContext, Locale.getDefault())
                .getFromLocation(location.latitude, location.longitude, 1)
                ?.firstOrNull()
        }.getOrNull() ?: return@withContext null

        address.toResolvedPlace(resolvedAt)
    }

    private fun Address.toResolvedPlace(resolvedAt: Long): ResolvedPlace {
        val cityValue = locality ?: subAdminArea
        val districtValue = subLocality?.takeUnless { it == cityValue }
        val roadValue = thoroughfare ?: featureName
        val fullAddressValue = getAddressLine(0)?.takeIf { it.isNotBlank() }
        val displayLabelValue = listOfNotNull(cityValue, districtValue, roadValue)
            .distinct()
            .joinToString(" · ")
            .ifBlank { fullAddressValue }
        val precisionValue = when {
            fullAddressValue != null -> "address"
            districtValue != null -> "district"
            cityValue != null -> "city"
            else -> "coordinates"
        }

        return ResolvedPlace(
            country = countryName,
            adminArea = adminArea,
            city = cityValue,
            district = districtValue,
            roadOrPoi = roadValue,
            displayLabel = displayLabelValue,
            fullAddress = fullAddressValue,
            precision = precisionValue,
            resolvedAt = resolvedAt
        )
    }
}
