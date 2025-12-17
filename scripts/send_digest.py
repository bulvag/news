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

# --- REQUIRED ENV ---
RSS_URL = os.environ["RSS_URL"]
EMAIL = os.environ["EMAIL"]
SMTP_PASS = os.environ["SMTP_PASS"]

# --- Derived (send from same Gmail to same Gmail) ---
TO_EMAIL = EMAIL
FROM_EMAIL = EMAIL
SMTP_USER = EMAIL

# --- Optional env (defaults to Gmail) ---
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))

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

    # čuvamo samo sent_links (last_sent_iso ostaje kompatibilno ako već postoji)
    state.setdefault("last_sent_iso", "1970-01-01T00:00:00Z")
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


def fetch_rss(url: str) -> str:
    try:
        with urlopen(url) as r:
            return r.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError) as e:
        raise RuntimeError(f"Ne mogu da učitam RSS: {e}") from e


def extract_items(xml_text: str):
    root = ET.fromstring(xml_text)
    channel = root.find("channel")
    if channel is None:
        return []

    items = []
    for it in channel.findall("item"):
        title = clean(it.findtext("title"))
        link = clean(it.findtext("link"))
        desc = sanitize_desc(it.findtext("description") or "")

        # pubDate nam više ne odlučuje da li je novo (samo za prikaz u mailu)
        pub = clean(it.findtext("pubDate"))
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

    # sortiraj: ako ima pub_dt, newest first; ako nema, ostavi redosled (stable sort)
    items.sort(key=lambda x: x["pub_dt"] or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True)
    return items


def build_html(items, subject):
    blocks = []
    for x in items:
        safe_title = hesc(x["title"])
        safe_link = hesc(x["link"], quote=True)

        if x["pub_dt"]:
            local_dt = x["pub_dt"].astimezone(BELGRADE)
            time_str = local_dt.strftime("%Y-%m-%d %H:%M") + " (Beograd)"
        else:
            time_str = "—"

        desc_html = x["description_html"]

        blocks.append(f"""
        <div style="border:1px solid #e5e7eb; border-radius:12px; padding:14px; margin:12px 0; background:#fff;">
          <div style="font-weight:800; font-size:16px; margin-bottom:6px;">{safe_title}</div>
          <div style="font-size:12px; color:#6b7280; margin-bottom:10px;">{time_str}</div>
          <div style="font-size:14px; line-height:1.5;">{desc_html}</div>
          <div style="margin-top:10px; font-size:13px;">
            <a href="{safe_link}">Otvori</a>
          </div>
        </div>
        """)

    safe_subject = hesc(subject)
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
</head>
<body style="background:#f9fafb; padding:18px; font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Arial; margin:0;">
  <div style="max-width:900px; margin:0 auto;">
    <div style="margin-bottom:14px;">
      <div style="font-size:20px; font-weight:800; margin:0 0 6px 0;">{safe_subject}</div>
      <div style="color:#6b7280; font-size:12px;">Ukupno novih tema: {len(items)}</div>
    </div>
    {''.join(blocks)}
  </div>
</body>
</html>
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

    rss = fetch_rss(RSS_URL)
    items = extract_items(rss)

    # NOVO: “novo” = link koji još nije poslat (ne oslanjamo se na pubDate)
    new_items = []
    for x in items:
        if not x["link"]:
            continue
        if x["link"] in sent_links:
            continue
        new_items.append(x)

    if not new_items:
        return

    MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "25"))
    new_items = new_items[:MAX_ITEMS]

    now = datetime.now(BELGRADE)
    slot = "jutro" if now.hour < 12 else "veče"
    subject = f"Vesti digest ({slot}) — {now.strftime('%Y-%m-%d')}"

    html = build_html(new_items, subject)
    send_email(subject, html)

    # update state: dodaj poslate linkove (rolling, dedup)
    updated_links = list(sent_links) + [x["link"] for x in new_items]
    seen = set()
    deduped = []
    for u in updated_links:
        if u in seen:
            continue
        seen.add(u)
        deduped.append(u)
    state["sent_links"] = deduped[-SENT_LINKS_LIMIT:]

    # last_sent_iso više nije bitan, ali ga ostavljamo kompatibilno
    state["last_sent_iso"] = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    save_state(state)


if __name__ == "__main__":
    main()
