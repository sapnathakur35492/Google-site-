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
SHORT_TIMEOUT = 8000          # ms – theme / flaky UI (never inherit 45s default)
DIALOG_TIMEOUT = 12000
PUBLISH_SETTLE_SECS = 3.0     # publish propagation before URL harvest
URL_POLL_SECS = 20.0          # total time to poll DOM for published URL
PAGE_LOAD_SETTLE_SECS = 2.5
INTER_ENTRY_PAUSE = 1.2       # seconds between entries to avoid rate-limits

SCREENSHOTS_DIR = os.path.abspath(os.path.join(os.getcwd(), "debug_screenshots"))
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

# Set GS_SKIP_SHARE=0 only if you must open Share and set "Published site" from automation.
# Default: skip Share entirely (faster; many tenants already default to Public).
SKIP_SHARE_DIALOG = os.environ.get("GS_SKIP_SHARE", "1").strip().lower() in ("1", "true", "yes", "on")


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
    """Fail fast if Google redirects to account login (session expired)."""
    try:
        u = page.url or ""
        if "accounts.google.com" in u and "signin" in u.lower():
            raise RuntimeError(
                "Google login required — open the browser profile once, sign in to Google, "
                "then restart automation."
            )
    except RuntimeError:
        raise
    except Exception:
        pass


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


def _dismiss_chrome(page, rounds=4):
    """Press Escape several times to close any popups / tooltips."""
    for _ in range(rounds):
        try:
            page.keyboard.press("Escape")
            time.sleep(0.12)
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

def branding_title(entry: SiteEntry) -> str:
    """
    Short name for site chrome, banner H1, and embed headline — matches consumer Sites
    examples (e.g. united-states-*-market) where nav title is one clean line.

    Prefer **Keyword** when present (sample XLSX uses it as the market title); else first
    line of Title. Full SEO/long copy stays only inside the Content column HTML.
    """
    kw = _normalize_nan(entry.keyword)
    if kw:
        return kw.strip()
    ttl = _normalize_nan(entry.title)
    if ttl:
        # Use full title, but strip extra whitespace/newlines
        return " ".join(ttl.split()).strip()
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
        '<body style="margin:0;padding:0;overflow:hidden;">'
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
        '#embedded-main span{overflow:visible!important;max-width:100%}'
        '#embedded-main strong,#embedded-main b{overflow:visible!important;max-width:100%}'
        '#embedded-main em,#embedded-main i{overflow:visible!important;max-width:100%}'
        'body,html{overflow:hidden!important;height:auto!important}'
        '</style></body></html>'
    )


def _html_text_len_approx(html_blob: str) -> int:
    text = re.sub(r"<[^>]+>", " ", html_blob or "")
    return max(1, len(re.sub(r"\s+", " ", text).strip()))


def resize_steps_for_embed(html_blob: str) -> int:
    n = _html_text_len_approx(html_blob)
    # Extra-aggressive multiplier to kill all scrollbars
    steps = int(math.ceil(n / 32.0) * 45)
    return max(1800, min(8000, steps))


# ===================================================================
#  Individual automation steps (each independently retryable)
# ===================================================================

def _fill_textarea_verified(page, textarea_locator, full_html: str):
    """Ensure 100% of embed HTML is injected — with triple-fallback."""
    textarea_locator.click(timeout=STEP_TIMEOUT)
    time.sleep(0.15)

    # Strategy 1: Playwright fill
    textarea_locator.fill(full_html)
    time.sleep(0.25)
    got = textarea_locator.input_value()
    if len(got) == len(full_html):
        return

    # Strategy 2: JS evaluate
    logger.warning("fill mismatch (%s vs %s), trying JS evaluate", len(got), len(full_html))
    textarea_locator.evaluate(
        "(el, val) => { el.value = val; el.dispatchEvent(new Event('input', { bubbles: true })); }",
        full_html,
    )
    time.sleep(0.25)
    got2 = textarea_locator.input_value()
    if len(got2) == len(full_html):
        return

    # Strategy 3: Clear + type (slower but reliable for very large content)
    logger.warning("JS evaluate also mismatched (%s), trying clear+clipboard", len(got2))
    textarea_locator.click(timeout=5000)
    page.keyboard.press("Control+A")
    page.keyboard.press("Backspace")
    time.sleep(0.1)
    textarea_locator.fill(full_html)
    time.sleep(0.3)
    got3 = textarea_locator.input_value()
    if len(got3) != len(full_html):
        raise RuntimeError(f"Embed HTML truncated after 3 attempts: expected {len(full_html)}, got {len(got3)}")


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
    page.keyboard.type(title[:120], delay=35)
    page.keyboard.press("Enter")
    time.sleep(0.8)

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

    # Power JS click for banner
    success = page.evaluate(
        """(t) => {
      // 1. Try to find the BIG H1 title first
      const h1s = Array.from(document.querySelectorAll('h1[contenteditable="true"], [role="main"] h1, header h1, .compact h1'));
      for (const h1 of h1s) {
        h1.scrollIntoView({ block: 'center' });
        h1.focus();
        h1.click();
        // Force set text via DOM first
        h1.innerText = t; 
        h1.dispatchEvent(new Event('input', { bubbles: true }));
        h1.dispatchEvent(new Event('blur', { bubbles: true }));
        return true;
      }
      
      // 2. Fallback to placeholder search
      const needles = [/click to edit text/i, /your page title/i, /click to edit/i, /click to edit heading/i, /your title/i];
      const boxes = Array.from(document.querySelectorAll('[role="textbox"], [contenteditable="true"]'));
      for (const el of boxes) {
        const txt = (el.innerText || el.textContent || '').trim().toLowerCase();
        if (needles.some(n => n.test(txt)) && txt.length < 60) {
          el.scrollIntoView({ block: 'center' });
          el.focus();
          el.click();
          el.innerText = t;
          el.dispatchEvent(new Event('input', { bubbles: true }));
          return true;
        }
      }
      return false;
    }""",
        title[:500],
    )
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


def _apply_all_branding_after_theme(page, brand: str):
    """
    Aristotle / theme apply resets banner placeholders — always re-apply chrome + banner after theme.
    """
    _dismiss_chrome(page, 2)
    _set_site_title_in_header(page, brand)
    _set_banner_page_title(page, brand)
    _set_banner_enter_site_name_if_present(page, brand)
    # Second pass: theme animation can lag
    time.sleep(0.35)
    if _banner_still_has_placeholder(page) or _topbar_still_untitled(page):
        _set_site_title_in_header(page, brand)
        _set_banner_page_title(page, brand)
        _set_banner_enter_site_name_if_present(page, brand)


def _refresh_branding_after_embed(page, brand: str):
    """Embed dialog steals focus — always set chrome + banner again so both places stay filled."""
    _dismiss_chrome(page, 2)
    time.sleep(0.28)
    _set_site_title_in_header(page, brand)
    _set_banner_page_title(page, brand)
    _set_banner_enter_site_name_if_present(page, brand)


def _banner_still_has_placeholder(page) -> bool:
    try:
        return page.evaluate(
            """() => {
          const bad = ['click to edit text', 'your page title', 'click to edit'];
          const boxes = document.querySelectorAll('[role="textbox"], [contenteditable="true"]');
          for (const el of boxes) {
            const t = (el.innerText || '').trim().toLowerCase();
            if (bad.some(b => t.includes(b))) return true;
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
    time.sleep(0.4)

    # Fill textarea
    ta = page.locator('div[role="dialog"] textarea').first
    ta.wait_for(state="visible", timeout=8000)
    _fill_textarea_verified(page, ta, premium_html)
    time.sleep(0.35)

    # Click Next
    page.get_by_role("button", name="Next").click(force=True)

    # Wait for preview
    try:
        page.wait_for_selector('div[role="dialog"] iframe', timeout=12000)
    except PlaywrightTimeoutError:
        logger.warning("Preview iframe not detected; continuing")
    time.sleep(1.0)

    # Click Insert
    insert_btn = page.locator('div[role="dialog"] div[role="button"]:has-text("Insert")').first
    insert_btn.click(force=True, timeout=8000)
    
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
        end_y = bottom_y + max(400, min(float(delta_y), 6200))
        page.mouse.move(cx, bottom_y)
        page.mouse.down()
        page.mouse.move(cx, end_y, steps=min(40, max(12, int(delta_y / 110))))
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
        end_x = sx + max(120, min(float(delta_x), 900))
        end_y = sy + max(400, min(float(delta_y), 6200))
        page.mouse.move(sx, sy)
        page.mouse.down()
        page.mouse.move(end_x, end_y, steps=min(38, max(14, int((delta_y + delta_x) / 180))))
        page.mouse.up()
        time.sleep(0.32)
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
    drag_px = max(1300.0, min(6200.0, steps * 2.05))
    dx_budget, dx_wide = _viewport_drag_budget(page)

    resize_loc = _embed_resize_target(page)
    if resize_loc is not None:
        # Bottom-centre pulls — vertical height (paragraph visibility)
        for pass_idx in range(4):
            ok = _drag_bottom_edge_expand(page, resize_loc, drag_px)
            if ok:
                logger.info("Embed bottom-edge drag pass %s (~%.0fpx tall)", pass_idx + 1, drag_px)
            time.sleep(0.26)
            resize_loc = _embed_resize_target(page)

        resize_loc = _embed_resize_target(page)
        if resize_loc is not None:
            # Bottom-right diagonal — height + width (embed often stays narrow until pulled)
            for _ in range(2):
                _drag_bottom_right_corner_expand(page, resize_loc, drag_px * 0.72, dx_budget)
                time.sleep(0.26)
                resize_loc = _embed_resize_target(page)

        resize_loc = _embed_resize_target(page)
        if resize_loc is not None:
            # Right-mid handle — full-bleed width toward reference-site layout
            for _ in range(3):
                _drag_right_edge_expand(page, resize_loc, dx_wide)
                time.sleep(0.24)
                resize_loc = _embed_resize_target(page)

    # Secondary: keyboard widen + height (still helps on some builds)
    time.sleep(0.15)
    for _ in range(14):
        page.keyboard.press("ArrowLeft")
    for _ in range(16):
        page.keyboard.press("Shift+ArrowRight")

    chunk = 220
    remaining = steps
    while remaining > 0:
        batch_sz = min(chunk, remaining)
        page.keyboard.down("Shift")
        for _ in range(batch_sz):
            page.keyboard.press("ArrowDown")
        page.keyboard.up("Shift")
        remaining -= batch_sz
        time.sleep(0.22)


def _click_published_site_dropdown(page) -> bool:
    """
    Open ONLY the 'Published site' row dropdown (Workspace shows org name — must change to Public).
    Does not touch the Draft row.
    """
    return page.evaluate(
        """() => {
      const dlg = document.querySelector('[role="dialog"]');
      if (!dlg) return false;

      const isInfoBanner = (t) =>
        (t || '').includes('Published viewers can see') || (t || '').includes('Learn more about sharing');

      const candidates = Array.from(dlg.querySelectorAll('[role="listitem"], tr, div'));
      for (const block of candidates) {
        const t = block.innerText || '';
        if (!t.includes('Published site')) continue;
        if (isInfoBanner(t)) continue;
        if (t.includes('Draft') && !t.includes('Published site')) continue;

        const ctrls = block.querySelectorAll('[role="button"], [role="combobox"], button');
        for (const c of ctrls) {
          const label = (c.innerText || '').trim();
          const aria = (c.getAttribute('aria-label') || '').toLowerCase();
          if (!label || /^done$/i.test(label) || label.includes('Copy')) continue;
          if (/published site|general access/i.test(aria)) {
            c.click();
            return true;
          }
          if (label.length >= 3 || c.getAttribute('aria-haspopup') === 'listbox' || c.getAttribute('aria-expanded')) {
            c.click();
            return true;
          }
        }
      }

      const spans = Array.from(dlg.querySelectorAll('span, div, p'));
      for (const el of spans) {
        const s = (el.textContent || '').trim();
        if (s !== 'Published site' && !s.startsWith('Published site')) continue;
        if (s.length > 60) continue;
        let row = el.closest('[role="listitem"]') || el.parentElement;
        for (let depth = 0; depth < 10 && row; depth++) {
          const btns = row.querySelectorAll('[role="button"], [role="combobox"]');
          for (const b of btns) {
            const bt = (b.innerText || '').trim();
            if (!bt || /^done$/i.test(bt)) continue;
            if (bt.length >= 3 || b.getAttribute('aria-haspopup')) {
              b.click();
              return true;
            }
          }
          row = row.parentElement;
        }
      }
      return false;
    }"""
    )


def _select_public_or_internet_option(page) -> bool:
    """Pick true public/internet visibility — not Workspace-only / organization."""
    return page.evaluate(
        """() => {
      const opts = Array.from(
        document.querySelectorAll(
          '[role="option"], [role="menuitem"], li[role="option"], div[role="presentation"] li, div[jsname] li'
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
        else if (/anyone with the link/i.test(t)) r = 82;
        else if (/anyone in .* can find|people at .* can/i.test(t)) r = 35;
        else if (/only people.*invited|specific people|restricted/i.test(t)) r = -20;
        else if (/your organization|same organization|people in your domain/i.test(t)) r = -15;
        else if (/\\bltd\\b|\\bpvt\\b|\\binc\\.\\b|technologies/i.test(t) && t.length < 120) r = -40;
        if (r > rank) {
          rank = r;
          best = o;
        }
      }
      if (best && rank >= 75) {
        best.click();
        return true;
      }
      if (best && rank >= 70) {
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
      const dlg = document.querySelector('[role="dialog"]');
      if (!dlg) return false;
      const rows = Array.from(dlg.querySelectorAll('[role="listitem"], tr'));
      for (const row of rows) {
        const t = row.innerText || '';
        if (!t.includes('Published site')) continue;
        const looksOrgOnly =
          /Ltd|Pvt|Technologies|RedOrange|organization/i.test(t) &&
          !/Public on the web|\\bPublic\\b|Anyone with the link/i.test(t);
        if (looksOrgOnly) return false;
        if (/Public on the web|\\bPublic\\b|Anyone with the link/i.test(t)) return true;
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
        'div[role="button"][aria-label*="Share" i]',
        '[data-tooltip*="Share" i]',
        '[aria-label*="share with others" i]',
    ]
    if not _click_first_visible(page, share_selectors, timeout=8000):
        page.mouse.click(1180, 40)
    time.sleep(1.2)

    try:
        page.wait_for_selector('div[role="dialog"]', timeout=DIALOG_TIMEOUT)
    except PlaywrightTimeoutError:
        raise RuntimeError("Share dialog did not appear")

    time.sleep(0.45)

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
        time.sleep(0.4)
        _select_public_or_internet_option(page)

    time.sleep(1.0)

    done_selectors = [
        'button:has-text("Done")',
        'div[role="button"]:has-text("Done")',
    ]
    if not _click_first_visible(page, done_selectors, timeout=6000):
        page.keyboard.press("Escape")
    time.sleep(0.55)


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
    URL-safe slug for the Publish dialog.
    If the slug matches the dhr-001 pattern, we use it exactly as provided
    to ensure the user's sequential requirement is met.
    """
    base = (entry.slug or "").strip().lower()
    if not base:
        base = slugify(branding_title(entry))
    
    # If it's a sequential slug like dhr-001, don't add random suffix
    if re.match(r"^[a-z]+-\d{3,4}$", base):
        return base[:58]

    # Otherwise, add random suffix to avoid collisions for generic slugs
    base = re.sub(r"-{2,}", "-", base).strip("-")[:44]
    suf = random.randint(1000, 9999)
    combined = f"{base}-{suf}"
    if len(combined) > 58:
        combined = f"{base[:38]}-{suf}"
    return combined


def _publish_and_capture_url(page, entry: SiteEntry):
    """Publish and return a validated public URL (never workspace domain roots)."""
    page.mouse.wheel(0, -8000)
    time.sleep(0.2)

    publish_selectors = [
        'div[role="button"]:has-text("Publish")',
        'button:has-text("Publish")',
    ]
    if not _click_first_visible(page, publish_selectors, timeout=6000):
        page.mouse.click(1260, 40)
    time.sleep(0.4)

    final_slug = _build_publish_slug(entry)

    # Ensure dialog is open
    dialog_opened = False
    for _ in range(3):
        if page.locator('div[role="dialog"]').is_visible(timeout=3000):
            dialog_opened = True
            break
        # Re-click if not opened
        _click_first_visible(page, publish_selectors, timeout=2000)
        time.sleep(1.0)

    if dialog_opened:
        try:
            dialog = page.locator('div[role="dialog"]').first
            inp = dialog.locator('input[type="text"], input:not([type="hidden"])').first
            
            def attempt_publish(slug_to_use):
                inp.click(timeout=3000)
                time.sleep(0.2)
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
                time.sleep(0.1)
                page.keyboard.type(slug_to_use, delay=20)
                time.sleep(0.5)
                
                # Use Enter first - it's the most reliable way in Google dialogs
                page.keyboard.press("Enter")
                time.sleep(1.0)

                # Fallback click
                try:
                    pub_btn = dialog.locator('div[role="button"]:has-text("Publish"), button:has-text("Publish"), [jsname="V67oCd"]').last
                    if pub_btn.is_visible(timeout=2000):
                        pub_btn.click(force=True, timeout=2000)
                except:
                    page.keyboard.press("Enter")

                # Wait for dialog to disappear
                for _ in range(5):
                    if not dialog.is_visible(timeout=500):
                        return True
                    time.sleep(1.0)
                    page.keyboard.press("Enter") # Re-try Enter
                return False
                
            success_pub = attempt_publish(final_slug)
            
            # If dialog is STILL visible, the slug was likely taken.
            # Append random suffix and retry.
            if not success_pub and dialog.is_visible(timeout=500):
                logger.warning("Publish dialog still open. Slug might be taken. Retrying with suffix.")
                final_slug = f"{final_slug}-{random.randint(100, 999)}"
                attempt_publish(final_slug)
                
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

    # 2. Theme first — Aristotle resets banner fields; branding is applied immediately after.
    _apply_theme(page)

    # 3. Site name + banner (after theme so placeholders are not wiped)
    _apply_all_branding_after_theme(page, brand)

    # 4. Full HTML embed
    batch.current_action = f"[{index}/{total}] Embedding content..."
    batch.save()
    _insert_embed_content(page, premium_html)
    # (Removed redundant branding refresh to avoid UI jumping)

    # 5. Stretch embed so full paragraph/html shows (no inner scrollbar)
    rsteps = resize_steps_for_embed(premium_html)
    logger.info("Resizing embed block (%s steps)", rsteps)
    _resize_embed_block(page, rsteps)
    page.mouse.wheel(0, -8000)
    time.sleep(0.5)

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
