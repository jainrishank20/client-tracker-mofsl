"""
Standalone GSheet sync — extracts sync_to_gsheet() from app.py without
loading Streamlit or any other UI dependency.
Run: python3 vm_sync_gsheet.py
"""
import json, os, sys, types

BASE = os.path.dirname(os.path.abspath(__file__))

# ── Minimal stubs so app.py imports without crashing ─────────────────────────
def _make_stub(name):
    m = types.ModuleType(name)
    # make any attribute access return a no-op callable or another stub
    class _Stub:
        def __init__(self, *a, **k): pass
        def __call__(self, *a, **k): return _Stub()
        def __getattr__(self, n): return _Stub()
        def __iter__(self): return iter([])
        def __bool__(self): return False
    m.__getattr__ = lambda n: _Stub()
    return m

# Streamlit stub
st = _make_stub("streamlit")
st.session_state      = {}
st.secrets            = {}
st.cache_data         = types.SimpleNamespace(
    clear=lambda: None,
    __call__=lambda fn=None, **k: (lambda f: f)(fn) if fn else lambda f: f
)
class _CM:
    def __enter__(self): return self
    def __exit__(self, *a): pass
st.spinner = lambda *a, **k: _CM()
st.set_page_config = lambda **k: None

for mod in ["streamlit", "streamlit.components", "streamlit.components.v1",
            "plotly", "plotly.express", "plotly.graph_objects"]:
    sys.modules[mod] = _make_stub(mod)
sys.modules["streamlit"] = st

# ── Load app.py ───────────────────────────────────────────────────────────────
import importlib.util
spec = importlib.util.spec_from_file_location("app", os.path.join(BASE, "app.py"))
app  = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(app)
except Exception:
    pass  # page-render code fails — that's fine, we only need sync_to_gsheet

# ── Run sync ──────────────────────────────────────────────────────────────────
trades_file = os.path.join(BASE, "trades.json")
if not os.path.exists(trades_file):
    print("ERROR: trades.json not found")
    sys.exit(1)

with open(trades_file) as f:
    trades = json.load(f)

print(f"Loaded {len(trades)} trades. Syncing to Google Sheet...")
result = app.sync_to_gsheet(trades)
print(f"Done: {result}")
