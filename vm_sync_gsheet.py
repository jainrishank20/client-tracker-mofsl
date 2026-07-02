"""
Standalone GSheet sync. Run: python3 vm_sync_gsheet.py
Copies only what sync_to_gsheet() needs from app.py — no Streamlit required.
"""
import json, os, sys
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))

GSHEET_KEY = os.path.join(BASE, "gsheet_key.json")
GSHEET_ID  = "1RBaZYY8Eheet13UJy6eRMJIFUzU9Yii335l5x_H5KVo"

CLIENTS = ["RIMK1205","RIMK1209","RIMK1215","RIMK1220","RIMK1238",
           "RIMK1247","RIMK1248","RIMK1249","RIMK1252","RIMK1256"]
CLIENT_NAMES = {
    "RIMK1205":"Siva Sankara Reddy","RIMK1209":"Sathyavrath",
    "RIMK1215":"Malleswari","RIMK1220":"Kalpana",
    "RIMK1238":"Iranna","RIMK1247":"Srujana",
    "RIMK1248":"Udayakumar","RIMK1249":"Sundareshwari",
    "RIMK1252":"Savitha","RIMK1256":"Sheeba",
}

TICKER_OVERRIDES_FILE = os.path.join(BASE, "ticker_overrides.json")

SYMBOL_MAP = {
    "ALKYL AMINES":"ALKYLAMINE","BALKRISHNA IND":"BALKRISIND",
    "BHARAT COKING COAL":"BHARATCOAL","BHARAT ELECTRONICS":"BEL",
    "BOMBAY BURMAH TRADING":"BBTC","CAMS":"CAMS","CANARA BANK":"CANBK",
    "CENTURY PLYBOARDS":"CENTURYPLY","CHOLAMANDALAM":"CHOLAFIN",
    "COFORGE":"COFORGE","DATA PATTERNS":"DATAPATTNS",
    "DELHIVERY":"DELHIVERY","DEVYANI INTERNATIONAL":"DEVYANI",
    "DIXON TECHNOLOGIES":"DIXON","DOMS INDUSTRIES":"DOMS",
    "EMMVEE PHOTOVOLTAIC":"EMMVEE","EQUITAS SMALL FIN BANK":"EQUITASBNK",
    "EICHER MOTORS":"EICHERMOT","EXIDE INDUSTRIES":"EXIDEIND",
    "FORTIS HEALTHCARE":"FORTIS","GARWARE TECH FIBRES":"GARFIBRES",
    "GMR AIRPORTS":"GMRAIRPORT","GODREJ INDUSTRIES":"GODREJIND",
    "GODREJ PROPERTIES":"GODREJPROP","GRAPHITE INDIA":"GRAPHITE",
    "GRINDWELL NORTON":"GRINDWELL","HDFC BANK":"HDFCBANK",
    "HIMADRI SPECIALITY CHEM":"HSCL","HINDUSTAN COPPER":"HINDCOPPER",
    "HINDUSTAN ZINC":"HINDZINC","IDBI BANK":"IDBI",
    "IDFC FIRST BANK":"IDFCFIRSTB","IIFL FINANCE":"IIFL",
    "IFCI":"IFCI","INDIAN ENERGY EXCHANGE":"IEX",
    "INDIAN HOTELS":"INDHOTEL","INDOCO REMEDIES":"INDOCO",
    "INFOSYS":"INFY","INTELLECT DESIGN ARENA":"INTELLECT",
    "INTERGLOBE AVIATION":"INDIGO","INOX WIND":"INOXWIND",
    "ITC":"ITC","JBM AUTO":"JBMA","JM FINANCIAL":"JMFINANCIL",
    "JSW ENERGY":"JSWENERGY","JUPITER WAGONS LIMITED":"JUPITERWAG",
    "KALPATARU":"KALPATARU","KALPATARU PROJECTS":"KPIL",
    "KAYNES TECHNOLOGY IND LTD":"KAYNES","KPIT TECHNOLOGIES":"KPITTECH",
    "L&T TECHNOLOGY SERVICES":"LTTS","LAURUS LABS":"LAURUSLABS",
    "LIC":"LICI","LUPIN":"LUPIN","M&M FINANCE":"M&MFIN",
    "MAX HEALTHCARE INS LTD":"MAXHEALTH","MAZAGON DOCK":"MAZDOCK",
    "MIRAE ENERGY ETF":"MAFANG","MIRAEAMC MAGOLDETF":"GOLDETF",
    "MIC ELECTRONICS":"MICEL","MOSCHIP TECHNOLOGIES":"MOSCHIP",
    "MRPL":"MRPL","MSTC":"MSTCLTD","NATCO PHARMA":"NATCOPHARM",
    "NELCO":"NELCO","NETWEB TECHNOLOGIES":"NETWEB",
    "NEWGEN SOFTWARE":"NEWGEN","NIMBUS AGRI":"NIMBUS",
    "NIPPON ETF IT":"ITBEES","NIPPON ETF LIQUID BEES":"LIQUIDBEES",
    "NIPPON LIFE AMC":"NAM-INDIA","NIPPON NETFSILVER":"SILVERBEES",
    "NMDC":"NMDC","NTPC":"NTPC","OBEROI REALTY":"OBEROIRLTY",
    "ORACLE FINANCIAL SERVICES":"OFSS","PARADEEP PHOSPHATES":"PARADEEP",
    "PERSISTENT SYSTEMS":"PERSISTENT","PG ELECTROPLAST":"PGEL",
    "PI INDUSTRIES":"PIIND","PIRAMAL PHARMA":"PPLPHARMA",
    "POONAWALLA FINCORP":"POONAWALLA","REC":"RECLTD",
    "REDINGTON":"REDINGTON","RELIANCE INDUSTRIES":"RELIANCE",
    "SAGILITY LIMITED":"SAGILITY","SAMVARDHANA MOTHERSON":"MOTHERSON",
    "SHAILY ENGINEERING":"SHAILY","SOUTH INDIAN BANK":"SOUTHBANK",
    "SPANDANA SPHOORTY":"SPANDANA","SUZLON ENERGY":"SUZLON",
    "TANLA PLATFORMS":"TANLA","TCS":"TCS",
    "TEJAS NETWORKS":"TEJASNET","TRANSPORT CORP OF INDIA":"TCI",
    "VARUN BEVERAGES":"VBL","VST INDUSTRIES":"VSTIND",
    "WHIRLPOOL INDIA":"WHIRLPOOL","WIPRO":"WIPRO",
    "WOCKHARDT":"WOCKPHARMA","ZEN TECHNOLOGIES":"ZENTEC",
    "ZENSAR TECHNOLOGIES":"ZENSARTECH","ANGEL ONE":"ANGELONE",
    "BANK OF INDIA":"BANKINDIA","BLUE JET HEALTHCARE":"BLUEJET",
    "ALEMBIC PHARMA":"APLLTD","ASHAPURA MINECHEM":"ASHAPURMIN",
    "GUJARAT NARMADA FERT":"GNFC","ZYDUS LIFESCIENCES":"ZYDUSLIFE",
    "INOXWIND":"INOXWIND","KPIL":"KPIL","BAJAJ HOUSING FINANCE":"BAJAJHFL",
    "PREMIER ENERGIES LIMITED":"PREMIERENE","AVENUE SUPERMARTS LIMITED":"DMART",
    "BSE LIMITED":"BSE","LARSEN & TOUBRO LTD.":"LT","NIIT LIMITED":"NIITLTD",
    "POWER FIN CORP LTD.":"PFC","K.P.R.MILL LIMITED":"KPRMILL",
    "ELGI EQUIPMENTS LTD":"ELGIEQUIP","IRB INFRA DEV LTD.":"IRBINFRA",
    "JAIN RESOURCE RECYCLING LIMITE":"JAINREC","PHOENIX MILLS":"PHOENIXLTD",
    "BANK OF MAHARASHTRA":"MAHABANK","CANARA ROBECO ASSET MANAGEMENT":"CRAMC",
    "MMTC":"MMTC","CHOLAHLDNG":"CHOLAHLDNG","BALKRISIND":"BALKRISIND",
}


def load_ticker_overrides():
    if os.path.exists(TICKER_OVERRIDES_FILE):
        try:
            with open(TICKER_OVERRIDES_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def fetch_cmp(scripts):
    """Fetch live CMP — returns empty dict on VM (no yfinance needed for sync)."""
    return {}


def gfinance_formula(script):
    overrides = load_ticker_overrides()
    if script in overrides and overrides[script]:
        sym = overrides[script].strip().upper().replace(".NS", "")
    else:
        sym = SYMBOL_MAP.get(script, script.replace(" ", "").replace("&", "").replace(".", ""))
    return f'=GOOGLEFINANCE("NSE:{sym}","price")'


# ── Pull sync_to_gsheet from app.py ──────────────────────────────────────────
# Inject the dependencies it needs into a minimal namespace, then exec only
# the function definition (lines 13-end of the function).

import importlib.util, types

# Build a fake module with everything sync_to_gsheet references
fake = types.ModuleType("app_sync")
fake.__file__ = os.path.join(BASE, "app.py")
fake.pd              = pd
fake.GSHEET_KEY      = GSHEET_KEY
fake.GSHEET_ID       = GSHEET_ID
fake.CLIENTS         = CLIENTS
fake.CLIENT_NAMES    = CLIENT_NAMES
fake.SYMBOL_MAP      = SYMBOL_MAP
fake.load_ticker_overrides = load_ticker_overrides
fake.fetch_cmp       = fetch_cmp
fake.gfinance_formula = gfinance_formula
fake.__import__      = __import__

# Read app.py and extract only the sync_to_gsheet function + needed constants
with open(os.path.join(BASE, "app.py")) as f:
    src = f.read()

# Find and exec just sync_to_gsheet
import ast, textwrap
tree = ast.parse(src)
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == "sync_to_gsheet":
        start = node.lineno - 1
        end   = node.end_lineno
        fn_src = "\n".join(src.splitlines()[start:end])
        exec(compile(fn_src, "app.py", "exec"), vars(fake))
        break

sync_to_gsheet = fake.sync_to_gsheet

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
