"""
Full rebuild for clients with saved CSVs. Preserves RIMK1209 data from existing trades.json.
"""
import sys, os, json
sys.path.insert(0, '.')
import import_all as mod
import pandas as pd
from collections import deque

DATA_FILE = r'C:\Users\jainr\Desktop\raghava_tracker\trades.json'
PROCESSED_FILE = r'C:\Users\jainr\Desktop\raghava_tracker\processed_orders.json'
RAW_CSV_DIR = 'raw_csvs'
CLIENTS_WITH_CSV = ['RIMK1209', 'RIMK1220', 'RIMK1238', 'RIMK1248', 'RIMK1249', 'RIMK1252']
CHARGE_K = ['brokerage', 'stt', 'gst', 'stamp', 'txn_chrg']


def saved_csvs_for(client):
    d = os.path.join(RAW_CSV_DIR, client)
    if not os.path.exists(d):
        return []
    return sorted([os.path.join(d, f) for f in os.listdir(d) if f.endswith('.csv')])


def make_order_key(order_no, trade_date, scrip, side):
    date_str = str(trade_date)[:10] if trade_date else ''
    no_str = str(order_no).strip().lstrip("'")
    return f"{no_str}|{date_str}|{str(scrip).strip().upper()}|{str(side).strip().upper()}"


def parse_csvs(files):
    frames = []
    for f in files:
        df = pd.read_csv(f)
        df['TRADE DATE'] = df['TRADE DATE'].apply(mod.parse_date)
        df['SELL/BUY'] = df['SELL/BUY'].str.strip()
        df['SCRIP NAME'] = df['SCRIP NAME'].str.strip()
        df['SCRIP'] = df['SCRIP NAME'].apply(mod.norm)
        frames.append(df)
    if not frames:
        return pd.DataFrame(), set()
    df = pd.concat(frames, ignore_index=True)
    # Dedup using TRADE NO — each execution gets a unique ID in MO CSVs
    if 'TRADE NO' in df.columns:
        df = df.drop_duplicates(subset=['TRADE DATE', 'TRADE NO', 'SCRIP NAME', 'SELL/BUY', 'TRADE QTY'])
    else:
        df = df.drop_duplicates()
    df['_order_key'] = df.apply(lambda r: make_order_key(
        r.get('ORDER NO', ''), r.get('TRADE DATE', ''),
        r.get('SCRIP NAME', ''), r.get('SELL/BUY', '')), axis=1)
    charge_cols = ['BROKERAGE', 'TRANSACTION CHARGES', 'GST', 'STAMP DUTY', 'STT/CTT', 'SEBI CHARGES', 'IPFT CHARGES']
    for col in charge_cols:
        if col not in df.columns:
            df[col] = 0
        df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
    orders = df.groupby(['TRADE DATE', 'SCRIP', 'SELL/BUY', 'ORDER NO']).apply(
        lambda g: pd.Series({
            'qty': g['TRADE QTY'].sum(),
            'price': round((g['MARKET PRICE'] * g['TRADE QTY']).sum() / g['TRADE QTY'].sum(), 2),
            'net_rate': round(g['NET RATE'].mean(), 2),
            'brokerage': round(g['BROKERAGE'].sum(), 2),
            'stt': round(g['STT/CTT'].sum(), 2),
            'gst': round(g['GST'].sum(), 2),
            'stamp': round(g['STAMP DUTY'].sum(), 2),
            'txn_chrg': round(g['TRANSACTION CHARGES'].sum(), 2),
        })
    ).reset_index()
    orders['_sort'] = pd.to_numeric(
        orders['ORDER NO'].astype(str).str.lstrip("'").str.strip(), errors='coerce').fillna(0)
    # Within same date, sort buys (B=0) before sells (S=1) so intraday sells always
    # find their matching buy even when sell ORDER NOs happen to be numerically lower.
    orders['_side_sort'] = (orders['SELL/BUY'] == 'S').astype(int)
    orders = orders.sort_values(['TRADE DATE', '_side_sort', '_sort']).drop(columns=['_sort', '_side_sort'])
    return orders, set(df['_order_key'].tolist())


def fifo(client, orders, trade_id_start):
    all_trades, trade_id = [], trade_id_start
    buy_queues = {}
    for scrip in sorted(orders['SCRIP'].unique()):
        s = orders[orders['SCRIP'] == scrip]
        buy_queues[scrip] = deque()
        bq = buy_queues[scrip]
        for _, row in s.iterrows():
            if row['SELL/BUY'] == 'B':
                bq.append({k: row[k] for k in ['TRADE DATE', 'qty', 'price', 'net_rate'] + CHARGE_K})
            else:
                sell_rem = row['qty']
                while sell_rem > 0 and bq:
                    buy = bq[0]
                    qty = min(buy['qty'], sell_rem)
                    fb = qty / buy['qty'] if buy['qty'] else 0
                    fs = qty / row['qty'] if row['qty'] else 0
                    t = {
                        'id': trade_id, 'client': client, 'script': scrip, 'type': 'Long',
                        'entry_date': buy['TRADE DATE'].strftime('%Y-%m-%d'),
                        'buy_qty': float(qty), 'buy_price': float(buy['price']),
                        'buy_net_rate': float(buy['net_rate']),
                        'exit_date': row['TRADE DATE'].strftime('%Y-%m-%d'),
                        'sell_qty': float(qty), 'sell_price': float(row['price']),
                        'sell_net_rate': float(row['net_rate']), 'notes': 'imported',
                    }
                    for k in CHARGE_K:
                        t['buy_' + k] = round(float(buy[k]) * fb, 2)
                        t['sell_' + k] = round(float(row[k]) * fs, 2)
                    total_buy_chg  = sum(t['buy_'  + k] for k in CHARGE_K)
                    total_sell_chg = sum(t['sell_' + k] for k in CHARGE_K)
                    t['total_charges'] = round(total_buy_chg + total_sell_chg, 2)
                    t['pnl']     = round((t['sell_price'] - t['buy_price']) * qty, 2)
                    t['net_pnl'] = round(t['pnl'] - t['total_charges'], 2)
                    t['return_pct'] = round(t['pnl'] / (t['buy_price'] * qty) * 100, 2) if t['buy_price'] else 0
                    t['invested']   = round(t['buy_price'] * qty, 2)
                    all_trades.append(t)
                    trade_id += 1
                    buy['qty'] -= qty
                    sell_rem -= qty
                    if buy['qty'] <= 0:
                        bq.popleft()
        for buy in buy_queues[scrip]:
            t = {
                'id': trade_id, 'client': client, 'script': scrip, 'type': 'Long',
                'entry_date': buy['TRADE DATE'].strftime('%Y-%m-%d'),
                'buy_qty': float(buy['qty']), 'buy_price': float(buy['price']),
                'buy_net_rate': float(buy['net_rate']),
                'exit_date': None, 'sell_qty': 0, 'sell_price': 0, 'sell_net_rate': 0, 'notes': 'imported',
            }
            for k in CHARGE_K:
                t['buy_' + k] = float(buy.get(k, 0))
                t['sell_' + k] = 0
            all_trades.append(t)
            trade_id += 1
    return all_trades


# Rebuild all clients from saved CSVs
result = []
new_processed = {}
trade_id = 1

for client in CLIENTS_WITH_CSV:
    files = saved_csvs_for(client)
    if not files:
        print(f'{client}: no saved CSVs, skipping')
        continue
    orders, keys = parse_csvs(files)
    if orders.empty:
        print(f'{client}: empty orders')
        continue
    new = fifo(client, orders, trade_id)
    open_c = sum(1 for t in new if not t['exit_date'])
    closed_c = sum(1 for t in new if t['exit_date'])
    print(f'{client}: {len(new)} trades ({open_c} open, {closed_c} closed)')
    result.extend(new)
    trade_id += len(new)
    new_processed[client] = list(keys)

# Renumber IDs sequentially
for i, t in enumerate(result, 1):
    t['id'] = i

# Save
with open(DATA_FILE, 'w') as f:
    json.dump(result, f, indent=2)

# Update processed_orders: keep RIMK1209 keys, replace others
with open(PROCESSED_FILE) as f:
    old_po = json.load(f)
new_po = {k: v for k, v in old_po.items() if k not in CLIENTS_WITH_CSV}
new_po.update(new_processed)
with open(PROCESSED_FILE, 'w') as f:
    json.dump(new_po, f, indent=2)

print(f'\nSaved {len(result)} trades to trades.json')
print('\nSanity check — verify no oversells:')
from collections import defaultdict
buy_tot = defaultdict(float)
sell_tot = defaultdict(float)
for t in result:
    key = (t['client'], t['script'])
    buy_tot[key] += t.get('buy_qty', 0)
    sell_tot[key] += t.get('sell_qty', 0)
oversells = [(k, buy_tot[k], sell_tot[k]) for k in buy_tot if sell_tot[k] > buy_tot[k] + 0.01]
if oversells:
    for k, b, s in oversells:
        print(f'  OVERSELL: {k} buy={b} sell={s}')
else:
    print('  All clear — no oversells detected.')

print('\nSavitha (RIMK1252) positions:')
for t in result:
    if t['client'] == 'RIMK1252':
        status = 'OPEN' if not t['exit_date'] else 'CLOSED'
        print(f'  {status} {t["script"]} qty={t["buy_qty"]} entry={t["entry_date"]} exit={t["exit_date"]}')
