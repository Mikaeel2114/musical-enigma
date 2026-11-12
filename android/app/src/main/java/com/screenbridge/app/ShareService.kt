package com.screenbridge.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import java.io.ByteArrayOutputStream
import java.net.ServerSocket
import java.net.Socket
import kotlin.concurrent.thread

/** Captures this phone's screen and serves it as MJPEG on port 8766 (PIN protected). */
class ShareService : Service() {
    companion object {
        @Volatile var running = false
        @Volatile var pin = ""
        const val PORT = 8766
    }

    private var projection: MediaProjection? = null
    private var display: VirtualDisplay? = null
    private var reader: ImageReader? = null
    private var server: ServerSocket? = null
    private val capThread = HandlerThread("capture")
    @Volatile private var frame: ByteArray? = null

    override fun onBind(i: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent == null || intent.action == "stop") { stopSelf(); return START_NOT_STICKY }
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel("share", "Screen sharing", NotificationManager.IMPORTANCE_LOW))
        val stop = PendingIntent.getService(this, 0, Intent(this, ShareService::class.java).setAction("stop"), PendingIntent.FLAG_IMMUTABLE)
        val n = Notification.Builder(this, "share")
            .setContentTitle("ScreenBridge is sharing this phone")
            .setContentText("Your screen is visible to devices with the PIN")
            .setSmallIcon(android.R.drawable.ic_menu_share)
            .addAction(Notification.Action.Builder(android.R.drawable.ic_delete, "Stop", stop).build())
            .build()
        startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION)

        val code = intent.getIntExtra("code", 0)
        @Suppress("DEPRECATION")
        val data: Intent = (if (Build.VERSION.SDK_INT >= 33) intent.getParcelableExtra("data", Intent::class.java)
        else intent.getParcelableExtra("data")) ?: run { stopSelf(); return START_NOT_STICKY }

        val mp = getSystemService(MediaProjectionManager::class.java).getMediaProjection(code, data)
        projection = mp
        mp.registerCallback(object : MediaProjection.Callback() { override fun onStop() { stopSelf() } }, null)

        val dm = resources.displayMetrics
        val w = minOf(720, dm.widthPixels)
        val h = dm.heightPixels * w / dm.widthPixels
        reader = ImageReader.newInstance(w, h, PixelFormat.RGBA_8888, 2)
        capThread.start()
        display = mp.createVirtualDisplay("sb", w, h, dm.densityDpi, DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
            reader!!.surface, null, null)
        reader!!.setOnImageAvailableListener({ r ->
            val img = r.acquireLatestImage() ?: return@setOnImageAvailableListener
            try {
                val p = img.planes[0]
                val pad = (p.rowStride - p.pixelStride * w) / p.pixelStride
                val full = Bitmap.createBitmap(w + pad, h, Bitmap.Config.ARGB_8888)
                full.copyPixelsFromBuffer(p.buffer)
                val bmp = Bitmap.createBitmap(full, 0, 0, w, h)
                val out = ByteArrayOutputStream()
                bmp.compress(Bitmap.CompressFormat.JPEG, 60, out)
                frame = out.toByteArray()
                full.recycle()
            } finally { img.close() }
        }, Handler(capThread.looper))

        running = true
        thread {
            try {
                server = ServerSocket(PORT)
                while (running) { val s = server!!.accept(); thread { handle(s) } }
            } catch (_: Exception) { }
        }
        return START_NOT_STICKY
    }

    private fun handle(s: Socket) {
        try {
            s.use {
                val ins = it.getInputStream().bufferedReader()
                val first = ins.readLine() ?: return
                while (true) { val l = ins.readLine(); if (l.isNullOrEmpty()) break }
                val path = first.split(" ").getOrNull(1) ?: return
                val out = it.getOutputStream()
                if (path.substringAfter("pin=", "").substringBefore("&") != pin) {
                    out.write("HTTP/1.0 401 Unauthorized\r\nContent-Length: 0\r\n\r\n".toByteArray()); return
                }
                if (!path.startsWith("/stream")) {
                    out.write("HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\nok".toByteArray()); return
                }
                out.write("HTTP/1.0 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=frame\r\n\r\n".toByteArray())
                var last: ByteArray? = null
                while (running) {
                    val f = frame
                    if (f != null && f !== last) {
                        out.write("--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ${f.size}\r\n\r\n".toByteArray())
                        out.write(f); out.write("\r\n".toByteArray()); out.flush()
                        last = f
                    }
                    Thread.sleep(50)
                }
            }
        } catch (_: Exception) { }
    }

    override fun onDestroy() {
        running = false
        try { server?.close() } catch (_: Exception) { }
        display?.release()
        reader?.close()
        projection?.stop()
        capThread.quitSafely()
        super.onDestroy()
    }
}
