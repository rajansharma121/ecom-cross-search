"""
Robust E-commerce Cross-Search Tool — backend (Flask).

Serves:
  - GET  /              → index.html (the dashboard)
  - POST /api/search    → JSON product lookup + cross-platform search

Run:
    pip install -r requirements.txt
    python app.py
Then open http://localhost:5005
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
import logging
from typing import Any

from flask import Flask, jsonify, request, send_from_directory

# Robust search/extract core
from cross_search import (
    build_search_query,
    detect_platform,
    extract_id,
    extract_product,
    filter_product_urls,
    search_platform,
    search_and_score,
    _title_from_url_slug_public,
    _strip_generic_words,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
)
log = logging.getLogger("cross-search")

HERE = os.path.dirname(os.path.abspath(__file__))
FRONTEND = os.path.join(HERE, "..", "frontend")
app = Flask(__name__, static_folder=FRONTEND, static_url_path="")


@app.get("/")
def index() -> Any:
    return send_from_directory(FRONTEND, "index.html")


@app.post("/api/search")
def api_search() -> Any:
    t0 = time.time()
    data: dict[str, Any] = request.get_json(silent=True) or {}
    raw_url: str = (data.get("url") or "").strip()
    max_results = int(data.get("max_results", 3))

    if not raw_url:
        return jsonify({"error": "Please paste a product URL."}), 400

    url = raw_url
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    platform = detect_platform(url)
    if platform == "unknown":
        return jsonify(
            {
                "error": (
                    "Can't identify the platform for that URL. "
                    "Supported: amazon.in, flipkart.com, meesho.com, myntra.com."
                )
            }
        ), 400

    # --- Extract source product (best-effort; browser fallback on block) ---
    src = extract_product(
        url,
        use_browser_on_block=True,
        browser_timeout=40,
    )

    src_title = src.get("title") or _title_from_url_slug_public(url) or "Title couldn't be read"
    src_price = src.get("price") or "—"
    src_image = src.get("image_url") or ""
    src_blocked = src.get("_page_quality") in ("blocked", "no_text") or not src.get("ok")
    src_error = src.get("error") or ""

    # --- Build search queries (1-3 variants) ---
    queries = build_search_query(
        title=src.get("title") or "",
        extra="",
        url_slug_title=_title_from_url_slug_public(url),
        platform=platform,
    )

    # --- Search the OTHER platforms (scored + deduped) ---
    results: dict[str, list[dict[str, Any]]] = {}
    for target_platform in ("amazon", "flipkart", "meesho", "myntra"):
        if target_platform == platform:
            continue  # skip source platform

        ranked = search_and_score(
            source_title=src_title,
            queries=queries,
            target_platform=target_platform,
            max_results=max_results,
        )

        platform_results = []
        for r in ranked:
            platform_results.append({
                "url": r["url"],
                "title": r["title"] or "—",
                "price": r["price"] or "—",
                "image_url": r["image_url"] or "",
                "score": round(r.get("score", 0.0) * 100, 1),
                "ok": r.get("ok", False),
                "blocked": r.get("blocked", False),
                "from_url_slug": r.get("_from_url_slug", False),
                "error": r.get("error") or "",
            })
        results[target_platform] = platform_results

    elapsed = round(time.time() - t0, 1)

    log.info(
        "search done in %.1fs for %s → source=%s title=%s price=%s "
        "queries=%d results: amazon=%d flipkart=%d meesho=%d",
        elapsed,
        url[:80],
        platform,
        src_title[:50],
        src_price,
        len(queries),
        len(results.get("amazon", [])),
        len(results.get("flipkart", [])),
        len(results.get("meesho", [])),
    )

    return jsonify(
        {
            "source": {
                "url": url,
                "platform": platform,
                "id": extract_id(url),
                "title": src_title,
                "price": src_price,
                "image_url": src_image,
                "blocked": src_blocked,
                "error": src_error,
            },
            "queries": queries,
            "results": results,
            "elapsed_seconds": elapsed,
        }
    )


def _title_from_url_slug(url: str) -> str:
    """Fallback title guess from the URL's slug, for when the page is blocked.

    Thin wrapper around cross_search._title_from_url_slug for the Flask layer.
    """
    return _title_from_url_slug_public(url)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5005))
    log.info("Starting cross-search backend on http://0.0.0.0:%d", port)
    app.run(host="0.0.0.0", port=port, debug=True)
