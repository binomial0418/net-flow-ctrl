package tw.netflow.netflow_tv

import android.content.ComponentName
import android.content.Context
import android.media.MediaMetadata
import android.media.session.MediaController
import android.media.session.MediaSession
import android.media.session.MediaSessionManager
import android.media.session.PlaybackState
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.service.notification.NotificationListenerService
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors

/**
 * Reports what the TV is playing to the net-flow-ctrl router.
 *
 * It is a notification listener only because that is what Android requires
 * before an app may read other apps' media sessions; it ignores notifications.
 * Once the user (or `adb shell cmd notification allow_listener`) grants it
 * notification access, the system keeps it bound -- no activity has to run.
 *
 * Every media session's metadata (title, artist -- the channel, for YouTube)
 * and playback state is posted as JSON to <server>/api/nowplaying whenever it
 * changes (debounced) and once a minute as a heartbeat.
 */
class NowPlayingService : NotificationListenerService() {
    private val main = Handler(Looper.getMainLooper())
    private val net = Executors.newSingleThreadExecutor()
    private var msm: MediaSessionManager? = null
    private val tracked = mutableMapOf<MediaSession.Token, Pair<MediaController, MediaController.Callback>>()

    private val sessionsChanged = MediaSessionManager.OnActiveSessionsChangedListener { list ->
        track(list ?: emptyList())
    }
    private val reportNow = Runnable { report() }
    private val heartbeat = object : Runnable {
        override fun run() {
            report()
            main.postDelayed(this, HEARTBEAT_MS)
        }
    }

    override fun onListenerConnected() {
        instance = this
        val me = ComponentName(this, NowPlayingService::class.java)
        val m = getSystemService(MediaSessionManager::class.java)
        msm = m
        try {
            m.addOnActiveSessionsChangedListener(sessionsChanged, me, main)
            track(m.getActiveSessions(me))
        } catch (e: SecurityException) {
            Log.w(TAG, "no access to media sessions", e)
        }
        main.removeCallbacks(heartbeat)
        main.post(heartbeat)
    }

    override fun onListenerDisconnected() {
        instance = null
        main.removeCallbacks(heartbeat)
        main.removeCallbacks(reportNow)
        msm?.removeOnActiveSessionsChangedListener(sessionsChanged)
        untrackAll()
        // Ask to be bound again (after an app update, a crash of the listener...).
        requestRebind(ComponentName(this, NowPlayingService::class.java))
    }

    private fun track(list: List<MediaController>) {
        val now = list.associateBy { it.sessionToken }
        for (token in tracked.keys - now.keys) {
            tracked.remove(token)?.let { (c, cb) -> c.unregisterCallback(cb) }
        }
        for ((token, c) in now) {
            if (token in tracked) continue
            val cb = object : MediaController.Callback() {
                override fun onMetadataChanged(metadata: MediaMetadata?) = schedule()
                override fun onPlaybackStateChanged(state: PlaybackState?) = schedule()
                override fun onSessionDestroyed() = schedule()
            }
            c.registerCallback(cb, main)
            tracked[token] = c to cb
        }
        schedule()
    }

    private fun untrackAll() {
        for ((c, cb) in tracked.values) c.unregisterCallback(cb)
        tracked.clear()
    }

    /** Coalesce a burst of changes (a new video changes several fields) into one report. */
    private fun schedule() {
        main.removeCallbacks(reportNow)
        main.postDelayed(reportNow, DEBOUNCE_MS)
    }

    fun report() {
        val sessions = JSONArray()
        for ((c, _) in tracked.values) sessions.put(describe(c))
        val body = JSONObject()
            .put("version", 1)
            .put("app", packageName)
            .put("sessions", sessions)
        val server = serverUrl(this)
        net.execute { post("$server/api/nowplaying", body) }
    }

    private fun describe(c: MediaController): JSONObject {
        val md = c.metadata
        val st = c.playbackState
        val o = JSONObject().put("package", c.packageName)
        if (md != null) {
            o.put("title", md.getString(MediaMetadata.METADATA_KEY_TITLE)
                ?: md.getString(MediaMetadata.METADATA_KEY_DISPLAY_TITLE))
            o.put("artist", md.getString(MediaMetadata.METADATA_KEY_ARTIST)
                ?: md.getString(MediaMetadata.METADATA_KEY_DISPLAY_SUBTITLE)
                ?: md.getString(MediaMetadata.METADATA_KEY_ALBUM_ARTIST))
            o.put("album", md.getString(MediaMetadata.METADATA_KEY_ALBUM))
            o.put("mediaId", md.getString(MediaMetadata.METADATA_KEY_MEDIA_ID))
            val dur = md.getLong(MediaMetadata.METADATA_KEY_DURATION)
            if (dur > 0) o.put("durationMs", dur)
        }
        if (st != null) {
            o.put("state", stateName(st.state))
            // The reported position is as of lastPositionUpdateTime; project it to now.
            var pos = st.position
            if (st.state == PlaybackState.STATE_PLAYING) {
                pos += ((SystemClock.elapsedRealtime() - st.lastPositionUpdateTime) * st.playbackSpeed).toLong()
            }
            if (pos >= 0) o.put("positionMs", pos)
        }
        return o
    }

    private fun post(url: String, body: JSONObject) {
        val prefs = getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        try {
            val conn = URL(url).openConnection() as HttpURLConnection
            conn.connectTimeout = 3000
            conn.readTimeout = 3000
            conn.requestMethod = "POST"
            conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/json; charset=utf-8")
            conn.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
            val code = conn.responseCode
            conn.disconnect()
            prefs.edit()
                .putLong("last_at", System.currentTimeMillis())
                .putString("last_result", if (code == 200) "OK" else "HTTP $code")
                .putString("last_body", body.toString())
                .apply()
        } catch (e: Exception) {
            prefs.edit()
                .putLong("last_at", System.currentTimeMillis())
                .putString("last_result", e.javaClass.simpleName + ": " + (e.message ?: ""))
                .apply()
        }
    }

    companion object {
        private const val TAG = "NetflowTV"
        const val PREFS = "netflow"
        const val DEFAULT_SERVER = "http://192.168.50.1"
        private const val DEBOUNCE_MS = 1000L
        private const val HEARTBEAT_MS = 60_000L

        /** The bound listener, if any: lets the activity trigger a report. */
        @Volatile
        var instance: NowPlayingService? = null

        fun serverUrl(ctx: Context): String =
            ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .getString("server", DEFAULT_SERVER)!!.trimEnd('/')

        fun stateName(s: Int): String = when (s) {
            PlaybackState.STATE_PLAYING -> "playing"
            PlaybackState.STATE_PAUSED -> "paused"
            PlaybackState.STATE_BUFFERING -> "buffering"
            PlaybackState.STATE_STOPPED -> "stopped"
            PlaybackState.STATE_NONE -> "none"
            PlaybackState.STATE_ERROR -> "error"
            PlaybackState.STATE_CONNECTING -> "connecting"
            else -> "other"
        }
    }
}
