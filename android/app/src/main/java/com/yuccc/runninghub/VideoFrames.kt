package com.yuccc.runninghub

import android.graphics.Bitmap
import android.media.MediaMetadataRetriever
import java.io.File
import java.io.FileOutputStream

/**
 * 视频尾帧抽取器（Android 端）。
 *
 * Chaquopy 内嵌 Python 没有 ffmpeg，无法解码 mp4，因此安卓端抽帧走系统
 * MediaMetadataRetriever（无需任何权限/Context，可解码 mp4/webm/mov 等）。
 *
 * Python 侧通过 Chaquopy 调用：VideoFrames.extractLastFrame(videoPath, outPath)。
 */
object VideoFrames {

    /**
     * 抽取视频最后一帧，保存为 JPEG 到 outPath。成功返回 true。
     * 取末尾前 100ms 处的帧，避免正好落在视频末端返回 null。
     */
    @JvmStatic
    fun extractLastFrame(videoPath: String, outPath: String): Boolean {
        val src = File(videoPath)
        if (!src.exists() || !src.isFile) return false

        var mmr: MediaMetadataRetriever? = null
        return try {
            mmr = MediaMetadataRetriever()
            mmr.setDataSource(videoPath)

            val durMs = mmr.extractMetadata(
                MediaMetadataRetriever.METADATA_KEY_DURATION
            )?.toLongOrNull() ?: 0L

            val us = if (durMs > 100) (durMs - 100) * 1000L else 0L

            // 优先精确取帧（不限关键帧）；失败再退到最近关键帧
            var bmp = mmr.getFrameAtTime(us, MediaMetadataRetriever.OPTION_CLOSEST)
            if (bmp == null) {
                bmp = mmr.getFrameAtTime(us, MediaMetadataRetriever.OPTION_CLOSEST_SYNC)
            }
            if (bmp == null) return false

            val out = File(outPath)
            out.parentFile?.mkdirs()
            FileOutputStream(out).use { fos ->
                bmp.compress(Bitmap.CompressFormat.JPEG, 92, fos)
            }
            bmp.recycle()
            true
        } catch (e: Exception) {
            e.printStackTrace()
            false
        } finally {
            try {
                mmr?.release()
            } catch (_: Exception) {
            }
        }
    }
}
