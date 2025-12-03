import os
import json
from datetime import datetime
from pathlib import Path
import xml.etree.ElementTree as ET

RAW_DIR = Path("raw")
OUTPUT = Path("news.xml")


def load_news():
    items = []

    if not RAW_DIR.exists():
        return items

    for fname in os.listdir(RAW_DIR):
        if not fname.endswith(".json"):
            continue

        path = RAW_DIR / fname
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            items.append(data)
        except Exception as e:
            print(f"Greška pri čitanju {path}: {e}")
            continue

    return items


def parse_date(item):
    """
    Pokuša da koristi published, ako nema uzima fetched_at.
    Ne komplikujemo sa pravim parsiranjem, samo sortiramo po stringu.
    """
    return item.get("published") or item.get("fetched_at") or ""


def generate_rss(items):
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text = "News digest (Danas + BBC)"
    ET.SubElement(channel, "link").text = "https://bulvag.github.io/news/news.xml"
    ET.SubElement(channel, "description").text = "Automatski digest vesti (sirova verzija, bez AI)"
    ET.SubElement(channel, "language").text = "sr"
    ET.SubElement(channel, "lastBuildDate").text = datetime.utcnow().strftime(
        "%a, %d %b %Y %H:%M:%S GMT"
    )

    # sortiramo po datumu i uzmemo 50 najnovijih
    items_sorted = sorted(
        items,
        key=lambda x: parse_date(x),
        reverse=True,
    )[:50]

    for it in items_sorted:
        title = it.get("title", "").strip() or "(bez naslova)"
        url = it.get("url", "").strip()
        source = it.get("source", "").strip()
        subtitle = it.get("subtitle", "").strip()
        full_text = it.get("full_text", "").strip()

        # opis za RSS – neki normalan tekst
        if subtitle:
            desc = subtitle
        elif full_text:
            desc = full_text[:500]
        else:
            desc = ""

        # dodamo izvor u opis da znaš odakle je
        if source:
            if desc:
                desc = f"[{source}] {desc}"
            else:
                desc = f"[{source}]"

        item_el = ET.SubElement(channel, "item")
        ET.SubElement(item_el, "title").text = title

        if url:
            ET.SubElement(item_el, "link").text = url
            ET.SubElement(item_el, "guid").text = url
        else:
            ET.SubElement(item_el, "guid").text = it.get("id", "")

        if desc:
            ET.SubElement(item_el, "description").text = desc

        pub = parse_date(it)
        if pub:
            ET.SubElement(item_el, "pubDate").text = pub

    tree = ET.ElementTree(rss)
    tree.write(OUTPUT, encoding="utf-8", xml_declaration=True)
    print(f"Upisano u {OUTPUT}")


def main():
    news = load_news()
    if not news:
        print("Nema vesti u raw/")
        return

    generate_rss(news)


if __name__ == "__main__":
    main()
