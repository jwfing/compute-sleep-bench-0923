"""Create an isolated Hermes home using only this experiment's credentials."""
import base64
import json
import os
from pathlib import Path
import time


def configure():
    secret_file = Path('/etc/secrets/bench-secrets.json')
    if secret_file.exists():
        allowed = {'BENCH_TOKEN', 'TAVILY_API_KEY', 'HERMES_PROVIDER', 'HERMES_MODEL',
                   'BENCH_CODEX_ACCESS_TOKEN'}
        values = json.loads(secret_file.read_text())
        for key in allowed:
            if values.get(key):
                os.environ[key] = values[key]
    token = os.environ.pop('BENCH_CODEX_ACCESS_TOKEN', '')
    home = Path(os.environ.get('HERMES_HOME', '/tmp/hermes'))
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    if token:
        claims = json.loads(base64.urlsafe_b64decode(token.split('.')[1] + '==='))
        if claims.get('exp', 0) < time.time() + 7200:
            raise RuntimeError('Codex access token must remain valid for at least two hours')
        auth = {'version': 1, 'providers': {}, 'active_provider': 'openai-codex',
                'credential_pool': {'openai-codex': [{
                    'id': 'benchmark', 'label': 'benchmark-access-only', 'source': 'manual',
                    'auth_type': 'oauth', 'priority': 0, 'access_token': token,
                    'base_url': 'https://chatgpt.com/backend-api/codex'}]}}
        p = home / 'auth.json'
        p.write_text(json.dumps(auth))
        p.chmod(0o600)
    config = {
        'model': {'provider': os.environ.get('HERMES_PROVIDER', 'openai-codex'),
                  'default': os.environ.get('HERMES_MODEL', 'gpt-6-luna')},
        'web': {'backend': 'tavily'},
        'compression': {'enabled': False},
        'memory': {'memory_enabled': False, 'user_profile_enabled': False},
        'auxiliary': {'web_extract': {'provider': 'main'}}}
    # JSON is valid YAML; no extra dependency required by the bootstrap.
    (home / 'config.yaml').write_text(json.dumps(config))


if __name__ == '__main__':
    configure()
    os.execvp('python', ['python', 'bench/server.py'])
