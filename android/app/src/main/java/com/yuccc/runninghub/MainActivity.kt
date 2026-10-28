package com.yuccc.runninghub

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.graphics.Color
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.view.ViewGroup
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
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
    private var filePathCallback: ValueCallback<Array<Uri>>? = null
    private var fileChooserIntent: Intent? = null

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
                // 后端就绪后再加载页面，确保前端 init() 的 API 请求成功
                progress.visibility = if (ok) View.GONE else View.VISIBLE
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
        // 暴露原生桥，供前端调用「浏览器登录」
        webView.addJavascriptInterface(Bridge(), "AndroidBridge")
        // 页面加载完把 App 自己 WebView 里已有的 runninghub 登录态回灌一次：
        // 用户上次在 App 内登录过就不用再登，也顺带刷新 token。
        webView.webViewClient = object : WebViewClient() {
            override fun onPageFinished(v: WebView?, url: String?) {
                super.onPageFinished(v, url)
                injectCachedSession()
            }
        }
        // 关键：让 <input type="file"> 能弹出系统文件选择器（上传参考图用）
        webView.webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(
                webView: WebView,
                filePathCallback: ValueCallback<Array<Uri>>,
                fileChooserParams: FileChooserParams
            ): Boolean {
                // 若已有回调未消费，先取消旧的（避免泄漏/重复弹窗）
                this@MainActivity.filePathCallback?.onReceiveValue(null)
                this@MainActivity.filePathCallback = filePathCallback
                try {
                    val intent = fileChooserParams.createIntent()
                    this@MainActivity.fileChooserIntent = intent
                    startActivityForResult(intent, REQ_FILE_CHOOSER)
                } catch (e: Exception) {
                    this@MainActivity.filePathCallback = null
                    return false
                }
                return true
            }
        }
    }

    private fun startPythonServer() {
        try {
            // 初始化产物导出器（MediaStore → 公共目录）
            Exporter.init(this)
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
            // 目标只读/异常时强制可写，避免静默失败导致前端停留在旧版本
            if (target.exists() && !target.canWrite()) {
                target.setWritable(true)
            }
            assets.open(assetName).use { input ->
                target.outputStream().use { output -> input.copyTo(output) }
            }
            android.util.Log.i("RunningHub", "asset 更新: $assetName -> ${target.absolutePath}")
        } catch (e: Exception) {
            android.util.Log.e("RunningHub", "asset 复制失败，删除后重试: $assetName", e)
            // 兜底：删掉旧文件重试一次（旧文件可能是只读/坏属主）
            try {
                target.delete()
                assets.open(assetName).use { input ->
                    target.outputStream().use { output -> input.copyTo(output) }
                }
                android.util.Log.i("RunningHub", "asset 重试成功: $assetName")
            } catch (e2: Exception) {
                android.util.Log.e("RunningHub", "asset 复制重试仍失败: $assetName", e2)
            }
        }
    }

    private fun waitForServer(onResult: (Boolean) -> Unit) {
        Thread {
            var ok = false
            for (i in 0 until 180) {  // 最长等 90s（Chaquopy 首次初始化可能很慢）
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

    companion object {
        private const val REQ_FILE_CHOOSER = 0x1001
    }

    override fun onBackPressed() {
        if (webView.canGoBack()) {
            webView.goBack()
        } else {
            super.onBackPressed()
        }
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == REQ_FILE_CHOOSER) {
            val cb = filePathCallback
            filePathCallback = null
            fileChooserIntent = null
            cb?.onReceiveValue(
                if (resultCode == Activity.RESULT_OK && data != null) {
                    if (data.clipData != null) {  // 多选
                        Array(data.clipData!!.itemCount) { i -> data.clipData!!.getItemAt(i).uri }
                    } else {
                        data.data?.let { arrayOf(it) } ?: arrayOf()
                    }
                } else {
                    null  // 用户取消 → 通知 WebView
                }
            )
        }
    }

    /** JS 桥：前端 window.AndroidBridge.* */
    inner class Bridge {
        @android.webkit.JavascriptInterface
        fun startBrowserLogin() {
            handler.post { openLoginWebView() }
        }
    }

    private var loginWebView: WebView? = null

    /**
     * 用独立的 WebView 打开 RunningHub 官网。
     * 用户在里面正常登录，页面跳转时自动检测 localStorage，取到 token 就关闭并回灌。
     */
    @SuppressLint("SetJavaScriptEnabled")
    private fun openLoginWebView() {
        if (loginWebView != null) return
        val lv = WebView(this)
        lv.settings.javaScriptEnabled = true
        lv.settings.domStorageEnabled = true
        lv.settings.databaseEnabled = true
        lv.settings.cacheMode = WebSettings.LOAD_DEFAULT
        // 去掉 WebView 标识：Google OAuth 会拒绝 wv 内核 UA（disallowed_useragent）
        lv.settings.userAgentString = lv.settings.userAgentString
            .replace("; wv", "")
            .replace(" Version/4.0", "")

        val dlg = android.app.AlertDialog.Builder(this)
            .setTitle("登录 RunningHub")
            .setView(lv)
            .setNegativeButton("取消") { _, _ -> cleanupLoginWebView() }
            .create()

        // Cookie 必须显式开启：Google OAuth 走第三方 cookie，不开会登不上
        try {
            val cm = android.webkit.CookieManager.getInstance()
            cm.setAcceptCookie(true)
            cm.setAcceptThirdPartyCookies(lv, true)
        } catch (e: Throwable) {}

        lv.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(v: WebView?, url: String?): Boolean = false
        }
        lv.webChromeClient = WebChromeClient()

        dlg.setOnDismissListener { cleanupLoginWebView() }
        loginWebView = lv
        // /Login/login 是 SPA 路由，直接打开为空白页；主页有登录按钮可正常弹窗
        lv.loadUrl("https://www.runninghub.ai")
        dlg.show()
        // 不依赖 onPageFinished：SPA 登录后不会重载页面，只挂在那里永远抓不到。
        // 弹窗一出现就开始轮询，直到拿到 token 或弹窗关闭。
        startPoll(lv, dlg)
    }

    /** 从 Cookie 串里取指定 name 的值 */
    private fun cookieValue(raw: String, name: String): String {
        if (raw.isEmpty()) return ""
        for (kv in raw.split(";")) {
            val t = kv.trim()
            if (t.startsWith(name + "=")) return t.substring(name.length + 1)
        }
        return ""
    }

    /**
     * App 冷启动时，把自身 WebView 里已有的 runninghub cookie 回灌到后端。
     * 用户上次在 App 内登录过，这里就能直接恢复，无需再次点「登录」。
     * 只在后端确实未登录时才触发，避免覆盖用户刚从网页版导入的更完整会话。
     */
    private fun injectCachedSession() {
        val cm = android.webkit.CookieManager.getInstance()
        val raw = try { cm.getCookie("https://www.runninghub.ai") ?: "" } catch (e: Throwable) { "" }
        val tok = cookieValue(raw, "Rh-Accesstoken")
        if (tok.isBlank()) return

        val payload = org.json.JSONObject().apply {
            put("token", tok)
            put("refresh", cookieValue(raw, "Rh-Refreshtoken"))
            put("identify", cookieValue(raw, "Rh-Identify"))
        }
        // 后端已配置就跳过（前端状态更权威），否则回灌
        webView.evaluateJavascript(
            "(function(){" +
            "  var p=" + org.json.JSONObject.quote(payload.toString()) + ";" +
            "  fetch('/api/session').then(function(r){return r.json();}).then(function(d){" +
            "    if(d && d.configured && !d.expired) return;" +
            "    if(typeof onBrowserLoginResult==='function') onBrowserLoginResult(p);" +
            "  }).catch(function(){});" +
            "  return 'ok';" +
            "})()",
            null)
    }


    /**
     * 弹窗存活期间轮询检测，拿到 token 立即导入并关窗。
     *
     * 双源合并，任一有 token 即可：
     *   1) Cookie（主源）：同步、不依赖 JS 执行；登录成功即写入，
     *      且 Rh-Identify 只在这里有（localStorage 里常常没有）。
     *   2) localStorage（副源）：补 cookie 里没有的 Rh-Comfy-Auth 等字段。
     */
    private fun startPoll(lv: WebView, dlg: android.app.AlertDialog) {
        val cm = android.webkit.CookieManager.getInstance()
        val jsLocal = """(function(){try{
            return JSON.stringify({
                token: localStorage.getItem('Rh-Accesstoken')||'',
                refresh: localStorage.getItem('Rh-Refreshtoken')||'',
                identify: localStorage.getItem('Rh-Identify')||'',
                comfy: localStorage.getItem('Rh-Comfy-Auth')||'',
                comfyExpire: Number(localStorage.getItem('Rh-Comfy-Expire-In')||0),
                expire: Number(localStorage.getItem('Rh-Expire-In')||0)
            });
        }catch(e){return '{}';}})()"""

        var tick: Runnable? = null
        val done = java.util.concurrent.atomic.AtomicBoolean(false)

        tick = object : Runnable {
            override fun run() {
                if (loginWebView == null || done.get()) return   // 弹窗已关，停止

                // 主源：Cookie
                val rawCk = try {
                    cm.getCookie("https://www.runninghub.ai") ?: ""
                } catch (e: Throwable) { "" }
                val ckTok = cookieValue(rawCk, "Rh-Accesstoken")

                lv.evaluateJavascript(jsLocal) { ret ->
                    if (loginWebView == null || done.get()) return@evaluateJavascript
                    val ls = try {
                        val r = org.json.JSONTokener(ret).nextValue() as? String ?: ret
                        org.json.JSONObject(r)
                    } catch (e: Throwable) { org.json.JSONObject() }

                    val tok = ckTok.ifBlank { ls.optString("token", "") }
                    if (tok.isBlank()) {
                        handler.postDelayed(tick!!, 800)      // 还没登录，继续等
                        return@evaluateJavascript
                    }

                    done.set(true)
                    val payload = org.json.JSONObject().apply {
                        put("token", tok)
                        put("refresh", ls.optString("refresh", "")
                            .ifBlank { cookieValue(rawCk, "Rh-Refreshtoken") })
                        put("identify", ls.optString("identify", "")
                            .ifBlank { cookieValue(rawCk, "Rh-Identify") })
                        put("comfy", ls.optString("comfy", ""))
                        put("comfyExpire", ls.optLong("comfyExpire", 0))
                        put("expire", ls.optLong("expire", 0))
                    }
                    handler.post {
                        // 落盘，下次冷启动可直接复用登录态
                        try { cm.flush() } catch (e: Throwable) {}
                        try { dlg.dismiss() } catch (e: Throwable) {}
                        webView.evaluateJavascript(
                            "onBrowserLoginResult(" + org.json.JSONObject.quote(payload.toString()) + ")",
                            null)
                    }
                }
            }
        }
        handler.postDelayed(tick!!, 600)
    }

    private fun cleanupLoginWebView() {
        try { loginWebView?.destroy() } catch (e: Throwable) {}
        loginWebView = null
    }
    override fun onDestroy() {
        // 释放未完成的文件选择回调，避免 WebView 泄漏
        filePathCallback?.onReceiveValue(null)
        filePathCallback = null
        webView.destroy()
        super.onDestroy()
    }

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()
}