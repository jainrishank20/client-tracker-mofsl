"""
Raghava Tracker — Telegram Bot
Python computes answers directly; LLM only parses intent.
"""

import json
import os
from pathlib import Path
from collections import defaultdict
from datetime import date

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

ALL_CODES = list(CLIENT_NAMES.keys())


def resolve_client(raw: str) -> str | None:
    """Fuzzy-match a client code/name — handles typos like RIMK129 → RIMK1209."""
    if not raw:
        return None
    up = raw.strip().upper()
    # Exact match
    if up in CLIENT_NAMES:
        return up
    # Name match
    if up in NAME_TO_CODE:
        return NAME_TO_CODE[up]
    # Partial prefix match on code (RIMK129 → RIMK1209)
    digits = up.replace("RIMK", "").strip()
    for code in ALL_CODES:
        if code.startswith(up) or up.startswith(code[:7]):
            return code
        # match by last digits
        if digits and code.endswith(digits):
            return code
    return None

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
}

PARSE_PROMPT = """You are a query parser for a stock portfolio bot. Today is {today}. Extract from the user message:
- client: RIMK code (map names too: Sathyavrath→RIMK1209, Kalpana→RIMK1220, Iranna→RIMK1238, Udayakumar→RIMK1248, Sundareshwari→RIMK1249, Savitha→RIMK1252). null if not mentioned.
- stock: stock name or ticker (use full name where possible). null if not mentioned.
- intent: one of:
    open_positions  → asking about open/current holdings only
    closed_trades   → asking about closed/exited trades only
    all_positions   → asking about all trades (open + closed)
    stock_detail    → asking about a specific stock (price, qty, p&l, status)
    entry_date      → asking when they bought / entry date for a stock
    pnl_on_date     → asking about profit/trades booked on a specific date or date range
    client_summary  → asking for overall summary/p&l of a client
    all_summary     → asking about all clients overall
- filter: "open", "closed", or "all" (default "all")
- date_from: ISO date YYYY-MM-DD if a specific date or start of range is mentioned. null otherwise.
- date_to: ISO date YYYY-MM-DD if an end of range is mentioned. Same as date_from for single-day queries. null otherwise.

Resolve relative dates using today ({today}): "today"→today, "yesterday"→yesterday, "this week"→Mon to today, "last week"→previous Mon-Sun.

Respond ONLY with valid JSON. Examples:
{{"client":"RIMK1238","stock":null,"intent":"open_positions","filter":"open","date_from":null,"date_to":null}}
{{"client":"RIMK1209","stock":null,"intent":"pnl_on_date","filter":"closed","date_from":"2026-06-08","date_to":"2026-06-08"}}
{{"client":"RIMK1209","stock":null,"intent":"pnl_on_date","filter":"closed","date_from":"2026-06-01","date_to":"2026-06-08"}}
{{"client":"RIMK1209","stock":"BHARAT ELECTRONICS","intent":"stock_detail","filter":"all","date_from":null,"date_to":null}}
{{"client":"RIMK1209","stock":null,"intent":"client_summary","filter":"all","date_from":null,"date_to":null}}
{{"client":null,"stock":null,"intent":"all_summary","filter":"all","date_from":null,"date_to":null}}"""


def load_config() -> dict:
    # Environment variables take priority (Railway deployment)
    env_token   = os.environ.get("TELEGRAM_TOKEN")
    env_groq    = os.environ.get("GROQ_API_KEY")
    env_chat_id = os.environ.get("ALLOWED_CHAT_ID", "")
    if env_token and env_groq:
        return {"telegram_token": env_token, "groq_api_key": env_groq,
                "allowed_chat_id": env_chat_id}
    # Fallback to local bot_config.json
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


# ── Maths helpers ─────────────────────────────────────────────────────────────

def wavg_buy(lots) -> float:
    total_qty = sum(t["buy_qty"] for t in lots)
    if not total_qty:
        return 0
    return round(sum(t["buy_price"] * t["buy_qty"] for t in lots) / total_qty, 2)


def wavg_sell(lots) -> float:
    total_qty = sum(t.get("sell_qty") or 0 for t in lots)
    if not total_qty:
        return 0
    return round(sum((t.get("sell_price") or 0) * (t.get("sell_qty") or 0) for t in lots) / total_qty, 2)


def compute_net_pnl(t) -> float:
    if t.get("net_pnl"):
        return float(t["net_pnl"])
    gross = (float(t.get("sell_price") or 0) - float(t.get("buy_price") or 0)) * float(t.get("buy_qty") or 0)
    charges = sum(float(t.get(f"buy_{k}", 0) or 0) + float(t.get(f"sell_{k}", 0) or 0)
                  for k in ["brokerage", "stt", "gst", "stamp", "txn_chrg"])
    return round(gross - charges, 2)


def holding_days(entry_date_str) -> int:
    try:
        entry = date.fromisoformat(entry_date_str[:10])
        return (date.today() - entry).days
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


# ── Answer builders ───────────────────────────────────────────────────────────

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
        lines.append(f"  • *{scr}*: {qty:.0f} shares @ {fmt_inr(avg)} | "
                     f"Invested: {fmt_inr(invested)} | Held {days}d (since {earliest})")
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
                     f"Buy {fmt_inr(bp)} → Sell {fmt_inr(sp)} | "
                     f"P&L: {pnl_str(pnl)} ({pct}) | Exited {exit_d}")
    total_sign = "+" if total_pnl >= 0 else ""
    lines.append(f"  _Total booked P&L: {total_sign}{fmt_inr(total_pnl)}_")
    return lines


def answer_open_positions(trades, client) -> str:
    pool = [t for t in trades if not client or t["client"] == client]
    open_lots = [t for t in pool if not t.get("exit_date")]
    if not open_lots:
        name = CLIENT_NAMES.get(client, client) if client else "anyone"
        return f"No open positions found for {name}."
    name = CLIENT_NAMES.get(client, client) if client else "All Clients"
    lines = [f"*{name} — Open Positions*\n"]
    if not client:
        by_client = defaultdict(list)
        for t in open_lots:
            by_client[t["client"]].append(t)
        for c, lots in sorted(by_client.items()):
            lines.append(f"*{CLIENT_NAMES.get(c,c)}:*")
            lines += build_open_section(lots)
            lines.append("")
    else:
        lines += build_open_section(open_lots)
    return "\n".join(lines)


def answer_closed_trades(trades, client) -> str:
    pool = [t for t in trades if not client or t["client"] == client]
    closed_lots = [t for t in pool if t.get("exit_date")]
    if not closed_lots:
        name = CLIENT_NAMES.get(client, client) if client else "anyone"
        return f"No closed trades found for {name}."
    name = CLIENT_NAMES.get(client, client) if client else "All Clients"
    lines = [f"*{name} — Closed Trades*\n"]
    if not client:
        by_client = defaultdict(list)
        for t in closed_lots:
            by_client[t["client"]].append(t)
        for c, lots in sorted(by_client.items()):
            lines.append(f"*{CLIENT_NAMES.get(c,c)}:*")
            lines += build_closed_section(lots)
            lines.append("")
    else:
        lines += build_closed_section(closed_lots)
    return "\n".join(lines)


def answer_stock_detail(trades, client, stock) -> str:
    su = stock.upper() if stock else ""
    for alias, full in TICKER_ALIAS.items():
        if alias == su:
            su = full.upper()
            break
    pool = [t for t in trades
            if (not client or t["client"] == client)
            and (su in (t.get("script") or "").upper()
                 or (t.get("script") or "").upper() in su)]
    if not pool:
        label = f"{stock}" + (f" in {CLIENT_NAMES.get(client,client)}" if client else "")
        return f"No trades found for {label}."

    lines = []
    by_client_script = defaultdict(list)
    for t in pool:
        by_client_script[(t["client"], t["script"])].append(t)

    for (c, scr), lots in sorted(by_client_script.items()):
        open_lots   = [t for t in lots if not t.get("exit_date")]
        closed_lots = [t for t in lots if t.get("exit_date")]
        name = CLIENT_NAMES.get(c, c)
        lines.append(f"*{scr}* — {name}\n")

        if open_lots:
            qty  = sum(t["buy_qty"] for t in open_lots)
            avg  = wavg_buy(open_lots)
            days = holding_days(min(t["entry_date"] for t in open_lots))
            inv  = round(qty * avg, 2)
            lines.append(f"🟢 *Open:* {qty:.0f} shares @ avg {fmt_inr(avg)}")
            lines.append(f"   Invested: {fmt_inr(inv)} | Holding for {days} days")
        else:
            lines.append("🟢 *Open:* None")

        if closed_lots:
            qty  = sum(t["buy_qty"] for t in closed_lots)
            bp   = wavg_buy(closed_lots)
            sp   = wavg_sell(closed_lots)
            pnl  = sum(compute_net_pnl(t) for t in closed_lots)
            pct  = ret_pct(bp, qty, pnl)
            lines.append(f"🔴 *Closed:* {qty:.0f} shares | Buy avg {fmt_inr(bp)} → Sell avg {fmt_inr(sp)}")
            lines.append(f"   Net P&L: {pnl_str(pnl)} ({pct})")
        else:
            lines.append("🔴 *Closed:* None")

        lines.append("")
    return "\n".join(lines).strip()


def answer_client_summary(trades, client) -> str:
    pool = [t for t in trades if not client or t["client"] == client]
    if not pool:
        return f"No data for {CLIENT_NAMES.get(client, client)}."

    open_lots   = [t for t in pool if not t.get("exit_date")]
    closed_lots = [t for t in pool if t.get("exit_date")]
    name = CLIENT_NAMES.get(client, client) if client else "Portfolio"

    invested   = sum(t["buy_price"] * t["buy_qty"] for t in open_lots)
    booked_pnl = sum(compute_net_pnl(t) for t in closed_lots)
    best  = max(closed_lots, key=lambda t: compute_net_pnl(t), default=None)
    worst = min(closed_lots, key=lambda t: compute_net_pnl(t), default=None)
    wins  = sum(1 for t in closed_lots if compute_net_pnl(t) > 0)
    win_rate = round(wins / len(closed_lots) * 100) if closed_lots else 0

    lines = [
        f"*{name} — Summary*\n",
        f"📂 Open positions: {len({t['script'] for t in open_lots})} stocks | Capital: {fmt_inr(invested)}",
        f"✅ Closed trades: {len(closed_lots)} lots | Booked P&L: {pnl_str(booked_pnl)}",
        f"🎯 Win rate: {win_rate}% ({wins}/{len(closed_lots)})",
    ]
    if best:
        lines.append(f"🏆 Best trade: {best['script']} → {pnl_str(compute_net_pnl(best))}")
    if worst:
        lines.append(f"📉 Worst trade: {worst['script']} → {pnl_str(compute_net_pnl(worst))}")
    return "\n".join(lines)


def answer_entry_date(trades, client, stock) -> str:
    su = stock.upper() if stock else ""
    for alias, full in TICKER_ALIAS.items():
        if alias == su:
            su = full.upper()
            break
    pool = [t for t in trades
            if (not client or t["client"] == client)
            and (su in (t.get("script") or "").upper()
                 or (t.get("script") or "").upper() in su)]
    if not pool:
        label = f"{stock}" + (f" in {CLIENT_NAMES.get(client, client)}" if client else "")
        return f"No trades found for {label}."

    lines = []
    by_client_script = defaultdict(list)
    for t in pool:
        by_client_script[(t["client"], t["script"])].append(t)

    for (c, scr), lots in sorted(by_client_script.items()):
        name = CLIENT_NAMES.get(c, c)
        open_lots   = [t for t in lots if not t.get("exit_date")]
        closed_lots = [t for t in lots if t.get("exit_date")]
        lines.append(f"*{scr}* — {name}\n")
        if open_lots:
            for t in sorted(open_lots, key=lambda x: x["entry_date"]):
                days = holding_days(t["entry_date"])
                lines.append(f"🟢 Bought {t['buy_qty']:.0f} sh on *{t['entry_date'][:10]}* "
                             f"@ {fmt_inr(t['buy_price'])} | Holding {days} days")
        if closed_lots:
            for t in sorted(closed_lots, key=lambda x: x["entry_date"]):
                lines.append(f"🔴 Bought {t['buy_qty']:.0f} sh on *{t['entry_date'][:10]}* "
                             f"@ {fmt_inr(t['buy_price'])} | Sold on {t['exit_date'][:10]}")
        lines.append("")
    return "\n".join(lines).strip()


def answer_pnl_on_date(trades, client, date_from, date_to) -> str:
    pool = [t for t in trades
            if (not client or t["client"] == client)
            and t.get("exit_date")
            and (not date_from or t["exit_date"][:10] >= date_from)
            and (not date_to   or t["exit_date"][:10] <= date_to)]

    if not pool:
        label = CLIENT_NAMES.get(client, client) if client else "any client"
        period = date_from if date_from == date_to else f"{date_from} → {date_to}"
        return f"No trades closed between {period} for {label}."

    # label the period nicely
    if date_from and date_from == date_to:
        period_label = f"on {date_from}"
    elif date_from and date_to:
        period_label = f"{date_from} to {date_to}"
    else:
        period_label = "in selected period"

    name = CLIENT_NAMES.get(client, client) if client else "All Clients"
    lines = [f"*{name} — Trades closed {period_label}*\n"]

    by_client = defaultdict(list)
    for t in pool:
        by_client[t["client"]].append(t)

    total_pnl = 0
    for c, lots in sorted(by_client.items()):
        cname = CLIENT_NAMES.get(c, c)
        if not client:
            lines.append(f"*{cname}:*")
        by_script = defaultdict(list)
        for t in lots:
            by_script[t["script"]].append(t)
        client_pnl = 0
        for scr, slts in sorted(by_script.items()):
            qty  = sum(t["buy_qty"] for t in slts)
            bp   = wavg_buy(slts)
            sp   = wavg_sell(slts)
            pnl  = sum(compute_net_pnl(t) for t in slts)
            pct  = ret_pct(bp, qty, pnl)
            client_pnl += pnl
            lines.append(f"  • *{scr}*: {qty:.0f} sh | "
                         f"{fmt_inr(bp)} → {fmt_inr(sp)} | {pnl_str(pnl)} ({pct})")
        if not client:
            lines.append(f"  _Subtotal: {pnl_str(client_pnl)}_\n")
        total_pnl += client_pnl

    lines.append(f"\n💰 *Total booked P&L: {pnl_str(total_pnl)}*")
    return "\n".join(lines)


def answer_all_summary(trades) -> str:
    lines = ["📊 *Portfolio Overview*\n"]
    total_invested = total_pnl = 0
    for code, name in CLIENT_NAMES.items():
        ct = [t for t in trades if t["client"] == code]
        open_t   = [t for t in ct if not t.get("exit_date")]
        closed_t = [t for t in ct if t.get("exit_date")]
        inv = sum(t["buy_price"] * t["buy_qty"] for t in open_t)
        pnl = sum(compute_net_pnl(t) for t in closed_t)
        total_invested += inv
        total_pnl += pnl
        lines.append(f"*{name}*: {len(open_t)} open | {len(closed_t)} closed | "
                     f"Deployed {fmt_inr(inv)} | P&L {pnl_str(pnl)}")
    lines.append(f"\n_Total deployed: {fmt_inr(total_invested)} | Total booked P&L: {pnl_str(total_pnl)}_")
    return "\n".join(lines)


def answer(trades, parsed) -> str:
    client    = resolve_client(parsed.get("client") or "")
    stock     = parsed.get("stock")
    intent    = parsed.get("intent", "all_positions")
    filt      = parsed.get("filter", "all")
    date_from = parsed.get("date_from")
    date_to   = parsed.get("date_to")

    if intent == "pnl_on_date" or (date_from and intent in ("client_summary", "all_summary")):
        return answer_pnl_on_date(trades, client, date_from, date_to)
    if intent == "all_summary":
        return answer_all_summary(trades)
    if intent == "client_summary":
        return answer_client_summary(trades, client)
    if intent == "entry_date" and stock:
        return answer_entry_date(trades, client, stock)
    if intent == "stock_detail" and stock:
        return answer_stock_detail(trades, client, stock)
    if intent == "open_positions" or filt == "open":
        return answer_open_positions(trades, client)
    if intent == "closed_trades" or filt == "closed":
        return answer_closed_trades(trades, client)
    # all_positions — show both
    open_ans   = answer_open_positions(trades, client)
    closed_ans = answer_closed_trades(trades, client)
    return open_ans + "\n\n" + closed_ans


def parse_intent(groq_client, question: str) -> dict:
    today_str = date.today().isoformat()
    prompt = PARSE_PROMPT.format(today=today_str)
    try:
        resp = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user",   "content": question}
            ],
            max_tokens=120,
            temperature=0
        )
        raw = resp.choices[0].message.content.strip()
        start, end = raw.find("{"), raw.rfind("}") + 1
        return json.loads(raw[start:end])
    except Exception:
        return {"client": None, "stock": None, "intent": "all_summary",
                "filter": "all", "date_from": None, "date_to": None}


# ── Telegram handlers ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👋 *Raghava Tracker Bot*\n\n"
        f"Your chat ID: `{update.effective_chat.id}`\n\n"
        f"*What you can ask:*\n"
        f"• _Open positions in RIMK1238?_\n"
        f"• _What price did I buy BEL in Sathyavrath?_\n"
        f"• _P&L for Suzlon?_\n"
        f"• _Summary for Kalpana_\n"
        f"• _How are all clients doing?_\n\n"
        f"*/summary* — quick overview of all clients\n"
        f"*/help* — show this message",
        parse_mode="Markdown"
    )


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    if not is_allowed(update, cfg):
        return
    trades = load_trades()
    await update.message.reply_text(answer_all_summary(trades), parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    if not is_allowed(update, cfg):
        return

    api_key = cfg.get("groq_api_key", "")
    if not api_key or "PASTE" in api_key:
        await update.message.reply_text("Groq API key not set in bot_config.json")
        return

    question = update.message.text
    trades   = load_trades()

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        groq_client = Groq(api_key=api_key)
        parsed = parse_intent(groq_client, question)
        reply  = answer(trades, parsed)
    except Exception as e:
        reply = f"⚠️ Error: {e}"

    # Telegram message limit is 4096 chars
    if len(reply) > 4000:
        reply = reply[:3990] + "\n\n_...truncated_"

    await update.message.reply_text(reply, parse_mode="Markdown")


def main():
    cfg = load_config()
    token = cfg.get("telegram_token", "")
    if not token or "PASTE" in token:
        print("=" * 60)
        print(f"Fill in telegram_token and groq_api_key in:\n  {CONFIG_FILE}")
        print("=" * 60)
        return
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
