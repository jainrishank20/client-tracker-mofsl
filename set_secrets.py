"""
Update all GitHub Actions Secrets from local files.
Run this whenever bot_config.json, gsheet_key.json, or ticker_overrides.json changes.

Usage:
    python set_secrets.py

Requirements: pip install PyNaCl requests
"""
import json, os, base64, urllib.request

BASE    = os.path.dirname(os.path.abspath(__file__))
CFG     = json.load(open(os.path.join(BASE, 'bot_config.json')))
TOKEN   = CFG['github_token']
REPO    = CFG['github_repo']

SECRETS = {
    'BOT_CONFIG':       os.path.join(BASE, 'bot_config.json'),
    'GSHEET_KEY':       os.path.join(BASE, 'gsheet_key.json'),
    'TICKER_OVERRIDES': os.path.join(BASE, 'ticker_overrides.json'),
}

def gh(path, method='GET', data=None):
    url = f'https://api.github.com/repos/{REPO}/{path}'
    req = urllib.request.Request(url, data=data, method=method, headers={
        'Authorization': f'token {TOKEN}',
        'Accept': 'application/vnd.github.v3+json',
        'Content-Type': 'application/json',
    })
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

def set_secret(name, value):
    from nacl import encoding, public
    pub_info = gh('actions/secrets/public-key')
    pub_key  = public.PublicKey(pub_info['key'].encode(), encoding.Base64Encoder)
    box      = public.SealedBox(pub_key)
    encrypted = base64.b64encode(box.encrypt(value.encode())).decode()
    payload   = json.dumps({'encrypted_value': encrypted, 'key_id': pub_info['key_id']}).encode()
    gh(f'actions/secrets/{name}', method='PUT', data=payload)
    print(f'  ✅ {name} updated')

print(f'Updating secrets for {REPO} ...')
for secret_name, filepath in SECRETS.items():
    if not os.path.exists(filepath):
        print(f'  ⚠️  Skipping {secret_name} — {filepath} not found')
        continue
    value = open(filepath).read()
    set_secret(secret_name, value)

print('\nDone. Run a test: send /run to the bot.')
