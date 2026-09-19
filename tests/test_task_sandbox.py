"""Real local files, Python subprocesses and HTTP; deterministic stand-in for the LLM."""
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from agent.task_bootstrap import bootstrap_command
from tests.test_tasks import TaskRollout


def execute_fixture_command(cmd):
    # Only executes commands constructed in this test or by our bootstrap/helper.
    args = shlex.split(cmd)
    assert args[0] == "python3"
    result = subprocess.run([sys.executable, "-X", "utf8", *args[1:]], capture_output=True,
                            encoding="utf-8", timeout=14)
    return "[exitCode:%d]\n%s%s" % (result.returncode, result.stdout, result.stderr)


class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="task_fixture_")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                query = parse_qs(urlsplit(self.path).query)
                owner.requests.append((self.path, self.headers.get("Authorization")))
                if self.headers.get("Authorization") != "Bearer fixture-key":
                    # Test HTTP errors and APIs that encode errors under HTTP 200.
                    status = 200 if urlsplit(self.path).path == "/business-error" else 401
                    data = {"status": "error", "code": 401,
                            "message": "Missing Authorization header. Expected Authorization: Bearer <api_key>"}
                elif urlsplit(self.path).path == "/broken":
                    status, data = 200, {"success": False, "message": "Query failed"}
                else:
                    status = 200
                    page = int(query.get("page", ["1"])[0])
                    data = {"data": {"items": ([{"name": "甲", "year": -500, "type": "建筑", "level": "世界遗产"},
                                                   {"name": "乙", "year": 1000, "type": "园林", "level": "国家级"}]
                                                  if page == 1 else [{"name": "丙", "year": 1500, "type": "建筑", "level": "世界遗产"}]),
                                     "total": 3, "page": page}}
                raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.url = "http://127.0.0.1:%d/heritage" % self.server.server_port
        (self.root / "task_test.md").write_text(
            'Read API_DOCS.md; query 北京, all pages; report total_count, world_heritage_count, types, oldest_era (name).', encoding="utf-8")
        (self.root / "API_DOCS.md").write_text(
            self.url + "\nX-API-Key: fixture-key\nSome documentation is outdated.", encoding="utf-8")

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def bootstrap(self):
        cmd = bootstrap_command("Read task_test.md", self.root.as_posix())
        output = execute_fixture_command(cmd)
        self.assertTrue(output.startswith("[exitCode:0]\n"), output)
        body = json.loads(output.partition("\n")[2])
        # Only remove the exact helper file and its fresh directory, never recursively.
        helper = Path(body["httpHelper"])
        self.addCleanup(helper.parent.rmdir)
        self.addCleanup(helper.unlink)
        return body

    def test_bootstrap_reads_task_and_docs_in_one_command(self):
        body = self.bootstrap()
        self.assertEqual([], body["errors"])
        self.assertEqual({"task_test.md", "API_DOCS.md"}, {Path(f["path"]).name for f in body["files"]})
        self.assertTrue(all(not f["truncated"] for f in body["files"]))

    def test_http_auth_retry_encoding_and_business_error(self):
        body = self.bootstrap()
        for endpoint in (self.url, self.url.replace("/heritage", "/business-error")):
            output = execute_fixture_command(shlex.join([
                "python3", body["httpHelper"], endpoint + "?city=北京&page=1", "--key", "fixture-key"]))
            self.assertTrue(output.startswith("[exitCode:0]\n"), output)
            result = json.loads(output.partition("\n")[2])
            self.assertTrue(result["ok"])
            self.assertEqual("Authorization", result["authHeader"])
            self.assertEqual(3, result["data"]["data"]["total"])
        self.assertEqual(4, len(self.requests))
        self.assertIn("city=%E5%8C%97%E4%BA%AC", self.requests[0][0])
        failed = execute_fixture_command(shlex.join([
            "python3", body["httpHelper"], self.url.replace("/heritage", "/broken"),
            "--key", "fixture-key", "--header", "Authorization"]))
        self.assertTrue(failed.startswith("[exitCode:1]\n"), failed)
        self.assertFalse(json.loads(failed.partition("\n")[2])["ok"])

    def start_task(self):
        sim = TaskRollout()
        for task in sim.p["teamOur"]["playerTasks"]:
            task["timeoutRounds"] = 15
        sim.reach("accept")
        sim.agent.memory["tasks"]["discoveries"]["自进化类1"] = {"directory": self.root.as_posix()}
        response = sim.step(False, phaseTask="Read task_test.md")
        self.assertFalse(response["prompt"])
        self.assertTrue(response["executeCmd"])
        output = execute_fixture_command(response["executeCmd"])
        body = json.loads(output.partition("\n")[2])
        helper = Path(body["httpHelper"])
        self.addCleanup(helper.parent.rmdir)
        self.addCleanup(helper.unlink)
        response = sim.step(False, lastCmdResult=output)
        self.assertIn("API_DOCS.md", response["prompt"])
        return sim, body

    def test_unproven_statistics_do_not_pass_as_sandbox_evidence(self):
        sim, bootstrap = self.start_task()
        response = sim.step(False, llmResp=json.dumps({"httpRequest": {
            "url": self.url + "?city=北京&page=1", "apiKey": "fixture-key"}}))
        output = execute_fixture_command(response["executeCmd"])
        response = sim.step(False, lastCmdResult=output)
        self.assertIn('"authHeader": "Authorization"', response["prompt"])
        # A deterministic stand-in for the LLM, using the observed nested schema.
        source = "\n".join([
            "import json, runpy",
            "request = runpy.run_path(%r)['request_json']" % bootstrap["httpHelper"],
            "records = []",
            "for page in range(1, 4):",
            "    response = request(%r + '&page=' + str(page), 'fixture-key', 'Authorization')" % (self.url + "?city=北京"),
            "    assert response['ok'], response",
            "    data = response['data']['data']",
            "    assert isinstance(data['items'], list) and all(isinstance(r, dict) for r in data['items'])",
            "    records.extend(data['items'])",
            "    if len(records) >= data['total']: break",
            "assert len(records) == data['total'] == len({r['name'] for r in records})",
            "print(json.dumps(dict(total_count=len(records), world_heritage_count=sum(r['level']=='世界遗产' for r in records), types=sorted({r['type'] for r in records}), oldest_era=min(records, key=lambda r:r['year'])['name']), ensure_ascii=False))",
        ])
        response = sim.step(False, llmResp=json.dumps({"executeCmd": "python3 -c " + shlex.quote(source)}))
        output = execute_fixture_command(response["executeCmd"])
        self.assertTrue(output.startswith("[exitCode:0]\n"), output)
        answer = json.loads(output.partition("\n")[2])
        self.assertEqual({"total_count": 3, "world_heritage_count": 2, "types": ["园林", "建筑"], "oldest_era": "甲"}, answer)
        sim.step(False, lastCmdResult=output)
        response = sim.step(False, llmResp=json.dumps({"taskAnswer": answer}))
        self.assertNotEqual('submitAnswer', response['roleCommandMap'].get(sim.rid, {}).get('action'))
        self.assertTrue(response['prompt'])

    def test_failed_task_keeps_discoveries_and_zero_exit_api_error_is_failure(self):
        sim, body = self.start_task()
        response = sim.step(False, llmResp=json.dumps({"executeCmd": "python3 -c 'print(0)'"}))
        error = {"status": "error", "code": 401, "message": "Missing Authorization: Bearer <api_key>"}
        response = sim.step(False, lastCmdResult="[exitCode:0]\n" + json.dumps(error))
        self.assertIn("API业务失败", response["prompt"])
        self.assertNotIn("命令正常完成", response["prompt"])
        sim.step(False, errors=[{"errorCode": 1}])
        facts = sim.agent.memory["tasks"]["discoveries"]["自进化类1"]
        self.assertEqual(self.root.as_posix(), facts["directory"])
        self.assertIn("Bearer", facts["authGuidance"])
        self.assertNotIn("fixture-key", json.dumps(facts))
        self.assertFalse(sim.agent.memory["tasks"]["experience"])

    def test_more_than_six_commands_allowed_only_with_time_for_submission(self):
        sim = TaskRollout(); sim.reach("llm")
        state = sim.agent.memory["tasks"]["active"]
        state["cmds"] = 6
        response = sim.step(False, llmResp=json.dumps({"executeCmd": "echo bounded"}))
        self.assertEqual("echo bounded", response["executeCmd"])
        sim.step(False, lastCmdResult='[exitCode:0]\n{"token":"observed"}')
        state["deadline"] = sim.p["roundNo"] + 3
        response = sim.step(False, llmResp=json.dumps({"executeCmd": "echo too_late"}))
        self.assertFalse(response["executeCmd"])
        self.assertIn('"remainingCommands": 0', response["prompt"])
        response = sim.step(False, llmResp=json.dumps({"taskAnswer": {"token": "observed"}}))
        self.assertEqual("submitAnswer", response["roleCommandMap"][sim.rid]["action"])

    def test_incomplete_pagination_is_rejected_before_submit(self):
        sim, _ = self.start_task()
        # Simulate a successful API response that declares 15 rows but only returns 10.
        partial = {"kind": "task_http", "ok": True, "status": 200,
                   "url": self.url + "?location=北京&offset=0&limit=10",
                   "authHeader": "Authorization",
                   "data": {"data": {"records": [{"id": str(i)} for i in range(10)],
                                      "pagination": {"total_count": 15, "offset": 0, "limit": 10}}}}
        sim.step(False, llmResp=json.dumps({"executeCmd": "echo partial"}))
        sim.step(False, lastCmdResult="[exitCode:0]\n" + json.dumps(partial, ensure_ascii=False))
        # The scheduler has observed this response; assert the hard gate directly as
        # well so the regression remains focused even if the simulator skips a round.
        sim.agent.memory["tasks"]["active"]["evidence"] = {
            "successfulCommand": True, "expectedCount": 15, "observedCount": 10}
        response = sim.step(False, llmResp=json.dumps({"taskAnswer": {
            "city": "北京", "total_count": 15, "world_heritage_count": 6,
            "types": ["建筑"], "oldest_era": "甲"}}))
        self.assertTrue(response["prompt"])
        self.assertIn("数据不完整", response["prompt"])
        self.assertNotEqual("submitAnswer", response["roleCommandMap"].get(sim.rid, {}).get("action"))

    def test_bootstrap_docs_survive_transcript_eviction_and_team_reset(self):
        sim, body = self.start_task()
        state = sim.agent.memory["tasks"]["active"]
        state["deadline"] += 15
        for _ in range(5):
            sim.step(False, llmResp=json.dumps({"executeCmd": "echo probe"}))
            response = sim.step(False, lastCmdResult="[exitCode:0]\nprobe")
        self.assertEqual(8, len(state["transcript"]))
        self.assertIn("API_DOCS.md", response["prompt"])
        self.assertEqual(body, state["bootstrap"])
        sim.p["teamOur"]["teamId"] = "new-match"
        sim.step(False, phaseTask="")
        self.assertFalse(sim.agent.memory["tasks"]["discoveries"])


if __name__ == "__main__":
    unittest.main()
