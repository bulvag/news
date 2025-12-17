import os, json, re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from html import escape as hesc
import xml.etree.ElementTree as ET
import smtplib, ssl
from email.message import EmailMessage
from urllib.request import urlopen
from urllib.error import URLError, HTTPError

STATE_PATH = "state.json"

RSS_URL = os.environ["RSS_URL"]
EMAIL = os.environ["EMAIL"]
SMTP_PASS = os.environ["SMTP_PASS"]

TO_EMAIL = EMAIL
FROM_EMAIL = EMAIL
SMTP_USER = EMAIL

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))

MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "25"))
FORCE_SEND = os.environ.get("FORCE_SEND", "0") == "1"  # ako staviš 1, šalje i kad "nema novih"

BELGRADE = ZoneInfo("Europe/Belgrade")
SENT_LINKS_LIMIT = 300


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            state = {}
    else:
        state = {}
    state.setdefault("sent_links", [])
    if not isinstance(state["sent_links"], list):
        state["sent_links"] = []
    return state


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def clean(s: str) -> str:
    s = s or ""
    s = re.sub(r"\s+", " ", s).strip()
    return s


def sanitize_desc(html: str) -> str:
    html = html or ""
    html = re.sub(r"(?is)<script.*?>.*?</script>", "", html)
    html = re.sub(r"(?is)<style.*?>.*?</style>", "", html)
    return html


def fetch_xml(url: str) -> str:
    try:
        with urlopen(url) as r:
            return r.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError) as e:
        raise RuntimeError(f"Ne mogu da učitam feed: {e}") from e


def find_text_any(el: ET.Element, local_name: str) -> str:
    """Nađi tekst iz prvog taga sa datim local name, bez obzira na namespace."""
    for child in el.iter():
        tag = child.tag
        if isinstance(tag, str) and tag.endswith("}" + local_name):
            return child.text or ""
        if tag == local_name:
            return child.text or ""
    return ""


def extract_items(xml_text: str):
    root = ET.fromstring(xml_text)

    # 1) RSS: <item> (namespace-agnostic)
    rss_items = root.findall(".//{*}item")
    if not rss_items:
        rss_items = root.findall(".//item")

    items = []

    if rss_items:
        for it in rss_items:
            title = clean(find_text_any(it, "title"))
            link = clean(find_text_any(it, "link"))
            desc = sanitize_desc(find_text_any(it, "description"))
            pub = clean(find_text_any(it, "pubDate"))

            pub_dt = None
            if pub:
                try:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(pub)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    pub_dt = dt.astimezone(timezone.utc)
                except Exception:
                    pub_dt = None

            items.append({
                "title": title,
                "link": link,
                "description_html": desc,
                "pub_dt": pub_dt
            })

        # newest first (ako ima vremena)
        items.sort(key=lambda x: x["pub_dt"] or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True)
        return items

    # 2) Atom: <entry>
    atom_entries = root.findall(".//{*}entry") or root.findall(".//entry")
    for ent in atom_entries:
        title = clean(find_text_any(ent, "title"))

        # Atom link je često <link href="..."/>
        link = ""
        for child in ent.iter():
            tag = child.tag
            if (isinstance(tag, str) and tag.endswith("}link")) or tag == "link":
                href = child.attrib.get("href")
                if href:
                    link = href.strip()
                    break
                if child.text and child.text.strip():
                    link = child.text.strip()
                    break

        # sadržaj: <content> ili <summary>
        desc = find_text_any(ent, "content") or find_text_any(ent, "summary")
        desc = sanitize_desc(desc)

        pub = clean(find_text_any(ent, "updated") or find_text_any(ent, "published"))
        pub_dt = None
        if pub:
            try:
                dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                pub_dt = dt.astimezone(timezone.utc)
            except Exception:
                pub_dt = None

        items.append({
            "title": title,
            "link": link,
            "description_html": desc,
            "pub_dt": pub_dt
        })

    items.sort(key=lambda x: x["pub_dt"] or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True)
    return items


def build_html(items, subject):
    blocks = []
    for x in items:
        safe_title = hesc(x["title"] or "(bez naslova)")
        safe_link = hesc(x["link"] or "", quote=True)

        if x["pub_dt"]:
            local_dt = x["pub_dt"].astimezone(BELGRADE)
            time_str = local_dt.strftime("%Y-%m-%d %H:%M") + " (Beograd)"
        else:
            time_str = "—"

        desc_html = x["description_html"] or ""

        open_link = f'<a href="{safe_link}">Otvori</a>' if safe_link else ""

        blocks.append(f"""
        <div style="border:1px solid #e5e7eb; border-radius:12px; padding:14px; margin:12px 0; background:#fff;">
          <div style="font-weight:800; font-size:16px; margin-bottom:6px;">{safe_title}</div>
          <div style="font-size:12px; color:#6b7280; margin-bottom:10px;">{time_str}</div>
          <div style="font-size:14px; line-height:1.5;">{desc_html}</div>
          <div style="margin-top:10px; font-size:13px;">{open_link}</div>
        </div>
        """)

    safe_subject = hesc(subject)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/></head>
<body style="background:#f9fafb; padding:18px; font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Arial; margin:0;">
  <div style="max-width:900px; margin:0 auto;">
    <div style="margin-bottom:14px;">
      <div style="font-size:20px; font-weight:800; margin:0 0 6px 0;">{safe_subject}</div>
      <div style="color:#6b7280; font-size:12px;">Ukupno tema u mailu: {len(items)}</div>
    </div>
    {''.join(blocks)}
  </div>
</body></html>
"""


def send_email(subject, html_body):
    msg = EmailMessage()
    msg["From"] = FROM_EMAIL
    msg["To"] = TO_EMAIL
    msg["Subject"] = subject
    msg.set_content("Digest je u HTML formatu (otvori u Gmail-u).")
    msg.add_alternative(html_body, subtype="html")

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context) as s:
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)


def main():
    print("RSS_URL:", RSS_URL)
    print("TO_EMAIL:", TO_EMAIL)
    print("SMTP_HOST/PORT:", SMTP_HOST, SMTP_PORT)

    state = load_state()
    sent_links = set(x for x in state.get("sent_links", []) if isinstance(x, str))

    xml = fetch_xml(RSS_URL)
    items = extract_items(xml)

    print("TOTAL items in feed:", len(items))
    if items:
        print("FIRST item title:", (items[0].get("title") or "")[:120])
        print("FIRST item link:", (items[0].get("link") or "")[:200])

    new_items = []
    for x in items:
        link = (x.get("link") or "").strip()
        if not link:
            continue
        if link in sent_links:
            continue
        new_items.append(x)

    print("NEW items:", len(new_items))
    if not new_items and not FORCE_SEND:
        print("Nothing new -> not sending (FORCE_SEND=0)")
        return

    if not new_items and FORCE_SEND:
        print("FORCE_SEND=1 -> sending first MAX_ITEMS from feed")
        new_items = items[:MAX_ITEMS]

    new_items = new_items[:MAX_ITEMS]

    now = datetime.now(BELGRADE)
    slot = "jutro" if now.hour < 12 else "veče"
    subject = f"Vesti digest ({slot}) — {now.strftime('%Y-%m-%d')}"

    html = build_html(new_items, subject)
    print("Sending email... items:", len(new_items))
    send_email(subject, html)
    print("Email sent.")

    # update state
    updated_links = list(sent_links) + [(x.get("link") or "").strip() for x in new_items if (x.get("link") or "").strip()]
    seen = set()
    deduped = []
    for u in updated_links:
        if u in seen:
            continue
        seen.add(u)
        deduped.append(u)
    state["sent_links"] = deduped[-SENT_LINKS_LIMIT:]
    save_state(state)


if __name__ == "__main__":
    main()
