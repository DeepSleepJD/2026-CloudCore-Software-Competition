#!/usr/bin/env python3
"""Official Demo-compatible entry point: python main3.py <port>."""
import os
from pathlib import Path
import sys


def main():
    root = Path(__file__).resolve().parent
    os.chdir(root)
    sys.path.insert(0, str(root / "src"))
    from zk_agent.__main__ import main as run
    run()


if __name__ == "__main__":
    main()
