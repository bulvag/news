import os
import json
from datetime import datetime, timedelta
from pathlib import Path
import xml.etree.ElementTree as ET
import openai

# FOLDERI
RAW_DIR = Path("raw")
RAW_OUTPUT = Path("news/news.xml")      # obične sirove vesti
DIGEST_OUTPUT = Path("news/digest.xml")  # AI digest

# KREIRAJ NEWS/ AKO NE POSTOJI
RAW_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
DIGEST_OUTPUT.parent.mkdir(parents=True, exist_ok=True)

# API ključ
openai.api_key = os.getenv("VESTI")


def load_recent_news(hours: int = 24):
    items = []
    if not RAW_DIR.exists():
        print("RAW_DIR ne postoji, nema vesti.")
        return items

    cutoff = datetime.utcnow() - timedelta(hours=hours)

    for fname in os.listdir(RAW_DIR):
        if not fname.endswith(".json"):
            continue

        path = RAW_DIR / fname
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"Greška pri čitanju {path}: {e}")
            continue

        fetched_at = data.get("fetched_at")
        if fetched_at:
            try:
                dt = datetime.fromisoformat(fetched_at.replace("Z", ""))
            except Exception:
                dt = None
        else:
            dt = None

        if dt is None or dt < cutoff:
            continue

        items.append(data)

    print(f"Učitano {len(items)} vesti (poslednjih {hours}h)")
    return items


def build_model_input(items):
    parts = []
    for idx, it in enumerate(items, start=1):
        title = it.get("title", "").strip()
        source = it.get("source", "").strip()
        subtitle = it.get("subtitle", "").strip()
        full = it.get("full_text", "").strip()
        url = it.get("url") or it.get("link") or ""

        if full and len(full) > 1200:
            full = full[:1200] + "…"

        block = f"""VEST {idx}
IZVOR: {source}
NASLOV: {title}
PODNASLOV: {subtitle}
TEKST: {full}
LINK: {url}
"""
        parts.append(block)

    return "\n\n".join(parts)


def call_openai_for_digest(raw_text: str) -> list:
    if not openai.api_key:
        print("Nema API ključa (env VESTI nije postavljen), preskačem AI digest.")
        return []

    system_msg = (
        "Ti si analitičar koji pravi pametan novinski pregled.\n"
        "Odgovaraj ISKLJUČIVO na srpskom jeziku.\n"
        "Grupiši vesti po temama, napravi sažetak, vrati JSON sa ključem 'topics'.\n"
    )

    user_msg = "Vesti:\n\n" + raw_text

    try:
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.4,
        )
    except Exception as e:
        print(f"Greška pri OpenAI pozivu: {e}")
        return []

    content = response["choices"][0]["message"]["content"]

    # parse JSON
    try:
        s = content.strip()
        start = s.find("{")
        end = s.rfind("}")
        if start != -1 and end != -1:
            s = s[start:end+1]

        data = json.loads(s)
        topics = data.get("topics", [])
        if not isinstance(topics, list):
            return []

        print(f"AI vratio {len(topics)} tema.")
        return topics

    except Exception as e:
        print("Greška pri JSON parsiranju:", e)
        print("Sirovi odgovor:")
        print(content)
        return []


def generate_raw_feed(items):
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text = "News digest (RAW feed)"
    ET.SubElement(channel, "link").text = "https://bulvag.github.io/news/news.xml"
    ET.SubElement(channel, "description").text = "Sirove vesti pre AI obrade"
    ET.SubElement(channel, "language").text = "sr"
    ET.SubElement(channel, "lastBuildDate").text = datetime.utcnow().strftime(
        "%a, %d %b %Y %H:%M:%S GMT"
    )

    for it in items:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = it.get("title", "")
        ET.SubElement(item, "link").text = it.get("url") or ""
        ET.SubElement(item, "guid").text = it.get("url") or ""
        ET.SubElement(item, "description").text = it.get("full_text", "")[:500]

        ET.SubElement(item, "pubDate").text = datetime.utcnow().strftime(
            "%a, %d %b %Y %H:%M:%S GMT"
        )

    tree = ET.ElementTree(rss)
    tree.write(RAW_OUTPUT, encoding="utf-8", xml_declaration=True)
    print("RAW feed upisan:", RAW_OUTPUT)


def generate_ai_digest(topics: list):
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text = "AI news digest (Danas + BBC)"
    ET.SubElement(channel, "link").text = "https://bulvag.github.io/news/digest.xml"
    ET.SubElement(channel, "description").text = "Tematski AI sažeci vesti"
    ET.SubElement(channel, "language").text = "sr"
    ET.SubElement(channel, "lastBuildDate").text = datetime.utcnow().strftime(
        "%a, %d %b %Y %H:%M:%S GMT"
    )

    for t in topics:
        title = t.get("title") or "Tema bez naslova"
        summary = t.get("summary") or ""
        links = t.get("links") or []

        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = title

        body = summary
        if links:
            body += "<br/><br/><b>Vesti:</b><br/>" + "<br/>".join(links)

        ET.SubElement(item, "description").text = body

        ET.SubElement(item, "pubDate").text = datetime.utcnow().strftime(
            "%a, %d %b %Y %H:%M:%S GMT"
        )

        guid = f"{title}-{datetime.utcnow().isoformat()}"
        ET.SubElement(item, "guid").text = guid

    tree = ET.ElementTree(rss)
    tree.write(DIGEST_OUTPUT, encoding="utf-8", xml_declaration=True)
    print("AI digest upisan:", DIGEST_OUTPUT)


def main():
    items = load_recent_news(hours=24)
    if not items:
        print("Nema vesti u RAW folderu!")
        return

    # RAW RSS
    generate_raw_feed(items)

    # AI digest
    raw_text = build_model_input(items)
    topics = call_openai_for_digest(raw_text)

    if topics:
        generate_ai_digest(topics)
    else:
        print("AI digest nije generisan (nema tema).")


if __name__ == "__main__":
    main()
