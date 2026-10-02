---
title: MQTT RPC + 中文语音合成
emoji: 🚀
colorFrom: indigo
colorTo: blue
sdk: gradio
sdk_version: "6.28.0"
python_version: "3.12.12"
app_file: app.py
pinned: false
---

# MQTT RPC + 中文语音合成

## 当前 503 的原因

当前 Space API 状态显示：`RUNTIME_ERROR`、硬件 `zero-a10g`，错误为 `No @spaces.GPU function detected during startup`。ZeroGPU 只支持 Gradio SDK，并要求应用注册至少一个 `@spaces.GPU` Gradio 回调。纯 MQTT/TTS 代码不调用 GPU，所以原应用被 ZeroGPU 拒绝，外部访问才会返回 503。

本版在语音页面提供了可选的 **ZeroGPU 自检** 按钮。MQTT RPC 和 Edge TTS 仍在 CPU 执行；只有手动点击自检按钮才会运行一个小型 CUDA 张量测试、申请 ZeroGPU 配额。无需也不要把所有 TTS/RPC 请求包进 `@spaces.GPU`。

Free 个人账号在满足资格时可使用有限的 ZeroGPU 配额；ZeroGPU 免费配额不是常驻 CPU 服务。如果账户不能把这个 Space 改为 CPU Basic，不要尝试选择付费升级。要停止一个当前正在计费的付费硬件 Space，请在 Space 页面点击 **Pause**。暂停后需手动恢复。

## 配置与文件

README 顶部保留 `sdk: gradio`、`sdk_version`、`python_version` 和 `app_file`。不要设置 `sdk: docker`，也不要在 Gradio SDK Space 中保留 Dockerfile。

把 `app.py`、`requirements.txt`、`.gitignore` 和完整的 `multi_mqtt/` 目录放在仓库根目录。不要提交 `.env`、私钥、访问令牌或密码；Secrets 应放入 **Settings → Variables and secrets**。

## 端口、SSR 与测试地址

`app.py` 在 `demo.launch()` 中显式设置 `ssr_mode=False`：HF 运行环境默认注入 `GRADIO_SSR_MODE=true`，日志会显示 `with SSR (Node proxy -> Python :7861)`，但 Node SSR 代理只把 `/config`、`/gradio_api/*` 等 Gradio 内建路径转发给 Python，自定义顶层路由（`/health`、`/ui`、`/rpc`）会被 SvelteKit 页面吞掉（返回 HTML 而不是 JSON/重定向）。关闭 SSR 后由 Python uvicorn 直接监听 Space 端口 `7860`，自定义路由才可达。仍然只保留一个 `demo.launch()`，不要额外启动 Uvicorn。

- Gradio UI 首页：`https://<用户名>-<space名>.hf.space/`
- `/ui`：兼容旧 URL，重定向到 `/`
- `/health`：FastAPI 健康检查
- `/rpc/<code>`：原有 WSGI HTTP RPC
- MQTT RPC：request topic `q`，仍走 MQTT，不是 HTTP `/rpc`

先确认 Space runtime 不再是 `RUNTIME_ERROR`，再测试 `curl -i https://<space>.hf.space/health`。Space 处于 Error 时所有 URL 都会得到 503。

## MQTT 客户端超时：检查签名参数

`multi_mqtt/client_mqtt.py` 中，`k` 是 `private_key` 的别名，不是发给 RPC 代码的变量。`k=2**128` 不是有效私钥；客户端会退回发送未签名请求，而服务端已经配置公钥并启用验签，于是丢弃请求、不发回包，最终超时。`a=1` 是 `allow_no_server_pubkey_response`，只影响客户端接受回包的策略，不会关闭服务端对请求的验签。

你的服务端配置了 `PUBLIC_KEY` 时，客户端必须用与该公钥对应的真实私钥签名：

```python
from pathlib import Path
import client_mqtt

client_private_key = Path("client-private.pem").read_bytes()
result = client_mqtt.rpc(
    "import platform, sys; (sys.executable, platform.node(), platform.machine(), platform.release())",
    request_topic="q",
    timeout=15,
    client_private_key_bytes=client_private_key,
)
print(result)
```

不要把客户端私钥提交到 Space/GitHub；上面是客户端本机读取私钥的示例。如果没有与服务端公钥匹配的私钥，生成一对匹配密钥并更新对应端，或在明确接受关闭验签风险后移除服务端公钥。不要用 `k=2**128` 代替密钥。

## 日志说明

- `No @spaces.GPU function detected during startup`：选择的是 ZeroGPU，但代码没有 GPU 回调；本版自检按钮提供实际的按需 GPU 回调。
- `address already in use`：只保留本文件 `app.py` 中的一个 `demo.launch()`；删除 Dockerfile/额外启动命令。本 Space 用 `ssr_mode=False`，由 Python 直接监听 `7860`，不经过 Node 代理。
- `Expecting value: line 1 column 1`：公共 MQTT broker 收到空载荷或非 JSON 消息，与 HTTP 端口无关。需要过滤时，可应用 `mqtt_non_json.patch`。

ZeroGPU 和免费 Space 有额度、休眠与重启限制；不保证 MQTT 长连接或生产 RPC SLA。