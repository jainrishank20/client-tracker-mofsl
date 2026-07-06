"""
Telegram bot for Client Tracker MOFSL.
Answers natural-language questions about trades, positions and ledger.
Fast: pre-filters data before sending to Groq so LLM only sees relevant rows.

Commands:
  /open        — all open positions with live unrealized P&L
  /ledger      — all ledger balances
  /summary     — snapshot (open/closed counts, total P&L)
  /pnl         — realized P&L by client
  /run         — trigger the daily pipeline on VM (download + import + sync)
  /alert SYM PRICE — set a price alert (e.g. /alert SUNPHARMA 2000)
  /alerts      — list active alerts
  /cancelalert SYM — remove an alert
  Or ask naturally: "Sathyavrath open trades", "Savitha ledger"
"""
import json, os, re, time, threading, subprocess, sys
import urllib.request, urllib.parse, urllib.error
from typing import Optional
from groq import Groq
try:
    import yfinance as yf
    _YF = True
except ImportError:
    _YF = False

BASE = os.path.dirname(os.path.abspath(__file__))

CFG_FILE    = os.path.join(BASE, 'bot_config.json')

def load_cfg():
    return json.load(open(CFG_FILE))

def save_cfg(cfg):
    json.dump(cfg, open(CFG_FILE, 'w'), indent=2)

cfg         = load_cfg()
TOKEN       = cfg['telegram_token']
CHAT_IDS    = {c.strip() for c in str(cfg['allowed_chat_id']).split(',')}
groq_client = Groq(api_key=cfg['groq_api_key'])

def get_names():
    return load_cfg().get('clients', {})

def get_name_to_code():
    names = get_names()
    m = {v.lower(): k for k, v in names.items()}
    m.update({k.lower(): k for k in names})
    return m

NAMES        = get_names()        # module-level for compatibility
NAME_TO_CODE = get_name_to_code()

ALERTS_FILE  = os.path.join(BASE, 'price_alerts.json')
_alerts_lock = threading.Lock()

# ── Symbol resolution ─────────────────────────────────────────────────────────

def _load_overrides():
    try:
        return json.load(open(os.path.join(BASE, 'ticker_overrides.json')))
    except Exception:
        return {}

def sym(script):
    """Convert raw CBOS script name to clean NSE ticker symbol."""
    ov = _load_overrides()
    if script in ov:
        return ov[script]
    return re.sub(r'[^A-Z0-9&]', '', script.upper())


# ── Alerts persistence ────────────────────────────────────────────────────────

def load_alerts() -> dict:
    with _alerts_lock:
        try:
            return json.load(open(ALERTS_FILE))
        except Exception:
            return {}

def save_alerts(alerts: dict):
    with _alerts_lock:
        json.dump(alerts, open(ALERTS_FILE, 'w'), indent=2)


def _push_config_to_github(cfg: dict):
    """Update the BOT_CONFIG GitHub Secret so next Actions run sees the new config."""
    try:
        import base64
        token = cfg.get('github_token', '')
        repo  = cfg.get('github_repo', '')
        if not token or not repo:
            return
        headers = {'Authorization': f'token {token}',
                   'Accept': 'application/vnd.github+json',
                   'Content-Type': 'application/json'}
        # Get repo public key
        req = urllib.request.Request(
            f'https://api.github.com/repos/{repo}/actions/secrets/public-key',
            headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            pk_data = json.loads(r.read())
        pub_key_b64 = pk_data['key']
        key_id      = pk_data['key_id']
        # Encrypt
        from nacl import public as nacl_public, encoding as nacl_encoding
        pub_key_bytes = base64.b64decode(pub_key_b64)
        pk  = nacl_public.PublicKey(pub_key_bytes)
        box = nacl_public.SealedBox(pk)
        encrypted = base64.b64encode(box.encrypt(json.dumps(cfg).encode())).decode()
        payload = json.dumps({'encrypted_value': encrypted, 'key_id': key_id}).encode()
        req = urllib.request.Request(
            f'https://api.github.com/repos/{repo}/actions/secrets/BOT_CONFIG',
            data=payload, method='PUT', headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            pass  # 204 No Content on success
    except Exception as e:
        print(f"GitHub Secret update failed: {e}")


# ── Data helpers ──────────────────────────────────────────────────────────────

def load_trades():
    try:
        return json.load(open(os.path.join(BASE, 'trades.json')))
    except Exception:
        return []

def load_ledger():
    try:
        return json.load(open(os.path.join(BASE, 'ledger.json')))
    except Exception:
        return {}

def detect_client(text: str) -> Optional[str]:
    t = text.lower()
    for name, code in NAME_TO_CODE.items():
        if name in t:
            return code
    return None

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

def fetch_cmp(symbols: list) -> dict:
    """Fetch live CMP for a list of NSE symbols. Returns {symbol: price}.
    Capped at 15 symbols — too many causes timeouts."""
    if not _YF or not symbols:
        return {}
    symbols = list(symbols)[:15]  # hard cap
    try:
        tickers = [s + '.NS' for s in symbols]
        data = yf.download(tickers, period='1d', interval='1m',
                           progress=False, auto_adjust=True,
                           timeout=10)
        result = {}
        if len(tickers) == 1:
            # Single ticker returns flat DataFrame
            try:
                price = float(data['Close'].dropna().iloc[-1])
                result[symbols[0]] = price
            except Exception:
                pass
        else:
            for s, t in zip(symbols, tickers):
                try:
                    price = float(data['Close'][t].dropna().iloc[-1])
                    result[s] = price
                except Exception:
                    pass
        return result
    except Exception:
        return {}

def fetch_single_cmp(symbol: str) -> Optional[float]:
    try:
        t = yf.Ticker(symbol + '.NS')
        data = t.history(period='1d', interval='1m')
        if not data.empty:
            return float(data['Close'].iloc[-1])
    except Exception:
        pass
    return None


# ── Formatted responses ───────────────────────────────────────────────────────

def trades_summary_for(client: str, trades: list) -> str:
    rows = [t for t in trades if t.get('client') == client]
    if not rows:
        return f"No trades found for {NAMES.get(client, client)}."
    open_t   = [t for t in rows if not t.get('exit_date')]
    closed_t = [t for t in rows if t.get('exit_date')]

    open_syms = list({sym(t.get('script','')) for t in open_t})
    cmp = fetch_cmp(open_syms)

    lines = [f"*{NAMES.get(client, client)}* ({client})"]
    lines.append(f"Open: {len(open_t)}  |  Closed: {len(closed_t)}")
    if open_t:
        lines.append("\n*Open positions:*")
        for t in open_t:
            qty   = int(t.get('buy_qty', 0))
            bp    = t.get('buy_price', 0)
            sc    = sym(t.get('script', '?'))
            line  = f"  {sc}  qty={qty}  @₹{bp:.2f}"
            if sc in cmp:
                unreal = (cmp[sc] - bp) * qty
                sign   = '+' if unreal >= 0 else ''
                line  += f"  ({sign}₹{fmt_inr(unreal)})"
            lines.append(line)
    if closed_t:
        total_pnl = sum(
            (t.get('sell_price',0) - t.get('buy_price',0)) * t.get('buy_qty',0)
            for t in closed_t
        )
        lines.append(f"\nClosed P&L: ₹{fmt_inr(total_pnl)}")
    return '\n'.join(lines)

def all_open_summary(trades: list) -> str:
    open_t = [t for t in trades if not t.get('exit_date')]
    if not open_t:
        return "No open positions across any client."
    by_client = {}  # type: dict
    for t in open_t:
        by_client.setdefault(t['client'], []).append(t)

    lines = [f"*Open Positions — {len(open_t)} total*\n"]
    total_inv = 0
    for code, rows in by_client.items():
        invested = sum(t.get('buy_price', 0) * t.get('buy_qty', 0) for t in rows)
        total_inv += invested
        lines.append(f"*{NAMES.get(code,code)}* — {len(rows)} pos — ₹{fmt_inr(invested)}")
    lines.append(f"\n*Total invested: ₹{fmt_inr(total_inv)}*")
    lines.append("_Ask about a specific client for their positions_")
    return '\n'.join(lines)

def ledger_summary(ledger: dict) -> str:
    lines = ["*Ledger Balances*\n"]
    lines.append(f"{'Client':<18} {'Delivery':>12} {'MTF':>14}")
    lines.append('─' * 46)
    for code, name in NAMES.items():
        d = ledger.get(code, {})
        combined = fmt_inr(d.get('combined', 0))
        mtf      = fmt_inr(d.get('mtf', 0))
        lines.append(f"{name:<18} {combined:>12} {mtf:>14}")
    return '`' + '\n'.join(lines) + '`'

def pnl_summary(trades: list) -> str:
    closed_t = [t for t in trades if t.get('exit_date')]
    lines = ["*Realized P&L by Client*\n"]
    total = 0
    for code, name in NAMES.items():
        rows = [t for t in closed_t if t.get('client') == code]
        if not rows:
            continue
        pnl  = sum((t.get('sell_price',0) - t.get('buy_price',0)) * t.get('buy_qty',0) for t in rows)
        sign = '+' if pnl >= 0 else ''
        total += pnl
        lines.append(f"{name:<18} {sign}₹{fmt_inr(pnl)}")
    lines.append('─' * 32)
    sign = '+' if total >= 0 else ''
    lines.append(f"{'TOTAL':<18} {sign}₹{fmt_inr(total)}")
    return '`' + '\n'.join(lines) + '`'


# ── /run — trigger daily pipeline ────────────────────────────────────────────

def trigger_daily_run(chat_id: str):
    """Run the daily pipeline in a background thread, send status updates."""
    def _run():
        send(chat_id, "⏳ Starting daily pipeline on VM...")
        script = os.path.join(BASE, 'run_daily_vm.py')
        try:
            r = subprocess.run(
                [sys.executable, script],
                capture_output=True, text=True, timeout=900
            )
            if r.returncode == 0:
                # Extract key stats from stdout
                lines = r.stdout.splitlines()
                stats = [l for l in lines if 'imported:' in l or 'Open' in l or 'Closed' in l]
                summary = '\n'.join(stats[-5:]) if stats else 'Done.'
                send(chat_id, f"✅ Daily run complete\n```\n{summary}\n```")
            else:
                send(chat_id, f"❌ Daily run failed\n```\n{r.stderr[-500:]}\n```")
        except subprocess.TimeoutExpired:
            send(chat_id, "❌ Daily run timed out after 15 minutes.")
        except Exception as e:
            send(chat_id, f"❌ Error: {e}")
    threading.Thread(target=_run, daemon=True).start()


# ── Price alert polling ───────────────────────────────────────────────────────

def _is_market_hours() -> bool:
    """True if current IST time is within market hours (9:15 – 15:30 Mon–Fri)."""
    import datetime, zoneinfo
    now = datetime.datetime.now(zoneinfo.ZoneInfo('Asia/Kolkata'))
    if now.weekday() >= 5:  # Saturday/Sunday
        return False
    t = now.time()
    return datetime.time(9, 15) <= t <= datetime.time(15, 30)

def alert_poller():
    """Background thread: poll prices every 5 min during market hours, fire alerts."""
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
                        target  = info['target']
                        chat_id = info['chat_id']
                        direction = info.get('direction', 'above')
                        hit = (direction == 'above' and price >= target) or \
                              (direction == 'below' and price <= target)
                        if hit:
                            send(chat_id,
                                 f"🔔 *Alert triggered!*\n"
                                 f"{sym_key} is now ₹{price:.2f} "
                                 f"({'≥' if direction=='above' else '≤'} target ₹{target:.2f})")
                            fired.append(sym_key)
                    if fired:
                        for k in fired:
                            del alerts[k]
                        save_alerts(alerts)
            time.sleep(300)  # 5 minutes
        except Exception:
            time.sleep(60)


# ── Groq: answer free-form questions ─────────────────────────────────────────

def ask_groq(question: str, context: str) -> str:
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


# ── Route messages ────────────────────────────────────────────────────────────

def handle(text: str, chat_id: str) -> Optional[str]:
    text  = text.strip()
    tl    = text.lower()
    NAMES = get_names()  # always fresh — picks up /addclient changes without restart
    trades = load_trades()
    ledger = load_ledger()

    if tl in ('/start', '/help'):
        return (
            "📊 *Client Tracker Bot*\n\n"
            "*Commands:*\n"
            "/open — all open positions\n"
            "/ledger — ledger balances\n"
            "/summary — snapshot\n"
            "/pnl — realized P&L by client\n"
            "/run — trigger daily pipeline\n"
            "/alert SYM PRICE \\[above\\|below\\] — price alert\n"
            "/alerts — list active alerts\n"
            "/cancelalert SYM — remove alert\n"
            "/clients — list all clients\n"
            "/addclient CODE NAME — add new client\n"
            "/removeclient CODE — remove client\n\n"
            "*Or ask naturally:*\n"
            "_'Sathyavrath open trades'_\n"
            "_'What is Savitha's ledger?'_"
        )

    if tl in ('/open', 'open', 'open positions', 'all open'):
        return all_open_summary(trades)

    if tl in ('/ledger', 'ledger', 'balances', 'ledger balance'):
        return ledger_summary(ledger)

    if tl in ('/pnl', 'pnl', 'realized pnl', 'realised pnl'):
        return pnl_summary(trades)

    if tl in ('/summary', 'summary'):
        open_t   = [t for t in trades if not t.get('exit_date')]
        closed_t = [t for t in trades if t.get('exit_date')]
        total_pnl = sum(
            (t.get('sell_price',0) - t.get('buy_price',0)) * t.get('buy_qty',0)
            for t in closed_t
        )
        sign = '+' if total_pnl >= 0 else ''
        return (
            f"📊 *Summary*\n"
            f"Total trades: {len(trades)}\n"
            f"Open: {len(open_t)}  |  Closed: {len(closed_t)}\n"
            f"Total realised P&L: {sign}₹{fmt_inr(total_pnl)}"
        )

    # /addclient CODE NAME
    m = re.match(r'^/addclient\s+([A-Z0-9]+)\s+(.+)$', text.strip(), re.IGNORECASE)
    if m:
        code = m.group(1).upper()
        name = m.group(2).strip()
        c = load_cfg()
        if 'clients' not in c:
            c['clients'] = {}
        if code in c['clients']:
            return f"Client *{code}* already exists as _{c['clients'][code]}_."
        c['clients'][code] = name
        save_cfg(c)
        _push_config_to_github(c)
        return (f"✅ Client *{code}* — _{name}_ added.\n"
                f"GitHub Secret updated. Next nightly run will include this client.")

    # /clients — list all clients
    if tl in ('/clients', '/listclients'):
        names = get_names()
        if not names:
            return "No clients configured."
        lines = ["*Configured Clients:*"]
        for code, name in names.items():
            lines.append(f"  `{code}` — {name}")
        return '\n'.join(lines)

    # /removeclient CODE
    m = re.match(r'^/removeclient\s+([A-Z0-9]+)$', text.strip(), re.IGNORECASE)
    if m:
        code = m.group(1).upper()
        c = load_cfg()
        if code not in c.get('clients', {}):
            return f"Client *{code}* not found."
        removed_name = c['clients'].pop(code)
        save_cfg(c)
        _push_config_to_github(c)
        return f"✅ Client *{code}* — _{removed_name}_ removed. GitHub Secret updated."

    # /run — trigger daily pipeline
    if tl == '/run':
        trigger_daily_run(chat_id)
        return None  # response sent async from thread

    # /alert SYMBOL PRICE [above|below]
    m = re.match(r'^/alert\s+([A-Z0-9&]+)\s+([\d.]+)(?:\s+(above|below))?$', text.upper())
    if m:
        sym_key   = m.group(1)
        target    = float(m.group(2))
        forced_dir = m.group(3)  # 'ABOVE', 'BELOW', or None
        alerts  = load_alerts()
        current = fetch_single_cmp(sym_key)
        if forced_dir:
            direction = forced_dir.lower()
        elif current is not None:
            direction = 'above' if target > current else 'below'
        else:
            direction = 'above'  # default when CMP unavailable
        alerts[sym_key] = {'target': target, 'chat_id': chat_id, 'direction': direction}
        save_alerts(alerts)
        cmp_str = f" (CMP ₹{current:.2f})" if current else ""
        return (f"🔔 Alert set: *{sym_key}* {'≥' if direction=='above' else '≤'} ₹{target:.2f}"
                f"{cmp_str}\nYou'll be notified when triggered.")

    if tl == '/alerts':
        alerts = load_alerts()
        if not alerts:
            return "No active alerts."
        lines = ["*Active Alerts:*"]
        for s, info in alerts.items():
            d = '≥' if info.get('direction','above') == 'above' else '≤'
            lines.append(f"  {s}  {d} ₹{info['target']:.2f}")
        return '\n'.join(lines)

    m = re.match(r'^/cancelalert\s+([A-Z0-9&]+)$', text.upper())
    if m:
        sym_key = m.group(1)
        alerts  = load_alerts()
        if sym_key in alerts:
            del alerts[sym_key]
            save_alerts(alerts)
            return f"✅ Alert for {sym_key} cancelled."
        return f"No alert found for {sym_key}."

    # Client-specific query
    client = detect_client(text)
    if client:
        if any(w in tl for w in ('open', 'position', 'trade', 'holding')):
            return trades_summary_for(client, trades)
        if any(w in tl for w in ('ledger', 'balance', 'debit', 'credit', 'mtf')):
            d = ledger.get(client, {})
            name = NAMES.get(client, client)
            return (
                f"*{name}* ledger\n"
                f"Delivery: ₹{fmt_inr(d.get('combined', 0))}\n"
                f"MTF:      ₹{fmt_inr(d.get('mtf', 0))}"
            )
        rows = [t for t in trades if t.get('client') == client]
        context = f"Client: {NAMES.get(client, client)} ({client})\n"
        context += f"Trades: {json.dumps(rows[:40])}\n"
        context += f"Ledger: {json.dumps(ledger.get(client, {}))}"
        return ask_groq(text, context)

    # General free-form
    open_t   = [t for t in trades if not t.get('exit_date')]
    closed_t = [t for t in trades if t.get('exit_date')]
    context  = f"Open trades ({len(open_t)}): {json.dumps(open_t[:30])}\n"
    context += f"Ledger: {json.dumps(ledger)}"
    return ask_groq(text, context)


# ── Telegram polling ──────────────────────────────────────────────────────────

def tg(method: str, _socket_timeout: int = 15, **kwargs):
    data = urllib.parse.urlencode(kwargs).encode()
    req  = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=_socket_timeout) as r:
        return json.loads(r.read())

def send(chat_id: str, text: str):
    MAX = 4000
    chunks = [text[i:i+MAX] for i in range(0, len(text), MAX)]
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
    # Start price alert poller in background
    threading.Thread(target=alert_poller, daemon=True).start()
    print("Price alert poller started.")

    offset = 0
    while True:
        try:
            updates = tg('getUpdates', _socket_timeout=35, offset=offset, timeout=30)
            for upd in updates.get('result', []):
                offset = upd['update_id'] + 1
                msg    = upd.get('message') or upd.get('edited_message')
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
        except urllib.error.URLError as e:
            print(f"Network error: {e} — retrying in 5s")
            time.sleep(5)
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(3)

if __name__ == '__main__':
    main()
