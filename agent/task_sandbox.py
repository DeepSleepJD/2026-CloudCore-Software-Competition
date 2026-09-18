"""Our bounded query/check helpers, embedded into commands for the judge sandbox.

No competition endpoint, credential, workspace or answer is embedded here.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.parse
import urllib.request

REPORT = '__ZK_TASK_RESULT__='


def field(value, path):
    if not path:
        return value
    for part in path.split('.'):
        if not isinstance(value, dict) or part not in value:
            raise ValueError('missing field: ' + path)
        value = value[part]
    return value


def aggregate(records, rules):
    answer = {}
    if not isinstance(rules, dict) or not 1 <= len(rules) <= 32:
        raise ValueError('aggregations must contain 1..32 answer fields')
    for name, rule in rules.items():
        where = rule.get('where', {})
        rows = [r for r in records if all(field(r, k) == v for k, v in where.items())]
        op = rule['op']
        if op == 'constant':
            # The model binds this from the current question, never a cached answer.
            answer[name] = rule['value']
        elif op == 'count':
            answer[name] = len(rows)
        elif op == 'distinct':
            values = [field(r, rule['field']) for r in rows]
            answer[name] = sorted(set(values))
        elif op in ('sum', 'min', 'max'):
            values = [field(r, rule['field']) for r in rows]
            if any(type(v) not in (int, float) for v in values):
                raise ValueError('numeric aggregation requires numeric values')
            if not values and op != 'sum':
                raise ValueError('min/max of empty records is undefined')
            answer[name] = {'sum': sum, 'min': min, 'max': max}[op](values)
        elif op in ('first_by', 'last_by'):
            if not rows:
                raise ValueError('cannot select from empty records')
            order = rule.get('order')
            def rank(row):
                value = field(row, rule['sort_field'])
                if order is not None:
                    if value not in order:
                        raise ValueError('sort value absent from explicit order')
                    return order.index(value)
                if type(value) not in (int, float):
                    raise ValueError('use numeric rank or explicit order, not guessed chronology')
                return value
            chosen = (min if op == 'first_by' else max)(rows, key=rank)
            answer[name] = field(chosen, rule['field'])
        else:
            raise ValueError('unsupported aggregation: ' + str(op))
    return answer


def document(plan):
    """Locate a named task and read explicit sibling Markdown documents together."""
    requested = Path(plan['path'])
    base = Path(plan.get('base') or Path.cwd())
    target = requested if requested.is_absolute() else base / requested
    if not target.is_file() and not requested.is_absolute():
        found, visited = set(), set()
        started = time.monotonic()
        roots = [base] if base != Path(base.anchor) else []
        if os.name != 'nt' and Path('/tmp').is_dir() and Path('/tmp') not in roots:
            roots.append(Path('/tmp'))
        for root in roots:
            for folder, dirs, files in os.walk(root, followlinks=False):
                dirs[:] = [d for d in dirs if d not in {'.git', '.venv', 'node_modules', '__pycache__'}]
                if folder in visited:
                    dirs[:] = []
                    continue
                visited.add(folder)
                if len(visited) > 2000 or time.monotonic() - started > 2:
                    raise ValueError('document search budget reached; specify an exact path')
                if len(Path(folder).relative_to(root).parts) >= 8:
                    dirs[:] = []
                if requested.name in files:
                    candidate = Path(folder, requested.name).resolve()
                    if candidate.as_posix().endswith('/' + requested.as_posix().lstrip('./')):
                        found.add(candidate)
            if found:
                break
        if len(found) != 1:
            raise ValueError('ambiguous task document' if found else 'task document not found')
        target = found.pop()
    target = target.resolve(strict=True)
    def read(path):
        with path.open(encoding='utf-8-sig') as stream:
            value = stream.read(12001)
        return {'path': path.as_posix(), 'text': value[:12000], 'truncated': len(value) > 12000}
    documents = [read(target)]
    # Follow only references in the primary document; do not recursively explore.
    referenced = re.findall(r'(?<![\w./-])([\w./-]+\.md)(?![\w])', documents[0]['text'])
    for name in dict.fromkeys(referenced):
        path = (target.parent / name).resolve()
        if path == target or not path.is_file():
            continue
        documents.append(read(path))
        if len(documents) == 3:
            break
    return {'documents': documents, 'complete': True}


def query(plan):
    pagination = plan['pagination']
    mode = pagination['mode']
    if mode not in ('offset', 'page', 'cursor', 'none'):
        raise ValueError('pagination mode must follow this task documentation')
    total_path = pagination.get('total_path')
    more_path = pagination.get('has_more_path')
    next_path = pagination.get('next_path')
    if mode != 'none' and not any((total_path, more_path, next_path)):
        raise ValueError('pagination needs total/has_more/next evidence')
    started = time.monotonic()
    records, seen_ids, seen_pages, cursors = [], set(), set(), set()
    index = pagination.get('start', 0 if mode != 'page' else 1)
    total = None
    for page_no in range(1, 25):
        if time.monotonic() - started > 9:
            raise ValueError('query time budget reached before completeness')
        params = dict(plan.get('params', {}))
        if mode != 'none':
            params[pagination['param']] = index
        if pagination.get('size_param'):
            params[pagination['size_param']] = pagination.get('size', 100)
        url = plan['url']
        url += ('&' if '?' in url else '?') + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers=plan.get('headers', {}))
        with urllib.request.urlopen(request, timeout=min(2, max(.1, 10 - (time.monotonic() - started)))) as response:
            if response.status != 200:
                raise ValueError('HTTP status is not 200')
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError('response too large; use smaller pages')
        data = json.loads(raw)
        success = plan.get('success')
        if success and field(data, success['path']) != success['equals']:
            raise ValueError('business success condition failed')
        if isinstance(data, dict) and (data.get('error') or data.get('success') is False or
                                      data.get('code') in (400, 401, 403, 404, 429, 500, '401', '403')):
            raise ValueError('API returned an error envelope')
        batch = field(data, plan['records_path'])
        if not isinstance(batch, list) or any(not isinstance(r, dict) for r in batch):
            raise ValueError('records must be an array of objects')
        signature = json.dumps(batch, sort_keys=True)
        if batch and signature in seen_pages:
            raise ValueError('repeated page; pagination did not progress')
        seen_pages.add(signature)
        if total_path:
            current = field(data, total_path)
            if type(current) is not int or current < 0 or (total is not None and total != current):
                raise ValueError('invalid or changing total')
            total = current
        previous_count = len(records)
        for record in batch:
            if plan.get('id_field'):
                record_id = field(record, plan['id_field'])
                if record_id is None:
                    raise ValueError('record ID is null')
                rid = json.dumps(record_id, sort_keys=True)
                if rid in seen_ids:
                    continue
                seen_ids.add(rid)
            records.append(record)
        if len(records) > 10000:
            raise ValueError('record budget exceeded')
        if total is not None and len(records) > total:
            raise ValueError('records exceed declared total')
        more = field(data, more_path) if more_path else None
        if more_path and type(more) is not bool:
            raise ValueError('has_more must be a boolean')
        next_value = field(data, next_path) if next_path else None
        end_cursor = next_path and next_value in (None, '')
        complete = (total is not None and len(records) == total) or more is False or end_cursor or mode == 'none'
        if complete:
            if (total is not None and len(records) != total) or more is True or (next_path and not end_cursor):
                raise ValueError('contradictory pagination metadata')
            return {'answer': aggregate(records, plan['aggregations']), 'complete': True,
                    'records': len(records), 'pages': page_no}
        if len(records) == previous_count:
            raise ValueError('pagination made no progress before declared end')
        if mode == 'cursor':
            if not next_path or json.dumps(next_value) in cursors:
                raise ValueError('missing or repeated next cursor')
            cursors.add(json.dumps(next_value))
            index = next_value
        elif mode == 'offset':
            index += len(batch)  # Advance raw offset, not deduplicated length.
        else:
            index += 1
    raise ValueError('page budget exceeded before completeness')


def check(plan):
    # The host constructs this from an explicit command in the current task.
    result = subprocess.run(plan['argv'], cwd=plan.get('cwd'), capture_output=True, timeout=8)
    if result.returncode:
        raise ValueError('checker failed: ' + result.stderr.decode(errors='replace')[-1500:] + result.stdout.decode(errors='replace')[-1500:])
    output = result.stdout.decode(errors='replace')
    import re
    if re.search(r'(?mi)^\s*(?:FAIL|FAILED|FAILURE|ERROR)\b', output):
        raise ValueError('checker reported failure despite exit code 0')
    tokens = set(re.findall(r'(?m)^\s*TOKEN\s*[:：=]\s*(\S+)\s*$', output))
    if len(tokens) != 1:
        raise ValueError('checker did not return exactly one TOKEN; inspect actual output format')
    return {'answer': {'token': tokens.pop()}, 'complete': True}


def run(payload):
    report = {'request': payload['request'], 'kind': payload['kind'], 'ok': False}
    try:
        report.update({'query': query, 'check': check, 'document': document}[payload['kind']](payload['plan']))
        # Reject NaN/Infinity and overly large results before reporting success.
        encoded = json.dumps(report, ensure_ascii=True, allow_nan=False)
        if len(encoded) > 50000:
            raise ValueError('answer too large')
        report['ok'] = True
    except Exception as exc:
        report.update(error=type(exc).__name__ + ': ' + str(exc)[:2000], complete=False)
        report.pop('answer', None)
    print(REPORT + json.dumps(report, ensure_ascii=True))
