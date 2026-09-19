"""
Playwright browser extraction for blocked/captcha/homepage product pages.

Only imported and used by cross_search.extract_product() when the requests
path fails. Kept in a separate module so the core library doesn't require
Playwright at import time.
"""

from __future__ import annotations

import logging
import time
import re
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

# Same user agents and headers as cross_search so the browser looks consistent.
_USER_AGENTS = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
        "Gecko/20100101 Firefox/125.0"
    ),
]

_BOT_WALL_MARKERS = (
    "captcha",
    "robot check",
    "access denied",
    "are you a human",
    "verify you are a human",
    "suspicious activity",
    "enter the characters",
    "typing test",
    "select all images",
)


def _looks_like_bot_wall(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in _BOT_WALL_MARKERS)


_PLATFORM_DOMAINS = {
    "amazon": "amazon.in",
    "flipkart": "flipkart.com",
    "meesho": "meesho.com",
}


def extract_via_browser(
    url: str,
    *,
    timeout: int = 40,
) -> dict[str, Any]:
    """Navigate `url` with Playwright, dismiss common popups, then extract
    title / price / image from the rendered DOM.

    Returns a dict with ok, title, price, image_url, _browser_used, error.
    Never raises on its own — a failure produces ok=False + an error message.
    """
    if sync_playwright is None:
        return {
            "ok": False,
            "title": "",
            "price": "",
            "image_url": "",
            "error": "browser fallback unavailable (playwright not installed)",
            "_browser_used": False,
        }

    platform = _detect_platform(url)
    result: dict[str, Any] = {
        "ok": False,
        "title": "",
        "price": "",
        "image_url": "",
        "error": "",
        "_browser_used": True,
    }

    browser = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=_USER_AGENTS[0],
                locale="en-IN",
                viewport={"width": 1366, "height": 768},
                extra_http_headers={
                    "Accept-Language": "en-IN,en;q=0.9,hi-IN;q=0.8",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
            page = ctx.new_page()

            # ---- Navigate (try domcontentloaded first, then load) ----
            load_ok = False
            for wait_until, to in (("domcontentloaded", 22000), ("load", 30000)):
                try:
                    page.goto(url, wait_until=wait_until, timeout=to)
                    load_ok = True
                    break
                except Exception:
                    continue
            if not load_ok:
                result["error"] = "page did not load in browser"
                return result

            # ---- Wait for network to settle (best-effort) ----
            for _ in range(3):
                try:
                    page.wait_for_load_state("networkidle", timeout=2000)
                    break
                except Exception:
                    time.sleep(1)

            # ---- Bot-wall check ----
            body = page.content()
            if _looks_like_bot_wall(body):
                result["error"] = "bot wall / captcha detected even in browser"
                return result

            # ---- Dismiss common popups / consent banners ----
            dismiss_selectors = [
                "button:has-text('Accept all')",
                "button:has-text('Accept')",
                "button:has-text('I agree')",
                "button:has-text('I understand')",
                "button:has-text('Continue')",
                "button:has-text('Close')",
                "button:has-text('Got it')",
                "button:has-text('Reject')",
                "[aria-label*='close']",
                "[aria-label*='Close']",
                "[class*='cookie-banner'] button",
                "[class*='consent'] button",
                "[class*='popup'] button",
                "div[role='dialog'] button",
            ]
            dismissed = False
            for sel in dismiss_selectors:
                try:
                    btn = page.query_selector(sel)
                    if btn:
                        btn.click()
                        time.sleep(0.6)
                        dismissed = True
                        break
                except Exception:
                    continue
            if dismissed:
                time.sleep(0.8)

            # ---- Re-scrape after dismissal ----
            body = page.content()

            # ---- Extract via BS4 ----
            extracted = _extract_via_bs4(body, platform, url)
            result.update(extracted)

            # ---- Quality gate ----
            if result.get("title") or result.get("price"):
                result["ok"] = True
            else:
                result["error"] = result.get("error") or "browser extraction returned no title or price"

            return result

    except Exception as e:
        log.exception("Playwright extraction failed for %s", url)
        result["error"] = f"browser error: {e}"
        return result
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


def _detect_platform(url: str) -> str:
    """Best-effort platform detection for the browser fallback."""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return "unknown"
    if "amazon." in host:
        return "amazon"
    if "flipkart.com" in host:
        return "flipkart"
    if "meesho.com" in host:
        return "meesho"
    return "unknown"


def _extract_via_bs4(html: str, platform: str, url: str) -> dict[str, Any]:
    """Same extraction logic as cross_search._extract_via_bs4, duplicated
    here so browser_extract can stand alone without importing cross_search."""
    soup = BeautifulSoup(html, "html.parser")
    result: dict[str, Any] = {"title": "", "price": "", "image_url": ""}

    # --- Title ---
    title = ""
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        title = og_title["content"].strip()
    if not title:
        tw = soup.find("meta", attrs={"name": "twitter:app:name:iphone"})
        if tw and tw.get("content"):
            title = tw["content"].strip()
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()

    if not title:
        selectors: list[str] = []
        if platform == "amazon":
            selectors = ["#productTitle", "#title span", "span#productTitle", "h1#title", "[data-asin]"]
        elif platform == "flipkart":
            selectors = [".pdp-title", "h1._2T4Right", "h1[data-testid='product-title']", "h1", "[class*='product-title']"]
        elif platform == "meesho":
            selectors = ["h1", "[class*='ProductTitle']", "[class*='product-title']", "[class*='product-name']", "h1[class]"]
        else:
            selectors = ["h1", "h1[class]", "[class*='title']"]
        for sel in selectors:
            el = soup.select_one(sel)
            if el and el.get_text(strip=True):
                title = el.get_text(strip=True)
                break

    if title:
        title = re.sub(r"\s*[:|\-–—]+\s*(Amazon(\.in)?|Flipkart(\.com)?|Meesho(\.com)?)\s*$", "", title, flags=re.I).strip()
    result["title"] = title

    # --- Image ---
    image = ""
    og_img = soup.find("meta", property="og:image")
    if og_img and og_img.get("content"):
        image = og_img["content"].strip()
    if not image:
        tw_img = soup.find("meta", attrs={"name": "twitter:image"})
        if tw_img and tw_img.get("content"):
            image = tw_img["content"].strip()
    if not image:
        img_selectors: list[str] = []
        if platform == "amazon":
            img_selectors = ["#landingImage", "#imgBlkFront img", "#imgTagAltLink img"]
        elif platform == "flipkart":
            img_selectors = ["img[alt*='product']", "img[data-id]", "[class*='ProductImage'] img", ".qPlp9b img", "img[src*='rukminim']"]
        elif platform == "meesho":
            img_selectors = ["img[alt*='product']", "[class*='ProductImage'] img", "[class*='product-image'] img", "img[src*='zoom']"]
        else:
            img_selectors = ["img[alt*='product']", "img[src*='product']", "img[src*='zoom']"]
        for sel in img_selectors:
            el = soup.select_one(sel)
            if el and el.get("src"):
                src = el["src"]
                if "logo" in src.lower() or "icon" in src.lower():
                    continue
                if src.startswith("//"):
                    src = "https:" + src
                image = src
                break
    if not image:
        for img_el in soup.find_all("img", src=True):
            src = img_el["src"]
            if "logo" in src.lower() or "icon" in src.lower() or "banner" in src.lower():
                continue
            if src.startswith("//"):
                src = "https:" + src
            if len(src) > 80 and any(ext in src.lower() for ext in (".jpg", ".jpeg", ".png", ".webp")):
                image = src
                break
    result["image_url"] = image

    # --- Price ---
    price = ""
    pm = soup.find("meta", property="product:price:amount") or soup.find("meta", attrs={"itemprop": "price"}) or soup.find("meta", attrs={"name": "price"})
    if pm and pm.get("content"):
        raw = pm["content"].strip()
        if raw:
            price = _clean_price(f"₹{raw}")
    if not price:
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                import json as _json
                data = _json.loads(script.string or "{}")
            except (_json.JSONDecodeError, TypeError):
                continue
            items: list[Any] = []
            if isinstance(data, dict):
                if data.get("@type") in ("Product", "Offer", "AggregateOffer"):
                    items = [data]
                elif "offers" in data:
                    off = data["offers"]
                    items = [off] if isinstance(off, dict) else off
            for item in items:
                if isinstance(item, dict):
                    p = item.get("price") or item.get("priceCurrency", "")
                    if isinstance(p, (int, float)) and p > 0:
                        price = f"₹{int(p):,}"
                        break
                    if isinstance(p, str) and re.search(r"\d", p):
                        price = _clean_price(p)
                        if price:
                            break
                    off = item.get("offers")
                    if isinstance(off, dict):
                        p2 = off.get("price")
                        if isinstance(p2, (int, float)) and p2 > 0:
                            price = f"₹{int(p2):,}"
                            break
                        p2s = off.get("price")
                        if p2s and isinstance(p2s, str) and re.search(r"\d", p2s):
                            price = _clean_price(p2s.replace(" ", ""))
                            if price:
                                break
    if not price:
        text = soup.get_text(" ", strip=True)
        for m in re.finditer(r"[₹Rs\.\s]?\s*([\d,]{3,8})(?:\.\d{1,2})?", text):
            cand = m.group(0).replace(" ", "")
            if not cand.startswith("₹"):
                cand = "₹" + cand.lstrip("Rs.")
            digits = re.sub(r"[^\d]", "", cand)
            if 100 <= int(digits) <= 1_000_000:
                price = cand
                break
        if not price:
            m = re.search(r"[₹]\s*[\d,]{3,}", text)
            if m:
                price = "₹" + re.sub(r"[^\d]", "", m.group(0))
    result["price"] = price

    return result


def _clean_price(text: str) -> str:
    if not text:
        return ""
    m = re.search(r"[₹Rs\.\s]?\s*[\d,]+(?:\.\d+)?", text, re.I)
    if m:
        s = m.group(0).replace(" ", "")
        if not s.startswith("₹"):
            s = "₹" + s.lstrip("Rs.")
        return s
    return text.strip()[:40]


__all__ = ["extract_via_browser", "sync_playwright"]
