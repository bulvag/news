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

def load_recent_news(hours: int = 6, max_items: int = 200):
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


# ---------- 2) PRIPREMA TEKSTA ZA MODEL (BEZ "VEST #") ----------

def build_model_input(items):
    """
    Za svaku vest pravimo blok sa naslovom, izvorom, tekstom i linkom.
    Full tekst skraćujemo da ne pregori kontekst.
    Vraća jedan veliki string za slanje modelu.
    NEMA oznaka tipa "VEST 5".
    """
    blocks = []
    MAX_CHARS = 800  # ~400 tokena po vesti

    for it in items:
        title = (it.get("title") or "").strip()
        source = (it.get("source") or "").strip()
        subtitle = (it.get("subtitle") or "").strip()
        full = (it.get("full_text") or "").strip()
        url = (it.get("url") or it.get("link") or "").strip()

        if len(full) > MAX_CHARS:
            full = full[:MAX_CHARS] + "…"

        block = (
            f"IZVOR: {source}\n"
            f"NASLOV: {title}\n"
            f"PODNASLOV: {subtitle}\n"
            f"TEKST: {full}\n"
            f"LINK: {url}\n"
        )
        blocks.append(block)

    # samo separator između vesti, bez "VEST #"
    return "\n\n-----\n\n".join(blocks)


# ---------- 3) POZIV OPENAI-a (AI GRUPIŠE I VRAĆA LINKS) ----------

def call_openai_for_digest(text: str) -> list:
    """
    AI dobija sve vesti i vraća listu tema sa sažetkom i linkovima.
    JSON format:
    {
      "topics": [
        {
          "title": "...",
          "summary": "...",
          "links": ["https://...", "..."]
        },
        ...
      ]
    }
    """
    if not API_KEY:
        print("Nema API ključa (VESTI), preskačem AI digest.")
        return []

    system_msg = (
        "Ti si napredni analitičar vesti. Radi ISKLJUČIVO na srpskom jeziku.\n\n"
        "Dobijaš veliki broj vesti iz različitih izvora. Za SVAKU vest moraš da odlučiš kojoj temi pripada "
        "i da je obradiš – nijedna vest ne sme da bude preskočena.\n\n"
        "OSNOVNA PRAVILA GRUPISANJA:\n"
        "- Prvo grupiši vesti po STVARNOJ temi / događaju (npr. 'SAD – izbori', "
        "'Protesti u Srbiji').\n"
        "- Državu ili region koristi samo kao deo naslova teme, uz jasno objašnjenje šta se dešava.\n"
        "- STROGO JE ZABRANJENO da praviš opšte, prazne teme tipa: 'Međunarodne vesti', "
        "'Svetski događaji', 'Društvene teme', 'Politički i društveni događaji', 'Različiti događaji' i slično.\n\n"
        "SRBIJA (VAŽNO):\n"
        "- NE pravi jednu ogromnu temu 'Srbija – politika i društvo'.\n"
        "- Umesto toga pravi više manjih tema po KONKRETNIM slučajevima i aferama.\n"
        "- Vesti grupiši po kombinaciji DRŽAVA + TEMA (npr. 'SAD – izbori').\n"
        "- Ako ima više sitnih vesti iz mnogo različitih zemalja, možeš da ih spojiš u jednu širu temu "
        "sličnog tipa (npr. 'Evropa – bezbednosni incidenti', 'Nemačka – kriminal i istrage'), "
        "ali u summary-ju mora jasno da piše KO je šta uradio i GDE.\n\n"
        "ZABRANE:\n"
        "- Nikada ne pravi teme sa potpuno praznim ili skoro praznim summary-jem.\n"
        "- Nikada ne pravi summary od jedne uopštene rečenice tipa: 'u svetu se dešavaju događaji', "
        "'postoji napetost', 'održavaju se političke aktivnosti', 'ima mnogo problema u društvu'.\n"
        "- Ne sme da postoji tema u kojoj se ne može prepoznati nijedna konkretna vest.\n\n"
        "SUMMARY (ZA SVAKU TEMU):\n"
        "- Summary mora da sadrži STVARNE INFORMACIJE iz vesti:\n"
        "  ko je, šta je uradio, gde, kada, zbog čega, kakve su posledice, kakve su reakcije.\n"
        "- Ako tema sadrži više vesti, summary mora da pokrije više ključnih vesti, ne samo jednu.\n"
        "- Koristi više rečenica, onoliko koliko je potrebno (5, 10, 15 i više), važno je da bude informativno i konkretno.\n"
        "- Ne piši pamflete ni opšte ocene, nego direktne činjenične opise događaja.\n\n"
        "POJEDINAČNE I PREOSTALE VESTI:\n"
        "- Teme treba da sadrže više povezanih vesti kad god je to moguće.\n"
        "- NIKAKO NE PRAVI odvojene teme gde svaka ima po jednu vest.\n"
        "- Ako na kraju ipak ostane nekoliko nepovezanih vesti koje ne možeš da uklopiš ni u jednu normalnu temu, "
        "napravi JEDNU zajedničku temu 'Preostale pojedinačne vesti (kratak pregled)'.\n"
        "- U summary-ju te teme koristi listu sa crticama i novim redom, gde svaka stavka ima mini-naslov i jednu jasnu rečenicu "
        "sa ključnom informacijom (ko, šta, gde).\n"
        "- Ne pravi zasebne teme za svaku sitnu vest ako možeš da je spojiš sa iole sličnim sadržajem.\n\n"
        "LINKOVI:\n"
        "- Za SVAKU temu obavezno popuni polje 'links' sa SVIM URL-ovima vesti koje pripadaju toj temi.\n"
        "- Linkove ne menjaš, ne prepisuješ ručno i ne izmišljaš.\n\n"
        "OUTPUT FORMAT (strogo obavezan):\n"
        "- Vrati isključivo VALIDAN JSON oblika:\n"
        "{ \"topics\": [ { \"title\": \"...\", \"summary\": \"...\", \"links\": [\"...\", \"...\"] } ] }\n"
        "- Ništa van JSON-a ne sme da se pojavi.\n"
        "- Svi naslovi i ceo summary za svaku temu moraju biti isključivo na SRPSKOM jeziku.\n"
    )

    user_msg = (
        "Ovo su vesti iz poslednjih nekoliko sati. "
        "Iskoristi SVE vesti, bez preskakanja.\n\n"
        + text
    )

    try:
        resp = client.chat.completions.create(
            model="gpt-4.1",
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


# ---------- 3b) POST-PROCESIRANJE TEMA (BEZ POJEDINAČNIH VESTI) ----------

def post_process_topics(topics: list, url_to_item: dict) -> list:
    """
    Uklanja teme koje imaju samo jedan link i prebacuje ih
    u zajedničku temu 'Preostale pojedinačne vesti (kratak pregled)'.
    Svaka preostala vest ide u NOVI RED u summary-ju.
    U KRAJNJEM RSS-U NEMA TEMA SA SAMO JEDNOM VESTI.
    """
    final_topics: list[dict] = []
    leftover_links: list[str] = []

    for t in topics:
        if not isinstance(t, dict):
            continue

        links = [u for u in (t.get("links") or []) if u]
        if len(links) <= 1:
            # ovde skupljamo pojedinačne vesti
            for u in links:
                if u not in leftover_links:
                    leftover_links.append(u)
        else:
            # normalne teme ostaju
            t["links"] = links
            final_topics.append(t)

    # ako nema preostalih, završavamo
    if not leftover_links:
        return final_topics

    # napravi bullet listu, svaki bullet u novom redu
    bullets = []
    for u in leftover_links:
        it = url_to_item.get(u, {})
        title = (it.get("title") or u).strip()
        source = (it.get("source") or "").strip()
        if source:
            bullets.append(f"- {title} ({source})")
        else:
            bullets.append(f"- {title}")

    # HTML sa <br/> između stavki → svaka vest u novom redu
    summary_html = "<br/>".join(bullets)

    leftover_topic = {
        "title": "Preostale pojedinačne vesti (kratak pregled)",
        "summary": summary_html,
        "links": leftover_links,
    }
    final_topics.append(leftover_topic)

    return final_topics


# ---------- 4) RAW RSS (news/news.xml) ----------

def generate_raw_feed(items: list):
    rss = ET.Element("rss", version="2.0")
    ch = ET.SubElement(rss, "channel")

    ET.SubElement(ch, "title").text = "News digest RAW (Danas + BBC)"
    ET.SubElement(ch, "link").text = "https://bulvag.github.io/news/news.xml"
    ET.SubElement(ch, "description").text = "Sirove vesti iz poslednjih sati"
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
    print("DIGEST: Broj tema =", len(topics))

    total_links = 0
    for t in topics:
        links = t.get("links") or []
        total_links += len(links)
    print("DIGEST: Ukupno obrađenih vesti (po linkovima) =", total_links)

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
            html_links = "<br/>".join([f'<a href="{u}">{u}</a>' for u in links])
            body_parts.append("<br/><br/><b>VESTI:</b><br/>" + html_links)

        desc = " ".join(body_parts) if body_parts else "Nema dodatnog sažetka."
        ET.SubElement(item, "description").text = desc

        guid = f"{title}-{datetime.utcnow().isoformat()}"
        ET.SubElement(item, "guid").text = guid
        ET.SubElement(item, "pubDate").text = datetime.utcnow().strftime(
            "%a, %d %b %Y %H:%M:%S GMT"
        )

    ET.ElementTree(rss).write(DIGEST_OUTPUT, encoding="utf-8", xml_declaration=True)
    print("DIGEST OK →", DIGEST_OUTPUT)


# ---------- 6) MAIN LOGIKA (više krugova + čišćenje starih fajlova) ----------

def run_full_digest(items: list, url_to_item: dict, max_rounds: int = 3):
    """
    Pokušavamo više krugova dok sve vesti koje imaju URL
    ne završe u nekoj temi.
    """
    all_urls = set(url_to_item.keys())
    remaining_urls = set(all_urls)
    all_topics: list[dict] = []

    for round_idx in range(1, max_rounds + 1):
        if not remaining_urls:
            break

        # pripremi listu vesti za ovaj krug
        round_items = [url_to_item[u] for u in remaining_urls]
        print(f"ROUND {round_idx}: šaljem {len(round_items)} vesti u model")

        text = build_model_input(round_items)
        topics = call_openai_for_digest(text)

        if not topics:
            print("Model vratio prazan odgovor u ovom krugu.")
            break

        all_topics.extend(topics)

        # pogledaj koje URL-ove je model realno upotrebio
        used_urls = set()
        for t in topics:
            links = t.get("links") or []
            for u in links:
                u = (u or "").strip()
                if u in remaining_urls:
                    used_urls.add(u)

        print(f"ROUND {round_idx}: model iskoristio {len(used_urls)} vesti")

        # ažuriraj remaining
        remaining_urls -= used_urls

        # ako se ništa nije pomerilo, nema svrhe ponavljati
        if not used_urls:
            print("U ovom krugu model nije pokrio nove vesti, prekidam.")
            break

    # ako su neke vesti ostale potpuno nepokrivene, samo ih dodajemo u zajedničku temu
    if remaining_urls:
        print(f"Ostalo još {len(remaining_urls)} vesti koje model nije iskoristio ni u jednoj temi.")
        leftover_links = sorted(remaining_urls)
        bullets = []
        for u in leftover_links:
            it = url_to_item.get(u, {})
            title = (it.get("title") or u).strip()
            source = (it.get("source") or "").strip()
            if source:
                bullets.append(f"- {title} ({source})")
            else:
                bullets.append(f"- {title}")
        summary_html = "<br/>".join(bullets)

        all_topics.append({
            "title": "Vesti koje model nije pokrio",
            "summary": summary_html,
            "links": list(leftover_links),
        })

    return all_topics


def clean_old_raw(days: int = 1):
    """
    Obriši .json fajlove iz RAW_DIR starije od `days` dana
    (po mtime, ne po fetched_at).
    """
    if not RAW_DIR.exists():
        return

    cutoff = datetime.utcnow() - timedelta(days=days)

    for fname in os.listdir(RAW_DIR):
        path = RAW_DIR / fname
        try:
            if path.suffix == ".json" and path.stat().st_mtime < cutoff.timestamp():
                path.unlink()
        except Exception as e:
            print("Greška pri brisanju:", e)


def main():
    items = load_recent_news(hours=6, max_items=200)
    if not items:
        print("Nema vesti, ništa ne radim.")
        clean_old_raw(days=1)
        return

    # mapa URL -> vest
    url_to_item = {}
    for it in items:
        url = (it.get("url") or it.get("link") or "").strip()
        if url:
            url_to_item[url] = it

    # 1) Sirov feed (RAW)
    generate_raw_feed(items)

    # 2) AI digest u više krugova, dok sve vesti ne budu pokrivene
    topics = run_full_digest(items, url_to_item)
    if topics:
        # 3) Post-procesiranje: izbaci pojedinačne teme u zajedničku listu
        topics = post_process_topics(topics, url_to_item)
        generate_digest(topics)
    else:
        print("Nema tema – digest nije generisan.")

    # 4) Čišćenje starih JSON fajlova iz raw/
    clean_old_raw(days=1)


if __name__ == "__main__":
    main()
