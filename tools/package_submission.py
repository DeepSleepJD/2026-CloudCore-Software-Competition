"""Build the official Demo-style CoreGeek/ deployment tree."""
import argparse
import io
import json
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]


def build(output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    files = {name: ROOT / name for name in ("main3.py", "run.sh", "config.example.json", "README.md")}
    files.update({"src/zk_agent/" + path.name: path for path in (ROOT / "zk_agent").glob("*.py")})
    files.update({"src/agent/" + path.name: path for path in (ROOT / "src/agent").glob("*.py")})
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    with tarfile.open(output, "w:gz") as archive:
        def add(name, raw, mode=0o644):
            item = tarfile.TarInfo("CoreGeek/" + name)
            item.size, item.mode = len(raw), mode
            archive.addfile(item, io.BytesIO(raw))
        for folder in ("", "src/", "src/agent/", "src/zk_agent/"):
            item = tarfile.TarInfo("CoreGeek/" + folder)
            item.type, item.mode = tarfile.DIRTYPE, 0o755
            archive.addfile(item)
        for name, path in sorted(files.items()):
            add(name, path.read_bytes().replace(b"\r\n", b"\n"), 0o755 if name.endswith(".sh") else 0o644)
        add("BUILD.json", json.dumps({"version": "v3.1-demo-compatible", "source_commit": revision}).encode())
        add("pyproject.toml", b'''[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"
[project]
name = "zk-agent"
version = "3.1.0"
requires-python = ">=3.11"
dependencies = []
[tool.setuptools]
package-dir = {"" = "src"}
[tool.setuptools.packages.find]
where = ["src"]
''')
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(build(args.output))
