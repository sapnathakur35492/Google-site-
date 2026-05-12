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
        
        print('Adding text box...')
        # Click Text Box on the right sidebar
        text_box_btn = page.locator('div[role="button"]').filter(has_text='Text box').first
        if not text_box_btn.is_visible():
            text_box_btn = page.get_by_text("Text box").first
        text_box_btn.click(force=True)
        time.sleep(2)
        
        print('Focusing iframe body...')
        page.wait_for_selector('iframe', timeout=15000)
        
        # Google sites might have multiple iframes. We need the editor one.
        # Let's try the first iframe body
        frame = page.frame_locator('iframe').first
        frame.locator('body').click(force=True)
        
        print('Filling content...')
        frame.locator('body').fill("<h1>Hello World Content</h1>")
        time.sleep(2)
        
        inner = frame.locator('body').inner_text()
        print(f"Verified content: {inner}")
        
        browser.close()
    except Exception as e:
        print(f'Error: {e}')
        browser.close()
