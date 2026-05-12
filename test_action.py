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
        print('1. Navigating to Google Sites...')
        page.goto('https://sites.google.com/u/0/create?template=blank&authuser=0', wait_until='domcontentloaded')
        page.wait_for_selector('body', timeout=15000)
        time.sleep(5)
        
        print('2. Checking Title...')
        header_title = page.locator('div[role="textbox"]').filter(has_text='Your page title').first
        if not header_title.is_visible():
            header_title = page.get_by_text('Your page title').first
        print(f"Header visible: {header_title.is_visible()}")
        header_title.click()
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        page.keyboard.type("Test Title")
        print('Title injected.')
        
        print('3. Clicking Embed...')
        embed_btn = page.locator('.RwbRsb, .d6wSYb, div[role="button"]').filter(has_text='Embed').first
        if not embed_btn.is_visible():
            embed_btn = page.get_by_text("Embed", exact=True).first
            
        # Use Force click or Evaluate to bypass intercept
        try:
            embed_btn.click(timeout=5000)
        except:
            print("Normal click failed, trying force click...")
            embed_btn.click(force=True, timeout=5000)
        time.sleep(2)
        
        print('4. Switching to Embed code tab...')
        embed_tab = page.get_by_text("Embed code")
        embed_tab.click(force=True)
        time.sleep(1)
        
        print('5. Looking for textarea...')
        ta = page.locator('textarea').first
        ta.fill("<h1>Hello World</h1>")
        time.sleep(1)
        
        print('6. Clicking Next...')
        next_btn = page.get_by_role("button", name="Next")
        if not next_btn.is_visible():
            next_btn = page.get_by_text("Next", exact=True).first
        next_btn.click(force=True)
        time.sleep(2)
        
        print('7. Clicking Insert...')
        insert_btn = page.get_by_role("button", name="Insert")
        if not insert_btn.is_visible():
            insert_btn = page.get_by_text("Insert", exact=True).first
        insert_btn.click(force=True)
        time.sleep(2)
        
        print('SUCCESS: All locators worked!')
        browser.close()
    except Exception as e:
        print(f'Error occurred: {e}')
        try:
            page.screenshot(path="error_shot.png")
            print("Screenshot saved to error_shot.png")
        except: pass
        browser.close()
