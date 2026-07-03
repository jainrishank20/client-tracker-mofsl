"""
Scrape COMBINED + MTF Voucher Ledger balance from CBOS Financial Summary.
"""
import asyncio, os, json
from mo_downloader import login, CLIENTS

BASE     = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(BASE, 'ledger.json')
SS_DIR   = r"C:\Users\jainr\AppData\Local\Temp"


def parse_indian(s: str) -> float:
    s = str(s).strip().replace(',', '').replace(' ', '')
    if not s or s in ('0.00', '0', '-', ''):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


async def get_popup_balance(page) -> float:
    """Read BALANCE from the LAST data row of the popup table (current running balance)."""
    await asyncio.sleep(2.5)
    val = await page.evaluate("""
        () => {
            // Look for any visible table with a BALANCE column
            for (const tbl of document.querySelectorAll('table')) {
                const style = window.getComputedStyle(tbl);
                if (style.display === 'none' || style.visibility === 'hidden') continue;
                // Find BALANCE column index from headers
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
                // Read the FIRST data row (newest entry = current running balance)
                const rows = Array.from(tbl.querySelectorAll('tr'));
                for (let i = 1; i < rows.length; i++) {
                    const cells = rows[i].querySelectorAll('td');
                    if (cells.length > balIdx && cells[balIdx].textContent.trim()) {
                        return cells[balIdx].textContent.trim();
                    }
                }
            }
            return null;
        }
    """)
    return parse_indian(val or '0')


async def close_popup(page):
    await page.evaluate("""
        () => {
            // Try modal close buttons
            for (const b of document.querySelectorAll(
                    '.close, [data-dismiss="modal"], button.close, .modal-header .close')) {
                const s = window.getComputedStyle(b);
                if (s.display !== 'none' && s.visibility !== 'hidden') {
                    b.click(); return;
                }
            }
        }
    """)
    await page.keyboard.press("Escape")
    await asyncio.sleep(0.8)


async def click_segment_link(page, segment: str) -> bool:
    """Click the Voucher Ledger link for COMBINED or MTF row."""
    return await page.evaluate("""
        (seg) => {
            for (const row of document.querySelectorAll('tr')) {
                const cells = row.querySelectorAll('td');
                if (!cells.length) continue;
                if (cells[0].textContent.trim().toUpperCase() !== seg) continue;
                // Look for any anchor in the row
                const link = row.querySelector('a');
                if (link) { link.click(); return true; }
                // Fallback: click second cell
                if (cells[1]) { cells[1].click(); return true; }
            }
            return false;
        }
    """, segment)


async def navigate_to_financial_summary(page) -> bool:
    """Click the Financial Summary tab/link on the client dashboard."""
    found = await page.evaluate("""
        () => {
            const keywords = ['FINANCIAL SUMMARY', 'FINANCIALSUMMARY', 'FIN SUMMARY'];
            for (const a of document.querySelectorAll('a, li, td')) {
                const text = a.textContent.trim().toUpperCase().replace(/\\s+/g, ' ');
                if (keywords.some(k => text.includes(k))) {
                    a.click();
                    return true;
                }
            }
            return false;
        }
    """)
    if found:
        await asyncio.sleep(2)
    return found


async def go_home(page, home_url: str):
    """Return to Home.aspx — use back() if possible to preserve session."""
    current = page.url
    if 'Home.aspx' in current and 'pass=' in current:
        return  # already on home
    try:
        await page.go_back(wait_until='domcontentloaded', timeout=6000)
        await asyncio.sleep(1)
        if 'Home.aspx' in page.url and 'pass=' in page.url:
            return
    except Exception:
        pass
    # Last resort: navigate directly (may lose session on 2nd+ call)
    await page.goto(home_url, wait_until='domcontentloaded')
    await asyncio.sleep(1.5)


async def scrape_client_ledger(page, client: str, home_url: str) -> dict:
    """Navigate to client dashboard and scrape COMBINED + MTF balances."""
    print(f"\n  {client}...")

    await go_home(page, home_url)

    # Dismiss any popup/overlay before interacting
    await dismiss_any_popup(page)

    # Type client code — CBOS uses autocomplete, must select from dropdown
    inp = page.locator('#txtClientCode')
    await inp.wait_for(state='visible', timeout=10000)
    await inp.click()
    await asyncio.sleep(0.2)
    await page.keyboard.press('Control+A')
    await inp.type(client, delay=100)
    await asyncio.sleep(1.5)  # wait for autocomplete dropdown

    # Select first autocomplete suggestion (ArrowDown + Enter)
    await page.keyboard.press('ArrowDown')
    await asyncio.sleep(0.3)
    await page.keyboard.press('Enter')
    await asyncio.sleep(0.5)

    # Inspect the View Dashboard button before clicking
    btn_info = await page.evaluate("""
        () => {
            const b = document.querySelector('#btnView_ClientDashboard');
            if (!b) return null;
            return {
                href: b.href,
                target: b.target,
                onclick: b.getAttribute('onclick'),
                text: b.textContent.trim()
            };
        }
    """)
    print(f"    btn_info: {btn_info}")

    # Click View Dashboard — watch for new tab
    async with page.context.expect_page(timeout=6000) as new_page_info:
        await page.locator('#btnView_ClientDashboard').click()
    try:
        dash_page = await new_page_info.value
        await dash_page.wait_for_load_state('domcontentloaded', timeout=10000)
        print(f"    Opened new tab: {dash_page.url[-60:]}")
    except Exception:
        # No new tab — dashboard might load in current page
        dash_page = page
        await asyncio.sleep(4)
        print(f"    No new tab, using current page")

    current_url = dash_page.url
    print(f"    URL: {current_url[-60:]}")

    # Save screenshot to see dashboard state
    await dash_page.screenshot(path=os.path.join(SS_DIR, f'cbos_{client}_dashboard.png'))

    # Navigate to Financial Summary on the dashboard page
    fs_loaded = await navigate_to_financial_summary(dash_page)
    if not fs_loaded:
        print(f"    Financial Summary not found — check cbos_{client}_dashboard.png")
        if dash_page is not page:
            await dash_page.close()
        return {'combined': 0.0, 'mtf': 0.0}

    await asyncio.sleep(1)

    # Dismiss "New Alert" popup if it appears on dashboard
    await dash_page.evaluate("""
        () => {
            for (const btn of document.querySelectorAll('button, .close, [class*="close"]')) {
                const t = btn.textContent.trim();
                const s = window.getComputedStyle(btn);
                if (s.display === 'none') continue;
                if (t === '×' || t === 'Close' || btn.innerHTML.includes('×')) {
                    btn.click(); return;
                }
            }
        }
    """)
    await asyncio.sleep(0.5)

    # Scrape COMBINED
    combined_bal = 0.0
    if await click_segment_link(dash_page, 'COMBINED'):
        combined_bal = await get_popup_balance(dash_page)
        print(f"    COMBINED = {combined_bal:,.2f}")
        await close_popup(dash_page)
    else:
        print(f"    COMBINED link not found")

    await asyncio.sleep(0.5)

    # Scrape MTF
    mtf_bal = 0.0
    if await click_segment_link(dash_page, 'MTF'):
        mtf_bal = await get_popup_balance(dash_page)
        print(f"    MTF      = {mtf_bal:,.2f}")
        await close_popup(dash_page)
    else:
        print(f"    MTF      = 0 (not found)")

    # Close new tab if we opened one
    if dash_page is not page:
        await dash_page.close()

    return {'combined': combined_bal, 'mtf': mtf_bal}


async def dismiss_any_popup(page):
    """Dismiss any modal or alert that might be blocking the page."""
    await page.evaluate("""
        () => {
            // Close any visible modals
            for (const b of document.querySelectorAll(
                    'button.close, [data-dismiss="modal"], .modal-footer button, button:not([style])')) {
                const t = b.textContent.trim().toUpperCase();
                const s = window.getComputedStyle(b);
                if (s.display === 'none' || s.visibility === 'hidden') continue;
                if (t === 'OK' || t === 'CLOSE' || t === 'CANCEL' || t === '×') {
                    b.click();
                    return;
                }
            }
        }
    """)
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    await asyncio.sleep(0.5)


async def main():
    from playwright.async_api import async_playwright

    ledger = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()

        await login(page)
        await asyncio.sleep(3)

        home_url = page.url
        print(f"Home URL: {home_url[:80]}")

        # Dismiss any post-login popup
        await dismiss_any_popup(page)
        await asyncio.sleep(1)

        # Screenshot to verify state
        await page.screenshot(path=os.path.join(SS_DIR, 'cbos_home.png'))
        print("Screenshot: cbos_home.png")

        # Verify #txtClientCode is visible
        try:
            await page.wait_for_selector('#txtClientCode', timeout=10000, state='visible')
            print("  #txtClientCode found!")
        except Exception as e:
            print(f"  WARNING: #txtClientCode not found: {e}")
            # Take a screenshot to see what's blocking
            await page.screenshot(path=os.path.join(SS_DIR, 'cbos_blocked.png'))
            print("  Screenshot: cbos_blocked.png — check this to see what is on screen")

        for client in CLIENTS:
            try:
                data = await scrape_client_ledger(page, client, home_url)
                ledger[client] = data
            except Exception as e:
                print(f"    ERROR: {e}")
                ledger[client] = {'combined': 0.0, 'mtf': 0.0}

        await browser.close()

    with open(OUT_PATH, 'w') as f:
        json.dump(ledger, f, indent=2)
    print(f"\nSaved: {OUT_PATH}")
    print("\nLedger:")
    for c, v in ledger.items():
        print(f"  {c}: Combined={v['combined']:>15,.2f}   MTF={v['mtf']:>15,.2f}")


if __name__ == '__main__':
    asyncio.run(main())
