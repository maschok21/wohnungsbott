#!/usr/bin/env python3
"""
Wohnungsbot fuer Kleinanzeigen.de
----------------------------------
Durchsucht eine oder mehrere Kleinanzeigen-Suchergebnisseiten, filtert die
Treffer nach deinen Kriterien und schickt neue, passende Inserate per
Telegram an dich.

Wird per Cronjob / GitHub Actions / systemd-Timer alle paar Minuten gestartet
(ein Skriptlauf = eine Pruefung, kein Daemon).
"""

import json
import os
import re
import sys
import time
from html import escape

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# 1) KONFIGURATION - hier alles eintragen / anpassen
# ---------------------------------------------------------------------------

# Telegram: Token kommt von @BotFather, Chat-ID ermittelst du wie in der
# Anleitung beschrieben. Wird AUSSCHLIESSLICH aus Umgebungsvariablen gelesen -
# kein Klartext-Fallback im Code, damit nichts im (oeffentlichen!) Repo landet.
# Lokal: vor dem Start exportieren, siehe Anleitung Schritt 5.
# GitHub Actions: als Repository-Secrets hinterlegen, siehe Anleitung Schritt 6.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    sys.exit(
        "Fehler: TELEGRAM_BOT_TOKEN und/oder TELEGRAM_CHAT_ID sind nicht gesetzt.\n"
        "Lokal:  export TELEGRAM_BOT_TOKEN=dein_token && export TELEGRAM_CHAT_ID=deine_id\n"
        "GitHub Actions: als Repository-Secrets anlegen (Settings > Secrets and "
        "variables > Actions)."
    )

# Eine oder mehrere Kleinanzeigen-Such-URLs (z.B. ein Stadtteil oder eine
# ganze Stadt). WICHTIG: keine Radius- oder Filter-URLs (.../r10, /preis:,
# /anbieter:, /sortierung: ...) verwenden - diese sind laut robots.txt fuer
# automatisierte Zugriffe gesperrt. Nur "nackte" Kategorie+Ort-URLs nehmen,
# z.B. https://www.kleinanzeigen.de/s-wohnung-mieten/berlin/c203l3331
SEARCH_URLS = [
    "https://www.kleinanzeigen.de/s-wohnung-mieten/duesseldorf/c203l2068",
]

# Preisobergrenze in Euro (Kaltmiete/Gesamtmiete je nachdem was im Inserat
# steht). None = kein Preisfilter.
MAX_PRICE_EUR = 750

# Mindestanzahl Zimmer / Mindestgroesse in qm. None = kein Filter.
MIN_ROOMS = 2
MIN_SIZE_QM = 50

# Begriffe, bei denen ein Inserat NIEMALS durchkommen soll (Gross-/
# Kleinschreibung egal). Hier stehen schon Tauschwohnung und WG drin, wie
# gewuenscht - einfach erweitern.
EXCLUDE_KEYWORDS = [
    r"tauschwohnung",
    r"wohnungstausch",
    r"tausche\b",
    r"\btausch\b",
    r"\bwg\b",
    r"wg-zimmer",
    r"wg gesucht",
    r"wbs erforderlich",
    r"wbs notwendig",
]

# Optional: Wenn hier Begriffe stehen, muss MINDESTENS EINER davon im
# Inserat vorkommen (z.B. ["balkon", "altbau"]). Leere Liste = kein
# Positiv-Filter, alles wird zugelassen, was nicht ausgeschlossen wurde.
INCLUDE_KEYWORDS = []

# Wo die schon gesehenen Anzeigen-IDs gespeichert werden, damit du nicht
# jedes Mal eine Benachrichtigung fuer dieselbe Anzeige bekommst.
SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_ads.json")

# Hoeflichkeitspause zwischen mehreren Anfragen (Sekunden).
DELAY_BETWEEN_REQUESTS = 3

# Wie viele Ergebnisseiten PRO Such-URL abgerufen werden (eine Seite hat ca.
# 25 Inserate). Ohne das wird nur Seite 1 (= neueste Inserate) geprueft - bei
# engen Filtern (z.B. niedrige Preisobergrenze) kann es sein, dass auf Seite 1
# zufaellig kein einziger Treffer liegt, obwohl es auf Seite 2/3 etc. welche
# gibt. Mehr Seiten = hoehere Trefferchance, aber auch mehr Anfragen pro Lauf.
PAGES_PER_SEARCH_URL = 3

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9",
}

EXCLUDE_PATTERN = re.compile("|".join(EXCLUDE_KEYWORDS), re.IGNORECASE) if EXCLUDE_KEYWORDS else None
INCLUDE_PATTERN = re.compile("|".join(INCLUDE_KEYWORDS), re.IGNORECASE) if INCLUDE_KEYWORDS else None


# ---------------------------------------------------------------------------
# 2) GESEHENE ANZEIGEN SPEICHERN
# ---------------------------------------------------------------------------

def load_seen() -> set:
    if not os.path.exists(SEEN_FILE):
        return set()
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (json.JSONDecodeError, OSError):
        return set()


def save_seen(seen: set) -> None:
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f)


# ---------------------------------------------------------------------------
# 3) SUCHERGEBNISSEITE LADEN UND PARSEN
# ---------------------------------------------------------------------------

def fetch_html(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text


def build_page_url(base_url: str, page: int) -> str:
    """Baut die URL fuer Ergebnisseite `page` (1-basiert) nach dem
    Kleinanzeigen-Pagination-Schema. Seite 1 = Basis-URL unveraendert, ab
    Seite 2 wird ein "seite:N/"-Segment direkt nach dem "s-..."-Kategorieteil
    eingefuegt, z.B.:
    https://www.kleinanzeigen.de/s-wohnung-mieten/duesseldorf/c203l2068
    -> https://www.kleinanzeigen.de/s-wohnung-mieten/seite:2/duesseldorf/c203l2068
    """
    if page <= 1:
        return base_url

    parts = base_url.split("/")
    for i, part in enumerate(parts):
        if part.startswith("s-"):
            parts.insert(i + 1, f"seite:{page}")
            break
    return "/".join(parts)


def _parse_price(price_text: str):
    digits = re.sub(r"[^\d]", "", price_text or "")
    return int(digits) if digits else None


def _parse_rooms(tags_text: str):
    m = re.search(r"([\d,.]+)\s*Zi", tags_text or "")
    return float(m.group(1).replace(",", ".")) if m else None


def _parse_size(tags_text: str):
    m = re.search(r"([\d,.]+)\s*m²", tags_text or "")
    return float(m.group(1).replace(",", ".")) if m else None


def parse_listings(html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    listings = []

    for article in soup.select("article.aditem"):
        ad_id = article.get("data-adid")
        href = article.get("data-href")
        if not ad_id or not href:
            continue

        title_el = article.select_one("a.ellipsis")
        title = title_el.get_text(strip=True) if title_el else ""

        desc_el = article.select_one(".aditem-main--middle--description")
        description = desc_el.get_text(strip=True) if desc_el else ""

        location_el = article.select_one(".aditem-main--top--left")
        location = location_el.get_text(strip=True) if location_el else ""

        tags_el = article.select_one(".aditem-main--middle--tags")
        tags_text = tags_el.get_text(" ", strip=True) if tags_el else ""

        price_el = article.select_one(".aditem-main--middle--price-shipping--price")
        if price_el:
            # Bei reduzierten Preisen steckt der alte (durchgestrichene) Preis
            # als verschachtelter Span mit drin - den vorher entfernen, sonst
            # werden beide Zahlen zusammengeklebt.
            old_price_el = price_el.select_one(".aditem-main--middle--price-shipping--old-price")
            if old_price_el:
                old_price_el.extract()
            price_text = price_el.get_text(strip=True)
        else:
            price_text = ""

        is_private = "Von Privat" in article.get_text()

        listings.append({
            "id": ad_id,
            "url": "https://www.kleinanzeigen.de" + href,
            "title": title,
            "description": description,
            "location": location,
            "tags_text": tags_text,
            "price_text": price_text or "VB / k.A.",
            "price": _parse_price(price_text),
            "rooms": _parse_rooms(tags_text),
            "size_qm": _parse_size(tags_text),
            "is_private": is_private,
        })

    return listings


# ---------------------------------------------------------------------------
# 4) FILTERLOGIK
# ---------------------------------------------------------------------------

def filter_reason(listing: dict) -> str:
    """Prueft alle Filter und gibt 'OK' zurueck, wenn die Anzeige durchkommt,
    sonst einen kurzen Klartext-Grund, an welchem Filter sie gescheitert ist.
    Wird sowohl fuers eigentliche Filtern als auch fuers Debug-Logging in
    main() benutzt, damit man sieht, WARUM eine Anzeige nicht gemeldet wird."""
    combined_text = " ".join([
        listing["title"], listing["description"], listing["tags_text"]
    ])

    if EXCLUDE_PATTERN:
        match = EXCLUDE_PATTERN.search(combined_text)
        if match:
            return f"ausgeschlossen durch Keyword '{match.group(0)}'"

    if INCLUDE_PATTERN and not INCLUDE_PATTERN.search(combined_text):
        return "kein INCLUDE_KEYWORD im Text gefunden"

    if MAX_PRICE_EUR is not None and listing["price"] is not None:
        if listing["price"] > MAX_PRICE_EUR:
            return f"Preis {listing['price']}€ > MAX_PRICE_EUR {MAX_PRICE_EUR}€"

    if MIN_ROOMS is not None and listing["rooms"] is not None:
        if listing["rooms"] < MIN_ROOMS:
            return f"Zimmer {listing['rooms']} < MIN_ROOMS {MIN_ROOMS}"

    if MIN_SIZE_QM is not None and listing["size_qm"] is not None:
        if listing["size_qm"] < MIN_SIZE_QM:
            return f"Groesse {listing['size_qm']}m² < MIN_SIZE_QM {MIN_SIZE_QM}m²"

    return "OK"


def passes_filters(listing: dict) -> bool:
    return filter_reason(listing) == "OK"


# ---------------------------------------------------------------------------
# 5) TELEGRAM-NACHRICHT SENDEN
# ---------------------------------------------------------------------------

def send_telegram_message(listing: dict) -> None:
    text = (
        f"<b>{escape(listing['title'])}</b>\n"
        f"{escape(listing['price_text'])}"
        f"{' | ' + escape(listing['tags_text']) if listing['tags_text'] else ''}\n"
        f"{escape(listing['location'])}"
        f"{' | Privat' if listing['is_private'] else ' | Gewerblich'}\n\n"
        f"{escape(listing['description'][:300])}\n\n"
        f"{listing['url']}"
    )

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    resp = requests.post(api_url, data=payload, timeout=15)
    if not resp.ok:
        print(f"Telegram-Fehler ({resp.status_code}): {resp.text}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 6) HAUPTPROGRAMM
# ---------------------------------------------------------------------------

def main() -> None:
    seen = load_seen()
    new_count = 0

    for url in SEARCH_URLS:
        for page in range(1, PAGES_PER_SEARCH_URL + 1):
            page_url = build_page_url(url, page)
            try:
                html = fetch_html(page_url)
            except requests.RequestException as exc:
                print(f"Konnte {page_url} nicht laden: {exc}", file=sys.stderr)
                break

            listings = parse_listings(html)
            print(f"{page_url} -> {len(listings)} Inserate gefunden")

            if not listings:
                # Keine Inserate mehr -> letzte Seite war schon erreicht.
                break

            for listing in listings:
                reason = filter_reason(listing)
                status = "neu" if listing["id"] not in seen else "schon gesehen"
                print(
                    f"  [{listing['id']}] ({status}) {reason} | "
                    f"{listing['price_text']} | {listing['tags_text']} | "
                    f"{listing['title'][:70]}"
                )

                if listing["id"] in seen:
                    continue
                seen.add(listing["id"])

                if reason == "OK":
                    send_telegram_message(listing)
                    new_count += 1

            time.sleep(DELAY_BETWEEN_REQUESTS)

    save_seen(seen)
    print(f"Fertig. {new_count} neue Benachrichtigung(en) verschickt.")


if __name__ == "__main__":
    main()
