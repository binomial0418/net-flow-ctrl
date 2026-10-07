package tw.netflow.netflow_tv

import android.content.Context
import android.content.Intent
import android.provider.Settings
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel

/** The status screen (Flutter) talks to the native side over one channel. */
class MainActivity : FlutterActivity() {
    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "netflow_tv").setMethodCallHandler { call, result ->
            when (call.method) {
                "status" -> result.success(status())
                "openAccessSettings" -> {
                    startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))
                    result.success(null)
                }
                "reportNow" -> {
                    NowPlayingService.instance?.report()
                    result.success(NowPlayingService.instance != null)
                }
                "setServer" -> {
                    val url = (call.arguments as? String)?.trim().orEmpty()
                    getSharedPreferences(NowPlayingService.PREFS, Context.MODE_PRIVATE).edit()
                        .putString("server", url.ifEmpty { NowPlayingService.DEFAULT_SERVER }).apply()
                    result.success(null)
                }
                else -> result.notImplemented()
            }
        }
    }

    private fun status(): Map<String, Any?> {
        val prefs = getSharedPreferences(NowPlayingService.PREFS, Context.MODE_PRIVATE)
        val enabled = Settings.Secure.getString(contentResolver, "enabled_notification_listeners").orEmpty()
        return mapOf(
            "granted" to enabled.split(':').any { it.startsWith("$packageName/") },
            "connected" to (NowPlayingService.instance != null),
            "server" to NowPlayingService.serverUrl(this),
            "lastAt" to prefs.getLong("last_at", 0L),
            "lastResult" to prefs.getString("last_result", null),
            "lastBody" to prefs.getString("last_body", null),
        )
    }
}
