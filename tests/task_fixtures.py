"""Synthetic document results for focused state-machine tests, using the wire envelope."""
import base64
import json
import re
import shlex


def helper_payload(command):
    args = shlex.split(command)
    assert args[:2] == ['python3', '-c']
    encoded = re.findall(r"b64decode\('([A-Za-z0-9+/=]+)'\)", args[2])[-1]
    return json.loads(base64.b64decode(encoded))


def document_reply(command, text):
    payload = helper_payload(command)
    assert payload['kind'] == 'document'
    return '[exitCode:0]\n__ZK_TASK_RESULT__=' + json.dumps({
        'request': payload['request'], 'kind': 'document', 'ok': True, 'complete': True,
        'documents': [{'path': '', 'text': text, 'truncated': False}]})
