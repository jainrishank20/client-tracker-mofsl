"""
Import ALL clients from broker CSVs.
- Uses MARKET PRICE (not net rate)
- FIFO matching across full history per client
- Same-day buys+sells matched chronologically
"""
import pandas as pd, json, os
from collections import deque

# ── name normaliser ──────────────────────────────────────────────────────────
RAW = {
    'BHARAT COKING COAL LTD':              'BHARAT COKING COAL',
    'BHARAT COKING COAL LIMITED':          'BHARAT COKING COAL',
    'BHARAT COKING COAL LTD.':             'BHARAT COKING COAL',
    'DIXON TECHNO (INDIA) LTD':            'DIXON TECHNOLOGIES',
    'DIXON TECHNOLOGIES (INDIA) LIM':      'DIXON TECHNOLOGIES',
    'DIXON TECHNOLOGIES LTD':             'DIXON TECHNOLOGIES',
    'EMMVEE PHOTOVOLTAIC PWR L':           'EMMVEE PHOTOVOLTAIC',
    'EMMVEE PHOTOVOLTAIC POWER LIMI':      'EMMVEE PHOTOVOLTAIC',
    'EMMVEE PHOTOVOLTAIC PWR LTD':         'EMMVEE PHOTOVOLTAIC',
    'EQUITAS SMALL FIN BNK LTD':          'EQUITAS SMALL FIN BANK',
    'EQUITAS SMALL FINANCE BANK LIM':      'EQUITAS SMALL FIN BANK',
    'EQUITAS SMALL FIN BNK LTD':          'EQUITAS SMALL FIN BANK',
    'EXIDE INDUSTRIES LTD.':               'EXIDE INDUSTRIES',
    'EXIDE INDUSTRIES LTD':               'EXIDE INDUSTRIES',
    'GRAPHITE INDIA LTD.':                'GRAPHITE INDIA',
    'GRAPHITE INDIA LTD':                 'GRAPHITE INDIA',
    'JBM AUTO LTD.':                      'JBM AUTO',
    'JBM AUTO LIMITED':                   'JBM AUTO',
    'LAURUS LABS LIMITED':                'LAURUS LABS',
    'REDINGTON LIMITED':                  'REDINGTON',
    'SAMVRDHNA MTHRSN INTL LTD':          'SAMVARDHANA MOTHERSON',
    'SAMVARDHANA MOTHERSON INTERNAT':      'SAMVARDHANA MOTHERSON',
    'SUZLON ENERGY LTD.':                 'SUZLON ENERGY',
    'SUZLON ENERGY LIMITED':              'SUZLON ENERGY',
    'TEJAS NETWORKS LIMITED':             'TEJAS NETWORKS',
    'WOCKHARDT LTD.':                     'WOCKHARDT',
    'WOCKHARDT LIMITED':                  'WOCKHARDT',
    'PI INDUSTRIES LTD':                  'PI INDUSTRIES',
    'P.I.INDUSTRIES LTD.':                'PI INDUSTRIES',
    'NMDC LTD.':                          'NMDC',
    'NMDC LTD':                           'NMDC',
    'NIPPONAMC - NETFSILVER':             'NIPPON NETFSILVER',
    'MIRAEAMC - ENERGY':                  'MIRAE ENERGY ETF',
    'IIFL FINANCE LIMITED':               'IIFL FINANCE',
    'IIFL FINANCE LTD':                   'IIFL FINANCE',
    'IFCI LTD.':                          'IFCI',
    'IFCI LTD':                           'IFCI',
    'INDIAN ENERGY EXC LTD':              'INDIAN ENERGY EXCHANGE',
    'INDIAN ENERGY EXCHANGE LIMITED':     'INDIAN ENERGY EXCHANGE',
    'Indian Energy Exchange Limited':     'INDIAN ENERGY EXCHANGE',
    'COMPUTER AGE MNGT SER LTD':          'CAMS',
    'GRINDWELL NORTON LIMITED':           'GRINDWELL NORTON',
    'HIMADRI SPECIALITY CHEM L':          'HIMADRI SPECIALITY CHEM',
    'HIMADRI SPECIALITY CHEM LTD':        'HIMADRI SPECIALITY CHEM',
    'GUJ NAR VAL FER & CHEM L':           'GUJARAT NARMADA FERT',
    'KALPATARU PROJECT INT LTD':          'KALPATARU PROJECTS',
    'KALPATARU LIMITED':                  'KALPATARU',
    'L&T TECHNOLOGY SER. LTD.':          'L&T TECHNOLOGY SERVICES',
    'ZENSAR TECHNOLOGIES  LTD':           'ZENSAR TECHNOLOGIES',
    'ZENSAR TECHNOLOGIES LTD':            'ZENSAR TECHNOLOGIES',
    'ORACLE FIN SERV SOFT LTD.':          'ORACLE FINANCIAL SERVICES',
    'SPANDANA SPHOORTY FIN LTD':          'SPANDANA SPHOORTY',
    'PIRAMAL PHARMA LIMITED':             'PIRAMAL PHARMA',
    'NETWEB TECH INDIA LTD':              'NETWEB TECHNOLOGIES',
    'THE INDIAN HOTELS CO. LTD':          'INDIAN HOTELS',
    'SHAILY ENG PLASTICS LTD':            'SHAILY ENGINEERING',
    'MMTC LIMITED':                       'MMTC',
    'TANLA PLATFORMS LIMITED':            'TANLA PLATFORMS',
    'ZEN TECHNOLOGIES LIMITED':           'ZEN TECHNOLOGIES',
    'INTELLECT DESIGN ARENA LIMITED':     'INTELLECT DESIGN ARENA',
    'INTELLECT DESIGN ARENA':             'INTELLECT DESIGN ARENA',
    'INTERGLOBE AVIATION LTD':            'INTERGLOBE AVIATION',
    'GARWARE TECH FIBRES LTD':            'GARWARE TECH FIBRES',
    'INOX WIND LIMITED':                  'INOX WIND',
    'Inox Wind Limited':                  'INOX WIND',
    'MOSCHIP TECHNOLOGIES LTD':           'MOSCHIP TECHNOLOGIES',
    'MOSCHIP TECHNOLOGIES LIMITED':       'MOSCHIP TECHNOLOGIES',
    'POONAWALLA FINCORP LTD':             'POONAWALLA FINCORP',
    'NATCO PHARMA LTD.':                  'NATCO PHARMA',
    'PG ELECTROPLAST LTD':               'PG ELECTROPLAST',
    'PG ELECTROPLAST LTD.':              'PG ELECTROPLAST',
    'ANGEL ONE LIMITED':                  'ANGEL ONE',
    'REC LIMITED':                        'REC',
    'VARUN BEVERAGES LIMITED':            'VARUN BEVERAGES',
    'Mazagon Dock Shipbuilders Limi':     'MAZAGON DOCK',
    'MAZAGON DOCK SHIPBUIL LTD':          'MAZAGON DOCK',
    'OBEROI REALTY LIMITED':              'OBEROI REALTY',
    'PERSISTENT SYSTEMS LTD':             'PERSISTENT SYSTEMS',
    'DEVYANI INTERNATIONAL LTD':          'DEVYANI INTERNATIONAL',
    'COMPUTER AGE MNGT SER LTD':          'CAMS',
    'DATA PATTERNS INDIA LTD':            'DATA PATTERNS',
    'GMR AIRPORTS LIMITED':               'GMR AIRPORTS',
    'THE SOUTH INDIAN BANK LTD':          'SOUTH INDIAN BANK',
    'ALKYL AMINES CHEM. LTD':             'ALKYL AMINES',
    'ALEMBIC PHARMA LTD':                 'ALEMBIC PHARMA',
    'ASHAPURA MINECHEM LTD':              'ASHAPURA MINECHEM',
    'BALKRISHNA IND. LTD':                'BALKRISHNA IND',
    'BANK OF INDIA':                      'BANK OF INDIA',
    'BLUE JET HEALTHCARE LTD':            'BLUE JET HEALTHCARE',
    'CENTURY PLYBOARDS (I) LTD':          'CENTURY PLYBOARDS',
    'COFORGE LIMITED':                    'COFORGE',
    'GODREJ INDUSTRIES LTD':              'GODREJ INDUSTRIES',
    'GODREJ PROPERTIES LTD':              'GODREJ PROPERTIES',
    'INDOCO REMEDIES LTD.':               'INDOCO REMEDIES',
    'JM FINANCIAL LTD.':                  'JM FINANCIAL',
    'LUPIN LIMITED':                      'LUPIN',
    'M&M FIN. SERVICES LTD':              'M&M FINANCE',
    'MIC ELECTRONICS LTD':               'MIC ELECTRONICS',
    'MRPL':                               'MRPL',
    'NELCO LTD':                          'NELCO',
    'NTPC LTD':                           'NTPC',
    'PARADEEP PHOSPHATES LTD':            'PARADEEP PHOSPHATES',
    'WIPRO LTD':                          'WIPRO',
    'WHIRLPOOL OF INDIA LTD':             'WHIRLPOOL INDIA',
    'VST INDUSTRIES LTD':                 'VST INDUSTRIES',
    'ZYDUS LIFESCIENCES LTD':             'ZYDUS LIFESCIENCES',
    'REDINGTON LTD':                      'REDINGTON',
    'DOMS INDUSTRIES LIMITED':            'DOMS INDUSTRIES',
    'SUZLON ENERGY LTD.':                 'SUZLON ENERGY',
    # variants found across client CSVs
    'Dixon Technologies (India) Lim':    'DIXON TECHNOLOGIES',
    'Emmvee Photovoltaic Power Limi':    'EMMVEE PHOTOVOLTAIC',
    'Equitas Small Finance Bank Lim':    'EQUITAS SMALL FIN BANK',
    'Inox Wind Limited':                 'INOX WIND',
    'Kalpataru Limited':                 'KALPATARU',
    'Laurus Labs Limited':               'LAURUS LABS',
    'Redington Limited':                 'REDINGTON',
    'Sagility Limited':                  'SAGILITY LIMITED',
    'Samvardhana Motherson Internat':    'SAMVARDHANA MOTHERSON',
    'Tejas Networks Limited':            'TEJAS NETWORKS',
    'Delhivery Limited':                 'DELHIVERY',
    'DELHIVERY LIMITED':                 'DELHIVERY',
    'BHARAT ELECTRONICS LTD':            'BHARAT ELECTRONICS',
    'HINDUSTAN COPPER LTD':              'HINDUSTAN COPPER',
    'HINDUSTAN COPPER LTD.':             'HINDUSTAN COPPER',
    'HINDUSTAN ZINC LTD.':               'HINDUSTAN ZINC',
    'HINDUSTAN ZINC LIMITED':            'HINDUSTAN ZINC',
    'IDBI BANK LTD.':                    'IDBI BANK',
    'INFOSYS LIMITED':                   'INFOSYS',
    'ITC LTD':                           'ITC',
    'ITC LTD.':                          'ITC',
    'JM FINANCIAL LIMITED':              'JM FINANCIAL',
    'JSW ENERGY LIMITED':                'JSW ENERGY',
    'KPIT TECHNOLOGIES LIMITED':         'KPIT TECHNOLOGIES',
    'LIFE INSURANCE CORPORATION OF':     'LIC',
    'MIRAEAMC - MAGOLDETF':              'MIRAEAMC MAGOLDETF',
    'NETWEB TECHNOLOGIES INDIA LIMI':    'NETWEB TECHNOLOGIES',
    'NIPPON INDIA ETF LIQUID BEES':      'NIPPON ETF LIQUID BEES',
    'NIP IND ETF LIQUID BEES':           'NIPPON ETF LIQUID BEES',
    'NIPPON L I A M LTD':                'NIPPON LIFE AMC',
    'NIPPON LIFE INDIA ASSET MANAGE':    'NIPPON LIFE AMC',
    'ORACLE FINANCIAL SERVICES SOFT':    'ORACLE FINANCIAL SERVICES',
    'CANARA BANK':                       'CANARA BANK',
    'FORTIS HEALTHCARE LTD':             'FORTIS HEALTHCARE',
    'FORTIS HEALTHCARE LTD.':            'FORTIS HEALTHCARE',
    'GARWARE TECHNICAL FIBRES LIMIT':    'GARWARE TECH FIBRES',
    'HDFC BANK LTD':                     'HDFC BANK',
    'IDFC FIRST BANK LIMITED':           'IDFC FIRST BANK',
    'MSTC LIMITED':                      'MSTC',
    'NEWGEN SOFTWARE TECH LTD':          'NEWGEN SOFTWARE',
    'NIP IND ETF IT':                    'NIPPON ETF IT',
    'RELIANCE INDUSTRIES LTD':           'RELIANCE INDUSTRIES',
    'TRANSPORT CORPN OF INDIA':          'TRANSPORT CORP OF INDIA',
    'CHOLAMANDALAM FIN HOL LTD':         'CHOLAMANDALAM',
    'BOMBAY BURMAH TRADING COR':         'BOMBAY BURMAH TRADING',
    'TATA CONSULTANCY SERV LT':          'TCS',
    'MANGALORE REFINERY & PETROCHEM':    'MRPL',
    'INTELLECT DESIGN ARENA LIMITED':    'INTELLECT DESIGN ARENA',
}

def norm(name):
    n = str(name).strip().upper()
    # try exact match first
    if n in RAW:
        return RAW[n]
    # try mixed-case key
    for k, v in RAW.items():
        if k.upper() == n:
            return v
    return n

def parse_date(s):
    for fmt in ('%d %b %Y', '%d-%b-%y', '%d %b %y'):
        try:
            return pd.to_datetime(s, format=fmt)
        except:
            pass
    return pd.to_datetime(s, errors='coerce')

def load_csv(path):
    df = pd.read_csv(path)
    df['TRADE DATE'] = df['TRADE DATE'].apply(parse_date)
    df['SELL/BUY'] = df['SELL/BUY'].str.strip()
    df['SCRIP NAME'] = df['SCRIP NAME'].str.strip()
    df['SCRIP'] = df['SCRIP NAME'].apply(norm)
    return df

# ── gather files per client ──────────────────────────────────────────────────
CLIENT_FILES = {
    'RIMK1209': [
        r'C:\Users\jainr\Downloads\TradeDetailsAndSummary_RIMK1209 (1).csv',
        r'C:\Users\jainr\Downloads\TradeDetailsAndSummary_RIMK1209.csv',
    ],
    'RIMK1220': [
        r'C:\Users\jainr\Downloads\TradeDetailsAndSummary_RIMK1220 (1).csv',
        r'C:\Users\jainr\Downloads\TradeDetailsAndSummary_RIMK1220.csv',
    ],
    'RIMK1238': [
        r'C:\Users\jainr\Downloads\TradeDetailsAndSummary_RIMK1238.csv',
    ],
    'RIMK1248': [
        r'C:\Users\jainr\Downloads\TradeDetailsAndSummary_RIMK1248.csv',
    ],
    'RIMK1252': [
        r'C:\Users\jainr\Downloads\TradeDetailsAndSummary_RIMK1252.csv',
    ],
}

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
            })
        ).reset_index()
        orders['ORDER NO SORT'] = orders['ORDER NO'].astype(str).str.lstrip("'").str.strip()
        orders['ORDER NO SORT'] = pd.to_numeric(orders['ORDER NO SORT'], errors='coerce').fillna(0)
        orders = orders.sort_values(['TRADE DATE', 'ORDER NO SORT']).drop(columns=['ORDER NO SORT'])

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
