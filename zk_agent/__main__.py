import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import sys
from threading import Lock

from .config import Config
from .strategy import Agent
from .baseline import BaselineAgent

LOG = logging.getLogger(__name__)


def make_server(port, config, host="0.0.0.0"):
    agent = BaselineAgent(config) if config.profile == "baseline" else Agent(config)
    lock = Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"status":"ok","agent":"zk_agent"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            reply = {"roleCommandMap": {}, "prompt": "", "executeCmd": ""}
            try:
                self.connection.settimeout(2)
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 2 * 1024 * 1024:
                    raise ValueError("invalid request size")
                raw = self.rfile.read(length)
                payload = json.loads(raw.decode("utf-8"))
                with lock:
                    reply = agent.decide(payload)
            except Exception:
                LOG.exception("decision failed; returning a valid empty turn")
            body = json.dumps(reply, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                LOG.warning("client disconnected")

        def log_message(self, fmt, *args):
            LOG.debug(fmt, *args)

    return ThreadingHTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser(description="Future War: fixed defence + reusable task solvers")
    parser.add_argument("port", type=int)
    parser.add_argument("--config")
    args = parser.parse_args()
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = Config.load(args.config)
    server = make_server(args.port, config)
    LOG.info("listening on 0.0.0.0:%s; PvP=%s", args.port, config.pvp_mode)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
