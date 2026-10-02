# AGENTS.md

> 面向 AI 协作者的上手指南：读完即可独立完成「分析 → 本地验证 → 部署 → 在线验证」全流程。
> 最后更新：2026-10-02。

- **项目**：MQTT/HTTP 双通道 RPC + Edge-TTS 中文语音合成
- **部署目标 A（HuggingFace Space）**：https://huggingface1q-q.hf.space ，仓库 `huggingface1Q/q`（Gradio SDK，硬件 `zero-a10g` ZeroGPU）
- **部署目标 B（RunxBuild）**：https://b0edc4cce.onrunxbuild.com/ ，GitHub 仓库 `qgbcs/_`（**分支 main，push 即自动构建**，私有）
- **本地仓库**：
  - HF 版：`d:\test\github\HuggingFace_Spaces`
  - RunxBuild 版：`d:\test\github\runx`

---

## 1. 项目是什么

一个"把任意 Python 代码片段发过去执行并拿结果"的远程执行服务，有两个入口：

1. **HTTP RPC**：`GET /<代码>` —— 路径本身就是代码。例如 `/r=1` 直接返回 `1`。
2. **MQTT RPC**：客户端把代码以 JSON 消息发到公共 broker 的 request topic `q`，服务端执行后在 reply topic 广播结果。

在 RPC 之上封装了 **Edge-TTS**：浏览器首页是自定义语音面板，点"生成语音"后**根路径 RPC 请求音频流，自动播放，边合成边播**；并带 **canvas 波形图**（替代早期 Gradio 原生 `gr.Audio` 的波形）。

## 2. 运行时架构

```
浏览器 (TTS 面板)
  │  GET /await%20tts('...',voice='...',response=response)
  ▼
HF 网关 ──► uvicorn (Gradio 6 自带 FastAPI, 端口 7860, ssr_mode=False)
             │
             ├─ /、/config、/gradio_api/* …… Gradio 前端
             ├─ /health、/ui、/qtts-ui.js …… FastAPI 自定义路由
             └─ 其余所有路径 ── RootRPCMiddleware ──► a2wsgi WSGIMiddleware
                                                       │ (environ, PEP3333)
                                                       ▼
                                   server_http_wsgi.application
                                       │ 守护线程 + queue（流式）
                                       ▼
                                   RPCRequestHandler.handle_rpc
                                       │
                                       ▼
                                   PythonExecutor.execute(code)
                                       │  持久 REPL 命名空间（进程内单例）
                                       ├─ tts(text,voice,**ka) ─► edge_tts.Communicate
                                       └─ 任意 Python 代码

旁路：lifespan 启动时连接 5 个公共 MQTT broker（MQTTServer, topic q）
      MQTT 消息 ─► 同一个 PythonExecutor 执行 ─► reply topic 广播结果
```

关键事实：

- **只有一个 HTTP 监听者**：Gradio 自己的 FastAPI server。不要导出顶层 `app`，不要另起 uvicorn，不要放 Dockerfile。
- **RPC 持久命名空间是进程内单例**：import、全局变量、def 在请求之间一直保留 → 必须单 worker。
- HTTP 与 MQTT 共用同一套 `PythonExecutor` 语义。

## 3. 目录与关键文件

| 路径 | 作用 |
|---|---|
| `app.py` | Space 入口：参数规范化、`tts()`/`synthesize()`、自定义 UI、FastAPI 路由、根路径 RPC 中间件、`demo.launch()` |
| `requirements.txt` | Space 依赖（gradio 6.28 / edge-tts 7.x / a2wsgi / paho-mqtt 等；**不含 torch**，ZeroGPU worker 自带） |
| `README.md` | HF Space 元数据（**YAML frontmatter 必须保留**）+ 运维说明 |
| `multi_mqtt/rpc_executor.py` | 与传输无关的 `PythonExecutor`：REPL 语义、顶层 await、协程调度 |
| `multi_mqtt/server_http.py` | 独立 BaseHTTP RPC 服务器 + `RPCRequestHandler.handle_rpc`（HTTP 下 RPC 语义的唯一权威实现） |
| `multi_mqtt/server_http_wsgi.py` | 把 `handle_rpc` 包成**流式 WSGI 应用**（守护线程 + queue），供 a2wsgi 挂载 |
| `multi_mqtt/server_mqtt.py` | MQTT 服务端 `MQTTServer` |
| `multi_mqtt/client_mqtt.py` | MQTT 客户端，`rpc()` 便捷函数 |
| `multi_mqtt/multi_mqtt.py` | MQTT 网络层：多 broker、签名/验签、TTLCache 去重、广播 |
| `multi_mqtt/tests/` | executor / http / mqtt 客户端测试 |
| `mqtt_non_json.patch` | 过滤公共 broker 上非 JSON 空载荷的补丁（按需） |

## 4. RPC 执行器语义（最重要的心智模型）

`PythonExecutor`（[multi_mqtt/rpc_executor.py](multi_mqtt/rpc_executor.py)）：

- `globals_dict` 与 `locals_dict` 是**同一个字典**（CPython `exec` 语义要求；分离会导致 exec 里定义的函数看不到模块级名字）。
- **REPL 语义**：最后一条语句若为裸表达式，求值并作为结果；否则取变量 `r`。
- **顶层 await**：自动包成 `async def __rpc_async__()` 执行；单表达式用 `return (expr)`，多语句执行后把局部变量合并回命名空间。
- **协程运行方式**：若提供了正在运行的 `main_loop` 则 `run_coroutine_threadsafe` 投递过去；否则在当前线程新建 event loop（并设为当前 loop，结束后关闭还原）。
- 请求级上下文变量（`request/q/response/p`）只在本次执行期间注入，执行完自动还原，不污染持久命名空间。
- 执行被 RLock 保护；捕获 `Exception` 和 `SystemExit`，错误以 traceback 字符串返回。

结果优先级（HTTP，见 `handle_rpc`）：

1. `response.data`（`set_data` 设置的）
2. 持久命名空间里的 `r`
3. 本次 stdout
4. 都没有 → 提示 `no 'r' variable ...`

> 注意：`r` 是持久的。连续发 `r='a'` 再发一个不赋值的表达式，第二个请求仍会回显旧 `r`，这是设计行为不是 bug。

## 5. HTTP RPC 调用手册

### 5.1 URL 即代码

- 独立服务器：路径是原始 percent-encoded 请求行，`handle_rpc` 负责 `unquote`（一次）。
- WSGI（线上）：网关已按 PEP 3333 处理，shim 用 `latin-1` 还原字节再按 UTF-8 解码，并设置 `path_already_decoded=True`，**绝不能再 unquote**。

保留给 Gradio/FastAPI 的路径（不进 RPC）见 `app.py`：

- 精确：`/`、`/config`、`/health`、`/ui`、`/favicon.ico`、`/manifest.json`、`/qtts-ui.js`、`/docs` 等
- 前缀：`/gradio_api/`、`/_app/`、`/static/`、`/assets/`、`/svelte/`、`/theme/`、`/file/`、`/rpc/`、`/api/` 等

其余一切路径都按 RPC 代码执行。旧前缀 `/rpc/<code>` 仍然保留。

### 5.2 curl 示例（Windows 下必须用 `curl.exe`，`curl` 是 PowerShell 别名）

```powershell
# 简单表达式
curl.exe -sS "https://huggingface1q-q.hf.space/r=1"                       # -> 1

# 中文：先 percent-encode
$code = "r='你好世界'"
$url  = "https://huggingface1q-q.hf.space/" + [uri]::EscapeDataString($code)
curl.exe -sS $url

# 异步
$code = "await tts('你好',voice='zh-CN-YunxiNeural',response=response)"
```

### 5.3 `response`（别名 `p`）流式 API

RPC 代码内可直接用请求级对象 `response`：

| 方法/属性 | 作用 |
|---|---|
| `set_status(code)` | 设置 HTTP 状态码（提交前有效） |
| `set_header(key, value)` | 设置响应头 |
| `set_data(data)` | 一次性返回整个 body（旧行为） |
| `write(data)` | **流式**：首次调用立即发送状态/响应头（无 Content-Length），后续写入直接发字节 |

TTS 就是流式范式：先 `set_header('Content-Type','audio/mpeg')`，edge-tts 每来一个音频块就 `response.write(data)`。配合网关 chunked 传输，浏览器可在合成完成前开播。

### 5.4 HTTP Range（206）与 TTS 音频缓存

**服务端能力（已实现，勿删）**：

- 流式响应首次提交即带 `Accept-Ranges: bytes`（见 `server_http.py` 的 `ResponseWrapper.write`）。
- `server_http_wsgi.application` 识别 `Range: bytes=...` 请求头：完整执行一次 RPC 收集全量响应后，返回 **206 Partial Content**（`Content-Range: bytes start-end/total`、正确的 Content-Length），支持 `100-199`、`4900-`、后缀 `-50`；越界 → 416。
- **例外**：`bytes=0-` 是浏览器媒体加载器的初始打开式请求，仍走流式 200 路径（保证点击后立刻边合成边播），不返回 206。

**TTS LRU 缓存**（`app.py`：`_TTS_CACHE`，64 条，线程安全）：

- 缓存键：`(fmt, text, voice, rate, volume, pitch)`——只有这些影响音频内容；boundary/proxy/connector 不进键。
- 首次合成完成写入缓存；之后相同请求（包括 Range 切片）直接从内存返回，**不重新调用 edge-tts**。
- 这是"seek 不卡顿、不重复合成"的关键。

### 5.5 鉴权

`start_rpc_server(key=...)` 可设置路径前缀密钥（`/<key>/<code>`）；当前线上 `key=''`（免鉴权，靠 HF Space 私有性/网络层控制）。修改时同时检查 `handle_rpc` 的前缀校验。

## 6. TTS API 手册

### 6.1 函数签名（`app.py`）

```python
async def tts(text: str,
              voice: str = 'zh-CN-YunxiNeural',
              fmt="mp3",
              response=None,
              **ka) -> bytes

async def synthesize(text: str, voice: str, **ka) -> str   # 返回临时 mp3 文件路径（Gradio 兼容）
```

`tts` 返回完整 mp3 字节；当传入 `response` 时同时流式写出。两者在导入时被显式注入 HTTP RPC 命名空间：

```python
server_http_wsgi.RPCRequestHandler.executor.globals_dict.update(
    {"tts": tts, "synthesize": synthesize})
```

> 不注入的后果：`/await tts(...)` 报 `NameError: name 'tts' is not defined`（HTTP RPC 命名空间默认只是 `server_http_wsgi` 的模块 globals）。

### 6.2 支持的 edge-tts 全参数（`**ka`）

`rate / volume / pitch / boundary / proxy / connector / connect_timeout / receive_timeout`，其他参数 → `TypeError`。对应 `edge_tts.Communicate(text, voice, *, rate='+0%', volume='+0%', pitch='+0Hz', boundary='SentenceBoundary', connector=None, proxy=None, connect_timeout=10, receive_timeout=60)`。

### 6.3 参数规范化（手动测 rate 出错的原因与修法）

edge-tts 的正则只接受**带符号**格式：`+30%`、`-20Hz`。`app.py` 统一容错：

| 输入 | 规范化结果 |
|---|---|
| `rate=30`（int/float） | `rate='+30%'` |
| `rate='50%'`、`rate='-10'` | `'+50%'`、`'-10%'` |
| `pitch=-30`、`pitch='-20hz'` | `'-30Hz'`、`'-20Hz'` |
| pitch 单位 | 支持 `Hz` 与 `st`（半音） |
| `boundary` | 必须是 `'WordBoundary'` / `'SentenceBoundary'` |
| bool | 一律拒绝 |

### 6.4 常用发音人

| voice | 说明 |
|---|---|
| `zh-CN-YunxiNeural` | 云希，阳光少年（默认） |
| `zh-CN-XiaoxiaoNeural` | 晓晓，温暖女声 |
| `zh-CN-YunjianNeural` | 云健，体育男声 |
| `zh-CN-XiaoyiNeural` | 晓艺，活泼少女 |
| `zh-CN-YunxiaNeural` | 云夏，卡通男声 |
| `zh-CN-YunyangNeural` | 云扬，新闻男声 |
| `zh-CN-liaoning-XiaobeiNeural` / `zh-CN-shaanxi-XiaoniNeural` | 东北话 / 陕西话 |
| `zh-HK-HiuGaaiNeural` / `zh-HK-HiuMaanNeural` / `zh-HK-WanLungNeural` | 粤语 |
| `zh-TW-HsiaoChenNeural` / `zh-TW-YunJheNeural` 等 | 台普 |

## 7. MQTT RPC 调用手册

- request topic：`q`；reply topic：默认广播 topic。客户端同时投递 **5 个公共 broker**，服务端只处理最先到达的（TTLCache 去重），响应同样广播，客户端接收首胜结果。
- 回包 JSON：`{req_id, r, stdout, ok, server_time, server_from}`（无 `code` 字段，不会被当成新命令）。

客户端：

```python
from pathlib import Path
from multi_mqtt import client_mqtt          # 或 sys.path 指向 multi_mqtt/

result = client_mqtt.rpc(
    "import platform,sys; (sys.executable, platform.node(), platform.machine())",
    request_topic="q",
    timeout=15,
    client_private_key_bytes=Path("client-private.pem").read_bytes(),  # 服务端启用验签时必需
)
print(result)
```

签名/安全模型（详见 [multi_mqtt/ReadMe.md](multi_mqtt/ReadMe.md)）：

- 客户端用自己的**私钥**对 `base_req_id|code|timestamp` 签名；服务端用配置的**公钥**验签——是不同侧的密钥材料。
- 私钥模式下，若未知服务端公钥且未显式 `allow_no_server_pubkey_response=True`，客户端默认**丢弃回包**。
- `k=2333+1234` 是合法私钥（`k` 是 private_key 别名），

服务端在 `app.py` 的 `lifespan` 中启动：`MQTTServer(request_topic='q', globals=globals(), server_public_key_bytes=PUBLIC_KEY)`，随 FastAPI 生命周期启停。

## 8. Web UI 工作原理

`TTS_UI`（`app.py` 内的 HTML 字符串）包含：文本框、发音人下拉、语速/音量/音调滑块、边界事件选择、生成按钮、`<canvas id="qtts-wave">`、自定义进度条 `#qtts-seek`（缓冲层+播放层）、时间标签、`<audio id="qtts-player" controls autoplay>`。

**两个必须知道的坑（都已处理）**：

1. **Gradio 6 的 `gr.HTML` 用 innerHTML 渲染，内嵌 `<script>` 根本不执行。**
   解法：HTML 末尾放
   `<img src="data:text/plain,boot" onerror="...动态创建 script，src=/qtts-ui.js...">`
   动态插入的外部 script 正常执行。
2. 脚本本体由 FastAPI 路由 `GET /qtts-ui.js` 提供（`Cache-Control: no-cache`），内容为 `QTTS_JS`，且该路径在 RPC 保留集合里。

JS 行为：

- 滑块联动实时标签（`+30%` / `-20Hz`）。
- 点击生成：拼出
  `await tts(<JSON文本>,voice=<JSON发音人>[,rate='+30%'...],response=response)`
  （非默认参数才出现），`"/"+encodeURIComponent(code)` 设为 `player.src`，并 `player.play()`（点击是用户手势，允许自动播放）。
- **波形**：同时 `fetch(url) → arrayBuffer → AudioContext.decodeAudioData → 分桶峰值 → canvas 绘制**；rAF 循环根据 `currentTime/duration` 把已播放部分染橙，未播放为灰色；支持 devicePixelRatio 与窗口缩放重算。波形在整段下载解码后画出，不影响音频先行开播。
- **本地缓冲内跳转（不发网络请求）**：`#qtts-seek`（`role="slider"`）支持 pointer 点击与拖动、键盘（←/→ ±5s、Home/End）；点击波形画布也可跳转。
  - **根因（已实测）**：chunked 无 Content-Length 的 mp3 即使全部缓冲完，媒体引擎仍报 `seekable=[0,0]`（实测 duration 已知也是如此），直接设 `currentTime` 会被拒绝 → "圆点立马弹回"。
  - **现行修法（Chrome/Edge）= MediaSource 流式引擎**：`startMSE()` 建 `MediaSource`，`fetch(url)` 以 ReadableStream 读响应，每个 mp3 块按序 `appendBuffer()` 到 SourceBuffer（mp3 是 generated timestamps，`mode` 只能是 `"sequence"`）。**已 append 的区间原生 seekable**——流式阶段就能在已缓存范围内任意点/拖，不再弹回；无需等全量、无需换源。
  - **legacy 兜底引擎**：浏览器不支持 MSE（Firefox/Safari）时，`player.src` 直接走流式；全量 fetch 拿到 arrayBuffer 后 `promoteToLocal()` 建 blob ObjectURL 切源（保留 currentTime 与播放状态），blob 源 seekable 覆盖全曲、duration 精确。
  - seek 一律夹到 `localCap()`（MSE=`bufferedEnd()`；blob=全曲），**物理上不可能发网络请求**；已结束状态下 seek 自动恢复播放。
  - 缓冲层（浅灰）/播放层（橙）由独立 rAF 循环实时刷新并同步 `aria-valuenow`。
- **时间显示三段式**：当前播放时间 / `已缓存 mm:ss`（此范围内可随意跳转）/ 总时长；流式期间总时长先取已缓冲终点。
- **空格键播放/暂停**：全局 keydown（命名函数 `onSpaceKeydown`），**仅文本编辑元素**（textarea/contenteditable/文本类 input；只读框不算）放行空格，其余任何焦点位置（range 滑块、select、按钮、`#qtts-seek`）都由快捷键拦截切换；焦点在原生 `<audio>` 内部时**不拦截、交给原生**（否则双方各切一次互相抵消，表现为"按空格无效"）；已结束时空格从头播放。
  - **脚本必须幂等**：`QTTS_JS` 所有持久监听一律走 `on(target,type,fn)`（记入 `window.__qttsCleanups`），rAF 循环检查 `uiAlive`；IIFE 开头先调用旧实例 `window.__qttsTeardown()` 再重绑。否则部署重连/Gradio 重挂载时 `img onerror` 会再次注入脚本，document 上叠加两个空格监听器，同一次按键被切换两次（暂停→立刻恢复），表现为"选中滑块按空格无效"。
- **请求 URL 显示框**：生成按钮下方 `#qtts-url`（只读、占满面板宽度、点击全选可复制）；点击生成时填入与实际请求完全一致的完整 URL（`location.origin + "/" + encodeURIComponent(code)`），首次生成后随文本/参数改动实时刷新。
- **localStorage 持久化**：键 `qtts-prefs-v1`，保存文本、发音人、语速/音量/音调、边界事件；刷新/重开页面自动恢复（含滑块标签），输入/改动即存。
- **自动化测试接口 `window.__qtts`**：`generate(text,voice)`（返回 Promise，全量缓存完成时 resolve）、`play()/pause()/toggle()`、`seek(秒)`、`state()`（currentTime/duration/bufferedEnd/seekable/paused/ended/promoted/mode/src）、`whenLocal()`、`mseType`。在页面控制台或 `browser_evaluate` 里调用即可全自动验证，无需手点。
- HTTP Range 206（见 §5.4）代码保留不动，作为直连/未提升场景的兜底；常规 UI 跳转不再走网络。

## 9. 本地开发环境（Windows）

环境工具**不在 PATH**，固定路径：

| 工具 | 路径 |
|---|---|
| Git | `C:\QGB\PortableGit\bin\git.exe`（其 push **不支持** `--config http.proxy=...` 参数写法，要用 `-c`） |
| Python | `C:\QGB\anaconda3\python.exe`（3.12） |
| 网络代理 | socks5：`socks5h://127.0.0.1:41080` |
| HTTP 客户端 | 用 `curl.exe`（不要用 `curl` 别名） |

本地编译检查：

```powershell
& "C:\QGB\anaconda3\python.exe" -m py_compile app.py multi_mqtt\server_http.py multi_mqtt\server_http_wsgi.py
```

本地直接驱动 WSGI（无需起端口，验证中文解码/流式）：

```python
import sys; sys.path.insert(0, "multi_mqtt")
import server_http_wsgi
code = "r='你好世界，一二三四五'"
pi   = "/" + code.encode("utf-8").decode("latin-1")   # 模拟 PEP3333 的 PATH_INFO
env  = {"REQUEST_METHOD": "GET", "PATH_INFO": pi, "QUERY_STRING": "", "REMOTE_ADDR": "127.0.0.1"}
print(b"".join(server_http_wsgi.application(env, lambda s,h: print(s))).decode())
```

## 10. 部署到 HuggingFace Space

### 10.1 Space 元数据（`README.md` frontmatter）

```yaml
sdk: gradio
sdk_version: "6.28.0"
python_version: "3.12.12"
app_file: app.py
```

不要改成 `sdk: docker`，不要放 Dockerfile。依赖由根目录 `requirements.txt` 安装。

### 10.2 ZeroGPU 硬性要求

硬件是 `zero-a10g`：启动期检测不到任何 `@spaces.GPU` 回调会直接 `RUNTIME_ERROR: No @spaces.GPU function detected during startup` → 所有 URL 返回 503。

- 保留 `app.py` 里折叠面板中的 `zerogpu_self_test()`（`@spaces.GPU(duration=15)`），仅手动点击时消耗配额。
- **不要**把 TTS/RPC 包进 `@spaces.GPU`（纯 CPU 逻辑，且会无谓消耗配额）。

### 10.3 必须 `ssr_mode=False`

HF 注入 `GRADIO_SSR_MODE=true`，Node SSR 代理只转发 `/config`、`/gradio_api/*` 等内建路径，自定义/根路径 RPC 会被 SvelteKit 吞掉。`demo.launch(..., ssr_mode=False)` 后由 Python uvicorn 直监听 `7860`（端口取 `GRADIO_SERVER_PORT`/`PORT`）。

### 10.4 提交与推送

```powershell
# 只暂存本次改的具体文件，不要 git add -A
& "C:\QGB\PortableGit\bin\git.exe" add app.py multi_mqtt/server_http.py multi_mqtt/server_http_wsgi.py
& "C:\QGB\PortableGit\bin\git.exe" commit -m "fix: ..."
& "C:\QGB\PortableGit\bin\git.exe" -c http.proxy=socks5h://127.0.0.1:41080 push origin main
```

remote `origin` 已在本地配置（URL 内嵌了写 token，**仅限本机，勿输出、勿提交**）。

### 10.5 轮询部署状态

```powershell
$token = "hf_xxx"   # 不要写进任何被提交的文件
curl.exe -sS --proxy socks5h://127.0.0.1:41080 `
  -H "Authorization: Bearer $token" `
  "https://huggingface.co/api/spaces/huggingface1Q/q"
# 看 runtime.stage：…BUILDING → APP_STARTING → RUNNING；以及 sha 是否为本次提交
# 日志（SSE）：.../logs/run 与 .../logs/build
```

典型一次部署 1–3 分钟。

### 10.6 RunxBuild 部署（目标 B）

平台事实（文档：https://www.runxbuild.com/docs/services/python/ ）：

- 自动执行构建命令 `pip install -r requirements.txt`；**应用必须监听 `PORT` 环境变量**并绑定 `0.0.0.0`。
- 每次 push 到部署分支（GitHub `qgbcs/_` 的 **main 分支**；本地 runx 仓库在 master 上，推送用 `master:main`）自动触发构建+滚动部署。
- `app.py` 的 `main()` 已满足要求：`server_port` 取 `GRADIO_SERVER_PORT`/`PORT`，`server_name="0.0.0.0"`；`ssr_mode=False` 在非 HF 环境同样生效。
- 与 HF 的差异：
  - HF 镜像预装 `spaces`；RunxBuild 靠 requirements 安装（已加 `spaces>=0.30`）。`@spaces.GPU` 在非 ZeroGPU 环境是透传 no-op，自检按钮在无 CUDA 机器上会报错——不要点。
  - 无 ZeroGPU 启动检测，不会因缺 GPU 回调而 503。
  - 已从 requirements 移除 torch（不再装任何 nvidia CUDA 包），构建快且不会 OOM。

本地操作（在 `d:\test\github\runx`）：

```powershell
# 克隆（空仓库也可克隆；token 仅用于本次，推送后立即从 remote URL 抹除）
& "C:\QGB\PortableGit\bin\git.exe" -c http.proxy=socks5h://127.0.0.1:41080 `
  clone "https://qgbcs:<TOKEN>@github.com/qgbcs/_.git" "D:\test\github\runx"
# 拷贝代码（app.py、requirements.txt、README.md、AGENTS.md、.gitignore、multi_mqtt/）后：
git add <具体文件>; git commit -m "..."; git -c http.proxy=socks5h://127.0.0.1:41080 push origin master
git remote set-url origin "https://github.com/qgbcs/_.git"   # 抹除 config 里的 token
```

按用户约定：**push 后即停止，不等待/不验证构建**（用户自行检测）。

## 11. 上线后验证清单

按顺序执行，任一不过先查 §12：

1. `curl.exe -sS "<space>/health"` → 200
2. `curl.exe -sS "<space>/r=1"` → `1`（根路径 RPC 通）
3. 中文 echo：`r='你好世界，一二三四五'` 编码后请求 → 原文返回（验证不是 `ä½ å¥½` 乱码）
4. TTS：`/await tts('苹果香蕉西瓜葡萄,语音内容验证',voice='zh-CN-YunxiNeural',response=response)`
   - HTTP 200，`Content-Type: audio/mpeg`，`Transfer-Encoding: chunked`
   - MP3 魔数：开头为 `FF F3`/`FF FB`（帧头）或 `ID3`
   - 时长与输入匹配（中文约 3–4 字/秒；若为乱码会显著更长）
5. 流式速度：`curl.exe -w "TTFB=%{time_starttransfer}s total=%{time_total}s"`；长文本应 TTFB ≪ total（实测 3.2s vs 18.4s）
6. 浏览器实测：点击生成 → status 变化、`paused=false` 自动播放、canvas 波形画出且已播放段染橙。

MP3 时长测量（本机无 mutagen 时）：`python -m pip install mutagen --proxy socks5h://127.0.0.1:41080`，
`mutagen.mp3.MP3(path).info.length`。

## 12. 故障排查表（均为真实踩过的坑）

| 现象 | 根因 | 处理 |
|---|---|---|
| 全部 URL 503，日志 `No @spaces.GPU function detected` | ZeroGPU 启动期检测不到 GPU 回调 | 保留 `@spaces.GPU` 自检回调；勿删 |
| 自定义路由返回 HTML/被吞 | SSR Node 代理只转发内建路径 | `ssr_mode=False`，只保留一个 `demo.launch()` |
| 音频读的不是输入文字（`ä½ å¥½` 式乱码朗读） | WSGI PATH_INFO 按 PEP3333 用 latin-1 承载字节，被直接当文本，且又被 unquote | shim 中 `encode('latin-1').decode('utf-8')` + `path_already_decoded` 跳过二次 unquote |
| 点按钮完全无反应、无网络请求 | Gradio 6 `gr.HTML` 内嵌 `<script>` 不执行 | img `onerror` 引导加载 `/qtts-ui.js` |
| 选中滑块按空格"无效"（新标签页正常，旧标签页不行） | 部署重连/Gradio 重挂载后脚本被再次注入，document 叠加多个空格监听器，一次按键被切换两次互相抵消 | `QTTS_JS` 幂等：旧实例 `__qttsTeardown()` 拆除全部监听/rAF 后再重绑（`on()` 统一登记，`uiAlive` 守卫 rAF） |
| 手动测 rate 报错 | edge-tts 只接受带符号 `+30%` | 用规范化函数；int 直接可用 |
| `/await tts(...)` → NameError tts | RPC 持久命名空间不含 app.py 的函数 | 启动时 `executor.globals_dict.update(...)` 注入 |
| MQTT 请求超时无回包 | 服务端验签开启但客户端未用匹配私钥 | 用与 PUBLIC_KEY 匹配的真实私钥 |
| 播放中拖动进度条不跳转/要暂停才能跳 | chunked 流无 Content-Length，原生时间轴无法定位未缓冲位置 | 自定义 `#qtts-seek`：只在本地缓冲范围内 seek，夹到 bufferedEnd，不发网络请求；未缓冲位置由服务端 Range 206 兜底 |
| RunxBuild 启动即 `ModuleNotFoundError: spaces` | 平台不像 HF 预装 spaces | requirements.txt 已加 `spaces>=0.30` |
| RunxBuild 构建 `OOMKilled; exitCode=137`（`Taking snapshot of full filesystem` 阶段） | `torch` 拉入整套 nvidia CUDA 包（cu13、triton、cuda-toolkit，解压后数 GB），构建机内存不足 | requirements.txt 移除 torch：它只在 `zerogpu_self_test()` 里惰性 import，ZeroGPU worker 运行时自带 torch，主进程不需要 |
| RunxBuild 服务无响应/健康检查失败 | 未监听平台给定 PORT | 确认用 `python app.py`（main 读取 PORT 绑 0.0.0.0），勿硬编码端口 |
| `address already in use` | 多个监听器/launch | 只允许 Gradio 一个监听者，删 Dockerfile/额外启动 |
| 发布即旧界面 | 浏览器缓存 | URL 加 `?v=时间戳`、DevTools Disable cache |
| `Expecting value: line 1 column 1` | 公共 broker 上的空/非 JSON 消息 | 参考 `mqtt_non_json.patch` |

## 13. 提交/部署演进（脉络）

```
64c081f 初始迁移到 Space → RUNTIME_ERROR（缺 @spaces.GPU）
b882706 加 ZeroGPU 自检回调
a54dc18 ssr_mode=False，自定义路由可达
4eba33e 根路径 RPC（/r=1）+ edge-tts 参数规范化 + 流式音频 UI
2ce1e6d 修复：tts/synthesize 注入 HTTP RPC 命名空间
9503b5e 修复：gr.HTML 脚本不执行 → img 引导 /qtts-ui.js
2996d07 修复：WSGI latin-1→utf-8（音频朗读乱码）+ 恢复 canvas 波形图
73cafd7 HTTP Range 206 + TTS LRU 缓存（播放中可 seek）
（本地）自定义 #qtts-seek：缓冲区内本地跳转零网络请求；AGENTS.md 补 Range/缓存；代码迁移 RunxBuild（qgbcs/_，master 本地、main 远端自动构建）
（本地）MediaSource 流式引擎：已缓存区间原生 seekable，seek 弹回根治；legacy 引擎保留 blob 提升；空格播放/暂停；三段时间（含已缓存）；localStorage 持久化文本与全部设置；移除 torch 修复 runx 构建 OOMKilled
（本地）空格快捷键修正：仅文本编辑元素放行（修复滑块聚焦时空格无效；原生 audio 焦点交原生避免双切换）；生成按钮下新增全宽只读 URL 显示框（生成时填充、参数实时刷新、点击全选）；multi_mqtt 以 D:\test\multi_mqtt 为唯一上游同步到两个仓库
（本地）根治"旧标签页滑块空格无效"：脚本重注入导致 document 叠加多个空格监听器→一次按键双切换抵消；QTTS_JS 改为幂等（on() 登记+__qttsTeardown 拆除+uiAlive 守卫 rAF），Playwright 真实鼠标点击滑块+真实空格按键验证
```

## 14. 安全红线

- 不提交：HF token、GitHub PAT、`.env`、客户端私钥、密码；Secrets 走平台环境变量/Space **Settings → Variables and secrets**。
- 本地 remote URL 内嵌 token：不要打印、不要写进文档/截图；用完立即 `git remote set-url` 抹除。
- 修改 RPC 暴露面（免鉴权执行任意代码）时，意识到该服务等价于公开 REPL；靠 Space/仓库隐私设置与网络层控制访问。
- 免费 Space/RunxBuild 有休眠、重启和配额限制，不承诺 MQTT 长连接/SLA。
