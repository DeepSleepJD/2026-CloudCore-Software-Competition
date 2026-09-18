"""Conservative, task-local evidence for offset pages returned by simple curl calls."""
import json
import shlex
import re
from urllib.parse import parse_qsl, urlsplit


def curl_request(command):
    try:
        args = shlex.split(command)
        # Bind evidence only to one GET. Unknown shell programs are not guessed.
        while len(args) >= 3 and args[0] == 'cd' and args[2] == '&&':
            args = args[3:]
        if not args or args.pop(0) != 'curl':
            return None
        headers, url = {}, None
        while args:
            arg = args.pop(0)
            if arg in ('-s', '-S', '-sS', '--silent', '--show-error', '-f', '--fail', '-g', '--globoff'):
                continue
            if arg in ('-H', '--header') and args:
                key, value = args.pop(0).split(':', 1)
                headers[key.lower().strip()] = value.strip()
            elif arg.startswith(('http://', 'https://')) and url is None:
                url = arg
            else:
                return None
        parsed = urlsplit(url or '')
        if not parsed.hostname:
            return None
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        params = dict(pairs)
        if len(pairs) != len(params):
            return None
        return {'url': parsed._replace(query='', fragment='').geturl(), 'params': params, 'headers': headers}
    except (ValueError, IndexError):
        return None


class PageEvidence:
    def __init__(self):
        self.key = None
        self.total = None
        self.pages = {}

    def observe(self, command, output):
        request = curl_request(command)
        if not request or not output.startswith('[exitCode:0]\n') or '[TRUNCATED]' in output:
            return False
        try:
            data = json.loads(output.split('\n', 1)[1])
            if data.get('error') or data.get('success') is False or data.get('code', 200) != 200:
                return False
            env = data.get('data', data)
            rows, page = env['records'], env['pagination']
            total, offset = page.get('total_count', page.get('total')), page['offset']
            if (type(total) is not int or not 0 <= total <= 10000 or type(offset) is not int
                    or offset < 0 or not isinstance(rows, list) or offset + len(rows) > total):
                return False
            if any(not isinstance(r, dict) or r.get('id') is None for r in rows):
                return False
            if int(request['params'].get('offset', 0)) != offset:
                return False
            business = {k: v for k, v in request['params'].items() if k not in ('offset', 'limit')}
            key = json.dumps([request['url'], business, request['headers']], sort_keys=True)
            if key != self.key:
                self.key, self.total, self.pages = key, total, {}
            if total != self.total:
                self.total, self.pages = total, {}
                return False
            if offset in self.pages and self.pages[offset] != rows:
                self.pages = {}
                return False
            self.pages[offset] = rows
            cursor, ids = 0, set()
            for start, batch in sorted(self.pages.items()):
                if start != cursor:
                    return False
                cursor += len(batch)
                for row in batch:
                    rid = json.dumps(row['id'], sort_keys=True)
                    if rid in ids:
                        return False
                    ids.add(rid)
            return cursor == total and len(ids) == total and page.get('has_next') is not True
        except (ValueError, TypeError, KeyError, AttributeError):
            return False


class ProtocolMemory:
    """Match-local verified interface facts. Never retain answers or city values."""
    def __init__(self):
        self.entries = {}

    @staticmethod
    def schema(value, depth=0):
        if depth >= 4:
            return type(value).__name__
        if isinstance(value, dict):
            return {k: ProtocolMemory.schema(v, depth + 1) for k, v in list(value.items())[:24]}
        if isinstance(value, list):
            return [ProtocolMemory.schema(value[0], depth + 1)] if value else []
        return type(value).__name__

    def remember(self, command, output, workspace):
        request = curl_request(command)
        if not request or not output.startswith('[exitCode:0]\n') or '[TRUNCATED]' in output:
            return
        try:
            value = json.loads(output.split('\n', 1)[1])
            if not isinstance(value, dict):
                return
            if value.get('error') or value.get('success') is False or value.get('code', 200) != 200:
                self.entries.pop((workspace, request['url']), None)
                return
            if not isinstance(value.get('data', value).get('records'), list):
                return
            self.store(request, workspace, response_schema=self.schema(value))
        except (ValueError, AttributeError):
            return

    def store(self, request, workspace, **evidence):
        value = {'url': request['url'], 'headers': request.get('headers', {}),
                 'parameter_names': sorted(request.get('params', {})),
                 'binding_rule': '所有业务参数和固定答案字段都从当前任务重新绑定；凭据以本题为准。', **evidence}
        self.entries[workspace, request['url']] = value
        while len(self.entries) > 8:
            del self.entries[next(iter(self.entries))]

    def relevant(self, text, workspace):
        origins = set()
        for url in re.findall(r'https?://[^\s`<>"，。；]+', text):
            try:
                parts = urlsplit(url)
                origins.add((parts.scheme, parts.netloc))
            except ValueError:
                continue
        return [entry for (folder, _), entry in self.entries.items() if folder == workspace
                and (urlsplit(entry['url']).scheme, urlsplit(entry['url']).netloc) in origins]
