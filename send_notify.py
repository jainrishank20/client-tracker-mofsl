import json, urllib.request, urllib.parse, os
from datetime import date

BASE   = os.path.dirname(os.path.abspath(__file__))
cfg    = json.load(open(os.path.join(BASE, 'bot_config.json')))
trades = json.load(open(os.path.join(BASE, 'trades.json')))

open_t   = [t for t in trades if not t.get('exit_date')]
closed_t = [t for t in trades if t.get('exit_date')]

pnl = {}
for t in closed_t:
    p = (t.get('sell_price', 0) - t.get('buy_price', 0)) * t.get('buy_qty', 0)
    pnl[t['client']] = pnl.get(t['client'], 0) + p

NAMES = {
    'RIMK1205': 'Siva Sankara Reddy',
    'RIMK1209': 'Sathyavrath',
    'RIMK1215': 'Malleswari',
    'RIMK1220': 'Kalpana',
    'RIMK1238': 'Iranna',
    'RIMK1247': 'Srujana',
    'RIMK1248': 'Udayakumar',
    'RIMK1249': 'Sundareshwari',
    'RIMK1252': 'Savitha',
    'RIMK1256': 'Sheeba',
}

today = date.today().strftime('%d %b %Y')
lines = [
    '✅ Daily Update Done',
    f"{len(trades)} trades ({len(open_t)} open, {len(closed_t)} closed)",
    f'GSheet synced {today}',
    '',
]
for c, name in NAMES.items():
    p  = pnl.get(c, 0)
    op = sum(1 for t in open_t   if t['client'] == c)
    cl = sum(1 for t in closed_t if t['client'] == c)
    sign = '+' if p >= 0 else ''
    lines.append(f"{name}: {sign}Rs.{int(p):,} ({cl} closed, {op} open)")

msg  = '\n'.join(lines)
data = urllib.parse.urlencode({'chat_id': cfg['allowed_chat_id'], 'text': msg}).encode()
req  = urllib.request.Request(
    f"https://api.telegram.org/bot{cfg['telegram_token']}/sendMessage", data=data)
urllib.request.urlopen(req, timeout=15)
print('Sent!')
