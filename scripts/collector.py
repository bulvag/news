import feedparser
import hashlib
import json
from datetime import datetime
from pathlib import Path

# Lista RSS izvora
SOURCES = [
    "http://www.danas.rs/rss/rss.asp",
    "https://feeds.bbci.co.uk/news/rss.xml",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://feeds.bbci.co.uk/news/world/asia/rss.xml",
    "https://feeds.bbci.co.uk/news/business/rss.xml",
    "https://feeds.bbci.co.uk/news/technology/rss.xml",
    "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml",
    "https://www.bbc.com/culture/feed.rss",
    "https://feeds.bbci.co.uk/news/world/latin_america/rss.xml",
    "https://feeds.bbci.co.uk/news/world/australia/rss.xml",
    "http://newsrss.bbc.co.uk/rss/newsonline_uk_edition/uk_politics/rss.xml",
    "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml",
    "https://feeds.bbci.co.uk/news/world/africa/rss.xml",
    "https://feeds.bbci.co.uk/news/world/asia/india/rss.xml",
    "https://feeds.bbci.co.uk/news/world/europe/rss.xml",
    "https://feeds.bbci.co.uk/news/world/asia/china/rss.xml",
]

RAW_DIR = Path("raw")
RAW_DIR.mkdir(exist_ok=True)

def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def save_news_item(item: dict):
    """Čuva vest kao JSON u raw/ folder (dedup automatski)."""
    news_id = item["id"]
    out_path = RAW_DIR / f"{news_id}.json"

    if out_path.exists():
        return False  # već postoji

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(item, f, ensure_ascii=False, indent=2)

    return True

def parse_source(url: str):
    feed = feedparser.parse(url)
    results = []

    for entry in feed.entries:
        title = entry.get("title", "").strip()
        desc = entry.get("summary", "").strip()
        link = entry.get("link", "").strip()

        full = title + "\n" + desc + "\n" + link
        item_id = hash_text(full)

        item = {
            "id": item_id,
            "source": feed.feed.get("title", "Unknown"),
            "title": title,
            "subtitle": desc,
            "url": link,
            "published": entry.get("published", ""),
            "fetched_at": datetime.utcnow().isoformat(),
            "full_text": desc,
        }

        results.append(item)

    return results

def main():
    print("Početak prikupljanja…")

    total_new = 0

    for src in SOURCES:
        print(f"Čitam: {src}")
        try:
            items = parse_source(src)
            for it in items:
                if save_news_item(it):
                    total_new += 1
        except Exception as e:
            print(f"Greška u {src}: {e}")

    print(f"Gotovo. Novih vesti: {total_new}")

if __name__ == "__main__":
    main()
