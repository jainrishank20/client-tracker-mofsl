"""
Raghava Tracker — Telegram Bot
Python computes answers directly; LLM only parses intent.
"""

import json
import os
from pathlib import Path
from collections import defaultdict
from datetime import date, timedelta

import yfinance as yf
from groq import Groq
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

BASE        = Path(__file__).parent
CONFIG_FILE = BASE / "bot_config.json"
TRADES_FILE = BASE / "trades.json"

CLIENT_NAMES = {
    "RIMK1209": "Sathyavrath",
    "RIMK1220": "Kalpana",
    "RIMK1238": "Iranna",
    "RIMK1248": "Udayakumar",
    "RIMK1249": "Sundareshwari",
    "RIMK1252": "Savitha",
}
NAME_TO_CODE = {v.upper(): k for k, v in CLIENT_NAMES.items()}
ALL_CODES    = list(CLIENT_NAMES.keys())

TICKER_ALIAS = {
    "BEL": "BHARAT ELECTRONICS", "RELIANCE": "RELIANCE INDUSTRIES",
    "ITC": "ITC", "TCS": "TCS", "INFY": "INFOSYS",
    "SBIN": "STATE BANK OF INDIA", "LT": "LARSEN",
    "TATAMOTORS": "TATA MOTORS", "TATASTEEL": "TATA STEEL",
    "BHARTIARTL": "BHARTI AIRTEL", "CANBK": "CANARA BANK",
    "IDBI": "IDBI BANK", "KPITTECH": "KPIT", "NEWGEN": "NEWGEN SOFTWARE",
    "MSTCLTD": "MSTC", "LICI": "LIC", "HSCL": "HIMADRI",
    "GMRAIRPORT": "GMR AIRPORTS", "CHOLAFIN": "CHOLAMANDALAM",
    "HINDCOPPER": "HINDUSTAN COPPER", "TCI": "TRANSPORT CORP",
    "ALKYL": "ALKYL AMINES", "SUZLON": "SUZLON ENERGY",
    "NATCO": "NATCO PHARMA", "NIIT": "NIIT",
    "INTELLECT": "INTELLECT DESIGN", "SIB": "SOUTH INDIAN BANK",
    "INOX": "INOX WIND", "EMMVEE": "EMMVEE PHOTOVOLTAIC",
    "NETWEB": "NETWEB TECHNOLOGIES", "HIMADRI": "HIMADRI SPECIALITY CHEM",
    "VARUN": "VARUN BEVERAGES", "ANGEL": "ANGEL ONE",
}

SYMBOL_MAP = {
    "ALKYL AMINES":"ALKYLAMINE","BHARAT COKING COAL":"BHARATCOAL","BHARAT ELECTRONICS":"BEL",
    "CAMS":"CAMS","CENTURY PLYBOARDS":"CENTURYPLY","COFORGE":"COFORGE",
    "DATA PATTERNS":"DATAPATTNS","DIXON TECHNOLOGIES":"DIXON","EMMVEE PHOTOVOLTAIC":"EMMVEE",
    "EQUITAS SMALL FIN BANK":"EQUITASBNK","EQUITAS SMALL FIN BNK":"EQUITASBNK",
    "EXIDE INDUSTRIES":"EXIDEIND","FORTIS HEALTHCARE":"FORTIS","GMR AIRPORTS":"GMRAIRPORT",
    "GMR AIRPORTS LIMITED":"GMRAIRPORT","GODREJ INDUSTRIES":"GODREJIND",
    "GODREJ PROPERTIES":"GODREJPROP","HIMADRI SPECIALITY CHEM":"HSCL",
    "HINDUSTAN COPPER":"HINDCOPPER","HINDUSTAN ZINC":"HINDZINC","IDBI BANK":"IDBI",
    "IFCI":"IFCI","IIFL FINANCE":"IIFL","INFOSYS":"INFY","INTELLECT DESIGN ARENA":"INTELLECT",
    "INTERGLOBE AVIATION":"INDIGO","INOX WIND":"INOXWIND","ITC":"ITC",
    "JAIN RESOURCE RECYCLING LIMITE":"JAINRES","JBM AUTO":"JBMA","JM FINANCIAL":"JMFINANCIL",
    "KALPATARU":"KALPATARU","KALPATARU PROJECTS":"KPIL","KAYNES TECHNOLOGY IND LTD":"KAYNES",
    "LARSEN & TOUBRO LTD.":"LT","LAURUS LABS":"LAURUSLABS","M&M FINANCE":"M&MFIN",
    "MAX HEALTHCARE INS LTD":"MAXHEALTH","MIRAE ENERGY ETF":"MAFANG","MMTC":"MMTC",
    "MOSCHIP TECHNOLOGIES":"MOSCHIP","MRPL":"MRPL","NATCO PHARMA":"NATCOPHARM",
    "NETWEB TECHNOLOGIES":"NETWEB","NIIT LIMITED":"NIIT","NMDC":"NMDC",
    "NIPPON LIFE AMC":"NAM-INDIA","NIPPON ETF LIQUID BEES":"LIQUIDBEES","NTPC":"NTPC",
    "ORACLE FINANCIAL SERVICES":"OFSS","PARADEEP PHOSPHATES":"PARADEEP",
    "PG ELECTROPLAST":"PGEL","PI INDUSTRIES":"PIIND","POONAWALLA FINCORP":"POONAWALLA",
    "REC":"RECLTD","REC LIMITED":"RECLTD","REDINGTON":"REDINGTON","SAGILITY LIMITED":"SAGILITY",
    "SAMVARDHANA MOTHERSON":"MOTHERSON","SHAILY ENGINEERING":"SHAILY",
    "SUZLON ENERGY":"SUZLON","TCS":"TCS","TEJAS NETWORKS":"TEJASNET",
    "TRANSPORT CORP OF INDIA":"TCI","TRANSPORT CORPN OF INDIA":"TCI",
    "VARUN BEVERAGES":"VBL","VST INDUSTRIES":"VSTIND","WIPRO":"WIPRO",
    "WOCKHARDT":"WOCKPHARMA","ZEN TECHNOLOGIES":"ZENTEC","ZYDUS LIFESCIENCES":"ZYDUSLIFE",
    "ANGEL ONE":"ANGELONE","BANK OF INDIA":"BANKINDIA","BLUE JET HEALTHCARE":"BLUEJET",
    "GRAPHITE INDIA":"GRAPHITE","GRINDWELL NORTON":"GRINDWELL","CANARA BANK":"CANBK",
    "BHARAT ELECTRONICS":"BEL","STATE BANK OF INDIA":"SBIN","MSTC":"MSTCLTD",
    "NEWGEN SOFTWARE":"NEWGEN","ALEMBIC PHARMA":"APLLTD","ASHAPURA MINECHEM":"ASHAPURMIN",
    "GUJARAT NARMADA FERT":"GNFC","AVENUE SUPERMARTS LIMITED":"DMART",
    "BSE LIMITED":"BSE","JUPITER WAGONS LIMITED":"JWL","ELGI EQUIPMENTS LTD":"ELGIEQUIP",
    "IRB INFRA DEV LTD.":"IRB","K.P.R.MILL LIMITED":"KPRMILL","PREMIER ENERGIES LIMITED":"PREMIERENE",
    "DELHIVERY":"DELHIVERY","POWER FIN CORP LTD.":"PFC",
}


def script_to_yf(script: str) -> str:
    sym = SYMBOL_MAP.get(script)
    if not sym:
        # fallback: strip spaces/dots
        sym = script.replace(" ", "").replace(".", "").replace("&", "").upper()
    return sym + ".NS"


def fetch_prices(scripts: list) -> dict[str, dict]:
    """Returns {script: {cmp, prev_close, change_pct}} for each script."""
    results = {s: {"cmp": None, "prev_close": None, "change_pct": None} for s in scripts}
    if not scripts:
        return results
    t2s = {script_to_yf(s): s for s in scripts}
    try:
        data = yf.download(
            " ".join(t2s.keys()), period="2d", interval="1d",
            progress=False, auto_adjust=True, threads=True
        )
        if not data.empty:
            close = data["Close"] if "Close" in data.columns else data.get("close")
            if close is None:
                raise ValueError("no close column")
            # Handle single vs multi ticker
            if hasattr(close, "columns"):
                for ticker, script in t2s.items():
                    col = close[ticker].dropna() if ticker in close.columns else None
                    if col is not None and len(col) >= 1:
                        cmp  = round(float(col.iloc[-1]), 2)
                        prev = round(float(col.iloc[-2]), 2) if len(col) >= 2 else cmp
                        results[script] = {
                            "cmp": cmp, "prev_close": prev,
                            "change_pct": round((cmp - prev) / prev * 100, 2) if prev else None
                        }
            else:
                # single ticker
                script = list(t2s.values())[0]
                col    = close.dropna()
                if len(col) >= 1:
                    cmp  = round(float(col.iloc[-1]), 2)
                    prev = round(float(col.iloc[-2]), 2) if len(col) >= 2 else cmp
                    results[script] = {
                        "cmp": cmp, "prev_close": prev,
                        "change_pct": round((cmp - prev) / prev * 100, 2) if prev else None
                    }
    except Exception:
        pass
    return results


def answer_movers(trades, client, threshold_pct: float, direction: str) -> str:
    """direction: 'up', 'down', or 'any'"""
    pool      = [t for t in trades if (not client or t["client"] == client)
                 and not t.get("exit_date")]
    if not pool:
        return "No open positions found."

    # Unique scripts
    scripts = list({t["script"] for t in pool})
    prices  = fetch_prices(scripts)

    results = []
    for scr, px in prices.items():
        pct = px.get("change_pct")
        if pct is None:
            continue
        if direction == "up"   and pct < threshold_pct:
            continue
        if direction == "down" and pct > -threshold_pct:
            continue
        if direction == "any"  and abs(pct) < threshold_pct:
            continue
        # which clients hold this
        holders = list({t["client"] for t in pool if t["script"] == scr})
        holder_names = ", ".join(CLIENT_NAMES.get(c, c) for c in sorted(holders))
        results.append((pct, scr, px["cmp"], px["prev_close"], holder_names))

    if not results:
        dir_label = {"up": f"+{threshold_pct}%+ up", "down": f"-{threshold_pct}%+ down", "any": f"{threshold_pct}%+ move"}[direction]
        name = CLIENT_NAMES.get(client, client) if client else "open portfolio"
        return f"No stocks in {name} with {dir_label} move today."

    results.sort(key=lambda x: x[0], reverse=(direction != "down"))
    name  = CLIENT_NAMES.get(client, client) if client else "Open Portfolio"
    label = {"up": "📈 Stocks Up Today", "down": "📉 Stocks Down Today", "any": "📊 Movers Today"}[direction]
    lines = [f"*{name} — {label}*\n"]
    for pct, scr, cmp, prev, holders in results:
        arrow = "🟢" if pct >= 0 else "🔴"
        lines.append(f"{arrow} *{scr}*: {fmt_inr(cmp)} ({pct:+.1f}%) | {holders}")
    lines.append(f"\n_Prev close → today's price_")
    return "\n".join(lines)


PARSE_PROMPT = """You are a query parser for a stock portfolio bot. Today is {today}. Extract from the user message:
- client: RIMK code (map names: Sathyavrath→RIMK1209, Kalpana→RIMK1220, Iranna→RIMK1238, Udayakumar→RIMK1248, Sundareshwari→RIMK1249, Savitha→RIMK1252). null if not mentioned.
- stock: stock name or ticker. null if not mentioned.
- intent: one of:
    open_positions  → open/current holdings
    closed_trades   → closed/exited trades
    stock_detail    → specific stock details (price, qty, p&l)
    stock_holders   → which clients hold a stock
    entry_date      → when was a stock bought
    pnl_on_date     → profit on a specific date/range
    monthly_pnl     → month-wise P&L breakdown
    long_positions  → positions held too long (stale)
    recent_activity → recent buys/sells
    top_trades      → best or worst trades
    concentration   → what % of portfolio is one stock
    compare_clients → compare two clients side by side
    movers_up       → stocks up today / up by X% today
    movers_down     → stocks down today / down by X% today
    movers_any      → any big movers today (no direction specified)
    client_summary  → overall summary for one client
    all_summary     → all clients overall
- filter: "open", "closed", "best", "worst", or "all"
- date_from: ISO YYYY-MM-DD. null if not mentioned.
- date_to: ISO YYYY-MM-DD. null if not mentioned.
- extra: integer (e.g. N for "top 5", days for "held > 60 days"). null if not mentioned.
- client2: second RIMK code for compare_clients intent. null otherwise.

Resolve relative dates from today ({today}): today, yesterday, this week (Mon→today), last week (prev Mon-Sun), this month (1st→today).

Respond ONLY with valid compact JSON, no explanation. Examples:
{{"client":"RIMK1209","stock":null,"intent":"pnl_on_date","filter":"closed","date_from":"2026-06-08","date_to":"2026-06-08","extra":null,"client2":null}}
{{"client":null,"stock":"SUZLON ENERGY","intent":"stock_holders","filter":"all","date_from":null,"date_to":null,"extra":null,"client2":null}}
{{"client":"RIMK1209","stock":null,"intent":"top_trades","filter":"best","date_from":null,"date_to":null,"extra":5,"client2":null}}
{{"client":"RIMK1209","stock":null,"intent":"long_positions","filter":"open","date_from":null,"date_to":null,"extra":60,"client2":null}}
{{"client":"RIMK1209","stock":null,"intent":"monthly_pnl","filter":"closed","date_from":null,"date_to":null,"extra":null,"client2":null}}
{{"client":"RIMK1209","stock":null,"intent":"compare_clients","filter":"all","date_from":null,"date_to":null,"extra":null,"client2":"RIMK1220"}}
{{"client":null,"stock":null,"intent":"recent_activity","filter":"all","date_from":"2026-06-03","date_to":"2026-06-10","extra":null,"client2":null}}
{{"client":"RIMK1209","stock":null,"intent":"movers_up","filter":"open","date_from":null,"date_to":null,"extra":0,"client2":null}}
{{"client":null,"stock":null,"intent":"movers_up","filter":"open","date_from":null,"date_to":null,"extra":5,"client2":null}}
{{"client":null,"stock":null,"intent":"movers_down","filter":"open","date_from":null,"date_to":null,"extra":5,"client2":null}}
{{"client":null,"stock":null,"intent":"movers_any","filter":"open","date_from":null,"date_to":null,"extra":3,"client2":null}}
{{"client":null,"stock":null,"intent":"all_summary","filter":"all","date_from":null,"date_to":null,"extra":null,"client2":null}}"""


# ── Config / data loading ──────────────────────────────────────────────────────

def load_config() -> dict:
    env_token   = os.environ.get("TELEGRAM_TOKEN")
    env_groq    = os.environ.get("GROQ_API_KEY")
    env_chat_id = os.environ.get("ALLOWED_CHAT_ID", "")
    if env_token and env_groq:
        return {"telegram_token": env_token, "groq_api_key": env_groq,
                "allowed_chat_id": env_chat_id}
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    template = {"telegram_token": "PASTE_BOT_TOKEN_HERE",
                "groq_api_key": "PASTE_GROQ_KEY_HERE", "allowed_chat_id": ""}
    CONFIG_FILE.write_text(json.dumps(template, indent=2), encoding="utf-8")
    return template


def load_trades() -> list:
    if TRADES_FILE.exists():
        return json.loads(TRADES_FILE.read_text(encoding="utf-8"))
    return []


def is_allowed(update: Update, cfg: dict) -> bool:
    allowed = cfg.get("allowed_chat_id", "")
    return not allowed or str(update.effective_chat.id) == str(allowed)


# ── Client / stock helpers ─────────────────────────────────────────────────────

def resolve_client(raw: str) -> str | None:
    if not raw:
        return None
    up = raw.strip().upper()
    if up in CLIENT_NAMES:
        return up
    if up in NAME_TO_CODE:
        return NAME_TO_CODE[up]
    digits = up.replace("RIMK", "").strip()
    for code in ALL_CODES:
        if code.startswith(up) or up.startswith(code[:7]):
            return code
        if digits and code.endswith(digits):
            return code
    return None


def resolve_stock(raw: str) -> str:
    if not raw:
        return ""
    su = raw.strip().upper()
    return TICKER_ALIAS.get(su, su)


def match_stock(script: str, su: str) -> bool:
    s = (script or "").upper()
    return su in s or s in su


# ── Maths helpers ──────────────────────────────────────────────────────────────

def wavg_buy(lots) -> float:
    total_qty = sum(t["buy_qty"] for t in lots)
    if not total_qty:
        return 0
    return round(sum(t["buy_price"] * t["buy_qty"] for t in lots) / total_qty, 2)


def wavg_sell(lots) -> float:
    total_qty = sum(t.get("sell_qty") or 0 for t in lots)
    if not total_qty:
        return 0
    return round(sum((t.get("sell_price") or 0) * (t.get("sell_qty") or 0)
                     for t in lots) / total_qty, 2)


def compute_net_pnl(t) -> float:
    if t.get("net_pnl"):
        return float(t["net_pnl"])
    gross = (float(t.get("sell_price") or 0) - float(t.get("buy_price") or 0)) * float(t.get("buy_qty") or 0)
    charges = sum(float(t.get(f"buy_{k}", 0) or 0) + float(t.get(f"sell_{k}", 0) or 0)
                  for k in ["brokerage", "stt", "gst", "stamp", "txn_chrg"])
    return round(gross - charges, 2)


def holding_days(entry_date_str) -> int:
    try:
        return (date.today() - date.fromisoformat(entry_date_str[:10])).days
    except Exception:
        return 0


def fmt_inr(n: float) -> str:
    return f"₹{n:,.2f}"


def pnl_str(pnl: float) -> str:
    sign = "+" if pnl >= 0 else ""
    return f"{sign}{fmt_inr(pnl)}"


def ret_pct(buy_price, qty, pnl) -> str:
    invested = buy_price * qty
    if not invested:
        return ""
    return f"{pnl/invested*100:+.1f}%"


def period_label(date_from, date_to) -> str:
    if date_from and date_from == date_to:
        return f"on {date_from}"
    if date_from and date_to:
        return f"{date_from} to {date_to}"
    return "all time"


# ── Answer builders ────────────────────────────────────────────────────────────

def build_open_section(open_lots: list) -> list:
    lines = ["🟢 *Open Positions:*"]
    by_script = defaultdict(list)
    for t in open_lots:
        by_script[t["script"]].append(t)
    total_invested = 0
    for scr, lots in sorted(by_script.items()):
        qty      = sum(t["buy_qty"] for t in lots)
        avg      = wavg_buy(lots)
        invested = round(qty * avg, 2)
        total_invested += invested
        earliest = min(t["entry_date"] for t in lots)[:10]
        days     = holding_days(earliest)
        lines.append(f"  • *{scr}*: {qty:.0f} sh @ {fmt_inr(avg)} | "
                     f"{fmt_inr(invested)} | {days}d (since {earliest})")
    lines.append(f"  _Total deployed: {fmt_inr(total_invested)}_")
    return lines


def build_closed_section(closed_lots: list) -> list:
    lines = ["🔴 *Closed Trades:*"]
    by_script = defaultdict(list)
    for t in closed_lots:
        by_script[t["script"]].append(t)
    total_pnl = 0
    for scr, lots in sorted(by_script.items()):
        qty  = sum(t["buy_qty"] for t in lots)
        bp   = wavg_buy(lots)
        sp   = wavg_sell(lots)
        pnl  = sum(compute_net_pnl(t) for t in lots)
        total_pnl += pnl
        pct  = ret_pct(bp, qty, pnl)
        exit_d = max(t["exit_date"] for t in lots)[:10]
        lines.append(f"  • *{scr}*: {qty:.0f} sh | "
                     f"{fmt_inr(bp)} → {fmt_inr(sp)} | {pnl_str(pnl)} ({pct}) | {exit_d}")
    lines.append(f"  _Total P&L: {pnl_str(total_pnl)}_")
    return lines


def answer_open_positions(trades, client) -> str:
    pool      = [t for t in trades if not client or t["client"] == client]
    open_lots = [t for t in pool if not t.get("exit_date")]
    if not open_lots:
        return f"No open positions for {CLIENT_NAMES.get(client, client) if client else 'anyone'}."
    name  = CLIENT_NAMES.get(client, client) if client else "All Clients"
    lines = [f"*{name} — Open Positions*\n"]
    if not client:
        by_c = defaultdict(list)
        for t in open_lots:
            by_c[t["client"]].append(t)
        for c, lots in sorted(by_c.items()):
            lines.append(f"*{CLIENT_NAMES.get(c,c)}:*")
            lines += build_open_section(lots)
            lines.append("")
    else:
        lines += build_open_section(open_lots)
    return "\n".join(lines)


def answer_closed_trades(trades, client) -> str:
    pool        = [t for t in trades if not client or t["client"] == client]
    closed_lots = [t for t in pool if t.get("exit_date")]
    if not closed_lots:
        return f"No closed trades for {CLIENT_NAMES.get(client, client) if client else 'anyone'}."
    name  = CLIENT_NAMES.get(client, client) if client else "All Clients"
    lines = [f"*{name} — Closed Trades*\n"]
    if not client:
        by_c = defaultdict(list)
        for t in closed_lots:
            by_c[t["client"]].append(t)
        for c, lots in sorted(by_c.items()):
            lines.append(f"*{CLIENT_NAMES.get(c,c)}:*")
            lines += build_closed_section(lots)
            lines.append("")
    else:
        lines += build_closed_section(closed_lots)
    return "\n".join(lines)


def answer_stock_detail(trades, client, stock) -> str:
    su   = resolve_stock(stock)
    pool = [t for t in trades
            if (not client or t["client"] == client) and match_stock(t.get("script"), su)]
    if not pool:
        return f"No trades found for {stock}" + (f" in {CLIENT_NAMES.get(client,client)}" if client else "") + "."
    lines = []
    by_cs = defaultdict(list)
    for t in pool:
        by_cs[(t["client"], t["script"])].append(t)
    for (c, scr), lots in sorted(by_cs.items()):
        open_lots   = [t for t in lots if not t.get("exit_date")]
        closed_lots = [t for t in lots if t.get("exit_date")]
        lines.append(f"*{scr}* — {CLIENT_NAMES.get(c,c)}\n")
        if open_lots:
            qty  = sum(t["buy_qty"] for t in open_lots)
            avg  = wavg_buy(open_lots)
            days = holding_days(min(t["entry_date"] for t in open_lots))
            lines.append(f"🟢 *Open:* {qty:.0f} sh @ avg {fmt_inr(avg)} | "
                         f"Invested {fmt_inr(round(qty*avg,2))} | {days} days")
        else:
            lines.append("🟢 *Open:* None")
        if closed_lots:
            qty = sum(t["buy_qty"] for t in closed_lots)
            bp  = wavg_buy(closed_lots)
            sp  = wavg_sell(closed_lots)
            pnl = sum(compute_net_pnl(t) for t in closed_lots)
            lines.append(f"🔴 *Closed:* {qty:.0f} sh | {fmt_inr(bp)} → {fmt_inr(sp)} | "
                         f"P&L {pnl_str(pnl)} ({ret_pct(bp,qty,pnl)})")
        else:
            lines.append("🔴 *Closed:* None")
        lines.append("")
    return "\n".join(lines).strip()


def answer_stock_holders(trades, stock) -> str:
    su = resolve_stock(stock)
    lines = [f"*Who holds {stock.upper()}?*\n"]
    found = False
    for code, name in CLIENT_NAMES.items():
        pool        = [t for t in trades if t["client"] == code and match_stock(t.get("script"), su)]
        open_lots   = [t for t in pool if not t.get("exit_date")]
        closed_lots = [t for t in pool if t.get("exit_date")]
        if not pool:
            continue
        found = True
        parts = []
        if open_lots:
            qty = sum(t["buy_qty"] for t in open_lots)
            avg = wavg_buy(open_lots)
            parts.append(f"🟢 {qty:.0f} sh @ {fmt_inr(avg)}")
        if closed_lots:
            pnl = sum(compute_net_pnl(t) for t in closed_lots)
            parts.append(f"🔴 closed {pnl_str(pnl)}")
        lines.append(f"  • *{name}*: " + " | ".join(parts))
    if not found:
        return f"No client holds {stock.upper()}."
    return "\n".join(lines)


def answer_entry_date(trades, client, stock) -> str:
    su   = resolve_stock(stock)
    pool = [t for t in trades
            if (not client or t["client"] == client) and match_stock(t.get("script"), su)]
    if not pool:
        return f"No trades found for {stock}."
    lines = []
    by_cs = defaultdict(list)
    for t in pool:
        by_cs[(t["client"], t["script"])].append(t)
    for (c, scr), lots in sorted(by_cs.items()):
        lines.append(f"*{scr}* — {CLIENT_NAMES.get(c,c)}\n")
        for t in sorted(lots, key=lambda x: x["entry_date"]):
            if not t.get("exit_date"):
                days = holding_days(t["entry_date"])
                lines.append(f"🟢 Bought {t['buy_qty']:.0f} sh on *{t['entry_date'][:10]}* "
                             f"@ {fmt_inr(t['buy_price'])} | holding {days}d")
            else:
                lines.append(f"🔴 Bought {t['buy_qty']:.0f} sh on *{t['entry_date'][:10]}* "
                             f"@ {fmt_inr(t['buy_price'])} | sold {t['exit_date'][:10]}")
        lines.append("")
    return "\n".join(lines).strip()


def answer_pnl_on_date(trades, client, date_from, date_to) -> str:
    pool = [t for t in trades
            if (not client or t["client"] == client)
            and t.get("exit_date")
            and (not date_from or t["exit_date"][:10] >= date_from)
            and (not date_to   or t["exit_date"][:10] <= date_to)]
    if not pool:
        return f"No trades closed {period_label(date_from, date_to)} for " \
               f"{CLIENT_NAMES.get(client, client) if client else 'any client'}."
    name  = CLIENT_NAMES.get(client, client) if client else "All Clients"
    lines = [f"*{name} — Trades closed {period_label(date_from, date_to)}*\n"]
    by_c  = defaultdict(list)
    for t in pool:
        by_c[t["client"]].append(t)
    total_pnl = 0
    for c, lots in sorted(by_c.items()):
        if not client:
            lines.append(f"*{CLIENT_NAMES.get(c,c)}:*")
        by_s = defaultdict(list)
        for t in lots:
            by_s[t["script"]].append(t)
        sub = 0
        for scr, slts in sorted(by_s.items()):
            qty = sum(t["buy_qty"] for t in slts)
            bp  = wavg_buy(slts)
            sp  = wavg_sell(slts)
            pnl = sum(compute_net_pnl(t) for t in slts)
            sub += pnl
            lines.append(f"  • *{scr}*: {qty:.0f} sh | {fmt_inr(bp)} → {fmt_inr(sp)} | "
                         f"{pnl_str(pnl)} ({ret_pct(bp,qty,pnl)})")
        if not client:
            lines.append(f"  _Subtotal: {pnl_str(sub)}_\n")
        total_pnl += sub
    lines.append(f"\n💰 *Total: {pnl_str(total_pnl)}*")
    return "\n".join(lines)


def answer_monthly_pnl(trades, client) -> str:
    pool = [t for t in trades
            if (not client or t["client"] == client) and t.get("exit_date")]
    if not pool:
        return "No closed trades found."
    name  = CLIENT_NAMES.get(client, client) if client else "All Clients"
    lines = [f"*{name} — Monthly P&L*\n"]
    by_month = defaultdict(list)
    for t in pool:
        month = t["exit_date"][:7]  # YYYY-MM
        by_month[month].append(t)
    grand = 0
    for month in sorted(by_month.keys()):
        lots = by_month[month]
        pnl  = sum(compute_net_pnl(t) for t in lots)
        grand += pnl
        bar = "█" * min(int(abs(pnl) / 5000), 10)
        sign = "🟢" if pnl >= 0 else "🔴"
        lines.append(f"{sign} *{month}*: {pnl_str(pnl)} ({len(lots)} trades) {bar}")
    lines.append(f"\n_Cumulative: {pnl_str(grand)}_")
    return "\n".join(lines)


def answer_long_positions(trades, client, min_days: int = 60) -> str:
    pool = [t for t in trades
            if (not client or t["client"] == client)
            and not t.get("exit_date")
            and holding_days(t["entry_date"]) >= min_days]
    if not pool:
        name = CLIENT_NAMES.get(client, client) if client else "anyone"
        return f"No positions held > {min_days} days for {name}."
    name  = CLIENT_NAMES.get(client, client) if client else "All Clients"
    lines = [f"*{name} — Positions held > {min_days} days*\n"]
    by_cs = defaultdict(list)
    for t in pool:
        by_cs[(t["client"], t["script"])].append(t)
    for (c, scr), lots in sorted(by_cs.items(), key=lambda x: -holding_days(min(t["entry_date"] for t in x[1]))):
        qty  = sum(t["buy_qty"] for t in lots)
        avg  = wavg_buy(lots)
        days = holding_days(min(t["entry_date"] for t in lots))
        inv  = round(qty * avg, 2)
        cname = CLIENT_NAMES.get(c, c)
        lines.append(f"  ⏳ *{scr}* ({cname}): {qty:.0f} sh @ {fmt_inr(avg)} | "
                     f"{fmt_inr(inv)} | *{days} days*")
    return "\n".join(lines)


def answer_recent_activity(trades, client, date_from, date_to) -> str:
    today_str = date.today().isoformat()
    df = date_from or (date.today() - timedelta(days=7)).isoformat()
    dt = date_to   or today_str
    pool = [t for t in trades if not client or t["client"] == client]
    buys  = [t for t in pool if t["entry_date"][:10] >= df and t["entry_date"][:10] <= dt]
    sells = [t for t in pool if t.get("exit_date") and
             t["exit_date"][:10] >= df and t["exit_date"][:10] <= dt]
    if not buys and not sells:
        return f"No activity from {df} to {dt}."
    name  = CLIENT_NAMES.get(client, client) if client else "All Clients"
    lines = [f"*{name} — Activity {df} to {dt}*\n"]
    if buys:
        lines.append("📥 *Buys:*")
        by_date = defaultdict(list)
        for t in buys:
            by_date[t["entry_date"][:10]].append(t)
        for d in sorted(by_date.keys()):
            for t in by_date[d]:
                cname = "" if client else f" ({CLIENT_NAMES.get(t['client'],t['client'])})"
                lines.append(f"  {d} — *{t['script']}*{cname}: "
                             f"{t['buy_qty']:.0f} sh @ {fmt_inr(t['buy_price'])}")
    if sells:
        lines.append("\n📤 *Sells:*")
        by_date = defaultdict(list)
        for t in sells:
            by_date[t["exit_date"][:10]].append(t)
        for d in sorted(by_date.keys()):
            for t in by_date[d]:
                pnl   = compute_net_pnl(t)
                cname = "" if client else f" ({CLIENT_NAMES.get(t['client'],t['client'])})"
                lines.append(f"  {d} — *{t['script']}*{cname}: "
                             f"{t['buy_qty']:.0f} sh @ {fmt_inr(t['sell_price'])} | {pnl_str(pnl)}")
    return "\n".join(lines)


def answer_top_trades(trades, client, n: int = 5, best: bool = True) -> str:
    pool = [t for t in trades
            if (not client or t["client"] == client) and t.get("exit_date")]
    if not pool:
        return "No closed trades found."
    ranked = sorted(pool, key=lambda t: compute_net_pnl(t), reverse=best)[:n]
    label  = "Best" if best else "Worst"
    name   = CLIENT_NAMES.get(client, client) if client else "All Clients"
    lines  = [f"*{name} — {label} {n} Trades*\n"]
    for i, t in enumerate(ranked, 1):
        pnl   = compute_net_pnl(t)
        pct   = ret_pct(t["buy_price"], t["buy_qty"], pnl)
        cname = "" if client else f" ({CLIENT_NAMES.get(t['client'],t['client'])})"
        lines.append(f"{i}. *{t['script']}*{cname}: {t['buy_qty']:.0f} sh | "
                     f"{fmt_inr(t['buy_price'])} → {fmt_inr(t['sell_price'])} | "
                     f"{pnl_str(pnl)} ({pct})")
    return "\n".join(lines)


def answer_concentration(trades, client, stock) -> str:
    su   = resolve_stock(stock)
    pool = [t for t in trades if not client or t["client"] == client]
    open_lots = [t for t in pool if not t.get("exit_date")]
    if not open_lots:
        return "No open positions found."
    total_inv = sum(t["buy_price"] * t["buy_qty"] for t in open_lots)
    scr_lots  = [t for t in open_lots if match_stock(t.get("script"), su)]
    if not scr_lots:
        return f"{stock.upper()} is not in any open position."
    scr_inv = sum(t["buy_price"] * t["buy_qty"] for t in scr_lots)
    pct     = round(scr_inv / total_inv * 100, 1) if total_inv else 0
    name    = CLIENT_NAMES.get(client, client) if client else "Portfolio"
    scr_name = scr_lots[0]["script"]
    return (f"*{scr_name}* is *{pct}%* of {name}'s open portfolio\n"
            f"  Invested: {fmt_inr(scr_inv)} of {fmt_inr(total_inv)} total")


def answer_compare_clients(trades, client1, client2) -> str:
    if not client1 or not client2:
        return "Please mention two client names to compare."
    lines = [f"*{CLIENT_NAMES.get(client1,client1)} vs {CLIENT_NAMES.get(client2,client2)}*\n"]
    rows  = []
    for c in [client1, client2]:
        ct        = [t for t in trades if t["client"] == c]
        open_t    = [t for t in ct if not t.get("exit_date")]
        closed_t  = [t for t in ct if t.get("exit_date")]
        inv       = sum(t["buy_price"] * t["buy_qty"] for t in open_t)
        pnl       = sum(compute_net_pnl(t) for t in closed_t)
        wins      = sum(1 for t in closed_t if compute_net_pnl(t) > 0)
        win_rate  = int(round(wins / len(closed_t) * 100)) if closed_t else 0
        best_t    = max(closed_t, key=lambda t: compute_net_pnl(t), default=None)
        rows.append((CLIENT_NAMES.get(c,c), len(open_t), len(closed_t), inv, pnl, win_rate, best_t))
    for name, n_open, n_closed, inv, pnl, wr, best_t in rows:
        lines.append(f"*{name}*")
        lines.append(f"  Open: {n_open} stocks | Deployed: {fmt_inr(inv)}")
        lines.append(f"  Closed: {n_closed} trades | Booked P&L: {pnl_str(pnl)}")
        lines.append(f"  Win rate: {wr}%")
        if best_t:
            lines.append(f"  Best: {best_t['script']} → {pnl_str(compute_net_pnl(best_t))}")
        lines.append("")
    return "\n".join(lines).strip()


def answer_client_summary(trades, client) -> str:
    pool = [t for t in trades if not client or t["client"] == client]
    if not pool:
        return f"No data for {CLIENT_NAMES.get(client, client)}."
    open_lots   = [t for t in pool if not t.get("exit_date")]
    closed_lots = [t for t in pool if t.get("exit_date")]
    name        = CLIENT_NAMES.get(client, client) if client else "Portfolio"
    invested    = sum(t["buy_price"] * t["buy_qty"] for t in open_lots)
    booked_pnl  = sum(compute_net_pnl(t) for t in closed_lots)
    wins        = sum(1 for t in closed_lots if compute_net_pnl(t) > 0)
    win_rate    = int(round(wins / len(closed_lots) * 100)) if closed_lots else 0
    best        = max(closed_lots, key=lambda t: compute_net_pnl(t), default=None)
    worst       = min(closed_lots, key=lambda t: compute_net_pnl(t), default=None)
    avg_hold    = round(sum(holding_days(t["entry_date"]) for t in open_lots) / len(open_lots)) if open_lots else 0
    lines = [
        f"*{name} — Summary*\n",
        f"📂 Open: {len({t['script'] for t in open_lots})} stocks | Deployed: {fmt_inr(invested)}",
        f"   Avg holding: {avg_hold} days",
        f"✅ Closed: {len(closed_lots)} trades | Booked P&L: {pnl_str(booked_pnl)}",
        f"🎯 Win rate: {win_rate}% ({wins}/{len(closed_lots)})",
    ]
    if best:
        lines.append(f"🏆 Best: {best['script']} → {pnl_str(compute_net_pnl(best))}")
    if worst:
        lines.append(f"📉 Worst: {worst['script']} → {pnl_str(compute_net_pnl(worst))}")
    return "\n".join(lines)


def answer_all_summary(trades) -> str:
    lines = ["📊 *Portfolio Overview*\n"]
    total_inv = total_pnl = 0
    for code, name in CLIENT_NAMES.items():
        ct      = [t for t in trades if t["client"] == code]
        open_t  = [t for t in ct if not t.get("exit_date")]
        closed_t = [t for t in ct if t.get("exit_date")]
        inv = sum(t["buy_price"] * t["buy_qty"] for t in open_t)
        pnl = sum(compute_net_pnl(t) for t in closed_t)
        total_inv += inv
        total_pnl += pnl
        lines.append(f"*{name}*: {len(open_t)} open | {len(closed_t)} closed | "
                     f"Deployed {fmt_inr(inv)} | P&L {pnl_str(pnl)}")
    lines.append(f"\n_Total deployed: {fmt_inr(total_inv)} | Total P&L: {pnl_str(total_pnl)}_")
    return "\n".join(lines)


def answer_today(trades) -> str:
    today_str = date.today().isoformat()
    buys  = [t for t in trades if t["entry_date"][:10] == today_str]
    sells = [t for t in trades if t.get("exit_date") and t["exit_date"][:10] == today_str]
    if not buys and not sells:
        return f"No trades recorded for today ({today_str})."
    lines = [f"📅 *Today's Activity — {today_str}*\n"]
    if buys:
        lines.append("📥 *Buys:*")
        for t in buys:
            lines.append(f"  • {CLIENT_NAMES.get(t['client'],t['client'])} — "
                         f"*{t['script']}*: {t['buy_qty']:.0f} sh @ {fmt_inr(t['buy_price'])}")
    if sells:
        lines.append("\n📤 *Sells:*")
        total = 0
        for t in sells:
            pnl = compute_net_pnl(t)
            total += pnl
            lines.append(f"  • {CLIENT_NAMES.get(t['client'],t['client'])} — "
                         f"*{t['script']}*: {t['buy_qty']:.0f} sh @ {fmt_inr(t['sell_price'])} | {pnl_str(pnl)}")
        lines.append(f"\n💰 *Today's booked P&L: {pnl_str(total)}*")
    return "\n".join(lines)


# ── Main dispatcher ────────────────────────────────────────────────────────────

def answer(trades, parsed) -> str:
    client    = resolve_client(parsed.get("client") or "")
    client2   = resolve_client(parsed.get("client2") or "")
    stock     = parsed.get("stock")
    intent    = parsed.get("intent", "all_summary")
    filt      = parsed.get("filter", "all")
    date_from = parsed.get("date_from")
    date_to   = parsed.get("date_to")
    extra     = parsed.get("extra")

    if intent == "all_summary":
        return answer_all_summary(trades)
    if intent == "client_summary":
        return answer_client_summary(trades, client)
    if intent == "pnl_on_date" or (date_from and intent in ("client_summary", "all_summary")):
        return answer_pnl_on_date(trades, client, date_from, date_to)
    if intent == "stock_holders" and stock:
        return answer_stock_holders(trades, stock)
    if intent == "stock_detail" and stock:
        return answer_stock_detail(trades, client, stock)
    if intent == "entry_date" and stock:
        return answer_entry_date(trades, client, stock)
    if intent == "monthly_pnl":
        return answer_monthly_pnl(trades, client)
    if intent == "long_positions":
        return answer_long_positions(trades, client, int(extra or 60))
    if intent == "recent_activity":
        return answer_recent_activity(trades, client, date_from, date_to)
    if intent == "top_trades":
        return answer_top_trades(trades, client, int(extra or 5), best=(filt != "worst"))
    if intent == "concentration" and stock:
        return answer_concentration(trades, client, stock)
    if intent == "compare_clients":
        return answer_compare_clients(trades, client, client2)
    if intent in ("movers_up", "movers_down", "movers_any"):
        direction = {"movers_up": "up", "movers_down": "down", "movers_any": "any"}[intent]
        threshold = float(extra or 0)
        return answer_movers(trades, client, threshold, direction)
    if intent == "open_positions" or filt == "open":
        return answer_open_positions(trades, client)
    if intent == "closed_trades" or filt == "closed":
        return answer_closed_trades(trades, client)
    return answer_open_positions(trades, client) + "\n\n" + answer_closed_trades(trades, client)


# ── Intent parsing ─────────────────────────────────────────────────────────────

def parse_intent(groq_client, question: str) -> dict:
    today_str = date.today().isoformat()
    prompt    = PARSE_PROMPT.format(today=today_str)
    fallback  = {"client": None, "stock": None, "intent": "all_summary",
                 "filter": "all", "date_from": None, "date_to": None,
                 "extra": None, "client2": None}
    try:
        resp = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user",   "content": question}
            ],
            max_tokens=150,
            temperature=0
        )
        raw   = resp.choices[0].message.content.strip()
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        return json.loads(raw[start:end])
    except Exception:
        return fallback


# ── Telegram handlers ──────────────────────────────────────────────────────────

HELP_TEXT = """👋 *Raghava Tracker Bot*

*Commands:*
/summary — all clients overview
/today — today's trades & P&L
/help — this message

*What you can ask:*
• _Open positions in Kalpana?_
• _Who holds Suzlon?_
• _P&L for RIMK1209 on 2026-06-08_
• _Monthly P&L for Sathyavrath_
• _Which positions held > 60 days?_
• _What did we trade this week?_
• _Top 5 trades in RIMK1209_
• _Worst 3 trades all clients_
• _What % of Iranna's portfolio is Emmvee?_
• _Compare Kalpana and Iranna_
• _When did I buy Intellect in RIMK1209?_
• _Summary for Udayakumar_"""


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    if not is_allowed(update, cfg):
        return
    await update.message.reply_text(answer_all_summary(load_trades()), parse_mode="Markdown")


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    if not is_allowed(update, cfg):
        return
    await update.message.reply_text(answer_today(load_trades()), parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    if not is_allowed(update, cfg):
        return
    api_key = cfg.get("groq_api_key", "")
    if not api_key or "PASTE" in api_key:
        await update.message.reply_text("Groq API key not configured.")
        return

    question = update.message.text
    trades   = load_trades()
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        groq_client = Groq(api_key=api_key)
        parsed      = parse_intent(groq_client, question)
        reply       = answer(trades, parsed)
    except Exception as e:
        reply = f"⚠️ Error: {e}"

    chunks = []
    while len(reply) > 4000:
        split_at = reply.rfind("\n", 0, 4000)
        if split_at == -1:
            split_at = 4000
        chunks.append(reply[:split_at])
        reply = reply[split_at:].lstrip("\n")
    chunks.append(reply)

    for chunk in chunks:
        await update.message.reply_text(chunk, parse_mode="Markdown")


def main():
    cfg   = load_config()
    token = cfg.get("telegram_token", "")
    if not token or "PASTE" in token:
        print(f"Fill in telegram_token and groq_api_key in: {CONFIG_FILE}")
        return
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(CommandHandler("today",   cmd_today))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
