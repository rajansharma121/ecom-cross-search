# Cross-Search Tool

Paste an Amazon.in, Flipkart, or Meesho product link — the tool finds
matching listings on the other two platforms.

## Run it on your own laptop (to try it out)

1. Install Python from python.org if you don't have it (3.10 or newer).
2. Open a terminal in this folder and run:
   ```
   cd backend
   pip install -r requirements.txt
   python app.py
   ```
3. Open **http://localhost:5005** in your browser. That's the dashboard.

## Put it online so others can use it (free tier works)

The backend needs to run on a server somewhere — your laptop being on
isn't enough for other people to reach it. **Render** has a simple free
option:

1. Create a free account at render.com.
2. Push this whole `cross-search-tool` folder to a GitHub repo.
3. In Render: **New → Web Service** → connect that repo.
4. Set:
   - **Root Directory:** `backend`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app` (add `gunicorn` to
     `requirements.txt` first — it's the production version of the
     `python app.py` dev server)
5. Deploy. Render gives you a public URL — that's what you share.

Railway.app works the same way if you'd rather use that.

## Things to know

- **Scraping Amazon/Flipkart/Meesho is inherently fragile.** These
  sites actively block bots (CAPTCHAs, 403s). The tool is built to
  degrade gracefully — it'll show "page blocked" rather than crash,
  and fall back to guessing the title from the URL — but expect some
  misses. This is the nature of scraping, not a bug to chase forever.
- **Rate limits.** If many people use it heavily, DuckDuckGo or the
  marketplaces may start blocking the server's IP temporarily. Fine
  for casual/internal use; not built for high volume.
- **No data is stored anywhere** — each search is stateless.
