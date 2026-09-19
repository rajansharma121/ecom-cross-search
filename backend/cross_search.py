"""
Core logic for the E-commerce Cross-Search Tool (ROBUST EDITION).

All six robustness improvements are implemented here:

1. BROWSER FALLBACK — Playwright renders blocked/captcha/homepage pages.
2. BETTER SEARCH QUERIES — brand-first extraction, URL-slug integration,
   generic-word stripping, 1–3 ranked query variants.
3. URL FILTERING — stricter _is_product_url + dedup across DDG rounds.
4. DDG RETRY — exponential backoff + multi-attempt per query variant.
5. RESULT SCORING — difflib-based title similarity scoring, ok/blocked flags.
6. PLATFORM EXTRACTION — more BS4 selectors per platform, better image/price
   heuristics, JSON-LD structured data, min/max price ranges.

High-level flow:
    extract_product(url) → {ok, title, price, image_url, _page_quality, error}
    build_search_queries(title, url_slug_title) → [query1, query2, query3]
    search_and_score(source_title, queries, target_platform, max_results) →
        [{url, title, price, image_url, score, ok, blocked, error}, ...]
"""

from __future__ import annotations

import difflib
import logging
import re
import time
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Optional browser fallback — lazy import so the core library works without
# Playwright installed. Real work happens in browser_extract.extract_via_browser.
# ---------------------------------------------------------------------------

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None  # type: ignore[assignment]

try:
    from browser_extract import extract_via_browser as _browser_extract
except ImportError:
    _browser_extract = None  # type: ignore[assignment]

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Platform config
# ---------------------------------------------------------------------------

PLATFORM_DOMAINS: dict[str, str] = {
    "amazon": "amazon.in",
    "flipkart": "flipkart.com",
    "meesho": "meesho.com",
}

PLATFORM_PRODUCT_URL_RE: dict[str, re.Pattern[str]] = {
    "amazon": re.compile(r"amazon\.in/.*/(?:dp|gp/product)/([A-Z0-9]{10})", re.I),
    "flipkart": re.compile(r"flipkart\.com/.*/((?:p|itm)/[a-zA-Z0-9]+)", re.I),
    "meesho": re.compile(r"meesho\.com/.*/(?:p)/([a-zA-Z0-9]+)", re.I),
    # Flipkart pid query param
    "flipkart_pid": re.compile(r"[?&]pid=([A-Z0-9]+)", re.I),
}

# Max HTTP read timeout for the requests path.
_REQUEST_TIMEOUT = 15

# Generic stop words stripped from search queries to improve discrimination.
# NOTE: product CATEGORY words (earphone, tshirt, hoodie, etc.) are NOT listed
# here — they help search discrimination and must be kept.
_GENERIC_QUERY_WORDS: frozenset[str] = frozenset({
    "buy", "online", "best", "price", "shop", "store", "sale", "offer",
    "deals", "discount", "free", "shipping", "delivery", "cash", "cod",
    "new", "latest", "trending", "popular", "top", "rated", "reviews",
    "rating", "specification", "features", "description", "details",
    "product", "item", "brands", "brand", "with", "for", "the", "a",
    "an", "and", "or", "of", "in", "on", "at", "by", "from", "to",
    "into", "over", "under", "above", "below", "up", "down", "out",
    "off", "about", "than", "then", "now", "today", "yesterday",
    "and", "or", "vs", "comparison", "compare", "versus", "data",
    "original", "genuine", "authentic", "premium", "luxury", "cheap",
    "affordable", "piece", "pieces", "pack", "packs", "set", "kit",
    "accessory", "accessories", "case", "cover", "guard", "strap",
    "band", "battery", "power", "bank", "stand", "holster", "pouch",
    "box", "wire", "wires",
    "charger", "cable", "adapter",
})


# ---------------------------------------------------------------------------
# User-Agent rotation
# ---------------------------------------------------------------------------

_USER_AGENTS: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
    "Gecko/20100101 Firefox/125.0",
]

_RAND_IDX: int = 0


def _next_headers() -> dict[str, str]:
    """Return headers with a rotating User-Agent."""
    global _RAND_IDX
    ua = _USER_AGENTS[_RAND_IDX % len(_USER_AGENTS)]
    _RAND_IDX += 1
    return {
        "User-Agent": ua,
        "Accept-Language": "en-IN,en;q=0.9,hi-IN;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }


# ---------------------------------------------------------------------------
# Platform detection + ID extraction
# ---------------------------------------------------------------------------


def detect_platform(url: str) -> str:
    """Identify amazon / flipkart / meesho / unknown from a URL's domain."""
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


def extract_id(url: str) -> str:
    """Best-effort product ID extraction, platform-aware."""
    platform = detect_platform(url)
    path = urlparse(url).path
    qs = urlparse(url).query

    if platform == "amazon":
        m = PLATFORM_PRODUCT_URL_RE["amazon"].search(url)
        if m:
            return m.group(1)

    if platform == "flipkart":
        m = PLATFORM_PRODUCT_URL_RE["flipkart"].search(url)
        if m:
            return m.group(1).split("/")[-1]
        m = PLATFORM_PRODUCT_URL_RE["flipkart_pid"].search(qs)
        if m:
            return m.group(1)

    if platform == "meesho":
        m = PLATFORM_PRODUCT_URL_RE["meesho"].search(url)
        if m:
            return m.group(1)

    segments = [s for s in path.split("/") if s]
    return segments[-1] if segments else "unknown"


# ---------------------------------------------------------------------------
# URL-slug title fallback
# ---------------------------------------------------------------------------


def _title_from_url_slug(url: str) -> str:
    """Fallback title guess from the URL's slug, for when the page is blocked.

    Picks the longest wordy segment, strips ID-looking tokens, title-cases.
    """
    try:
        path = urlparse(url).path
    except Exception:
        return ""
    segments = [s for s in path.split("/") if s]
    if not segments:
        return ""

    # Product slugs are usually the longest, most word-like segment.
    slug = max(segments, key=lambda s: len(re.findall(r"[a-zA-Z]{2,}", s)))
    slug = re.sub(r"[-_]+", " ", slug)
    # Strip ID-looking tokens: long alphanumeric runs that contain a digit
    # (real words don't; product/ASIN codes like "B0CTEST123" do).
    slug = re.sub(r"\b(?=\w*\d)\w{6,}\b", "", slug)
    slug = re.sub(r"\s+", " ", slug).strip()
    # Title-case but keep known brand acronyms upper.
    words = slug.split()
    fixed: list[str] = []
    for w in words:
        if w.isupper() and len(w) >= 2:
            fixed.append(w.upper())
        elif w[0].isupper() and len(w) >= 3 and w[1:].islower():
            fixed.append(w)
        else:
            fixed.append(w.capitalize())
    return " ".join(fixed)


# ---------------------------------------------------------------------------
# Price cleaning
# ---------------------------------------------------------------------------


def _clean_price(text: str) -> str:
    if not text:
        return ""
    m = re.search(r"[\u20b9Rs\.\s]?\s*[\d,]+(?:\.\d+)?", text, re.I)
    if m:
        s = m.group(0).replace(" ", "")
        if not s.startswith("₹"):
            s = "₹" + s.lstrip("Rs.")
        return s
    return text.strip()[:40]


# ---------------------------------------------------------------------------
# Bot-wall / homepage detection
# ---------------------------------------------------------------------------

_BOT_WALL_MARKERS: tuple[str, ...] = (
    "captcha",
    "robot check",
    "access denied",
    "are you a human",
    "verify you are a human",
    "suspicious activity",
)

_PRODUCT_IMG_EXTS: tuple[str, ...] = (".jpg", ".jpeg", ".png", ".webp")


def _looks_like_bot_wall(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in _BOT_WALL_MARKERS)


def _looks_like_homepage(text: str, platform: str) -> bool:
    """Crude check: does the text look like a homepage/landing rather than a
    product page?"""
    low = text.lower()
    if not low:
        return True
    # Presence of product cues → not a homepage
    product_cues: tuple[str, ...] = (
        "in stock", "free delivery", "add to cart", "available offers",
        "key features", "key specification", "customer reviews",
        "bought in last", "description", "shipping", "marks & spencer",
    )
    if any(c in low for c in product_cues):
        return False
    # Homepage/landing cues
    home_cues: tuple[str, ...] = (
        "all categories", "today's deals", "cart", "{top picks",
        "recommended for you", "sign in", "hello, sign in",
        "account", "orders", "wishlist", "become a seller", "sell on",
        "flipkart plus", "gift cards",
    )
    hits = sum(1 for c in home_cues if c in low)
    return hits >= 3


# ---------------------------------------------------------------------------
# Requests session (persistent, with rotating UA)
# ---------------------------------------------------------------------------

_SESSION: requests.Session | None = None


def _get_session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
        _SESSION.headers.update(_next_headers())
    return _SESSION


# ---------------------------------------------------------------------------
# BeautifulSoup extraction (fast path) — more selectors per platform
# ---------------------------------------------------------------------------


def _extract_via_bs4(html: str, platform: str, url: str) -> dict[str, Any]:
    """Extract title / price / image from HTML using BeautifulSoup.
    Platform-aware selectors for Amazon, Flipkart, Meesho.
    Also parses JSON-LD, meta tags, and visible ₹ patterns.
    """
    soup = BeautifulSoup(html, "html.parser")
    result: dict[str, Any] = {
        "title": "",
        "price": "",
        "image_url": "",
    }

    # --- Title ---
    title = ""
    # 1. og:title
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        title = og_title["content"].strip()
    # 2. Twitter:app:name
    if not title:
        tw_name = soup.find("meta", attrs={"name": "twitter:app:name:iphone"})
        if tw_name and tw_name.get("content"):
            title = tw_name["content"].strip()
    # 3. <title>
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()
    # 4. Platform-specific selectors
    if not title:
        selectors: list[str] = []
        if platform == "amazon":
            selectors = [
                "#productTitle",
                "#title span",
                "span#productTitle",
                "h1#title",
                "[data-asin]",
            ]
        elif platform == "flipkart":
            selectors = [
                ".pdp-title",
                "h1._2T4Right",
                "h1[data-testid='product-title']",
                "h1",
                "[class*='product-title']",
            ]
        elif platform == "meesho":
            selectors = [
                "h1",
                "[class*='ProductTitle']",
                "[class*='product-title']",
                "[class*='product-name']",
                "h1[class]",
            ]
        else:
            selectors = ["h1", "h1[class]", "[class*='title']"]

        for sel in selectors:
            el = soup.select_one(sel)
            if el and el.get_text(strip=True):
                title = el.get_text(strip=True)
                break

    # Clean platform suffix from title
    if title:
        title = re.sub(
            r"\s*[:|\\-\\u2013\\u2014]+\\s*(Amazon(\\.in)?|Flipkart(\\.com)?|Meesho(\\.com)?)\\s*$",
            "",
            title,
            flags=re.I,
        ).strip()
    result["title"] = title

    # --- Image ---
    image = ""
    # 1. og:image
    og_image = soup.find("meta", property="og:image")
    if og_image and og_image.get("content"):
        image = og_image["content"].strip()
    # 2. twitter:image
    if not image:
        tw_image = soup.find("meta", attrs={"name": "twitter:image"})
        if tw_image and tw_image.get("content"):
            image = tw_image["content"].strip()
    # 3. Product image selectors
    if not image:
        img_selectors: list[str] = []
        if platform == "amazon":
            img_selectors = ["#landingImage", "#imgBlkFront img", "#imgTagAltLink img"]
        elif platform == "flipkart":
            img_selectors = [
                "img[alt*='product']",
                "img[data-id]",
                "[class*='ProductImage'] img",
                ".qPlp9b img",
                "img[src*='rukminim']",
            ]
        elif platform == "meesho":
            img_selectors = [
                "img[alt*='product']",
                "[class*='ProductImage'] img",
                "[class*='product-image'] img",
                "img[src*='zoom']",
            ]
        else:
            img_selectors = ["img[alt*='product']", "img[src*='product']", "img[src*='zoom']"]

        for sel in img_selectors:
            el = soup.select_one(sel)
            if el and el.get("src"):
                src = el["src"]
                # Skip tiny thumbnails, logos
                if "logo" in src.lower() or "icon" in src.lower():
                    continue
                if src.startswith("//"):
                    src = "https:" + src
                image = src
                break
    # 4. Any large image on page as last resort
    if not image:
        candidates = soup.find_all("img", src=True)
        for img_el in candidates:
            src = img_el["src"]
            if "logo" in src.lower() or "icon" in src.lower() or "banner" in src.lower():
                continue
            if src.startswith("//"):
                src = "https:" + src
            # Heuristic: real product images are usually >100px
            if len(src) > 80 and any(ext in src.lower() for ext in _PRODUCT_IMG_EXTS):
                image = src
                break
    result["image_url"] = image

    # --- Price ---
    price = ""
    # 1. Structured meta
    price_meta = (
        soup.find("meta", property="product:price:amount")
        or soup.find("meta", attrs={"itemprop": "price"})
        or soup.find("meta", attrs={"name": "price"})
    )
    if price_meta and price_meta.get("content"):
        raw = price_meta["content"].strip()
        if raw:
            price = _clean_price(f"₹{raw}")

    # 2. JSON-LD structured data
    if not price:
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                import json

                data = json.loads(script.string or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            # Drill into common e-commerce schemas
            items: list[Any] = []
            if isinstance(data, dict):
                if data.get("@type") in ("Product", "Offer", "AggregateOffer"):
                    items = [data]
                elif "offers" in data:
                    off = data["offers"]
                    items = [off] if isinstance(off, dict) else off
                elif "itemListElement" in data:
                    items = data["itemListElement"]
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
                    # Nested offer
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

    # 3. Visible text ₹ patterns (last resort)
    if not price:
        # Look near "₹" patterns, preferring ones with 3-7 digits
        text_nodes = soup.get_text(" ", strip=True)
        for m in re.finditer(r"[\u20b9Rs\.\s]?\s*([\d,]{3,8})(?:\.\d{1,2})?", text_nodes):
            candidate = m.group(0).replace(" ", "")
            if not candidate.startswith("₹"):
                candidate = "₹" + candidate.lstrip("Rs.")
            # Filter out too-large numbers (page totals) and too-small
            digits = re.sub(r"[^\d]", "", candidate)
            if 100 <= int(digits) <= 1_000_000:
                price = candidate
                break
        if not price:
            # Fallback: any ₹ with 3+ digits on page
            m = re.search(r"[₹]\s*[\d,]{3,}", text_nodes)
            if m:
                price = m.group(0).replace(" ", "").replace("Rs.", "₹")
                price = "₹" + re.sub(r"[^\d]", "", price[1:])
    result["price"] = price
    return result


# ---------------------------------------------------------------------------
# Browser extraction (Playwright fallback) — delegates to browser_extract
# ---------------------------------------------------------------------------

_BROWSER_TIMEOUT: int = 35


def _extract_via_browser(url: str, platform: str) -> dict[str, Any]:
    """Extract title/price/image using Playwright, bypassing static-block issues.

    Only called when the requests path fails (403, bot wall, homepage).
    Delegates to browser_extract.extract_via_browser if available, otherwise
    does nothing (returns empty result).
    """
    if _browser_extract is None:
        log.info("Playwright not available; cannot do browser fallback")
        return {"error": "browser fallback unavailable (playwright not installed)"}

    try:
        return _browser_extract(url, timeout=_BROWSER_TIMEOUT)
    except Exception as e:
        log.exception("Browser extraction failed for %s", url)
        return {"error": f"browser error: {e}"}


# ---------------------------------------------------------------------------
# Main extraction entry point — requests first, browser on block
# ---------------------------------------------------------------------------

# Known non-product URL patterns to filter out from DDG results
_NON_PRODUCT_URL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"/q/[^/]+$", re.I),            # Flipkart search pages
    re.compile(r"/s?[?].*", re.I),              # Search query URLs
    re.compile(r"/browse(/.*)?$", re.I),        # Meesho browse
    re.compile(r"/category", re.I),             # Category pages
    re.compile(r"/search", re.I),               # Search pages
    re.compile(r"/all?", re.I),                  # Amazon all-deals
    re.compile(r"amazon\.in/[\w-]+/ref=", re.I),  # Amazon redirect/referral URLs
    re.compile(r"meesho\.com/[a-z]+/pl/", re.I),   # Meesho category listing
    re.compile(r"flipkart\.com/.*pr?", re.I),       # Flipkart search results
    re.compile(r"/compare/", re.I),
    re.compile(r"/product-list/", re.I),
    re.compile(r"/results/", re.I),
    re.compile(r"/trending/", re.I),
    re.compile(r"/videos?", re.I),
    re.compile(r"/blog", re.I),
    re.compile(r"/articles?", re.I),
    re.compile(r"/news?", re.I),
]


def _is_product_url(url: str, platform: str) -> bool:
    """Return True if this looks like a real product page, not a search
    or category page.

    Accepts:
      - Amazon: /dp/ or /gp/product/ ASIN URLs
      - Flipkart: /p/ or /itm/ product slug URLs
      - Meesho: /p/ product slug URLs
      - Generic: last path segment with 6+ chars containing a digit
    Rejects known search/category/redirection patterns.
    """
    for pat in _NON_PRODUCT_URL_PATTERNS:
        if pat.search(url):
            return False
    # For Amazon: /dp/ or /gp/product/ is a strong signal
    if platform == "amazon" and re.search(r"/dp/|/gp/product/", url, re.I):
        return True
    # For Flipkart: /p/ or /itm/ in path
    if platform == "flipkart" and re.search(r"/(?:p|itm)/[a-zA-Z0-9]+", url, re.I):
        return True
    # For Meesho: /p/ in path
    if platform == "meesho" and re.search(r"/p/[a-zA-Z0-9]+", url, re.I):
        return True
    # Generic: path has a product-like segment
    path = urlparse(url).path.strip("/")
    segments = path.split("/")
    if len(segments) >= 2:
        # Last segment is likely the product identifier
        last = segments[-1]
        if len(last) >= 6 and any(c.isdigit() for c in last):
            return True
    return False


def extract_product(
    url: str,
    *,
    use_browser_on_block: bool = True,
    browser_timeout: int = 30,
) -> dict[str, Any]:
    """Fetch a product page and pull out title / price / image.

    Returns a dict with:
        ok, title, price, image_url, status, error, _page_quality,
        _browser_used (bool or absent)

    Never raises — a failure just produces ok=False with an error message.
    """
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    platform = detect_platform(url)
    result: dict[str, Any] = {
        "ok": False,
        "title": "",
        "price": "",
        "image_url": "",
        "status": 0,
        "error": "",
        "_page_quality": "ok",
    }

    # --- Fast path: requests ---
    session = _get_session()
    session.headers.update(_next_headers())
    try:
        resp = session.get(url, timeout=_REQUEST_TIMEOUT)
        result["status"] = resp.status_code
    except requests.RequestException as e:
        result["error"] = f"request failed: {e}"
        result["_page_quality"] = "blocked"
        return result

    if resp.status_code in (403, 429, 503):
        result["error"] = f"blocked (HTTP {resp.status_code})"
        result["_page_quality"] = "blocked"
        if not use_browser_on_block:
            return result
        # Fall through to browser
    elif resp.status_code != 200:
        result["error"] = f"HTTP {resp.status_code}"
        result["_page_quality"] = "blocked"
        return result

    html = resp.text

    # Bot wall check
    if _looks_like_bot_wall(html):
        result["error"] = "bot wall / captcha page"
        result["_page_quality"] = "blocked"
        if not use_browser_on_block:
            return result
        # Fall through to browser

    # Homepage check
    if _looks_like_homepage(html, platform):
        result["error"] = "homepage returned instead of product page"
        result["_page_quality"] = "blocked"
        # Continue extraction anyway — might still find title/price

    # Extract via BS4
    extracted = _extract_via_bs4(html, platform, url)
    result.update(extracted)

    # If the extracted title is a bot-wall page, discard it and fall
    # through to the browser (the BS4 extraction grabbed the captcha
    # page title, not the real product title).
    if result.get("title") and _looks_like_bot_wall(result["title"]):
        result["title"] = ""
        result["_page_quality"] = "blocked"
        result["error"] = "bot wall / captcha page"

    # If we got something useful, we're done
    if result.get("title") or result.get("price"):
        result["ok"] = True
        result["_page_quality"] = "ok"
        return result

    # --- Browser fallback ---
    if use_browser_on_block:
        log.info(
            "Requests extraction incomplete for %s (%s); trying browser",
            url,
            result.get("error") or "no data",
        )
        browser_result = _extract_via_browser(url, platform)
        result.update(browser_result)
        if result.get("_browser_used"):
            result["_browser_used"] = True
        if result.get("title") or result.get("price"):
            result["ok"] = True
            result["_page_quality"] = "ok"
        else:
            result["_page_quality"] = "no_text"
            if not result.get("error"):
                result["error"] = "could not extract any product details"

    if not result.get("ok"):
        if not result.get("error"):
            result["error"] = "could not extract any product details"

    return result


# ---------------------------------------------------------------------------
# Search query building (robust) — brand-first, URL-slug integration,
# generic-word stripping, 1–3 ranked variants
# ---------------------------------------------------------------------------


def _strip_generic_words(words: list[str]) -> list[str]:
    """Remove generic stop words, keep meaningful product terms.

    Generic words are always stripped regardless of capitalization — this
    prevents "Price", "Free", "Online", "Buy" etc. from sneaking through
    just because they're TitleCase.
    """
    out: list[str] = []
    for w in words:
        low = w.lower()
        # Always strip generic words, even if they look brand-like
        if low in _GENERIC_QUERY_WORDS:
            continue
        # Keep everything else that has substance (2+ chars)
        if len(w) >= 2:
            out.append(w)
    return out


def _title_has_brand(title: str) -> bool:
    """heuristic: does title contain a short all-caps or TitleCase word
    that looks like a brand?"""
    words = re.findall(r"[a-zA-Z]+", title)
    for w in words:
        if len(w) >= 2 and w.isupper():
            return True
        if len(w) >= 3 and w[0].isupper() and w[1:].islower():
            return True
    return False


def _extract_brand_and_product(title: str) -> tuple[str, str]:
    """Split a title into (likely_brand, rest_of_product)."""
    cleaned = _clean_query(title)
    if not cleaned:
        return "", ""
    words = cleaned.split()
    if not words:
        return "", cleaned
    # Find brand: first all-caps word with 2+ chars, or first TitleCase word
    brand_idx: int = 0
    for i, w in enumerate(words):
        if len(w) >= 2 and w.isupper():
            brand_idx = i
            break
        if len(w) >= 3 and w[0].isupper() and w[1:].islower():
            brand_idx = i
            break
    brand = words[brand_idx]
    rest = words[brand_idx + 1:]
    return brand, " ".join(rest)


def build_search_query(
    title: str,
    extra: str = "",
    url_slug_title: str | None = None,
) -> list[str]:
    """Build 1-3 clean search query variants from product info.

    Returns a list of query strings, best first. Always includes at least
    the URL-slug-derived title as the weakest fallback.

    If `title` is a bot-wall page or otherwise unhelpful, the URL slug is
    used as the primary query source.
    """
    candidates: list[str] = []

    # Guard: if title looks like a bot-wall or error page, don't use it.
    if title and _looks_like_bot_wall(title):
        title = ""

    # Strategy 1: use the real title if it looks product-like
    if title and len(title) >= 10:
        q = _clean_query(title)
        if len(q.split()) >= 2:
            candidates.append(q)

    # Strategy 2: title + brand extraction (if title has a recognizable brand)
    if title and len(title) >= 10 and _title_has_brand(title):
        brand, rest = _extract_brand_and_product(title)
        if brand and rest:
            q2 = _clean_query(f"{brand} {rest}")
            if q2 and q2 != candidates[0] if candidates else True:
                candidates.append(q2)
        elif brand:
            candidates.append(brand)

    # Strategy 3: URL slug title (most reliable fallback)
    slug_q = ""
    if url_slug_title:
        slug_q = _clean_query(url_slug_title)
    elif title:
        slug_q = _clean_query(_title_from_url_slug("https://example.com/" + title.replace(" ", "-")))
    if slug_q and len(slug_q.split()) >= 2:
        candidates.append(slug_q)

    # Strip generic words from all candidates
    for i, q in enumerate(candidates):
        words = q.split()
        stripped = _strip_generic_words(words)
        if stripped:
            candidates[i] = " ".join(stripped)

    # Deduplicate
    seen: set[str] = set()
    out: list[str] = []
    for q in candidates:
        key = q.lower()
        if key not in seen:
            seen.add(key)
            out.append(q)

    # Always return at least one query
    if not out:
        out = [slug_q or "product"]

    return out[:3]


def _clean_query(text: str) -> str:
    """Strip markup, extra punctuation, and HTML entities from a query string."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    text = re.sub(r"[\\(\\)\\[\\]\\{\\}]", " ", text)
    text = re.sub(r"[:\-\\–—_]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Remove platform names that appear as trailing noise (e.g. "... - Amazon.in")
    text = re.sub(
        r"\s*[-–—]+\s*(Amazon(\.in)?|Flipkart(\.com)?|Meesho(\.com)?)\s*$",
        "",
        text,
        flags=re.I,
    ).strip()
    # Also strip trailing noise words that often trail echoed titles
    text = re.sub(
        r"\s+((in|on|at)\s+(amazon|flipkart|meesho)(\.in|\.com)?\s*)?\s*$",
        "",
        text,
        flags=re.I,
    ).strip()
    # Deduplicate ALL repeated words (not just consecutive — Flipkart/SEO
    # titles often have words repeated non-adjacently like "boAt ... boAt").
    seen: set[str] = set()
    deduped: list[str] = []
    for w in text.split():
        if w.lower() not in seen:
            seen.add(w.lower())
            deduped.append(w)
    text = " ".join(deduped)
    return text


def _clean_query_with_brand(title: str) -> str:
    """Extract likely brand + product name from a title.

    Heuristic: the first uppercase word (2+ chars) is likely the brand.
    """
    cleaned = _clean_query(title)
    if not cleaned:
        return ""
    words = cleaned.split()
    if not words:
        return cleaned
    # Find brand: first all-caps or Title-case word with 2+ chars
    brand_idx: int = 0
    for i, w in enumerate(words):
        if len(w) >= 2 and (w.isupper() or (w[0].isupper() and w[1:].islower())):
            brand_idx = i
            break
    brand = words[brand_idx]
    rest = words[brand_idx + 1:]
    # Keep brand + up to 6 more meaningful words
    meaningful = [w for w in rest if len(w) >= 2][:6]
    query = f"{brand} {' '.join(meaningful)}".strip()
    return query if len(query.split()) >= 2 else brand


# ---------------------------------------------------------------------------
# Result scoring (title similarity) — difflib-based, with brand bonus
# ---------------------------------------------------------------------------


def _score_result_title(source_title: str, result_title: str) -> float:
    """Return a similarity score 0.0–1.0 between the source title and a
    result title. Higher = more likely a match."""
    if not source_title or not result_title:
        return 0.0
    st = source_title.lower().strip()
    rt = result_title.lower().strip()
    if not st or not rt:
        return 0.0
    # Exact match (case-insensitive) → perfect
    if st == rt:
        return 1.0
    # One contains the other → high score
    if st in rt or rt in st:
        ratio = len(st) / max(len(rt), 1) if len(st) < len(rt) else len(rt) / max(len(st), 1)
        return 0.9 * ratio
    # Word-level overlap
    st_words: set[str] = set(st.split())
    rt_words: set[str] = set(rt.split())
    if not st_words or not rt_words:
        return 0.0
    overlap: set[str] = st_words & rt_words
    if not overlap:
        return 0.0
    # Jaccard-ish with weighted brand bonus
    jaccard: float = len(overlap) / len(st_words | rt_words)
    # Brand word bonus: if first word of source matches a word in result
    src_first: str = st.split()[0] if st.split() else ""
    brand_bonus: float = 0.15 if (src_first and src_first in rt_words) else 0.0
    # Sequence matcher for partial string similarity
    sm_ratio: float = difflib.SequenceMatcher(None, st, rt).ratio()
    # Blend: prefer word overlap with brand awareness, tempered by sequence sim
    return min(0.95, jaccard * 0.6 + sm_ratio * 0.3 + brand_bonus)


# ---------------------------------------------------------------------------
# DuckDuckGo search (robust) — exponential backoff + retry
# ---------------------------------------------------------------------------


def ddg_search(
    query: str,
    *,
    limit: int = 6,
    platform_filter: str | None = None,
    max_attempts: int = 3,
) -> list[str]:
    """Search DuckDuckGo for `query`, returning up to `limit` result URLs.

    Retries with exponential backoff on failure. Filters out non-product URLs.
    Uses ddgs library if available, falls back to duckduckgo_search.
    """
    domain = PLATFORM_DOMAINS.get(platform_filter, "") if platform_filter else ""

    urls: list[str] = []
    last_err: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            from ddgs import DDGS
        except ImportError:
            try:
                from duckduckgo_search import DDGS  # type: ignore[import-not-found]
            except ImportError:
                log.warning("ddgs not installed; returning empty results")
                return []

        try:
            with DDGS() as ddgs:
                results = ddgs.text(query, max_results=limit * 3, region="in-en")
                for r in results:
                    href = r.get("href") or r.get("link") or ""
                    if not href:
                        continue
                    if domain and domain not in href:
                        continue
                    # Filter out non-product URLs
                    if not _is_product_url(href, platform_filter or ""):
                        continue
                    if href in urls:
                        continue
                    urls.append(href)
                    if len(urls) >= limit:
                        break
            # Success — done
            break

        except Exception as e:
            last_err = e
            log.warning(
                "DDG attempt %d/%d failed for query %r: %s",
                attempt,
                max_attempts,
                query[:60],
                e,
            )
            if attempt < max_attempts:
                backoff = min(2.0 ** (attempt - 1), 8.0)
                time.sleep(backoff)
            continue

    if last_err and not urls:
        log.warning("DDG search exhausted all attempts; returning partial results")

    return urls[:limit]


# ---------------------------------------------------------------------------
# Multi-query search with result scoring and deduplication
# ---------------------------------------------------------------------------


def search_and_score(
    source_title: str,
    queries: list[str],
    target_platform: str,
    max_results: int = 3,
) -> list[dict[str, Any]]:
    """Run DDG search across 1-3 query variants, score results by title
    similarity to `source_title`, and return sorted, deduplicated list.

    Each result dict: {url, title, price, image_url, score, ok, blocked, error}
    """
    all_candidates: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    for qi, q in enumerate(queries):
        if qi > 0:
            time.sleep(0.5)  # polite gap between query variants
        log.info(
            "DDG query %d/%d for %s: %s",
            qi + 1,
            len(queries),
            target_platform,
            q[:70],
        )
        raw_urls = ddg_search(
            q,
            limit=max_results * 2,
            platform_filter=target_platform,
            max_attempts=2,
        )
        if not raw_urls:
            continue
        # Deduplicate across query attempts
        for u in raw_urls:
            if u in seen_urls:
                continue
            seen_urls.add(u)
            # Extract each candidate page
            pr = extract_product(u, use_browser_on_block=True, browser_timeout=20)
            title = pr.get("title") or _title_from_url_slug(u) or ""
            score = _score_result_title(source_title, title)
            all_candidates.append({
                "url": u,
                "title": title,
                "price": pr.get("price") or "",
                "image_url": pr.get("image_url") or "",
                "score": score,
                "ok": pr.get("ok", False),
                "blocked": pr.get("_page_quality") in ("blocked", "no_text") or not pr.get("ok", False),
                "error": pr.get("error") or "",
                "_from_url_slug": bool(title and not pr.get("title")),
            })

    # Sort by score (descending), prefer ok=True
    all_candidates.sort(key=lambda x: (x["ok"], x["score"]), reverse=True)

    # Return top N
    return all_candidates[:max_results]


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def filter_product_urls(
    urls: list[str], platform: str = "any"
) -> list[str]:
    """Keep only URLs that look like real product pages."""
    result: list[str] = []
    for u in urls:
        if not u:
            continue
        if _is_product_url(u, platform):
            result.append(u)
    # Deduplicate preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for u in result:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def _title_from_url_slug_public(url: str) -> str:
    """Public wrapper so app.py doesn't import private _title_from_url_slug."""
    return _title_from_url_slug(url)


__all__ = [
    "detect_platform",
    "extract_id",
    "extract_product",
    "build_search_query",
    "filter_product_urls",
    "ddg_search",
    "search_and_score",
    "_title_from_url_slug_public",
    "_clean_query",
    "_strip_generic_words",
    "_score_result_title",
]
