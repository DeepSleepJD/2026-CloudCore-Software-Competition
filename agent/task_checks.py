"""Small task contracts and evidence checks, independent of a particular task set."""
import json
import math
import re
import shlex

REPORT = '__ZK_TASK_RESULT__='


def identity(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return value.strip()
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def describe(text):
    """Only a labelled submission section supplies an example, never API examples."""
    lower = text.lower()
    family = 'unknown'
    if re.search(r'修复|部署|repair|deployment', lower) and re.search(r'验收|check|verify', lower):
        family = 'engineering'
    elif re.search(r'https?://', text) and re.search(r'查询|统计|query|records|pagination', lower):
        family = 'query'
    example = None
    section = re.search(r'(?mi)^#{1,6}\s*(?:答案格式|提交格式|提交规则|答案输出格式|submission format|answer format)[^\n]*\n', text)
    if section:
        body = re.split(r'(?m)^#{1,6}\s', text[section.end():], maxsplit=1)[0]
        for sample in re.findall(r'```(?:json)?\s*\n(.*?)```', body, re.S | re.I):
            try:
                value = json.loads(sample)
                if isinstance(value, (dict, list)):
                    example = value
                    break
            except ValueError:
                continue  # Placeholder examples do not establish a machine-checkable shape.
    # Narrow automatic checker: both command and optional cwd must be explicit.
    checker = None
    if family == 'engineering':
        snippets = re.findall(r'`([^`\n]+)`', text)
        directories = set()
        for snippet in snippets:
            try:
                argv = shlex.split(snippet)
            except ValueError:
                continue
            if len(argv) == 2 and argv[0] == 'cd':
                directories.add(argv[1])
        candidates = []
        for snippet in snippets:
            try:
                argv = shlex.split(snippet)
            except ValueError:
                continue
            if len(argv) == 1 and argv[0] in ('./check', './verify', './check.sh', './verify.sh'):
                candidates.append(argv)
            elif len(argv) == 2 and argv[0] in ('python', 'python3', 'bash', 'sh') and re.fullmatch(r'(?:\./)?(?:check|verify)\.(?:py|sh)', argv[1]):
                candidates.append(argv)
        # Multiple distinct commands are ambiguous; leave them to ordinary exploration.
        unique = {tuple(a) for a in candidates}
        if len(unique) == 1 and len(directories) <= 1 and isinstance(example, dict) and set(example) == {'token'} and 'TOKEN' in text:
            checker = {'argv': list(next(iter(unique))), 'cwd': next(iter(directories), None)}
    return {'family': family, 'example': example, 'checker': checker}


def shape_error(answer, example):
    if example is None:
        return ''
    if isinstance(answer, str):
        try:
            answer = json.loads(answer)
        except ValueError:
            return '提交要求JSON；当前答案无法解析。'

    def check(value, sample, path):
        if isinstance(sample, dict):
            if not isinstance(value, dict) or not value:
                return path + '需要非空对象。'
            extra = value.keys() - sample.keys()
            if extra:
                return path + '含未要求的字段：' + ','.join(sorted(extra))
            # Partial fields may earn partial credit; never invent missing values.
            for key in value:
                error = check(value[key], sample[key], path + '.' + key)
                if error:
                    return error
        elif isinstance(sample, list):
            if not isinstance(value, list):
                return path + '需要数组。'
        elif sample is not None:
            if type(sample) in (int, float):
                if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)):
                    return path + '需要有限数字，不能用字符串或布尔值。'
            elif type(value) is not type(sample):
                return path + '的类型不符合提交示例。'
        return ''
    return check(answer, example, 'answer')


def query_observation(output):
    """Recognize explicit failures/incomplete pages; unknown formats remain unknown."""
    if not output.startswith('[exitCode:0]\n') or '[TRUNCATED]' in output:
        return '命令失败或输出截断，不能据此认定查询完成。'
    body = output.split('\n', 1)[1].strip()
    try:
        data = json.loads(body)
    except ValueError:
        return ''
    if not isinstance(data, dict):
        return ''
    if data.get('error') or data.get('success') is False or data.get('code') in (400, 401, 403, 404, 429, 500, '401', '403'):
        return '接口明确返回错误，不能当作空数据。'
    envelope = data.get('data', data)
    if isinstance(envelope, dict) and isinstance(envelope.get('records'), list):
        page = envelope.get('pagination') or data.get('pagination') or {}
        if isinstance(page, dict):
            total = page.get('total_count', page.get('total'))
            if type(total) is int and total != len(envelope['records']):
                return '当前记录数与总量不符；必须取齐分页，不能只统计本页。'
            if page.get('has_next') is True:
                return '接口仍有下一页，当前结果不完整。'
    return ''


WORKFLOWS = {
    'query': '读取本题接口要求→优先query工具取齐数据并聚合→检查字段→提交。失败与空结果不同；非GET或特殊统计可使用command，但必须自行取齐并校验。',
    'engineering': '读取本题spec→command修复当前工作区→执行本题明确指定的验收→提交真实token。不得修改验收程序；不要自己编造token。',
    'unknown': '按本题要求探索；已有足够证据就answer，不强行套用查询或修复流程。',
}
