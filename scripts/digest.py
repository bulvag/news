import os
import json
from datetime import datetime, timedelta
from pathlib import Path
import xml.etree.ElementTree as ET
import openai
import textwrap

RAW_DIR = Path("raw")
OUTPUT = Path("news.xml")

# U GitHub Actions će doći iz secrets
openai.api_key = os.getenv("VESTI")


def load_recent_news(hours: int = 24):
    """Učitaj vesti iz raw/ u poslednjih X sati (default 24)."""
    items = []
    if not RAW_DIR.exists():
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
        url = it.get("url", "").strip()

        # ograničimo full_text da ne bude predugačko
        if full and len(full) > 800:
            full = full[:800] + "…"

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
        print("Nema OPENAI_API_KEY, preskačem AI digest.")
        return []

    system_msg = (
        "Ti si analitičar koji pravi PAMETAN novinski pregled za jednu osobu.\n"
        "Radiš na srpskom jeziku (ijekavica ili ekavica su obe ok, samo budi konzistentan).\n"
        "Ulaz su pojedinačne vesti (naslov, tekst, izvor, link). "
        "Tvoj zadatak je:\n"
        "1) da sam grupišeš vesti po temama/žarištima (npr. Ukrajina/Rusija, Gaza/Izrael, "
        "   izbori u nekoj zemlji, protesti, ekonomske teme, klimatske katastrofe itd.).\n"
        "2) da za SVAKU temu napraviš jedan tematski sažetak od ~8–12 rečenica.\n"
        "3) da kontekst i „šira slika“ budu UTKANI u tekst, ne da ih odvajaš u posebnu sekciju.\n"
        "4) brojke i detaljne statistike koristi samo ako su stvarno bitni.\n"
        "5) za svaku temu navedi listu linkova važnih vesti koje opisuješ.\n"
        "6) stil: jasno, sažeto, bez senzacionalizma, kao dobar analitički newsletter.\n"
        "Obavezno odgovori u JSON formatu sa ključem 'topics', gde je svaki element:\n"
        "{\n"
        '  "title": "naslov teme",\n'
        '  "summary": "opsežan sažetak na srpskom",\n'
        '  "links": ["https://...","https://..."]\n'
        "}\n"
    )

    user_msg = (
        "Ovo su vesti iz poslednja 24 sata. Nemoj da praviš pregled po državama po difoltu, "
        "nego po stvarnim temama koje uočavaš. Ako se neka tema pojavljuje samo jednom, "
        "i dalje je uključi kao zasebnu temu, ali nemoj da izmišljas teme ako ih nema.\n\n"
        "Vesti:\n\n"
        + raw_text
    )

    # Pošto smo u najnovijem OpenAI API-ju, koristimo 'client' objekat u realnom kodu,
    # ali za jednostavnost koristimo stariji stil:
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
    # pokušaj parsiranja JSON-a
    try:
        # ako je model možda vratio tekst pre/posle, pokušaj da izvučeš blok sa JSON-om
        content_stripped = content.strip()
        # najnaivnije: nađi prvi '{' i poslednju '}' i uzmi to
        start = content_stripped.find("{")
        end = content_stripped.rfind("}")
        if start != -1 and end != -1:
            json_str = content_stripped[start : end + 1]
        else:
            json_str = content_stripped

        data = json.loads(json_str)
        topics = data.get("topics", [])
        if not isinstance(topics, list):
            return []
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
        title = topic.get("title", "").strip() or "Tema bez naslova"
        summary = topic.get("summary", "").strip()
        links = topic.get("links", [])

        item_el = ET.SubElement(channel, "item")
        ET.SubElement(item_el, "title").text = title

        # Description = sažetak + linkovi
        desc_parts = [summary] if summary else []
        if links:
            links_text = "\n\nVesti:\n" + "\n".join(f"- {u}" for u in links)
            desc_parts.append(links_text)

        desc = "\n".join(desc_parts) if desc_parts else "Nema detaljnog sažetka."
        ET.SubElement(item_el, "description").text = desc

        # pubDate = sada
        ET.SubElement(item_el, "pubDate").text = datetime.utcnow().strftime(
            "%a, %d %b %Y %H:%M:%S GMT"
        )

        # kao guid možemo uzeti naslov + timestamp
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
