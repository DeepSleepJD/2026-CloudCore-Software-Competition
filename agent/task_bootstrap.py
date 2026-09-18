"""Build a bounded sandbox command; no local task-file reads or execution."""
import re
import shlex
from pathlib import Path


SCRIPT = r'''
import json, os, re, signal, tempfile, time
from pathlib import Path
if hasattr(signal, "alarm"):
    signal.alarm(12)
end = time.monotonic() + 4
name = Path(CONFIG["name"]).name
matches = set()
roots = [CONFIG.get("directory"), "/tmp/selfEvolutionTask", os.getcwd(), "/tmp", "/workspace", "/app", "/home", "/opt"]
explicit = Path(CONFIG["name"])
if explicit.is_absolute() and explicit.is_file():
    matches.add(str(explicit.resolve()))
for root in roots:
    if not root or matches:
        continue
    candidate = Path(root) / name
    if candidate.is_file():
        matches.add(str(candidate.resolve()))
        break
for root in roots:
    if not root or matches:
        continue
    count = 0
    for base, dirs, files in os.walk(root):
        count += 1
        if time.monotonic() > end or count > 3000:
            break
        dirs[:] = [d for d in sorted(dirs) if d not in
                   (".git", "node_modules", ".cache", "proc", "sys", "dev")]
        if name in files:
            matches.add(str((Path(base) / name).resolve()))
            if len(matches) > 1:
                break
result = {"kind": "task_bootstrap", "files": [], "errors": []}
if len(matches) != 1:
    result["errors"].append("Task file missing or ambiguous: " + repr(sorted(matches)))
else:
    task = Path(next(iter(matches)))
    result["taskFile"] = task.as_posix()
    queue, seen, budget = [task], set(), 36000
    while queue and len(seen) < 4 and budget > 0:
        path = queue.pop(0)
        if str(path) in seen:
            continue
        seen.add(str(path))
        try:
            limit = min(14000, budget)
            with path.open("rb") as source:
                raw = source.read(limit + 1)
            truncated = len(raw) > limit
            content = raw[:limit].decode("utf-8", errors="replace")
            budget -= len(raw[:limit])
            result["files"].append({"path": path.as_posix(), "text": content, "truncated": truncated})
            if path == task:
                refs = re.findall(r"[A-Za-z0-9_./-]+\.md", content)
                for ref in refs + ["API_DOCS.md", "README.md"]:
                    related = (task.parent / ref).resolve()
                    if related.is_file() and related.parent == task.parent:
                        queue.append(related)
        except OSError as exc:
            result["errors"].append(str(exc))
try:
    helper = Path(tempfile.mkdtemp(prefix="task_tools_")) / "task_http.py"
    helper.write_text(CONFIG["httpSource"], encoding="utf-8")
    result["httpHelper"] = helper.as_posix()
except OSError as exc:
    result["errors"].append("HTTP helper unavailable: " + str(exc))
# Stay below the platform's 64KB output limit, accounting for JSON escaping.
while len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 56000 and result["files"]:
    largest = max(result["files"], key=lambda entry: len(entry["text"]))
    largest["text"] = largest["text"][:len(largest["text"]) // 2]
    largest["truncated"] = True
print(json.dumps(result, ensure_ascii=False))
'''


def bootstrap_command(description, directory=None):
    names = list(dict.fromkeys(re.findall(r"[A-Za-z0-9_./-]+\.md", description)))
    if len(names) != 1:
        return ""
    config = {"name": names[0], "directory": directory,
              "httpSource": Path(__file__).with_name("sandbox_http.py").read_text(encoding="utf-8")}
    source = "CONFIG = " + repr(config) + "\n" + SCRIPT
    return "python3 -c " + shlex.quote(source)
