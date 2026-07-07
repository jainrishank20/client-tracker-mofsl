import json, urllib.request, urllib.parse, os
from datetime import date

BASE = os.path.dirname(os.path.abspath(__file__))

try:
    cfg    = json.load(open(os.path.join(BASE, 'bot_config.json')))
    ledger = json.load(open(os.path.join(BASE, 'ledger.json')))
    trades = json.load(open(os.path.join(BASE, 'trades.json')))
except FileNotFoundError as e:
    print(f"ERROR: {e} — aborting notify")
    raise SystemExit(1)

# clients list from config, fallback to ledger keys
CLIENTS = list(cfg.get('clients', {}).keys()) or sorted(ledger.keys())

open_count   = sum(1 for t in trades if not t.get('exit_date'))
closed_count = sum(1 for t in trades if t.get('exit_date'))
today        = date.today().strftime('%d %b %Y')

# Per-client open position counts (for health check footer)
from collections import defaultdict as _dd
_by_client = _dd(lambda: {'open': 0, 'closed': 0})
for t in trades:
    key = 'open' if not t.get('exit_date') else 'closed'
    _by_client[t['client']][key] += 1


def fmt(val: float) -> str:
    """Indian number format, blank if zero. e.g. -58,71,034"""
    if val == 0.0:
        return ''
    sign  = '-' if val < 0 else ''
    abval = abs(int(round(val)))
    s     = str(abval)
    if len(s) <= 3:
        return f"{sign}{s}"
    head, tail = s[:-3], s[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    groups.insert(0, head)
    return f"{sign}{','.join(groups)},{tail}"


rows = []
for c in CLIENTS:
    d = fmt(ledger.get(c, {}).get('combined', 0.0))
    m = fmt(ledger.get(c, {}).get('mtf', 0.0))
    rows.append((c, d, m))

rows_display = rows  # use client codes — short, fixed width, fits mobile
w0 = max(len(r[0]) for r in rows_display) if rows_display else 6
w1 = max(len('Delivery'), max((len(r[1]) for r in rows_display), default=0))
w2 = max(len('MTF'),      max((len(r[2]) for r in rows_display), default=0))


def row_line(c, d, m):
    return f"{c:<{w0}}  {d:>{w1}}  {m:>{w2}}"


header = row_line('Client', 'Delivery', 'MTF')
sep    = '─' * len(header)

lines = [
    f'📊 *Ledger Balance — {today}*',
    f'`{sep}`',
    f'`{header}`',
    f'`{sep}`',
]
for name, d, m in rows_display:
    lines.append(f'`{row_line(name, d, m)}`')
lines.append(f'`{sep}`')

# ── Trade count footer ────────────────────────────────────────────────────────
lines.append('')
lines.append(f'`{"Trades":─<38}`')
lines.append(f'`{"Client":<10}  {"Open":>5}  {"Closed":>6}`')
for c in CLIENTS:
    op = _by_client[c]['open']
    cl = _by_client[c]['closed']
    flag = '  ⚠' if op == 0 and cl == 0 else ''
    lines.append(f'`{c:<10}  {op:>5}  {cl:>6}{flag}`')
lines.append(f'`{"Total":<10}  {open_count:>5}  {closed_count:>6}`')
lines.append(f'`{"":─<38}`')

msg = '\n'.join(lines)


def send(token, chat_id):
    data = urllib.parse.urlencode({
        'chat_id':    chat_id,
        'text':       msg,
        'parse_mode': 'Markdown',
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    urllib.request.urlopen(req, timeout=15)


# Send to all chat IDs (comma-separated in config)
chat_ids = [c.strip() for c in str(cfg['allowed_chat_id']).split(',')]
for chat_id in chat_ids:
    send(cfg['telegram_token'], chat_id)
    print(f'Sent to {chat_id}!')

print(msg)
