import pandas as pd, json
from collections import deque

name_map = {
    'BHARAT COKING COAL LTD': 'BHARAT COKING COAL LIMITED',
    'DIXON TECHNO (INDIA) LTD': 'DIXON TECHNOLOGIES LTD',
    'DIXON TECHNOLOGIES (INDIA) LIM': 'DIXON TECHNOLOGIES LTD',
    'EMMVEE PHOTOVOLTAIC PWR L': 'EMMVEE PHOTOVOLTAIC',
    'EMMVEE PHOTOVOLTAIC POWER LIMI': 'EMMVEE PHOTOVOLTAIC',
    'EQUITAS SMALL FIN BNK LTD': 'EQUITAS SMALL FINANCE BANK',
    'EQUITAS SMALL FINANCE BANK LIM': 'EQUITAS SMALL FINANCE BANK',
    'EXIDE INDUSTRIES LTD.': 'EXIDE INDUSTRIES LTD',
    'GRAPHITE INDIA LTD.': 'GRAPHITE INDIA LTD',
    'JBM AUTO LTD.': 'JBM AUTO LIMITED',
    'LAURUS LABS LIMITED': 'LAURUS LABS LTD',
    'REDINGTON LIMITED': 'REDINGTON LTD',
    'SAMVRDHNA MTHRSN INTL LTD': 'SAMVARDHANA MOTHERSON',
    'SAMVARDHANA MOTHERSON INTERNAT': 'SAMVARDHANA MOTHERSON',
    'SUZLON ENERGY LTD.': 'SUZLON ENERGY LIMITED',
    'TEJAS NETWORKS LIMITED': 'TEJAS NETWORKS LTD',
    'WOCKHARDT LTD.': 'WOCKHARDT LIMITED',
    'PI INDUSTRIES LTD': 'PI INDUSTRIES LTD',
    'P.I.INDUSTRIES LTD.': 'PI INDUSTRIES LTD',
    'NMDC LTD.': 'NMDC LTD',
    'NIPPONAMC - NETFSILVER': 'NIPPON NETFSILVER',
    'MIRAEAMC - ENERGY': 'MIRAE ENERGY ETF',
    'IIFL FINANCE LIMITED': 'IIFL FINANCE LTD',
    'IFCI LTD.': 'IFCI LTD',
    'INDIAN ENERGY EXC LTD': 'INDIAN ENERGY EXCHANGE',
    'COMPUTER AGE MNGT SER LTD': 'CAMS LTD',
    'GRINDWELL NORTON LIMITED': 'GRINDWELL NORTON LTD',
    'HIMADRI SPECIALITY CHEM L': 'HIMADRI SPECIALITY CHEM LTD',
    'GUJ NAR VAL FER & CHEM L': 'GUJARAT NARMADA VALLEY FERT',
    'KALPATARU PROJECT INT LTD': 'KALPATARU PROJECTS LTD',
    'L&T TECHNOLOGY SER. LTD.': 'L&T TECHNOLOGY SERVICES LTD',
    'ZENSAR TECHNOLOGIES  LTD': 'ZENSAR TECHNOLOGIES LTD',
    'ORACLE FIN SERV SOFT LTD.': 'ORACLE FINANCIAL SERVICES LTD',
    'SPANDANA SPHOORTY FIN LTD': 'SPANDANA SPHOORTY FIN LTD',
    'PIRAMAL PHARMA LIMITED': 'PIRAMAL PHARMA LTD',
    'NETWEB TECH INDIA LTD': 'NETWEB TECHNOLOGIES LTD',
    'THE INDIAN HOTELS CO. LTD': 'INDIAN HOTELS LTD',
    'WOCKHARDT LIMITED': 'WOCKHARDT LTD',
    'SHAILY ENG PLASTICS LTD': 'SHAILY ENGINEERING PLASTICS',
    'MMTC LIMITED': 'MMTC LTD',
    'TANLA PLATFORMS LIMITED': 'TANLA PLATFORMS LTD',
    'ZEN TECHNOLOGIES LIMITED': 'ZEN TECHNOLOGIES LTD',
    'INTELLECT DESIGN ARENA LIMITED': 'INTELLECT DESIGN ARENA LTD',
    'INTERGLOBE AVIATION LTD': 'INTERGLOBE AVIATION LTD',
}

def load_and_clean(path):
    df = pd.read_csv(path)
    df['TRADE DATE'] = pd.to_datetime(df['TRADE DATE'], format='%d-%b-%y', errors='coerce')
    df['SELL/BUY'] = df['SELL/BUY'].str.strip()
    df['SCRIP NAME'] = df['SCRIP NAME'].str.strip().str.upper()
    df['SCRIP'] = df['SCRIP NAME'].apply(lambda x: name_map.get(x, x))
    return df

# Load both — older first, newer second
df1 = load_and_clean(r'C:\Users\jainr\Downloads\TradeDetailsAndSummary_RIMK1209 (1).csv')
df2 = load_and_clean(r'C:\Users\jainr\Downloads\TradeDetailsAndSummary_RIMK1209.csv')
df = pd.concat([df1, df2], ignore_index=True).sort_values('TRADE DATE')
print(f'Combined: {len(df)} rows | {df["TRADE DATE"].min().date()} to {df["TRADE DATE"].max().date()}')

# Aggregate to order level (collapse partial fills)
orders = df.groupby(['TRADE DATE','SCRIP','SELL/BUY','ORDER NO']).apply(
    lambda g: pd.Series({
        'qty':      g['TRADE QTY'].sum(),
        'net_rate': round(g['NET RATE'].mean(), 2),
    })
).reset_index().sort_values('TRADE DATE')

# FIFO matching per script
trades, trade_id = [], 1

for scrip in sorted(orders['SCRIP'].unique()):
    s = orders[orders['SCRIP'] == scrip].sort_values('TRADE DATE')
    buy_queue = deque()

    for _, row in s.iterrows():
        if row['SELL/BUY'] == 'B':
            buy_queue.append({
                'date': row['TRADE DATE'],
                'qty': row['qty'],
                'net_rate': row['net_rate']
            })
        else:
            sell_rem = row['qty']
            while sell_rem > 0 and buy_queue:
                buy = buy_queue[0]
                qty = min(buy['qty'], sell_rem)
                trades.append({
                    'id': trade_id,
                    'client': 'RIMK1209',
                    'script': scrip,
                    'type': 'Long',
                    'entry_date': buy['date'].strftime('%Y-%m-%d'),
                    'buy_qty': float(qty),
                    'buy_price': float(buy['net_rate']),
                    'exit_date': row['TRADE DATE'].strftime('%Y-%m-%d'),
                    'sell_qty': float(qty),
                    'sell_price': float(row['net_rate']),
                    'notes': 'auto-imported'
                })
                trade_id += 1
                buy['qty'] -= qty
                sell_rem -= qty
                if buy['qty'] <= 0:
                    buy_queue.popleft()

    # Remaining buys = still open
    while buy_queue:
        buy = buy_queue.popleft()
        trades.append({
            'id': trade_id,
            'client': 'RIMK1209',
            'script': scrip,
            'type': 'Long',
            'entry_date': buy['date'].strftime('%Y-%m-%d'),
            'buy_qty': float(buy['qty']),
            'buy_price': float(buy['net_rate']),
            'exit_date': None,
            'sell_qty': 0,
            'sell_price': 0,
            'notes': 'auto-imported'
        })
        trade_id += 1

# Summary
closed  = [t for t in trades if t['exit_date']]
opened  = [t for t in trades if not t['exit_date']]
pnl     = sum((t['sell_price'] - t['buy_price']) * t['sell_qty'] for t in closed)
capital = sum(t['buy_qty'] * t['buy_price'] for t in opened)
winners = sum(1 for t in closed if t['sell_price'] > t['buy_price'])
wr      = winners / len(closed) * 100 if closed else 0

print(f'\n========== RIMK1209 Full History ==========')
print(f'Total trades    : {len(trades)}')
print(f'Open positions  : {len(opened)}')
print(f'Closed trades   : {len(closed)}')
print(f'Win rate        : {winners}/{len(closed)} = {wr:.0f}%')
print(f'Realised P&L    : Rs {pnl:,.0f}')
print(f'Capital deployed: Rs {capital:,.0f}')

# Save FIRST
out_path = r'C:\Users\jainr\Desktop\raghava_tracker\trades.json'
with open(out_path, 'w') as f:
    json.dump(trades, f, indent=2)
print(f'Saved {len(trades)} trades -> {out_path}')

print('\nMonthly Realised P&L:')
monthly = {}
for t in closed:
    m = t['exit_date'][:7]
    monthly[m] = monthly.get(m, 0) + (t['sell_price'] - t['buy_price']) * t['sell_qty']
for m in sorted(monthly):
    v = monthly[m]
    sign = '+' if v >= 0 else ''
    print(f'  {m}  {sign}Rs {v:>10,.0f}')
