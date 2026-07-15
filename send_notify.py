import json, urllib.request, urllib.parse, os, sys
from datetime import date

BASE = os.path.dirname(os.path.abspath(__file__))

try:
    cfg    = json.loads(open(os.path.join(BASE, 'bot_config.json'), encoding='utf-8-sig').read())
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
today_iso    = date.today().isoformat()

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


def fmt_signed(val: float) -> str:
    """Like fmt but always shows sign, and blank if zero."""
    if val == 0.0:
        return ''
    sign = '+' if val > 0 else ''
    return f"{sign}{fmt(val)}"


# ── P&L movement section ──────────────────────────────────────────────────────

def _calc_unrealized_pnl() -> dict:
    """Fetch live CMPs via yfinance and return per-client unrealized P&L."""
    try:
        import yfinance as yf
        sys.path.insert(0, BASE)
        from symbol_map import resolve as sym_resolve
    except ImportError as e:
        print(f"P&L movement skipped: {e}")
        return {}

    try:
        overrides = json.load(open(os.path.join(BASE, 'ticker_overrides.json')))
    except Exception:
        overrides = {}

    open_t = [t for t in trades if not t.get('exit_date')]
    if not open_t:
        return {}

    # Resolve all scripts → NSE tickers
    ticker_map = {}
    for t in open_t:
        sc = t.get('script', '')
        if sc not in ticker_map:
            ticker_map[sc] = sym_resolve(sc, overrides)

    unique = list(set(ticker_map.values()))
    cmp_data = {}
    try:
        ns_tickers = [s + '.NS' for s in unique]
        if len(ns_tickers) == 1:
            df = yf.download(ns_tickers, period='1d', interval='1m',
                             progress=False, auto_adjust=True, timeout=20)
            try:
                # yfinance ≥0.2.18 returns MultiIndex even for one ticker
                cmp_data[unique[0]] = float(df['Close'].squeeze().dropna().iloc[-1])
            except Exception:
                pass
        else:
            df = yf.download(ns_tickers, period='1d', interval='1m',
                             progress=False, auto_adjust=True, timeout=20)
            for sym, ns in zip(unique, ns_tickers):
                try:
                    cmp_data[sym] = float(df['Close'][ns].dropna().iloc[-1])
                except Exception:
                    pass
    except Exception as e:
        print(f"yfinance fetch failed: {e}")
        return {}

    pnl = {}
    for t in open_t:
        client = t.get('client', '')
        ticker = ticker_map.get(t.get('script', ''), '')
        cmp    = cmp_data.get(ticker)
        if cmp is None:
            continue
        bp  = float(t.get('buy_price', 0) or 0)
        qty = float(t.get('buy_qty', 0) or 0)
        pnl[client] = pnl.get(client, 0.0) + (cmp - bp) * qty

    return pnl


SNAP_PATH = os.path.join(BASE, 'pnl_snapshot.json')

def _load_snapshot() -> dict:
    try:
        return json.load(open(SNAP_PATH))
    except Exception:
        return {}

def _save_snapshot(pnl: dict):
    try:
        json.dump({'date': today_iso, 'unrealized': pnl}, open(SNAP_PATH, 'w'), indent=2)
    except Exception as e:
        print(f"Could not save P&L snapshot: {e}")


def _pnl_movement_lines() -> list:
    """Build the P&L movement section. Returns [] if data unavailable."""
    today_pnl = _calc_unrealized_pnl()
    if not today_pnl:
        return []

    snap      = _load_snapshot()
    snap_date = snap.get('date', '')
    prev_pnl  = snap.get('unrealized', {})

    # Don't overwrite snapshot if already ran today
    if snap_date != today_iso:
        _save_snapshot(today_pnl)

    has_prev  = bool(prev_pnl) and snap_date != today_iso
    prev_label = snap_date[5:] if has_prev else None  # MM-DD

    # Build rows per client
    rows = []
    for c in CLIENTS:
        cur = today_pnl.get(c, 0.0)
        if has_prev:
            delta = cur - prev_pnl.get(c, 0.0)
            arrow = '▲' if delta >= 0 else '▼'
            rows.append((c, arrow, fmt_signed(delta)))
        else:
            rows.append((c, '▲' if cur >= 0 else '▼', fmt(cur)))

    if not any(r[2] for r in rows):
        return []

    if has_prev:
        total_delta = sum(today_pnl.get(c, 0) - prev_pnl.get(c, 0) for c in CLIENTS)
        header_txt  = f'P&L Movement (vs {prev_label})'
    else:
        total_delta = sum(today_pnl.get(c, 0) for c in CLIENTS)
        header_txt  = 'Unrealized P&L (today)'

    total_str = fmt_signed(total_delta) or '0'
    w0 = max(len(r[0]) for r in rows)
    w1 = max(len(total_str), max((len(r[2]) for r in rows if r[2]), default=0)) or 1
    sep = '─' * (w0 + w1 + 5)

    out = ['', f'`{header_txt}`', f'`{sep}`']
    for c, arrow, val in rows:
        if val:
            out.append(f'`{c:<{w0}}  {arrow} {val:>{w1}}`')
    out.append(f'`{sep}`')
    t_arrow = '▲' if total_delta >= 0 else '▼'
    out.append(f'`{"Total":<{w0}}  {t_arrow} {total_str:>{w1}}`')
    return out


# ── Build message ─────────────────────────────────────────────────────────────

rows = []
for c in CLIENTS:
    d = fmt(ledger.get(c, {}).get('combined', 0.0))
    m = fmt(ledger.get(c, {}).get('mtf', 0.0))
    rows.append((c, d, m))

rows_display = rows
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

# P&L movement
lines.extend(_pnl_movement_lines())

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
