"""
VM version of mo_downloader.py — headless Chromium, Linux paths.
Run: python3 vm_downloader.py
"""
import asyncio, imaplib, email, email.utils, re, os, time
from datetime import date, timezone
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

MO_USERNAME        = "RRISHANKMK"
MO_PASSWORD        = "Rishank@2012"
GMAIL_USER         = "jainrishank20@gmail.com"
GMAIL_APP_PASSWORD = "chos xzci zyso zzef"

CLIENTS = [
    "RIMK1205","RIMK1209","RIMK1215","RIMK1220","RIMK1238",
    "RIMK1247","RIMK1248","RIMK1249","RIMK1252","RIMK1256",
]

DOWNLOAD_DIR    = "/home/opc/client-tracker-mofsl/mo_csvs"
LOGIN_URL       = "https://backoffice.motilaloswal.com/Login.aspx"
DATE_OPTION     = "Current Financial Year"
FINANCIAL_YEARS = ["2025-2026", "2026-2027"]


def get_otp_from_gmail(sent_after: float, max_wait=120) -> str:
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
                if email_ts < sent_after - 30:
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
                if m and email_ts > best_time:
                    best_otp, best_time = m.group(1), email_ts
            mail.logout()
            if best_otp:
                return best_otp
        except Exception as e:
            print(f"  Gmail error: {e}")
        time.sleep(5)
    raise RuntimeError("OTP not received within timeout.")


async def login(page):
    print("Logging in...")
    await page.goto(LOGIN_URL, wait_until="networkidle")
    await asyncio.sleep(2)
    await page.locator('input[type="text"], input:not([type="password"]):not([type="hidden"])').first.fill(MO_USERNAME)
    await page.locator('input[type="password"]').first.fill(MO_PASSWORD)
    login_time = time.time()
    await page.locator('button, input[type="submit"]').filter(has_text=re.compile(r'sign\s*in', re.I)).first.click()
    await asyncio.sleep(2)
    try:
        btn = page.locator('button:has-text("Login")').filter(has_not_text="Sign")
        if await btn.count() > 0:
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
    print("  Logged in.")


async def navigate_to_trade_details(page):
    r = await page.evaluate("""
        () => {
            for (const a of document.querySelectorAll('a.switch-page, a[class*="switch-page"]')) {
                const txt = a.textContent.trim();
                if (txt.startsWith('Trade Details And S')) { a.click(); return {clicked: txt}; }
            }
            return {notFound: true};
        }
    """)
    print(f"  Nav: {r}")
    await asyncio.sleep(3)
    if await page.locator('[name*="TradeDetailsSumry"]').count() > 0:
        return True
    await page.evaluate("""
        () => {
            for (const a of document.querySelectorAll('td a, table a')) {
                if (a.textContent.trim().startsWith('Trade Details And S')) { a.click(); return; }
            }
        }
    """)
    await asyncio.sleep(3)
    return await page.locator('[name*="TradeDetailsSumry"]').count() > 0


async def close_download_modal(page):
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


async def download_client(page, client, download_dir, fy="2026-2027", first=False):
    print(f"\nProcessing {client} [{fy}]...")
    if first:
        if not await navigate_to_trade_details(page):
            raise RuntimeError("Could not navigate to Trade Details page")
    else:
        await page.evaluate("""
            () => {
                for (const el of document.querySelectorAll('a, button')) {
                    if (el.textContent.trim() === 'Reset' &&
                        !el.closest('.top-bar, header, nav, .navbar')) { el.click(); return; }
                }
            }
        """)
        await asyncio.sleep(1.5)

    await page.evaluate("""
        () => {
            for (const b of document.querySelectorAll('button')) {
                if (['Ok','OK','Close'].includes(b.textContent.trim())) { b.click(); return; }
            }
        }
    """)
    await asyncio.sleep(0.5)

    await page.locator('#DivTradeDetailsSumryFilterSearch .select2-selection').click()
    await asyncio.sleep(1)
    await page.locator('input.select2-search__field').fill(client)
    await asyncio.sleep(2)
    try:
        await page.locator(f'li.select2-results__option:has-text("{client}")').first.click(timeout=5000)
    except PWTimeout:
        await page.keyboard.press("Enter")
    await asyncio.sleep(0.5)

    r1 = await page.evaluate("""
        () => {
            """ + _SET_SELECT_JS + """
            const res = {};
            for (const s of document.querySelectorAll('select')) {
                const opts = Array.from(s.options).map(o => o.text.trim().toUpperCase());
                if (!res.seg && opts.includes('EQ'))            res.seg = setSelect(s, 'EQ');
                if (!res.rpt && opts.some(o => o === 'DETAIL')) res.rpt = setSelect(s, 'DETAIL');
            }
            return res;
        }
    """)
    print(f"  Seg={r1.get('seg')}, Rpt={r1.get('rpt')}")
    await asyncio.sleep(1)

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

    # Exchange = ALL
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
    r2 = await page.evaluate("""
        () => {
            for (const el of document.querySelectorAll('li, label, span, div, input')) {
                const txt = el.textContent.trim();
                const s = window.getComputedStyle(el);
                if (s.display === 'none' || s.visibility === 'hidden') continue;
                if (txt === 'Select all' || txt === 'Select All') { el.click(); return 'clicked: ' + txt; }
            }
            return 'Select all not found';
        }
    """)
    print(f"  Exchange: {r2}")
    await asyncio.sleep(0.5)
    await page.keyboard.press("Escape")
    await asyncio.sleep(0.3)

    # Date
    await asyncio.sleep(0.5)
    date_inp = page.locator('#txtEDP_Report_TradeDetailsSumry_fromdate')
    current_val = await date_inp.input_value()
    if len(current_val) > 5:
        print(f"  Date: auto-set '{current_val}'")
    else:
        date_set = False
        for attempt in range(3):
            await date_inp.click(force=True)
            await asyncio.sleep(1)
            coords = await page.evaluate("""
                (option) => {
                    for (const el of document.querySelectorAll('li, span, div, a')) {
                        const s = window.getComputedStyle(el);
                        if (s.display === 'none' || s.visibility === 'hidden') continue;
                        const r = el.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) continue;
                        if (el.textContent.trim() === option)
                            return {x: r.left + r.width/2, y: r.top + r.height/2, label: el.textContent.trim()};
                    }
                    for (const el of document.querySelectorAll('li')) {
                        const s = window.getComputedStyle(el);
                        if (s.display === 'none' || s.visibility === 'hidden') continue;
                        const r = el.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0 && el.textContent.trim().match(/\d{2}\/\w+\/\d{4}/))
                            return {x: r.left + r.width/2, y: r.top + r.height/2, label: el.textContent.trim()};
                    }
                    return null;
                }
            """, DATE_OPTION)
            if coords:
                await page.mouse.click(coords['x'], coords['y'])
                await asyncio.sleep(0.5)
                val = await date_inp.input_value()
                if len(val) > 5:
                    date_set = True
                    break
            else:
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.5)
        if not date_set:
            raise RuntimeError(f"Could not set date — {client} [{fy}]")

    # Download
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
    print("  Download clicked...")

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
    if modal_txt.startswith('error:'):
        raise RuntimeError(f"Validation — {modal_txt}")
    await asyncio.sleep(1)

    # Poll for SUCCESS
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
        status_cell = next((c for c in row_cells if c in ('SUCCESS','FAILED','PENDING','PROCESSING')), None)
        print(f"  Row: {row_cells}")
        if status_cell == 'SUCCESS':
            break
        if status_cell == 'FAILED':
            raise RuntimeError(f"Server FAILED — {client}")
        await page.evaluate("""
            () => {
                for (const b of document.querySelectorAll('button')) {
                    if (b.textContent.trim().toUpperCase() === 'REFRESH') { b.click(); return; }
                }
            }
        """)
        await asyncio.sleep(3)
    else:
        raise RuntimeError(f"Timed out — {client}")

    noofrows = next((int(c) for c in row_cells if re.fullmatch(r'\d+', c)), None)
    if noofrows == 0:
        print(f"  No trades for {client} — skipping.")
        await close_download_modal(page)
        return None

    fy_tag    = fy.replace("-", "_")
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
    if os.path.exists(save_path):
        os.remove(save_path)
    await dl.save_as(save_path)
    print(f"  Saved: {save_path}")
    await close_download_modal(page)


async def main():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await login(page)
        first = True
        for fy in FINANCIAL_YEARS:
            print(f"\n{'='*40}\nFY: {fy}\n{'='*40}")
            for client in CLIENTS:
                try:
                    await download_client(page, client, DOWNLOAD_DIR, fy=fy, first=first)
                    first = False
                except Exception as e:
                    print(f"  ERROR {client} [{fy}]: {e}")
                    await close_download_modal(page)
                    first = False
        await browser.close()
    print(f"\nDone. CSVs in: {DOWNLOAD_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
