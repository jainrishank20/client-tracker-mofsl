"""
Motilal Oswal CBOS - Automated Trade CSV Downloader

Usage:
    python mo_downloader.py           # current FY only (daily use)
    python mo_downloader.py --full    # both FYs (initial setup / new client)
"""
import asyncio, imaplib, email, email.utils, re, os, sys, time, glob, json
from datetime import date, timezone
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ── CONFIG ────────────────────────────────────────────────────────────────────
_cfg = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bot_config.json')))
MO_USERNAME        = _cfg['mo_username']
MO_PASSWORD        = _cfg['mo_password']
GMAIL_USER         = _cfg['gmail_user']
GMAIL_APP_PASSWORD = _cfg['gmail_app_password']

CLIENTS = [
    "RIMK1205","RIMK1209","RIMK1215","RIMK1220","RIMK1238",
    "RIMK1247","RIMK1248","RIMK1249","RIMK1252","RIMK1256",
]

DOWNLOAD_DIR = r"C:\Users\jainr\Downloads\MO_Trades"
LOGIN_URL    = "https://backoffice.motilaloswal.com/Login.aspx"
DATE_OPTION  = "Current Financial Year"

FULL_MODE       = "--full" in sys.argv
FINANCIAL_YEARS = ["2025-2026", "2026-2027"] if FULL_MODE else ["2026-2027"]
# ─────────────────────────────────────────────────────────────────────────────


def get_otp_from_gmail(sent_after: float, max_wait=120) -> str:
    """Poll Gmail for the latest OTP email received after sent_after."""
    print("  Waiting for OTP email...")
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            mail.select("inbox")

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
                if email_ts < sent_after - 30:  # 30s grace for clock drift only
                    continue

                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() in ("text/plain", "text/html"):
                            candidate = part.get_payload(decode=True).decode(errors="ignore")
                            if len(candidate.strip()) > len(body.strip()):
                                body = candidate  # keep longest non-empty part
                else:
                    body = msg.get_payload(decode=True).decode(errors="ignore")

                m = re.search(r"\b(\d{6})\b", body)
                if m and email_ts > best_time:
                    best_otp, best_time = m.group(1), email_ts

            mail.logout()
            if best_otp:
                return best_otp
        except Exception as e:
            print(f"  Gmail error: {e}")
        time.sleep(5)

    raise RuntimeError("OTP not received within 90 seconds.")


async def login(page):
    print("Logging in...")

    await page.goto(LOGIN_URL, wait_until="networkidle")
    await asyncio.sleep(2)

    await page.locator('input[type="text"], input:not([type="password"]):not([type="hidden"])').first.fill(MO_USERNAME)
    await page.locator('input[type="password"]').first.fill(MO_PASSWORD)

    login_time = time.time()  # capture JUST before Sign In click so OTP is definitely newer
    await page.locator('button, input[type="submit"]').filter(has_text=re.compile(r'sign\s*in', re.I)).first.click()
    await asyncio.sleep(2)

    # Dismiss "session already active" popup if it appears
    try:
        btn = page.locator('button:has-text("Login")').filter(has_not_text="Sign")
        if await btn.count() > 0:
            print("  Session conflict — clicking Login...")
            await btn.first.click()
            await asyncio.sleep(1)
    except Exception:
        pass

    await page.wait_for_selector(
        'mat-dialog-container input, [role="dialog"] input, .cdk-overlay-container input',
        timeout=20000
    )
    otp = get_otp_from_gmail(sent_after=login_time)
    print(f"  OTP received: {otp}")

    await page.locator(
        'mat-dialog-container input, [role="dialog"] input, .cdk-overlay-container input'
    ).first.fill(otp)
    await page.click('button:has-text("Validate")')

    await page.wait_for_url("**/Home.aspx**", timeout=30000)
    await page.wait_for_load_state("networkidle")
    print("  Logged in successfully.")


async def navigate_to_trade_details(page):
    """JS-click the Trade Details And Summary switch-page link (bypasses pointer intercept)."""
    r = await page.evaluate("""
        () => {
            for (const a of document.querySelectorAll('a.switch-page, a[class*="switch-page"]')) {
                const txt = a.textContent.trim();
                const bc  = a.getAttribute('data-breadcrumb') || '';
                const jsn = a.getAttribute('data-jsname') || '';
                if (txt.startsWith('Trade Details And S') ||
                    bc.includes('Trade Details') ||
                    jsn.toLowerCase().includes('tradedetail')) {
                    a.click();
                    return {clicked: txt.substring(0, 40), jsn};
                }
            }
            return {notFound: true};
        }
    """)
    print(f"  Nav: {r}")
    await asyncio.sleep(3)

    if await page.locator('[name*="TradeDetailsSumry"]').count() > 0:
        print("  On Trade Details page.")
        return True

    # Fallback: bookmark link in Home page table
    await page.evaluate("""
        () => {
            for (const a of document.querySelectorAll('td a, table a')) {
                if (a.textContent.trim().startsWith('Trade Details And S')) { a.click(); return; }
            }
        }
    """)
    await asyncio.sleep(3)

    if await page.locator('[name*="TradeDetailsSumry"]').count() > 0:
        print("  On Trade Details page (via bookmark).")
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


async def download_client(page, client: str, download_dir: str, fy: str = "2026-2027", first: bool = False):
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

    # ── Filter Search (Select2 AJAX autocomplete) ──
    await page.locator('#DivTradeDetailsSumryFilterSearch .select2-selection').click()
    await asyncio.sleep(1)
    await page.locator('input.select2-search__field').fill(client)
    await asyncio.sleep(2)
    # Capture the full display name from the autocomplete result before clicking
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
            coords = await page.evaluate("""
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
    await asyncio.sleep(1)

    # ── Poll the FIRST row (most recent = current client) until SUCCESS ──
    print("  Polling for SUCCESS...")
    for _ in range(30):
        row_cells = await page.evaluate("""
            () => {
                const rows = document.querySelectorAll('#Commn_Download_Master tbody tr, .modal tbody tr');
                if (!rows.length) return null;
                return Array.from(rows[0].querySelectorAll('td')).map(td => td.textContent.trim());
            }
        """)
        if not row_cells:
            await asyncio.sleep(3)
            continue
        status_cell = next((c for c in row_cells if c in ('SUCCESS', 'FAILED', 'PENDING', 'PROCESSING')), None)
        print(f"  Row: {row_cells}")
        if status_cell == 'SUCCESS':
            break
        if status_cell == 'FAILED':
            raise RuntimeError(f"Server reported FAILED for {client}")
        # Click Refresh
        await page.evaluate("""
            () => {
                for (const b of document.querySelectorAll('button')) {
                    if (b.textContent.trim().toUpperCase() === 'REFRESH') { b.click(); return; }
                }
            }
        """)
        await asyncio.sleep(3)
    else:
        raise RuntimeError(f"Timed out waiting for SUCCESS — {client}")

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

    # ── Click Download in the first row ──
    fy_tag = fy.replace("-", "_")
    save_path = os.path.join(download_dir, f"TradeDetailsAndSummary_{client}_{fy_tag}.csv")
    async with page.expect_download(timeout=60000) as dl_info:
        await page.evaluate("""
            () => {
                const rows = document.querySelectorAll('#Commn_Download_Master tbody tr, .modal tbody tr');
                if (rows.length) {
                    const link = rows[0].querySelector('a, button');
                    if (link) link.click();
                }
            }
        """)
    dl = await dl_info.value
    try:
        if os.path.exists(save_path):
            os.remove(save_path)
        await dl.save_as(save_path)
        print(f"  Saved: {save_path}")
    except PermissionError:
        # File is open in Excel — save with timestamp suffix
        ts = date.today().strftime("%Y%m%d")
        save_path = os.path.join(download_dir, f"TradeDetailsAndSummary_{client}_{fy_tag}_{ts}.csv")
        await dl.save_as(save_path)
        print(f"  Saved (Excel was open): {save_path}")
    finally:
        await close_download_modal(page)


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

    async def _get_popup_balance(pg):
        await asyncio.sleep(2.5)
        val = await pg.evaluate("""
            () => {
                for (const tbl of document.querySelectorAll('table')) {
                    const s = window.getComputedStyle(tbl);
                    if (s.display === 'none' || s.visibility === 'hidden') continue;
                    let balIdx = -1;
                    const ths = tbl.querySelectorAll('th');
                    if (ths.length) {
                        const hdrs = Array.from(ths).map(h => h.textContent.trim().toUpperCase());
                        balIdx = hdrs.indexOf('BALANCE');
                    }
                    if (balIdx < 0) {
                        const firstTr = tbl.querySelector('tr');
                        if (firstTr) {
                            const cells = Array.from(firstTr.querySelectorAll('th,td'))
                                               .map(c => c.textContent.trim().toUpperCase());
                            balIdx = cells.indexOf('BALANCE');
                        }
                    }
                    if (balIdx < 0) continue;
                    const rows = Array.from(tbl.querySelectorAll('tr'));
                    for (let i = 1; i < rows.length; i++) {
                        const cells = rows[i].querySelectorAll('td');
                        if (cells.length > balIdx && cells[balIdx].textContent.trim())
                            return cells[balIdx].textContent.trim();
                    }
                }
                return null;
            }
        """)
        return _parse_indian(val or '0')

    async def _close_popup(pg):
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

    async def _click_segment(pg, seg):
        return await pg.evaluate("""
            (seg) => {
                for (const row of document.querySelectorAll('tr')) {
                    const cells = row.querySelectorAll('td');
                    if (!cells.length || cells[0].textContent.trim().toUpperCase() !== seg) continue;
                    const link = row.querySelector('a');
                    if (link) { link.click(); return true; }
                    if (cells[1]) { cells[1].click(); return true; }
                }
                return false;
            }
        """, seg)

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
        if found:
            await asyncio.sleep(2)
        return found

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
            await page.goto(home_url, wait_until='domcontentloaded', timeout=20000)
            await asyncio.sleep(1.5)
            await _dismiss_alert(page)

            inp = page.locator('#txtClientCode')
            await inp.wait_for(state='visible', timeout=10000)
            await inp.click()
            await page.keyboard.press('Control+A')
            await inp.type(client, delay=80)
            await asyncio.sleep(1.5)
            await page.keyboard.press('ArrowDown')
            await asyncio.sleep(0.3)
            await page.keyboard.press('Enter')
            await asyncio.sleep(0.5)

            async with page.context.expect_page(timeout=6000) as new_page_info:
                await page.locator('#btnView_ClientDashboard').click()
            dash_page = await new_page_info.value
            await dash_page.wait_for_load_state('domcontentloaded', timeout=10000)
            await asyncio.sleep(2)

            fs_loaded = await _nav_fin_summary(dash_page)
            if not fs_loaded:
                print(f"    Financial Summary not found")
                await dash_page.close()
                ledger[client] = {'combined': 0.0, 'mtf': 0.0}
                continue

            await asyncio.sleep(1)
            await _dismiss_alert(dash_page)

            combined_bal = 0.0
            if await _click_segment(dash_page, 'COMBINED'):
                combined_bal = await _get_popup_balance(dash_page)
                print(f"    COMBINED = {combined_bal:,.2f}")
                await _close_popup(dash_page)
            else:
                print(f"    COMBINED not found")

            await asyncio.sleep(0.5)

            mtf_bal = 0.0
            if await _click_segment(dash_page, 'MTF'):
                mtf_bal = await _get_popup_balance(dash_page)
                print(f"    MTF      = {mtf_bal:,.2f}")
                await _close_popup(dash_page)
            else:
                print(f"    MTF      = 0")

            await dash_page.close()
            ledger[client] = {'combined': combined_bal, 'mtf': mtf_bal}

        except Exception as e:
            print(f"    ERROR: {e}")
            ledger[client] = {'combined': 0.0, 'mtf': 0.0}

    out_path = os.path.join(BASE, 'ledger.json')
    with open(out_path, 'w') as f:
        json.dump(ledger, f, indent=2)
    print(f"\nLedger saved: {out_path}")
    return ledger


async def main():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()

        await login(page)
        home_url = page.url

        first = True
        for fy in FINANCIAL_YEARS:
            print(f"\n{'='*40}\nFinancial Year: {fy}\n{'='*40}")
            for client in CLIENTS:
                try:
                    await download_client(page, client, DOWNLOAD_DIR, fy=fy, first=first)
                    first = False
                except Exception as e:
                    print(f"  ERROR for {client} [{fy}]: {e}")
                    await close_download_modal(page)
                    first = False

        # Scrape ledger in the same session — no second login needed
        await scrape_ledger_balances(page, home_url)

        await browser.close()

    print(f"\nDone. CSVs saved in: {DOWNLOAD_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
