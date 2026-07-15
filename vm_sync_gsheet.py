"""
Standalone GSheet sync. Run: python3 vm_sync_gsheet.py
sync_to_gsheet is copied verbatim from app.py — no Streamlit needed.
"""
import json, os, sys, time
import pandas as pd
from datetime import datetime, date

BASE = os.path.dirname(os.path.abspath(__file__))

# Guard: refuse to sync if trades.json is older than 6 hours — stale data corrupts the sheet.
# GitHub Actions always has fresh data; local runs almost never do.
_trades_path = os.path.join(BASE, "trades.json")
_age_hours = (time.time() - os.path.getmtime(_trades_path)) / 3600
if _age_hours > 6:
    print(f"ERROR: trades.json is {_age_hours:.1f}h old. Run daily pipeline first or use GitHub Actions.")
    print("Refusing to sync — stale trades.json would corrupt the GSheet.")
    sys.exit(1)

GSHEET_KEY = os.path.join(BASE, "gsheet_key.json")
GSHEET_ID  = "1RBaZYY8Eheet13UJy6eRMJIFUzU9Yii335l5x_H5KVo"

_cfg = json.loads(open(os.path.join(BASE, "bot_config.json"), encoding="utf-8-sig").read())
CLIENT_NAMES = _cfg.get("clients", {})
CLIENTS = list(CLIENT_NAMES.keys())

TICKER_OVERRIDES_FILE = os.path.join(BASE, "ticker_overrides.json")

from symbol_map import SYMBOL_MAP



_overrides_cache = None

def load_ticker_overrides():
    global _overrides_cache
    if _overrides_cache is not None:
        return _overrides_cache
    if os.path.exists(TICKER_OVERRIDES_FILE):
        try:
            with open(TICKER_OVERRIDES_FILE, encoding="utf-8-sig") as f:
                _overrides_cache = json.load(f)
                return _overrides_cache
        except Exception as e:
            print(f"  load_ticker_overrides FAILED: {e}")
    return {}


def fetch_cmp(scripts):
    """Fetch live CMP via yfinance. Returns {script: price} for scripts that resolve."""
    try:
        import yfinance as yf
        import sys
        overrides = load_ticker_overrides()
        sys.path.insert(0, BASE)
        from symbol_map import resolve as _resolve
        ticker_map = {s: _resolve(s, overrides) for s in scripts}
        unique_tickers = list(set(ticker_map.values()))
        ns_tickers = [t + '.NS' for t in unique_tickers]
        if not ns_tickers:
            return {}
        if len(ns_tickers) == 1:
            df = yf.download(ns_tickers, period='1d', interval='1m',
                             progress=False, auto_adjust=True, timeout=20)
            try:
                price = float(df['Close'].squeeze().dropna().iloc[-1])
                return {s: price for s, t in ticker_map.items() if t == unique_tickers[0]}
            except Exception:
                return {}
        df = yf.download(ns_tickers, period='1d', interval='1m',
                         progress=False, auto_adjust=True, timeout=20)
        result = {}
        for s, ticker in ticker_map.items():
            try:
                price = float(df['Close'][ticker + '.NS'].dropna().iloc[-1])
                result[s] = price
            except Exception:
                pass
        print(f"  yfinance fetched CMP for {len(result)}/{len(scripts)} scripts")
        return result
    except Exception as e:
        print(f"  fetch_cmp failed: {e}")
        return {}


def sync_to_gsheet(trades: list):
    """Push all trade data to Google Sheets — tabs with formatting, CMP formulas, conditional colors."""
    import gspread
    from gspread_formatting import (
        CellFormat, Color, TextFormat, BooleanCondition, BooleanRule,
        ConditionalFormatRule, GridRange, format_cell_range, format_cell_ranges,
        set_frozen, get_conditional_format_rules, set_column_width
    )

    import time as _time
    from gspread.exceptions import APIError as _GAPIError

    _call_times = []
    _last_call  = [0.0]
    def _rate_limit():
        now = _time.time()
        gap = now - _last_call[0]
        if gap < 1.1:
            _time.sleep(1.1 - gap)
        _call_times[:] = [t for t in _call_times if now - t < 60]
        if len(_call_times) >= 30:
            wait = 62 - (now - _call_times[0])
            if wait > 0:
                _time.sleep(wait)
            _call_times.clear()
        _last_call[0] = _time.time()
        _call_times.append(_last_call[0])

    def _retry(fn, *args, **kwargs):
        _rate_limit()
        for attempt in range(6):
            try:
                return fn(*args, **kwargs)
            except _GAPIError as e:
                if e.response.status_code == 429:
                    _time.sleep(70)
                    _call_times.clear()
                else:
                    raise
        return fn(*args, **kwargs)

    _r_fcr  = format_cell_range
    _r_fcrs = format_cell_ranges
    _r_sf   = set_frozen
    _r_scw  = set_column_width
    def format_cell_range(*a, **kw):  return _retry(_r_fcr,  *a, **kw)
    def format_cell_ranges(*a, **kw): return _retry(_r_fcrs, *a, **kw)
    def set_frozen(*a, **kw):         return _retry(_r_sf,   *a, **kw)
    def set_column_width(*a, **kw):   return _retry(_r_scw,  *a, **kw)

    gc = gspread.service_account(filename=GSHEET_KEY)
    sh = gc.open_by_key(GSHEET_ID)

    for stale in ["⚡ Intraday", "📌 Script Summary"]:
        try:
            sh.del_worksheet(sh.worksheet(stale))
        except Exception:
            pass

    df_all = pd.DataFrame(trades)
    if df_all.empty:
        return "No trades to sync."

    df_all['entry_date'] = pd.to_datetime(df_all['entry_date'], errors='coerce')
    df_all['exit_date']  = pd.to_datetime(df_all['exit_date'],  errors='coerce')
    open_df   = df_all[df_all['exit_date'].isna()].copy()
    closed_df = df_all[df_all['exit_date'].notna()].copy()

    for _col in ["buy_price", "sell_price", "buy_qty", "sell_qty"]:
        closed_df[_col] = pd.to_numeric(closed_df[_col], errors="coerce").fillna(0)
        open_df[_col]   = pd.to_numeric(open_df[_col],   errors="coerce").fillna(0)
    # Charge columns may be absent on open-trade rows (open trades have no sell_other/buy_other).
    # Fill NaN → 0 on df_all so total_charges() never gets NaN from pandas .get() on a Series.
    _charge_fill = ['buy_brokerage','buy_stt','buy_gst','buy_stamp','buy_txn','buy_other',
                    'sell_brokerage','sell_stt','sell_gst','sell_stamp','sell_txn','sell_other']
    for _col in _charge_fill:
        if _col not in df_all.columns:
            df_all[_col] = 0.0
        df_all[_col] = pd.to_numeric(df_all[_col], errors='coerce').fillna(0)
    closed_df["pnl"] = (closed_df["sell_price"] - closed_df["buy_price"]) * closed_df["buy_qty"]
    if "net_pnl" in closed_df.columns:
        closed_df["net_pnl"] = pd.to_numeric(closed_df["net_pnl"], errors="coerce").fillna(0)
    else:
        closed_df["net_pnl"] = closed_df["pnl"]
    open_df["invested"] = open_df["buy_price"] * open_df["buy_qty"]

    def total_charges(row, prefix):
        keys = [f'{prefix}_brokerage', f'{prefix}_stt', f'{prefix}_gst',
                f'{prefix}_stamp', f'{prefix}_txn', f'{prefix}_other']
        return sum(float(row.get(k, 0) or 0) for k in keys)

    HEADER_BG   = Color(0.13, 0.13, 0.18)
    HEADER_FG   = Color(1, 1, 1)
    ALT_BG      = Color(0.95, 0.95, 0.97)
    GREEN_BG    = Color(0.84, 0.97, 0.89)
    RED_BG      = Color(0.99, 0.87, 0.87)
    GREEN_FG    = Color(0.08, 0.50, 0.25)
    RED_FG      = Color(0.65, 0.05, 0.05)

    header_fmt = CellFormat(
        backgroundColor=HEADER_BG,
        textFormat=TextFormat(bold=True, foregroundColor=HEADER_FG, fontSize=10),
        horizontalAlignment='CENTER'
    )
    num_fmt    = CellFormat(numberFormat={"type":"NUMBER","pattern":"#,##0.00"}, horizontalAlignment='RIGHT')
    center_fmt = CellFormat(horizontalAlignment='CENTER')

    def col_letter(n):
        s = ""
        while n:
            n, r = divmod(n-1, 26)
            s = chr(65+r) + s
        return s

    def upsert_ws(name, rows=200, cols=20):
        _time.sleep(1)
        try:
            ws = sh.worksheet(name)
            if ws.row_count < rows or ws.col_count < cols:
                ws.resize(rows=max(rows, ws.row_count), cols=max(cols, ws.col_count))
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=name, rows=rows, cols=cols)
        return ws

    def write_df(ws, df):
        if df.empty:
            _retry(ws.clear)
            try:
                sh.batch_update({'requests': [{'updateCells': {
                    'range': {'sheetId': ws.id},
                    'fields': 'userEnteredFormat'
                }}]})
            except Exception:
                pass
            _retry(ws.update, [["No data"]], value_input_option='RAW')
            return
        df2 = df.copy()
        for c in df2.select_dtypes(include=['datetime64[ns]']).columns:
            df2[c] = df2[c].dt.strftime('%Y-%m-%d').fillna('')
        df2 = df2.fillna('').astype(str)
        def try_num(v):
            try: return float(v) if '.' in v else int(v)
            except: return v
        data = [df2.columns.tolist()] + [[try_num(v) for v in r] for r in df2.values.tolist()]
        # Clear data + ALL formatting immediately before write (single gap ~1s).
        # Must clear formatting here — not in upsert_ws — so stale row styles
        # (e.g. a previous total-row that is now a data row) don't persist.
        _retry(ws.clear)
        try:
            sh.batch_update({'requests': [{'updateCells': {
                'range': {'sheetId': ws.id},
                'fields': 'userEnteredFormat'
            }}]})
        except Exception:
            pass
        _retry(ws.update, data, value_input_option='USER_ENTERED')
        _time.sleep(0.5)

    def apply_header_fmt(ws, ncols):
        fmt_range = f"A1:{col_letter(ncols)}1"
        format_cell_range(ws, fmt_range, header_fmt)
        set_frozen(ws, rows=1)

    _cleared_ws_rules = set()
    _pending_rules = {}

    def apply_pnl_conditional(ws, pnl_col_idx, nrows):
        col = col_letter(pnl_col_idx)
        rng = GridRange.from_a1_range(f"{col}2:{col}{nrows+1}", ws)
        if ws.id not in _pending_rules:
            rules = get_conditional_format_rules(ws)
            rules.clear()
            _pending_rules[ws.id] = rules
        rules = _pending_rules[ws.id]
        rules.append(ConditionalFormatRule(
            ranges=[rng],
            booleanRule=BooleanRule(
                condition=BooleanCondition('NUMBER_GREATER', ['0']),
                format=CellFormat(backgroundColor=GREEN_BG,
                                  textFormat=TextFormat(bold=True, foregroundColor=GREEN_FG))
            )
        ))
        rules.append(ConditionalFormatRule(
            ranges=[rng],
            booleanRule=BooleanRule(
                condition=BooleanCondition('NUMBER_LESS', ['0']),
                format=CellFormat(backgroundColor=RED_BG,
                                  textFormat=TextFormat(bold=True, foregroundColor=RED_FG))
            )
        ))

    def flush_pnl_rules(ws):
        if ws.id in _pending_rules:
            _pending_rules[ws.id].save()
            del _pending_rules[ws.id]

    def apply_num_cols(ws, col_indices, nrows):
        ranges = [(f"{col_letter(i)}2:{col_letter(i)}{nrows+1}", num_fmt) for i in col_indices]
        if ranges:
            format_cell_ranges(ws, ranges)

    from symbol_map import resolve as _sym_resolve

    def resolve_symbol(script):
        overrides = load_ticker_overrides()
        return _sym_resolve(script, overrides)

    def gfinance_formula(script):
        sym = resolve_symbol(script)
        return f'=IFERROR(GOOGLEFINANCE("NSE:{sym}","price"),"—")'

    # ── Manual CMP tab — auto-fill unresolved scripts, read user-supplied tickers ─
    def get_or_create_manual_ws(sh):
        try:
            return sh.worksheet("✏️ Manual CMP")
        except Exception:
            ws_manual = sh.add_worksheet("✏️ Manual CMP", rows=50, cols=3)
            ws_manual.update([['Script (from trades — auto filled)', 'NSE Ticker (you fill this)', 'Last Fetched Price']],
                             range_name='A1:C1', value_input_option='USER_ENTERED')
            format_cell_range(ws_manual, 'A1:C1', header_fmt)
            set_column_width(ws_manual, 'A', 260)
            set_column_width(ws_manual, 'B', 160)
            set_column_width(ws_manual, 'C', 140)
            return ws_manual

    def read_manual_ticker_map(ws_manual):
        """Returns {script_upper: nse_ticker} for rows where user filled col B."""
        result = {}
        try:
            rows = ws_manual.get_all_values()
            for row in rows[1:]:
                if len(row) >= 2 and row[0].strip() and row[1].strip():
                    result[row[0].strip().upper()] = row[1].strip().upper().replace('.NS', '')
        except Exception as e:
            print(f"  Manual ticker read failed: {e}")
        return result

    # Step 1 — fetch CMP via yfinance for all open scripts
    cmp_map = {}
    if not open_df.empty:
        scripts = tuple(open_df['script'].unique())
        try:
            cmp_map = fetch_cmp(scripts)
        except Exception:
            cmp_map = {}

    # Step 2 — read manual tab, fetch additional prices for user-supplied tickers
    ws_manual = get_or_create_manual_ws(sh)
    manual_ticker_map = read_manual_ticker_map(ws_manual)

    if manual_ticker_map and not open_df.empty:
        import yfinance as _yf
        for s in open_df['script'].unique():
            if s in cmp_map:
                continue
            nse_ticker = manual_ticker_map.get(s.upper())
            if not nse_ticker:
                continue
            try:
                info = _yf.Ticker(nse_ticker + '.NS').fast_info
                price = info.get('lastPrice') or info.get('regularMarketPreviousClose')
                if price:
                    cmp_map[s] = float(price)
            except Exception:
                pass

    # Step 3 — update Manual CMP tab: add any unresolved scripts not already listed,
    #           write fetched price in col C so user sees it worked
    if not open_df.empty:
        try:
            all_rows = ws_manual.get_all_values()
            existing_scripts = {r[0].strip().upper() for r in all_rows[1:] if r and r[0].strip()}
            new_rows = []
            for s in sorted(open_df['script'].unique()):
                if s not in cmp_map and s.upper() not in existing_scripts:
                    new_rows.append([s, '', ''])  # script in col A, user fills col B
            if new_rows:
                next_row = len(all_rows) + 1
                ws_manual.update(new_rows,
                                 range_name=f'A{next_row}:C{next_row + len(new_rows) - 1}',
                                 value_input_option='USER_ENTERED')
            # Write fetched price in col C for rows where we now have a price
            all_rows2 = ws_manual.get_all_values()
            price_updates = []
            for i, row in enumerate(all_rows2[1:], start=2):
                if not row or not row[0].strip():
                    continue
                s_up = row[0].strip().upper()
                # Find matching script in cmp_map (case-insensitive)
                matched_price = None
                for s, p in cmp_map.items():
                    if s.upper() == s_up:
                        matched_price = p
                        break
                price_updates.append([round(matched_price, 2) if matched_price else '—'])
            if price_updates:
                ws_manual.update(price_updates,
                                 range_name=f'C2:C{len(price_updates)+1}',
                                 value_input_option='USER_ENTERED')
        except Exception as e:
            print(f"  Manual CMP tab update failed: {e}")

    def cmp_value_or_formula(script):
        if script in cmp_map:
            return cmp_map[script]
        return gfinance_formula(script)

    pct_fmt  = CellFormat(numberFormat={"type":"NUMBER","pattern":'0.00"%"'}, horizontalAlignment='RIGHT')
    tot_fmt  = CellFormat(backgroundColor=Color(0.13,0.13,0.18),
                          textFormat=TextFormat(bold=True, foregroundColor=Color(1,1,1)),
                          horizontalAlignment='RIGHT')
    synced_at = datetime.now(tz=__import__('zoneinfo').ZoneInfo('Asia/Kolkata')).strftime('%d %b %Y %I:%M %p IST')

    def add_totals_row(ws, df, num_cols, pct_cols, last_row):
        ncols = len(df.columns)
        tot_row = [''] * ncols
        tot_row[0] = 'TOTAL'
        for ci in num_cols:
            col = col_letter(ci)
            tot_row[ci-1] = f'=SUM({col}2:{col}{last_row})'
        ws.update([tot_row], range_name=f'A{last_row+1}:{col_letter(ncols)}{last_row+1}',
                  value_input_option='USER_ENTERED')
        format_cell_range(ws, f'A{last_row+1}:{col_letter(ncols)}{last_row+1}', tot_fmt)
        for ci in pct_cols:
            col = col_letter(ci)
            format_cell_range(ws, f'{col}{last_row+1}', pct_fmt)

    # ═══ TAB 1 — Overview ═══════════════════════════════════════════════════════
    rows_ov = []
    ov_errors = []
    for c in CLIENTS:
        try:
            cd = closed_df[closed_df['client']==c]
            od = open_df[open_df['client']==c]
            gross_c = ((cd['sell_price'] - cd['buy_price']) * cd['sell_qty']).sum() if not cd.empty else 0
            buy_chg = cd.apply(lambda r: total_charges(r,'buy'), axis=1).sum()  if not cd.empty else 0
            sel_chg = cd.apply(lambda r: total_charges(r,'sell'),axis=1).sum() if not cd.empty else 0
            net_c   = gross_c - buy_chg - sel_chg
            unreal  = 0
            if not od.empty:
                for _, r in od.iterrows():
                    cmp = cmp_map.get(r['script'])
                    if cmp and cmp not in ('', None):
                        unreal += (float(cmp) - float(r['buy_price'])) * float(r['buy_qty'])
            rows_ov.append({
                'Client Code':        c,
                'Client Name':        CLIENT_NAMES.get(c, c),
                'Open Positions':     int(open_df[open_df['client']==c]['script'].nunique()) if not od.empty else 0,
                'Closed Trades':      len(cd),
                'Realized Gross P&L': round(gross_c, 2),
                'Total Charges':      round(buy_chg + sel_chg, 2),
                'Realized Net P&L':   round(net_c, 2),
                'Unrealized P&L':     round(unreal, 2),
                'Total P&L':          round(net_c + unreal, 2),
            })
        except Exception as _ov_err:
            ov_errors.append(f"{c}: {_ov_err}")
    df_ov = pd.DataFrame(rows_ov)
    ws_ov = upsert_ws("📊 Overview", rows=25, cols=12)
    write_df(ws_ov, df_ov)
    apply_header_fmt(ws_ov, len(df_ov.columns))
    # Replace static Unrealized P&L (col H) and Total P&L (col I) with live formulas
    # H = SUMIF from Open Positions tab col K (Unrealized P&L), I = Realized Net + Unrealized
    n = len(df_ov)
    unreal_fmls = [[f"=SUMIF('📋 Open Positions'!A:A,A{r},'📋 Open Positions'!K:K)",
                    f"=G{r}+H{r}"] for r in range(2, n+2)]
    ws_ov.update(unreal_fmls, range_name=f'H2:I{n+1}', value_input_option='USER_ENTERED')
    apply_num_cols(ws_ov, [5,6,7,8,9], len(df_ov))
    for ci in [7,8,9]: apply_pnl_conditional(ws_ov, ci, len(df_ov))
    flush_pnl_rules(ws_ov)
    add_totals_row(ws_ov, df_ov, [5,6,7,8,9], [], len(df_ov)+1)
    ws_ov.update([[f'Last synced: {synced_at}']],
                 range_name=f'A{len(df_ov)+3}', value_input_option='USER_ENTERED')
    format_cell_range(ws_ov, f'A{len(df_ov)+3}',
                      CellFormat(textFormat=TextFormat(italic=True, foregroundColor=Color(0.5,0.5,0.5))))
    set_column_width(ws_ov, 'A', 110); set_column_width(ws_ov, 'B', 140)

    # ═══ TAB 2 — Open Positions ═════════════════════════════════════════════════
    _time.sleep(2)
    if not open_df.empty:
        grp = open_df.groupby(['client','script']).apply(lambda g: pd.Series({
            'Qty':           g['buy_qty'].sum(),
            'Avg Buy Price': round((g['buy_price']*g['buy_qty']).sum() / g['buy_qty'].sum(), 2),
            'Invested (₹)': round((g['buy_price']*g['buy_qty']).sum(), 2),
            'First Entry':   g['entry_date'].min().strftime('%Y-%m-%d') if pd.notna(g['entry_date'].min()) else '',
            'Last Entry':    g['entry_date'].max().strftime('%Y-%m-%d') if pd.notna(g['entry_date'].max()) else '',
        })).reset_index()
        grp['Client Name'] = grp['client'].map(CLIENT_NAMES)
        grp['Days Held']   = (pd.Timestamp.today() - pd.to_datetime(grp['First Entry'])).dt.days.astype(int)
        grp['CMP']         = grp['script'].map(cmp_value_or_formula)
        grp['Symbol']      = grp['script'].map(resolve_symbol)
        show_op = grp[['client','Client Name','Symbol','Qty','Avg Buy Price',
                        'Invested (₹)','CMP','First Entry','Last Entry','Days Held']].copy()
        show_op.columns = ['Client','Client Name','Symbol','Qty','Avg Buy Price',
                           'Invested (₹)','CMP','First Entry','Last Entry','Days Held']
        show_op = show_op.sort_values(['Client', 'Symbol']).reset_index(drop=True)
    else:
        show_op = pd.DataFrame(columns=['Client','Client Name','Symbol','Qty','Avg Buy Price',
                                        'Invested (₹)','CMP','First Entry','Last Entry','Days Held'])
    ws_op = upsert_ws("📋 Open Positions", rows=max(len(show_op)+15,100), cols=15)
    write_df(ws_op, show_op)
    nrows_op = len(show_op)
    ncols_op = len(show_op.columns)
    apply_header_fmt(ws_op, ncols_op)
    if nrows_op > 0:
        ws_op.update([['Unrealized P&L','% Return']],
                     range_name='K1:L1', value_input_option='USER_ENTERED')
        format_cell_range(ws_op, 'K1:L1', header_fmt)
        fml = []
        for ri in range(2, nrows_op+2):
            g,d,e = f'G{ri}',f'D{ri}',f'E{ri}'
            fml.append([f'=IF(ISNUMBER({g}),({g}-{e})*{d},"")',
                        f'=IF(ISNUMBER({g}),({g}-{e})/{e}*100,"")'])
        ws_op.update(fml, range_name=f'K2:L{nrows_op+1}', value_input_option='USER_ENTERED')
        apply_num_cols(ws_op, [5,6,7,11], nrows_op)
        format_cell_range(ws_op, f'L2:L{nrows_op+1}', pct_fmt)
        apply_pnl_conditional(ws_op, 11, nrows_op)
        apply_pnl_conditional(ws_op, 12, nrows_op)
        flush_pnl_rules(ws_op)
        tr = nrows_op + 2
        tot = ['TOTAL','','','','','','','','','',
               f'=SUM(K2:K{nrows_op+1})',
               f'=IF(F{tr}<>0,K{tr}/F{tr}*100,"")']
        tot[3] = f'=SUM(D2:D{nrows_op+1})'
        tot[5] = f'=SUM(F2:F{nrows_op+1})'
        ws_op.update([tot], range_name=f'A{tr}:L{tr}', value_input_option='USER_ENTERED')
        format_cell_range(ws_op, f'A{tr}:L{tr}', tot_fmt)
        format_cell_range(ws_op, f'L{tr}', pct_fmt)
    set_frozen(ws_op, rows=1, cols=3)

    # ═══ TAB 3 — Trade Ledger ═══════════════════════════════════════════════════
    _time.sleep(2)
    ledger_rows = []
    for _, r in df_all.iterrows():
        is_open = pd.isna(r['exit_date'])
        gross   = round((float(r['sell_price']) - float(r['buy_price'])) * float(r['sell_qty']), 2) if not is_open else ''
        charges = round(total_charges(r,'buy') + total_charges(r,'sell'), 2)
        net     = round(gross - charges, 2) if gross != '' else ''
        _bp  = float(r['buy_price'])
        _sq  = float(r['sell_qty']) if not is_open else 0
        pct  = round(gross / (_bp * _sq) * 100, 2) if gross != '' and _bp > 0 and _sq > 0 else ''
        hold = (r['exit_date'] - r['entry_date']).days if not is_open and pd.notna(r['exit_date']) and pd.notna(r['entry_date']) else ''
        ledger_rows.append({
            'Client':       r['client'],
            'Client Name':  CLIENT_NAMES.get(r['client'], r['client']),
            'Script':       r['script'],
            'Status':       'Open' if is_open else 'Closed',
            'Entry Date':   r['entry_date'].strftime('%Y-%m-%d') if pd.notna(r['entry_date']) else '',
            'Buy Qty':      float(r['buy_qty']),
            'Buy Price':    float(r['buy_price']),
            'Invested (₹)': round(float(r['buy_price']) * float(r['buy_qty']), 2),
            'Exit Date':    '' if is_open else (r['exit_date'].strftime('%Y-%m-%d') if pd.notna(r['exit_date']) else ''),
            'Sell Qty':     float(r['sell_qty']) if not is_open else '',
            'Sell Price':   float(r['sell_price']) if not is_open else '',
            'Gross P&L':    gross,
            'Charges':      charges,
            'Net P&L':      net,
            '% Return':     pct,
            'Hold Days':    hold,
        })
    df_ledger = pd.DataFrame(ledger_rows).sort_values(['Client','Script','Entry Date']).reset_index(drop=True)
    ws_sc = upsert_ws("📌 Trade Ledger", rows=max(len(df_ledger)+10, 200), cols=17)
    write_df(ws_sc, df_ledger)
    nrows_sc = len(df_ledger)
    apply_header_fmt(ws_sc, len(df_ledger.columns))
    apply_num_cols(ws_sc, [7,8,12,13,14], nrows_sc)
    format_cell_range(ws_sc, f'O2:O{nrows_sc+1}', pct_fmt)
    apply_pnl_conditional(ws_sc, 12, nrows_sc)
    apply_pnl_conditional(ws_sc, 14, nrows_sc)
    flush_pnl_rules(ws_sc)
    set_frozen(ws_sc, rows=1, cols=3)

    # ═══ TAB 4 — P&L Summary ════════════════════════════════════════════════════
    _time.sleep(2)
    pnl_rows = []
    pnl_errors = []
    for c in CLIENTS:
        try:
            cd = closed_df[closed_df['client']==c]
            od = open_df[open_df['client']==c]
            gross_c   = ((cd['sell_price']-cd['buy_price'])*cd['sell_qty']).sum() if not cd.empty else 0
            buy_chg   = cd.apply(lambda r: total_charges(r,'buy'), axis=1).sum()  if not cd.empty else 0
            sel_chg   = cd.apply(lambda r: total_charges(r,'sell'),axis=1).sum()  if not cd.empty else 0
            net_booked= gross_c - buy_chg - sel_chg
            total_chg = buy_chg + sel_chg
            unreal_py = 0
            if not od.empty:
                for _, r in od.iterrows():
                    cmp = cmp_map.get(r['script'])
                    if cmp:
                        unreal_py += (float(cmp)-float(r['buy_price']))*float(r['buy_qty'])
            capital   = round((od['buy_price']*od['buy_qty']).sum(), 2) if not od.empty else 0
            if not cd.empty:
                wins     = ((cd['sell_price']-cd['buy_price'])>0).sum()
                win_rate = round(wins/len(cd)*100, 1)
                best     = round(((cd['sell_price']-cd['buy_price'])*cd['sell_qty']).max(), 2)
                worst    = round(((cd['sell_price']-cd['buy_price'])*cd['sell_qty']).min(), 2)
                trades_count = len(cd)
            else:
                win_rate = trades_count = best = worst = 0
            pnl_rows.append({
                'Client':            c,
                'Name':              CLIENT_NAMES.get(c, c),
                'Open Positions':    len(od['script'].unique()) if not od.empty else 0,
                'Capital Deployed':  capital,
                'Closed Trades':     trades_count,
                'Booked Gross P&L':  round(gross_c, 2),
                'Total Charges':     round(total_chg, 2),
                'Booked Net P&L':    round(net_booked, 2),
                'Unrealized P&L*':   round(unreal_py, 2),
                'Total P&L*':        round(net_booked + unreal_py, 2),
                'Win Rate %':        win_rate,
                'Best Trade':        best,
                'Worst Trade':       worst,
            })
        except Exception as _pnl_err:
            pnl_errors.append(f"{c}: {_pnl_err}")
    df_pnl = pd.DataFrame(pnl_rows)
    ws_pnl = upsert_ws("💰 P&L Summary", rows=25, cols=14)
    write_df(ws_pnl, df_pnl)
    apply_header_fmt(ws_pnl, len(df_pnl.columns))
    nrows_pnl = len(df_pnl)
    # Replace static Unrealized P&L (col I=9) and Total P&L (col J=10) with live SUMIF formulas
    # Col I = SUMIF from Open Positions tab K (live GOOGLEFINANCE Unrealized P&L)
    # Col J = Booked Net P&L (col H=8) + Unrealized (col I=9)
    pnl_live_fmls = [[
        f"=SUMIF('📋 Open Positions'!A:A,A{r},'📋 Open Positions'!K:K)",
        f"=H{r}+I{r}"
    ] for r in range(2, nrows_pnl+2)]
    ws_pnl.update(pnl_live_fmls, range_name=f'I2:J{nrows_pnl+1}', value_input_option='USER_ENTERED')
    apply_num_cols(ws_pnl, [4,6,7,8,9,10,12,13], nrows_pnl)
    apply_pnl_conditional(ws_pnl, 8,  nrows_pnl)
    apply_pnl_conditional(ws_pnl, 9,  nrows_pnl)
    apply_pnl_conditional(ws_pnl, 10, nrows_pnl)
    apply_pnl_conditional(ws_pnl, 12, nrows_pnl)
    apply_pnl_conditional(ws_pnl, 13, nrows_pnl)
    flush_pnl_rules(ws_pnl)
    add_totals_row(ws_pnl, df_pnl, [4,6,7,8,9,10,12,13], [11], nrows_pnl+1)
    ws_pnl.update([['* Unrealized & Total P&L are live (from Open Positions tab).']],
                  range_name=f'A{nrows_pnl+3}', value_input_option='USER_ENTERED')
    format_cell_range(ws_pnl, f'A{nrows_pnl+3}',
                      CellFormat(textFormat=TextFormat(italic=True, foregroundColor=Color(0.5,0.5,0.5))))
    set_frozen(ws_pnl, rows=1, cols=2)

    # ═══ TAB 5 — Closed Trades ══════════════════════════════════════════════════
    _time.sleep(2)
    if not closed_df.empty:
        cl = closed_df.copy()
        cl['Client Name']  = cl['client'].map(CLIENT_NAMES)
        cl['Gross P&L']    = ((cl['sell_price'] - cl['buy_price']) * cl['sell_qty']).round(2)
        cl['Buy Charges']  = cl.apply(lambda r: round(total_charges(r,'buy'), 2), axis=1)
        cl['Sell Charges'] = cl.apply(lambda r: round(total_charges(r,'sell'),2), axis=1)
        cl['Net P&L']      = (cl['Gross P&L'] - cl['Buy Charges'] - cl['Sell Charges']).round(2)
        denom = cl['buy_price'] * cl['sell_qty']
        cl['% Return']     = cl['Gross P&L'].where(denom == 0, cl['Gross P&L'] / denom.replace(0, float('nan')) * 100).round(2).fillna(0)
        cl['Type']         = cl.apply(lambda r: 'Intraday'
                              if pd.notna(r['entry_date']) and pd.notna(r['exit_date']) and r['entry_date'].date()==r['exit_date'].date() else 'Delivery', axis=1)
        cl['Hold Days']    = (cl['exit_date'] - cl['entry_date']).dt.days
        cl['Entry Date']   = cl['entry_date'].dt.strftime('%Y-%m-%d')
        cl['Exit Date']    = cl['exit_date'].dt.strftime('%Y-%m-%d')
        show_cl = cl[['client','Client Name','script','sell_qty','buy_price','sell_price',
                       'Entry Date','Exit Date','Hold Days','Gross P&L','Buy Charges',
                       'Sell Charges','Net P&L','% Return','Type']].copy()
        show_cl.columns = ['Client','Client Name','Script','Qty','Buy Price','Sell Price',
                           'Entry Date','Exit Date','Hold Days','Gross P&L','Buy Charges',
                           'Sell Charges','Net P&L','% Return','Type']
        show_cl = show_cl.sort_values(['Client','Exit Date'], ascending=[True,False]).reset_index(drop=True)
    else:
        show_cl = pd.DataFrame()
    ws_cl = upsert_ws("📈 Closed Trades", rows=max(len(show_cl)+10,100), cols=16)
    write_df(ws_cl, show_cl)
    apply_header_fmt(ws_cl, len(show_cl.columns) if not show_cl.empty else 15)
    if not show_cl.empty:
        nrows_cl = len(show_cl)
        apply_num_cols(ws_cl, [5,6,10,11,12,13], nrows_cl)
        format_cell_range(ws_cl, f'N2:N{nrows_cl+1}', pct_fmt)
        apply_pnl_conditional(ws_cl, 10, nrows_cl)
        apply_pnl_conditional(ws_cl, 13, nrows_cl)
        apply_pnl_conditional(ws_cl, 14, nrows_cl)
        flush_pnl_rules(ws_cl)
        add_totals_row(ws_cl, show_cl, [10,11,12,13], [14], nrows_cl+1)
    set_frozen(ws_cl, rows=1, cols=3)

    # ═══ TAB 6 — Monthly P&L ════════════════════════════════════════════════════
    _time.sleep(1)
    if not closed_df.empty:
        _mn_df = closed_df.copy()
        _mn_df["Month"] = _mn_df["exit_date"].dt.to_period("M").astype(str)
        # Compute net per trade = gross - all charges (buy + sell side)
        _charge_cols = ['buy_brokerage','buy_stt','buy_gst','buy_stamp','buy_txn','buy_other',
                        'sell_brokerage','sell_stt','sell_gst','sell_stamp','sell_txn','sell_other']
        for _cc in _charge_cols:
            if _cc not in _mn_df.columns:
                _mn_df[_cc] = 0
            _mn_df[_cc] = pd.to_numeric(_mn_df[_cc], errors='coerce').fillna(0)
        _mn_df["_total_charges"] = _mn_df[[c for c in _charge_cols if c in _mn_df.columns]].sum(axis=1)
        _mn_df["_net_pnl"]       = _mn_df["pnl"] - _mn_df["_total_charges"]
        _mn_rows = []
        for _month, _mg in _mn_df.groupby("Month"):
            _row = {"Month": _month}
            _row["All Clients Gross"] = round(_mg["pnl"].sum(), 2)
            _row["All Clients Net"]   = round(_mg["_net_pnl"].sum(), 2)
            _row["Trades"]            = len(_mg)
            for _c in CLIENTS:
                _cg = _mg[_mg["client"] == _c]
                _row[f"{CLIENT_NAMES[_c]} Net"] = round(_cg["_net_pnl"].sum(), 2) if not _cg.empty else 0
            _mn_rows.append(_row)
        df_monthly = pd.DataFrame(_mn_rows).sort_values("Month", ascending=False)
        ws_mn = upsert_ws("📆 Monthly P&L", rows=max(len(df_monthly)+20, 50),
                          cols=len(df_monthly.columns)+2)
        write_df(ws_mn, df_monthly)
        apply_header_fmt(ws_mn, len(df_monthly.columns))
        _mn_num_cols = list(range(2, 4 + len(CLIENTS) + 1))
        apply_num_cols(ws_mn, _mn_num_cols, len(df_monthly))
        for _cc in range(2, 4 + len(CLIENTS) + 1):
            apply_pnl_conditional(ws_mn, _cc, len(df_monthly))
        flush_pnl_rules(ws_mn)
        add_totals_row(ws_mn, df_monthly, _mn_num_cols, [], len(df_monthly)+1)
        set_frozen(ws_mn, rows=1, cols=1)

    # ═══ TAB 7 — Capital ════════════════════════════════════════════════════════
    _time.sleep(1)
    try:
        import os as _os
        _ledger_file = _os.path.join(_os.path.dirname(__file__), "ledger.json")
        _ledger_data = json.loads(open(_ledger_file, encoding='utf-8-sig').read()) if _os.path.exists(_ledger_file) else {}
    except Exception:
        _ledger_data = {}

    # ledger.json format: {"RIMK1205": {"combined": 12345.0, "mtf": 0.0}, ...}
    # combined = net delivery ledger balance, mtf = MTF balance
    cap_rows_gs = []
    for c in CLIENTS:
        raw = _ledger_data.get(c, {})
        combined = raw.get('combined', 0.0) if isinstance(raw, dict) else 0.0
        mtf      = raw.get('mtf',      0.0) if isinstance(raw, dict) else 0.0
        od_c   = open_df[open_df["client"] == c] if not open_df.empty else pd.DataFrame()
        deployed = (od_c["buy_price"] * od_c["buy_qty"]).sum() if not od_c.empty else 0
        net_closed_pnl = closed_df[closed_df["client"]==c]["net_pnl"].sum() if not closed_df.empty else 0
        cap_rows_gs.append({
            "Client Code":       c,
            "Client Name":       CLIENT_NAMES.get(c, c),
            "Ledger Balance":    round(combined, 2),
            "MTF Balance":       round(mtf, 2),
            "Deployed (Open)":   round(deployed, 2),
            "Booked Net P&L":    round(net_closed_pnl, 2),
        })
    df_cap_gs = pd.DataFrame(cap_rows_gs)
    ws_cap = upsert_ws("💰 Capital", rows=max(len(df_cap_gs)+20, 50), cols=8)
    write_df(ws_cap, df_cap_gs)
    apply_header_fmt(ws_cap, len(df_cap_gs.columns))
    if not df_cap_gs.empty:
        apply_num_cols(ws_cap, [3,4,5,6], len(df_cap_gs))
        apply_pnl_conditional(ws_cap, 6, len(df_cap_gs))
        flush_pnl_rules(ws_cap)
        add_totals_row(ws_cap, df_cap_gs, [3,4,5,6], [], len(df_cap_gs)+1)

    total = len(open_df) + len(closed_df)
    all_errors = (ov_errors or []) + (pnl_errors or [])
    err_str = f" ⚠️ Errors: {'; '.join(all_errors)}" if all_errors else ""
    return f"✅ Synced {total} trades ({len(open_df)} open, {len(closed_df)} closed) · {len(rows_ov)} clients · Last synced: {synced_at}{err_str}"


# ── Run ───────────────────────────────────────────────────────────────────────
trades_file = os.path.join(BASE, "trades.json")
if not os.path.exists(trades_file):
    print("ERROR: trades.json not found")
    sys.exit(1)

with open(trades_file) as f:
    trades = json.load(f)

print(f"Loaded {len(trades)} trades. Syncing to Google Sheet...")
result = sync_to_gsheet(trades)
print(f"Done: {result}")

# ── GSheet QA: auto-resolve #N/A CMP symbols via yfinance ────────────────────
try:
    import gspread, urllib.parse, urllib.request, yfinance as yf
    from oauth2client.service_account import ServiceAccountCredentials

    _scope  = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    _creds  = ServiceAccountCredentials.from_json_keyfile_name(GSHEET_KEY, _scope)
    _gc     = gspread.authorize(_creds)
    _sh     = _gc.open_by_key(GSHEET_ID)
    _ws_op  = _sh.worksheet("📋 Open Positions")
    _vals   = _ws_op.get_all_values()

    # Collect unique symbols with #N/A CMP
    _na_symbols = {}   # symbol → [client, ...]
    if len(_vals) > 1:
        _header   = _vals[0]
        _ci       = {h: i for i, h in enumerate(_header)}
        _sym_i    = _ci.get('Symbol', 2)
        _client_i = _ci.get('Client', 0)
        _cmp_i    = _ci.get('CMP', 6)
        for _row in _vals[1:]:
            if len(_row) > _cmp_i and str(_row[_cmp_i]).strip() in ('#N/A', '#ERROR!', '#REF!', '#VALUE!', '—'):
                _sym = _row[_sym_i].strip() if len(_row) > _sym_i else ''
                _cli = _row[_client_i].strip() if len(_row) > _client_i else '?'
                if _sym:
                    _na_symbols.setdefault(_sym, []).append(_cli)

    if _na_symbols:
        _overrides = load_ticker_overrides()
        _auto_fixed   = {}   # symbol → resolved ticker
        _still_broken = []   # symbols we couldn't resolve

        def _try_resolve(raw_sym):
            """Try common NSE ticker derivations for a raw CBOS symbol name."""
            candidates = []
            # 1. strip spaces / common suffixes
            clean = raw_sym.replace(' ', '').upper()
            candidates.append(clean)
            # 2. drop "INDIA" / "LTD" / "LIMITED" suffix variants
            for suffix in ('INDIA', 'LTD', 'LIMITED', 'INDUSTRIES', 'INFRA'):
                if clean.endswith(suffix) and len(clean) > len(suffix) + 3:
                    candidates.append(clean[:-len(suffix)])
            # dedupe preserving order
            seen = set()
            unique = [c for c in candidates if not (c in seen or seen.add(c))]
            for ticker in unique:
                try:
                    info = yf.Ticker(ticker + '.NS').fast_info
                    if info.get('lastPrice') or info.get('regularMarketPreviousClose'):
                        return ticker
                except Exception:
                    pass
            return None

        for _sym in _na_symbols:
            _resolved = _try_resolve(_sym)
            if _resolved:
                _overrides[_sym] = _resolved
                _auto_fixed[_sym] = _resolved
                print(f"QA auto-fixed: {_sym} → {_resolved}")
            else:
                _still_broken.append(_sym)
                print(f"QA could not resolve: {_sym}")

        # Persist resolved tickers to ticker_overrides.json
        if _auto_fixed:
            with open(TICKER_OVERRIDES_FILE, 'w') as _f:
                json.dump(_overrides, _f, indent=2)
            # Re-run sync so the sheet gets updated CMP values immediately
            print("Re-syncing GSheet with auto-resolved tickers...")
            result2 = sync_to_gsheet(trades)
            print(f"Re-sync done: {result2}")

        # Send Telegram summary
        _token    = _cfg.get("telegram_token", "")
        _chat_ids = str(_cfg.get("allowed_chat_id", "")).split(",")
        _lines = ["⚠️ *GSheet QA — CMP #N/A detected*\n"]
        if _auto_fixed:
            _lines.append("✅ *Auto-fixed:*")
            for s, t in _auto_fixed.items():
                clients = ', '.join(dict.fromkeys(_na_symbols[s]))
                _lines.append(f"  • {s} → `{t}` ({clients})")
        if _still_broken:
            _lines.append("\n❌ *Could not resolve (fix manually):*")
            for s in _still_broken:
                clients = ', '.join(dict.fromkeys(_na_symbols[s]))
                _lines.append(f"  • {s} ({clients})")
            _lines.append("\nAdd to `symbol_map.py` and redeploy.")
        _msg = '\n'.join(_lines)
        for _cid in _chat_ids:
            _cid = _cid.strip()
            if not _cid:
                continue
            _data = urllib.parse.urlencode({
                "chat_id": _cid, "text": _msg, "parse_mode": "Markdown"
            }).encode()
            urllib.request.urlopen(
                urllib.request.Request(f"https://api.telegram.org/bot{_token}/sendMessage", data=_data),
                timeout=15
            )
    else:
        print("QA: All CMP values OK — no #N/A found.")
except Exception as _qa_err:
    print(f"QA check failed (non-fatal): {_qa_err}")
