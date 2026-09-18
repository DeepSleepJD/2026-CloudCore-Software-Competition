import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from urllib.request import Request, urlopen

from test_agent import payload
from test_jd_v4 import scene
from test_task_protocol import SKILL, run_fixture_solver
from zk_agent.baseline import BaselineAgent
from zk_agent.config import Config
from tools.package_submission import build


class DeploymentTests(unittest.TestCase):
    def test_nullable_official_demo_fields_still_produce_actions(self):
        with tempfile.TemporaryDirectory() as temp:
            for field in ("robot", "teamEnemy", "vendorShopList", "weaponShopList"):
                data = payload(1, towers=False)
                data[field] = None
                result = BaselineAgent(Config.baseline(state_dir=temp)).decide(data)
                self.assertTrue(result["roleCommandMap"], field)

    def test_null_collections_and_role_defaults(self):
        data = payload(1, towers=False)
        data.update(robot={"roles": None}, teamEnemy={"roles": None}, vendorShopList=None, weaponShopList=None)
        data["teamOur"]["playerTasks"] = None
        data["mapInfo"]["zones"] = None
        for role in data["teamOur"]["roles"]:
            role.update(backpack=None, cooldown=None, attackRange=None, backPackCapability=None)
        with tempfile.TemporaryDirectory() as temp:
            result = BaselineAgent(Config.baseline(state_dir=temp)).decide(data)
            self.assertTrue(result["roleCommandMap"])
        self.assertIsNone(data["teamOur"]["roles"][0]["backpack"])

    def test_extracted_demo_main3_entry_from_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = build(root / "CoreGeek.tar.gz")
            with tarfile.open(archive) as pack:
                self.assertTrue(all(n == "CoreGeek" or n.startswith("CoreGeek/") for n in pack.getnames()))
                for name in ("main3.py", "pyproject.toml", "agent/server.py", "agent/strategy.py"):
                    self.assertIsNotNone(pack.getmember("CoreGeek/" + name))
                self.assertEqual(pack.getmember("CoreGeek/run.sh").mode, 0o755)
                self.assertNotIn(b"\r", pack.extractfile("CoreGeek/run.sh").read())
                # Older Windows Python 3.10 builds predate tarfile filters.
                # This archive was generated above from our fixed file allowlist.
                if hasattr(tarfile, "data_filter"):
                    pack.extractall(root, filter="data")
                else:
                    pack.extractall(root)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            env = os.environ.copy()
            env.pop("PYTHONPATH", None)
            log = root / "startup.log"
            with log.open("w", encoding="utf-8") as stream:
                process = subprocess.Popen([sys.executable, str(root / "CoreGeek/main3.py"), str(port)],
                                           cwd=root, env=env, stdout=stream, stderr=stream)
                try:
                    url = f"http://127.0.0.1:{port}/"
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        try:
                            with urlopen(url, timeout=0.5) as response:
                                status = json.load(response)
                            break
                        except OSError:
                            if process.poll() is not None:
                                self.fail(log.read_text(encoding="utf-8"))
                            time.sleep(0.05)
                    else:
                        self.fail("packaged entry failed to start")
                    self.assertEqual(status["version"], "v6-jd")
                    for round_no in (1, 2, 3):
                        data = payload(round_no, towers=False)
                        data.update(robot=None, teamEnemy=None, phaseTask=None)
                        for role in data["teamOur"]["roles"]:
                            role["backpack"] = None
                        request = Request(url, json.dumps(data).encode(), {"Content-Type": "application/json"})
                        with urlopen(request, timeout=5) as response:
                            self.assertTrue(json.load(response)["roleCommandMap"])
                    # Exercise the packaged task path over HTTP, including the actual
                    # generated wrapper executing our fixed, reviewed fixture solver.
                    task = scene(4)
                    task['teamOur']['roles'][3]['pos'] = {'x':24, 'y':14}
                    task['teamOur']['playerTasks'] = [{'taskType':'自进化类1', 'isValid':True, 'timeoutRounds':30}]

                    def post_task(**changes):
                        task.update(llmResp='', lastCmdResult='')
                        task.update(changes)
                        request = Request(url, json.dumps(task).encode(), {"Content-Type": "application/json"})
                        with urlopen(request, timeout=5) as response:
                            answer = json.load(response)
                        task['roundNo'] += 1
                        return answer

                    self.assertEqual(post_task()['roleCommandMap']['20011']['action'], 'acceptTask')
                    self.assertEqual(post_task(phaseTask='请阅读task_http.md，获取任务信息')['executeCmd'], 'cat -- task_http.md')
                    self.assertTrue(post_task(lastCmdResult='[exitCode:0]\n城市历史建筑统计：count=23')['prompt'])
                    cmd = post_task(llmResp=json.dumps(SKILL))['executeCmd']
                    answer = post_task(lastCmdResult=run_fixture_solver(cmd))['roleCommandMap']['20011']
                    self.assertEqual(answer['action'], 'submitAnswer')
                    self.assertEqual(json.loads(answer['taskAnswer']), {'count':23})
                    with urlopen(url, timeout=5) as response:
                        status = json.load(response)
                    self.assertEqual(status["requests"], 8)
                    self.assertIsNone(status["last_error"])
                finally:
                    process.terminate()
                    process.wait(timeout=5)
            self.assertIn("round=1 actions=", log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
