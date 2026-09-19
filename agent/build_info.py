"""Content fingerprint works in uploaded packages without .git."""
import hashlib
from pathlib import Path
import sys


def fingerprint():
    root = Path(__file__).resolve().parent.parent
    files = sorted((root / 'agent').glob('*.py')) + [root / 'main3.py', root / 'run.sh']
    hashes = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in files if p.is_file()}
    digest = hashlib.sha256(''.join(k + v for k, v in sorted(hashes.items())).encode()).hexdigest()
    return {'buildId': digest[:16], 'moduleRoot': str(root), 'entrypoint': str(Path(sys.argv[0]).resolve()),
            'files': hashes}


BUILD = fingerprint()

if __name__ == '__main__':
    import json
    print(json.dumps(BUILD, ensure_ascii=False, indent=2))
