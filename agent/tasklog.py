"""Task-layer debug log: every line tagged with the round, file plus stderr."""
import logging
import sys

LOG = logging.getLogger("task")
_CONFIGURED = False


def setup():
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True
    LOG.setLevel(logging.DEBUG)
    LOG.propagate = False
    fmt = logging.Formatter("%(asctime)s %(message)s")
    try:
        fh = logging.FileHandler("task_debug.log", encoding="utf-8")
        fh.setFormatter(fmt)
        LOG.addHandler(fh)
    except OSError:
        pass  # read-only deployment: stderr still carries everything
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    LOG.addHandler(sh)


def rlog(round_no, message):
    setup()
    LOG.debug("R%s %s", round_no, message)
