"""
Motilal Oswal CBOS - Automated Trade CSV Downloader

Usage:
    python mo_downloader.py           # current FY only (daily use)
    python mo_downloader.py --full    # both FYs (initial setup / new client)
"""
import asyncio, imaplib, email, email.utils, re, os, sys, time, json, hashlib
import datetime
from datetime import date, timezone
try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
except ImportError:
    async_playwright = None  # type: ignore
    PWTimeout = Exception    # type: ignore

# ── CONFIG ────────────────────────────────────────────────────────────────────
_cfg = json.loads(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bot_config.json'), encoding='utf-8-sig').read())
MO_USERNAME        = _cfg['mo_username']
MO_PASSWORD        = _cfg['mo_password']
GMAIL_USER         = _cfg['gmail_user']
GMAIL_APP_PASSWORD = _cfg['gmail_app_password']
TG_TOKEN           = _cfg.get('telegram_token', '')
TG_CHAT            = str(_cfg.get('allowed_chat_id', '')).split(',')[0].strip()

OTP_FILE = '/tmp/pending_otp.txt'  # written by Telegram bot /otp command

if "clients" not in _cfg or not _cfg["clients"]:
    raise RuntimeError("bot_config.json missing 'clients' key — cannot determine which accounts to download")
CLIENTS = list(_cfg["clients"].keys())

BASE         = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = (
    os.path.join(BASE, 'mo_csvs')
    if os.name != 'nt'
    else r"C:\Users\jainr\Downloads\MO_Trades"
)
LOGIN_URL    = "https://backoffice.motilaloswal.com/Login.aspx"
HOME_URL     = "https://backoffice.motilaloswal.com/Home.aspx"
DATE_OPTION  = "Current Financial Year"

FULL_MODE = "--full" in sys.argv
DOWNLOADS_ONLY = "--downloads-only" in sys.argv

def _current_financial_years():
    today = date.today()
    fy_start = today.year if today.month >= 4 else today.year - 1
    years = []
    for y in range(max(2024, fy_start - 1), fy_start + 1):
        years.append(f"{y}-{y+1}")
    return years

def _current_fy():
    today = date.today()
    fy_start = today.year if today.month >= 4 else today.year - 1
    return f"{fy_start}-{fy_start+1}"

FINANCIAL_YEARS = _current_financial_years() if FULL_MODE else [_current_fy()]

# Session-level registry: (client, fy) → md5 of saved CSV.
# If two entries share the same hash → downloader gave one client the wrong file.
_CSV_HASHES: dict[str, str] = {}


def _assert_csv_unique(client: str, fy: str, csv_path: str) -> None:
    """Raise immediately if this CSV is byte-identical to a previously saved one."""
    h = hashlib.md5(open(csv_path, "rb").read()).hexdigest()
    key = f"{client}_{fy}"
    for prev_key, prev_hash in _CSV_HASHES.items():
        if h == prev_hash:
            raise RuntimeError(
                f"DUPLICATE CSV DETECTED: {key} is byte-identical to {prev_key} "
                f"(md5={h}) — downloader saved the wrong file for {client}. "
                f"Aborting to prevent bad data from reaching QA / GSheet."
            )
    _CSV_HASHES[key] = h
    print(f"  CSV integrity: unique (md5={h[:8]}…)")

# Clients confirmed to have FY25-26 trade history.
# Everyone NOT in this set is assumed to have joined FY26-27 only.
FY2526_CLIENTS = {"RIMK1205", "RIMK1209", "RIMK1215", "RIMK1220", "RIMK1248"}

# Explicit skip map — belt-and-suspenders; auto-skip logic below also handles unknowns.
NO_HISTORY_FY: dict[str, set] = {
    "RIMK1238": {"2025-2026"},
    "RIMK1247": {"2025-2026"},
    "RIMK1249": {"2025-2026"},
    "RIMK1252": {"2025-2026"},
    "RIMK1256": {"2025-2026"},
    "RIMK1258": {"2025-2026"},
    "SHU9BH":   {"2025-2026"},
}

# Guard: any client not in FY2526_CLIENTS and not in NO_HISTORY_FY is a new
# addition that was never classified. Auto-skip FY25-26 for them (prevents 90s
# CBOS hangs) and print a loud warning so the operator can classify them.
_unclassified = [c for c in CLIENTS if c not in FY2526_CLIENTS and c not in NO_HISTORY_FY]
if _unclassified:
    print(f"\n⚠️  UNCLASSIFIED CLIENTS (auto-skipping FY25-26): {_unclassified}")
    print(f"   If any joined FY25-26, add them to FY2526_CLIENTS in mo_downloader.py")
    print(f"   Otherwise add them to NO_HISTORY_FY to silence this warning.\n")
    for _c in _unclassified:
        NO_HISTORY_FY.setdefault(_c, set()).add("2025-2026")
# ─────────────────────────────────────────────────────────────────────────────


def _tg_send(msg: str):
    """Send a Telegram message (best-effort, no raise on failure)."""
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        import urllib.request, urllib.parse
        data = urllib.parse.urlencode({'chat_id': TG_CHAT, 'text': msg}).encode()
        urllib.request.urlopen(
            f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            data=data, timeout=10
        )
    except Exception:
        pass


def _poll_otp_file(deadline: float):
    """Check OTP_FILE for a fresh 6-digit OTP written by the Telegram /otp command."""
    if not os.path.exists(OTP_FILE):
        return None
    try:
        mtime = os.path.getmtime(OTP_FILE)
        if mtime < deadline - 300:  # ignore stale files older than 5 min before deadline
            return None
        val = open(OTP_FILE).read().strip()
        if re.fullmatch(r'\d{6}', val):
            os.remove(OTP_FILE)  # consume it
            return val
    except Exception:
        pass
    return None


def get_otp_from_gmail(sent_after: float, max_wait=180) -> str:
    """Poll Gmail IMAP for OTP. Falls back to Telegram /otp command if IMAP fails."""
    print("  Waiting for OTP email...")
    deadline = time.time() + max_wait
    imap_failed = False
    tg_prompted = False
    tg_prompt_after = time.time() + 60  # send Telegram prompt after 60s if no fresh email found

    while time.time() < deadline:
        # 1. Check Telegram OTP file first (user may have already sent /otp)
        otp = _poll_otp_file(deadline)
        if otp:
            print(f"  OTP received via Telegram: {otp}")
            return otp

        # 2. Try Gmail IMAP
        if not imap_failed:
            mail = None
            try:
                mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=15)
                mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
                mail.select('"[Gmail]/All Mail"')

                today_imap = date.today().strftime("%d-%b-%Y")
                _, data = mail.search(None, f'SUBJECT "OTP For CBOS" SINCE {today_imap}')
                ids = data[0].split() if data[0] else []
                print(f"  Found {len(ids)} OTP email(s) today")

                best_otp, best_time = None, 0
                for uid in ids:
                    _, msg_data = mail.fetch(uid, "(RFC822)")
                    msg = email.message_from_bytes(msg_data[0][1])
                    try:
                        dt = email.utils.parsedate_to_datetime(msg.get("Date", ""))
                        email_ts = dt.astimezone(timezone.utc).timestamp()
                    except Exception:
                        email_ts = 0

                    print(f"  Email ts={email_ts:.0f} sent_after={sent_after:.0f} diff={(email_ts-sent_after):.0f}s")
                    if email_ts < sent_after - 90:
                        continue

                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() in ("text/plain", "text/html"):
                                candidate = part.get_payload(decode=True).decode(errors="ignore")
                                if len(candidate.strip()) > len(body.strip()):
                                    body = candidate
                    else:
                        body = msg.get_payload(decode=True).decode(errors="ignore")

                    m = re.search(r"\b(\d{6})\b", body)
                    if m and email_ts >= best_time:
                        best_otp, best_time = m.group(1), email_ts

                if best_otp:
                    print(f"  OTP attempt 1: {best_otp}")
                    return best_otp

            except Exception as e:
                print(f"  Gmail IMAP error: {e}")
                imap_failed = True
            finally:
                try:
                    if mail:
                        mail.logout()
                except Exception:
                    pass

        # 3. Prompt via Telegram if: IMAP failed OR 60s passed with no fresh email
        should_prompt = (imap_failed or time.time() > tg_prompt_after) and not tg_prompted
        if should_prompt:
            _tg_send(
                "⚠️ CBOS login needs OTP.\n"
                "Check your email for a 6-digit OTP from CBOS and reply:\n"
                "/otp 123456\n\n"
                "(Pipeline waits up to 3 min for your reply)"
            )
            tg_prompted = True
            reason = "IMAP unavailable" if imap_failed else "no fresh OTP after 60s"
            print(f"  Telegram prompt sent ({reason}) — waiting for /otp reply.")

        time.sleep(5)

    raise RuntimeError(f"OTP not received within {max_wait} seconds.")


async def login(page):
    print("Logging in...")

    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(2)

    await page.locator('input[type="text"], input:not([type="password"]):not([type="hidden"])').first.fill(MO_USERNAME)
    await page.locator('input[type="password"]').first.fill(MO_PASSWORD)

    await page.locator('button, input[type="submit"]').filter(has_text=re.compile(r'sign\s*in', re.I)).first.click()
    await asyncio.sleep(2)

    # Dismiss "session already active" popup if it appears
    # Reset login_time AFTER this click — this is when CBOS sends the OTP
    login_time = time.time()
    try:
        btn = page.locator('button:has-text("Login")').filter(has_not_text="Sign")
        if await btn.count() > 0:
            print("  Session conflict — clicking Login...")
            login_time = time.time()  # OTP triggered by THIS click, not Sign In
            await btn.first.click()
            await asyncio.sleep(1)
    except Exception:
        pass

    await page.wait_for_selector(
        'mat-dialog-container input, [role="dialog"] input, .cdk-overlay-container input',
        timeout=20000
    )

    OTP_INPUT  = 'mat-dialog-container input, [role="dialog"] input, .cdk-overlay-container input'
    RESEND_BTN = 'button:has-text("Resend"), a:has-text("Resend"), span:has-text("Resend")'

    for attempt in range(3):
        try:
            if attempt == 0:
                otp = get_otp_from_gmail(sent_after=login_time - 30, max_wait=180)
            else:
                # Click resend and wait for a fresh OTP
                print(f"  OTP attempt {attempt+1}: clicking Resend...")
                resend_time = time.time()
                try:
                    await page.locator(RESEND_BTN).first.click(timeout=5000)
                except Exception:
                    print("  No Resend button found")
                await asyncio.sleep(3)
                otp = get_otp_from_gmail(sent_after=resend_time, max_wait=180)
        except RuntimeError:
            if attempt < 2:
                print(f"  OTP not received on attempt {attempt+1}, trying Resend...")
                continue
            raise

        print(f"  OTP attempt {attempt+1}: {otp}")
        inp = page.locator(OTP_INPUT).first
        await inp.click()
        await inp.fill(otp)
        await asyncio.sleep(0.5)
        await page.click('button:has-text("Validate")')

        try:
            await page.wait_for_url("**/Home.aspx**", timeout=15000)
            await page.wait_for_load_state("domcontentloaded")
            print("  Logged in successfully.")
            return
        except PWTimeout:
            # Check for error message on page
            err_text = await page.locator(
                'mat-error, .error-message, [class*="error"], [class*="invalid"]'
            ).all_inner_texts()
            print(f"  OTP failed (attempt {attempt+1}): {err_text}")
            if attempt == 2:
                raise RuntimeError(f"Login failed after 3 OTP attempts. Last errors: {err_text}")


async def navigate_to_trade_details(page):
    """Navigate to the Trade Details And Summary page.
    CBOS now uses a <select> dropdown for navigation (changed from sidebar links in UI update).
    """
    await asyncio.sleep(2)  # let Angular render

    # Primary: find the nav <select> with "Trade Details And Summary" option and select it
    r = await page.evaluate("""
        () => {
            for (const sel of document.querySelectorAll('select')) {
                for (const opt of sel.options) {
                    if (opt.text.includes('Trade Detail') && opt.text.includes('Summar')) {
                        sel.value = opt.value;
                        sel.dispatchEvent(new Event('change', {bubbles: true}));
                        if (typeof $ !== 'undefined') $(sel).trigger('change');
                        return {selected: opt.text.trim(), value: opt.value.substring(0, 60)};
                    }
                }
            }
            return {notFound: true};
        }
    """)
    print(f"  Nav: {r}")
    await asyncio.sleep(3)

    if await page.locator('[name*="TradeDetailsSumry"], [id*="TradeDetailsSumry"]').count() > 0:
        print("  On Trade Details page.")
        return True

    return False


async def close_download_modal(page):
    """Close the Download History modal and remove backdrop."""
    await page.evaluate("""
        () => {
            const modal = document.getElementById('Commn_Download_Master');
            if (!modal) return;
            if (typeof $ !== 'undefined') { $(modal).modal('hide'); }
            else { modal.classList.remove('show'); modal.style.display = 'none'; }
            const backdrop = document.querySelector('.modal-backdrop');
            if (backdrop) backdrop.remove();
            document.body.classList.remove('modal-open');
            document.body.style.overflow = '';
        }
    """)
    await asyncio.sleep(0.5)


async def _download_from_row(page, row_idx, save_path: str):
    """Click the CBOS download link and save the resulting file.

    Uses Playwright's built-in download interception (accept_downloads=True on
    the browser context) — works regardless of whether secureDownloadChunked()
    uses blob URLs, window.location, iframes, or fetch-based chunking.

    Retries the link click up to 5 times without re-submitting the Download form,
    so no extra server-side files are generated on retry.
    """
    async def _accept_dialog(dialog):
        print(f"  [dialog auto-accept] {dialog.type}: {dialog.message[:80]}")
        await dialog.accept()
    page.on('dialog', _accept_dialog)

    sdc_error = [False]  # set by console listener when SDC throws

    def _on_console(msg):
        if msg.type in ('error', 'warning'):
            print(f"  [JS {msg.type}] {msg.text[:120]}")
        if msg.type == 'error' and ('[SDC] error' in msg.text or '500' in msg.text or 'Failed to load resource' in msg.text):
            sdc_error[0] = True

    def _on_response(resp):
        ct = resp.headers.get('content-type', '')
        cd = resp.headers.get('content-disposition', '')
        if any(k in ct+cd for k in ('csv', 'attachment', 'octet', 'download')):
            print(f"  [NET] {resp.status} {resp.url[:90]} | ct={ct[:40]} | cd={cd[:40]}")

    page.on('console', _on_console)
    page.on('response', _on_response)

    try:
        for _attempt in range(5):
            if _attempt > 0:
                print(f"  Download link retry {_attempt}/4 — re-clicking row {row_idx}...")
                await asyncio.sleep(5)

            sdc_error[0] = False  # reset for this attempt

            # Re-read link each attempt in case rows shifted
            link_html = await page.evaluate("""
                (idx) => {
                    const rows = document.querySelectorAll('#Commn_Download_Master tbody tr, .modal tbody tr');
                    const row = rows[idx != null ? idx : 0] || rows[0];
                    if (!row) return 'no row';
                    const el = row.querySelector('a, button');
                    return el ? el.outerHTML.substring(0, 150) : 'no link';
                }
            """, row_idx if row_idx is not None else 0)
            print(f"  Link HTML: {link_html}")

            dl_holder: list = [None]
            dl_event = asyncio.Event()

            def _on_download(dl, _h=dl_holder, _e=dl_event):
                _h[0] = dl
                _e.set()

            page.context.on('download', _on_download)
            try:
                # Wrap secureDownloadChunked to log progress; reset each attempt
                await page.evaluate("""
                    () => {
                        if (typeof secureDownloadChunked !== 'function') return;
                        const orig = window.__sdcOrig || secureDownloadChunked;
                        window.__sdcOrig = orig;
                        window.secureDownloadChunked = async function(fileId, filename) {
                            console.log('[SDC] called fileId=' + fileId + ' filename=' + filename);
                            try {
                                const result = await orig.apply(this, arguments);
                                console.log('[SDC] completed fileId=' + fileId);
                                return result;
                            } catch(e) {
                                console.error('[SDC] error fileId=' + fileId + ': ' + e);
                                throw e;
                            }
                        };
                    }
                """)
                await page.evaluate("""
                    (idx) => {
                        const rows = document.querySelectorAll('#Commn_Download_Master tbody tr, .modal tbody tr');
                        const row  = rows[idx != null ? idx : 0] || rows[0];
                        if (!row) return;
                        const link = row.querySelector('a, button');
                        if (link) link.click();
                    }
                """, row_idx if row_idx is not None else 0)

                # Wait up to 5 minutes; bail immediately on SDC error
                deadline = asyncio.get_event_loop().time() + 300
                while not dl_event.is_set():
                    if asyncio.get_event_loop().time() > deadline:
                        print(f"  Download timeout on attempt {_attempt+1}")
                        break
                    if sdc_error[0]:
                        print(f"  SDC error on attempt {_attempt+1} — retrying")
                        break
                    try:
                        await page.evaluate("() => fetch(window.location.href, {method:'HEAD'}).catch(()=>{})")
                    except Exception:
                        pass
                    try:
                        await asyncio.wait_for(dl_event.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        pass
            finally:
                page.context.remove_listener('download', _on_download)

            if dl_event.is_set():
                download = dl_holder[0]
                failure = await download.failure()
                if failure:
                    raise RuntimeError(f"Download stream failed: {failure}")
                if os.path.exists(save_path):
                    os.remove(save_path)
                await download.save_as(save_path)
                size = os.path.getsize(save_path)
                print(f"  Saved: {save_path} ({size} bytes)")
                return

        raise RuntimeError("Download failed after 5 attempts — secureDownloadChunked never triggered a save")
    finally:
        page.remove_listener('dialog', _accept_dialog)
        page.remove_listener('console', _on_console)
        page.remove_listener('response', _on_response)


# Shared JS helper — set a <select> value by text and trigger Select2/jQuery change.
_SET_SELECT_JS = """
    function setSelect(select, valueUpper) {
        let opt = Array.from(select.options).find(o => o.text.trim().toUpperCase() === valueUpper);
        if (!opt) opt = Array.from(select.options).find(o => o.text.trim().toUpperCase().includes(valueUpper));
        if (!opt) return null;
        select.value = opt.value;
        if (typeof $ !== 'undefined') $(select).trigger('change');
        else select.dispatchEvent(new Event('change', {bubbles: true}));
        return opt.text.trim();
    }
"""


async def download_client(page, client: str, download_dir: str, fy: str = "2026-2027", first: bool = False, used_filenames: set = None):
    print(f"\nProcessing {client} [{fy}]...")

    if first:
        if not await navigate_to_trade_details(page):
            raise RuntimeError("Could not navigate to Trade Details page")
    else:
        # JS-click Reset (Playwright click blocked by main-section overlay)
        await page.evaluate("""
            () => {
                for (const el of document.querySelectorAll('a, button')) {
                    if (el.textContent.trim() === 'Reset' &&
                        !el.closest('.top-bar, header, nav, .navbar')) { el.click(); return; }
                }
            }
        """)
        await asyncio.sleep(1.5)

    # Dismiss any leftover error popup
    await page.evaluate("""
        () => {
            for (const b of document.querySelectorAll('button')) {
                if (['Ok','OK','Close'].includes(b.textContent.trim())) { b.click(); return; }
            }
        }
    """)
    await asyncio.sleep(0.5)

    # ── Filter Search ──
    # If we're not on Trade Details page, navigate there first
    if await page.locator('[name*="TradeDetailsSumry"], [id*="TradeDetailsSumry"]').count() == 0:
        print("  Not on Trade Details page — navigating...")
        if not await navigate_to_trade_details(page):
            raise RuntimeError("Could not navigate to Trade Details page")

    # Try to find the client filter — CBOS UI has changed; try multiple approaches
    filter_found = False

    # Option A: Select2 trigger — try both old (-results) and new (-container) aria-controls values
    _filter_sel = None
    for _ac in ('select2-txtEDP_TradeDetailsSumry_FilterSearch-container',
                'select2-txtEDP_TradeDetailsSumry_FilterSearch-results'):
        _loc = f'[aria-controls="{_ac}"], [aria-owns="{_ac}"]'
        if await page.locator(_loc).count() > 0:
            _filter_sel = _loc
            break
    if _filter_sel:
        await page.locator(_filter_sel).click(timeout=5000)
        await asyncio.sleep(1)
        await page.locator('input.select2-search__field').fill(client)
        filter_found = True
        print(f"  Filter: Select2 ({_filter_sel[:60]})")

    # Option B: jQuery Select2 open on the underlying input (works even if trigger span ID changed)
    if not filter_found:
        r = await page.evaluate("""
            (inputId) => {
                if (typeof $ !== 'undefined' && $(('#' + inputId)).length) {
                    try {
                        $('#' + inputId).select2('open');
                        return 'select2-open';
                    } catch(e) { return 'select2-open-failed: ' + e; }
                }
                return 'jquery-not-found';
            }
        """, 'txtEDP_TradeDetailsSumry_FilterSearch')
        print(f"  Filter B: {r}")
        if 'select2-open' == r:
            await asyncio.sleep(1)
            sf = page.locator('input.select2-search__field')
            if await sf.count() > 0:
                await sf.fill(client)
                filter_found = True
                print("  Filter: jQuery select2('open')")

    # Option C: click any Select2 trigger span on the page, then type
    if not filter_found:
        spans = page.locator('span.select2-selection--single, span.select2-selection')
        n = await spans.count()
        for i in range(n):
            try:
                await spans.nth(i).click(timeout=3000)
                await asyncio.sleep(1)
                sf = page.locator('input.select2-search__field')
                if await sf.count() > 0:
                    await sf.fill(client)
                    filter_found = True
                    print(f"  Filter: Select2 span #{i}")
                    break
            except Exception:
                continue

    # Option D: direct plain input fill (if Select2 was replaced with plain input)
    if not filter_found:
        inp = page.locator('#txtEDP_TradeDetailsSumry_FilterSearch')
        if await inp.count() > 0:
            await inp.click()
            await asyncio.sleep(0.5)
            await inp.fill(client)
            filter_found = True
            print("  Filter: plain input fill")

    if not filter_found:
        debug = await page.evaluate("""
            () => {
                const els = [];
                for (const el of document.querySelectorAll('input, select, span[class*="select2"], [aria-controls], [aria-labelledby]')) {
                    const s = window.getComputedStyle(el);
                    if (s.display === 'none' || s.visibility === 'hidden') continue;
                    els.push({tag: el.tagName, id: el.id, cls: el.className.substring(0,50),
                              aria: el.getAttribute('aria-controls') || el.getAttribute('aria-labelledby') || ''});
                }
                return els.slice(0, 25);
            }
        """)
        print(f"  FILTER DEBUG (all visible inputs): {debug}")
        raise RuntimeError("Could not find client filter input")

    await asyncio.sleep(2)
    client_display = await page.evaluate("""
        (code) => {
            for (const li of document.querySelectorAll('li.select2-results__option')) {
                if (li.textContent.includes(code)) return li.textContent.trim();
            }
            return null;
        }
    """, client)
    if client_display:
        print(f"  Client display name: {client_display}")
    try:
        await page.locator(f'li.select2-results__option:has-text("{client}")').first.click(timeout=5000)
        print(f"  Client: selected from autocomplete")
    except PWTimeout:
        await page.keyboard.press("Enter")
        print(f"  Client: accepted via Enter")
    await asyncio.sleep(0.5)

    # ── Segment = EQ, Report Type = Detail (via underlying <select> + jQuery) ──
    # NOTE: date is set AFTER segment — setting segment via jQuery resets the date field
    r1 = await page.evaluate("""
        () => {
            """ + _SET_SELECT_JS + """
            const res = {};
            for (const s of document.querySelectorAll('select')) {
                const opts = Array.from(s.options).map(o => o.text.trim().toUpperCase());
                if (!res.seg && opts.includes('EQ'))           res.seg = setSelect(s, 'EQ');
                if (!res.rpt && opts.some(o => o === 'DETAIL')) res.rpt = setSelect(s, 'DETAIL');
            }
            return res;
        }
    """)
    print(f"  Segment={r1.get('seg')}, ReportType={r1.get('rpt')}")
    await asyncio.sleep(1)  # Exchange options load after Segment changes

    # ── Financial Year ──
    fy_set = await page.evaluate("""
        (fy) => {
            """ + _SET_SELECT_JS + """
            for (const s of document.querySelectorAll('select')) {
                const opts = Array.from(s.options).map(o => o.text.trim());
                if (opts.some(o => o.includes('-20'))) {
                    const set = setSelect(s, fy.toUpperCase());
                    if (set) return set;
                }
            }
            return null;
        }
    """, fy)
    print(f"  FY={fy_set}")

    # ── Exchange = ALL (multi-select checkbox — click "Select all") ──
    # Open the exchange multi-select dropdown
    exch_opened = await page.evaluate("""
        () => {
            const exchSel = Array.from(document.querySelectorAll('select'))
                                 .find(s => s.id && s.id.toLowerCase().includes('exchange'));
            if (exchSel) {
                // Find the Select2/custom wrapper and click it to open
                const wrapper = exchSel.closest('.select2, [class*="multiselect"], [class*="dropdown"]')
                             || exchSel.nextElementSibling
                             || exchSel.parentElement;
                if (wrapper) { wrapper.click(); return 'wrapper clicked'; }
            }
            return 'not found';
        }
    """)
    await asyncio.sleep(0.8)
    # Click "Select all" checkbox inside the opened dropdown
    r2 = await page.evaluate("""
        () => {
            for (const el of document.querySelectorAll('li, label, span, div, input')) {
                const txt = el.textContent.trim();
                const s = window.getComputedStyle(el);
                if (s.display === 'none' || s.visibility === 'hidden') continue;
                if (txt === 'Select all' || txt === 'Select All') {
                    el.click(); return 'clicked: ' + txt;
                }
            }
            return 'Select all not found';
        }
    """)
    print(f"  Exchange: {r2}")
    await asyncio.sleep(0.5)
    # Close the dropdown by clicking elsewhere
    await page.keyboard.press("Escape")
    await asyncio.sleep(0.3)

    # Verify Exchange count — must be ≥ 2 (NSE-EQ + BSE-EQ); retry once if not
    exch_count = await page.evaluate("""
        () => {
            const exchSel = Array.from(document.querySelectorAll('select'))
                                 .find(s => s.id && s.id.toLowerCase().includes('exchange'));
            if (exchSel) return Array.from(exchSel.selectedOptions).length;
            // Fallback: read the badge/count span shown by the multiselect widget
            const badge = document.querySelector('[class*="multiselect"] [class*="count"], [class*="multiselect"] [class*="badge"]');
            return badge ? parseInt(badge.textContent.trim()) : -1;
        }
    """)
    print(f"  Exchange selected count: {exch_count}")
    if exch_count != -1 and exch_count < 2:
        print("  WARNING: Exchange not fully selected — retrying Select all")
        await page.evaluate("""
            () => {
                const exchSel = Array.from(document.querySelectorAll('select'))
                                     .find(s => s.id && s.id.toLowerCase().includes('exchange'));
                if (exchSel) {
                    const wrapper = exchSel.closest('.select2, [class*="multiselect"], [class*="dropdown"]')
                                 || exchSel.nextElementSibling || exchSel.parentElement;
                    if (wrapper) wrapper.click();
                }
            }
        """)
        await asyncio.sleep(0.8)
        await page.evaluate("""
            () => {
                for (const el of document.querySelectorAll('li, label, span, div, input')) {
                    const txt = el.textContent.trim();
                    const s = window.getComputedStyle(el);
                    if (s.display === 'none' || s.visibility === 'hidden') continue;
                    if (txt === 'Select all' || txt === 'Select All') { el.click(); return; }
                }
            }
        """)
        await asyncio.sleep(0.5)
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.3)

    # ── Trade Date ──
    # After FY change, the form may auto-populate the date field.
    # If it already has a value, use it (covers historical FY where "Current Financial Year"
    # option disappears from the dropdown).
    # If still empty, explicitly click DATE_OPTION; if that option isn't in the DOM either,
    # fall back to clicking the FIRST visible option in the date dropdown.
    await asyncio.sleep(0.5)  # let Angular update date after FY change
    date_inp = page.locator('#txtEDP_Report_TradeDetailsSumry_fromdate')
    current_val = await date_inp.input_value()

    if len(current_val) > 5:
        print(f"  Date: using form auto-set '{current_val}'")
    else:
        date_set = False
        for attempt in range(3):
            await date_inp.click(force=True)
            await asyncio.sleep(1)
            # Try preferred DATE_OPTION first; fall back to first visible li in dropdown
            coords = await page.evaluate(r"""
                (option) => {
                    const visibleLi = [];
                    for (const el of document.querySelectorAll('li, span, div, a')) {
                        const s = window.getComputedStyle(el);
                        if (s.display === 'none' || s.visibility === 'hidden') continue;
                        const r = el.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) continue;
                        if (el.textContent.trim() === option) {
                            return {x: r.left + r.width/2, y: r.top + r.height/2,
                                    label: el.textContent.trim()};
                        }
                        visibleLi.push({el, r, txt: el.textContent.trim()});
                    }
                    // option not found — pick first visible <li> in a dropdown-like container
                    for (const {el, r, txt} of visibleLi) {
                        if (el.tagName === 'LI' && txt.match(/\d{2}\/\w+\/\d{4}/)) {
                            return {x: r.left + r.width/2, y: r.top + r.height/2, label: txt};
                        }
                    }
                    return null;
                }
            """, DATE_OPTION)
            if coords:
                print(f"  Date: clicking '{coords['label']}' at ({coords['x']:.0f},{coords['y']:.0f})")
                await page.mouse.click(coords['x'], coords['y'])
                await asyncio.sleep(0.5)
                val = await date_inp.input_value()
                print(f"  Date: value after click = '{val}'")
                if len(val) > 5:
                    date_set = True
                    break
                print(f"  Date attempt {attempt+1}: value didn't update, retrying...")
            else:
                print(f"  Date attempt {attempt+1}: no date option found in DOM")
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.5)
        if not date_set:
            raise RuntimeError(f"Could not set date to {DATE_OPTION}")

    # ── Click Download button (JS — bypasses main-section pointer intercept) ──
    await page.evaluate("""
        () => {
            const el = document.getElementById('btnEDP_Report_TradeDetailsumry_download');
            if (el) { el.click(); return; }
            for (const el of document.querySelectorAll('a, button')) {
                if (el.textContent.trim() === 'Download' && !el.classList.contains('switch-page')) {
                    el.click(); return;
                }
            }
        }
    """)
    print("  Download clicked — waiting for modal...")

    # Wait for a visible modal (error or Download History)
    await page.wait_for_function("""
        () => Array.from(document.querySelectorAll('.modal')).some(m => {
            const s = window.getComputedStyle(m);
            return s.display !== 'none' && s.visibility !== 'hidden' && (m.innerText||'').trim().length > 5;
        })
    """, timeout=8000)

    modal_txt = await page.evaluate("""
        () => {
            for (const m of document.querySelectorAll('.modal')) {
                const s = window.getComputedStyle(m);
                if (s.display === 'none' || s.visibility === 'hidden') continue;
                const txt = (m.innerText || '').trim();
                if (!txt) continue;
                // Dismiss validation errors
                if (/cannot be blank|invalid|please select/i.test(txt)) {
                    const btn = m.querySelector('button');
                    if (btn) btn.click();
                    return 'error: ' + txt.substring(0, 80);
                }
                return 'ok: ' + txt.substring(0, 40);
            }
            return 'no-modal';
        }
    """)
    print(f"  Modal: {modal_txt}")
    if modal_txt.startswith('error:'):
        raise RuntimeError(f"Validation — {modal_txt}")

    # ── Snapshot filenames already in the modal BEFORE our click ─────────────────
    # Only rows whose filename wasn't present pre-click belong to this client.
    # This fixes the "previous client's SUCCESS row" false-match when downloads
    # run close together (clients 1-2 min apart, 3-min window caught old rows).
    _pre_click_filenames = set()
    try:
        _pre_rows = await page.evaluate("""
            () => {
                const rows = document.querySelectorAll('#Commn_Download_Master tbody tr, .modal tbody tr');
                return Array.from(rows).map(r => {
                    const tds = r.querySelectorAll('td');
                    return tds.length ? tds[0].textContent.trim() : '';
                }).filter(Boolean);
            }
        """) or []
        _pre_click_filenames = set(_pre_rows)
    except Exception:
        pass
    _download_clicked_at = datetime.datetime.now()
    print(f"  Download clicked at: {_download_clicked_at.strftime('%H:%M:%S')} | pre-click rows: {len(_pre_click_filenames)}")

    def _parse_createdon(text):
        """Parse 'Aug  8 2026  8:27PM' → datetime, or None."""
        m = re.search(r'(\w{3})\s+(\d+)\s+(\d{4})\s+(\d+):(\d+)(AM|PM)', text)
        if not m:
            return None
        mon_str, day, year, hr, mn, ampm = m.groups()
        mon_map = {'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,'Jul':7,'Aug':8,
                   'Sep':9,'Oct':10,'Nov':11,'Dec':12}
        hr = int(hr)
        if ampm == 'PM' and hr != 12:
            hr += 12
        elif ampm == 'AM' and hr == 12:
            hr = 0
        try:
            return datetime.datetime(int(year), mon_map.get(mon_str, 1), int(day), hr, int(mn))
        except Exception:
            return None

    # ── Poll: find the fresh row for THIS client ──────────────────────────────
    print("  Polling for SUCCESS...")
    row_cells = None
    fresh_row_idx = None
    _cutoff = _download_clicked_at - datetime.timedelta(minutes=1)
    for _poll_i in range(90):  # 90×2s = 3 min max
        if _poll_i > 0 and _poll_i % 10 == 0:
            # Keepalive ping — use a relative path that doesn't require session tokens
            await page.evaluate("() => fetch(window.location.href, {method:'HEAD'}).catch(()=>{})")
        all_rows = await page.evaluate("""
            () => {
                const rows = document.querySelectorAll('#Commn_Download_Master tbody tr, .modal tbody tr');
                return Array.from(rows).map(r =>
                    Array.from(r.querySelectorAll('td')).map(td => td.textContent.trim())
                );
            }
        """) or []

        found = None
        found_idx = None
        for i, cells in enumerate(all_rows):
            status = next((c for c in cells if c in ('SUCCESS', 'FAILED', 'PENDING', 'PROCESSING', 'IN PROGRESS')), None)
            if not status:
                # check for multi-word statuses
                row_text = ' '.join(cells)
                if 'IN PROGRESS' in row_text:
                    status = 'IN PROGRESS'
                else:
                    continue
            # Skip filenames already used by a previous FY in this session
            filename_cell = cells[0] if cells else ''
            if used_filenames and filename_cell in used_filenames:
                continue
            # Skip rows that existed before we clicked Download — belongs to a prior client
            if filename_cell and filename_cell in _pre_click_filenames:
                continue
            # CREATEDON is the 3rd column (index 2)
            createdon_text = cells[2] if len(cells) > 2 else ''
            file_dt = _parse_createdon(createdon_text)
            if file_dt is None or file_dt < _cutoff:
                continue  # too old
            # This row was generated at or after our download click — it's fresh
            found, found_idx = cells, i
            break

        if found is not None:
            row_cells = found
            fresh_row_idx = found_idx
            status_cell = next((c for c in row_cells if c in ('SUCCESS', 'FAILED', 'PENDING', 'PROCESSING', 'IN PROGRESS')), None)
            if not status_cell and 'IN PROGRESS' in ' '.join(row_cells):
                status_cell = 'IN PROGRESS'
            print(f"  Row[{fresh_row_idx}] status={status_cell}: {row_cells}")
            if status_cell == 'SUCCESS':
                break
            if status_cell == 'FAILED':
                raise RuntimeError(f"Server reported FAILED for {client}")
            # PENDING / PROCESSING / IN PROGRESS — keep polling
        else:
            print(f"  Waiting for fresh row (cur={len(all_rows)} rows, cutoff={_cutoff.strftime('%H:%M')})...")

        # Refresh the modal table
        await page.evaluate("""
            () => {
                for (const b of document.querySelectorAll('button')) {
                    if (b.textContent.trim().toUpperCase() === 'REFRESH') { b.click(); return; }
                }
            }
        """)
        await asyncio.sleep(2)
    else:
        raise RuntimeError(f"Timed out waiting for SUCCESS — {client}")

    # Track this filename so subsequent FYs don't re-use it
    cbos_filename = row_cells[0] if row_cells else ''
    if used_filenames is not None and cbos_filename:
        used_filenames.add(cbos_filename)

    # ── Check NOOFROWS — skip if 0 ──
    noofrows = next(
        (int(c) for c in row_cells if re.fullmatch(r'\d+', c)),
        None
    )
    print(f"  NOOFROWS: {noofrows}")
    if noofrows == 0:
        print(f"  No trades for {client} — skipping.")
        await close_download_modal(page)
        return None

    # ── Trigger download and save via Playwright download interception ──
    fy_tag = fy.replace("-", "_")
    save_path = os.path.join(download_dir, f"TradeDetailsAndSummary_{client}_{fy_tag}.csv")
    try:
        await _download_from_row(page, fresh_row_idx, save_path)
    except PermissionError:
        ts = date.today().strftime("%Y%m%d")
        save_path = os.path.join(download_dir, f"TradeDetailsAndSummary_{client}_{fy_tag}_{ts}.csv")
        await _download_from_row(page, fresh_row_idx, save_path)
    finally:
        await close_download_modal(page)

    # Verify this CSV isn't a duplicate of another client's file
    _assert_csv_unique(client, fy, save_path)


async def scrape_ledger_balances(page, home_url: str) -> dict:
    """Scrape COMBINED+MTF Voucher Ledger balance for all clients.
    Reuses the existing CBOS session — call this after CSV downloads."""
    # Helpers inlined to avoid circular import with mo_ledger_scraper

    def _parse_indian(s):
        s = str(s).strip().replace(',', '').replace(' ', '')
        if not s or s in ('0.00', '0', '-', ''):
            return 0.0
        try:
            return float(s)
        except ValueError:
            return 0.0

    async def _click_segment(pg, seg):
        """Click the COMBINED or MTF row link on the Financial Summary page."""
        result = await pg.evaluate("""
            (seg) => {
                const allCells0 = [];
                for (const row of document.querySelectorAll('tr')) {
                    const cells = row.querySelectorAll('td');
                    if (!cells.length) continue;
                    const txt = cells[0].textContent.trim().toUpperCase();
                    allCells0.push(txt);
                    if (txt !== seg) continue;
                    const link = row.querySelector('a');
                    if (link) { link.click(); return {found: true, rows: allCells0}; }
                }
                return {found: false, rows: allCells0};
            }
        """, seg)
        if not result['found']:
            print(f"    WARNING: _click_segment({seg}) not found. Table rows seen: {result['rows'][:10]}")
        return result['found']

    async def _get_popup_balance(pg):
        """Read BALANCE column from any visible table — called after clicking COMBINED/MTF link."""
        await asyncio.sleep(2.5)
        val = await pg.evaluate("""
            () => {
                for (const tbl of document.querySelectorAll('table')) {
                    const s = window.getComputedStyle(tbl);
                    if (s.display === 'none' || s.visibility === 'hidden') continue;
                    let balIdx = -1, dateIdx = -1;
                    const ths = tbl.querySelectorAll('th');
                    if (ths.length) {
                        const hdrs = Array.from(ths).map(h => h.textContent.trim().toUpperCase());
                        balIdx = hdrs.indexOf('BALANCE');
                        dateIdx = hdrs.findIndex(h => h.includes('VOUCHER') && h.includes('DATE') || h === 'DATE');
                    }
                    if (balIdx < 0) {
                        const firstTr = tbl.querySelector('tr');
                        if (firstTr) {
                            const cells = Array.from(firstTr.querySelectorAll('th,td'))
                                               .map(c => c.textContent.trim().toUpperCase());
                            balIdx = cells.indexOf('BALANCE');
                            dateIdx = cells.findIndex(h => h.includes('VOUCHER') && h.includes('DATE') || h === 'DATE');
                        }
                    }
                    if (balIdx < 0) continue;
                    const rows = Array.from(tbl.querySelectorAll('tr'));
                    // Find the row with the most recent date (handles both asc and desc sort)
                    const MON = {jan:1,feb:2,mar:3,apr:4,may:5,jun:6,jul:7,aug:8,sep:9,oct:10,nov:11,dec:12};
                    function parseDate(s) {
                        s = s.trim();
                        // DD Mon YYYY  e.g. "19 Aug 2026"
                        const m1 = s.match(/^(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})$/);
                        if (m1) return parseInt(m1[3])*10000 + (MON[m1[2].toLowerCase()]||0)*100 + parseInt(m1[1]);
                        // DD-MM-YYYY or DD/MM/YYYY
                        const m2 = s.match(/^(\d{1,2})[-\/](\d{1,2})[-\/](\d{4})$/);
                        if (m2) return parseInt(m2[3])*10000 + parseInt(m2[2])*100 + parseInt(m2[1]);
                        return 0;
                    }
                    let bestScore = -1, bestVal = null;
                    for (let i = 1; i < rows.length; i++) {
                        const cells = rows[i].querySelectorAll('td');
                        if (cells.length <= balIdx) continue;
                        const bal = cells[balIdx].textContent.trim();
                        if (!bal) continue;
                        if (dateIdx >= 0 && cells.length > dateIdx) {
                            const score = parseDate(cells[dateIdx].textContent);
                            if (score > bestScore) { bestScore = score; bestVal = bal; }
                        } else {
                            bestVal = bal;
                        }
                    }
                    if (bestVal !== null) return bestVal;
                }
                return null;
            }
        """)
        return _parse_indian(val or '0')

    async def _close_popup(pg):
        """Close any open popup by clicking its close button, then pressing Escape."""
        await pg.evaluate("""
            () => {
                for (const b of document.querySelectorAll(
                        '.close, [data-dismiss="modal"], .modal-header .close')) {
                    if (window.getComputedStyle(b).display !== 'none') { b.click(); return; }
                }
            }
        """)
        await pg.keyboard.press("Escape")
        await asyncio.sleep(0.8)

    async def _nav_fin_summary(pg):
        found = await pg.evaluate("""
            () => {
                const kw = ['FINANCIAL SUMMARY', 'FIN SUMMARY'];
                for (const a of document.querySelectorAll('a, li, td')) {
                    const t = a.textContent.trim().toUpperCase().replace(/\\s+/g, ' ');
                    if (kw.some(k => t.includes(k))) { a.click(); return true; }
                }
                return false;
            }
        """)
        if not found:
            return False
        # Wait up to 8s for COMBINED/MTF row to appear (page renders async)
        for _ in range(8):
            await asyncio.sleep(1)
            ready = await pg.evaluate("""
                () => {
                    for (const row of document.querySelectorAll('tr')) {
                        const cells = row.querySelectorAll('td');
                        if (!cells.length) continue;
                        const txt = cells[0].textContent.trim().toUpperCase();
                        if (txt === 'COMBINED' || txt === 'MTF') return true;
                    }
                    return false;
                }
            """)
            if ready:
                break
        return True

    async def _dismiss_alert(pg):
        await pg.evaluate("""
            () => {
                for (const btn of document.querySelectorAll('button, .close')) {
                    if (window.getComputedStyle(btn).display === 'none') continue;
                    if (btn.textContent.trim() === '×' || btn.innerHTML.includes('×'))
                        { btn.click(); return; }
                }
            }
        """)
        await asyncio.sleep(0.5)

    ledger = {}
    print(f"\n{'='*40}\nScraping Ledger Balances\n{'='*40}")

    for client in CLIENTS:
        print(f"\n  {client}...")
        try:
            await asyncio.sleep(1)
            await _dismiss_alert(page)

            inp = page.locator('#txtClientCode')
            await inp.wait_for(state='visible', timeout=15000)
            await inp.click()
            await page.keyboard.press('Control+A')
            await inp.type(client, delay=80)
            await asyncio.sleep(1.5)
            await page.keyboard.press('ArrowDown')
            await asyncio.sleep(0.3)
            await page.keyboard.press('Enter')
            await asyncio.sleep(0.5)

            # Open dashboard in a popup tab so the original page stays at Home.aspx.
            # The popup shares the same browser context (cookies/session) so it is
            # fully authenticated; closing it leaves the main page untouched.
            popup = None
            async with page.context.expect_page() as popup_info:
                await page.locator('#btnView_ClientDashboard').click()
            popup = await popup_info.value
            await popup.wait_for_load_state('domcontentloaded', timeout=15000)
            await asyncio.sleep(2)

            pg = popup  # all dashboard operations use the popup

            fs_loaded = await _nav_fin_summary(pg)
            if not fs_loaded:
                await asyncio.sleep(2)
                fs_loaded = await _nav_fin_summary(pg)
            if not fs_loaded:
                print(f"    Financial Summary not found")
                ledger[client] = {'combined': 0.0, 'mtf': 0.0}
                if popup:
                    await popup.close()
                continue

            await asyncio.sleep(1)
            await _dismiss_alert(pg)

            combined_bal = 0.0
            if await _click_segment(pg, 'COMBINED'):
                combined_bal = await _get_popup_balance(pg)
                if combined_bal == 0.0:
                    await asyncio.sleep(2)
                    combined_bal = await _get_popup_balance(pg)
                print(f"    COMBINED = {combined_bal:,.2f}")
                await _close_popup(pg)
            else:
                print(f"    COMBINED row not found")

            await asyncio.sleep(0.5)

            mtf_bal = 0.0
            if await _click_segment(pg, 'MTF'):
                mtf_bal = await _get_popup_balance(pg)
                if mtf_bal == 0.0:
                    await asyncio.sleep(2)
                    mtf_bal = await _get_popup_balance(pg)
                print(f"    MTF      = {mtf_bal:,.2f}")
                await _close_popup(pg)
            else:
                print(f"    MTF row not found")

            await popup.close()
            popup = None

            ledger[client] = {'combined': combined_bal, 'mtf': mtf_bal}
            # Keepalive on the MAIN page (use current URL so session tokens are included)
            await page.evaluate("() => fetch(window.location.href, {method:'HEAD'}).catch(()=>{})")

        except Exception as e:
            print(f"    ERROR: {e}")
            ledger[client] = {'combined': 0.0, 'mtf': 0.0}
            if 'popup' in dir() and popup:
                try:
                    await popup.close()
                except Exception:
                    pass
                popup = None

    out_path = os.path.join(BASE, 'ledger.json')
    with open(out_path, 'w') as f:
        json.dump(ledger, f, indent=2)
    print(f"\nLedger saved: {out_path}")
    return ledger


async def main():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    if async_playwright is None:
        raise RuntimeError("playwright is not installed — run: pip install playwright && playwright install chromium")
    async with async_playwright() as p:
        headless = os.name != 'nt'  # headless on Linux VM, visible on Windows
        launch_args = ['--no-sandbox', '--disable-setuid-sandbox', '--no-zygote',
                       '--disable-dev-shm-usage', '--disable-gpu',
                       '--disable-software-rasterizer', '--ozone-platform=headless'] if os.name != 'nt' else []
        # Pass env explicitly so playwright's Node.js driver forwards it to chromium.
        # Keep LD_LIBRARY_PATH so stubs in /home/opc/lib are found (set by vm_daily_run.sh).
        # Stubs only exist for libs missing from system paths, so no shadowing of real libs.
        # Suppress ATK/AT-SPI initialisation so stub functions are never actually called.
        pw_env = dict(os.environ)
        pw_env['NO_AT_BRIDGE'] = '1'           # prevent ATK bridge init
        pw_env['DBUS_SESSION_BUS_ADDRESS'] = 'disabled:'  # no D-Bus → AT-SPI skipped
        pw_env['AT_SPI_BUS_ADDRESS'] = 'disabled:'
        pw_env['GTK_A11Y'] = 'none'            # GTK4: disable accessibility
        browser = await p.chromium.launch(headless=headless, args=launch_args, env=pw_env)
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()

        await login(page)
        # Scrape ledger first — session is freshest right after login
        await scrape_ledger_balances(page, HOME_URL)

        # Client-outer loop: finish all FYs for one client before moving to next.
        # Always navigate to the clean HOME_URL constant (never page.url which may
        # contain one-time CBOS tokens that redirect to Login.aspx on re-use).
        async def _ensure_session(pg):
            cur = pg.url
            if 'backoffice.motilaloswal.com' in cur and 'login' not in cur.lower():
                return  # Already on a live CBOS page — don't navigate (kills session tokens)
            await pg.goto(HOME_URL, wait_until='domcontentloaded', timeout=15000)
            await asyncio.sleep(1.5)
            if 'login.aspx' in pg.url.lower():
                print("  Session expired — re-logging in...")
                await login(pg)

        # Skip _ensure_session for the very first client — we just logged in and
        # are already on Home.aspx. Navigating to HOME_URL again right after login
        # strips CBOS session tokens from the URL and causes a redirect to Login.aspx.
        _skip_next_session_check = True
        _used_filenames: set = set()  # CBOS filenames already downloaded this session

        for client in CLIENTS:
            for fy in FINANCIAL_YEARS:
                if fy in NO_HISTORY_FY.get(client, set()):
                    print(f"  Skipping {client} [{fy}] — no history for this FY")
                    continue
                fy_safe = fy.replace('-', '_')
                local_path = os.path.join(DOWNLOAD_DIR, f"TradeDetailsAndSummary_{client}_{fy_safe}.csv")
                if os.path.exists(local_path) and os.path.getsize(local_path) > 500:
                    import datetime as _dt
                    mtime = os.path.getmtime(local_path)
                    mdate = _dt.date.fromtimestamp(mtime)
                    today = _dt.date.today()
                    if mdate == today:
                        print(f"  Already downloaded {client} [{fy}] today ({os.path.getsize(local_path)} bytes) — skipping")
                        continue
                    else:
                        print(f"  CSV for {client} [{fy}] is from {mdate} (stale) — re-downloading")
                try:
                    if _skip_next_session_check:
                        _skip_next_session_check = False
                    else:
                        await _ensure_session(page)
                    await download_client(page, client, DOWNLOAD_DIR, fy=fy, first=True, used_filenames=_used_filenames)
                except Exception as e:
                    _skip_next_session_check = False
                    print(f"  ERROR for {client} [{fy}]: {e}")
                    await close_download_modal(page)

                    # Retry 1: wait 60s (CBOS 500s are often transient rate-limits)
                    print(f"  Retrying {client} [{fy}] in 60s...")
                    await asyncio.sleep(60)
                    try:
                        await _ensure_session(page)
                        await download_client(page, client, DOWNLOAD_DIR, fy=fy, first=True, used_filenames=_used_filenames)
                        print(f"  Retry 1 succeeded for {client} [{fy}]")
                    except Exception as e2:
                        print(f"  Retry 1 failed for {client} [{fy}]: {e2}")
                        await close_download_modal(page)

                        # Retry 2: wait 3 minutes + fresh login
                        print(f"  Retrying {client} [{fy}] in 3 min with fresh login...")
                        await asyncio.sleep(180)
                        try:
                            await login(page)
                            await download_client(page, client, DOWNLOAD_DIR, fy=fy, first=True, used_filenames=_used_filenames)
                            print(f"  Retry 2 (fresh login) succeeded for {client} [{fy}]")
                        except Exception as e3:
                            print(f"  Retry 2 also failed for {client} [{fy}]: {e3}")
                            await close_download_modal(page)
                            _tg_send(
                                f"⚠️ CBOS download FAILED for {client} [{fy}] after 3 attempts.\n"
                                f"Error: {str(e3)[:100]}\n"
                                "Yesterday's data will be used for this client."
                            )

        await browser.close()

    print(f"\nDone. CSVs saved in: {DOWNLOAD_DIR}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        if 'TargetClosed' in type(e).__name__ or 'TargetClosed' in str(e):
            print(f"\nBrowser was closed (TargetClosed): {e}")
            raise  # re-raise so the caller sees a real failure
        else:
            raise
