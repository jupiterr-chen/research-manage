"""OpenAI 兼容 LLM stub(R-RUN-10):/v1/models、/v1/chat/completions。

- 返回含 usage 的固定文本,不返回 tool_calls(分析师节点直接产出文本)
- STUB_MODE:timeout(挂 60s)| 5xx(返回 500)| 空 = 正常
- STUB_DELAY_SECONDS:每请求延迟(取消/续跑测试用)
- 每个请求打印一行 REQ 日志到 stdout(docker logs 计数用)
"""

from __future__ import annotations

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODELS = ["gpt-5.6", "gpt-5.6-luna"]
STUB_MODE = os.environ.get("STUB_MODE", "")
STUB_DELAY = float(os.environ.get("STUB_DELAY_SECONDS", "0"))


def _now() -> str:
    return time.strftime("%H:%M:%S")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静默默认访问日志,用 REQ 行替代
        pass

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/v1/models"):
            self._json(200, {"object": "list", "data": [{"id": m, "object": "model"} for m in MODELS]})
        else:
            self._json(404, {"error": {"message": f"no route {self.path}"}})

    def do_POST(self):  # noqa: N802
        if not self.path.startswith("/v1/chat/completions"):
            self._json(404, {"error": {"message": f"no route {self.path}"}})
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            req = json.loads(raw)
        except ValueError:
            self._json(400, {"error": {"message": "bad json"}})
            return
        print(f"REQ {_now()} model={req.get('model')} msgs={len(req.get('messages', []))}", flush=True)
        if STUB_MODE == "5xx":
            self._json(500, {"error": {"message": "stub injected 5xx"}})
            return
        if STUB_DELAY > 0:
            time.sleep(STUB_DELAY)
        if STUB_MODE == "timeout":
            time.sleep(60)
        stream = bool(req.get("stream"))
        prompt_tail = ""
        for msg in req.get("messages", [])[-2:]:
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
            prompt_tail = f"{prompt_tail}{str(content)[-120:]}"
        reply = (
            "[stub] Based on the provided context: "
            f"{prompt_tail.strip()[:200] or '(no context)'} — analysis summary with balanced "
            "bullish and bearish considerations; conclusion: hold with moderate confidence."
        )
        usage = {
            "prompt_tokens": max(16, len(raw) // 4),
            "completion_tokens": 60,
            "total_tokens": max(16, len(raw) // 4) + 60,
        }
        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            chunk = {
                "id": "chatcmpl-stub",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": reply}}],
            }
            data = (f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n").encode()
            self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
            final = {
                "id": "chatcmpl-stub",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": usage,
            }
            data2 = (f"data: {json.dumps(final)}\n\ndata: [DONE]\n\n").encode()
            self.wfile.write(f"{len(data2):X}\r\n".encode() + data2 + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            return
        self._json(
            200,
            {
                "id": "chatcmpl-stub",
                "object": "chat.completion",
                "model": req.get("model", MODELS[0]),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": reply},
                        "finish_reason": "stop",
                    }
                ],
                "usage": usage,
            },
        )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[llm-stub] listening on :{port} mode={STUB_MODE!r} delay={STUB_DELAY}", flush=True)
    server.serve_forever()
