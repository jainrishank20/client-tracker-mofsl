import json, urllib.request, urllib.parse, os, sys
from datetime import date

BASE = os.path.dirname(os.path.abspath(__file__))

try:
    cfg    = json.loads(open(os.path.join(BASE, 'bot_config.json'), encoding='utf-8-sig').read())
    trades = json.load(open(os.path.join(BASE, 'trades.json')))
except FileNotFoundError as e:
    print(f"ERROR: {e} — aborting notify")
    try:
        _c = json.loads(open(os.path.join(BASE, 'bot_config.json'), encoding='utf-8-sig').read())
        _tok = _c.get('telegram_token') or _c.get('token', '')
        _cid = str(_c.get('chat_id', ''))
        if _tok and _cid:
            import urllib.request as _ur
            _ur.urlopen(f"https://api.telegram.org/bot{_tok}/sendMessage",
                        data=f'chat_id={_cid}&text=❌ Pipeline ERROR: {e} — trades.json missing, pipeline failed.'.encode(),
                        timeout=10)
    except Exception:
        pass
    raise SystemExit(1)

try:
    ledger = json.load(open(os.path.join(BASE, 'ledger.json')))
except FileNotFoundError:
    print("WARNING: ledger.json missing — ledger columns will show as N/A")
    ledger = {}

# clients list from config, fallback to ledger keys
CLIENTS = list(cfg.get('clients', {}).keys()) or sorted(ledger.keys())

# Count unique open companies (net buy_qty - sell_qty > 0 per client+script pair)
from collections import defaultdict as _dd
_net = _dd(float)
_closed_scripts = _dd(set)
for t in trades:
    if not t.get('exit_date'):
        _net[(t['client'], t['script'])] += (t.get('buy_qty') or 0) - (t.get('sell_qty') or 0)
    else:
        _closed_scripts[t['client']].add(t['script'])

open_count   = sum(1 for v in _net.values() if v > 0)
closed_count = len(set(s for ss in _closed_scripts.values() for s in ss))
today        = date.today().strftime('%d %b %Y')
today_iso    = date.today().isoformat()

# Current FY start (April 1)
_td = date.today()
FY_START = date(_td.year if _td.month >= 4 else _td.year - 1, 4, 1)

# Per-client open position counts (unique companies per client)
_by_client = _dd(lambda: {'open': 0, 'closed': 0})
_client_net = _dd(float)
for (client, script), qty in _net.items():
    if qty > 0:
        _by_client[client]['open'] += 1
for client, scripts in _closed_scripts.items():
    _by_client[client]['closed'] = len(scripts)


def fmt(val: float) -> str:
    """Indian number format, blank if zero. e.g. -58,71,034.50"""
    if val == 0.0:
        return ''
    sign  = '-' if val < 0 else ''
    abval = round(abs(val), 2)
    int_v = int(abval)
    frac  = round(abval - int_v, 2)
    s     = str(int_v)
    if len(s) <= 3:
        base = f"{sign}{s}"
        if frac >= 0.005:
            base += f".{round(frac * 100):02d}"
        return base
    head, tail = s[:-3], s[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    groups.insert(0, head)
    base = f"{sign}{','.join(groups)},{tail}"
    if frac >= 0.005:
        base += f".{round(frac * 100):02d}"
    return base


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
        overrides = json.loads(open(os.path.join(BASE, 'ticker_overrides.json'), encoding='utf-8-sig').read())
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
            df = yf.download(ns_tickers, period='5d', interval='1d',
                             progress=False, auto_adjust=True, timeout=20)
            try:
                # yfinance ≥0.2.18 returns MultiIndex even for one ticker
                cmp_data[unique[0]] = float(df['Close'].squeeze().dropna().iloc[-1])
            except Exception:
                try:
                    info = yf.Ticker(ns_tickers[0]).fast_info
                    price = getattr(info, 'last_price', None) or getattr(info, 'previous_close', None)
                    if price:
                        cmp_data[unique[0]] = float(price)
                except Exception:
                    pass
        else:
            df = yf.download(ns_tickers, period='5d', interval='1d',
                             progress=False, auto_adjust=True, timeout=20)
            for sym, ns in zip(unique, ns_tickers):
                try:
                    cmp_data[sym] = float(df['Close'][ns].dropna().iloc[-1])
                except Exception:
                    try:
                        info = yf.Ticker(ns).fast_info
                        price = getattr(info, 'last_price', None) or getattr(info, 'previous_close', None)
                        if price:
                            cmp_data[sym] = float(price)
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


# Compute once, shared by both movement and summary sections
_unrealized_cache = None
def _get_unrealized():
    global _unrealized_cache
    if _unrealized_cache is None:
        _unrealized_cache = _calc_unrealized_pnl()
    return _unrealized_cache


def _pnl_summary_lines() -> list:
    """Realized (FY to date) + Unrealized + Total per client."""
    # Realized: sum net_pnl of closed trades in current FY
    realized = {}
    for t in trades:
        if t.get('exit_date') and t.get('net_pnl') is not None:
            try:
                if date.fromisoformat(t['exit_date']) >= FY_START:
                    c = t['client']
                    realized[c] = realized.get(c, 0.0) + float(t['net_pnl'])
            except (ValueError, TypeError):
                pass

    unrealized = _get_unrealized()

    rows = []
    tot_r = tot_u = tot_t = 0.0
    for c in CLIENTS:
        r = realized.get(c, 0.0)
        u = unrealized.get(c, 0.0)
        t_val = r + u
        tot_r += r; tot_u += u; tot_t += t_val
        rows.append((c, r, u, t_val))

    if not any(r[1] or r[2] for r in rows):
        return []

    def _col(v):
        return fmt_signed(round(v)) if v else ''

    w0 = max(len(r[0]) for r in rows)
    w1 = max(len('Realized'), max((len(_col(r[1])) for r in rows), default=0))
    w2 = max(len('Total'),    max((len(_col(r[3])) for r in rows), default=0))
    sep = '─' * (w0 + w1 + w2 + 6)

    out = ['', f'`📈 P&L Summary (FY {FY_START.year}-{FY_START.year % 100 + 1})`',
           f'`{sep}`',
           f'`{"Client":<{w0}}  {"Realized":>{w1}}  {"Total":>{w2}}`',
           f'`{sep}`']
    for c, r, u, t_val in rows:
        out.append(f'`{c:<{w0}}  {_col(r):>{w1}}  {_col(t_val):>{w2}}`')
    out.append(f'`{sep}`')
    out.append(f'`{"Total":<{w0}}  {_col(tot_r):>{w1}}  {_col(tot_t):>{w2}}`')
    return out


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
    today_pnl = _get_unrealized()
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
            rows.append((c, arrow, fmt_signed(round(delta))))
        else:
            rows.append((c, '▲' if cur >= 0 else '▼', fmt(round(cur))))

    if not any(r[2] for r in rows):
        return []

    if has_prev:
        total_delta = sum(today_pnl.get(c, 0) - prev_pnl.get(c, 0) for c in CLIENTS)
        header_txt  = f'P&L Movement (vs {prev_label})'
    else:
        total_delta = sum(today_pnl.get(c, 0) for c in CLIENTS)
        header_txt  = 'Unrealized P&L (today)'

    total_str = fmt_signed(round(total_delta)) or '0'
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
    d = fmt(round(ledger.get(c, {}).get('combined', 0.0)))
    m = fmt(round(ledger.get(c, {}).get('mtf', 0.0)))
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

# P&L summary (realized + unrealized + total)
lines.extend(_pnl_summary_lines())

# P&L movement (day-over-day change)
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


def _send_chunks(token, chat_id, text):
    MAX = 4000
    for i in range(0, len(text), MAX):
        chunk = text[i:i+MAX]
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=urllib.parse.urlencode({'chat_id': chat_id, 'text': chunk, 'parse_mode': 'Markdown'}).encode(),
            timeout=15
        )


def send(token, chat_id):
    _send_chunks(token, chat_id, msg)


# Send to all chat IDs (comma-separated in config)
chat_ids = [c.strip() for c in str(cfg['allowed_chat_id']).split(',')]
for chat_id in chat_ids:
    try:
        send(cfg['telegram_token'], chat_id)
        print(f'Sent to {chat_id}!')
    except Exception as e:
        print(f'Failed to send to {chat_id}: {e}')

print(msg)
