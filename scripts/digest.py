import os
import json
from datetime import datetime, timedelta
from pathlib import Path
import xml.etree.ElementTree as ET
from openai import OpenAI

# -------------------------------
# FOLDERI
# -------------------------------
RAW_DIR = Path("raw")
RAW_OUTPUT = Path("news/news.xml")
DIGEST_OUTPUT = Path("news/digest.xml")

RAW_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
DIGEST_OUTPUT.parent.mkdir(parents=True, exist_ok=True)

# -------------------------------
# OPENAI
# -------------------------------
API_KEY = os.getenv("VESTI")
client = OpenAI(api_key=API_KEY)


# -------------------------------
# LOADING RAW JSON VESTI
# -------------------------------
def load_recent_news(hours=24):
    items = []
    cutoff = datetime.utcnow() - timedelta(hours=hours)

    if not RAW_DIR.exists():
        return items

    for fname in os.listdir(RAW_DIR):
        if not fname.endswith(".json"):
            continue

        path = RAW_DIR / fname

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except:
            continue

        ts = data.get("fetched_at")
        if not ts:
            continue

        try:
            dt = datetime.fromisoformat(ts.replace("Z", ""))
        except:
            continue

        if dt >= cutoff:
            items.append(data)

    print(f"Učitano {len(items)} vesti")
    return items


# -------------------------------
# INPUT ZA GPT
# -------------------------------
def build_model_input(items):
    blocks = []
    for i, it in enumerate(items, 1):
        full = it.get("full_text", "")
        if len(full) > 1200:
            full = full[:1200] + "…"

        block = f"""
VEST {i}
IZVOR: {it.get('source','')}
NASLOV: {it.get('title','')}
PODNASLOV: {it.get('subtitle','')}
TEKST: {full}
LINK: {it.get('url') or it.get('link') or ''}
"""
        blocks.append(block)

    return "\n\n".join(blocks)


# -------------------------------
# GPT → topics JSON
# -------------------------------
def call_openai_for_digest(text):
    if not API_KEY:
        print("Nema API key-a")
        return []

    system_msg = (
        "Ti si analitičar vesti. "
        "Odgovaraj ISKLJUČIVO na srpskom jeziku. "
        "Grupiši vesti po temama i vrati JSON oblika:\n"
        "{ 'topics': [ { 'title': '', 'summary': '', 'links': [] } ] }"
    )

    user_msg = "Vesti:\n\n" + text

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.4,
        )
    except Exception as e:
        print("OpenAI ERROR:", e)
        return []

    content = resp.choices[0].message.content.strip()

    # ------------------------
    # JSON ekstrakcija
    # ------------------------
    try:
        s = content
        s = s[s.find("{") : s.rfind("}") + 1]
        data = json.loads(s)
        topics = data.get("topics", [])
    except Exception as e:
        print("JSON ERROR:", e)
        print(content)
        return []

    # ------------------------
    # NORMALIZACIJA — fix GPT gluposti
    # ------------------------
    fixed = []
    for t in topics:
        if isinstance(t, dict):
            fixed.append({
                "title": t.get("title", "Bez naslova"),
                "summary": t.get("summary", ""),
                "links": t.get("links", []),
            })
        elif isinstance(t, str):
            # GPT nekad vrati string umesto dict
            fixed.append({
                "title": t,
                "summary": "",
                "links": [],
            })

    return fixed


# -------------------------------
# RAW RSS
# -------------------------------
def generate_raw_feed(items):
    rss = ET.Element("rss", version="2.0")
    ch = ET.SubElement(rss, "channel")

    ET.SubElement(ch, "title").text = "News digest RAW"
    ET.SubElement(ch, "link").text = "https://bulvag.github.io/news/news.xml"
    ET.SubElement(ch, "description").text = "Sirove vesti"
    ET.SubElement(ch, "language").text = "sr"

    for it in items:
        item = ET.SubElement(ch, "item")
        ET.SubElement(item, "title").text = it.get("title", "")
        ET.SubElement(item, "link").text = it.get("url", "")
        ET.SubElement(item, "guid").text = it.get("url", "")
        ET.SubElement(item, "description").text = (it.get("full_text") or "")[:500]
        ET.SubElement(item, "pubDate").text = datetime.utcnow().strftime(
            "%a, %d %b %Y %H:%M:%S GMT"
        )

    ET.ElementTree(rss).write(RAW_OUTPUT, encoding="utf-8", xml_declaration=True)
    print("RAW OK")


# -------------------------------
# DIGEST RSS
# -------------------------------
def generate_digest(topics):
    rss = ET.Element("rss", version="2.0")
    ch = ET.SubElement(rss, "channel")

    ET.SubElement(ch, "title").text = "AI digest"
    ET.SubElement(ch, "link").text = "https://bulvag.github.io/news/digest.xml"
    ET.SubElement(ch, "description").text = "AI tematski sažeci"
    ET.SubElement(ch, "language").text = "sr"

    for t in topics:
        item = ET.SubElement(ch, "item")
        ET.SubElement(item, "title").text = t.get("title", "Bez naslova")

        body = t.get("summary", "")
        links = t.get("links", [])

        if links:
            body += "<br/><br/><b>VESTI:</b><br/>" + "<br/>".join(links)

        ET.SubElement(item, "description").text = body
        ET.SubElement(item, "guid").text = t.get("title", "") + datetime.utcnow().isoformat()
        ET.SubElement(item, "pubDate").text = datetime.utcnow().strftime(
            "%a, %d %b %Y %H:%M:%S GMT"
        )

    ET.ElementTree(rss).write(DIGEST_OUTPUT, encoding="utf-8", xml_declaration=True)
    print("DIGEST OK")


# -------------------------------
# MAIN
# -------------------------------
def main():
    items = load_recent_news()
    if not items:
        print("Nema vesti!")
        return

    generate_raw_feed(items)

    topics = call_openai_for_digest(build_model_input(items))
    if topics:
        generate_digest(topics)
    else:
        print("Nema tema — digest preskočen")


if __name__ == "__main__":
    main()
