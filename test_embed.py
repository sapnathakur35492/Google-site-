from playwright.sync_api import sync_playwright
import os
import time

user_data_dir = os.path.abspath(os.path.join('google_session'))

with sync_playwright() as p:
    try:
        browser = p.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=False,
            slow_mo=0,
            args=['--disable-blink-features=AutomationControlled', '--start-maximized'],
            no_viewport=True
        )
        page = browser.pages[0]
        page.set_default_timeout(15000)
        page.goto('https://sites.google.com/u/0/create?template=blank&authuser=0', wait_until='domcontentloaded')
        page.wait_for_selector('body', timeout=15000)
        time.sleep(5)
        
        print('1. Clicking Embed...')
        embed_btn = page.locator('.RwbRsb, .d6wSYb, div[role="button"]').filter(has_text='Embed').first
        if not embed_btn.is_visible():
            embed_btn = page.get_by_text("Embed", exact=True).first
            
        embed_btn.click(force=True, timeout=5000)
        time.sleep(2)
        
        print('2. Switching to Embed code tab...')
        embed_tab = page.get_by_text("Embed code")
        embed_tab.click(force=True)
        time.sleep(1)
        
        print('3. Looking for textarea...')
        ta = page.locator('textarea').first
        ta.fill("<h2>Test Header</h2><p>This is a test HTML embed.</p>")
        time.sleep(1)
        
        print('4. Clicking Next...')
        next_btn = page.get_by_role("button", name="Next")
        if not next_btn.is_visible():
            next_btn = page.get_by_text("Next", exact=True).first
        next_btn.click(force=True)
        time.sleep(3) # Wait for preview rendering
        
        print('5. Clicking Insert...')
        insert_btn = page.get_by_role("button", name="Insert")
        if not insert_btn.is_visible():
            insert_btn = page.get_by_text("Insert", exact=True).first
        insert_btn.click(force=True)
        time.sleep(4) # Wait for widget to be placed on page
        
        print('SUCCESS: HTML Embed inserted successfully!')
        
        browser.close()
    except Exception as e:
        print(f'Error occurred: {e}')
        browser.close()
