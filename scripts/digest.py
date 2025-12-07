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


# ---------- 2) PRIPREMA TEKSTA ZA MODEL ----------

def build_model_input(items):
    """
    Za svaku vest pravimo blok sa naslovom, izvorom, tekstom i linkom.
    Full tekst skraćujemo da ne pregori kontekst.
    Vraća jedan veliki string za slanje modelu.
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


# ---------- 3) POZIV OPENAI-a (AI GRUPIŠE PREKO ID-JEVA) ----------

def call_openai_for_digest(text: str) -> list:
    """
    AI SAM bira teme.
    Sada tražimo da direktno vrati 'links' umesto ID-jeva.
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
        "OBAVEZNE BLOK-TEME (ako postoje vesti):\n"
        "- 'Rusija – Ukrajina – Belorusija' – sve vesti iz tog rata / regiona (borbe, napadi, sankcije, izbori, unutrašnja politika), "
        "ali unutar summary-ja jasno razdvajaj podteme.\n"
        "- 'Izrael – Palestina – Bliski istok' – rat, diplomatija, UN, protesti, napadi, pogibije.\n"
        "- 'Protesti u Srbiji' – SVE vesti o protestima, blokadama, okupljanjima, zahtevima, incidentima i reakcijama u Srbiji.\n"
        "- 'Sport' – JEDNA jedina tema za sve sportove. Unutar summary-ja obavezno pravi podceline: "
        "fudbal, košarka, tenis, Formula 1 (ako je ima), reprezentacije Srbije (ako ih ima), ostali sportovi.\n\n"
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
        "Ovo su vesti iz poslednjih nekoliko sati (svaka počinje sa 'VEST N'). "
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
        ET.SubElement(item, "description").text = desc   # ← OVO POPRAVLJAMO

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
    "Za ove vesti napravi JEDNU temu.\n"
    "Unutar te teme napravi više podgrupa (tematskih sekcija), kao podnaslove.\n"
    "Svaka podgrupa treba da ima naziv i listu vesti koje joj pripadaju.\n"
    "Podgrupe biraj semantički: država, region, politika, sport, kultura, ekonomija, protesti, vojska...\n"
    "Na kraju te jedne teme dodaj klasičan summary od 5–10 rečenica.\n"
    "Na kraju stavi klasičnu listu linkova svih vesti.\n"
    "Vrati JSON formata:\n"
    "{\n"
    "  \"topics\": [\n"
    "    {\n"
    "      \"title\": \"Preostale vesti (tematski pregled)\",\n"
    "      \"summary\": \"...\",\n"
    "      \"links\": [\"...\"]\n"
    "    }\n"
    "  ]\n"
    "}\n"
)
        try:
            resp = client.chat.completions.create(
                model="gpt-4.1",
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

# ----------------------------------------
# BRISANJE STARIH VESTI (npr. starijih od 1 dan)
# ----------------------------------------

cutoff = datetime.utcnow() - timedelta(days=1)

for fname in os.listdir(RAW_DIR):
    path = RAW_DIR / fname
    try:
        if path.suffix == ".json" and path.stat().st_mtime < cutoff.timestamp():
            path.unlink()
    except Exception as e:
        print("Greška pri brisanju:", e)
