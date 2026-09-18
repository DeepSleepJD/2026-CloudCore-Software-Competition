"""Same POST/JSON transport as the official demo; no third-party dependencies."""
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from .strategy import Agent

LOG = logging.getLogger(__name__)
EMPTY = {"roleCommandMap": {}, "prompt": "", "executeCmd": ""}


class AgentServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address):
        super().__init__(address, Handler)
        self.agent = Agent()
        self.decision_lock = Lock()
        self.status = {"requests": 0, "last_round": None, "last_actions": 0, "last_error": None}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        with self.server.decision_lock:
            body = json.dumps({"status": "ok", "version": "v5-jd", **self.server.status}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 8 * 1024 * 1024:
                raise ValueError("invalid request length")
            self.connection.settimeout(3)
            request = json.loads(self.rfile.read(size).decode("utf-8"))
            with self.server.decision_lock:
                self.server.status.update(requests=self.server.status["requests"] + 1, last_round=request.get("roundNo"))
                try:
                    response = self.server.agent.decide(request)
                    self.server.status.update(last_actions=len(response["roleCommandMap"]), last_error=None)
                except Exception as error:
                    self.server.status.update(last_actions=0, last_error=type(error).__name__)
                    raise
            LOG.info("round=%s actions=%s", request.get("roundNo"), len(response["roleCommandMap"]))
        except Exception:
            LOG.exception("request/decision failed; returning valid empty response")
            response = EMPTY
        body = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


def serve(port):
    with AgentServer(("0.0.0.0", port)) as server:
        LOG.info("version=v5-jd listening on 0.0.0.0:%s", port)
        server.serve_forever()
