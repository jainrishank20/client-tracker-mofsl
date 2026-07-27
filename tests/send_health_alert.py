"""
Post-pipeline Telegram health alert.
Fires on every run (success AND failure) so data issues are caught immediately.
"""
import json, os, sys, argparse, urllib.request, urllib.parse
from collections import defaultdict

parser = argparse.ArgumentParser()
parser.add_argument('--status', default='unknown')
parser.add_argument('--full',   default='false')
parser.add_argument('--sync-gsheet-outcome',  default='skipped')
parser.add_argument('--fetch-pnl-outcome',    default='skipped')
parser.add_argument('--sync-vm-outcome',      default='skipped')
parser.add_argument('--push-vm-outcome',      default='skipped')
args = parser.parse_args()

BASE        = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_PATH    = os.path.join(BASE, 'bot_config.json')
TRADES_PATH = os.path.join(BASE, 'trades.json')

cfg = json.loads(open(CFG_PATH, encoding='utf-8-sig').read())

# ── Build per-client summary ──────────────────────────────────────────────────
trades_ok   = os.path.exists(TRADES_PATH)
trades      = json.load(open(TRADES_PATH)) if trades_ok else []

by_client   = defaultdict(list)
for t in trades:
    by_client[t['client']].append(t)

clients     = list(cfg.get('clients', {}).keys())
names       = cfg.get('clients', {})

total_open   = sum(1 for t in trades if not t.get('exit_date'))
total_closed = sum(1 for t in trades if t.get('exit_date'))
total        = len(trades)

status_icon = '✅' if args.status == 'success' else '❌'
mode_tag    = ' `[FULL]`' if args.full == 'true' else ''

lines = [f"{status_icon} *Pipeline complete*{mode_tag}",
         f"`{total} trades  |  {total_open} open  |  {total_closed} closed`",
         ""]

# Per-client table
lines.append("`Client          Open  Closed`")
lines.append("`─────────────────────────────`")

duplicate_detected = False
open_sigs = {}
for c in clients:
    ts  = by_client.get(c, [])
    op  = sum(1 for t in ts if not t.get('exit_date'))
    cl  = sum(1 for t in ts if t.get('exit_date'))
    name = names.get(c, c)[:12]
    flag = ''

    # Duplicate-data detector
    if op > 0:
        sig = frozenset(t['script'] for t in ts if not t.get('exit_date'))
        if sig in open_sigs:
            flag = ' ⚠️DUP'
            duplicate_detected = True
        else:
            open_sigs[sig] = c

    # Zero-trade detector
    if len(ts) == 0:
        flag = ' ❌MISSING'

    lines.append(f"`{c}  {name:<12}  {op:>4}  {cl:>6}{flag}`")

lines.append("`─────────────────────────────`")

if duplicate_detected:
    lines.append("")
    lines.append("⚠️ *DUPLICATE DATA DETECTED* — downloader gave wrong CSV to at least one account\\. Check pipeline logs\\.")

if not trades_ok or total == 0:
    lines.append("")
    lines.append("❌ *trades\\.json is EMPTY* — no data synced to GSheet or VM\\.")

# ── SSH/VM step outcomes ──────────────────────────────────────────────────────
def _outcome_icon(o):
    return '✅' if o == 'success' else ('⏭' if o in ('skipped', '') else '❌')

ssh_lines = []
if args.sync_gsheet_outcome not in ('success', 'skipped', ''):
    ssh_lines.append(f"  {_outcome_icon(args.sync_gsheet_outcome)} GSheet sync: {args.sync_gsheet_outcome}")
if args.push_vm_outcome not in ('success', 'skipped', ''):
    ssh_lines.append(f"  {_outcome_icon(args.push_vm_outcome)} Push to VM: {args.push_vm_outcome} \\(bot may have stale data\\)")
if args.sync_vm_outcome not in ('success', 'skipped', ''):
    ssh_lines.append(f"  {_outcome_icon(args.sync_vm_outcome)} VM config sync: {args.sync_vm_outcome}")
if ssh_lines:
    lines.append("")
    lines.append("⚠️ *Step failures \\(SSH may be blocked\\):*")
    lines.extend(ssh_lines)

msg = '\n'.join(lines)

def send(token, chat_id, text):
    data = urllib.parse.urlencode({
        'chat_id': chat_id, 'text': text, 'parse_mode': 'MarkdownV2'
    }).encode()
    try:
        urllib.request.urlopen(
            urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data),
            timeout=15
        )
    except Exception as e:
        # Fallback: plain text
        try:
            data2 = urllib.parse.urlencode({
                'chat_id': chat_id,
                'text': text.replace('*','').replace('`','').replace('\\',''),
            }).encode()
            urllib.request.urlopen(
                urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data2),
                timeout=15
            )
        except Exception as e2:
            print(f"Health alert send failed (both MarkdownV2 and plain): {e2}")

chat_ids = [c.strip() for c in str(cfg['allowed_chat_id']).split(',')]
for cid in chat_ids:
    send(cfg['telegram_token'], cid, msg)
    print(f"Health alert sent to {cid}")

print(msg.replace('`',''))
