#!/usr/bin/env python3
"""Mock OpenAI-compatible completion server for testing TriggerComplete.

Streams a fixed text word by word with a delay, and records every request:
GET /stats returns them (body, tokens sent, finished or aborted), POST /reset clears them,
POST /config {"text": ..., "delay": ..., "think": ...} changes the reply at runtime.

  python3 tools/mock_server.py --port 8080 --delay 0.1
"""

import argparse
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"requests": []}
LOCK = threading.Lock()


def make_handler(args):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *a):
            if args.verbose:
                super().log_message(fmt, *a)

        def _json(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/stats":
                with LOCK:
                    return self._json(200, STATE)
            self._json(404, {"error": "not found"})

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            if self.path == "/reset":
                with LOCK:
                    STATE["requests"].clear()
                return self._json(200, {"ok": True})
            if self.path == "/config":
                for k, v in json.loads(raw or b"{}").items():
                    setattr(args, k, v)
                return self._json(200, {"ok": True})
            if self.path not in ("/v1/chat/completions", "/v1/completions"):
                return self._json(404, {"error": {"message": "unknown path " + self.path}})
            body = json.loads(raw or b"{}")
            chat = self.path.endswith("chat/completions")
            rec = {"path": self.path, "body": body, "auth": self.headers.get("Authorization"),
                   "tokens_sent": 0, "finished": False, "aborted": False, "t_start": time.time()}
            with LOCK:
                STATE["requests"].append(rec)

            text = args.text
            if args.think:
                text = "<think>Let me think about this.</think>\n\n" + text
            tokens = re.findall(r"\s*\S+", text) or [text]
            tokens = tokens[: int(body.get("max_tokens") or len(tokens))]

            if not body.get("stream"):
                piece = {"message": {"role": "assistant", "content": text}} if chat else {"text": text}
                rec["finished"] = True
                return self._json(200, {"choices": [dict(index=0, finish_reason="stop", **piece)]})

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for tok in tokens:
                    time.sleep(args.delay)
                    choice = {"index": 0, "delta": {"content": tok}} if chat else {"index": 0, "text": tok}
                    self.wfile.write(b"data: " + json.dumps({"choices": [choice]}).encode() + b"\n\n")
                    self.wfile.flush()
                    rec["tokens_sent"] += 1
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                rec["finished"] = True
            except (BrokenPipeError, ConnectionResetError):
                rec["aborted"] = True
            rec["t_end"] = time.time()
            self.close_connection = True

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--delay", type=float, default=0.05, help="seconds between tokens")
    ap.add_argument("--text", default="jumps over the lazy dog and runs into the forest.")
    ap.add_argument("--think", action="store_true", help="prepend a <think> block")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args))
    srv.daemon_threads = True
    print("mock server on http://127.0.0.1:%d/v1" % args.port, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
