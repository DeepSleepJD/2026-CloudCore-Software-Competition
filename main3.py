"""Platform entry point: python3 /home/docker/CoreGeek/main3.py <port>."""
import argparse
import logging
import sys
from agent.server import serve


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CloudCore survival HTTP agent")
    parser.add_argument("port", type=int)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    serve(args.port)
