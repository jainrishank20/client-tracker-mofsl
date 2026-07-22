"""
Telegram bot for Client Tracker MOFSL.
Answers natural-language questions about trades, positions and ledger.

Commands:
  /open        — all open positions with live unrealized P&L
  /ledger      — all ledger balances
  /summary     — snapshot (open/closed counts, total P&L)
  /pnl         — realized P&L by client
  /run         — trigger the daily pipeline
  /alert SYM PRICE [above|below] — set a price alert
  /alerts      — list active alerts
  /cancelalert SYM — remove an alert
  /clients     — list all clients
  /addclient CODE NAME — add new client
  /removeclient CODE  — remove client
  Or ask naturally: "Sathyavrath open trades", "Savitha ledger"
"""
import json, os, re, time, threading, datetime, sys
import urllib.request, urllib.parse, urllib.error
from typing import Optional
from groq import Groq
try:
    import yfinance as yf
    _YF = True
except ImportError:
    _YF = False

BASE     = os.path.dirname(os.path.abspath(__file__))
CFG_FILE = os.path.join(BASE, 'bot_config.json')

# ── Config helpers ────────────────────────────────────────────────────────────

def load_cfg() -> dict:
    return json.loads(open(CFG_FILE, encoding='utf-8-sig').read())

def save_cfg(cfg: dict):
    json.dump(cfg, open(CFG_FILE, 'w'), indent=2)

def get_names() -> dict:
    """Always fresh — picks up /addclient changes without restart."""
    return load_cfg().get('clients', {})

def get_name_to_code(names: dict) -> dict:
    """Build lookup: both 'savitha' → 'RIMK1252' and 'rimk1252' → 'RIMK1252'."""
    m = {v.lower(): k for k, v in names.items()}
    m.update({k.lower(): k for k in names})
    return m

# Module-level constants (token never changes, no need to reload)
_cfg   = load_cfg()
TOKEN  = _cfg['telegram_token']
CHAT_IDS = {c.strip() for c in str(_cfg['allowed_chat_id']).split(',')}
groq_client = Groq(api_key=_cfg.get('groq_api_key', ''))

ALERTS_FILE  = os.path.join(BASE, 'price_alerts.json')
_alerts_lock = threading.Lock()

# ── Symbol resolution ─────────────────────────────────────────────────────────

def load_overrides() -> dict:
    """Load ticker_overrides.json once per request — caller caches the result."""
    try:
        return json.loads(open(os.path.join(BASE, 'ticker_overrides.json'), encoding='utf-8-sig').read())
    except Exception:
        return {}

try:
    from symbol_map import resolve as _sym_resolve
except ImportError:
    def _sym_resolve(script, overrides):
        if script in overrides:
            v = overrides[script]
            if v:
                return v.strip().upper().replace(".NS", "")
        return re.sub(r'[^A-Z0-9&]', '', script.upper())

def sym(script: str, overrides: dict) -> str:
    """Convert CBOS script name to clean NSE ticker via SYMBOL_MAP + suffix stripping."""
    return _sym_resolve(script, overrides)

# ── Alerts persistence ────────────────────────────────────────────────────────

def load_alerts() -> dict:
    with _alerts_lock:
        # price_alerts.json is the live source (written by save_alerts at runtime).
        # bot_config.json['alerts'] is the backup restored on VM reboot — fallback only.
        try:
            return json.load(open(ALERTS_FILE))
        except Exception:
            pass
        try:
            return load_cfg().get('alerts', {})
        except Exception:
            return {}

def save_alerts(alerts: dict):
    with _alerts_lock:
        try:
            json.dump(alerts, open(ALERTS_FILE, 'w'), indent=2)
        except Exception:
            pass
        try:
            c = load_cfg()
            c['alerts'] = alerts
            save_cfg(c)
            threading.Thread(target=_push_config_to_github, args=(c,), daemon=True).start()
        except Exception:
            pass

# ── GitHub Secret sync ────────────────────────────────────────────────────────

def _push_config_to_github(cfg: dict):
    """Update BOT_CONFIG GitHub Secret (runs in background thread)."""
    try:
        import base64
        token = cfg.get('github_token', '')
        repo  = cfg.get('github_repo', '')
        if not token or not repo:
            return
        headers = {
            'Authorization': f'token {token}',
            'Accept': 'application/vnd.github+json',
            'Content-Type': 'application/json',
        }
        req = urllib.request.Request(
            f'https://api.github.com/repos/{repo}/actions/secrets/public-key',
            headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            pk_data = json.loads(r.read())
        from nacl import public as nacl_public, encoding as nacl_encoding
        pub_key_bytes = base64.b64decode(pk_data['key'])
        box = nacl_public.SealedBox(nacl_public.PublicKey(pub_key_bytes))
        encrypted = base64.b64encode(box.encrypt(json.dumps(cfg).encode())).decode()
        payload = json.dumps({'encrypted_value': encrypted, 'key_id': pk_data['key_id']}).encode()
        req = urllib.request.Request(
            f'https://api.github.com/repos/{repo}/actions/secrets/BOT_CONFIG',
            data=payload, method='PUT', headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            pass
        print("GitHub Secret BOT_CONFIG updated.")
    except Exception as e:
        print(f"GitHub Secret update failed: {e}")

def push_config_async(cfg: dict):
    threading.Thread(target=_push_config_to_github, args=(cfg,), daemon=True).start()

# ── Data helpers ──────────────────────────────────────────────────────────────

def load_trades() -> list:
    try:
        return json.load(open(os.path.join(BASE, 'trades.json')))
    except Exception:
        return []

def load_ledger() -> dict:
    try:
        return json.load(open(os.path.join(BASE, 'ledger.json')))
    except Exception:
        return {}

def detect_client(text: str, names: dict) -> Optional[str]:
    """Detect client code from free-text. Uses fresh names dict passed in."""
    name_to_code = get_name_to_code(names)
    t = text.lower()
    # Longer names first to avoid partial matches
    for name in sorted(name_to_code.keys(), key=len, reverse=True):
        if name in t:
            return name_to_code[name]
    return None

def detect_all_clients(text: str, names: dict) -> list:
    """Detect ALL client codes mentioned in text (returns list, deduped, in order)."""
    name_to_code = get_name_to_code(names)
    t = text.lower()
    seen, result = set(), []
    for name in sorted(name_to_code.keys(), key=len, reverse=True):
        if name in t:
            code = name_to_code[name]
            if code not in seen:
                seen.add(code)
                result.append(code)
    return result

def arrow(val: float) -> str:
    """Return ▲ for positive, ▼ for negative."""
    return '▲' if val >= 0 else '▼'

def fmt_inr(val: float) -> str:
    if val == 0:
        return '0'
    sign = '-' if val < 0 else ''
    s = str(abs(int(round(val))))
    if len(s) <= 3:
        return f"{sign}{s}"
    head, tail = s[:-3], s[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    groups.insert(0, head)
    return f"{sign}{','.join(groups)},{tail}"

# ── CMP fetching ──────────────────────────────────────────────────────────────

def fetch_cmp(symbols: list) -> dict:
    """Fetch live CMP for a list of NSE symbols. Returns {symbol: price}."""
    if not _YF or not symbols:
        return {}
    symbols = list(dict.fromkeys(symbols))  # dedupe, no cap
    result = {}
    # yfinance handles up to ~100 tickers per call; batch in chunks of 50 to be safe
    for i in range(0, len(symbols), 50):
        chunk = symbols[i:i+50]
        try:
            tickers = [s + '.NS' for s in chunk]
            data = yf.download(tickers, period='1d', interval='1m',
                               progress=False, auto_adjust=True, timeout=15)
            if len(tickers) == 1:
                try:
                    result[chunk[0]] = float(data['Close'].dropna().iloc[-1])
                except Exception:
                    pass
            else:
                for s, t in zip(chunk, tickers):
                    try:
                        result[s] = float(data['Close'][t].dropna().iloc[-1])
                    except Exception:
                        pass
        except Exception:
            pass
    return result

def fetch_single_cmp(symbol: str) -> Optional[float]:
    if not _YF:
        return None
    try:
        data = yf.Ticker(symbol + '.NS').history(period='1d', interval='1m')
        if not data.empty:
            return float(data['Close'].iloc[-1])
    except Exception:
        pass
    return None

# ── Formatted responses ───────────────────────────────────────────────────────

def _table(headers: list, rows: list) -> str:
    """Render a fixed-width monospace table with a header row and separator."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    def fmt_row(cells, aligns):
        parts = []
        for cell, w, align in zip(cells, widths, aligns):
            s = str(cell)
            parts.append(s.ljust(w) if align == 'l' else s.rjust(w))
        return '  '.join(parts)
    sep = '─' * (sum(widths) + 2 * (len(widths) - 1))
    aligns = ['r', 'l'] + ['r'] * (len(headers) - 2)
    lines = [
        f"`{fmt_row(headers, aligns)}`",
        f"`{sep}`",
    ]
    for row in rows:
        lines.append(f"`{fmt_row(row, aligns)}`")
    return '\n'.join(lines)


def trades_summary_for(client: str, trades: list, names: dict, overrides: dict) -> str:
    all_rows = [t for t in trades if t.get('client') == client]
    if not all_rows:
        return f"No trades found for {names.get(client, client)}."
    open_t   = [t for t in all_rows if not t.get('exit_date')]
    closed_t = [t for t in all_rows if t.get('exit_date')]

    # Merge FIFO lots by scrip — total qty, weighted avg price
    merged = {}
    for t in open_t:
        sc  = sym(t.get('script', '?'), overrides)
        qty = t.get('buy_qty', 0)
        bp  = t.get('buy_price', 0)
        if sc not in merged:
            merged[sc] = {'qty': 0.0, 'cost': 0.0}
        merged[sc]['qty']  += qty
        merged[sc]['cost'] += qty * bp
    for sc in merged:
        q = merged[sc]['qty']
        merged[sc]['avg'] = merged[sc]['cost'] / q if q else 0

    cmp_data = fetch_cmp(list(merged.keys()))

    name = names.get(client, client)
    lines = [f"*{name}* ({client})"]
    lines.append(f"Open: {len(merged)} scrip(s)  |  Closed: {len(closed_t)} trade(s)")

    if merged:
        lines.append("")
        table_rows = []
        for i, (sc, d) in enumerate(sorted(merged.items()), 1):
            qty = int(d['qty'])
            avg = d['avg']
            pnl_str = ''
            if sc in cmp_data:
                unreal  = (cmp_data[sc] - avg) * qty
                pnl_str = f"{arrow(unreal)} Rs {fmt_inr(abs(unreal))}"
            table_rows.append([i, sc, qty, f"{avg:.2f}", pnl_str])
        headers = ['#', 'Script', 'Qty', 'Avg Price', 'Unreal P&L']
        lines.append(_table(headers, table_rows))

    if closed_t:
        total_pnl = sum(_net_pnl(t) for t in closed_t)
        lines.append(f"\nRealized P&L: *{arrow(total_pnl)} Rs {fmt_inr(abs(total_pnl))}*")
    return '\n'.join(lines)


def today_trades_for(client: str, trades: list, names: dict, overrides: dict) -> str:
    today_str  = datetime.date.today().isoformat()
    today_disp = datetime.date.today().strftime('%d %b %Y')
    client_trades = [t for t in trades if t.get('client') == client]

    # Buys: entered today AND still open
    buys  = [t for t in client_trades
             if (t.get('entry_date') or '')[:10] == today_str and not t.get('exit_date')]
    # Sells: exited today
    sells = [t for t in client_trades
             if (t.get('exit_date') or '')[:10] == today_str]

    if not buys and not sells:
        return f"No trades for {names.get(client, client)} today ({today_disp})."

    name  = names.get(client, client)
    lines = [f"*{name} — {today_disp}*"]

    if buys:
        lines.append(f"\n*Buys  ({len(buys)})*")
        table_rows = []
        for i, t in enumerate(buys, 1):
            sc    = sym(t['script'], overrides)
            qty   = int(t.get('buy_qty') or 0)
            price = f"{float(t.get('buy_price') or 0):.2f}"
            table_rows.append([i, sc, qty, price])
        lines.append(_table(['#', 'Script', 'Qty', 'Price'], table_rows))

    net_pnl = 0.0
    if sells:
        lines.append(f"\n*Sells  ({len(sells)})*")
        table_rows = []
        for i, t in enumerate(sells, 1):
            sc    = sym(t['script'], overrides)
            qty   = int(t.get('sell_qty') or t.get('buy_qty') or 0)
            price = f"{float(t.get('sell_price') or 0):.2f}"
            pnl   = (float(t.get('sell_price') or 0) - float(t.get('buy_price') or 0)) * qty
            net_pnl += pnl
            sign  = '+' if pnl >= 0 else ''
            pnl_s = f"{sign}₹{fmt_inr(pnl)}"
            table_rows.append([i, sc, qty, price, pnl_s])
        lines.append(_table(['#', 'Script', 'Qty', 'Price', 'P&L'], table_rows))
        sign = '+' if net_pnl >= 0 else ''
        lines.append(f"\n*Net P&L today: {sign}₹{fmt_inr(net_pnl)}*")

    return '\n'.join(lines)


def all_open_summary(trades: list, names: dict, overrides: dict) -> str:
    open_t = [t for t in trades if not t.get('exit_date')]
    if not open_t:
        return "No open positions across any client."

    by_client = {}
    for t in open_t:
        by_client.setdefault(t['client'], []).append(t)

    # Merge lots per client per scrip to count unique symbols
    lines = [f"*Open Positions — {len(open_t)} lots across {len(by_client)} clients*\n"]
    total_inv = 0.0
    for code in sorted(by_client.keys()):
        rows = by_client[code]
        scrips = set(sym(t.get('script', ''), overrides) for t in rows)
        invested = sum(t.get('buy_price', 0) * t.get('buy_qty', 0) for t in rows)
        total_inv += invested
        lines.append(f"*{names.get(code, code)}*  {len(scrips)} scrip(s)  ₹{fmt_inr(invested)}")
    lines.append(f"\n*Total invested: ₹{fmt_inr(total_inv)}*")
    lines.append("_Ask about a specific client for full positions_")
    return '\n'.join(lines)


def ledger_summary(ledger: dict, names: dict) -> str:
    codes = list(names.keys()) or sorted(ledger.keys())
    rows  = []
    for c in codes:
        d = ledger.get(c, {})
        rows.append((c, fmt_inr(d.get('combined', 0)), fmt_inr(d.get('mtf', 0))))
    if not rows:
        return "No ledger data."
    w0 = max(len(r[0]) for r in rows)
    w1 = max(len('Delivery'), max(len(r[1]) for r in rows))
    w2 = max(len('MTF'),      max(len(r[2]) for r in rows))
    sep = '─' * (w0 + w1 + w2 + 4)
    def row_line(c, d, m): return f"{c:<{w0}}  {d:>{w1}}  {m:>{w2}}"
    out = ["*Ledger Balances*", f"`{sep}`",
           f"`{row_line('Client','Delivery','MTF')}`", f"`{sep}`"]
    for c, d, m in rows:
        out.append(f"`{row_line(c, d, m)}`")
    out.append(f"`{sep}`")
    return '\n'.join(out)


def _net_pnl(t: dict) -> float:
    """Net P&L for a closed trade — uses net_pnl if available, else gross minus all charges."""
    if t.get('net_pnl') not in (None, ''):
        return float(t['net_pnl'])
    gross = (float(t.get('sell_price', 0)) - float(t.get('buy_price', 0))) * float(t.get('sell_qty') or t.get('buy_qty', 0))
    charges = sum(float(t.get(k, 0) or 0) for k in (
        'buy_brokerage','buy_stt','buy_gst','buy_stamp','buy_txn','buy_other',
        'sell_brokerage','sell_stt','sell_gst','sell_stamp','sell_txn','sell_other'))
    return gross - charges


def pnl_summary(trades: list, names: dict) -> str:
    closed_t = [t for t in trades if t.get('exit_date')]
    rows, total = [], 0.0
    for code in names:
        ct = [t for t in closed_t if t.get('client') == code]
        if not ct:
            continue
        pnl = sum(_net_pnl(t) for t in ct)
        total += pnl
        rows.append((code, pnl))
    if not rows:
        return "No closed trades yet."
    w0  = max(len(r[0]) for r in rows)
    w1  = max(len('P&L'), max(len(fmt_inr(abs(r[1]))) + 3 for r in rows))
    sep = '─' * (w0 + w1 + 2)
    def row_line(c, p):
        s = f"{arrow(p)} {fmt_inr(abs(p))}"
        return f"{c:<{w0}}  {s:>{w1}}"
    out = ["*Realized P&L*", f"`{sep}`",
           f"`{'Client':<{w0}}  {'P&L':>{w1}}`", f"`{sep}`"]
    for code, pnl in rows:
        out.append(f"`{row_line(code, pnl)}`")
    out.append(f"`{sep}`")
    out.append(f"`{'TOTAL':<{w0}}  {arrow(total)} {fmt_inr(abs(total)):>{w1-2}}`")
    return '\n'.join(out)


# ── /update — self-update from GitHub (no SSH needed) ────────────────────────

def self_update(chat_id: str):
    """Pull latest code from GitHub, set up systemd if needed, restart cleanly."""
    import subprocess

    def _run():
        try:
            send(chat_id, "Updating bot from GitHub...")
            c     = load_cfg()
            token = c.get('github_token', '')
            repo  = c.get('github_repo', '')
            if not token or not repo:
                send(chat_id, "Error: github_token/github_repo missing from bot_config.json")
                return

            bot_dir = os.path.dirname(os.path.abspath(__file__))

            # Download files from GitHub — write to .tmp first, syntax-check before replacing
            for fname in ('telegram_bot.py', 'symbol_map.py', 'ticker_overrides.json', 'mo_downloader.py'):
                url = f"https://api.github.com/repos/{repo}/contents/{fname}"
                req = urllib.request.Request(url, headers={
                    'Authorization': f'token {token}',
                    'Accept': 'application/vnd.github.v3.raw',
                })
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = r.read()
                dest = os.path.join(bot_dir, fname)
                tmp  = dest + '.tmp'
                with open(tmp, 'wb') as f:
                    f.write(data)
                # Syntax-check Python files before replacing live copy
                if fname.endswith('.py'):
                    result = subprocess.run(
                        ['python3', '-m', 'py_compile', tmp],
                        capture_output=True, text=True
                    )
                    if result.returncode != 0:
                        os.remove(tmp)
                        send(chat_id, f"Update aborted — syntax error in {fname}:\n{result.stderr}")
                        return
                os.replace(tmp, dest)

            send(chat_id, "Files updated. Restarting bot now...")
            # os.execv replaces current process with fresh Python — works regardless of service name
            # systemctl restart is a fallback in case execv somehow fails
            time.sleep(1)
            try:
                os.execv(sys.executable, [sys.executable] + sys.argv)
            except Exception:
                subprocess.Popen(['sudo', 'systemctl', 'restart', 'tgbot'])

        except Exception as e:
            send(chat_id, f"Update failed: {e}")

    threading.Thread(target=_run, daemon=True).start()


# ── /run — trigger daily pipeline ────────────────────────────────────────────

def trigger_daily_run(chat_id: str):
    def _run():
        try:
            c     = load_cfg()
            token = c.get('github_token', '')
            repo  = c.get('github_repo', '')
            if not token or not repo:
                send(chat_id, "github_token/github_repo missing from bot_config.json")
                return
            payload = json.dumps({'ref': 'main'}).encode()
            req = urllib.request.Request(
                f'https://api.github.com/repos/{repo}/actions/workflows/daily_run.yml/dispatches',
                data=payload, method='POST',
                headers={
                    'Authorization': f'token {token}',
                    'Accept': 'application/vnd.github+json',
                    'Content-Type': 'application/json',
                }
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                pass
            send(chat_id,
                 "GitHub Actions run triggered!\n"
                 "Full pipeline: CBOS download → import → GSheet sync → notify\n"
                 "Takes ~15-20 min. You'll get the ledger message when done.")
        except Exception as e:
            send(chat_id, f"Failed to trigger run: {e}")
    threading.Thread(target=_run, daemon=True).start()


# ── Price alert polling ───────────────────────────────────────────────────────

def _is_market_hours() -> bool:
    import zoneinfo
    now = datetime.datetime.now(zoneinfo.ZoneInfo('Asia/Kolkata'))
    if now.weekday() >= 5:
        return False
    t = now.time()
    return datetime.time(9, 15) <= t <= datetime.time(15, 30)

def alert_poller():
    while True:
        try:
            if _is_market_hours():
                alerts = load_alerts()
                if alerts:
                    fired = []
                    for sym_key, info in list(alerts.items()):
                        price = fetch_single_cmp(sym_key)
                        if price is None:
                            continue
                        target    = info.get('target')
                        chat_id   = info.get('chat_id', '')
                        if target is None or not chat_id:
                            continue
                        direction = info.get('direction', 'above')
                        hit = (direction == 'above' and price >= target) or \
                              (direction == 'below' and price <= target)
                        if hit:
                            send(chat_id,
                                 f"Alert: {sym_key} is now Rs {price:.2f} "
                                 f"({'above' if direction=='above' else 'below'} target Rs {target:.2f})")
                            fired.append(sym_key)
                    if fired:
                        for k in fired:
                            del alerts[k]
                        save_alerts(alerts)
            time.sleep(300)
        except Exception:
            time.sleep(60)


# ── Groq helpers ─────────────────────────────────────────────────────────────

_SLIM_KEYS = ('client','script','entry_date','exit_date','buy_qty','buy_price','sell_qty','sell_price')

def _slim(trades: list) -> list:
    """Strip charge fields before sending to Groq — keeps tokens under limit."""
    return [{k: t.get(k) for k in _SLIM_KEYS} for t in trades]


# ── Groq: answer free-form questions ─────────────────────────────────────────

def ask_groq(question: str, context: str) -> str:
    try:
        resp = groq_client.chat.completions.create(
            model='llama-3.1-8b-instant',
            messages=[
                {
                    'role': 'system',
                    'content': (
                        'You are a concise assistant for a stock broker. '
                        'Answer using only the data provided. '
                        'Use Indian number format (lakhs/crores). '
                        'Keep answers short and direct. No markdown headers.'
                    )
                },
                {
                    'role': 'user',
                    'content': f"Data:\n{context}\n\nQuestion: {question}"
                }
            ],
            max_tokens=400,
            temperature=0.1,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"Could not get answer: {e}"


def brokerage_summary_for(client: str, trades: list, names: dict, tl: str) -> str:
    """Return charges paid by a client, optionally filtered by month or shown month-wise."""
    import calendar as _cal
    rows = [t for t in trades if t.get('client') == client]
    if not rows:
        return f"No trades found for {names.get(client, client)}."

    today = datetime.date.today()
    name  = names.get(client, client)

    # Month-wise breakdown requested
    if any(w in tl for w in ('month wise', 'month-wise', 'monthly', 'each month', 'per month', 'month by month')):
        # Collect brokerage only per month (buy_brokerage on entry_date, sell_brokerage on exit_date)
        month_totals = {}
        for t in rows:
            for field, date_str in [('buy', t.get('entry_date') or ''), ('sell', t.get('exit_date') or '')]:
                if not date_str:
                    continue
                try:
                    d = datetime.date.fromisoformat(date_str[:10])
                    key = (d.year, d.month)
                    month_totals[key] = month_totals.get(key, 0.0) + float(t.get(f'{field}_brokerage', 0) or 0)
                except Exception:
                    pass
        if not month_totals:
            return f"No brokerage data found for {name}."
        lines = [f"*{name} — Brokerage Month-Wise*", "`Month        Brokerage`", "`─────────────────────`"]
        grand = 0.0
        for (yr, mo) in sorted(month_totals):
            label = datetime.date(yr, mo, 1).strftime('%b %Y')
            val   = month_totals[(yr, mo)]
            grand += val
            lines.append(f"`{label:<12} Rs {fmt_inr(val):>8}`")
        lines.append("`─────────────────────`")
        lines.append(f"`{'Total':<12} Rs {fmt_inr(grand):>8}`")
        return '\n'.join(lines)

    # Single-period filter
    filter_label = 'All time'
    from_date = None
    to_date   = None

    if 'last month' in tl:
        first_this = today.replace(day=1)
        last_month_end = first_this - datetime.timedelta(days=1)
        from_date = last_month_end.replace(day=1)
        to_date   = last_month_end
        filter_label = from_date.strftime('%b %Y')
    elif 'this month' in tl:
        from_date = today.replace(day=1)
        to_date   = today
        filter_label = today.strftime('%b %Y')
    else:
        months = ['january','february','march','april','may','june',
                  'july','august','september','october','november','december']
        for i, mn in enumerate(months, 1):
            if mn in tl:
                yr = today.year if i <= today.month else today.year - 1
                from_date = datetime.date(yr, i, 1)
                to_date   = datetime.date(yr, i, _cal.monthrange(yr, i)[1])
                filter_label = from_date.strftime('%b %Y')
                break

    def _date_in_range(d_str: str) -> bool:
        if from_date is None:
            return True
        if not d_str:
            return False
        try:
            return from_date <= datetime.date.fromisoformat(d_str[:10]) <= to_date
        except Exception:
            return False

    buy_filtered  = [t for t in rows if _date_in_range(t.get('entry_date') or '')]
    sell_filtered = [t for t in rows if t.get('exit_date') and _date_in_range(t.get('exit_date') or '')]

    if not buy_filtered and not sell_filtered:
        return f"No trades found for {name} in {filter_label}."

    def _sb(field): return sum((t.get(field, 0) or 0) for t in buy_filtered)
    def _ss(field): return sum((t.get(field, 0) or 0) for t in sell_filtered)

    total_brokerage = _sb('buy_brokerage') + _ss('sell_brokerage')
    total_stt       = _sb('buy_stt')       + _ss('sell_stt')
    total_gst       = _sb('buy_gst')       + _ss('sell_gst')
    total_stamp     = _sb('buy_stamp')     + _ss('sell_stamp')
    total_txn       = _sb('buy_txn')       + _ss('sell_txn')
    total_all       = total_brokerage + total_stt + total_gst + total_stamp + total_txn

    return (
        f"*{name} — Charges ({filter_label})*\n"
        f"`Brokerage:   Rs {fmt_inr(total_brokerage)}`\n"
        f"`STT:         Rs {fmt_inr(total_stt)}`\n"
        f"`GST:         Rs {fmt_inr(total_gst)}`\n"
        f"`Stamp duty:  Rs {fmt_inr(total_stamp)}`\n"
        f"`Txn charges: Rs {fmt_inr(total_txn)}`\n"
        f"`─────────────────────────`\n"
        f"`Total:       Rs {fmt_inr(total_all)}`"
    )


def search_by_script(query: str, trades: list, names: dict) -> Optional[str]:
    """Find all clients with open positions matching keywords in the query."""
    stop = {'which', 'all', 'clients', 'client', 'have', 'has', 'who', 'the',
            'a', 'an', 'in', 'on', 'for', 'and', 'any', 'open', 'stock', 'share',
            'are', 'holding', 'hold', 'also', 'currently', 'how', 'many', 'tell',
            'me', 'about', 'position', 'positions', 'buy', 'bought', 'today',
            'now', 'give', 'show', 'list', 'get', 'find', 'check', 'see', 'with'}
    # Known short tickers (3 chars or less) that are real NSE symbols
    known_short = {t['script'].upper() for t in trades} | {
        'ITC', 'BSE', 'LIC', 'PNB', 'NCC', 'REC', 'BEL', 'IEX', 'SCI',
        'TCS', 'M&M', 'PFC', 'BEML', 'MRPL', 'IFCI', 'IDBI', 'NMDC', 'NTPC',
    }
    words = [re.sub(r'[^A-Z0-9&]', '', w) for w in query.upper().split()]
    terms = [w for w in words if w and w.lower() not in stop and (len(w) >= 4 or w in known_short)]
    if not terms:
        return None
    open_trades = [t for t in trades if not t.get('exit_date')]
    _ov = load_overrides()
    def _matches(t):
        script = (t.get('script') or '').upper()
        ticker = sym(t.get('script', ''), _ov).upper()
        return any(term in script or term in ticker for term in terms)
    matches: dict[str, set] = {}
    for t in open_trades:
        if _matches(t):
            script = (t.get('script') or '').upper()
            matches.setdefault(t.get('client', ''), set()).add(script)
    if not matches:
        return f"No open positions found matching '{' '.join(terms)}'."
    # Build qty + weighted avg buy price per client per script
    qty_map:   dict[str, dict[str, float]] = {}
    cost_map:  dict[str, dict[str, float]] = {}  # total cost (qty * price)
    for t in open_trades:
        script = (t.get('script') or '').upper()
        if _matches(t):
            c   = t.get('client', '')
            qty = float(t.get('buy_qty') or 0)
            bp  = float(t.get('buy_price') or 0)
            qty_map.setdefault(c, {})
            cost_map.setdefault(c, {})
            qty_map[c][script]  = qty_map[c].get(script, 0)  + qty
            cost_map[c][script] = cost_map[c].get(script, 0) + qty * bp

    all_scripts = sorted(set(s for ss in matches.values() for s in ss))
    single = len(all_scripts) == 1
    lines = [f"*Open positions — {', '.join(all_scripts)}*"]
    for code, scripts in sorted(matches.items()):
        name = names.get(code, code)
        parts = []
        for s in sorted(scripts):
            qty  = qty_map.get(code, {}).get(s, 0)
            cost = cost_map.get(code, {}).get(s, 0)
            avg  = cost / qty if qty else 0
            # Only prefix script name when multiple stocks in result
            prefix = f"{s}: " if not single else ""
            parts.append(f"{prefix}{int(qty)} qty @ Rs {avg:,.0f}")
        lines.append(f"  {name} ({code}) — {', '.join(parts)}")
    return '\n'.join(lines)


def transaction_history_for(client: str, trades: list, names: dict) -> str:
    """Full trade-by-trade history for a client, sorted by entry date descending.
    Reads directly from trades.json — no Groq involved."""
    rows = sorted(
        [t for t in trades if t.get('client') == client],
        key=lambda t: t.get('entry_date') or '',
        reverse=True
    )
    name = names.get(client, client)
    if not rows:
        return f"No trades found for {name}."

    MAX_MSG = 3500  # Telegram message limit ~4096 chars; leave headroom
    header = f"*{name} — Transaction History ({len(rows)} trades)*\n"
    lines = []
    for t in rows:
        script   = t.get('script', '?')
        qty      = int(t.get('buy_qty') or 0)
        bp       = float(t.get('buy_price') or 0)
        entry    = (t.get('entry_date') or '')[:10]
        invested = fmt_inr(qty * bp)
        if t.get('exit_date'):
            sp     = float(t.get('sell_price') or 0)
            exit_d = t.get('exit_date', '')[:10]
            pnl    = (sp - bp) * qty
            sign   = '+' if pnl >= 0 else ''
            lines.append(
                f"`{entry}  {script[:20]:<20}  {qty:>5} qty @ {bp:>8,.0f}`\n"
                f"`{'':>10}  Sold {exit_d} @ {sp:>8,.0f}  P&L {sign}{fmt_inr(pnl)}`"
            )
        else:
            lines.append(
                f"`{entry}  {script[:20]:<20}  {qty:>5} qty @ {bp:>8,.0f}  [OPEN]`"
            )

    # Split into chunks if too long for one message
    chunks, current = [], header
    for line in lines:
        if len(current) + len(line) + 2 > MAX_MSG:
            chunks.append(current)
            current = f"*{name} — (contd.)*\n"
        current += line + '\n'
    chunks.append(current)
    return '\n---SPLIT---\n'.join(chunks)


def capital_summary_for(client: str, trades: list, names: dict) -> str:
    """Return capital deployed in open positions for a client."""
    open_t = [t for t in trades if t.get('client') == client and not t.get('exit_date')]
    name   = names.get(client, client)
    if not open_t:
        return f"{name} has no open positions currently."
    total = sum((t.get('buy_qty', 0) or 0) * (t.get('buy_price', 0) or 0) for t in open_t)
    w = max(len(t.get('script', '?')) for t in open_t)
    lines = [f"*{name} — Capital Deployed*"]
    for t in open_t:
        cap = (t.get('buy_qty', 0) or 0) * (t.get('buy_price', 0) or 0)
        lines.append(f"`{t.get('script','?'):<{w}}  Rs {fmt_inr(cap)}`")
    lines.append(f"`{'─'*(w+18)}`")
    lines.append(f"`{'TOTAL':<{w}}  Rs {fmt_inr(total)}`")
    lines.append(f"_({len(open_t)} open positions)_")
    return '\n'.join(lines)


# ── Route messages ────────────────────────────────────────────────────────────

def handle(text: str, chat_id: str) -> Optional[str]:
    text = text.strip()
    tl   = text.lower()

    # Always reload fresh — picks up /addclient without restart
    names     = get_names()
    overrides = load_overrides()
    trades    = load_trades()
    ledger    = load_ledger()

    if tl in ('/start', '/help'):
        return (
            "*Client Tracker Bot*\n\n"
            "*📊 Data*\n"
            "/open — all open positions\n"
            "/ledger — ledger balances\n"
            "/pnl — realized P&L by client\n"
            "/summary — quick snapshot\n"
            "/today — trades entered/exited today\n\n"
            "*⚙️ Pipeline*\n"
            "/run — trigger fresh download + GSheet sync\n"
            "/fixcron — daily run stopped? use this to fix it\n"
            "/update — pull latest bot code & restart\n\n"
            "*🔧 Maintenance*\n"
            "/addticker SYMBOL TICKER — fix a #N/A CMP in GSheet\n"
            "/addclient CODE NAME — add new client\n"
            "/removeclient CODE — remove client\n"
            "/clients — list all clients\n\n"
            "*🔔 Alerts*\n"
            "/alert SYM PRICE [above|below] — set price alert\n"
            "/alerts — list active alerts\n"
            "/cancelalert SYM — cancel alert\n\n"
            "*Or just ask naturally:*\n"
            "`Sathyavrath open trades`\n"
            "`What is Savitha ledger`\n"
            "`Who has EPACKPEB?`"
        )

    if tl in ('/open', 'open', 'open positions', 'all open'):
        return all_open_summary(trades, names, overrides)

    if tl in ('/ledger', 'ledger', 'balances', 'ledger balance'):
        return ledger_summary(ledger, names)

    if tl in ('/pnl', 'pnl', 'realized pnl', 'realised pnl'):
        return pnl_summary(trades, names)

    if tl in ('/summary', 'summary'):
        open_t   = [t for t in trades if not t.get('exit_date')]
        closed_t = [t for t in trades if t.get('exit_date')]
        total_pnl = sum(_net_pnl(t) for t in closed_t)
        sign = '+' if total_pnl >= 0 else ''
        return (
            f"*Summary*\n"
            f"Total trades: {len(trades)}\n"
            f"Open: {len(open_t)}  |  Closed: {len(closed_t)}\n"
            f"Total realised P&L: {sign}Rs {fmt_inr(total_pnl)}"
        )

    # /clients
    if tl in ('/clients', '/listclients'):
        if not names:
            return "No clients configured."
        lines = ["*Configured Clients:*"]
        for code, name in names.items():
            lines.append(f"  {code} — {name}")
        return '\n'.join(lines)

    # /addclient CODE NAME
    m = re.match(r'^/addclient\s+([A-Z0-9]+)\s+(.+)$', text.strip(), re.IGNORECASE)
    if m:
        code = m.group(1).upper()
        cname = m.group(2).strip()
        try:
            c = load_cfg()
            c.setdefault('clients', {})
            if code in c['clients']:
                return f"Client {code} already exists as {c['clients'][code]}."
            c['clients'][code] = cname
            save_cfg(c)
            push_config_async(c)
            return f"Client {code} ({cname}) added successfully."
        except Exception as e:
            return f"addclient failed: {e}"

    # /removeclient CODE
    m = re.match(r'^/removeclient\s+([A-Z0-9]+)$', text.strip(), re.IGNORECASE)
    if m:
        code = m.group(1).upper()
        try:
            c = load_cfg()
            if code not in c.get('clients', {}):
                return f"Client {code} not found."
            removed_name = c['clients'].pop(code)
            save_cfg(c)
            push_config_async(c)
            return f"Client {code} ({removed_name}) removed."
        except Exception as e:
            return f"removeclient failed: {e}"

    # /update — pull latest code from GitHub and restart (no SSH needed)
    if tl == '/update':
        self_update(chat_id)
        return None

    # /addticker SYMBOL TICKER — add/update a ticker override and re-sync GSheet
    m_at = re.match(r'^/addticker\s+(\S+)\s+(\S+)$', text.strip(), re.IGNORECASE)
    if m_at:
        raw_sym = m_at.group(1).upper().strip()
        nse_ticker = m_at.group(2).upper().strip()
        try:
            overrides_path = os.path.join(BASE, 'ticker_overrides.json')
            ovr = load_overrides()
            ovr[raw_sym] = nse_ticker
            with open(overrides_path, 'w') as _f:
                json.dump(ovr, _f, indent=2)
            send(chat_id, f"Saved: {raw_sym} -> {nse_ticker}\n\nTriggering GSheet re-sync...")
            trigger_daily_run(chat_id)
        except Exception as e:
            send(chat_id, f"Failed to save ticker override: {e}")
        return None

    # /run
    if tl == '/run':
        trigger_daily_run(chat_id)
        return None  # sent async

    # /fixcron — re-install VM crontab from inside the VM (bypasses GHA SSH block)
    if tl == '/fixcron':
        def _fixcron():
            try:
                import subprocess
                send(chat_id, "Re-installing crontab on VM...")
                script = (
                    "crontab -l 2>/dev/null | grep -v 'vm_daily_run' > /tmp/new_crontab || true\n"
                    "echo '0 14 * * 1-6 /home/opc/client-tracker-mofsl/vm_daily_run.sh false >> /home/opc/vm_daily_run.log 2>&1' >> /tmp/new_crontab\n"
                    "echo '30 5 * * 0  /home/opc/client-tracker-mofsl/vm_daily_run.sh true  >> /home/opc/vm_daily_run.log 2>&1' >> /tmp/new_crontab\n"
                    "crontab /tmp/new_crontab\n"
                    "echo 'Crontab installed:'\n"
                    "crontab -l\n"
                )
                result = subprocess.run(['bash', '-c', script], capture_output=True, text=True, timeout=30)
                out = (result.stdout + result.stderr).strip()
                send(chat_id, f"Crontab result:\n```\n{out}\n```")
            except Exception as e:
                send(chat_id, f"fixcron failed: {e}")
        threading.Thread(target=_fixcron, daemon=True).start()
        return None

    # /alert SYMBOL PRICE [above|below]
    m = re.match(r'^/alert\s+([A-Z0-9&.-]+)\s+([\d.]+)(?:\s+(above|below))?$', text.upper())
    if m:
        sym_key    = m.group(1)
        target     = float(m.group(2))
        forced_dir = m.group(3)
        alerts     = load_alerts()
        current    = fetch_single_cmp(sym_key)
        if forced_dir:
            direction = forced_dir.lower()
        elif current is not None:
            direction = 'above' if target > current else 'below'
        else:
            direction = 'above'
        alerts[sym_key] = {'target': target, 'chat_id': chat_id, 'direction': direction}
        save_alerts(alerts)
        cmp_str = f" (CMP Rs {current:.2f})" if current else ""
        return (f"Alert set: {sym_key} {'above' if direction=='above' else 'below'} Rs {target:.2f}"
                f"{cmp_str}. You will be notified when triggered.")

    if tl == '/alerts':
        alerts = load_alerts()
        if not alerts:
            return "No active alerts."
        lines = ["*Active Alerts:*"]
        for s, info in alerts.items():
            d = 'above' if info.get('direction', 'above') == 'above' else 'below'
            lines.append(f"  {s}  {d}  Rs {info['target']:.2f}")
        return '\n'.join(lines)

    m = re.match(r'^/cancelalert\s+([A-Z0-9&.-]+)$', text.upper())
    if m:
        sym_key = m.group(1)
        alerts  = load_alerts()
        if sym_key in alerts:
            del alerts[sym_key]
            save_alerts(alerts)
            return f"Alert for {sym_key} cancelled."
        return f"No alert found for {sym_key}."

    # "today's trades" with no client → show ALL clients
    if tl == '/today' or ('today' in tl and any(w in tl for w in ('trade', 'bought', 'sold', 'taken'))):
        today_str  = datetime.date.today().isoformat()
        today_disp = datetime.date.today().strftime('%d %b %Y')
        parts = [f"*All Trades — {today_disp}*"]
        any_found = False
        for code in names:
            section = today_trades_for(code, trades, names, overrides)
            if 'No trades' not in section:
                parts.append('\n' + section)
                any_found = True
        if not any_found:
            return f"No trades recorded for any client today ({today_disp})."
        return '\n'.join(parts)

    # Multi-client brokerage query (e.g. "brokerage for RIMK1252 and RIMK1256 month wise")
    is_brokerage_q = any(w in tl for w in ('brokerage', 'commission', 'charge', 'charges', 'fee', 'fees',
                                             'tax', 'taxes', 'stt', 'gst', 'stamp', 'cost', 'costs'))
    if is_brokerage_q:
        all_clients = detect_all_clients(text, names)
        if len(all_clients) > 1:
            parts = [brokerage_summary_for(c, trades, names, tl) for c in all_clients]
            return '\n\n'.join(parts)

    # Client-specific query
    client = detect_client(text, names)
    if client:
        if 'today' in tl and any(w in tl for w in ('trade', 'bought', 'sold', 'taken')):
            return today_trades_for(client, trades, names, overrides)
        if any(w in tl for w in ('open', 'position', 'trade', 'holding')):
            return trades_summary_for(client, trades, names, overrides)
        if any(w in tl for w in ('ledger', 'balance', 'debit', 'credit', 'mtf')):
            d    = ledger.get(client, {})
            name = names.get(client, client)
            return (
                f"*{name}* ledger\n"
                f"Delivery: Rs {fmt_inr(d.get('combined', 0))}\n"
                f"MTF:      Rs {fmt_inr(d.get('mtf', 0))}"
            )
        if any(w in tl for w in ('pnl', 'p&l', 'profit', 'loss', 'earn', 'return', 'realized', 'realised')):
            return trades_summary_for(client, trades, names, overrides)
        if is_brokerage_q:
            return brokerage_summary_for(client, trades, names, tl)
        if any(w in tl for w in ('capital', 'invest', 'deploy', 'deployed', 'exposure', 'invested')):
            return capital_summary_for(client, trades, names)
        if any(w in tl for w in ('transaction', 'history', 'all trade', 'trade history', 'full history',
                                  'all transaction', 'transactions', 'statement', 'account statement')):
            result = transaction_history_for(client, trades, names)
            if '---SPLIT---' in result:
                for chunk in result.split('---SPLIT---'):
                    if chunk.strip():
                        send(chat_id, chunk.strip())
                return None
            return result
        # Free-form client question → Groq
        rows    = [t for t in trades if t.get('client') == client]
        context = (f"Client: {names.get(client, client)} ({client})\n"
                   f"Trades: {json.dumps(_slim(rows[:25]))}\n"
                   f"Ledger: {json.dumps(ledger.get(client, {}))}")
        return ask_groq(text, context)

    # Try stock/script search for any query — catches "who has X", "anyone holding X",
    # "does anyone have X", "show clients with X", etc. without needing exact trigger words
    result = search_by_script(text, trades, names)
    if result and not result.startswith('No open positions'):
        return result

    # General free-form → Groq
    open_t   = [t for t in trades if not t.get('exit_date')]
    closed_t = [t for t in trades if t.get('exit_date')]
    context  = (f"Open ({len(open_t)} trades): {json.dumps(_slim(open_t[:20]))}\n"
                f"Closed ({len(closed_t)} total): {json.dumps(_slim(closed_t[:10]))}\n"
                f"Ledger: {json.dumps(ledger)}")
    return ask_groq(text, context)


# ── Telegram polling ──────────────────────────────────────────────────────────

def tg(method: str, _socket_timeout: int = 15, **kwargs):
    data = urllib.parse.urlencode({k: v for k, v in kwargs.items()}).encode()
    req  = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=_socket_timeout) as r:
        return json.loads(r.read())

def send(chat_id: str, text: str):
    """Send a message, splitting on newlines to avoid breaking markdown."""
    MAX = 4000
    if len(text) <= MAX:
        chunks = [text]
    else:
        # Split on newlines, reassemble into chunks under MAX
        chunks, current = [], []
        current_len = 0
        for line in text.split('\n'):
            if current_len + len(line) + 1 > MAX and current:
                chunks.append('\n'.join(current))
                current, current_len = [], 0
            current.append(line)
            current_len += len(line) + 1
        if current:
            chunks.append('\n'.join(current))

    for chunk in chunks:
        try:
            tg('sendMessage', chat_id=chat_id, text=chunk, parse_mode='Markdown')
        except Exception:
            try:
                tg('sendMessage', chat_id=chat_id, text=chunk)
            except Exception as e:
                print(f"Send error: {e}")

def main():
    print("Bot starting...")
    # Restore alerts from bot_config.json on VM rebuild
    if not os.path.exists(ALERTS_FILE):
        try:
            backed_up = load_cfg().get('alerts', {})
            if backed_up:
                json.dump(backed_up, open(ALERTS_FILE, 'w'), indent=2)
                print(f"Restored {len(backed_up)} alert(s) from bot_config.json")
        except Exception:
            pass
    threading.Thread(target=alert_poller, daemon=True).start()
    print("Price alert poller started.")

    offset = 0
    while True:
        try:
            updates = tg('getUpdates', _socket_timeout=35, offset=offset, timeout=30)
            for upd in updates.get('result', []):
                offset  = upd['update_id'] + 1
                msg     = upd.get('message') or upd.get('edited_message')
                if not msg:
                    continue
                chat_id = str(msg['chat']['id'])
                text    = msg.get('text', '').strip()
                if not text or chat_id not in CHAT_IDS:
                    continue
                print(f"[{chat_id}] {text}")
                try:
                    reply = handle(text, chat_id)
                    if reply:
                        send(chat_id, reply)
                except Exception as e:
                    send(chat_id, f"Error: {e}")
        except urllib.error.HTTPError as e:
            if e.code == 409:
                print("FATAL: 409 Conflict — another bot instance is running. Exiting.")
                sys.exit(1)
            print(f"HTTP {e.code}: {e} — retrying in 10s")
            time.sleep(10)
        except urllib.error.URLError as e:
            print(f"Network error: {e} — retrying in 5s")
            time.sleep(5)
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(3)

if __name__ == '__main__':
    main()
