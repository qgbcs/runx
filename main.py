import os
import sys
from pathlib import Path



sys.path.insert(0, str(Path(__file__).resolve().parent / "multi_mqtt"))

from server_http import RPCRequestHandler, ThreadedHTTPServer, start_rpc_server
from server_mqtt import MQTTServer


async def tts(text: str, voice: str = "zh-CN-XiaoxiaoNeural", fmt: str = "mp3", response=None) -> bytes:
    import edge_tts
    buf = b""
    comm = edge_tts.Communicate(text, voice=voice)
    async for chunk in comm.stream():
        if chunk["type"] == "audio":
            buf += chunk["data"]
    if response is not None:
        response.set_header("Content-Type", "audio/mpeg")
        response.set_data(buf)
    return buf


class RunxRequestHandler(RPCRequestHandler):
    def do_GET(self):
        if self.path.split("?", 1)[0] == "/health":
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


def main():
    global ghs,gms
    host = "0.0.0.0"
    port = int(os.environ.get("PORT", "3001"))
    ghs=start_rpc_server(port=port, ip=host, globals=globals(), locals=locals(), listen=False)

    public_key = b"ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBER9c5vu215n+5gv1YjGdm78Nf99wpfqw1fIT8nXib2FLUglq4NBMe7hLp2VOkqv9z00m5Wn+uUADH4zyXLiWzI="
    gms = MQTTServer(globals=globals(), server_public_key_bytes=public_key)
    gms.mqtt_net.is_windows_cmd = False
    
    http_server = ThreadedHTTPServer((host, port), RunxRequestHandler)
    gms.start(block=False)
    try:
        http_server.serve_forever()
    finally:
        http_server.server_close()
        gms.mqtt_net.stop()


if __name__ == "__main__":
    main()