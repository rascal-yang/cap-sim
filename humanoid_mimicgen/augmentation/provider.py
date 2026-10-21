"""Small Responses transport; no account-session or CLI authentication fallback."""
import json
import os
import re
import time
import urllib.request
from urllib.parse import urlsplit
from .contracts import require
from ..workflow.contracts import WorkflowError, canonical, positive

FIELDS = {'protocol', 'base_url', 'model', 'key_env', 'timeout_s', 'max_rounds', 'max_output_tokens', 'wall_s'}


def validate_config(config):
    canonical(config)
    require(isinstance(config, dict) and FIELDS <= set(config) <= FIELDS | {'reasoning_effort', 'stream'}, 'Provider configuration fields')
    require(config['protocol'] == 'responses', 'Unsupported API protocol')
    if 'reasoning_effort' in config:
        require(config['reasoning_effort'] in ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'), 'Reasoning effort')
    if 'stream' in config:
        require(type(config['stream']) is bool, 'Stream must be boolean')
    url = urlsplit(config['base_url'])
    require(url.scheme == 'https' and url.netloc and not any((url.username, url.password, url.query, url.fragment)),
            'HTTPS endpoint without embedded credentials required')
    require(isinstance(config['model'], str) and config['model'].strip(), 'Explicit model required')
    require(isinstance(config['key_env'], str) and re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', config['key_env']), 'Credential environment name')
    for key in ('timeout_s', 'wall_s'): positive(config[key], key)
    for key in ('max_rounds', 'max_output_tokens'):
        require(type(config[key]) is int and config[key] > 0, key)
    return config


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise WorkflowError('api_unavailable', 'Endpoint redirect refused')


def read_stream(response, timeout_s):
    deadline = time.monotonic()+timeout_s
    total = 0
    data = []
    limit = 16*1024*1024
    while True:
        require(time.monotonic() < deadline, 'Stream time budget exceeded', 'api_unavailable')
        line = response.readline(limit+1)
        total += len(line)
        require(total <= limit, 'API response too large', 'api_unavailable')
        if line.startswith(b'data:'):
            data.append(line[5:].strip())
        if (not line or not line.strip()) and data:
            payload = b'\n'.join(data); data = []
            if payload != b'[DONE]':
                event = json.loads(payload)
                if event.get('type') in ('response.completed', 'response.incomplete', 'response.failed'):
                    require(isinstance(event.get('response'), dict), 'Missing terminal response', 'invalid_api_response')
                    return event['response']
                require(event.get('type') != 'error', 'Provider stream error', 'api_unavailable')
        if not line:
            raise WorkflowError('invalid_api_response', 'Stream ended without terminal response')


class ResponsesProvider:
    def __init__(self, config):
        self.config = validate_config(config.copy())

    def respond(self, input_items, tools, instructions):
        c = self.config
        key = os.environ.get(c['key_env'])
        require(bool(key), 'Configured API credential absent', 'api_unavailable')
        body = {'model': c['model'], 'input': input_items, 'tools': tools, 'instructions': instructions,
                'store': False, 'parallel_tool_calls': False, 'max_output_tokens': c['max_output_tokens']}
        if 'reasoning_effort' in c:
            body['reasoning'] = {'effort': c['reasoning_effort']}
        if c.get('stream'):
            body['stream'] = True
        request = urllib.request.Request(c['base_url'].rstrip('/')+'/responses', data=canonical(body),
            headers={'User-Agent': 'cap-sim-augmentation/1.0', 'Content-Type': 'application/json', 'Authorization': 'Bearer '+key})
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=c['timeout_s']) as response:
                if c.get('stream'):
                    result = read_stream(response, c['timeout_s'])
                else:
                    raw = response.read(16*1024*1024+1)
                    require(len(raw) <= 16*1024*1024, 'API response too large', 'api_unavailable')
                    result = json.loads(raw)
            reported = result.get('model')
            require(isinstance(reported, str) and (reported == c['model'] or reported.startswith(c['model']+'-')),
                    'Reported model differs from requested model', 'invalid_api_response')
            return result
        except WorkflowError:
            raise
        except Exception as exc:
            # Never persist request headers or arbitrary provider exception bodies.
            raise WorkflowError('api_unavailable', type(exc).__name__) from None
