"""Bounded heritage query SOP, shared by sandbox execution and local validation.

No answers or city tables. The schema is learned from an actual API response.
Network calls occur only through the sandbox-supplied request function.
"""
import json
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit


def at(value, path):
    for key in path:
        value = value[key]
    return value


def learn_sop(response):
    if response.get('ok') is not True:
        return None
    parsed = urlsplit(response.get('url', ''))
    query = parse_qs(parsed.query)
    # Only learn an observed city argument, never derive it from the answer.
    city_param = next((k for k in ('location', 'city') if k in query), None)
    if not city_param:
        return None
    pending = [(response.get('data'), [])]
    while pending:
        node, path = pending.pop()
        if not isinstance(node, dict):
            continue
        rows, pagination = node.get('records'), node.get('pagination')
        if (isinstance(rows, list) and rows and isinstance(pagination, dict)
                and all(k in pagination for k in ('total_count', 'offset', 'limit'))
                and all(isinstance(r, dict) and all(k in r for k in
                        ('id', 'name', 'type', 'era', 'protected_level')) for r in rows)):
            return {'version': 1, 'kind': 'heritage_offset',
                    'endpoint': urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', '')),
                    'cityParam': city_param, 'authHeader': response.get('authHeader') or 'X-API-Key',
                    'recordsPath': path + ['records'], 'paginationPath': path + ['pagination'],
                    'pageSize': min(100, max(1, pagination['limit']))}
        pending.extend((v, path + [k]) for k, v in node.items() if isinstance(v, dict))
    return None


def era_rank(era):
    if not isinstance(era, str) or not era:
        raise ValueError('Missing era')
    periods = ['旧石器', '新石器', '先秦', '夏', '商', '西周', '东周', '周', '春秋', '战国',
               '秦', '西汉', '东汉', '汉', '三国', '六朝', '东吴', '魏晋', '西晋', '东晋', '晋',
               '南北朝', '南朝', '北朝', '北魏', '北齐', '隋', '唐',
               '五代', '辽', '北宋', '南宋', '宋', '金', '元', '明', '清', '民国', '现代', '当代']
    matches = [i for i, period in enumerate(periods) if period in era]
    if not matches:
        raise ValueError('Unrecognized era; inspect actual records: ' + era)
    return min(matches)


def validate_pages(config, city, pages):
    """Recompute from page records; never trust a model's counts/complete flag."""
    if config.get('version') != 1 or config.get('kind') != 'heritage_offset':
        raise ValueError('Unsupported SOP version/schema')
    if not isinstance(city, str) or not city or not isinstance(pages, list) or not 1 <= len(pages) <= 30:
        raise ValueError('Missing city/pages')
    records, seen, expected, offset = [], set(), None, 0
    for response in pages:
        if response.get('ok') is not True or response.get('status') != 200:
            raise ValueError('HTTP/business failure')
        payload = response['data']
        if (not isinstance(payload, dict) or payload.get('code') not in (None, 0, 200, '200')
                or payload.get('error') or payload.get('success') is False
                or payload.get('status') in ('error', 'failed', 'failure')):
            raise ValueError('Business failure')
        url = urlsplit(response['url'])
        if urlunsplit((url.scheme, url.netloc, url.path, '', '')) != config['endpoint']:
            raise ValueError('Endpoint changed')
        query = parse_qs(url.query)
        if query.get(config['cityParam']) != [city] or query.get('offset') != [str(offset)]:
            raise ValueError('Wrong city/page request')
        rows = at(payload, config['recordsPath'])
        page = at(payload, config['paginationPath'])
        total, actual_offset, limit = (page[k] for k in ('total_count', 'offset', 'limit'))
        if any(type(v) is not int for v in (total, actual_offset, limit)) or total <= 0 or limit <= 0:
            raise ValueError('Invalid pagination metadata')
        if expected is None:
            expected = total
        if total != expected or actual_offset != offset or offset >= total:
            raise ValueError('Pagination changed or repeated')
        if not isinstance(rows, list) or not rows or len(rows) > limit:
            raise ValueError('Invalid/empty page')
        for row in rows:
            if not isinstance(row, dict) or not all(k in row for k in ('id', 'name', 'type', 'era', 'protected_level')):
                raise ValueError('Record schema changed')
            if not all(isinstance(row[k], str) and row[k] for k in ('name', 'type', 'era', 'protected_level')):
                raise ValueError('Invalid record fields')
            key = json.dumps(row['id'], sort_keys=True)
            if row['id'] is None or key in seen:
                raise ValueError('Duplicate/missing record ID')
            seen.add(key)
            records.append(row)
        offset += len(rows)
    if len(records) != expected:
        raise ValueError('Incomplete pages: %d/%s' % (len(records), expected))
    earliest = min(era_rank(r['era']) for r in records)
    names = {r['name'] for r in records if era_rank(r['era']) == earliest}
    if len(names) != 1:
        raise ValueError('Ambiguous oldest era; inspect records instead of guessing')
    answer = {'city': city, 'total_count': len(records),
              'world_heritage_count': sum(r['protected_level'] == '世界遗产' for r in records),
              'types': sorted({r['type'] for r in records}), 'oldest_era': next(iter(names))}
    return answer, {'successfulCommand': True, 'invalid': False,
                    'expectedCount': expected, 'observedCount': len(records),
                    'uniqueCount': len(seen), 'paginationComplete': True}


def solve_sop(config, city, api_key, request):
    pages, offset, expected = [], 0, None
    header = config['authHeader']
    for _ in range(30):
        query = urlencode({config['cityParam']: city, 'offset': offset, 'limit': config['pageSize']})
        response = request(config['endpoint'] + '?' + query, api_key, header)
        if response.get('ok') is not True:
            raise ValueError('API failed: ' + json.dumps(response, ensure_ascii=False))
        header = response.get('authHeader') or header
        rows = at(response['data'], config['recordsPath'])
        page = at(response['data'], config['paginationPath'])
        total = page['total_count']
        if type(total) is not int or total <= 0 or (expected is not None and total != expected):
            raise ValueError('Invalid/changing total')
        expected = total
        if page['offset'] != offset or not isinstance(rows, list) or not rows:
            raise ValueError('Pagination stalled')
        pages.append(response)
        offset += len(rows)
        if len(json.dumps(pages, ensure_ascii=False).encode('utf-8')) > 48000:
            raise ValueError('Proof exceeds output budget')
        if offset >= total:
            break
    answer, evidence = validate_pages(config, city, pages)
    result = {'kind': 'task_result', 'complete': True, 'answer': answer,
              'evidence': evidence, 'pages': pages}
    if len(json.dumps(result, ensure_ascii=False).encode('utf-8')) > 56000:
        raise ValueError('Result exceeds output budget')
    return result
