"""Task evidence in private JSONL files and reconstructable stdout records.

The payloads intentionally include the actual task, model and sandbox content.
Treat these logs like match credentials; do not publish them without review.
"""
import hashlib
import json
import logging
from pathlib import Path
import time
import uuid

LOG = logging.getLogger(__name__)
PREFIX = 'TASK_TRACE '


class TaskTrace:
    MAX_TEXT = 262144
    MAX_FILE = 8 * 1024 * 1024
    BACKUPS = 4
    CHUNK = 3000

    def __init__(self, cache_path):
        self.run_id = uuid.uuid4().hex[:16]
        self.path = Path(cache_path).with_name(Path(cache_path).stem + '_' + self.run_id + '_trace.jsonl')
        self.sequence = 0
        self.task_number = 0
        self.task_id = None
        self.task_open = False
        self.calls = {}
        self.exit_reason = None

    def start(self, round_no, **data):
        self.task_number += 1
        self.task_id = self.run_id + ':' + str(self.task_number)
        self.task_open = True
        self.exit_reason = None
        self.calls = {}
        self.emit('task_attempt', round_no, **data)

    def end(self, round_no, reason, **data):
        if self.task_open:
            self.emit('task_end', round_no, reason=reason, exit_reason=self.exit_reason, **data)
        self.task_open = False
        self.calls = {}

    def _bounded(self, value, path, clipped):
        if isinstance(value, str) and len(value) > self.MAX_TEXT:
            clipped.append({'path': path, 'characters': len(value), 'kept': self.MAX_TEXT,
                            'sha256': hashlib.sha256(value.encode('utf-8', errors='replace')).hexdigest()})
            return value[:self.MAX_TEXT]
        if isinstance(value, dict):
            return {str(k): self._bounded(v, path + '/' + str(k), clipped) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._bounded(v, path + '/' + str(i), clipped) for i, v in enumerate(value)]
        return value

    def emit(self, event, round_no, **data):
        # Observability must never turn a playable round into an empty response.
        try:
            self.sequence += 1
            clipped = []
            entry = {'schema': 1, 'agent_version': 'v7.4-jd', 'run_id': self.run_id, 'task_id': self.task_id,
                     'event_id': self.run_id + ':' + str(self.sequence), 'event': event,
                     'round': round_no, 'time_ns': time.time_ns(),
                     **self._bounded(data, '', clipped), 'clipped': clipped}
            raw = json.dumps(entry, ensure_ascii=True, separators=(',', ':'))
            # Stdout remains available if the filesystem is full or read-only.
            try:
                if len(raw) <= self.CHUNK:
                    LOG.info('%s%s', PREFIX, raw)
                else:
                    total = (len(raw) + self.CHUNK - 1) // self.CHUNK
                    for i in range(total):
                        chunk = {'event': 'trace_chunk', 'event_id': entry['event_id'],
                                 'part': i + 1, 'parts': total, 'data': raw[i*self.CHUNK:(i+1)*self.CHUNK]}
                        LOG.info('%s%s', PREFIX, json.dumps(chunk, ensure_ascii=True, separators=(',', ':')))
            except Exception:
                pass
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size + len(raw) + 1 > self.MAX_FILE:
                    for i in range(self.BACKUPS, 0, -1):
                        source = Path(str(self.path) + '.' + str(i-1)) if i > 1 else self.path
                        if source.exists():
                            source.replace(Path(str(self.path) + '.' + str(i)))
                with self.path.open('a', encoding='utf-8') as stream:
                    stream.write(raw + '\n')
            except OSError:
                LOG.warning('task trace file unavailable; evidence is also printed with TASK_TRACE')
            return entry['event_id']
        except Exception:
            return None

    def response(self, round_no, response, pioneer_id, duplicate=False):
        actions = response.get('roleCommandMap') or {}
        action = actions.get(str(pioneer_id), actions.get(pioneer_id))
        if not self.task_open and not (action and action.get('action') in ('acceptTask', 'submitAnswer')):
            return
        payload = {'prompt': response.get('prompt') or '', 'executeCmd': response.get('executeCmd') or '',
                   'pioneer_action': action, 'duplicate': duplicate}
        event_id = self.emit('response', round_no, **payload)
        if not duplicate:
            self.calls = {key: {'event_id': event_id, 'round': round_no} for key in ('prompt', 'executeCmd') if payload[key]}

    def receive(self, world, pending):
        correlations = {key: {**call, 'round_gap': world.round - call['round']} for key, call in self.calls.items()}
        self.emit('input', world.round, pending=pending, replies_to=correlations,
                  task=world.data.get('phaseTask') or '', llm_response=world.data.get('llmResp') or '',
                  command_result=world.data.get('lastCmdResult') or '', errors=world.data.get('errors') or [],
                  role_results=world.data.get('lastRoundRoleActionResults') or {}, pioneer=world.pioneer)
        self.calls = {}
