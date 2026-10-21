"""Small Responses transport; no account-session or CLI authentication fallback."""
import json
import os
import re
import urllib.request
from urllib.parse import urlsplit
from .contracts import require
from ..workflow.contracts import WorkflowError, canonical, positive

FIELDS = {'protocol', 'base_url', 'model', 'key_env', 'timeout_s', 'max_rounds', 'max_output_tokens', 'wall_s'}


def validate_config(config):
    canonical(config)
    require(isinstance(config, dict) and set(config) == FIELDS, 'Provider configuration fields')
    require(config['protocol'] == 'responses', 'Unsupported API protocol')
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


class ResponsesProvider:
    def __init__(self, config):
        self.config = validate_config(config.copy())

    def respond(self, input_items, tools, instructions):
        c = self.config
        key = os.environ.get(c['key_env'])
        require(bool(key), 'Configured API credential absent', 'api_unavailable')
        body = {'model': c['model'], 'input': input_items, 'tools': tools, 'instructions': instructions,
                'store': False, 'parallel_tool_calls': False, 'max_output_tokens': c['max_output_tokens']}
        request = urllib.request.Request(c['base_url'].rstrip('/')+'/responses', data=canonical(body),
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer '+key})
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=c['timeout_s']) as response:
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
