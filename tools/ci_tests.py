"""Run regression tests and expose failure details as CI annotations."""
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

if __name__ == "__main__":
    # Preserve Chinese failure evidence on Windows runners with a legacy locale.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for case, detail in result.errors + result.failures:
            message = (str(case) + "\n" + detail).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            print("::error::" + message)
    sys.exit(not result.wasSuccessful())
