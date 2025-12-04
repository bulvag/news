# scripts/digest.py

import os
import json
from datetime import datetime, timedelta
from pathlib import Path
import xml.etree.ElementTree as ET
from openai import OpenAI

# --- PUTANJE / FOLDERI ---

RAW_DIR = Path("raw")                  # tu collector upisuje sirove JSON vesti
RAW_OUTPUT = Path("news/news.xml")     # sirovi RSS (za tebe / backup)
DIGEST_OUTPUT = Path("news/digest.xml")  # AI digest RSS za Inoreader

RAW_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
DIGEST_OUTPUT.parent.mkdir(parents=True, exist_ok=True)

# --- OPENAI KLIJENT ---

API_KEY = os.getenv("VESTI")
client = OpenAI(api_key=API_KEY)


# ---------- 1) UČITAVANJE VESTI ----------

def load_recent_news(hours: int = 1, max_items: int = 100):
    """
    Učitaj vesti iz RAW_DIR koje su novije od `hours` sati,
    sortiraj po datumu opadajuće i uzmi najviše `max_items` komada.
    """
    items = []
    if not RAW_DIR.exists():
        print("RAW_DIR ne postoji, nema vesti.")
        return []

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

        ts = data.get("fetched_at")
        if not ts:
            continue

        try:
            dt = datetime.fromisoformat(ts.replace("Z", ""))
        except Exception:
            continue

        if dt >= cutoff:
            data["_dt"] = dt  # privremeno za sortiranje
            items.append(data)

    # najnovije prve
    items.sort(key=lambda x: x["_dt"], reverse=True)

    # ograniči broj da ne probijemo context i budžet
    items = items[:max_items]

    print(f"Učitano {len(items)} vesti (poslednjih {hours}h, max {max_items})")
    return items


# ---------- 2) PRIPREMA TEKSTA ZA MODEL ----------

def build_model_input(items):
    """
    Za svaku vest pravimo blok sa naslovom, izvorom, tekstom i linkom.
    Full tekst skraćujemo da ne pregori kontekst.
    """
    blocks = []
    MAX_CHARS = 800  # ~400 tokena po vesti

    for i, it in enumerate(items, 1):
        title = (it.get("title") or "").strip()
        source = (it.get("source") or "").strip()
        subtitle = (it.get("subtitle") or "").strip()
        full = (it.get("full_text") or "").strip()
        url = (it.get("url") or it.get("link") or "").strip()

        if len(full) > MAX_CHARS:
            full = full[:MAX_CHARS] + "…"

        block = f"""VEST {i}
IZVOR: {source}
NASLOV: {title}
PODNASLOV: {subtitle}
TEKST: {full}
LINK: {url}
"""
        blocks.append(block)

    return "\n\n-----\n\n".join(blocks)


# ---------- 3) POZIV OPENAI-a (AI GRUPIŠE, NE MI) ----------

def call_openai_for_digest(text: str) -> list:
    """
    AI SAM bira teme, ali mu eksplicitno kažemo:
    - svaka vest mora biti u nekoj temi,
    - svaka tema ima listu SVIH linkova vesti u toj temi,
    - izlaz je striktan JSON sa 'topics'.
    """
    if not API_KEY:
        print("Nema API ključa (VESTI), preskačem AI digest.")
        return []

    system_msg = (
        "Ti si novinar koji piše dnevni pregled vesti za jednu osobu.\n"
        "Ulaz su pojedinačne vesti (naslov, tekst, izvor, link).\n"
        "Tvoj zadatak je:\n"
        "1) Da SAM odrediš tematske grupe (teme) na osnovu stvarnih žarišta u vestima "
        "(npr. rat u Ukrajini, Gaza/Izrael, politika u Srbiji, protesti u Srbiji, ekonomija, Zapadna Afrika, Indija, itd.).\n"
        "2) Svaku vest MORAŠ da smestiš u neku temu. Nema preskakanja vesti.\n"
        "3) Broj tema može biti veliki (koliko god da treba) – ne ograničavaj se na par tema.\n"
        "4) Za SVAKU temu:\n"
        "   - 'title': kratak naslov teme na srpskom jeziku.\n"
        "   - 'summary': sažetak od 5–12 rečenica na SRPSKOM jeziku - to su grupisane vesti na istu temu "
        "(mirno, jasno, bez senzacionalizma; objasni šta se dešava i zašto je važno).\n"
        "   - 'links': lista SVIH linkova vesti koje spominješ u toj temi. Nemoj izostavljati vesti.\n"
        "5) Svaka vest bi treba da se pojavi u tačno jednoj temi, osim ako je nešto posebno relevantno za sve.\n"
        "Odgovori ISKLJUČIVO u JSON formatu, bez ikakvog dodatnog teksta, ovako:\n"
        "{\n"
        "  \"topics\": [\n"
        "    {\"title\": \"…\", \"summary\": \"…\", \"links\": [\"https://…\", \"https://…\"]},\n"
        "    {\"title\": \"…\", \"summary\": \"…\", \"links\": [\"https://…\"]}\n"
        "  ]\n"
        "}\n"
    )

    user_msg = (
        "Ovo su vesti iz poslednja 24 sata (svaka počinje sa 'VEST N'). "
        "Iskoristi SVE vesti, bez preskakanja.\n\n"
        + text
    )

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

    # izvuci JSON
    try:
        s = content
        start = s.find("{")
        end = s.rfind("}")
        if start != -1 and end != -1:
            s = s[start : end + 1]

        data = json.loads(s)
        topics = data.get("topics", [])
        if not isinstance(topics, list):
            print("JSON nema validan 'topics' niz.")
            return []

        print(f"AI vratio {len(topics)} tema.")
        return topics

    except Exception as e:
        print("JSON ERROR:", e)
        print("RAW odgovor:\n", content[:2000])
        return []


# ---------- 4) RAW RSS (news/news.xml) ----------

def generate_raw_feed(items: list):
    rss = ET.Element("rss", version="2.0")
    ch = ET.SubElement(rss, "channel")

    ET.SubElement(ch, "title").text = "News digest RAW (Danas + BBC)"
    ET.SubElement(ch, "link").text = "https://bulvag.github.io/news/news.xml"
    ET.SubElement(ch, "description").text = "Sirove vesti iz poslednja 24h"
    ET.SubElement(ch, "language").text = "sr"
    ET.SubElement(ch, "lastBuildDate").text = datetime.utcnow().strftime(
        "%a, %d %b %Y %H:%M:%S GMT"
    )

    for it in items:
        item = ET.SubElement(ch, "item")
        ET.SubElement(item, "title").text = it.get("title", "")
        link = (it.get("url") or it.get("link") or "").strip()
        ET.SubElement(item, "link").text = link
        ET.SubElement(item, "guid").text = link
        ET.SubElement(item, "description").text = (it.get("full_text") or "")[:500]
        ET.SubElement(item, "pubDate").text = datetime.utcnow().strftime(
            "%a, %d %b %Y %H:%M:%S GMT"
        )

    ET.ElementTree(rss).write(RAW_OUTPUT, encoding="utf-8", xml_declaration=True)
    print("RAW OK →", RAW_OUTPUT)


# ---------- 5) AI DIGEST RSS (news/digest.xml) ----------

def generate_digest(topics: list):
    rss = ET.Element("rss", version="2.0")
    ch = ET.SubElement(rss, "channel")

    ET.SubElement(ch, "title").text = "AI digest (Danas + BBC)"
    ET.SubElement(ch, "link").text = "https://bulvag.github.io/news/digest.xml"
    ET.SubElement(ch, "description").text = "Tematski AI sažeci vesti"
    ET.SubElement(ch, "language").text = "sr"
    ET.SubElement(ch, "lastBuildDate").text = datetime.utcnow().strftime(
        "%a, %d %b %Y %H:%M:%S GMT"
    )

    for t in topics:
        # fallback ako model ipak vrati string umesto objekta
        if isinstance(t, str):
            title = t
            summary = t
            links = []
        else:
            title = (t.get("title") or "Bez naslova").strip()
            summary = (t.get("summary") or "").strip()
            links = t.get("links") or []

        item = ET.SubElement(ch, "item")
        ET.SubElement(item, "title").text = title

        body_parts = []
        if summary:
            body_parts.append(summary)
        if links:
            body_parts.append("<br/><br/><b>VESTI:</b><br/>" + "<br/>".join(links))

        desc = " ".join(body_parts) if body_parts else "Nema dodatnog sažetka."
        ET.SubElement(item, "description").text = desc

        guid = f"{title}-{datetime.utcnow().isoformat()}"
        ET.SubElement(item, "guid").text = guid
        ET.SubElement(item, "pubDate").text = datetime.utcnow().strftime(
            "%a, %d %b %Y %H:%M:%S GMT"
        )

    ET.ElementTree(rss).write(DIGEST_OUTPUT, encoding="utf-8", xml declaration=True)
    print("DIGEST OK →", DIGEST_OUTPUT)


# ---------- 6) MAIN ----------

def main():
    items = load_recent_news(hours=24, max_items=200)
    if not items:
        print("Nema vesti, ništa ne radim.")
        return

    # 1) Sirov feed
    generate_raw_feed(items)

    # 2) AI digest
    text = build_model_input(items)
    topics = call_openai_for_digest(text)
    if topics:
        generate_digest(topics)
    else:
        print("AI digest nije generisan (nema ili loš odgovor).")


if __name__ == "__main__":
    main()
