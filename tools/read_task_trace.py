"""Recover TASK_TRACE records from a platform stdout log or private JSONL files."""
import argparse
import json
from pathlib import Path
import sys

PREFIX = 'TASK_TRACE '


def decode(lines):
    events, chunks, issues = {}, {}, []
    for line_no, line in enumerate(lines, 1):
        at = line.find(PREFIX)
        raw = line[at + len(PREFIX):] if at >= 0 else line.strip()
        if at < 0 and not raw.startswith('{'):
            continue
        try:
            value = json.loads(raw)
        except ValueError:
            if at >= 0:
                issues.append('Malformed/truncated trace line ' + str(line_no))
            continue
        if not isinstance(value, dict) or not value.get('event_id'):
            continue
        if value.get('event') == 'trace_chunk':
            event_id = value['event_id']
            part, count, data = value.get('part'), value.get('parts'), value.get('data')
            if type(part) is not int or type(count) is not int or not 1 <= part <= count <= 20000 or not isinstance(data, str):
                issues.append('Invalid chunk ' + event_id)
                continue
            group = chunks.setdefault(event_id, {'count': count, 'parts': {}})
            if group['count'] != count or (part in group['parts'] and group['parts'][part] != data):
                issues.append('Conflicting chunk ' + event_id)
            group['parts'][part] = data
        else:
            events[value['event_id']] = value
    for event_id, group in chunks.items():
        if event_id in events:
            continue  # A full JSONL record can replace missing stdout chunks.
        if len(group['parts']) != group['count']:
            issues.append('Incomplete event ' + event_id + ': %s/%s chunks' % (len(group['parts']), group['count']))
            continue
        try:
            value = json.loads(''.join(group['parts'][i] for i in range(1, group['count'] + 1)))
            if not isinstance(value, dict) or value.get('event_id') != event_id:
                raise ValueError('event identity mismatch')
            events[event_id] = value
        except (ValueError, KeyError):
            issues.append('Corrupted event ' + event_id)
    result = sorted(events.values(), key=lambda e: (e.get('time_ns', 0), e['event_id']))
    return result, issues


def summarize(events):
    tasks = {}
    for event in events:
        task_id = event.get('task_id')
        if not task_id:
            continue
        task = tasks.setdefault(task_id, {'task_id': task_id, 'rounds': [], 'prompt_calls': 0,
                                         'command_calls': 0, 'submissions': [], 'errors': [],
                                         'stops': [], 'end': 'not_observed_in_log', 'clipped_events': []})
        task['rounds'].append(event['round'])
        if event.get('clipped'):
            task['clipped_events'].append(event['event_id'])
        if event['event'] == 'response' and not event.get('duplicate'):
            task['prompt_calls'] += bool(event.get('prompt'))
            task['command_calls'] += bool(event.get('executeCmd'))
        elif event['event'] == 'submission':
            task['submissions'].append({'round': event['round'], 'answer': event.get('answer')})
        elif event['event'] == 'input':
            for error in event.get('errors') or []:
                task['errors'].append({'round': event['round'], **error})
        elif event['event'] in ('survival_action', 'work_stopped'):
            task['stops'].append({'round': event['round'], 'reason': event.get('reason')})
        elif event['event'] == 'task_end':
            task['end'] = event['reason']
            task['exit_reason'] = event.get('exit_reason')
    for task in tasks.values():
        task['rounds'] = [min(task['rounds']), max(task['rounds'])]
    return list(tasks.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('logs', type=Path, nargs='+')
    parser.add_argument('--output', type=Path, help='Write reconstructed full events as UTF-8 JSONL')
    args = parser.parse_args()
    def lines():
        for path in args.logs:
            with path.open(encoding='utf-8', errors='replace') as stream:
                yield from stream
    events, issues = decode(lines())
    if args.output:
        args.output.write_text(''.join(json.dumps(e, ensure_ascii=True) + '\n' for e in events), encoding='utf-8')
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
    print(json.dumps({'events': len(events), 'trace_issues': issues, 'tasks': summarize(events)}, ensure_ascii=False, indent=2))
    return 1 if issues else 0


if __name__ == '__main__':
    raise SystemExit(main())
