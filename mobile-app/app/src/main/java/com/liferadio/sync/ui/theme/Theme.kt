package com.liferadio.sync.ui.theme

import android.app.Activity
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Shapes
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.SideEffect
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.unit.dp
import androidx.core.view.WindowCompat

// Shared with the WebUI's Warm Sand Cyan design language.
val WarmSand = Color(0xFFF5F1EA)
val WarmCard = Color(0xFFFFFDF9)
val WarmCardHover = Color(0xFFFFF9F0)
val WarmText = Color(0xFF3E3A36)
val WarmTextSecondary = Color(0xFF9B9186)
val WarmBorder = Color(0xFFE8DED0)
val CyanAccent = Color(0xFF35D4AC)
val Success = CyanAccent
val Warning = Color(0xFFFBBF24)
val Danger = Color(0xFFDC2626)
val Info = Color(0xFF38BDF8)

private val LightColorScheme = lightColorScheme(
    primary = CyanAccent,
    onPrimary = Color.White,
    primaryContainer = Color(0xFFE4F9F3),
    onPrimaryContainer = Color(0xFF176B58),
    secondary = Warning,
    onSecondary = WarmText,
    secondaryContainer = Color(0xFFFFF4D6),
    onSecondaryContainer = Color(0xFF8A5B00),
    tertiary = Info,
    background = WarmSand,
    onBackground = WarmText,
    surface = WarmCard,
    onSurface = WarmText,
    surfaceVariant = WarmCardHover,
    onSurfaceVariant = WarmTextSecondary,
    outline = WarmBorder,
    error = Danger,
    onError = Color.White,
    errorContainer = Color(0xFFFEF2F2),
    onErrorContainer = Danger
)

private val DarkColorScheme = darkColorScheme(
    primary = CyanAccent,
    onPrimary = Color(0xFF073B30),
    primaryContainer = Color(0xFF155A4B),
    onPrimaryContainer = Color(0xFFB8F4E4),
    secondary = Warning,
    onSecondary = Color(0xFF493100),
    secondaryContainer = Color(0xFF5E4300),
    onSecondaryContainer = Color(0xFFFFE29A),
    tertiary = Info,
    background = Color(0xFF1F1C19),
    onBackground = Color(0xFFF4EDE4),
    surface = Color(0xFF292520),
    onSurface = Color(0xFFF4EDE4),
    surfaceVariant = Color(0xFF342E29),
    onSurfaceVariant = Color(0xFFC9BEB2),
    outline = Color(0xFF51483F),
    error = Color(0xFFFF8A80),
    onError = Color(0xFF5F0000)
)

private val LifeLinkShapes = Shapes(
    extraSmall = RoundedCornerShape(8.dp),
    small = RoundedCornerShape(10.dp),
    medium = RoundedCornerShape(18.dp),
    large = RoundedCornerShape(22.dp),
    extraLarge = RoundedCornerShape(28.dp)
)

@Composable
fun LifeRadioTheme(
    darkTheme: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit
) {
    val colorScheme = if (darkTheme) DarkColorScheme else LightColorScheme
    val view = LocalView.current
    if (!view.isInEditMode) {
        SideEffect {
            val window = (view.context as Activity).window
            window.statusBarColor = colorScheme.background.toArgb()
            window.navigationBarColor = colorScheme.surface.toArgb()
            WindowCompat.getInsetsController(window, view).apply {
                isAppearanceLightStatusBars = !darkTheme
                isAppearanceLightNavigationBars = !darkTheme
            }
        }
    }

    MaterialTheme(colorScheme = colorScheme, shapes = LifeLinkShapes, content = content)
}
