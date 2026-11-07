package org.cloudreve.android.ui.theme

import android.os.Build
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.dynamicDarkColorScheme
import androidx.compose.material3.dynamicLightColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext

private val LightColors = lightColorScheme(
    primary = Color(0xFF4A6FA5),
    secondary = Color(0xFF6B8BB8),
    surface = Color(0xFFFAFBFD),
)

private val DarkColors = darkColorScheme(
    primary = Color(0xFF9DB8DC),
    secondary = Color(0xFF8AA4CC),
    surface = Color(0xFF121417),
)

/**
 * Material You: follow the wallpaper palette like the system Files app on
 * Android 12+, fall back to the Cloudreve blues below that.
 */
@Composable
fun CloudreveTheme(content: @Composable () -> Unit) {
    val dark = isSystemInDarkTheme()
    val context = LocalContext.current
    val scheme = when {
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.S ->
            if (dark) dynamicDarkColorScheme(context) else dynamicLightColorScheme(context)
        dark -> DarkColors
        else -> LightColors
    }
    MaterialTheme(colorScheme = scheme, content = content)
}
