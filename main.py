"""兼容入口：RunxBuild 服务可能配置为 `python main.py` 启动。

实际应用（Gradio UI + HTTP/MQTT RPC）全部在 app.py，这里直接复用它的
启动逻辑：读取 PORT、绑定 0.0.0.0、ssr_mode=False。
"""

from app import main

if __name__ == "__main__":
    main()
