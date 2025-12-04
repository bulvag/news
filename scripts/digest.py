import os
import json
from datetime import datetime, timedelta
from pathlib import Path
import xml.etree.ElementTree as ET
import openai

RAW_DIR = Path("raw")
OUTPUT = Path("news.xml")

# u GitHub Actions dolazi iz env promenljive VESTI
openai.api_key = os.getenv("VESTI")


def load_recent_news(hours: int = 24):
    """Učitaj vesti iz raw/ u poslednjih X sati (default 24)."""
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
    """Napravi tekstualni input za model: lista vesti sa osnovnim podacima."""
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
    """
    Poziv modelu: da sam smisli teme i vrati listu tema sa sažecima.
    Vraća listu dict-ova: { "title": ..., "summary": ..., "links": [...] }
    """
    if not openai.api_key:
        print("Nema API ključa (env VESTI nije postavljen), preskačem AI digest.")
        return []

    system_msg = (
        "Ti si analitičar koji pravi PAMETAN novinski pregled za jednu osobu.\n"
        "OBAVEZNO odgovaraj ISKLJUČIVO na srpskom jeziku (ekavica ili ijekavica su ok, "
        "ali nemoj koristiti engleski osim u nazivima institucija ili ličnim imenima).\n"
        "Ulaz su pojedinačne vesti (naslov, tekst, izvor, link). "
        "Tvoj zadatak je:\n"
        "1) da SAM grupišeš vesti po temama/žarištima (npr. Ukrajina/Rusija, Gaza/Izrael, "
        "   izbori u nekoj zemlji, protesti, ekonomske teme, klimatske katastrofe itd.).\n"
        "2) da za SVAKU temu napraviš jedan tematski sažetak od približno 5–12 rečenica.\n"
        "   Počni sa 1–2 rečenice koje jasno kažu šta je suština problema danas, "
        "   a zatim objasni širi kontekst i posledice.\n"
        "3) Brojke i statistike koristi samo ako su stvarno bitne za razumevanje.\n"
        "4) Za svaku temu navedi listu linkova najvažnijih vesti koje opisuješ.\n"
        "5) Stil: jasno, smireno, bez senzacionalizma, kao dobar analitički newsletter.\n"
        "Obavezno odgovori u JSON formatu sa ključem 'topics', gde je svaki element:\n"
        "{\n"
        '  \"title\": \"naslov teme\",\n'
        '  \"summary\": \"opsežan sažetak na srpskom\",\n'
        '  \"links\": [\"https://...\",\"https://...\"]\n'
        "}\n"
    )

    user_msg = (
        "Ovo su vesti iz poslednja 24 sata. Nemoj da praviš pregled po državama po difoltu, "
        "nego po stvarnim temama koje uočavaš. Ako se neka tema pojavljuje samo jednom, "
        "i dalje je uključi kao zasebnu temu, ali nemoj da izmišljaš teme ako ih nema.\n\n"
        "Vesti:\n\n"
        + raw_text
    )

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
        print(f"Greška pri pozivu OpenAI API: {e}")
        return []

    content = response["choices"][0]["message"]["content"]
    try:
        content_stripped = content.strip()
        start = content_stripped.find("{")
        end = content_stripped.rfind("}")
        if start != -1 and end != -1:
            json_str = content_stripped[start : end + 1]
        else:
            json_str = content_stripped

        data = json.loads(json_str)
        topics = data.get("topics", [])
        if not isinstance(topics, list):
            print("JSON nema listu 'topics'.")
            return []
        print(f"Model vratio {len(topics)} tema.")
        return topics
    except Exception as e:
        print("Greška pri parsiranju JSON odgovora:", e)
        print("Sirovi odgovor:")
        print(content)
        return []


def generate_rss_from_topics(topics: list):
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text = "AI news digest (Danas + BBC)"
    ET.SubElement(channel, "link").text = "https://bulvag.github.io/news/news.xml"
    ET.SubElement(channel, "description").text = "Tematski AI sažeci vesti"
    ET.SubElement(channel, "language").text = "sr"
    ET.SubElement(channel, "lastBuildDate").text = datetime.utcnow().strftime(
        "%a, %d %b %Y %H:%M:%S GMT"
    )

    for topic in topics:
        title = (topic.get("title") or "").strip() or "Tema bez naslova"
        summary = (topic.get("summary") or "").strip()
        links = topic.get("links") or []

        item_el = ET.SubElement(channel, "item")
        ET.SubElement(item_el, "title").text = title

        desc_parts = []
        if summary:
            desc_parts.append(summary)

        if links:
            links_lines = "<br/>".join(links)
            links_html = f"<br/><br/><b>Vesti:</b><br/>{links_lines}"
            desc_parts.append(links_html)

        desc = " ".join(desc_parts) if desc_parts else "Nema detaljnog sažetka."
        ET.SubElement(item_el, "description").text = desc

        ET.SubElement(item_el, "pubDate").text = datetime.utcnow().strftime(
            "%a, %d %b %Y %H:%M:%S GMT"
        )

        guid_val = f"{title}-{datetime.utcnow().isoformat()}"
        ET.SubElement(item_el, "guid").text = guid_val

    tree = ET.ElementTree(rss)
    tree.write(OUTPUT, encoding="utf-8", xml_declaration=True)
    print(f"Upisano u {OUTPUT} (AI digest)")


def main():
    items = load_recent_news(hours=24)
    if not items:
        print("Nema dovoljno vesti za AI digest.")
        return

    raw_text = build_model_input(items)
    topics = call_openai_for_digest(raw_text)

    if not topics:
        print("Nema AI tema, preskačem generisanje.")
        return

    generate_rss_from_topics(topics)


if __name__ == "__main__":
    main()
