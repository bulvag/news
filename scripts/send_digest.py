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
FORCE_SEND = os.environ.get("FORCE_SEND", "0") == "1"      # 1 = pošalji i kad nema novih (test)
SEND_EMPTY = os.environ.get("SEND_EMPTY", "0") == "1"      # 1 = pošalji “nema novih” mail

BELGRADE = ZoneInfo("Europe/Belgrade")
SENT_LINKS_LIMIT = 300

URL_RE = re.compile(r"https?://[^\s\"'<>()]+", re.IGNORECASE)


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
    for child in el.iter():
        tag = child.tag
        if isinstance(tag, str) and tag.endswith("}" + local_name):
            return child.text or ""
        if tag == local_name:
            return child.text or ""
    return ""


def extract_items(xml_text: str):
    root = ET.fromstring(xml_text)
    rss_items = root.findall(".//{*}item") or root.findall(".//item")
    items = []

    for it in rss_items:
        title = clean(find_text_any(it, "title"))
        link = clean(find_text_any(it, "link"))
        guid = clean(find_text_any(it, "guid"))
        desc = sanitize_desc(find_text_any(it, "description"))

        final_link = link
        if not final_link and guid and guid.startswith("http"):
            final_link = guid
        if not final_link:
            m = URL_RE.search(desc or "")
            if m:
                final_link = m.group(0)

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
            "link": final_link,
            "description_html": desc,
            "pub_dt": pub_dt
        })

    items.sort(key=lambda x: x["pub_dt"] or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True)
    return items


def build_html(items, subject, empty_note: str | None = None):
    if empty_note:
        note = f"""
        <div style="border:1px solid #e5e7eb; border-radius:12px; padding:14px; margin:12px 0; background:#fff;">
          <div style="font-weight:800; font-size:16px; margin-bottom:6px;">Nema novih vesti u ovom terminu</div>
          <div style="font-size:14px; line-height:1.5; color:#374151;">{hesc(empty_note)}</div>
        </div>
        """
        blocks_html = note
        count = 0
    else:
        blocks = []
        for x in items:
            safe_title = hesc(x["title"] or "(bez naslova)")
            link_val = (x.get("link") or "").strip()
            safe_link = hesc(link_val, quote=True) if link_val else ""

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
        blocks_html = "".join(blocks)
        count = len(items)

    safe_subject = hesc(subject)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/></head>
<body style="background:#f9fafb; padding:18px; font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Arial; margin:0;">
  <div style="max-width:900px; margin:0 auto;">
    <div style="margin-bottom:14px;">
      <div style="font-size:20px; font-weight:800; margin:0 0 6px 0;">{safe_subject}</div>
      <div style="color:#6b7280; font-size:12px;">Ukupno tema u mailu: {count}</div>
    </div>
    {blocks_html}
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
    state = load_state()
    sent_links = set(x for x in state.get("sent_links", []) if isinstance(x, str))

    xml = fetch_xml(RSS_URL)
    items = extract_items(xml)

    new_items = []
    for x in items:
        link = (x.get("link") or "").strip()
        key = link if link else (x.get("title") or "").strip()
        if not key:
            continue
        if key in sent_links:
            continue
        new_items.append(x)

    now = datetime.now(BELGRADE)
    slot = "jutro" if now.hour < 12 else "veče"
    subject = f"Vesti digest ({slot}) — {now.strftime('%Y-%m-%d')}"

    if not new_items and not FORCE_SEND and not SEND_EMPTY:
        return

    if not new_items and FORCE_SEND:
        new_items = items[:MAX_ITEMS]

    if not new_items and SEND_EMPTY:
        html = build_html([], subject, empty_note="Digest feed je ažuriran, ali nema novih tema u odnosu na prethodno poslato.")
        send_email(subject, html)
        return

    new_items = new_items[:MAX_ITEMS]
    html = build_html(new_items, subject)
    send_email(subject, html)

    updated = list(sent_links)
    for x in new_items:
        link = (x.get("link") or "").strip()
        key = link if link else (x.get("title") or "").strip()
        if key:
            updated.append(key)

    seen = set()
    deduped = []
    for u in updated:
        if u in seen:
            continue
        seen.add(u)
        deduped.append(u)

    state["sent_links"] = deduped[-SENT_LINKS_LIMIT:]
    save_state(state)


if __name__ == "__main__":
    main()
