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


# ---------- 2) PRIPREMA TEKSTA ZA MODEL + MAPA ID → URL ----------

def build_model_input(items):
    """
    Za svaku vest pravimo blok sa naslovom, izvorom, tekstom i linkom.
    Full tekst skraćujemo da ne pregori kontekst.
    Vraća:
      - veliki tekst za model
      - mapu {id_vesti (int): url}
    """
    blocks = []
    id_to_url = {}
    MAX_CHARS = 800  # ~400 tokena po vesti

    for i, it in enumerate(items, 1):
        title = (it.get("title") or "").strip()
        source = (it.get("source") or "").strip()
        subtitle = (it.get("subtitle") or "").strip()
        full = (it.get("full_text") or "").strip()
        url = (it.get("url") or it.get("link") or "").strip()

        if len(full) > MAX_CHARS:
            full = full[:MAX_CHARS] + "…"

        # zapamti mapiranje ID -> URL (ako ga ima)
        if url:
            id_to_url[i] = url

        block = f"""VEST {i}
IZVOR: {source}
NASLOV: {title}
PODNASLOV: {subtitle}
TEKST: {full}
LINK: {url}
"""
        blocks.append(block)

    big_text = "\n\n-----\n\n".join(blocks)
    return big_text, id_to_url


# ---------- 3) POZIV OPENAI-a (AI GRUPIŠE PREKO ID-JEVA) ----------

def call_openai_for_digest(text: str) -> list:
    """
    AI SAM bira teme.
    UMESTO linkova, tražimo da vrati listu ID-jeva vesti (brojevi iz 'VEST N').
    JSON format:
    {
      "topics": [
        {
          "title": "...",
          "summary": "...",
          "ids": [1, 5, 23]
        },
        ...
      ]
    }
    """
    if not API_KEY:
        print("Nema API ključa (VESTI), preskačem AI digest.")
        return []

    system_msg = (
    "Ti si napredni analitičar vesti. Odgovaraj ISKLJUČIVO na srpskom jeziku.\n\n"
    "Dobićeš veliki broj vesti iz različitih izvora. Svaku vest moraš obraditi — ništa se ne preskače.\n\n"
    "Tvoj zadatak:\n"
    "1. Automatski grupiši vesti u tematske celine:\n"
    "   – koristi semantičko razumevanje,\n"
    "   – teme formiraj inteligentno (ne po ključnim rečima),\n"
    "   – grupiši vesti koje govore o istom događaju, istoj situaciji, istoj državi, regionu ili narativu,\n"
    "   – napravi onoliko tema koliko je zaista potrebno (10, 20, 30… neograničeno).\n\n"
    "2. Za svaku temu kreiraj:\n"
    "   • \"title\" – kratak, precizan tematski naslov,\n"
    "   • \"summary\" – sažetak od 5–12 jasnih rečenica koji opisuje šta se dešava,\n"
    "   • \"links\" – SVE URL-ove vesti koje pripadaju toj temi.\n\n"
    "3. Pravila:\n"
    "   – apsolutno ni jedna vest ne sme ostati van tema,\n"
    "   – ne izmišljaš ništa,\n"
    "   – ne grupišeš po redosledu, nego po značenju,\n"
    "   – ako je vest usamljena, napravi joj samostalnu temu.\n\n"
    "Vrati isključivo VALIDAN JSON bez ičega van njega:\n"
    "{\n"
    "  \"topics\": [\n"
    "    {\n"
    "      \"title\": \"...\",\n"
    "      \"summary\": \"...\",\n"
    "      \"links\": [\"...\", \"...\"]\n"
    "    }\n"
    "  ]\n"
    "}\n"
)

    user_msg = (
        "Ovo su vesti iz poslednjih nekoliko sati (svaka počinje sa 'VEST N'). "
        "Iskoristi SVE vesti, bez preskakanja.\n\n",
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


# ---------- 3b) MAPIRANJE ID-JEVA NA LINKOVE ----------

def attach_links_to_topics(topics: list, id_to_url: dict):
    """
    U svaki topic ubacuje 'links' na osnovu 'ids' i mape id_to_url.
    """
    for t in topics:
        raw_ids = t.get("ids") or []
        links = []
        for _id in raw_ids:
            # model može da vrati int ili string
            try:
                i = int(_id)
            except (TypeError, ValueError):
                continue
            url = id_to_url.get(i)
            if url and url not in links:
                links.append(url)
        t["links"] = links


# ---------- 3c) POPUNI NEDOSTAJAJUĆE VESTI ----------

def fill_missing_ids(topics: list, id_to_url: dict):
    """
    Nađi sve ID-jeve koji NISU ni u jednoj temi i gurni ih u posebnu temu
    'Pojedinačne vesti (bez tematske grupe)'.
    """
    all_ids = set(id_to_url.keys())
    used_ids = set()

    for t in topics:
        for _id in (t.get("ids") or []):
            try:
                used_ids.add(int(_id))
            except (TypeError, ValueError):
                continue

    missing_ids = sorted(all_ids - used_ids)

    if not missing_ids:
        print("Svi ID-jevi su pokriveni u temama.")
        return

    extra_links = []
    for i in missing_ids:
        url = id_to_url.get(i)
        if url and url not in extra_links:
            extra_links.append(url)

    if not extra_links:
        print("Postoje ID-jevi bez URL-a, preskačem dodatnu temu.")
        return

    topics.append({
        "title": "Pojedinačne vesti (bez tematske grupe)",
        "summary": "Ove vesti nisu ušle u veće tematske celine, ali su i dalje relevantne.",
        "ids": missing_ids,
        "links": extra_links,
    })

    print(f"Dodat ekstra topic sa {len(missing_ids)} preostalih vesti.")


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


# ---------- 6) MAIN ----------

def run_full_digest(items: list, max_rounds: int = 3):
    """
    Pokušavamo više krugova dok sve vesti koje imaju URL
    ne završe u nekoj temi.
    """
    # mapa: url -> vest
    url_to_item = {}
    for it in items:
        url = (it.get("url") or it.get("link") or "").strip()
        if url:
            url_to_item[url] = it

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

    # ako i dalje ima vesti koje nisu upakovane u temu,
    # pravimo poseban poslednji poziv: svaka vest = posebna tema
    if remaining_urls:
        leftover_items = [url_to_item[u] for u in remaining_urls]
        print(f"Ostalo još {len(leftover_items)} vesti – pravim posebne teme za svaku.")

        # od ovih vesti pravimo tekst gde eksplicitno tražimo:
        # "svaka vest = jedna tema"
        text = build_model_input(leftover_items)
        system_msg = (
            "Za ove vesti napravi PO JEDNU TEMU ZA SVAKU vest.\n"
            "Za svaku vest:\n"
            "- 'title' neka bude naslov vesti,\n"
            "- 'summary' 3–6 rečenica na srpskom o toj jednoj vesti,\n"
            "- 'links' neka sadrži samo URL te vesti.\n"
            "Vrati JSON sa ključem 'topics', bez dodatnog teksta."
        )
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_msg},
                    {
                        "role": "user",
                        "content": "Ovo su vesti. Svaka vest treba da bude posebna tema:\n\n"
                        + text,
                    },
                ],
                temperature=0.2,
            )
            content = resp.choices[0].message.content.strip()
            s = content[content.find("{") : content.rfind("}") + 1]
            data = json.loads(s)
            extra_topics = data.get("topics", [])
            print(f"Posebne teme za preostale vesti: {len(extra_topics)}")
            all_topics.extend(extra_topics)
        except Exception as e:
            print("Greška u poslednjem krugu za preostale vesti:", e)

    return all_topics


def main():
    items = load_recent_news(hours=6, max_items=200)
    if not items:
        print("Nema vesti, ništa ne radim.")
        return

    # 1) Sirov feed (RAW)
    generate_raw_feed(items)

    # 2) AI digest u više krugova, dok sve vesti ne budu pokrivene
    topics = run_full_digest(items)
    if topics:
        generate_digest(topics)
    else:
        print("Nema tema – digest nije generisan.")

if __name__ == "__main__":
    main()
