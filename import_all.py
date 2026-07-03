"""
Import ALL clients from broker CSVs.
- Uses MARKET PRICE (not net rate)
- FIFO matching across full history per client
- Same-day buys+sells matched chronologically
"""
import pandas as pd, json, os, re
from collections import deque

_SUFFIX_RE = re.compile(
    r'\s+(LIMITED|LTD\.?|CO\.?\s*LTD\.?|CORP\.?|PVT\.?|INC\.?)\.?\s*$',
    re.IGNORECASE
)

# ── name normaliser ──────────────────────────────────────────────────────────
RAW = {
    # ── truncated / abbreviated names that can't be inferred by suffix stripping ──
    'BHARAT COKING COAL LTD':              'BHARAT COKING COAL',
    'BHARAT COKING COAL LTD.':             'BHARAT COKING COAL',
    'DIXON TECHNO (INDIA) LTD':            'DIXON TECHNOLOGIES',
    'DIXON TECHNOLOGIES (INDIA) LIM':      'DIXON TECHNOLOGIES',
    'Dixon Technologies (India) Lim':      'DIXON TECHNOLOGIES',
    'EMMVEE PHOTOVOLTAIC PWR L':           'EMMVEE PHOTOVOLTAIC',
    'EMMVEE PHOTOVOLTAIC POWER LIMI':      'EMMVEE PHOTOVOLTAIC',
    'EMMVEE PHOTOVOLTAIC PWR LTD':         'EMMVEE PHOTOVOLTAIC',
    'Emmvee Photovoltaic Power Limi':      'EMMVEE PHOTOVOLTAIC',
    'EQUITAS SMALL FIN BNK LTD':           'EQUITAS SMALL FIN BANK',
    'EQUITAS SMALL FINANCE BANK LIM':      'EQUITAS SMALL FIN BANK',
    'Equitas Small Finance Bank Lim':      'EQUITAS SMALL FIN BANK',
    'SAMVRDHNA MTHRSN INTL LTD':           'SAMVARDHANA MOTHERSON',
    'SAMVARDHANA MOTHERSON INTERNAT':      'SAMVARDHANA MOTHERSON',
    'Samvardhana Motherson Internat':      'SAMVARDHANA MOTHERSON',
    'P.I.INDUSTRIES LTD.':                 'PI INDUSTRIES',
    'NIPPONAMC - NETFSILVER':              'NIPPON NETFSILVER',
    'MIRAEAMC - ENERGY':                   'MIRAE ENERGY ETF',
    'MIRAEAMC - MAGOLDETF':                'MIRAEAMC MAGOLDETF',
    'INDIAN ENERGY EXC LTD':               'INDIAN ENERGY EXCHANGE',
    'COMPUTER AGE MNGT SER LTD':           'CAMS',
    'HIMADRI SPECIALITY CHEM L':           'HIMADRI SPECIALITY CHEM',
    'GUJ NAR VAL FER & CHEM L':            'GUJARAT NARMADA FERT',
    'KALPATARU PROJECT INT LTD':           'KALPATARU PROJECTS',
    'L&T TECHNOLOGY SER. LTD.':            'L&T TECHNOLOGY SERVICES',
    'ORACLE FIN SERV SOFT LTD.':           'ORACLE FINANCIAL SERVICES',
    'ORACLE FINANCIAL SERVICES SOFT':      'ORACLE FINANCIAL SERVICES',
    'SPANDANA SPHOORTY FIN LTD':           'SPANDANA SPHOORTY',
    'NETWEB TECH INDIA LTD':               'NETWEB TECHNOLOGIES',
    'NETWEB TECHNOLOGIES INDIA LIMI':      'NETWEB TECHNOLOGIES',
    'THE INDIAN HOTELS CO. LTD':           'INDIAN HOTELS',
    'SHAILY ENG PLASTICS LTD':             'SHAILY ENGINEERING',
    'INTELLECT DESIGN ARENA':              'INTELLECT DESIGN ARENA',
    'GARWARE TECH FIBRES LTD':             'GARWARE TECH FIBRES',
    'GARWARE TECHNICAL FIBRES LIMIT':      'GARWARE TECH FIBRES',
    'Mazagon Dock Shipbuilders Limi':      'MAZAGON DOCK',
    'MAZAGON DOCK SHIPBUIL LTD':           'MAZAGON DOCK',
    'BALKRISHNA IND. LTD':                 'BALKRISHNA IND',
    'CENTURY PLYBOARDS (I) LTD':           'CENTURY PLYBOARDS',
    'M&M FIN. SERVICES LTD':               'M&M FINANCE',
    'WHIRLPOOL OF INDIA LTD':              'WHIRLPOOL INDIA',
    'LIFE INSURANCE CORPORATION OF':       'LIC',
    'NIPPON INDIA ETF LIQUID BEES':        'NIPPON ETF LIQUID BEES',
    'NIP IND ETF LIQUID BEES':             'NIPPON ETF LIQUID BEES',
    'NIPPON L I A M LTD':                  'NIPPON LIFE AMC',
    'NIPPON LIFE INDIA ASSET MANAGE':      'NIPPON LIFE AMC',
    'NIP IND ETF IT':                      'NIPPON ETF IT',
    'TRANSPORT CORPN OF INDIA':            'TRANSPORT CORP OF INDIA',
    'CHOLAMANDALAM FIN HOL LTD':           'CHOLAMANDALAM',
    'BOMBAY BURMAH TRADING COR':           'BOMBAY BURMAH TRADING',
    'TATA CONSULTANCY SERV LT':            'TCS',
    'MANGALORE REFINERY & PETROCHEM':      'MRPL',
    'ALKYL AMINES CHEM. LTD':              'ALKYL AMINES',
    'ALKYL AMINES CHEMICALS LTD.':         'ALKYL AMINES',
    'ALKYL AMINES CHEMICALS':              'ALKYL AMINES',
    'NEWGEN SOFTWARE TECH LTD':            'NEWGEN SOFTWARE',
    'ZENSAR TECHNOLOGIES  LTD':            'ZENSAR TECHNOLOGIES',
    'Sagility Limited':                    'SAGILITY',
    'Himadri Speciality Chemical Lt':      'HIMADRI SPECIALITY CHEM',
    'HIMADRI SPECIALITY CHEMICAL LT':      'HIMADRI SPECIALITY CHEM',
    'K.P.R.MILL LIMITED':                  'KPR MILL',
    'K.P.R.MILL':                          'KPR MILL',
    'Spandana Sphoorty Financial Li':      'SPANDANA SPHOORTY',
    'SPANDANA SPHOORTY FINANCIAL LI':      'SPANDANA SPHOORTY',
    'ZEE ENTERTAINMENT ENTERPRISES':       'ZEE ENTERTAINMENT ENT',
    'ZEE ENTERTAINMENT ENT LTD':           'ZEE ENTERTAINMENT ENT',
    'Indian Railway Finance Corpora':      'INDIAN RAILWAY FIN CORP L',
    'INDIAN RAILWAY FINANCE CORPORA':      'INDIAN RAILWAY FIN CORP L',
    'INDIAN RAILWAY FIN CORP L':           'INDIAN RAILWAY FIN CORP L',
}

def norm(name):
    n = str(name).strip().upper()
    if n in RAW:
        return RAW[n]
    for k, v in RAW.items():
        if k.upper() == n:
            return v
    # strip common suffixes (LIMITED, LTD., LTD) and retry — handles cross-exchange name variants
    stripped = _SUFFIX_RE.sub('', n).strip()
    if stripped != n:
        if stripped in RAW:
            return RAW[stripped]
        for k, v in RAW.items():
            if k.upper() == stripped:
                return v
        return stripped
    return n

def parse_date(s):
    for fmt in ('%d %b %Y', '%d-%b-%y', '%d %b %y'):
        try:
            return pd.to_datetime(s, format=fmt)
        except:
            pass
    return pd.to_datetime(s, errors='coerce')

EXCLUDED_TERMINALS = {'30023'}  # dealer/sub-broker terminal — all trades excluded

def load_csv(path):
    df = pd.read_csv(path)
    if 'TERMINAL NO' in df.columns:
        terminal = df['TERMINAL NO'].astype(str).str.strip()
        df = df[~terminal.isin(EXCLUDED_TERMINALS)]
    df['TRADE DATE'] = df['TRADE DATE'].apply(parse_date)
    df['SELL/BUY'] = df['SELL/BUY'].str.strip()
    df['SCRIP NAME'] = df['SCRIP NAME'].str.strip()
    df['SCRIP'] = df['SCRIP NAME'].apply(norm)
    return df

# ── gather files per client — auto-discover from CSV_DIR ─────────────────────
import glob, platform, re

if platform.system() == 'Windows':
    CSV_DIR = r'C:\Users\jainr\Downloads\MO_Trades'
else:
    CSV_DIR = '/home/opc/client-tracker-mofsl/mo_csvs'

CLIENT_FILES = {}
for f in sorted(glob.glob(os.path.join(CSV_DIR, 'TradeDetailsAndSummary_RIMK*.csv'))):
    m = re.search(r'(RIMK\d+)', os.path.basename(f))
    if m:
        CLIENT_FILES.setdefault(m.group(1), []).append(f)

if __name__ == '__main__':
    all_trades, trade_id = [], 1

    for client, files in CLIENT_FILES.items():
        frames = [load_csv(f) for f in files if os.path.exists(f)]
        if not frames:
            continue
        df = pd.concat(frames, ignore_index=True).sort_values('TRADE DATE')

        charge_cols = ['BROKERAGE','TRANSACTION CHARGES','GST','STAMP DUTY','STT/CTT','SEBI CHARGES','IPFT CHARGES']
        for col in charge_cols:
            if col not in df.columns: df[col] = 0
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

        if 'PRODUCT' not in df.columns:
            df['PRODUCT'] = 'DELIVERY'
        orders = df.groupby(['TRADE DATE', 'SCRIP', 'SELL/BUY', 'ORDER NO']).apply(
            lambda g: pd.Series({
                'qty':        g['TRADE QTY'].sum(),
                'price':      round((g['MARKET PRICE'] * g['TRADE QTY']).sum() / g['TRADE QTY'].sum(), 2),
                'net_rate':   round(g['NET RATE'].mean(), 2),
                'brokerage':  round(g['BROKERAGE'].sum(), 2),
                'stt':        round(g['STT/CTT'].sum(), 2),
                'gst':        round(g['GST'].sum(), 2),
                'stamp':      round(g['STAMP DUTY'].sum(), 2),
                'txn_chrg':   round(g['TRANSACTION CHARGES'].sum(), 2),
                'other':      round((g['SEBI CHARGES'] + g['IPFT CHARGES']).sum(), 2),
                'product':    g['PRODUCT'].iloc[0] if 'PRODUCT' in g.columns else 'DELIVERY',
            })
        ).reset_index()
        orders['ORDER NO SORT'] = pd.to_numeric(
            orders['ORDER NO'].astype(str).str.lstrip("'").str.strip(), errors='coerce'
        ).fillna(0)
        # sort buys before sells on same date — order numbers across NSE/BSE aren't comparable
        orders['SIDE SORT'] = (orders['SELL/BUY'] == 'S').astype(int)
        orders = orders.sort_values(['TRADE DATE', 'SIDE SORT', 'ORDER NO SORT']).drop(columns=['ORDER NO SORT', 'SIDE SORT'])

        for scrip in sorted(orders['SCRIP'].unique()):
            s = orders[orders['SCRIP'] == scrip]
            buy_queue = deque()

            for _, row in s.iterrows():
                if row['SELL/BUY'] == 'B':
                    buy_queue.append({
                        'date':      row['TRADE DATE'],
                        'qty':       row['qty'],
                        'price':     row['price'],
                        'net_rate':  row['net_rate'],
                        'brokerage': row['brokerage'],
                        'stt':       row['stt'],
                        'gst':       row['gst'],
                        'stamp':     row['stamp'],
                        'txn_chrg':  row['txn_chrg'],
                        'other':     row['other'],
                        'product':   row.get('product', 'DELIVERY'),
                    })
                else:
                    sell_rem = row['qty']
                    while sell_rem > 0 and buy_queue:
                        buy = buy_queue[0]
                        qty      = min(buy['qty'], sell_rem)
                        frac_buy = qty / buy['qty'] if buy['qty'] else 0
                        frac_sel = qty / row['qty'] if row['qty'] else 0
                        all_trades.append({
                            'id':            trade_id,
                            'client':        client,
                            'script':        scrip,
                            'type':          'Long',
                            'entry_date':    buy['date'].strftime('%Y-%m-%d'),
                            'buy_qty':       float(qty),
                            'buy_price':     float(buy['price']),
                            'buy_net_rate':  float(buy['net_rate']),
                            'exit_date':     row['TRADE DATE'].strftime('%Y-%m-%d'),
                            'sell_qty':      float(qty),
                            'sell_price':    float(row['price']),
                            'sell_net_rate': float(row['net_rate']),
                            'buy_brokerage': round(buy['brokerage'] * frac_buy, 2),
                            'buy_stt':       round(buy['stt']       * frac_buy, 2),
                            'buy_gst':       round(buy['gst']       * frac_buy, 2),
                            'buy_stamp':     round(buy['stamp']     * frac_buy, 2),
                            'buy_txn':       round(buy['txn_chrg']  * frac_buy, 2),
                            'sell_brokerage':round(row['brokerage'] * frac_sel, 2),
                            'sell_stt':      round(row['stt']       * frac_sel, 2),
                            'sell_gst':      round(row['gst']       * frac_sel, 2),
                            'sell_stamp':    round(row['stamp']     * frac_sel, 2),
                            'sell_txn':      round(row['txn_chrg']  * frac_sel, 2),
                            'product':       buy.get('product', 'DELIVERY'),
                            'notes':         'imported',
                        })
                        trade_id += 1
                        buy['qty'] -= qty
                        sell_rem  -= qty
                        if buy['qty'] <= 0:
                            buy_queue.popleft()

            while buy_queue:
                buy = buy_queue.popleft()
                all_trades.append({
                    'id':            trade_id,
                    'client':        client,
                    'script':        scrip,
                    'type':          'Long',
                    'entry_date':    buy['date'].strftime('%Y-%m-%d'),
                    'buy_qty':       float(buy['qty']),
                    'buy_price':     float(buy['price']),
                    'buy_net_rate':  float(buy['net_rate']),
                    'exit_date':     None,
                    'sell_qty':      0,
                    'sell_price':    0,
                    'sell_net_rate': 0,
                    'buy_brokerage': float(buy['brokerage']),
                    'buy_stt':       float(buy['stt']),
                    'buy_gst':       float(buy['gst']),
                    'buy_stamp':     float(buy['stamp']),
                    'buy_txn':       float(buy['txn_chrg']),
                    'sell_brokerage':0, 'sell_stt':0, 'sell_gst':0,
                    'sell_stamp':0, 'sell_txn':0,
                    'product':       buy.get('product', 'DELIVERY'),
                    'notes':         'imported',
                })
                trade_id += 1

    closed = [t for t in all_trades if t['exit_date']]
    opened = [t for t in all_trades if not t['exit_date']]
    pnl    = sum((t['sell_price'] - t['buy_price']) * t['sell_qty'] for t in closed)
    cap    = sum(t['buy_qty'] * t['buy_price'] for t in opened)
    wins   = sum(1 for t in closed if t['sell_price'] > t['buy_price'])

    print(f'All clients imported: {len(all_trades)} trades')
    print(f'  Open  : {len(opened)}')
    print(f'  Closed: {len(closed)}   Win rate: {wins}/{len(closed)} = {wins/len(closed)*100:.0f}%')
    print(f'  Realised P&L : Rs {pnl:,.0f}')
    print(f'  Open capital : Rs {cap:,.0f}')
    print()

    by_client = {}
    for t in all_trades:
        by_client.setdefault(t['client'], []).append(t)
    for c, ts in sorted(by_client.items()):
        cl = [t for t in ts if t['exit_date']]
        op = [t for t in ts if not t['exit_date']]
        p  = sum((t['sell_price']-t['buy_price'])*t['sell_qty'] for t in cl)
        k  = sum(t['buy_qty']*t['buy_price'] for t in op)
        print(f'  {c}: {len(op)} open, {len(cl)} closed | P&L Rs {p:>10,.0f} | Capital Rs {k:>12,.0f}')

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trades.json')
    with open(out, 'w') as f:
        json.dump(all_trades, f, indent=2)
    print(f'\nSaved to {out}')
