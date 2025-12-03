import os
import json
from datetime import datetime
import xml.etree.ElementTree as ET

RAW_DIR = "raw"
OUTPUT = "news.xml"

def load_news():
    items = []
    for fname in os.listdir(RAW_DIR):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(RAW_DIR, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            items.append(data)
        except Exception:
            continue
    return items

def generate_rss(items):
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text = "Daily News Digest"
    ET.SubElement(channel, "link").text = "https://bulvag.github.io/news/"
    ET.SubElement(channel, "description").text = "Automatski digest vesti"
    ET.SubElement(channel, "language").text = "sr"

    # Sortiramo najnovije
    items = sorted(items, key=lambda x: x.get("published", ""), reverse=True)[:50]

    for item in items:
        i = ET.SubElement(channel, "item")
        ET.SubElement(i, "title").text = item["title"]
        ET.SubElement(i, "link").text = item["link"]
        ET.SubElement(i, "description").text = item["summary"]
        ET.SubElement(i, "pubDate").text = item["published"]

    tree = ET.ElementTree(rss)
    tree.write(OUTPUT, encoding="utf-8", xml_declaration=True)

def main():
    news = load_news()
    if not news:
        print("No news loaded")
        return
    generate_rss(news)
    print("Generated", OUTPUT)

if __name__ == "__main__":
    main()
