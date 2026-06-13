import sys, json, glob, importlib.util
from collections import deque
from pathlib import Path
import pandas as pd

BASE = "/home/opc/client-tracker-mofsl"
sys.path.insert(0, BASE)

spec = importlib.util.spec_from_file_location("import_all", BASE+"/import_all.py")
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)

CHARGE_K = ["brokerage","stt","gst","stamp","txn_chrg"]
COL_MAP = {"brokerage":"BROKERAGE","stt":"STT/CTT","gst":"GST","stamp":"STAMP DUTY","txn_chrg":"TRANSACTION CHARGES"}
CHARGE_COLS = ["BROKERAGE","TRANSACTION CHARGES","GST","STAMP DUTY","STT/CTT","SEBI CHARGES","IPFT CHARGES"]

def parse_files(files):
    frames = []
    for f in files:
        df_raw = pd.read_csv(f)
        df_raw["TRADE DATE"] = df_raw["TRADE DATE"].apply(mod.parse_date)
        df_raw["SELL/BUY"] = df_raw["SELL/BUY"].str.strip()
        df_raw["SCRIP NAME"] = df_raw["SCRIP NAME"].str.strip()
        df_raw["SCRIP"] = df_raw["SCRIP NAME"].apply(mod.norm)
        frames.append(df_raw)
    df = pd.concat(frames, ignore_index=True)
    if "TRADE NO" in df.columns:
        df = df.drop_duplicates(subset=["TRADE DATE","TRADE NO","SCRIP NAME","SELL/BUY","TRADE QTY"])
    for col in CHARGE_COLS:
        if col not in df.columns:
            df[col] = 0
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    if "PRODUCT" not in df.columns:
        df["PRODUCT"] = "DELIVERY"
    else:
        df["PRODUCT"] = df["PRODUCT"].str.strip().fillna("DELIVERY")
        df["PRODUCT"] = df["PRODUCT"].apply(lambda p: "DELIVERY" if str(p).upper() == "DELIVERY" else "INTRADAY")
    orders = df.groupby(["TRADE DATE","SCRIP","SELL/BUY","ORDER NO","PRODUCT"]).apply(
        lambda g: pd.Series({
            "qty":      g["TRADE QTY"].sum(),
            "price":    round((g["MARKET PRICE"]*g["TRADE QTY"]).sum()/g["TRADE QTY"].sum(), 2),
            "net_rate": round(g["NET RATE"].mean(), 2),
            **{k: round(g[v].sum(), 2) for k, v in COL_MAP.items()},
        }), include_groups=False
    ).reset_index()
    orders["_sort"] = pd.to_numeric(
        orders["ORDER NO"].astype(str).str.lstrip("'").str.strip(), errors="coerce").fillna(0)
    return orders.sort_values(["TRADE DATE","_sort"]).drop(columns=["_sort"])

def fifo(client, orders, trade_id_start):
    all_trades, trade_id = [], trade_id_start
    buy_queues = {}
    for scrip in sorted(orders["SCRIP"].unique()):
        s = orders[orders["SCRIP"] == scrip]
        for _, row in s.iterrows():
            prod = row.get("PRODUCT", "DELIVERY")
            qkey = (scrip, prod)
            if qkey not in buy_queues:
                buy_queues[qkey] = deque()
            bq = buy_queues[qkey]
            if row["SELL/BUY"] == "B":
                entry = {"TRADE DATE": row["TRADE DATE"], "qty": row["qty"],
                         "price": row["price"], "net_rate": row["net_rate"]}
                for k in CHARGE_K:
                    entry[k] = row[k]
                bq.append(entry)
            else:
                sell_rem = row["qty"]
                while sell_rem > 0 and bq:
                    buy = bq[0]
                    qty = min(buy["qty"], sell_rem)
                    fb = qty/buy["qty"] if buy["qty"] else 0
                    fs = qty/row["qty"] if row["qty"] else 0
                    t = {
                        "id": trade_id, "client": client, "script": scrip, "type": "Long",
                        "entry_date": buy["TRADE DATE"].strftime("%Y-%m-%d"),
                        "buy_qty": float(qty), "buy_price": float(buy["price"]),
                        "buy_net_rate": float(buy["net_rate"]),
                        "exit_date": row["TRADE DATE"].strftime("%Y-%m-%d"),
                        "sell_qty": float(qty), "sell_price": float(row["price"]),
                        "sell_net_rate": float(row["net_rate"]), "notes": "imported",
                    }
                    for k in CHARGE_K:
                        t["buy_"+k] = round(float(buy[k])*fb, 2)
                        t["sell_"+k] = round(float(row[k])*fs, 2)
                    all_trades.append(t)
                    trade_id += 1
                    buy["qty"] -= qty
                    sell_rem -= qty
                    if buy["qty"] <= 0:
                        bq.popleft()
    for (scrip, prod), bq in buy_queues.items():
        if prod == "INTRADAY":
            continue
        for buy in bq:
            t = {
                "id": trade_id, "client": client, "script": scrip, "type": "Long",
                "entry_date": buy["TRADE DATE"].strftime("%Y-%m-%d"),
                "buy_qty": float(buy["qty"]), "buy_price": float(buy["price"]),
                "buy_net_rate": float(buy["net_rate"]),
                "exit_date": None, "sell_qty": 0, "sell_price": 0, "sell_net_rate": 0,
                "notes": "imported",
            }
            for k in CHARGE_K:
                t["buy_"+k] = float(buy.get(k, 0))
                t["sell_"+k] = 0
            all_trades.append(t)
            trade_id += 1
    return all_trades

def check_isin_conflicts(df):
    conflicts = []
    if "ISIN" not in df.columns:
        return conflicts
    isin_map = df.groupby("ISIN")["SCRIP"].apply(set).to_dict()
    for isin, scrips in isin_map.items():
        if len(scrips) > 1:
            conflicts.append("  WARNING: ISIN "+isin+" maps to multiple names: "+str(sorted(scrips)))
    return conflicts

def validate_fifo_balance(client, orders, client_trades):
    errors = []
    deliv = orders[orders["PRODUCT"] == "DELIVERY"] if "PRODUCT" in orders.columns else orders
    for scrip in sorted(deliv["SCRIP"].unique()):
        sp = deliv[deliv["SCRIP"] == scrip]
        raw_buy  = round(float(sp[sp["SELL/BUY"] == "B"]["qty"].sum()), 2)
        raw_sell = round(float(sp[sp["SELL/BUY"] == "S"]["qty"].sum()), 2)
        ct = [t for t in client_trades if t["script"] == scrip]
        fifo_buy  = round(sum(t["buy_qty"]  for t in ct), 2)
        fifo_sell = round(sum(t["sell_qty"] for t in ct), 2)
        if abs(fifo_buy - raw_buy) > 0.01:
            errors.append("  ERROR: "+client+" / "+scrip+": BUY qty — CSV="+str(raw_buy)+" FIFO="+str(fifo_buy)+" diff="+str(round(fifo_buy-raw_buy,2)))
        if abs(fifo_sell - raw_sell) > 0.01:
            errors.append("  ERROR: "+client+" / "+scrip+": SELL qty — CSV="+str(raw_sell)+" FIFO="+str(fifo_sell)+" diff="+str(round(fifo_sell-raw_sell,2)))
    return errors

all_trades = []
trade_id = 1
all_errors = []
all_warnings = []
clients = sorted([p.name for p in Path(BASE+"/raw_csvs").iterdir() if p.is_dir()])
for client in clients:
    files = sorted(glob.glob(BASE+"/raw_csvs/"+client+"/*.csv"))
    if not files:
        continue
    orders = parse_files(files)
    new = fifo(client, orders, trade_id)
    # validate before accepting
    warns = check_isin_conflicts(pd.concat([pd.read_csv(f) for f in files], ignore_index=True).assign(SCRIP=lambda d: d["SCRIP NAME"].str.strip().apply(mod.norm)) if True else pd.DataFrame())
    errs = validate_fifo_balance(client, orders, new)
    all_warnings.extend(warns)
    all_errors.extend(errs)
    all_trades.extend(new)
    trade_id += len(new)
    open_c = sum(1 for t in new if t["exit_date"] is None)
    closed_c = len(new) - open_c
    status = " ✓" if not errs else " ✗ ERRORS"
    print(client+": "+str(len(new))+" trades ("+str(open_c)+" open, "+str(closed_c)+" closed)"+status)

print("")
if all_warnings:
    print("=== ISIN WARNINGS ===")
    for w in all_warnings:
        print(w)
    print("")

if all_errors:
    print("=== FIFO BALANCE ERRORS ===")
    for e in all_errors:
        print(e)
    print("")
    print("REBUILD ABORTED — trades.json NOT updated. Fix errors above first.")
    sys.exit(1)

with open(BASE+"/trades.json", "w") as f:
    json.dump(all_trades, f, indent=2)
with open(BASE+"/processed_orders.json", "w") as f:
    json.dump({}, f)
print("ALL CHECKS PASSED. Total: "+str(len(all_trades))+" trades saved.")
