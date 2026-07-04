"""
Telegram bot for Client Tracker MOFSL.
Answers natural-language questions about trades, positions and ledger.
Fast: pre-filters data before sending to Groq so LLM only sees relevant rows.
"""
import json, os, re, time, urllib.request, urllib.parse, urllib.error
from typing import Optional
from groq import Groq
try:
    import yfinance as yf
    _YF = True
except ImportError:
    _YF = False

BASE = os.path.dirname(os.path.abspath(__file__))

cfg         = json.load(open(os.path.join(BASE, 'bot_config.json')))
TOKEN       = cfg['telegram_token']
CHAT_IDS    = {c.strip() for c in str(cfg['allowed_chat_id']).split(',')}
groq_client = Groq(api_key=cfg['groq_api_key'])

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
NAME_TO_CODE = {v.lower(): k for k, v in NAMES.items()}
NAME_TO_CODE.update({k.lower(): k for k in NAMES})  # also match codes directly

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


# ── Data helpers ──────────────────────────────────────────────────────────────

def load_trades():
    path = os.path.join(BASE, 'trades.json')
    try:
        return json.load(open(path))
    except Exception:
        return []

def load_ledger():
    path = os.path.join(BASE, 'ledger.json')
    try:
        return json.load(open(path))
    except Exception:
        return {}

def detect_client(text: str) -> Optional[str]:
    """Return client code if text mentions a known client name or code."""
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
    """Fetch live CMP for a list of NSE symbols. Returns {symbol: price}."""
    if not _YF or not symbols:
        return {}
    try:
        tickers = [s + '.NS' for s in symbols]
        data = yf.download(tickers, period='1d', interval='1m',
                           progress=False, auto_adjust=True)
        result = {}
        for s, t in zip(symbols, tickers):
            try:
                price = float(data['Close'][t].dropna().iloc[-1])
                result[s] = price
            except Exception:
                pass
        return result
    except Exception:
        return {}

def trades_summary_for(client: str, trades: list) -> str:
    rows = [t for t in trades if t.get('client') == client]
    if not rows:
        return f"No trades found for {NAMES.get(client, client)}."
    open_t   = [t for t in rows if not t.get('exit_date')]
    closed_t = [t for t in rows if t.get('exit_date')]

    # Fetch live CMP for open positions
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
    for code, rows in by_client.items():
        lines.append(f"*{NAMES.get(code, code)}* ({len(rows)})")
        for t in rows:
            lines.append(f"  {sym(t.get('script','?'))}  qty={int(t.get('buy_qty',0))}  @₹{t.get('buy_price',0):.2f}")
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

def handle(text: str) -> str:
    text = text.strip()
    tl   = text.lower()
    trades = load_trades()
    ledger = load_ledger()

    # /start or /help
    if tl in ('/start', '/help'):
        return (
            "📊 *Client Tracker Bot*\n\n"
            "Commands:\n"
            "/open — all open positions\n"
            "/ledger — all ledger balances\n"
            "/summary — today's snapshot\n\n"
            "Or just ask naturally:\n"
            "_'Sathyavrath open trades'_\n"
            "_'What is Savitha's ledger?'_\n"
            "_'Show Iranna positions'_"
        )

    # /open — all open positions
    if tl in ('/open', 'open', 'open positions', 'all open'):
        return all_open_summary(trades)

    # /ledger — all ledger balances
    if tl in ('/ledger', 'ledger', 'balances', 'ledger balance'):
        return ledger_summary(ledger)

    # /summary
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

    # Client-specific query
    client = detect_client(text)
    if client:
        # Quick structured response for simple queries
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
        # General question about this client — send filtered data to Groq
        rows = [t for t in trades if t.get('client') == client]
        context = f"Client: {NAMES.get(client, client)} ({client})\n"
        context += f"Trades: {json.dumps(rows[:40])}\n"
        context += f"Ledger: {json.dumps(ledger.get(client, {}))}"
        return ask_groq(text, context)

    # General free-form — send compact summary to Groq
    open_t   = [t for t in trades if not t.get('exit_date')]
    closed_t = [t for t in trades if t.get('exit_date')]
    context  = f"Open trades ({len(open_t)}): {json.dumps(open_t[:30])}\n"
    context += f"Ledger: {json.dumps(ledger)}"
    return ask_groq(text, context)


# ── Telegram polling ──────────────────────────────────────────────────────────

def tg(method: str, **kwargs):
    data = urllib.parse.urlencode(kwargs).encode()
    req  = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

def send(chat_id: str, text: str):
    try:
        tg('sendMessage', chat_id=chat_id, text=text, parse_mode='Markdown')
    except Exception:
        # Fallback without markdown
        try:
            tg('sendMessage', chat_id=chat_id, text=text)
        except Exception as e:
            print(f"Send error: {e}")

def main():
    print("Bot starting...")
    offset = 0
    while True:
        try:
            updates = tg('getUpdates', offset=offset, timeout=30)
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
                    reply = handle(text)
                except Exception as e:
                    reply = f"Error: {e}"
                send(chat_id, reply)
        except urllib.error.URLError as e:
            print(f"Network error: {e} — retrying in 5s")
            time.sleep(5)
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(3)

if __name__ == '__main__':
    main()
