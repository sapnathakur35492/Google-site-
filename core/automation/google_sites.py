import html as html_module
import logging
import math
import os
import random
import re
import time
import traceback
from functools import wraps
from urllib.parse import urlparse

from django.utils.text import slugify

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from ..models import SiteBatch, SiteEntry

os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

TEASER_LINE = "\U0001f4ca\U0001f4e9 Access Comprehensive Industry Insights"

# ---------------------------------------------------------------------------
#  Configuration constants
# ---------------------------------------------------------------------------
MAX_RETRIES_PER_ENTRY = 2
STEP_TIMEOUT = 15000          # ms – per-element interaction
DIALOG_TIMEOUT = 12000
URL_POLL_SECS = 20.0          # total time to poll DOM for published URL
# Settle times (reduced for hyper-speed)
PAGE_LOAD_SETTLE_SECS = 0.5
INTER_ENTRY_PAUSE = 0.5
PUBLISH_SETTLE_SECS = 0.5
SHORT_TIMEOUT = 5000

SCREENSHOTS_DIR = os.path.abspath(os.path.join(os.getcwd(), "debug_screenshots"))
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

# Enforce Public visibility for phone/external access
SKIP_SHARE_DIALOG = False


# ===================================================================
#  Utility helpers
# ===================================================================

def _normalize_nan(s):
    if s is None:
        return ""
    t = str(s).strip()
    if t.lower() == "nan":
        return ""
    return t


def _retry(max_attempts=3, delay=1.0, label="operation"):
    """Decorator: retry a callable up to *max_attempts* times."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            last_err = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:
                    last_err = exc
                    logger.warning("[%s] attempt %s/%s failed: %s", label, attempt, max_attempts, exc)
                    if attempt < max_attempts:
                        time.sleep(delay * attempt)
            raise last_err
        return wrapper
    return decorator


def _safe_screenshot(page, name="error"):
    """Take a debug screenshot; never raise."""
    try:
        if page and not page.is_closed():
            path = os.path.join(SCREENSHOTS_DIR, f"{name}_{int(time.time())}.png")
            page.screenshot(path=path, full_page=False)
            logger.info("Screenshot saved: %s", path)
    except Exception:
        pass


def _smart_wait(page, timeout_ms=8000):
    """Wait for network-idle, but never block the pipeline."""
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        pass


def _ensure_logged_into_sites(page):
    """
    Stays open and WAITS indefinitely if Google asks for login. 
    Does not close chrome until user manually signs in.
    """
    logger.info("Verifying Google session (Manual Login Mode)...")
    
    # Increase timeout for manual login phase
    page.set_default_timeout(0) 
    
    while True:
        try:
            u = page.url or ""
            
            # 1. If we are already on a Google Sites page, we are good to go!
            if "sites.google.com" in u and ("create" in u or "home" in u or "d/" in u):
                logger.info("Login confirmed! Proceeding with automation...")
                # Reset to standard timeout (45 seconds)
                page.set_default_timeout(45000)
                return

            # 2. If we are on a login/challenge page, just wait and log
            if "accounts.google.com" in u:
                logger.warning("GOOGLE LOGIN/VERIFICATION REQUIRED: Chrome will stay open. Please complete login manually in the browser window.")
                time.sleep(5.0)
                continue

            # 3. If we are lost or on about:blank, try to go to Google Sites
            logger.info("Navigating to Google Sites to trigger login check...")
            try:
                page.goto("https://sites.google.com/u/0/create?template=blank&authuser=0", timeout=0)
            except Exception as nav_err:
                logger.debug("Navigation sync issue (expected during redirect): %s", nav_err)
            
            time.sleep(3.0)

        except Exception as e:
            logger.warning("Waiting for manual login... (Do not close Chrome): %s", e)
            time.sleep(5.0)


def _grant_clipboard(page):
    """Allow reading clipboard after Google’s “Copy link” actions."""
    try:
        ctx = page.context
        ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://sites.google.com")
    except Exception as exc:
        logger.debug("clipboard permission grant skipped: %s", exc)


def _click_aristotle_theme_js(page) -> bool:
    """
    Google Sites theme picker DOM varies; use text-based search + scrollIntoView.
    Returns True if a click was dispatched.
    """
    return page.evaluate(
        """() => {
      const needles = ['aristotle', 'Aristotle'];
      const candidates = Array.from(
        document.querySelectorAll('[role="listitem"], [role="option"], button, div[tabindex], span')
      );
      for (const el of candidates) {
        const t = (el.innerText || '').trim();
        const a = (el.getAttribute('aria-label') || '').trim();
        if (needles.some(n => t === n || t.startsWith(n + ' ') || a.toLowerCase().includes('aristotle'))) {
          try {
            el.scrollIntoView({ block: 'center', inline: 'center' });
          } catch (e) {}
          el.click();
          return true;
        }
      }
      return false;
    }"""
    )


def _looks_like_valid_published_url(url: str) -> bool:
    """
    Reject workspace roots like https://sites.google.com/redorangetechnologies.com (404).
    Require a real site path: /view/slug/... or /a/domain/slug/...
    """
    if not url or "sites.google.com" not in url:
        return False
    u = url.strip().split("?")[0].rstrip("/")
    try:
        p = urlparse(u)
    except Exception:
        return False
    if (p.scheme or "").lower() not in ("http", "https"):
        return False
    parts = [seg for seg in (p.path or "").split("/") if seg]
    if len(parts) < 2:
        return False
    # Single segment that looks like a bare domain name → not a published page URL
    if len(parts) == 1 and "." in parts[0] and parts[0] not in ("view", "new", "u"):
        return False
    if parts[0] == "view":
        return len(parts) >= 2
    if parts[0] == "a":
        return len(parts) >= 3
    if parts[0] in ("d", "u", "new") or "/edit" in u:
        return False
    return len(parts) >= 2


def _extract_published_url_from_dom(page) -> str:
    """
    Harvest published URLs; prefer /view/ links. Caller validates with _looks_like_valid_published_url.
    """
    raw = page.evaluate(
        """() => {
      const bad = (u) =>
        !u ||
        u.includes('/edit') ||
        u.includes('/d/') ||
        u.includes('create?') ||
        u.includes('sites.google.com/new');
      const found = [];
      const push = (u) => {
        if (typeof u !== 'string') return;
        let s = u.trim();
        const m = s.match(/https:\\/\\/sites\\.google\\.com[^\\s"'<>]+/i);
        if (m) s = m[0];
        if (s.includes('sites.google.com') && !bad(s)) found.push(s.split('&')[0]);
      };
      document.querySelectorAll('a[href*="sites.google.com"]').forEach((a) => push(a.href));
      document.querySelectorAll('input, textarea').forEach((el) => push(el.value || ''));
      const re = /https:\\/\\/sites\\.google\\.com[^\\s"'<>]+/gi;
      const txt = document.body ? (document.body.innerText || '') : '';
      let mm;
      while ((mm = re.exec(txt))) push(mm[0]);
      const uniq = [...new Set(found)];
      const pref = uniq.filter((u) => u.includes('/view/'));
      const ordered = pref.length ? pref : uniq;
      return ordered;
    }"""
    )
    if not raw:
        return ""
    for cand in raw:
        c = str(cand).split("?")[0].rstrip("/")
        if _looks_like_valid_published_url(c):
            return c
    return ""


def _clipboard_text_async(page) -> str:
    """Read system clipboard (HTTPS + permission)."""
    try:
        txt = page.evaluate(
            """async () => {
      try {
        return await navigator.clipboard.readText();
      } catch (e) {
        return '';
      }
    }"""
        )
        return (txt or "").strip()
    except Exception:
        return ""


def _verify_public_page_loads(page, url: str, expected_text: str = "") -> bool:
    """
    Verify the URL is live and contains our brand name via background request.
    This ensures we don't capture someone else's site or a 404.
    """
    if not url or "sites.google.com" not in url:
        return False
    try:
        response = page.request.get(url, timeout=12000)
        if response.status == 200:
            if "accounts.google.com" in response.url:
                return False
            text = response.text().lower()
            if "404. that's an error" in text or "page not found" in text:
                return False
            # Dynamic check: verify our brand is actually on this page
            if expected_text and expected_text.lower() not in text:
                return False
            return True
        return False
    except Exception:
        return False


def _dismiss_chrome(page, rounds=3):
    """Press Escape several times to close any popups / tooltips."""
    for _ in range(rounds):
        try:
            page.keyboard.press("Escape")
            time.sleep(0.08)
        except Exception:
            break

def _click_first_visible(page, selectors, timeout=5000, force=True):
    """Try a list of selectors; click the first visible one. Returns True on success."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible(timeout=min(timeout, 3000)):
                loc.click(force=force, timeout=timeout)
                return True
        except Exception:
            continue
    return False


# ===================================================================
#  Content builders
# ===================================================================

def site_name_short(entry: SiteEntry) -> str:
    """First 3 words for the top-left 'Site Name' in the header."""
    kw = _normalize_nan(entry.keyword) or _normalize_nan(entry.title) or "Market Insight"
    words = kw.split()
    return " ".join(words[:3]).strip()


def branding_title(entry: SiteEntry) -> str:
    """Full Title for the big Banner / Hero section."""
    ttl = _normalize_nan(entry.title)
    if ttl:
        # Prioritize full Title for the banner
        return " ".join(ttl.split()).strip()
    kw = _normalize_nan(entry.keyword)
    if kw:
        return kw.strip()
    return "Market Insight"


def build_premium_embed_html(entry: SiteEntry) -> str:
    """Full HTML widget: teaser + styled H1 + raw XLSX content (already HTML)."""
    headline = html_module.escape(branding_title(entry))
    body_html = entry.content or ""
    if isinstance(body_html, float):
        body_html = ""
    body_html = str(body_html)

    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '</head>'
        '<body style="margin:0;padding:0;overflow:visible;">'
        '<div style="font-family:\'Segoe UI\',Roboto,Arial,sans-serif;color:#212121;'
        'line-height:1.65;font-size:16px;max-width:960px;margin:0 auto;padding:12px 24px 120px;box-sizing:border-box;width:100%;height:auto;overflow:visible;">'
        f'<div style="font-size:1.05rem;margin-bottom:12px;color:#374151;">{TEASER_LINE}</div>'
        f'<h1 style="font-weight:700;font-size:2.1rem;line-height:1.3;margin:0 0 24px;color:#1a237e;text-align:center;word-wrap:break-word;">{headline}</h1>'
        f'<div id="embedded-main">{body_html}</div>'
        '</div>'
        '<style>'
        '#embedded-main{overflow:visible!important;max-width:100%;height:auto!important;min-height:auto!important}'
        '#embedded-main *{overflow:visible!important;max-width:100%;height:auto!important}'
        '#embedded-main h2{font-size:1.38rem;margin:1.45em 0 .55em;color:#1967d2;font-weight:600;border-bottom:1px solid #dadce0;padding-bottom:6px;overflow:visible!important}'
        '#embedded-main h3{font-size:1.12rem;margin:1.1em 0 .42em;color:#333;font-weight:600;overflow:visible!important}'
        '#embedded-main p{margin:.72em 0;overflow:visible!important;max-width:100%;word-wrap:break-word}'
        '#embedded-main ul,#embedded-main ol{margin:.72em 0;padding-left:1.45em;overflow:visible!important}'
        '#embedded-main a{color:#1967d2!important;text-decoration:underline;cursor:pointer;pointer-events:auto;display:inline-block}'
        '#embedded-main a:hover{color:#1557b0!important;text-decoration:underline}'
        '#embedded-main div{overflow:visible!important;max-width:100%;height:auto!important}'
        '#embedded-main span{max-width:100%}'
        '#embedded-main strong,#embedded-main b{max-width:100%}'
        '#embedded-main em,#embedded-main i{max-width:100%}'
        'body,html{overflow-x:hidden!important;overflow-y:visible!important;height:auto!important}'
        '/* Hide scrollbars across all browsers but keep content scrollable by mouse if needed */'
        'body::-webkit-scrollbar, *::-webkit-scrollbar { display: none !important; }'
        'body, * { -ms-overflow-style: none !important; scrollbar-width: none !important; }'
        '</style></body></html>'
    )


def _html_text_len_approx(html_blob: str) -> int:
    text = re.sub(r"<[^>]+>", " ", html_blob or "")
    return max(1, len(re.sub(r"\s+", " ", text).strip()))


def resize_steps_for_embed(html_blob: str) -> int:
    chars = len(html_blob or "")
    # Aggressive scaling: ~3 steps per char + 3500 buffer
    steps = int(chars * 3.1) + 3500
    return max(3500, min(85000, steps))


# ===================================================================
#  Individual automation steps (each independently retryable)
# ===================================================================

def _fill_textarea_verified(page, textarea_locator, full_html: str):
    """Ensure 100% of embed HTML is injected — with triple-fallback."""
    textarea_locator.click(timeout=STEP_TIMEOUT)
    time.sleep(0.15)

    # Strategy 1: Playwright fill (fastest)
    textarea_locator.fill(full_html)
    time.sleep(0.3)
    got = textarea_locator.input_value()
    if len(got) == len(full_html):
        return

    # Strategy 2: JS evaluate
    logger.warning("fill mismatch (%s vs %s), trying JS evaluate", len(got), len(full_html))
    textarea_locator.evaluate(
        "(el, val) => { el.value = val; el.dispatchEvent(new Event('input', { bubbles: true })); }",
        full_html,
    )
    time.sleep(0.4)
    got2 = textarea_locator.input_value()
    if len(got2) == len(full_html):
        return

    # Strategy 3: Super-Robust Chunked Injection (for massive 15k+ content)
    logger.info("Attempting Super-Robust Chunked Injection (Total length: %s chars)...", len(full_html))
    try:
        # Clear first
        textarea_locator.click(timeout=8000)
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        time.sleep(0.5)

        # Inject in 5000-char chunks to bypass browser clipboard limits
        chunk_size = 5000
        for i in range(0, len(full_html), chunk_size):
            chunk = full_html[i : i + chunk_size]
            page.evaluate("(t) => { navigator.clipboard.writeText(t); }", chunk)
            time.sleep(0.3)
            page.keyboard.press("Control+V")
            time.sleep(0.4) # Small pause between chunks
        
        # Settle
        time.sleep(1.5)
        
        got_val = textarea_locator.input_value()
        # Verify both length and that the end of the content matches
        if len(got_val) >= len(full_html) * 0.99 or full_html[-50:] in got_val:
            logger.info("Content verified: Super-Robust Injection SUCCESS (%s chars)", len(got_val))
            return
        else:
             logger.warning("Chunked injection verification failed: %s/%s chars.", len(got_val), len(full_html))
    except Exception as e:
        logger.warning("Chunked injection failed: %s", e)

    # Strategy 4: Final fallback - Direct JS Value Set
    logger.warning("Pasting failed verification, trying direct JS value set...")
    textarea_locator.evaluate("(el, val) => { el.value = val; el.dispatchEvent(new Event('input', { bubbles: true })); }", full_html)
    time.sleep(1.0)
    
    final_got = textarea_locator.input_value()
    if len(final_got) < len(full_html) * 0.9:
        raise RuntimeError(f"FATAL: Could not achieve 100% content load. Expected {len(full_html)}, got {len(final_got)}")


def _click_banner_placeholder_js(page) -> bool:
    """Find banner/canvas title field — handles 'Click to edit text' (new UI) and legacy placeholders."""
    return page.evaluate(
        """() => {
      const matchPlaceholder = (raw) => {
        const t = (raw || '').trim().toLowerCase();
        if (!t) return false;
        return (
          t.includes('your page title') ||
          t.includes('click to edit text') ||
          t === 'click to edit' ||
          t.includes('click to edit heading')
        );
      };
      const boxes = Array.from(
        document.querySelectorAll('[role="textbox"], [contenteditable="true"]')
      );
      for (const el of boxes) {
        const raw = el.innerText || el.textContent || '';
        if (matchPlaceholder(raw)) {
          try {
            el.scrollIntoView({ block: 'center', inline: 'center' });
          } catch (e) {}
          el.focus();
          el.click();
          return true;
        }
      }
      const main =
        document.querySelector('[role="main"]') ||
        document.querySelector('main') ||
        document.body;
      const mr = main.getBoundingClientRect();
      let best = null;
      let bestArea = 0;
      for (const el of boxes) {
        const br = el.getBoundingClientRect();
        if (br.top > mr.top + mr.height * 0.5) continue;
        const area = br.width * br.height;
        if (area > bestArea && area > 8000) {
          bestArea = area;
          best = el;
        }
      }
      if (best) {
        best.scrollIntoView({ block: 'center' });
        best.focus();
        best.click();
        return true;
      }
      return false;
    }"""
    )


def _click_untitled_document_chip_js(page) -> bool:
    """Header chip still reads Untitled — click it via DOM (Workspace UI differs)."""
    return page.evaluate(
        """() => {
      const roots = Array.from(document.querySelectorAll('header, [role="banner"], div[jsname]'));
      for (const root of roots) {
        const buttons = root.querySelectorAll('[role="button"], span[role="button"], div[role="button"]');
        for (const b of buttons) {
          const t = (b.innerText || '').trim();
          if (/^Untitled site$/i.test(t) || /^Untitled$/i.test(t)) {
            b.click();
            return true;
          }
        }
      }
      const globalBtns = document.querySelectorAll('[role="button"]');
      for (const b of globalBtns) {
        const t = (b.innerText || '').trim();
        if (t === 'Untitled site' || t === 'Untitled') {
          const r = b.getBoundingClientRect();
          if (r.top < 120 && r.left < 480) {
            b.click();
            return true;
          }
        }
      }
      return false;
    }"""
    )


@_retry(max_attempts=3, delay=0.45, label="set-site-title")
def _set_site_title_in_header(page, title: str):
    """Extreme search for the top-bar document title."""
    title = (title or "").strip()
    if not title:
        return
    _dismiss_chrome(page, 2)
    time.sleep(0.5)

    # Use a comprehensive JS search to find the 'Untitled' button/input
    success = page.evaluate(
        """(t) => {
      const needles = [/untitled site/i, /^untitled$/i, /enter site name/i, /site name/i];
      const all = Array.from(document.querySelectorAll('div[role="button"], button, span[role="button"], [contenteditable="true"], input'));
      for (const el of all) {
        const txt = (el.innerText || el.textContent || el.value || '').trim().toLowerCase();
        const aria = (el.getAttribute('aria-label') || '').toLowerCase();
        if (needles.some(n => n.test(txt) || n.test(aria))) {
          // If it's the top-left area
          const r = el.getBoundingClientRect();
          if (r.top < 150 && r.left < 500) {
            el.focus();
            el.click();
            // Try to set value directly if it's an input
            if (el.tagName === 'INPUT') {
              el.value = t;
              el.dispatchEvent(new Event('input', { bubbles: true }));
              el.dispatchEvent(new Event('change', { bubbles: true }));
            }
            return true;
          }
        }
      }
      return false;
    }""",
        title[:120],
    )

    if not success:
        # Fallback to absolute click if JS search failed
        page.mouse.click(125, 48)
        time.sleep(0.3)

    # Keyboard entry with A-Backspace-Type sequence
    page.keyboard.press("Control+A")
    time.sleep(0.1)
    page.keyboard.press("Backspace")
    time.sleep(0.2)
    # Type title
    page.keyboard.type(title[:120], delay=25)
    page.keyboard.press("Enter")
    time.sleep(0.3)
    page.keyboard.press("Escape")
    time.sleep(0.6)

    # Final verify/correct
    if _topbar_still_untitled(page):
        logger.warning("Header still untitled, trying forced JS injection")
        page.evaluate(
            """(t) => {
          const btn = Array.from(document.querySelectorAll('header [role="button"], [role="banner"] [role="button"]'))
            .find(b => /untitled/i.test(b.innerText));
          if (btn) {
            btn.click();
            setTimeout(() => {
              const inp = document.activeElement;
              if (inp) {
                if (inp.tagName === 'INPUT') inp.value = t;
                else inp.innerText = t;
                inp.dispatchEvent(new Event('input', { bubbles: true }));
                inp.dispatchEvent(new Event('blur', { bubbles: true }));
              }
            }, 200);
          }
        }""",
            title[:120],
        )
        time.sleep(1.0)


def _topbar_still_untitled(page) -> bool:
    try:
        return page.evaluate(
            """() => {
          const buttons = document.querySelectorAll('header [role="button"], [role="banner"] [role="button"]');
          for (const b of buttons) {
            const t = (b.innerText || '').trim();
            if (/^Untitled site$/i.test(t) || /^Untitled$/i.test(t)) return true;
          }
          return false;
        }"""
        )
    except Exception:
        return False


@_retry(max_attempts=3, delay=0.45, label="set-banner-title")
def _set_banner_page_title(page, title: str):
    """Extreme search for the large banner title (hero)."""
    title = (title or "").strip()
    if not title:
        return
    time.sleep(0.4)

    # Aggressive Typing Method
    try:
        # Find the title box and click it to focus
        found = page.evaluate("""() => {
            const isPlaceholder = (txt) => {
                const lower = (txt || '').toLowerCase();
                return lower.includes('your page title') || lower.includes('click to edit') || lower.includes('your title');
            };
            const targets = Array.from(document.querySelectorAll('h1[contenteditable="true"], [role="textbox"], [contenteditable="true"]'));
            for (const el of targets) {
                if (isPlaceholder(el.innerText)) {
                    el.scrollIntoView({ block: 'center' });
                    const rect = el.getBoundingClientRect();
                    return { x: rect.left + rect.width/2, y: rect.top + rect.height/2 };
                }
            }
            // If no placeholder, try the first H1
            const h1 = document.querySelector('h1');
            if (h1) {
                const rect = h1.getBoundingClientRect();
                return { x: rect.left + rect.width/2, y: rect.top + rect.height/2 };
            }
            return null;
        }""")
        
        if found:
            page.mouse.click(found['x'], found['y'])
            time.sleep(0.3)
            page.keyboard.press("Control+A")
            time.sleep(0.1)
            page.keyboard.press("Backspace")
            time.sleep(0.2)
            page.keyboard.type(title[:200], delay=30)
            page.keyboard.press("Enter")
            time.sleep(0.5)
            page.keyboard.press("Escape")
            return True
    except Exception as e:
        logger.warning(f"Keyboard title injection failed: {e}")
    
    # Fallback to JS Injection
    success = page.evaluate(
        """(t) => {
      const targets = Array.from(document.querySelectorAll('h1, [role="textbox"], [contenteditable="true"]'));
      for (const el of targets) {
        if (/your page title|click to edit/i.test(el.innerText || '')) {
          el.innerText = t;
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('blur', { bubbles: true }));
          return true;
        }
      }
      return false;
    }""",
        title,
    )
    return success
    if not success:
        # Try a wider range of coordinates for the banner title
        coords = [(640, 320), (400, 460), (640, 400)]
        for cx, cy in coords:
            page.mouse.click(cx, cy)
            time.sleep(0.2)
            page.keyboard.press("Control+A")
            page.keyboard.type(title[:500], delay=20)
            time.sleep(0.3)
            if not _banner_still_has_placeholder(page):
                success = True
                break

    page.keyboard.press("Escape")
    time.sleep(0.4)


def _set_banner_enter_site_name_if_present(page, title: str):
    """Small banner label 'Enter site name' (left of hero) — some themes use this instead of centre title."""
    title = (title or "").strip()
    if not title:
        return
    try:
        placeholders = ["Enter site name", "Site name"]
        for phrase in placeholders:
            loc = page.get_by_text(phrase, exact=True).first
            if loc.is_visible(timeout=1500):
                loc.click(timeout=4000)
                time.sleep(0.2)
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
                page.keyboard.type(title[:120], delay=20)
                time.sleep(0.2)
                page.keyboard.press("Enter")
                time.sleep(0.3)
                return
    except Exception:
        pass


def _apply_all_branding_after_theme(page, entry: SiteEntry):
    """
    Aristotle / theme apply resets banner placeholders — always re-apply chrome + banner after theme.
    """
    brand_full = branding_title(entry)
    brand_short = site_name_short(entry)
    
    _dismiss_chrome(page, 2)
    _set_site_title_in_header(page, brand_short)
    _set_banner_page_title(page, brand_full)
    _set_banner_enter_site_name_if_present(page, brand_short)
    
    # Theme animation can lag; wait significantly
    time.sleep(1.2)
    for _ in range(3):
        if _banner_still_has_placeholder(page) or _topbar_still_untitled(page):
            logger.info("Branding refresh pass...")
            _set_site_title_in_header(page, brand_short)
            _set_banner_page_title(page, brand_full)
            _set_banner_enter_site_name_if_present(page, brand_short)
            time.sleep(1.0)
        else:
            break


def _refresh_branding_after_embed(page, entry: SiteEntry):
    """Embed dialog steals focus — always set chrome + banner again so both places stay filled."""
    brand_full = branding_title(entry)
    brand_short = site_name_short(entry)
    
    _dismiss_chrome(page, 2)
    time.sleep(0.28)
    _set_site_title_in_header(page, brand_short)
    _set_banner_page_title(page, brand_full)
    _set_banner_enter_site_name_if_present(page, brand_short)


def _banner_still_has_placeholder(page) -> bool:
    try:
        return page.evaluate(
            """() => {
          const bad = ['your page title', 'click to edit', 'your title'];
          const h1s = Array.from(document.querySelectorAll('h1'));
          const textboxes = Array.from(document.querySelectorAll('[role="textbox"], [contenteditable="true"]'));
          const all = [...h1s, ...textboxes];
          for (const el of all) {
            const t = (el.innerText || '').trim().toLowerCase();
            if (bad.some(b => t === b || t.includes(b)) && t.length < 60) return true;
          }
          return false;
        }"""
        )
    except Exception:
        return False


def _apply_theme(page):
    """
    Apply Aristotle theme — bounded time only (never wait on Playwright default 45s).
    If the picker UI changed, we skip theme rather than stalling the batch.
    """
    try:
        themes_tab = page.locator('[role="tab"]:has-text("Themes")').first
        themes_tab.click(timeout=SHORT_TIMEOUT)
        time.sleep(0.45)

        # Prefer JS click — matches visible label even when role=listitem layout changes
        if not _click_aristotle_theme_js(page):
            # Fallback: Playwright locator with strict timeout (no 45s hang)
            try:
                page.locator(
                    '[role="listitem"]:has-text("Aristotle"), [aria-label*="Aristotle" i]'
                ).first.click(timeout=SHORT_TIMEOUT, force=True)
            except Exception:
                logger.warning("Aristotle theme tile not found — continuing with default theme")

        time.sleep(0.45)
    except Exception as e:
        logger.warning("Theme application skipped: %s", e)

    try:
        page.locator('[role="tab"]:has-text("Insert")').first.click(force=True, timeout=SHORT_TIMEOUT)
        time.sleep(0.35)
    except Exception:
        pass


@_retry(max_attempts=2, delay=1.0, label="embed-content")
def _insert_embed_content(page, premium_html: str):
    """Open Embed dialog, paste HTML, insert widget."""
    # Ensure Insert tab is active
    try:
        page.locator('[role="tab"]:has-text("Insert")').first.click(force=True)
        time.sleep(0.4)
    except Exception:
        pass

    # Click the Embed button
    embed_selectors = [
        'div[role="menuitem"]:has-text("Embed")',
        'div[aria-label="Embed"]',
        '[data-tooltip="Embed" i]',
    ]
    if not _click_first_visible(page, embed_selectors, timeout=8000):
        raise RuntimeError("Could not find Embed button in Insert panel")

    time.sleep(0.6)

    # Switch to "Embed code" tab in the dialog
    embed_code_tab = page.locator('div[role="dialog"] [role="tab"]:has-text("Embed code")').first
    embed_code_tab.click(force=True, timeout=8000)
    time.sleep(0.6)
    
    # Ensure tab is actually selected before proceeding
    page.locator('div[role="dialog"] [role="tab"][aria-selected="true"]:has-text("Embed code")').wait_for(timeout=5000)

    # Fill textarea
    ta = page.locator('div[role="dialog"] textarea').first
    ta.wait_for(state="visible", timeout=8000)
    
    # Split massive HTML into 5000-char chunks
    chunks = [premium_html[i : i + 5000] for i in range(0, len(premium_html), 5000)]
    for i, chunk in enumerate(chunks):
        if i == 0:
            ta.fill(chunk)
        else:
            page.keyboard.press("Control+End")
            page.keyboard.type(chunk)
        time.sleep(0.2)

    # Wait for 'Next' button to be ENABLED (Massive content takes time to process)
    next_btn = page.locator('div[role="dialog"] div[role="button"]:has-text("Next")').first
    try:
        next_btn.wait_for(state="visible", timeout=15000)
        # Extra wait for massive data processing
        time.sleep(2.5) 
        next_btn.click(force=True)
    except:
        page.keyboard.press("Enter")

    # CRITICAL: Dynamic wait time based on content length
    # Base 30s + 1s per 2500 characters (e.g., 50k chars = 50s wait)
    char_count = len(premium_html)
    dynamic_wait = 30.0 + (char_count / 2500.0)
    dynamic_wait = min(120.0, dynamic_wait) # Cap at 2 minutes for sanity
    
    logger.info("Massive content detected (%s chars). Waiting %.1fs for preview engine...", char_count, dynamic_wait)
    time.sleep(dynamic_wait) 

    # Click Insert (Wait for it to be active and visible)
    insert_btn = page.locator('div[role="dialog"] div[role="button"]:has-text("Insert")').first
    try:
        insert_btn.wait_for(state="visible", timeout=20000)
        time.sleep(2.0)
        insert_btn.click(force=True, timeout=30000)
    except Exception as e:
        logger.warning("Insert button error (massive content delay?): %s. Trying Enter key...", e)
        page.keyboard.press("Enter")
    
    # Wait for the widget to appear in the main editor
    time.sleep(3.5)
    _smart_wait(page, timeout_ms=5000)


def _embed_resize_target(page):
    """Locator for the outer embed widget (selection bounds / handles)."""
    ordered = [
        '[data-section-type="EMBED"]',
        '[data-section-type="embed"]',
        '[data-section-id]',
    ]
    for sel in ordered:
        loc = page.locator(sel).last
        try:
            if loc.count() > 0 and loc.is_visible(timeout=800):
                return loc
        except Exception:
            continue
    mf = page.locator('[role="main"] iframe, main iframe').last
    if mf.count():
        return mf
    bf = page.locator('iframe[src*="blob:"], iframe[srcdoc]').last
    if bf.count():
        return bf
    return None


def _drag_bottom_edge_expand(page, target_locator, delta_y: float) -> bool:
    """
    Pull the embed bottom edge down like the blue circular handle (mouse drag).
    Starts from bottom-centre of the widget bounding box.
    """
    if target_locator is None:
        return False
    try:
        box = target_locator.bounding_box()
        if not box or box.get("height", 0) < 40:
            return False
        cx = box["x"] + box["width"] / 2
        bottom_y = box["y"] + box["height"] - 6
        
        page.mouse.move(cx, bottom_y)
        page.mouse.down()
        
        # Continuous Scroll-Drag: Break the drag into chunks and scroll the page down while dragging
        total_drag = float(delta_y)
        num_flicks = 5
        for i in range(num_flicks):
            flick_y = bottom_y + ((i + 1) * (total_drag / num_flicks))
            # Move mouse relative to viewport
            page.mouse.move(cx, min(950, flick_y), steps=8)
            # Scroll the page to pull the handle up, effectively increasing drag distance
            page.mouse.wheel(0, total_drag / num_flicks)
            time.sleep(0.15)
            
        page.mouse.up()
        time.sleep(0.35)
        return True
    except Exception as exc:
        logger.warning("Mouse drag resize failed: %s", exc)
        return False


def _drag_bottom_right_corner_expand(page, target_locator, delta_y: float, delta_x: float) -> bool:
    """Diagonal drag from bottom-right handle — grows height and width (fixes narrow left column)."""
    if target_locator is None:
        return False
    try:
        box = target_locator.bounding_box()
        if not box:
            return False
        sx = box["x"] + box["width"] - 10
        sy = box["y"] + box["height"] - 10
        
        page.mouse.move(sx, sy)
        page.mouse.down()
        time.sleep(0.15)

        # Motion 1: Drag horizontally to hit full edges
        page.mouse.move(sx + 1200, sy, steps=5)
        time.sleep(0.1)
        
        # Motion 2: Continuous Scroll-Drag Vertically
        total_drag = float(delta_y)
        num_flicks = 6
        for i in range(num_flicks):
            page.mouse.move(sx + 1200, min(950, sy + ((i+1)*(total_drag/num_flicks))), steps=10)
            page.mouse.wheel(0, total_drag / num_flicks)
            time.sleep(0.15)
        
        page.mouse.up()
        time.sleep(0.1)
        return True
    except Exception as exc:
        logger.warning("Corner drag resize failed: %s", exc)
        return False


def _drag_right_edge_expand(page, target_locator, delta_x: float) -> bool:
    """Middle-right handle — stretch embed toward full content column width."""
    if target_locator is None:
        return False
    try:
        box = target_locator.bounding_box()
        if not box:
            return False
        mx = box["x"] + box["width"] - 8
        my = box["y"] + box["height"] / 2
        end_x = mx + max(150, min(float(delta_x), 1100))
        page.mouse.move(mx, my)
        page.mouse.down()
        page.mouse.move(end_x, my, steps=22)
        page.mouse.up()
        time.sleep(0.28)
        return True
    except Exception as exc:
        logger.warning("Right-edge drag failed: %s", exc)
        return False


def _viewport_drag_budget(page) -> tuple[float, float]:
    """Max horizontal stretch toward canvas right edge."""
    vp = page.viewport_size
    if not vp:
        return 700.0, 950.0
    w = float(vp["width"])
    return min(850.0, w * 0.42), min(1050.0, w * 0.48)


def _resize_embed_block(page, steps: int):
    """
    Select the embedded HTML widget and stretch vertically:
    1) Mouse-drag from bottom edge (matches editor blue handles — primary).
    2) Keyboard Shift+Arrow resize (secondary — legacy Sites behaviour).
    """
    _dismiss_chrome(page, 2)
    time.sleep(0.25)

    main_iframe = page.locator('[role="main"] iframe, main iframe, article iframe').last
    section_candidates = [
        '[data-section-type="EMBED"]',
        '[data-section-type="embed"]',
        '[data-section-id]',
        "[data-selection]",
        'section[data-section-id]',
    ]

    selected = False

    for sel in section_candidates:
        try:
            loc = page.locator(sel).last
            if loc.count() > 0:
                loc.click(force=True, timeout=4000)
                selected = True
                break
        except Exception:
            continue

    if not selected:
        try:
            if main_iframe.count():
                main_iframe.click(force=True, timeout=5000)
                selected = True
        except Exception:
            pass

    if not selected:
        try:
            blob_frames = page.locator('iframe[src*="blob:"], iframe[srcdoc]')
            if blob_frames.count() > 0:
                blob_frames.last.click(force=True, timeout=4000)
                selected = True
        except Exception:
            pass

    if not selected:
        try:
            for _ in range(14):
                page.keyboard.press("Tab")
            page.keyboard.press("Enter")
            selected = True
        except Exception:
            pass

    if not selected:
        try:
            vp = page.viewport_size
            if vp:
                page.mouse.click(vp["width"] // 2, min(560, int(vp["height"] * 0.52)))
            else:
                page.mouse.click(640, 520)
            selected = True
        except Exception:
            pass

    if not selected:
        logger.warning("Could not select embed for resize — content still published but height may be short")
        return

    time.sleep(0.35)

    # Pixel drag scales with content volume (same heuristic as keyboard steps)
    # Pixel drag scales with content volume. Removed 6200px cap for massive reports.
    drag_px = max(1300.0, min(120000.0, steps * 2.15))
    dx_budget, dx_wide = _viewport_drag_budget(page)

    resize_loc = _embed_resize_target(page)
    if resize_loc is not None:
        # Bottom-centre pulls — vertical height (paragraph visibility)
        # Reduced to 2 passes for speed; corner drag will do the rest
        for pass_idx in range(2):
            ok = _drag_bottom_edge_expand(page, resize_loc, drag_px)
            if ok:
                logger.info("Embed bottom-edge drag pass %s (~%.0fpx tall)", pass_idx + 1, drag_px)
            time.sleep(0.2)
            resize_loc = _embed_resize_target(page)

        resize_loc = _embed_resize_target(page)
        if resize_loc is not None:
            # Bottom-right diagonal — height + width
            _drag_bottom_right_corner_expand(page, resize_loc, drag_px * 0.85, dx_budget)
            time.sleep(0.2)

        resize_loc = _embed_resize_target(page)
        if resize_loc is not None:
            # Right-mid handle — full-bleed width
            _drag_right_edge_expand(page, resize_loc, dx_wide)
            time.sleep(0.2)

    # Secondary: keyboard widen + height
    time.sleep(0.1)
    # Fast widen
    page.keyboard.press("Control+A") # Ensure selected
    for _ in range(8):
        page.keyboard.press("ArrowLeft")
    for _ in range(12):
        page.keyboard.press("Shift+ArrowRight")

    # NEW STRATEGY: Multi-drag flicks (MUCH faster than keyboard loop)
    # Dragging 85k pixels via keyboard takes 15 mins. This takes 10 seconds.
    logger.info("Executing Hyper-Drag flicks for %s steps...", steps)
    
    for flick in range(4): # 4 massive flicks to ensure full expansion
        resize_loc = _embed_resize_target(page)
        if resize_loc:
            # Click and drag down aggressively
            _drag_bottom_edge_expand(page, resize_loc, 25000)
            time.sleep(0.5)
            # Scroll down to let the DOM expand
            page.mouse.wheel(0, 15000)
            time.sleep(0.3)
    
    # Final fine-tuning (only 500 steps instead of 85k)
    page.keyboard.down("Shift")
    for _ in range(500):
        page.keyboard.press("ArrowDown")
    page.keyboard.up("Shift")


def _click_published_site_dropdown(page) -> bool:
    """
    Open ONLY the 'Published site' row dropdown (Workspace shows org name — must change to Public).
    Does not touch the Draft row.
    """
    return page.evaluate(
        """() => {
      const dlgs = Array.from(document.querySelectorAll('[role="dialog"], [role="presentation"], .modal-dialog'));
      const dlg = dlgs.find(d => d.innerText.includes('General access') || d.innerText.includes('Share')) || dlgs[0];
      if (!dlg) return false;

      const candidates = Array.from(dlg.querySelectorAll('[role="listitem"], tr, div'));
      for (const block of candidates) {
        const t = block.innerText || '';
        if (!t.includes('Published site')) continue;
        if (t.includes('RedOrangeTechnologies') || t.includes('Public') || t.includes('Anyone')) {
            const ctrls = block.querySelectorAll('[role="button"], [role="combobox"], button');
            for (const c of ctrls) {
                const label = (c.innerText || '').trim();
                if (/^done$/i.test(label) || label.includes('Copy')) continue;
                c.scrollIntoView({ block: 'center' });
                c.click();
                return true;
            }
        }
      }

      // Fallback search
      const spans = Array.from(dlg.querySelectorAll('span, div, p'));
      for (const el of spans) {
        const s = (el.textContent || '').trim();
        if (!s.includes('Published site')) continue;
        const row = el.closest('[role="listitem"]') || el.parentElement;
        const btn = row.querySelector('[role="button"], [role="combobox"], button');
        if (btn) { btn.click(); return true; }
      }
      return false;
    }"""
    )


def _select_public_or_internet_option(page) -> bool:
    """Pick true public/internet visibility — not Workspace-only / organization."""
    # Playwright's get_by_text is very reliable for these menus
    try:
        # Try finding the 'Public' option in the list
        public_item = page.get_by_text("Public", exact=True).first
        if public_item.is_visible(timeout=5000):
            public_item.click(force=True)
            return True
    except:
        pass

    return page.evaluate(
        """() => {
      const opts = Array.from(
        document.querySelectorAll(
          '[role="option"], [role="menuitem"], li[role="option"], div[role="presentation"] li, div[jsname] li, span'
        )
      );
      let best = null;
      let rank = -999;
      for (const o of opts) {
        const raw = (o.innerText || '').trim();
        const t = raw.toLowerCase();
        if (!t || t.length > 240) continue;
        let r = -999;
        if (/^public$/i.test(raw.trim())) r = 100;
        else if (/public on the web|on the web$/i.test(t)) r = 98;
        else if (/anyone on the internet|internet to find|whole internet/i.test(t)) r = 96;
        if (r > rank) {
          rank = r;
          best = o;
        }
      }
      if (best && rank >= 90) {
        best.scrollIntoView({ block: 'center' });
        best.click();
        return true;
      }
      return false;
    }"""
    )


def _published_site_already_public_or_link(page) -> bool:
    """If Published site row already shows Public / link — skip opening org dropdown."""
    return page.evaluate(
        """() => {
      const dlgs = Array.from(document.querySelectorAll('[role="dialog"]'));
      const dlg = dlgs.find(d => d.innerText.includes('General access')) || dlgs[0];
      if (!dlg) return false;
      const rows = Array.from(dlg.querySelectorAll('[role="listitem"], tr'));
      for (const row of rows) {
        const t = row.innerText || '';
        if (!t.includes('Published site')) continue;
        const isPublic = /Public on the web|\\bPublic\\b|Anyone with the link/i.test(t);
        if (isPublic) return true;
      }
      return false;
    }"""
    )


@_retry(max_attempts=3, delay=1.5, label="make-public")
def _make_site_public(page):
    """
    Share → General access → Published site = Public / internet (never leave org-only default).
    If already Public / link-accessible, only confirm Done (no unnecessary org state).
    """
    page.mouse.wheel(0, -8000)
    time.sleep(0.4)

    share_selectors = [
        'button[aria-label*="Share" i]',
        'div[role="button"][aria-label*="Share" i]',
        '[aria-label*="Share with others" i]',
        'div[jsname="V67oCd"]:has-text("Share")',
        'div[role="button"]:has-text("Share")',
        '[data-tooltip*="Share" i]',
    ]
    if not _click_first_visible(page, share_selectors, timeout=8000):
        # Desperate coordinate fallback for top-right share icon area
        page.mouse.click(1150, 45)
    
    # Wait for the dialog to animate and settle
    time.sleep(1.5)

    # Wait for the dialog to appear by checking for common text inside it (General access)
    try:
        page.locator('text=/General access|Share with/i').wait_for(state="visible", timeout=DIALOG_TIMEOUT)
    except Exception:
        try:
            # Fallback to general dialog role or presentation role
            page.locator('[role="dialog"], [role="presentation"]').first.wait_for(state="visible", timeout=4000)
        except:
             raise RuntimeError("Share dialog did not appear")

    time.sleep(0.6)

    if _published_site_already_public_or_link(page):
        logger.info("Published site already public/link — skipping dropdown change")
        done_selectors = [
            'button:has-text("Done")',
            'div[role="button"]:has-text("Done")',
        ]
        if _click_first_visible(page, done_selectors, timeout=5000):
            time.sleep(0.45)
        else:
            page.keyboard.press("Escape")
            time.sleep(0.35)
        return

    if not _click_published_site_dropdown(page):
        logger.warning("Published-site row dropdown not found; trying fallback scan")
        page.evaluate(
            """() => {
          const dlg = document.querySelector('[role="dialog"]');
          if (!dlg) return;
          const rows = Array.from(dlg.querySelectorAll('[role="listitem"]'));
          for (const row of rows) {
            const t = row.innerText || '';
            if (!t.includes('Published site')) continue;
            const b = row.querySelector('[role="button"], [role="combobox"]');
            if (b) {
              b.click();
              break;
            }
          }
        }"""
        )

    time.sleep(0.85)

    if not _select_public_or_internet_option(page):
        logger.warning("Could not select Public option via JS.")
        # Do NOT use key-based entry here as it might type into 'Add people' box
        pass

    time.sleep(1.2)

    # Click the blue DONE button
    time.sleep(1.0)
    try:
        done_btn = page.get_by_role("button", name="Done").first
        if done_btn.is_visible(timeout=5000):
            done_btn.click(force=True)
            time.sleep(2.0)
            return
    except:
        pass

    done_selectors = [
        'button:has-text("Done")',
        'div[role="button"]:has-text("Done")',
        'span:has-text("Done")',
        '.VfPpkd-LgbsSe:has-text("Done")',
    ]
    if not _click_first_visible(page, done_selectors, timeout=8000):
        # Desperate coordinate fallback for the Done button at bottom-right of the dialog
        logger.warning("Done button not found via selectors; using coordinate fallback")
        page.keyboard.press("Enter")
        time.sleep(1.0)
        page.mouse.click(640, 520) 
    
    time.sleep(2.5)


def _workspace_publish_url_candidates(page, published_slug: str):
    """Extra patterns for Google Workspace Sites when /view/... is not used."""
    candidates = []
    u = page.url or ""
    m = re.search(r"sites\.google\.com/a/([^/]+)/", u)
    if m:
        dom = m.group(1)
        candidates.append(f"https://sites.google.com/a/{dom}/{published_slug}/home")
        candidates.append(f"https://sites.google.com/a/{dom}/{published_slug}")
    return candidates


def _fallback_urls_for_slug(page, published_slug: str):
    """Ordered list of URLs to probe after publish (consumer first, then Workspace hints)."""
    out = [
        f"https://sites.google.com/view/{published_slug}/home",
        f"https://sites.google.com/view/{published_slug}",
    ]
    out.extend(_workspace_publish_url_candidates(page, published_slug))
    return out


def _harvest_live_site_url(page, entry: SiteEntry, published_slug: str) -> str:
    """
    Aggregate DOM + clipboard + slug fallbacks. Never store workspace-root URLs (404).
    """
    page.mouse.wheel(0, -8000)
    time.sleep(0.35)

    link_selectors = [
        '[aria-label*="Copy published site link" i]',
        '[aria-label*="Copy link" i]',
        '[aria-label*="site link" i]',
        'div[role="button"][aria-label*="published" i]',
        '[data-tooltip*="published link" i]',
        '[aria-label*="link to published" i]',
    ]

    deadline = time.time() + URL_POLL_SECS
    last_clipboard_try = 0.0

    def _take_if_valid(u: str) -> str:
        if not u:
            return ""
        u = u.strip().split()[0].split("?")[0].rstrip("/")
        if _looks_like_valid_published_url(u):
            return u
        return ""

    while time.time() < deadline:
        url = _extract_published_url_from_dom(page)
        ok = _take_if_valid(url)
        if ok:
            return ok

        now = time.time()
        if now - last_clipboard_try > 2.5:
            _click_first_visible(page, link_selectors, timeout=2500)
            time.sleep(0.5)
            cb = _clipboard_text_async(page)
            last_clipboard_try = now
            if cb and "sites.google.com" in cb:
                for line in cb.splitlines():
                    ok = _take_if_valid(line)
                    if ok:
                        return ok

        try:
            dlg_input = page.locator('div[role="dialog"] input[type="text"]').first
            if dlg_input.is_visible(timeout=400):
                raw = dlg_input.input_value()
                ok = _take_if_valid(raw or "")
                if ok:
                    return ok
                dlg_input.click()
                page.keyboard.press("Control+A")
                page.keyboard.press("Control+C")
                time.sleep(0.35)
                cb = _clipboard_text_async(page)
                ok = _take_if_valid(cb or "")
                if ok:
                    return ok
        except Exception:
            pass

        time.sleep(0.45)

    for fb in _fallback_urls_for_slug(page, published_slug):
        # Pass the brand name to ensure we verify the CORRECT site
        brand = branding_title(entry)
        if _verify_public_page_loads(page, fb, expected_text=brand):
            logger.info("Harvest verified fallback URL: %s", fb)
            return fb.rstrip("/")

    return ""


def _build_publish_slug(entry: SiteEntry) -> str:
    """
    Strict sequential format: dhr-NNN
    Stays well under the 30-char limit.
    """
    base = (entry.slug or "").strip().lower()
    if not base:
        base = slugify(branding_title(entry))[:20]
    
    # Ensure it's not too long
    return base[:28]


def _publish_and_capture_url(page, entry: SiteEntry):
    """Publish and return a validated public URL (never workspace domain roots)."""
    logger.info("Triggering Publish flow...")
    page.keyboard.press("Escape")
    time.sleep(0.1)
    page.keyboard.press("Escape")
    time.sleep(0.2)
    # Click in a safe neutral top-bar area (away from Home icon at 0-50 and Publish at 1000+)
    page.mouse.click(350, 25) 
    time.sleep(0.4)

    publish_selectors = [
        'div[role="button"]:has-text("Publish")',
        'div[aria-label="Publish"]',
        'div[jsname="V67oCd"]',
        'button:has-text("Publish")',
        '[data-tooltip*="Publish" i]',
        'div[role="button"] >> text="Publish"',
    ]

    # 1. Click main "Publish" button (Power JS Click)
    try:
        page.wait_for_selector('div[role="button"]:has-text("Publish"), [jsname="V67oCd"]', timeout=4000)
    except:
        pass

    success = page.evaluate("""() => {
        const findBtn = () => {
            const allBtns = Array.from(document.querySelectorAll('div[role="button"]'));
            return allBtns.find(el => el.innerText.trim() === 'Publish') || 
                   document.querySelector('[jsname="V67oCd"]') ||
                   allBtns.find(el => el.innerText.includes('Publish'));
        };
        const btn = findBtn();
        if (btn) { btn.click(); return true; }
        return false;
    }""")
    if not success:
        _click_first_visible(page, publish_selectors, timeout=3000)
    
    time.sleep(0.5)

    final_slug = _build_publish_slug(entry)

    # Ensure dialog is open
    dialog_opened = False
    for attempt_dlg in range(5):
        if page.locator('div[role="dialog"]').first.is_visible(timeout=2000):
            dialog_opened = True
            break
        # Re-click with increasing force
        logger.info(f"Publish dialog not open (attempt {attempt_dlg+1}), re-clicking...")
        _click_first_visible(page, publish_selectors, timeout=3000)
        page.keyboard.press("Enter") # Sometimes Enter triggers it if button is focused
        time.sleep(1.5)

    if dialog_opened:
        try:
            dialog = page.locator('div[role="dialog"]').first
            
            # Visibility setting skipped to go direct to publishing
            # manage_link = dialog.locator('text=/Manage|Who can view/i').first
            # if manage_link.is_visible(timeout=2000):
            #     logger.info("Found visibility Manage link in Publish dialog; setting to Public...")
            #     manage_link.click()
            #     time.sleep(1.5)
            #     _make_site_public(page) # This will handle the nested Share dialog
            #     time.sleep(1.0)

            inp = dialog.locator('input[type="text"], input:not([type="hidden"])').first
            
            def attempt_publish(slug_to_use):
                inp.click(timeout=3000)
                time.sleep(0.2)
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
                time.sleep(0.1)
                page.keyboard.type(slug_to_use, delay=10)
                
                # Wait for slug validation
                time.sleep(2.0) 
                
                # Check for "already taken" or any other validation warning
                is_invalid = page.evaluate("""() => {
                    const dlg = document.querySelector('[role="dialog"]');
                    if (!dlg) return false;
                    const txt = dlg.innerText || '';
                    // Catch common Google Sites validation errors
                    return /already taken|great address, but|invalid|shorter|longer/i.test(txt);
                }""")
                
                if is_invalid:
                    return False

                # Power click the dialog's Publish button
                clicked = page.evaluate("""() => {
                    const d = document.querySelector('[role="dialog"]');
                    if (!d) return false;
                    const btns = Array.from(d.querySelectorAll('div[role="button"], button'));
                    const b = btns.find(el => {
                        const t = el.innerText.trim();
                        return t === 'Publish' || t === 'PUBLISH';
                    });
                    if (b && !b.getAttribute('aria-disabled') && !b.disabled) {
                        b.click();
                        return true;
                    }
                    return false;
                }""")
                
                if not clicked:
                    page.keyboard.press("Enter")
                
                time.sleep(1.0)

                # Wait for dialog to disappear (indicates success)
                for _ in range(8):
                    if not dialog.is_visible(timeout=500):
                        return True
                    time.sleep(1.0)
                    page.keyboard.press("Enter")
                return False
                
            success_pub = attempt_publish(final_slug)
            
            # If taken, try suffixes: dhr-001-1, dhr-001-2... up to -20
            if not success_pub and dialog.is_visible(timeout=500):
                base_slug = final_slug[:25] # Leave room for suffix
                for suffix_idx in range(1, 21):
                    new_try = f"{base_slug}-{suffix_idx}"
                    logger.warning(f"Slug '{final_slug}' taken, trying dynamic fallback: {new_try}")
                    if attempt_publish(new_try):
                        success_pub = True
                        final_slug = new_try
                        break
                
                if not success_pub:
                    # Final desperate try with timestamp suffix
                    new_try = f"{base_slug[:20]}-{int(time.time()) % 10000}"
                    attempt_publish(new_try)
                    final_slug = new_try
                
        except Exception as e:
            logger.warning("Publish dialog interaction: %s", e)
            page.keyboard.press("Enter")
    else:
        logger.warning("Publish dialog never appeared, hitting Enter as fallback")
        page.keyboard.press("Enter")

    time.sleep(PUBLISH_SETTLE_SECS)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5000)
    except PlaywrightTimeoutError:
        pass

    url = _harvest_live_site_url(page, entry, final_slug)

    if not url or not _looks_like_valid_published_url(url):
        raise RuntimeError(
            "Could not capture a valid published URL with a real page path (/view/... or /a/...). "
            "Check publish finished and GS_SKIP_SHARE=0 once if the site is not public."
        )

    return url.rstrip("/")


# ===================================================================
#  Main orchestrator
# ===================================================================

def _process_single_entry(page, entry, index, total, batch):
    """Process one SiteEntry end-to-end. Returns (success: bool, url_or_error: str)."""
    brand = branding_title(entry)
    premium_html = build_premium_embed_html(entry)

    logger.info("[%s/%s] Processing: %s", index, total, brand[:80])
    batch.current_action = f"[{index}/{total}] Building: {brand[:40]}..."
    batch.save()

    # 1. Navigate to blank template
    page.goto(
        "https://sites.google.com/u/0/create?template=blank&authuser=0",
        wait_until="domcontentloaded",
    )
    _smart_wait(page, timeout_ms=12000)
    time.sleep(PAGE_LOAD_SETTLE_SECS)
    _ensure_logged_into_sites(page)
    _dismiss_chrome(page)
    # Ensure we are at the top
    page.mouse.wheel(0, -10000)

    # 2. Theme first — Aristotle resets banner fields; branding is applied immediately after.
    _apply_theme(page)

    # 3. Site name + banner (after theme so placeholders are not wiped)
    _apply_all_branding_after_theme(page, entry)

    # 4. Full HTML embed
    batch.current_action = f"[{index}/{total}] Embedding content..."
    batch.save()
    _insert_embed_content(page, premium_html)

    # 5. Stretch embed so full paragraph/html shows (no inner scrollbar)
    rsteps = resize_steps_for_embed(premium_html)
    logger.info("Resizing embed block (%s steps)", rsteps)
    _resize_embed_block(page, rsteps)
    
    # FINAL VERIFICATION: Ensure banner title didn't revert during resizing
    if _banner_still_has_placeholder(page):
        logger.info("Banner placeholder detected before publish; fixing...")
        _set_banner_page_title(page, branding_title(entry))

    # Click a safe area to deselect everything so Publish button is active/clickable immediately
    page.keyboard.press("Escape")
    time.sleep(0.1)
    page.mouse.click(400, 20) 
    page.mouse.wheel(0, -10000)
    time.sleep(0.4)

    # 6. Share (optional — skip by default; set GS_SKIP_SHARE=0 to force Share dialog)
    if SKIP_SHARE_DIALOG:
        logger.info("Skipping Share dialog (GS_SKIP_SHARE default); publish assumes tenant Public defaults.")
        batch.current_action = f"[{index}/{total}] Skipping Share — publishing..."
        batch.save()
    else:
        batch.current_action = f"[{index}/{total}] Setting public visibility..."
        batch.save()
        _make_site_public(page)

    # 7. Publish → capture live URL → next spreadsheet row
    batch.current_action = f"[{index}/{total}] Publishing..."
    batch.save()
    url = _publish_and_capture_url(page, entry)

    return url


def run_automation(batch_id):
    """Main entry point — called from the Django view in a background thread."""
    batch = None
    context = None
    browser = None

    # Scalability: Restart browser every N entries to clear memory
    RESTART_EVERY = 40

    try:
        batch = SiteBatch.objects.get(id=batch_id)
        entries_list = list(batch.entries.filter(status="pending"))

        if not entries_list:
            batch.status = "completed"
            batch.current_action = "No pending entries."
            batch.save()
            return

        user_data_dir = os.path.abspath(os.path.join(os.getcwd(), "google_session"))
        logger.info("Starting bulk engine for %s row(s)", len(entries_list))
        
        batch.current_action = f"Initializing engine for {len(entries_list)} sites..."
        batch.save()

        with sync_playwright() as p:
            def launch_ctx():
                logger.info("Launching/Restarting browser context...")
                batch.current_action = "Launching browser (Playwright)..."
                batch.save()
                ctx = p.chromium.launch_persistent_context(
                    user_data_dir=user_data_dir,
                    headless=False,
                    slow_mo=0,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--start-maximized",
                        "--disable-dev-shm-usage",
                        "--no-sandbox",
                    ],
                    no_viewport=True,
                )
                pg = ctx.pages[0]
                pg.set_default_timeout(45000)
                _grant_clipboard(pg)
                return ctx, pg

            context, page = launch_ctx()
            
            # CRITICAL: Wait for manual login BEFORE starting the loop
            _ensure_logged_into_sites(page)
            
            total = len(entries_list)

            for index, entry in enumerate(entries_list, 1):
                # FORCE sequential slug dhr-001, dhr-002...
                new_slug = f"dhr-{str(index).zfill(3)}"
                if entry.slug != new_slug:
                    entry.slug = new_slug
                    entry.save()

                # Scalability: Periodic browser restart
                if index > 1 and (index - 1) % RESTART_EVERY == 0:
                    logger.info("Reached %s entries, restarting browser to maintain stability...", index - 1)
                    batch.current_action = f"Restarting browser for stability ({index}/{total})..."
                    batch.save()
                    context.close()
                    time.sleep(2.0)
                    context, page = launch_ctx()

                if page.is_closed():
                    logger.error("Page closed unexpectedly; attempting to relaunch")
                    context, page = launch_ctx()

                # Refresh entry from DB
                current_entry = SiteEntry.objects.get(id=entry.id)
                current_entry.status = "processing"
                current_entry.save()

                success = False
                last_error = ""

                for attempt in range(1, MAX_RETRIES_PER_ENTRY + 1):
                    try:
                        url = _process_single_entry(page, current_entry, index, total, batch)

                        current_entry.published_url = url
                        current_entry.status = "success"
                        current_entry.error_message = None
                        current_entry.retry_count = attempt - 1
                        current_entry.save()

                        batch.completed_sites += 1
                        batch.save()
                        logger.info("SUCCESS [%s/%s]: %s", index, total, url)

                        _dismiss_chrome(page, 3)
                        success = True
                        break

                    except Exception as entry_err:
                        last_error = str(entry_err)
                        logger.warning("Entry attempt %s/%s failed: %s", attempt, MAX_RETRIES_PER_ENTRY, entry_err)
                        _safe_screenshot(page, f"entry_{current_entry.id}_attempt{attempt}")
                        _dismiss_chrome(page, 5)

                        if "accounts.google.com" in (page.url or ""):
                            logger.error("Session expired or login required. Aborting batch.")
                            raise RuntimeError("Google session expired. Please log in manually and restart.")

                        if attempt < MAX_RETRIES_PER_ENTRY:
                            # If it failed, try to refresh page or go back to start for next attempt
                            try:
                                page.goto("about:blank")
                                time.sleep(1.0)
                            except: pass

                if not success:
                    current_entry.status = "failed"
                    current_entry.error_message = last_error[:500]
                    current_entry.retry_count = MAX_RETRIES_PER_ENTRY
                    current_entry.save()
                    batch.failed_sites += 1
                    batch.save()
                    logger.error("FAILED [%s/%s]: %s", index, total, last_error[:200])
                    _dismiss_chrome(page, 5)

                if index < total:
                    time.sleep(INTER_ENTRY_PAUSE)

            batch.status = "completed"
            batch.current_action = f"Done — {batch.completed_sites} success, {batch.failed_sites} failed"
            batch.save()

            if context:
                context.close()

    except Exception as e:
        logger.error("Global fatal: %s\n%s", e, traceback.format_exc())
        if batch:
            batch.status = "failed"
            batch.current_action = f"Fatal error: {str(e)[:120]}"
            batch.save()
        try:
            if context:
                context.close()
        except Exception:
            pass
