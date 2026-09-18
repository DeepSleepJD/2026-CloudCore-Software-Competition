"""Copied into the task sandbox by bootstrap; never called by the HTTP agent."""
import argparse
import json
import signal
import time
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

# Shared deadline across all pages when imported by a sandbox script.
DEADLINE = time.monotonic() + 10


def api_failed(data):
    if not isinstance(data, dict):
        return False
    code = str(data.get("code", ""))
    status = data.get("status")
    return (status in ("error", "failed", "failure") or data.get("success") is False
            or data.get("ok") is False or bool(data.get("error"))
            or (len(code) == 3 and code.startswith(("4", "5")))
            or (isinstance(status, int) and status >= 400))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(url, api_key="", header="X-API-Key"):
    """Return an explicit envelope; callers must check ok before using data."""
    split = urlsplit(url)
    url = urlunsplit((split.scheme, split.netloc, split.path,
                     urlencode(parse_qsl(split.query, keep_blank_values=True)), ""))
    if split.scheme not in ("http", "https"):
        return {"ok": False, "error": "Only HTTP APIs are supported"}
    for attempt in range(2):
        remaining = DEADLINE - time.monotonic()
        if remaining <= 0:
            return {"ok": False, "error": "Local command time budget exhausted"}
        headers = {}
        if api_key:
            headers[header] = "Bearer " + api_key if header == "Authorization" else api_key
        try:
            try:
                response = build_opener(NoRedirect()).open(Request(url, headers=headers),
                                                          timeout=min(3, remaining))
            except HTTPError as exc:
                response = exc
            with response:
                status = response.code
                raw = response.read(48001)
            if len(raw) > 48000:
                return {"ok": False, "status": status, "error": "[TRUNCATED] Reduce page size"}
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        ok = 200 <= status < 300 and not api_failed(data)
        message = str(data.get("message", "")) if isinstance(data, dict) else ""
        # Reuse only the caller-supplied task key, and only on explicit server guidance.
        if not ok and attempt == 0 and api_key and header != "Authorization" and (
                "authorization" in message.lower() and "bearer" in message.lower()):
            header = "Authorization"
            continue
        return {"kind": "task_http", "ok": ok, "status": status, "url": url,
                "authHeader": header if api_key else None, "data": data}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--key", default="")
    parser.add_argument("--header", default="X-API-Key")
    args = parser.parse_args()
    if hasattr(signal, "alarm"):
        signal.alarm(12)
    result = request_json(args.url, args.key, args.header)
    output = json.dumps(result, ensure_ascii=False)
    if len(output.encode("utf-8")) > 56000:
        result = {"ok": False, "error": "[TRUNCATED] Reduce page size"}
        output = json.dumps(result)
    print(output)
    raise SystemExit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
