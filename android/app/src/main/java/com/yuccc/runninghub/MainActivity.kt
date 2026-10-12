package com.yuccc.runninghub

import android.annotation.SuppressLint
import android.graphics.Color
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.view.ViewGroup
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.widget.ProgressBar
import androidx.appcompat.app.AppCompatActivity
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.io.File
import java.net.HttpURLConnection
import java.net.URL

class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private lateinit var progress: ProgressBar
    private val handler = Handler(Looper.getMainLooper())

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // ---- UI：WebView + 顶部加载条 ----
        webView = WebView(this)
        progress = ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal)
        progress.visibility = View.GONE

        val root = FrameLayout(this)
        root.setBackgroundColor(Color.parseColor("#0F1115"))
        root.addView(webView, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.MATCH_PARENT
        ))
        val bar = FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, dp(3)
        )
        root.addView(progress, bar)
        setContentView(root)

        setupWebView()

        // 清 WebView 缓存，避免覆盖安装后残留旧页面（localhost 版）
        WebView(this).clearCache(true)
        webView.clearHistory()

        // ---- 启动内嵌 Python 后端 ----
        startPythonServer()
        waitForServer { ok ->
            handler.post {
                progress.visibility = if (ok) View.GONE else View.VISIBLE
                // 无论后端是否就绪都加载页面，避免缓存旧页面；后端起来后页面会自动连上
                webView.loadUrl("http://127.0.0.1:8765/")
            }
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun setupWebView() {
        val ws: WebSettings = webView.settings
        ws.javaScriptEnabled = true
        ws.domStorageEnabled = true
        ws.allowFileAccess = true
        ws.mediaPlaybackRequiresUserGesture = false
        ws.cacheMode = WebSettings.LOAD_NO_CACHE
        webView.webViewClient = WebViewClient()
    }

    private fun startPythonServer() {
        try {
            // 初始化 Chaquopy 运行时
            if (!Python.isStarted()) {
                Python.start(AndroidPlatform(this))
            }
            // 前端页面复制到可写目录（Python 后端从这里读 HTML）
            copyAssetToFile("webui.html", File(filesDir, "webui.html"))
            // 校正 rh_webui 的运行时根目录为 filesDir（可读写），并在后台线程拉起 HTTP 服务
            val py = Python.getInstance()
            Thread {
                try {
                    py.getModule("serve").callAttr("start", filesDir.absolutePath)
                } catch (t: Throwable) {
                    t.printStackTrace()
                }
            }.start()
        } catch (t: Throwable) {
            t.printStackTrace()
        }
    }

    private fun copyAssetToFile(assetName: String, target: File) {
        try {
            assets.open(assetName).use { input ->
                target.outputStream().use { output -> input.copyTo(output) }
            }
        } catch (e: Exception) {
            e.printStackTrace()
        }
    }

    private fun waitForServer(onResult: (Boolean) -> Unit) {
        Thread {
            var ok = false
            for (i in 0 until 60) {  // 最长等 30s
                try {
                    val conn = URL("http://127.0.0.1:8765/").openConnection()
                            as HttpURLConnection
                    conn.connectTimeout = 1000
                    conn.readTimeout = 1000
                    conn.requestMethod = "GET"
                    val code = conn.responseCode
                    conn.disconnect()
                    if (code in 200..499) { ok = true; break }
                } catch (e: Exception) { /* 服务未起，重试 */ }
                try { Thread.sleep(500) } catch (e: InterruptedException) { break }
            }
            onResult(ok)
        }.start()
    }

    override fun onBackPressed() {
        if (webView.canGoBack()) {
            webView.goBack()
        } else {
            super.onBackPressed()
        }
    }

    override fun onDestroy() {
        webView.destroy()
        super.onDestroy()
    }

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()
}