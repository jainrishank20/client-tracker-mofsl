import json, urllib.request, urllib.parse, os
from datetime import date

BASE   = os.path.dirname(os.path.abspath(__file__))
cfg    = json.load(open(os.path.join(BASE, 'bot_config.json')))
ledger = json.load(open(os.path.join(BASE, 'ledger.json')))
trades = json.load(open(os.path.join(BASE, 'trades.json')))

CLIENTS = ['RIMK1205','RIMK1209','RIMK1215','RIMK1220','RIMK1238',
           'RIMK1247','RIMK1248','RIMK1249','RIMK1252','RIMK1256']

open_count   = sum(1 for t in trades if not t.get('exit_date'))
closed_count = sum(1 for t in trades if t.get('exit_date'))
today        = date.today().strftime('%d %b %Y')


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

w0 = max(len(r[0]) for r in rows)
w1 = max(len('Delivery'), max(len(r[1]) for r in rows))
w2 = max(len('MTF'),      max(len(r[2]) for r in rows))


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
for c, d, m in rows:
    lines.append(f'`{row_line(c, d, m)}`')
lines.append(f'`{sep}`')

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
