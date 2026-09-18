"""Expose the Demo's serve(port) entry with the current strategy."""
from zk_agent.__main__ import make_server
from zk_agent.config import Config


def serve(port):
    server = make_server(port, Config.load(None))
    try:
        server.serve_forever()
    finally:
        server.server_close()
