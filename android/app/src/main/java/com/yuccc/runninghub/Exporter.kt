package com.yuccc.runninghub

import android.content.ContentValues
import android.content.Context
import android.os.Build
import android.os.Environment
import android.provider.MediaStore
import java.io.File

/**
 * 产物导出器：把 app 私有目录（filesDir/workflows/webui_out）里的产物，
 * 通过 MediaStore 写入公共目录（无需任何存储权限，targetSdk 34 下系统认可的唯一通道）。
 *
 * 图片 → Pictures/RunningHub（相册直接可见）
 * 视频 → Movies/RunningHub
 * 其它 → Download/RunningHub
 *
 * Python 侧通过 Chaquopy 调用：Exporter.init(ctx) 一次，之后 export(srcPath) 即可。
 */
object Exporter {

    @Volatile
    private var appContext: Context? = null

    @JvmStatic
    fun init(context: Context) {
        if (appContext == null) {
            appContext = context.applicationContext
        }
    }

    /** 导出单个文件，成功返回写入的 displayName，失败返回空串。 */
    @JvmStatic
    fun export(srcPath: String, displayName: String?): String {
        val ctx = appContext ?: return ""
        val src = File(srcPath)
        if (!src.exists() || !src.isFile) return ""
        val name = displayName?.takeIf { it.isNotBlank() } ?: src.name

        return try {
            val ext = name.substringAfterLast('.', "").lowercase()
            val isVideo = ext in setOf("mp4", "webm", "mov", "mkv", "avi")
            val isImage = ext in setOf("png", "jpg", "jpeg", "webp", "gif", "bmp")

            val collection = when {
                isImage -> MediaStore.Images.Media.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY)
                isVideo -> MediaStore.Video.Media.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY)
                else -> MediaStore.Downloads.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY)
            }
            val relPath = when {
                isImage -> Environment.DIRECTORY_PICTURES + "/RunningHub"
                isVideo -> Environment.DIRECTORY_MOVIES + "/RunningHub"
                else -> Environment.DIRECTORY_DOWNLOADS + "/RunningHub"
            }

            val values = ContentValues().apply {
                put(MediaStore.MediaColumns.DISPLAY_NAME, name)
                put(MediaStore.MediaColumns.MIME_TYPE, mimeOf(ext))
                put(MediaStore.MediaColumns.RELATIVE_PATH, relPath)
                if (Build.VERSION.SDK_INT >= 29) {
                    put(MediaStore.MediaColumns.IS_PENDING, 1)
                }
            }

            val resolver = ctx.contentResolver
            val uri = resolver.insert(collection, values) ?: return ""
            resolver.openOutputStream(uri)?.use { out ->
                src.inputStream().use { it.copyTo(out) }
            } ?: run {
                resolver.delete(uri, null, null)
                return ""
            }
            if (Build.VERSION.SDK_INT >= 29) {
                values.clear()
                values.put(MediaStore.MediaColumns.IS_PENDING, 0)
                resolver.update(uri, values, null, null)
            }
            name
        } catch (e: Exception) {
            e.printStackTrace()
            ""
        }
    }

    /** 导出文件列表（内部循环调用）。 */
    @JvmStatic
    fun exportList(paths: Array<String>): Array<String> {
        val done = mutableListOf<String>()
        for (p in paths) {
            val n = export(p, null)
            if (n.isNotEmpty()) done.add(n)
        }
        return done.toTypedArray()
    }

    private fun mimeOf(ext: String): String = when (ext) {
        "png" -> "image/png"
        "jpg", "jpeg" -> "image/jpeg"
        "webp" -> "image/webp"
        "gif" -> "image/gif"
        "bmp" -> "image/bmp"
        "mp4" -> "video/mp4"
        "webm" -> "video/webm"
        "mov" -> "video/quicktime"
        "mkv" -> "video/x-matroska"
        "avi" -> "video/x-msvideo"
        "mp3" -> "audio/mpeg"
        else -> "application/octet-stream"
    }
}