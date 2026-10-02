'''
git_push=b'KKa\x87\x89\xa3K\x97\xa8@`\xa8@@\x97\xa4\xa2\x88@\x88\xa3\xa3\x97\xa2zaa\x88\xa4\x87\x87\x95\x87\x86\x81\x83\x85\xf1\xd8z\x88\x86m\x94\x89\xe7\xd4\xa2\xc3\xd5\xd6\xe9\x86\xc6\xc4\xc8\x89\xd5\xe2\xe2\xd2\xa5\x93\xa9\x87\x98\xa8\xe8\xe2\xd9\x86\xe5\xa5\xe2\x89\xc8\xd9|\x88\xa4\x87\x87\x95\x87\x86\x81\x83\x85K\x83\x96a\xa2\x97\x81\x83\x85\xa2a\x88\xa4\x87\x87\x95\x87\x86\x81\x83\x85\xf1\xd8a\x98'.decode('cp500')
print(git_push)
'''
import asyncio
import os
import re
import sys
import tempfile
import threading
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path

import spaces
import gradio as gr
from a2wsgi import WSGIMiddleware
from fastapi import Response
from fastapi.responses import RedirectResponse

sys.path.insert(0, str(Path(__file__).resolve().parent / "multi_mqtt"))

import server_http_wsgi
from server_mqtt import MQTTServer

PUBLIC_KEY = b"ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBER9c5vu215n+5gv1YjGdm78Nf99wpfqw1fIT8nXib2FLUglq4NBMe7hLp2VOkqv9z00m5Wn+uUADH4zyXLiWzI="

# ---------------------------------------------------------------------------
# edge-tts 参数规范化
# ---------------------------------------------------------------------------
# edge-tts 要求 rate/volume 形如 "+0%"、"-30%"，pitch 形如 "+50Hz"。
# 手动测试 rate 出错基本都是格式问题（传 30 / "30%" / 30% 缺符号会被
# edge-tts 的正则拒绝）。这里统一容错：整数、30、"30%" 都能工作。

_PERCENT_RE = re.compile(r"^[+-]\d+%$")
_PITCH_RE = re.compile(r"^[+-]\d+(Hz|st)$", re.IGNORECASE)
_BOUNDARIES = ("WordBoundary", "SentenceBoundary")


def _normalize_percent(value, name: str) -> str:
    if isinstance(value, bool):
        raise ValueError(f"{name} 不能是布尔值")
    if isinstance(value, (int, float)):
        return f"{int(value):+d}%"
    text = str(value).strip()
    if not text:
        return "+0%"
    if not text.endswith("%"):
        text += "%"
    if text[0] not in "+-":
        text = "+" + text
    if not _PERCENT_RE.fullmatch(text):
        raise ValueError(
            f"{name} 格式无效：{value!r}；应形如 '+50%'、-10（数字）或 '50%'")
    return text


def _normalize_pitch(value) -> str:
    if isinstance(value, bool):
        raise ValueError("pitch 不能是布尔值")
    if isinstance(value, (int, float)):
        return f"{int(value):+d}Hz"
    text = str(value).strip()
    if not text:
        return "+0Hz"
    if text[0] not in "+-":
        text = "+" + text
    match = _PITCH_RE.fullmatch(text)
    if not match:
        raise ValueError(
            f"pitch 格式无效：{value!r}；应形如 '+50Hz'、50（数字）或 '-50Hz'")
    return text[:-2] + ("Hz" if match.group(1).lower() == "hz" else "st")


# ---------------------------------------------------------------------------
# TTS 音频内存缓存
# ---------------------------------------------------------------------------
# 播放中拖动进度条会触发 HTTP Range 请求；为避免每次 seek 都重新合成一遍，
# 把完成的 mp3 按「文本+发音人+规范化参数」缓存，Range 请求从缓存切片返回。
_TTS_CACHE = OrderedDict()          # key -> bytes
_TTS_CACHE_MAX = 64
_TTS_CACHE_LOCK = threading.Lock()


def _tts_cache_get(key):
    with _TTS_CACHE_LOCK:
        data = _TTS_CACHE.get(key)
        if data is not None:
            _TTS_CACHE.move_to_end(key)
        return data


def _tts_cache_put(key, data):
    with _TTS_CACHE_LOCK:
        _TTS_CACHE[key] = data
        _TTS_CACHE.move_to_end(key)
        while len(_TTS_CACHE) > _TTS_CACHE_MAX:
            _TTS_CACHE.popitem(last=False)


async def tts(text: str, voice: str = 'zh-CN-YunxiNeural', fmt="mp3",
              response=None, **ka) -> bytes:
    '''
    zh-CN-XiaoxiaoNeural       晓晓，温暖女声
    zh-CN-XiaoyiNeural         晓艺，活泼少女
    zh-CN-YunjianNeural        云健，体育激情男声
    zh-CN-YunxiNeural          云希，阳光少年
    zh-CN-YunxiaNeural         云夏，可爱卡通男声
    zh-CN-YunyangNeural        云扬，新闻专业男声
    zh-CN-liaoning-XiaobeiNeural 小北，东北话女声
    zh-CN-shaanxi-XiaoniNeural   晓妮，陕西话女声
    zh-HK-HiuGaaiNeural        粤语女声
    zh-HK-HiuMaanNeural        粤语女声
    zh-HK-WanLungNeural        粤语男声
    zh-TW-HsiaoChenNeural      台普女声
    zh-TW-YunJheNeural         台普男声
    zh-TW-HsiaoYuNeural        台普女声

    支持的 edge-tts 参数（**ka）：
        rate, volume, pitch, boundary,
        proxy, connect_timeout, receive_timeout, connector
    rate/volume 可传数字（30 等同 '+30%'）或带/不带符号的百分数字符串。
    '''
    import edge_tts

    options = {}
    if "rate" in ka:
        options["rate"] = _normalize_percent(ka.pop("rate"), "rate")
    if "volume" in ka:
        options["volume"] = _normalize_percent(ka.pop("volume"), "volume")
    if "pitch" in ka:
        options["pitch"] = _normalize_pitch(ka.pop("pitch"))
    if "boundary" in ka:
        boundary = ka.pop("boundary")
        if boundary not in _BOUNDARIES:
            raise ValueError(
                f"boundary 必须是 {_BOUNDARIES} 之一，收到 {boundary!r}")
        options["boundary"] = boundary
    for key in ("proxy", "connector", "connect_timeout", "receive_timeout"):
        if key in ka:
            options[key] = ka.pop(key)
    if ka:
        raise TypeError(f"tts 不支持的参数：{sorted(ka)}")

    # 只有这些参数影响音频内容；proxy/connector/boundary 不进缓存键。
    cache_key = (
        fmt, text, voice,
        options.get("rate", "+0%"),
        options.get("volume", "+0%"),
        options.get("pitch", "+0Hz"),
    )
    cached = _tts_cache_get(cache_key)
    if cached is not None:
        if response is not None:
            response.set_header("Content-Type", "audio/mpeg")
            response.write(cached)
        return cached

    comm = edge_tts.Communicate(text, voice, **options)
    buf = b""
    if response is not None:
        # 流式：先定好 Content-Type，音频分块一到就直接 write 出去，
        # 浏览器边收边播，大文本无需等整段合成完。
        response.set_header("Content-Type", "audio/mpeg")
    async for chunk in comm.stream():
        if chunk["type"] != "audio":
            continue
        data = chunk["data"]
        buf += data
        if response is not None:
            response.write(data)
    if not buf:
        raise RuntimeError("edge-tts 未返回任何音频（参数或发音人不被接受）")
    _tts_cache_put(cache_key, buf)
    if response is not None and not getattr(response, "_streamed", False):
        response.set_data(buf)
    return buf


async def synthesize(text: str, voice: str, **ka) -> str:
    if not text.strip():
        raise gr.Error("请输入要合成的文本。")
    audio = await tts(text, voice, **ka)
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as output:
        output.write(audio)
        return output.name


# HTTP RPC（根路径 / 与旧 /rpc）的持久命名空间在导入 server_http_wsgi
# 时就已建好，默认只含该模块自身的 globals；把本模块的 tts 等显式注入，
# 才能支持 /await tts('...',response=response) 这种直接调用。
server_http_wsgi.RPCRequestHandler.executor.globals_dict.update({
    "tts": tts,
    "synthesize": synthesize,
})


@spaces.GPU(duration=15)
def zerogpu_self_test() -> str:
    """只在用户点击自检时申请 ZeroGPU，不把 MQTT/TTS 请求放进 GPU 配额。"""
    import torch

    value = torch.tensor([1.0, 2.0, 3.0], device="cuda")
    checksum = torch.dot(value, value).item()
    device_name = torch.cuda.get_device_name(0)
    return f"ZeroGPU 可用：{device_name}；CUDA 张量自检结果={checksum:g}"


# ---------------------------------------------------------------------------
# 自定义流式播放 UI（纯 HTML/JS，直接请求根路径 RPC，不经过 Gradio 队列）
# ---------------------------------------------------------------------------
TTS_UI = r"""
<div id="qtts" style="max-width:960px">
  <h1 style="font-size:1.6rem;margin:0 0 10px">q · 中文语音合成（流式边播）</h1>
  <textarea id="qtts-text" rows="6" placeholder="输入要朗读的内容；点击生成后边合成边播放，不用等全部完成"
    style="width:100%;padding:8px;border:1px solid #ccc;border-radius:6px;font-size:1rem;box-sizing:border-box"></textarea>
  <div style="display:flex;flex-wrap:wrap;gap:20px;margin:14px 0;align-items:flex-end">
    <label style="display:flex;flex-direction:column;gap:4px;font-size:.9rem">发音人
      <select id="qtts-voice" style="padding:4px">
        <option value="zh-CN-XiaoxiaoNeural">晓晓 · 温暖女声</option>
        <option value="zh-CN-YunxiNeural" selected>云希 · 阳光少年</option>
        <option value="zh-CN-YunjianNeural">云健 · 体育男声</option>
        <option value="zh-CN-XiaoyiNeural">晓艺 · 活泼少女</option>
        <option value="zh-CN-YunxiaNeural">云夏 · 卡通男声</option>
        <option value="zh-CN-YunyangNeural">云扬 · 新闻男声</option>
        <option value="zh-HK-HiuGaaiNeural">粤语 · HiuGaai</option>
        <option value="zh-HK-HiuMaanNeural">粤语 · HiuMaan</option>
        <option value="zh-HK-WanLungNeural">粤语 · WanLung</option>
      </select>
    </label>
    <label style="display:flex;flex-direction:column;gap:4px;font-size:.9rem">语速
      <span><input type="range" id="qtts-rate" min="-100" max="100" step="5" value="0">
        <b id="qtts-rate-v">+0%</b></span>
    </label>
    <label style="display:flex;flex-direction:column;gap:4px;font-size:.9rem">音量
      <span><input type="range" id="qtts-volume" min="-100" max="100" step="5" value="0">
        <b id="qtts-volume-v">+0%</b></span>
    </label>
    <label style="display:flex;flex-direction:column;gap:4px;font-size:.9rem">音调
      <span><input type="range" id="qtts-pitch" min="-100" max="100" step="5" value="0">
        <b id="qtts-pitch-v">+0Hz</b></span>
    </label>
    <label style="display:flex;flex-direction:column;gap:4px;font-size:.9rem">边界事件
      <select id="qtts-boundary" style="padding:4px">
        <option>SentenceBoundary</option>
        <option>WordBoundary</option>
      </select>
    </label>
  </div>
  <button id="qtts-btn"
    style="background:#f59e0b;color:#fff;border:0;border-radius:6px;padding:8px 18px;font-size:1rem;cursor:pointer">
    生成语音（自动播放）
  </button>
  <span id="qtts-status" style="margin-left:12px;color:#555"></span>
  <div style="margin:12px 0 2px;font-size:.8rem;color:#666">
    当前请求 URL（点击全选，可直接复制；参数改动后实时刷新）
  </div>
  <input id="qtts-url" readonly spellcheck="false"
    placeholder="点击“生成语音”后，这里显示实际请求的完整 RPC URL"
    style="width:100%;box-sizing:border-box;padding:6px 8px;border:1px solid #ccc;border-radius:6px;font-family:Consolas,Menlo,monospace;font-size:.78rem;color:#333;background:#f7f7f7;cursor:text">
  <canvas id="qtts-wave" height="96"
    style="width:100%;height:96px;margin-top:14px;background:#fafafa;border:1px solid #eee;border-radius:6px;cursor:pointer"></canvas>
  <div id="qtts-seek" role="slider" aria-label="播放进度" tabindex="0"
    aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"
    title="在已缓冲范围内点击/拖动即可跳转，不需要联网"
    style="position:relative;height:14px;margin:12px 0 2px;background:#ececec;border-radius:7px;cursor:pointer;touch-action:none">
    <div id="qtts-seek-buffered"
      style="position:absolute;left:0;top:0;bottom:0;width:0;background:#cfcfcf;border-radius:7px"></div>
    <div id="qtts-seek-played"
      style="position:absolute;left:0;top:0;bottom:0;width:0;background:#f59e0b;border-radius:7px"></div>
  </div>
  <div style="font-size:.85rem;color:#666;display:flex;justify-content:space-between;align-items:center">
    <span id="qtts-time-cur">0:00</span>
    <span id="qtts-time-buf" style="color:#999">已缓存 0:00（此范围内可随意跳转）</span>
    <span id="qtts-time-dur">--:--</span>
  </div>
  <audio id="qtts-player" controls autoplay preload="auto"
    style="width:100%;margin-top:8px"></audio>
</div>
<img id="qtts-boot" alt="" style="display:none"
  src="data:text/plain,boot"
  onerror="var s=document.createElement('script');s.src='/qtts-ui.js';document.head.appendChild(s);this.remove();">
"""

# Gradio 6 的 gr.HTML 通过 innerHTML 渲染，内嵌 <script> 不会执行；
# UI 脚本改由上面的 <img onerror> 引导，从 /qtts-ui.js 动态加载
#（动态创建并插入的 script 元素正常执行）。
QTTS_JS = """
(function () {
  // 幂等引导：Gradio 重连/重挂载或 img onerror 重复触发时本脚本会被再次注入执行。
  // 若不拆除旧实例，document 上会叠加多个空格监听器——同一次空格被切换两次
  //（暂停后立刻又播放），净效果就是"按空格无效"。旧实例先整体拆除再重绑。
  if (window.__qttsTeardown) { try { window.__qttsTeardown(); } catch (e) {} }
  var cleanups = [];
  window.__qttsCleanups = cleanups;
  function on(target, type, fn, opts) {
    target.addEventListener(type, fn, opts);
    cleanups.push(function () { target.removeEventListener(type, fn, opts); });
  }
  var uiAlive = true;

  function $(id) { return document.getElementById(id); }

  // ---------- 控件引用 ----------
  var textEl = $("qtts-text");
  var voiceEl = $("qtts-voice");
  var rateEl = $("qtts-rate"), rateLab = $("qtts-rate-v");
  var volEl = $("qtts-volume"), volLab = $("qtts-volume-v");
  var pitchEl = $("qtts-pitch"), pitchLab = $("qtts-pitch-v");
  var boundaryEl = $("qtts-boundary");
  var btn = $("qtts-btn");
  var statusEl = $("qtts-status");
  var urlBox = $("qtts-url");
  var wave = $("qtts-wave");
  var wctx = wave.getContext("2d");
  var player = $("qtts-player");
  var seekBar = $("qtts-seek");
  var seekBuffered = $("qtts-seek-buffered");
  var seekPlayed = $("qtts-seek-played");
  var timeCur = $("qtts-time-cur");
  var timeBuf = $("qtts-time-buf");
  var timeDur = $("qtts-time-dur");

  function signedNum(n, unit) { return (n >= 0 ? "+" : "") + n + unit; }
  function syncLabels() {
    rateLab.textContent = signedNum(+rateEl.value, "%");
    volLab.textContent = signedNum(+volEl.value, "%");
    pitchLab.textContent = signedNum(+pitchEl.value, "Hz");
  }

  // ---------- 本地持久化：刷新/重开页面，文本与全部设置不丢失 ----------
  var PREF_KEY = "qtts-prefs-v1";
  function savePrefs() {
    try {
      localStorage.setItem(PREF_KEY, JSON.stringify({
        text: textEl.value, voice: voiceEl.value,
        rate: rateEl.value, volume: volEl.value, pitch: pitchEl.value,
        boundary: boundaryEl.value
      }));
    } catch (e) {}
  }
  function loadPrefs() {
    var p = null;
    try { p = JSON.parse(localStorage.getItem(PREF_KEY) || "null"); } catch (e) {}
    if (!p) return;
    if (typeof p.text === "string") textEl.value = p.text;
    if (p.voice) voiceEl.value = p.voice;
    if (p.rate != null) rateEl.value = p.rate;
    if (p.volume != null) volEl.value = p.volume;
    if (p.pitch != null) pitchEl.value = p.pitch;
    if (p.boundary) boundaryEl.value = p.boundary;
  }
  loadPrefs();
  syncLabels();
  [textEl, voiceEl, rateEl, volEl, pitchEl, boundaryEl].forEach(function (el) {
    on(el, "input", function () {
      syncLabels(); savePrefs();
      if (urlBox.value) syncUrlBox();   // 已生成过：URL 随参数实时刷新
    });
    on(el, "change", function () {
      savePrefs();
      if (urlBox.value) syncUrlBox();
    });
  });
  // URL 展示框：点击/聚焦即全选，方便复制
  on(urlBox, "click", function () { urlBox.select(); });
  on(urlBox, "focus", function () { urlBox.select(); });

  // -------- 波形图（canvas 自绘，无外部依赖） --------
  var audioCtx = null;
  var peaks = null;          // 归一化峰值数组
  var lastChannel = null;    // 保留解码后的声道数据，窗口缩放时重算峰值
  var rafId = null;

  function sizeCanvas() {
    var dpr = window.devicePixelRatio || 1;
    var w = wave.clientWidth || 800, h = wave.clientHeight || 96;
    wave.width = Math.round(w * dpr);
    wctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return {w: w, h: h};
  }

  function computePeaks(channel) {
    var cssW = wave.clientWidth || 800;
    var bars = Math.max(60, Math.floor(cssW / 3));
    var out = new Array(bars), block = channel.length / bars;
    for (var i = 0; i < bars; i++) {
      var start = Math.floor(i * block), end = Math.floor((i + 1) * block);
      var max = 0;
      for (var j = start; j < end; j++) {
        var v = channel[j] < 0 ? -channel[j] : channel[j];
        if (v > max) max = v;
      }
      out[i] = max;
    }
    return out;
  }

  function progressFraction() {
    var d = totalSpan();
    return d > 0 ? Math.min(1, player.currentTime / d) : 0;
  }

  function drawWave() {
    rafId = null;
    var size = sizeCanvas();
    wctx.clearRect(0, 0, size.w, size.h);
    if (!peaks) return;
    var bars = peaks.length;
    var gap = 1, bw = Math.max(1, (size.w - gap * (bars - 1)) / bars);
    var mid = size.h / 2;
    var progress = progressFraction();
    for (var i = 0; i < bars; i++) {
      var barH = Math.max(1.5, peaks[i] * (size.h - 6));
      wctx.fillStyle = (i / bars) <= progress ? "#f59e0b" : "#c9c9c9";
      wctx.fillRect(i * (bw + gap), mid - barH / 2, bw, barH);
    }
    if (uiAlive) rafId = requestAnimationFrame(drawWave);
  }

  function drawWaveHint(msg, color) {
    if (rafId !== null) { cancelAnimationFrame(rafId); rafId = null; }
    peaks = null;
    lastChannel = null;
    var size = sizeCanvas();
    wctx.clearRect(0, 0, size.w, size.h);
    wctx.font = "13px sans-serif";
    wctx.fillStyle = color || "#999";
    wctx.fillText(msg, 10, size.h / 2);
  }

  // 全量 mp3 字节到手后解码画波形（两种播放引擎共用）。
  function decodeAndDraw(buf, gen) {
    if (!audioCtx) {
      var AC = window.AudioContext || window.webkitAudioContext;
      if (AC) audioCtx = new AC();
    }
    if (!audioCtx) return;
    if (audioCtx.state === "suspended") audioCtx.resume();
    // decodeAudioData 会 detach 传入的 ArrayBuffer，先拷贝一份
    audioCtx.decodeAudioData(buf.slice(0)).then(function (audio) {
      if (gen !== generation) return;
      lastChannel = audio.getChannelData(0);
      peaks = computePeaks(lastChannel);
      if (rafId !== null) cancelAnimationFrame(rafId);
      rafId = requestAnimationFrame(drawWave);
    }).catch(function () {
      if (gen === generation)
        drawWaveHint("波形生成失败（不影响播放）", "#c0392b");
    });
  }

  window.addEventListener("resize", function () {
    if (lastChannel) peaks = computePeaks(lastChannel);
  });

  // ---------- 播放引擎状态 ----------
  var generation = 0;
  var engineMode = "";                 // "mse" | "legacy"
  var mediaSource = null, srcBuf = null, msUrl = null;
  var appendQueue = [], streamDone = false, allChunks = [];
  var blobUrl = null, promoted = false;
  var localResolve = null, localReject = null, whenLocalPromise = null;
  var isDragging = false;

  // Chrome/Edge：用 MediaSource 喂流。已 append 的区间原生 seekable，
  // 缓冲区内任意点击/拖动都不会再被弹回；不支持时走 legacy 直接流+blob 提升。
  var MSE_TYPE = (function () {
    try {
      if (!window.MediaSource) return "";
      if (MediaSource.isTypeSupported("audio/mpeg")) return "audio/mpeg";
      if (MediaSource.isTypeSupported('audio/mpeg; codecs="mp3"'))
        return 'audio/mpeg; codecs="mp3"';
    } catch (e) {}
    return "";
  })();

  function fmtTime(s) {
    s = Math.max(0, Math.floor(s || 0));
    var m = Math.floor(s / 60), ss = s % 60;
    return m + ":" + (ss < 10 ? "0" : "") + ss;
  }

  function bufferedEnd() {
    return player.buffered.length ? player.buffered.end(player.buffered.length - 1) : 0;
  }

  function knownDuration() {
    var d = player.duration;
    return (d && d > 0 && d !== Infinity) ? d : 0;
  }

  // 流式期间 duration 未知，用已缓冲终点作为临时总长
  function totalSpan() {
    if (promoted && knownDuration()) return knownDuration();
    return knownDuration() || bufferedEnd();
  }

  // 本地可跳到的最远位置：MSE/已 append 区间或 blob 全曲
  function localCap() {
    if (promoted) return knownDuration() || bufferedEnd();
    return bufferedEnd();
  }

  function seekTo(t) {
    var cap = localCap();
    t = Math.max(0, Math.min(t, cap));   // 绝不超出本地已有数据
    var wasEnded = player.ended;
    try { player.currentTime = t; } catch (e) {}
    // 已播完时点击进度条：恢复播放（否则 seek 后停在该位置）
    if (wasEnded && t < (knownDuration() || Infinity)) {
      player.play().catch(function () {});
    }
    return player.currentTime;
  }

  function seekToFraction(fr) {
    var total = totalSpan();
    if (!total) return;
    seekTo(Math.min(1, Math.max(0, fr)) * total);
  }

  function fractionFromEvent(el, ev) {
    var r = el.getBoundingClientRect();
    return (ev.clientX - r.left) / r.width;
  }

  on(seekBar, "pointerdown", function (ev) {
    isDragging = true;
    try { seekBar.setPointerCapture(ev.pointerId); } catch (e) {}
    seekToFraction(fractionFromEvent(seekBar, ev));
  });
  on(seekBar, "pointermove", function (ev) {
    if (isDragging) seekToFraction(fractionFromEvent(seekBar, ev));
  });
  on(seekBar, "pointerup", function () { isDragging = false; });
  on(seekBar, "pointercancel", function () { isDragging = false; });

  // 键盘（role=slider）：←/→ 5 秒，Home/End 跳到本地可达端点
  on(seekBar, "keydown", function (ev) {
    var t = player.currentTime || 0, step = 5;
    if (ev.key === "ArrowRight") seekTo(t + step);
    else if (ev.key === "ArrowLeft") seekTo(t - step);
    else if (ev.key === "Home") seekTo(0);
    else if (ev.key === "End") seekTo(localCap());
    else return;
    ev.preventDefault();
  });

  // 点击波形画布同样按本地缓冲跳转
  on(wave, "pointerdown", function (ev) {
    seekToFraction(fractionFromEvent(wave, ev));
  });

  // ---------- 空格键：播放/暂停（除文本输入外，任何焦点位置都由快捷键控制） ----------
  var TEXT_INPUT_TYPES = {
    text: 1, search: 1, url: 1, tel: 1, email: 1,
    password: 1, number: 1, "": 1
  };
  function onSpaceKeydown(ev) {
    if (ev.code !== "Space" || ev.repeat) return;
    var tgt = ev.target, tag = (tgt && tgt.tagName || "").toLowerCase();
    // 焦点在浏览器原生播放器内部时交给原生处理，避免双方各切一次互相抵消
    if (tgt === player) return;
    // 只放行真正的文本编辑场景（只读展示框不算，如 URL 显示框）
    if (tag === "textarea" || (tgt && tgt.isContentEditable)) return;
    if (tag === "input" && !tgt.readOnly &&
        TEXT_INPUT_TYPES.hasOwnProperty((tgt.type || "").toLowerCase())) return;
    if (!player.src) return;
    ev.preventDefault();
    if (player.paused || player.ended) {
      if (player.ended) seekTo(0);
      player.play().catch(function () {});
    } else {
      player.pause();
    }
  }
  on(document, "keydown", onSpaceKeydown);

  // ---------- 合成代码与任务生命周期 ----------
  function buildCode(text) {
    var ka = [];
    if (+rateEl.value !== 0)
      ka.push("rate='" + signedNum(+rateEl.value, "%") + "'");
    if (+volEl.value !== 0)
      ka.push("volume='" + signedNum(+volEl.value, "%") + "'");
    if (+pitchEl.value !== 0)
      ka.push("pitch='" + signedNum(+pitchEl.value, "Hz") + "'");
    if (boundaryEl.value === "WordBoundary")
      ka.push("boundary='WordBoundary'");
    // 与 /await tts('...',voice='...',rate='+50%',response=response) 完全一致
    return "await tts(" + JSON.stringify(text)
      + ",voice=" + JSON.stringify(voiceEl.value)
      + (ka.length ? "," + ka.join(",") : "")
      + ",response=response)";
  }

  // 完整请求 URL 展示框（与实际发往播放器/fetch 的 URL 完全一致）
  function syncUrlBox() {
    try {
      urlBox.value = location.origin + "/" +
        encodeURIComponent(buildCode(textEl.value));
    } catch (e) {}
  }

  function resetEngine() {
    if (engineMode === "mse" && mediaSource) {
      try { if (srcBuf && mediaSource.readyState === "open") srcBuf.abort(); } catch (e) {}
      try { if (mediaSource.readyState !== "closed") mediaSource.endOfStream(); } catch (e) {}
      if (msUrl) { try { URL.revokeObjectURL(msUrl); } catch (e) {} }
    }
    if (blobUrl) { try { URL.revokeObjectURL(blobUrl); } catch (e) {} }
    mediaSource = null; srcBuf = null; msUrl = null;
    blobUrl = null; engineMode = "";
    appendQueue = []; streamDone = false; allChunks = [];
  }

  function startGeneration(text, voice) {
    if (!text || !text.trim()) {
      statusEl.textContent = "请先输入文本";
      return Promise.reject(new Error("empty text"));
    }
    if (voice) voiceEl.value = voice;
    savePrefs();
    generation += 1;
    var gen = generation;
    promoted = false;
    player.pause();
    resetEngine();
    player.removeAttribute("src");
    player.load();
    drawWaveHint("波形生成中…");
    statusEl.textContent = "正在合成、流式传输…";
    player.onloadeddata = function () {
      if (gen === generation && !promoted)
        statusEl.textContent = "已开始播放（边下边播）";
    };
    player.onended = function () {
      if (gen === generation) statusEl.textContent = "播放完成";
    };
    player.onerror = function () {
      if (gen !== generation) return;
      statusEl.textContent = "播放出错（可能是合成失败或网络中断）";
      if (localReject) {
        localReject(new Error("media error"));
        localResolve = null; localReject = null;
      }
    };
    var url = "/" + encodeURIComponent(buildCode(text));
    syncUrlBox();
    whenLocalPromise = new Promise(function (res, rej) {
      localResolve = res; localReject = rej;
    });
    if (MSE_TYPE) startMSE(url, gen);
    else startLegacy(url, gen);
    return whenLocalPromise;
  }

  // ---------- 引擎 A：MediaSource（fetch 流式读 → SourceBuffer 顺序追加） ----------
  function startMSE(url, gen) {
    engineMode = "mse";
    mediaSource = new MediaSource();
    msUrl = URL.createObjectURL(mediaSource);
    player.src = msUrl;
    // 处在点击手势的同步链路里调用 play；有数据后浏览器自然开播
    player.play().catch(function (e) {
      statusEl.textContent = "浏览器拦截了自动播放，请直接点击播放器：" + e.message;
    });
    mediaSource.addEventListener("sourceopen", function onOpen() {
      mediaSource.removeEventListener("sourceopen", onOpen);
      if (gen !== generation) return;
      try {
        srcBuf = mediaSource.addSourceBuffer(MSE_TYPE);
        // mp3 是 generated timestamps 格式，mode 只能用 "sequence"
        srcBuf.mode = "sequence";
        srcBuf.addEventListener("updateend", pumpAppend);
        srcBuf.addEventListener("error", onBufError);
      } catch (e) {
        failLocal(e);
        return;
      }
      streamFetch(url, gen);
    });
  }

  function onBufError() {
    failLocal(new Error("source buffer error"));
  }

  function failLocal(e) {
    if (localReject) {
      localReject(e);
      localResolve = null; localReject = null;
    }
  }

  function streamFetch(url, gen) {
    fetch(url).then(function (resp) {
      if (!resp.ok || !resp.body) throw new Error("bad response " + resp.status);
      var reader = resp.body.getReader();
      function read() {
        reader.read().then(function (res) {
          if (gen !== generation) return;
          if (res.done) { streamDone = true; pumpAppend(); return; }
          allChunks.push(res.value);
          appendQueue.push(res.value);
          pumpAppend();
          read();
        }).catch(failLocal);
      }
      read();
    }).catch(failLocal);
  }

  function pumpAppend() {
    if (!srcBuf || srcBuf.updating) return;
    if (appendQueue.length) {
      var chunk = appendQueue.shift();
      try { srcBuf.appendBuffer(chunk); } catch (e) { failLocal(e); }
      return;
    }
    if (streamDone && mediaSource && mediaSource.readyState === "open") {
      try { mediaSource.endOfStream(); } catch (e) {}
      finishLocal(generation);
    }
  }

  function concatChunks(chunks) {
    var total = 0, i;
    for (i = 0; i < chunks.length; i++) total += chunks[i].byteLength;
    var out = new Uint8Array(total), off = 0;
    for (i = 0; i < chunks.length; i++) {
      out.set(chunks[i], off);
      off += chunks[i].byteLength;
    }
    return out.buffer;
  }

  function finishLocal(gen) {
    if (gen !== generation) return;
    var full = concatChunks(allChunks);
    decodeAndDraw(full, gen);
    statusEl.textContent =
      "已全部缓存到本地：可任意点击/拖动跳转，无需联网（空格可暂停/播放）";
    if (localResolve) {
      localResolve(state());
      localResolve = null; localReject = null;
    }
  }

  // ---------- 引擎 B（兜底）：直接流式播放，全量后 blob 提升 ----------
  function startLegacy(url, gen) {
    engineMode = "legacy";
    player.src = url;
    fetch(url).then(function (resp) { return resp.arrayBuffer(); })
      .then(function (buf) {
        if (gen !== generation) return;
        promoteToLocal(buf, gen);
        decodeAndDraw(buf, gen);
      })
      .catch(failLocal);
    player.play().catch(function (e) {
      statusEl.textContent = "浏览器拦截了自动播放，请直接点击播放器：" + e.message;
    });
  }

  // 全量 mp3 已在内存 → 换成 blob ObjectURL：seekable 覆盖全曲、
  // duration 精确、全程本地零网络请求；保留播放位置与播放状态。
  function promoteToLocal(buf, gen) {
    var url2 = URL.createObjectURL(new Blob([buf], { type: "audio/mpeg" }));
    var wasPlaying = !player.paused && !player.ended;
    var pos = player.currentTime || 0;
    function onMeta() {
      player.removeEventListener("loadedmetadata", onMeta);
      if (gen !== generation) { URL.revokeObjectURL(url2); return; }
      blobUrl = url2;
      promoted = true;
      try { player.currentTime = Math.min(pos, player.duration || pos); } catch (e) {}
      if (wasPlaying) {
        player.play().catch(function () {
          statusEl.textContent = "已切换到本地源，请点播放器播放";
        });
      }
      statusEl.textContent =
        "已全部缓存到本地：可任意点击/拖动跳转，无需联网（空格可暂停/播放）";
      if (localResolve) {
        localResolve(state());
        localResolve = null; localReject = null;
      }
    }
    player.addEventListener("loadedmetadata", onMeta);
    player.src = url2;
  }

  // ---------- 进度条/时间标签刷新 ----------
  function updateSeekUI() {
    var total = totalSpan(), be = bufferedEnd(),
        cur = player.currentTime, d = knownDuration();
    if (total > 0) {
      seekBuffered.style.width = (be / total * 100) + "%";
      seekPlayed.style.width = (Math.min(cur, be) / total * 100) + "%";
    } else {
      seekBuffered.style.width = "0%";
      seekPlayed.style.width = "0%";
    }
    timeCur.textContent = fmtTime(cur);
    timeBuf.textContent = "已缓存 " + fmtTime(be);
    timeDur.textContent = d ? fmtTime(d) : "--:--";
    seekBar.setAttribute("aria-valuenow",
      total > 0 ? Math.round(cur / total * 100) : 0);
  }
  (function seekUILoop() {
    if (!uiAlive) return;
    updateSeekUI();
    requestAnimationFrame(seekUILoop);
  })();

  on(btn, "click", function () {
    startGeneration(textEl.value).catch(function () {});
  });

  // -------- 自动化测试 / 调试接口（window.__qtts） --------
  function ranges(o) {
    var a = [];
    for (var i = 0; i < o.length; i++)
      a.push([+o.start(i).toFixed(3), +o.end(i).toFixed(3)]);
    return a;
  }

  function state() {
    var d = knownDuration();
    return {
      currentTime: +player.currentTime.toFixed(3),
      duration: d ? +d.toFixed(3) : null,
      bufferedEnd: +bufferedEnd().toFixed(3),
      seekable: ranges(player.seekable),
      paused: player.paused,
      ended: player.ended,
      promoted: promoted,
      mode: engineMode,
      src: player.currentSrc || player.src || ""
    };
  }

  window.__qtts = {
    generate: function (text, voice) {
      return startGeneration(text || textEl.value, voice);
    },
    play: function () { return player.play(); },
    pause: function () { player.pause(); },
    toggle: function () {
      if (player.paused || player.ended) return player.play();
      player.pause();
      return Promise.resolve();
    },
    seek: function (t) { seekTo(t); return state(); },
    state: state,
    whenLocal: function () { return whenLocalPromise; },
    mseType: MSE_TYPE
  };

  // 脚本被重复注入执行时，新实例在开头调用它拆除本实例的全部全局监听/循环
  window.__qtts.teardown = function () {
    uiAlive = false;
    while (cleanups.length) {
      try { cleanups.pop()(); } catch (e) {}
    }
    if (rafId !== null) {
      try { cancelAnimationFrame(rafId); } catch (e) {}
      rafId = null;
    }
  };
  window.__qttsTeardown = window.__qtts.teardown;
})();
"""

with gr.Blocks(title="q · 中文语音合成") as demo:
    gr.HTML(TTS_UI)

    with gr.Accordion("ZeroGPU 自检（仅手动点击时申请 GPU）", open=False):
        gr.Markdown("MQTT RPC 和 Edge TTS 都在 CPU 上运行；只有点击这个按钮才会申请 ZeroGPU 并消耗少量每日配额。")
        gpu_test = gr.Button("运行 ZeroGPU 自检")
        gpu_test_result = gr.Textbox(label="自检结果", interactive=False)
        gpu_test.click(zerogpu_self_test, inputs=[], outputs=gpu_test_result)


@asynccontextmanager
async def lifespan(app):
    mqtt_server = MQTTServer(request_topic='q', globals=globals(), server_public_key_bytes=PUBLIC_KEY)
    mqtt_server.mqtt_net.is_windows_cmd = False
    try:
        await asyncio.to_thread(mqtt_server.start, block=False)
        yield
    finally:
        await asyncio.to_thread(mqtt_server.mqtt_net.stop)


# Gradio's own FastAPI server is the only HTTP listener.
# Do not export a top-level `app` or start Uvicorn separately.
server = gr.Server()


@server.get("/health")
def health():
    return {"ok": True}


@server.get("/ui", include_in_schema=False)
def ui_compat():
    return RedirectResponse(url="/")


@server.get("/qtts-ui.js", include_in_schema=False)
def tts_ui_js():
    return Response(
        content=QTTS_JS,
        media_type="text/javascript; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )


# 兼容旧 /rpc/<code> 前缀；新的 RPC 直接在根路径 /<code>（见下面的中间件）。
server.mount("/rpc", WSGIMiddleware(server_http_wsgi.application))


# ---------------------------------------------------------------------------
# 根路径 RPC：/r=1 直接执行，不需要 /rpc 前缀。
# 下面列出的保留路径继续交给 Gradio/UI；其余所有路径都当作 RPC 代码。
# ---------------------------------------------------------------------------
RESERVED_EXACT = {
    "", "/", "/config", "/config/", "/favicon.ico", "/theme.css", "/robots.txt",
    "/manifest.json", "/login", "/login/", "/logout", "/health", "/ui",
    "/docs", "/redoc", "/openapi.json", "/qtts-ui.js",
}
RESERVED_PREFIX = (
    "/gradio_api/", "/_app/", "/static/", "/assets/", "/svelte/", "/theme/",
    "/monitoring", "/pwa_icon", "/proxy=", "/file/", "/rpc/", "/api/",
)


class RootRPCMiddleware:
    def __init__(self, app, rpc_app):
        self.app = app
        self.rpc_app = rpc_app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path") or "/"
        if path in RESERVED_EXACT or any(path.startswith(p) for p in RESERVED_PREFIX):
            return await self.app(scope, receive, send)
        return await self.rpc_app(scope, receive, send)


server.add_middleware(
    RootRPCMiddleware,
    rpc_app=WSGIMiddleware(server_http_wsgi.application),
)


def main():
    port = int(os.environ.get("GRADIO_SERVER_PORT", os.environ.get("PORT", "7860")))
    demo.launch(
        _app=server,
        app_kwargs={"lifespan": lifespan},
        server_name="0.0.0.0",
        server_port=port,
        # HF 注入 GRADIO_SSR_MODE=true；但 Node SSR 代理只转发 /config、
        # /gradio_api/* 等内建路径，会吞掉自定义/根路径 RPC。
        # 显式关闭 SSR，让 Python uvicorn 直接监听 7860。
        ssr_mode=False,
        show_error=True,
        share=False,
    )


if __name__ == "__main__":
    main()
