import os
import sys
from pathlib import Path



sys.path.insert(0, str(Path(__file__).resolve().parent / "multi_mqtt"))

from server_http import RPCRequestHandler, ThreadedHTTPServer, start_rpc_server
from server_mqtt import MQTTServer


async def tts(text: str, voice: str ='zh-CN-YunxiNeural',fmt="mp3",response=None,**ka) -> bytes:
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
    '''
    import edge_tts
    ka.setdefault('voice',voice)
    
    comm = edge_tts.Communicate(text,**ka)
    buf = b""
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
    gms = MQTTServer(request_topic='runx',globals=globals(), server_public_key_bytes=public_key)
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