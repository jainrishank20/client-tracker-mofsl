"""
Standalone GSheet sync — calls sync_to_gsheet() directly, no browser needed.
Run: python3 vm_sync_gsheet.py
"""
import json, os, sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

# Import sync function and required globals from app.py
import importlib.util
spec = importlib.util.spec_from_file_location("app", os.path.join(BASE, "app.py"))
app  = importlib.util.module_from_spec(spec)

# Minimal streamlit stub so app.py imports without crashing
import types
st = types.ModuleType("streamlit")
st.cache_data         = types.SimpleNamespace(clear=lambda: None)
st.session_state      = {}
st.secrets            = {}
st.set_page_config    = lambda **k: None
st.markdown           = lambda *a, **k: None
st.error              = lambda *a, **k: None
st.success            = lambda *a, **k: None
st.warning            = lambda *a, **k: None
st.info               = lambda *a, **k: None
st.spinner            = lambda *a, **k: types.SimpleNamespace(__enter__=lambda s: None, __exit__=lambda s,*a: None)
st.sidebar            = types.SimpleNamespace(
    title=lambda *a,**k: None, markdown=lambda *a,**k: None,
    button=lambda *a,**k: False, radio=lambda *a,**k: None,
    selectbox=lambda *a,**k: None, text_input=lambda *a,**k: ""
)
import sys as _sys
_sys.modules["streamlit"] = st
_sys.modules["streamlit.components"] = types.ModuleType("streamlit.components")
_sys.modules["streamlit.components.v1"] = types.ModuleType("streamlit.components.v1")

spec.loader.exec_module(app)

# Load trades
trades_file = os.path.join(BASE, "trades.json")
if not os.path.exists(trades_file):
    print("ERROR: trades.json not found")
    sys.exit(1)

with open(trades_file) as f:
    trades = json.load(f)

print(f"Loaded {len(trades)} trades. Syncing to Google Sheet...")
result = app.sync_to_gsheet(trades)
print(f"Done: {result}")
