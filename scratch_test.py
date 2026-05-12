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
        print('Navigating to Google Sites...')
        page.goto('https://sites.google.com/u/0/create?template=blank&authuser=0', wait_until='domcontentloaded')
        page.wait_for_selector('body', timeout=15000)
        time.sleep(5) # Wait longer
        
        print('Checking Title locator...')
        header_title = page.locator('div[role="textbox"]').filter(has_text='Your page title').first
        if header_title.is_visible():
            print('Title locator Found!')
        else:
            print('Title locator NOT Found. Trying alternatives...')
            header_title = page.get_by_text('Your page title').first
            if header_title.is_visible():
                print('Alternative Title locator Found!')
            else:
                print('Alternative Title locator ALSO NOT Found!')
                
        print('Checking Embed locator...')
        embed_btn = page.get_by_text('Embed', exact=True).first
        if embed_btn.is_visible():
            print('Embed button Found!')
        else:
            print('Embed button NOT Found!')
            embed_alt = page.locator('.d6wSYb').nth(2)
            if embed_alt.is_visible():
                print('Embed alt locator Found!')
        browser.close()
    except Exception as e:
        print(f'Error: {e}')
