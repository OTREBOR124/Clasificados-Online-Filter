#!/usr/bin/env python3
"""
Buscador de propiedades - ClasificadosOnline (Puerto Rico)

Scans the newest real-estate listings on clasificadosonline.com, keeps track of
which ones it has already seen, filters them against config.json, and writes a
static page to docs/index.html.

Run locally:   python scraper.py
Debug parsing: python scraper.py --debug     (saves raw HTML to data/debug_page.html)
"""

import argparse
import html
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
DOCS_DIR = os.path.join(ROOT, "docs")
STATE_PATH = os.path.join(DATA_DIR, "listings.json")
CONFIG_PATH = os.path.join(ROOT, "config.json")

BASE = "https://www.clasificadosonline.com"
LIST_PATH = "/clasificados/BienesRaices/RealEstate/default.asp?Version=1&offset={offset}"
DETAIL_URL = BASE + "/UDRealEstateDetail.asp?ID={id}"
PER_PAGE = 15
AST = timezone(timedelta(hours=-4))  # Puerto Rico, no DST

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; personal-listing-watcher/1.0; low-volume personal use)",
    "Accept-Language": "es-PR,es;q=0.9,en;q=0.8",
}

DETAIL_RE = re.compile(r"UDRealEstateDetail\.asp\?(?:ID|REForSaleAdID)=(\d+)", re.I)
PARTNER_RE = re.compile(r"PartnersListingREID\.asp", re.I)
PRICE_RE = re.compile(r"\$\s?([\d,]{3,})")
COUNT_RE = re.compile(r"(\d[\d,]*)\s*al\s*(\d[\d,]*)\s*de\s*(\d[\d,]*)", re.I)
BEDS_RE = re.compile(r"Habitaciones\s*(>?=?\s*\d+)", re.I)
BATHS_RE = re.compile(r"Bathrooms\s*(\d+(?:\s*1/2)?)", re.I)
FALLBACK_ROOMS_RE = re.compile(r"(>?=?\s*\d+)\s*-\s*(\d+(?:\s*1/2)?)\s*\$")

PROPERTY_TYPES = [
    "Apartamento/WalkUp", "Apartamento", "MultiFamiliar", "Multi Familiar",
    "Town House", "Casa", "Solar", "Terreno", "Finca", "Chalet", "Condominio",
    "Local", "Comercial", "Edificio", "Oficina", "Panteon",
]

MUNICIPIOS = [
    "Adjuntas", "Aguada", "Aguadilla", "Aguas Buenas", "Aibonito", "Añasco",
    "Arecibo", "Arroyo", "Barceloneta", "Barranquitas", "Bayamón", "Cabo Rojo",
    "Caguas", "Camuy", "Canóvanas", "Carolina", "Cataño", "Cayey", "Ceiba",
    "Ciales", "Cidra", "Coamo", "Comerío", "Corozal", "Culebra", "Dorado",
    "Fajardo", "Florida", "Guánica", "Guayama", "Guayanilla", "Guaynabo",
    "Gurabo", "Hatillo", "Hormigueros", "Humacao", "Isabela", "Jayuya",
    "Juana Díaz", "Juncos", "Lajas", "Lares", "Las Marías", "Las Piedras",
    "Loíza", "Luquillo", "Manatí", "Maricao", "Maunabo", "Mayagüez", "Moca",
    "Morovis", "Naguabo", "Naranjito", "Orocovis", "Patillas", "Peñuelas",
    "Ponce", "Quebradillas", "Rincón", "Río Grande", "Sabana Grande",
    "Salinas", "San Germán", "San Juan", "San Lorenzo", "San Sebastián",
    "Santa Isabel", "Toa Alta", "Toa Baja", "Trujillo Alto", "Utuado",
    "Vega Alta", "Vega Baja", "Vieques", "Villalba", "Yabucoa", "Yauco",
]

# ClasificadosOnline splits a few municipalities into zones. These are their
# exact labels, taken from the site's own area picker.
COL_AREAS = [
    "Carolina - Isla Verde", "Humacao - Palmas", "San Juan - Condado-Miramar",
    "San Juan - Hato Rey", "San Juan - Río Piedras", "San Juan - Santurce",
    "San Juan - Viejo SJ", "Toa Baja - Levittown",
]


# ---------------------------------------------------------------- helpers

def plain(text):
    """Lowercase, accent-stripped, whitespace-collapsed — for matching."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text).strip().lower()


MUNI_INDEX = sorted(((plain(m), m) for m in MUNICIPIOS), key=lambda p: -len(p[0]))
TYPE_INDEX = sorted(((plain(t), t) for t in PROPERTY_TYPES), key=lambda p: -len(p[0]))


def zone_key(text):
    """Normalizes 'San Juan - Hato Rey' and 'San Juan-Hato Rey' to one form."""
    return re.sub(r"\s*-\s*", "-", plain(text))


AREA_INDEX = {zone_key(a): a for a in COL_AREAS}


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1, sort_keys=True)


# ---------------------------------------------------------------- fetching

def fetch_page(session, offset, debug=False):
    url = BASE + LIST_PATH.format(offset=offset)
    resp = session.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    # The site serves Windows-1252 without always declaring it.
    declared = (resp.encoding or "").lower()
    if declared in ("", "iso-8859-1", "latin-1", "latin1"):
        resp.encoding = "cp1252"
    if debug:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(os.path.join(DATA_DIR, "debug_page.html"), "w", encoding="utf-8") as fh:
            fh.write(resp.text)
        print(f"  saved raw HTML -> data/debug_page.html ({len(resp.text)} chars)")
    return resp.text


def total_count(page_html):
    """Reads the '5641 al 5655 de 9461' counter."""
    match = COUNT_RE.search(BeautifulSoup(page_html, "html.parser").get_text(" "))
    if not match:
        return None
    return int(match.group(3).replace(",", ""))


# ---------------------------------------------------------------- parsing

def ids_under(node):
    found = set()
    for anchor in node.find_all("a", href=True):
        match = DETAIL_RE.search(anchor["href"])
        if match:
            found.add(match.group(1))
    return found


def enclosing_block(anchor, listing_id):
    """Largest ancestor that still describes only this one listing."""
    node, best = anchor, anchor.parent
    for _ in range(12):
        parent = node.parent
        if parent is None or parent.name in ("body", "html", "[document]"):
            break
        if ids_under(parent) - {listing_id}:
            break
        node = parent
        if "$" in node.get_text():
            best = node
    return best or anchor.parent


def find_first(index, text):
    for needle, label in index:
        if needle in text:
            return label
    return None


def find_location(flat):
    """Returns (municipio, zona).

    The city cell renders as '<Municipio>PR' or '<Municipio> - <Zona>PR'.
    Anchoring to that PR suffix matters: broker names carry town names too
    ("KW Grand Homes Mayaguez" on a listing in Lares), and a loose search picks
    the wrong one.
    """
    for match in re.finditer(r"pr(?![a-z])", flat):
        window = flat[max(0, match.start() - 60):match.start()]
        for needle, label in MUNI_INDEX:
            at = window.rfind(needle)  # nearest to the PR marker, not the first mention
            if at == -1:
                continue
            tail = window[at + len(needle):]
            if not re.fullmatch(r"[ ,.\-a-z0-9]{0,25}", tail):
                continue
            zona = AREA_INDEX.get(zone_key(window[at:]))
            return label, zona or label

    caption = re.search(r"bienes raices (.{2,45}?) real estate", flat)
    if caption:
        for needle, label in MUNI_INDEX:
            if caption.group(1).startswith(needle):
                zona = AREA_INDEX.get(zone_key(caption.group(1)))
                return label, zona or label

    fallback = find_first(MUNI_INDEX, flat)
    return fallback, fallback


def parse_listings(page_html):
    soup = BeautifulSoup(page_html, "html.parser")
    blocks, out = {}, []

    for anchor in soup.find_all("a", href=True):
        match = DETAIL_RE.search(anchor["href"])
        if match and match.group(1) not in blocks:
            blocks[match.group(1)] = enclosing_block(anchor, match.group(1))

    for listing_id, block in blocks.items():
        text = re.sub(r"\s+", " ", block.get_text(" ")).strip()
        flat = plain(text)

        prices = [int(p.replace(",", "")) for p in PRICE_RE.findall(text)]
        price = max(prices) if prices else None

        beds = baths = None
        bed_match, bath_match = BEDS_RE.search(text), BATHS_RE.search(text)
        if bed_match:
            beds = bed_match.group(1).replace(" ", "")
        if bath_match:
            baths = re.sub(r"\s+", " ", bath_match.group(1)).strip()
        if beds is None or baths is None:
            fallback = FALLBACK_ROOMS_RE.search(text)
            if fallback:
                beds = beds or fallback.group(1).replace(" ", "")
                baths = baths or fallback.group(2)

        # Title: longest anchor label pointing at this listing.
        titles = []
        for anchor in block.find_all("a", href=True):
            hit = DETAIL_RE.search(anchor["href"])
            if hit and hit.group(1) == listing_id:
                label = re.sub(r"\s+", " ", anchor.get_text(" ")).strip()
                if label and plain(label) not in {t for t, _ in TYPE_INDEX}:
                    titles.append(label)
        title = max(titles, key=len) if titles else None

        broker = None
        for anchor in block.find_all("a", href=True):
            if PARTNER_RE.search(anchor["href"]):
                label = re.sub(r"\s+", " ", anchor.get_text(" ")).strip()
                if label and not label.startswith("http"):
                    broker = label
                break

        municipio, zona = find_location(flat)
        out.append({
            "id": listing_id,
            "url": DETAIL_URL.format(id=listing_id),
            "title": title or "(sin título)",
            "price": price,
            "beds": beds,
            "baths": baths,
            "municipio": municipio,
            "zona": zona,
            "type": find_first(TYPE_INDEX, flat),
            "broker": broker,
            "source": "ClasificadosOnline",
        })

    return out


def numeric_beds(value):
    if not value:
        return None
    digits = re.search(r"\d+", value)
    return int(digits.group()) if digits else None


def newest_first(page_html):
    """True if listing IDs get smaller as offset grows (newest at offset 0)."""
    listings = parse_listings(page_html)
    ids = [int(x["id"]) for x in listings if x["id"].isdigit()]
    return max(ids) if ids else 0


# ---------------------------------------------------------------- filtering

def matches(listing, config):
    """Returns 'match', 'revisar', or None.

    'revisar' is for zones ClasificadosOnline doesn't split finely enough —
    Santurce and Guaynabo cover both the blocks you want and the ones you don't,
    so those listings surface separately instead of being guessed at.
    """
    price = listing.get("price")
    if config.get("min_price") and (price is None or price < config["min_price"]):
        return None
    if config.get("max_price") and (price is None or price > config["max_price"]):
        return None

    beds = numeric_beds(listing.get("beds"))
    if config.get("min_beds") and (beds is None or beds < config["min_beds"]):
        return None

    wanted_types = [plain(t) for t in config.get("property_types") or []]
    found_type = plain(listing.get("type") or "")
    # Prefix match so "Apartamento" also catches "Apartamento/WalkUp".
    if wanted_types and not any(found_type.startswith(t) for t in wanted_types if t):
        return None

    haystack = plain(f"{listing.get('title')} {listing.get('zona')} {listing.get('broker')}")
    include = [plain(k) for k in config.get("keywords_any") or []]
    if include and not any(k in haystack for k in include):
        return None
    for word in config.get("keywords_exclude") or []:
        if plain(word) in haystack:
            return None

    zones_ok = {zone_key(z) for z in config.get("zones_include") or []}
    zones_review = {zone_key(z) for z in config.get("zones_review") or []}
    if not zones_ok and not zones_review:
        return "match"

    here = zone_key(listing.get("zona") or "")
    if here in zones_ok:
        return "match"
    if here in zones_review:
        for word in config.get("zone_reject_keywords") or []:
            if plain(word) in haystack:
                return None
        for word in config.get("zone_promote_keywords") or []:
            if plain(word) in haystack:
                return "match"
        return "revisar"
    return None


# ---------------------------------------------------------------- scraping

def scrape(config, debug=False):
    session = requests.Session()
    pages = max(1, int(config.get("pages_to_scan", 8)))
    delay = float(config.get("seconds_between_requests", 3))

    first_page = fetch_page(session, 0, debug=debug)
    total = total_count(first_page)
    print(f"  site reports {total if total else 'unknown'} listings")

    top_id = newest_first(first_page)
    offsets = [i * PER_PAGE for i in range(pages)]

    # If the listing order turns out to be oldest-first, the new arrivals sit at
    # the far end instead. One probe request settles it.
    if total and total > PER_PAGE * 2:
        time.sleep(delay)
        tail_offset = ((total - 1) // PER_PAGE) * PER_PAGE
        tail_html = fetch_page(session, tail_offset)
        tail_id = newest_first(tail_html)
        if tail_id > top_id:
            print("  detected oldest-first order; scanning from the end")
            offsets = [max(0, tail_offset - i * PER_PAGE) for i in range(pages)]

    seen, found = set(), []
    for index, offset in enumerate(offsets):
        page_html = first_page if offset == 0 and index == 0 else None
        if page_html is None:
            time.sleep(delay)
            page_html = fetch_page(session, offset)
        rows = parse_listings(page_html)
        print(f"  offset {offset}: {len(rows)} listings")
        if not rows:
            break
        for row in rows:
            if row["id"] not in seen:
                seen.add(row["id"])
                found.append(row)

    return found


# ---------------------------------------------------------------- rendering

def money(value):
    return f"${value:,}" if value else "Precio no indicado"


def rooms_label(listing):
    beds, baths = listing.get("beds"), listing.get("baths")
    if not beds and not baths:
        return None
    parts = []
    if beds and beds != "0":
        parts.append(f"{beds} hab")
    if baths and baths != "0":
        parts.append(f"{baths} baño" + ("" if baths == "1" else "s"))
    return " · ".join(parts) or None


def day_label(iso_date, today):
    date = datetime.fromisoformat(iso_date).date()
    if date == today:
        return "Hoy"
    if date == today - timedelta(days=1):
        return "Ayer"
    months = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
              "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
    return f"{date.day} de {months[date.month - 1]}"


def listing_row(listing, new_ids, first_run):
    is_new = listing["id"] in new_ids and not first_run
    meta = " · ".join(b for b in [listing.get("type"), listing.get("zona")] if b)
    detalle = " · ".join(b for b in [rooms_label(listing), listing.get("broker")] if b)
    return f"""
        <li class="fila{' fila--nueva' if is_new else ''}">
          <a class="fila__enlace" href="{html.escape(listing['url'])}" target="_blank" rel="noopener">
            <div class="fila__texto">
              <p class="fila__meta">{html.escape(meta) or '&nbsp;'}</p>
              <h3 class="fila__titulo">{html.escape(listing['title'])}</h3>
              {f'<p class="fila__detalle">{html.escape(detalle)}</p>' if detalle else ''}
            </div>
            <p class="fila__precio">{money(listing.get('price'))}</p>
          </a>
        </li>"""


def filter_summary(config):
    bits = []
    lo, hi = config.get("min_price"), config.get("max_price")
    if lo and hi:
        bits.append(f"{money(lo)}–{money(hi)}")
    elif hi:
        bits.append(f"hasta {money(hi)}")
    elif lo:
        bits.append(f"desde {money(lo)}")
    if config.get("min_beds"):
        bits.append(f"{config['min_beds']}+ hab")
    if config.get("property_types"):
        bits.append(", ".join(config["property_types"]))
    zones = (config.get("zones_include") or []) + (config.get("zones_review") or [])
    if zones:
        short = [z.replace("San Juan - ", "") for z in zones]
        bits.append(", ".join(short))
    return " · ".join(bits) if bits else "Sin filtros — mostrando todo"


def render(listings, config, run_time, new_ids, first_run):
    today = run_time.date()
    confirmed = [x for x in listings if x.get("_status") != "revisar"]
    review = [x for x in listings if x.get("_status") == "revisar"]

    groups = {}
    for listing in confirmed:
        groups.setdefault(listing["first_seen"][:10], []).append(listing)

    sections = []
    for date_key in sorted(groups, reverse=True):
        rows = sorted(groups[date_key], key=lambda x: -int(x["id"]))
        cards = "".join(listing_row(x, new_ids, first_run) for x in rows)
        sections.append(f"""
    <section class="grupo">
      <h2 class="grupo__dia">{html.escape(day_label(date_key, today))}<span class="grupo__cuenta">{len(rows)}</span></h2>
      <ul class="grupo__lista">{cards}
      </ul>
    </section>""")

    if not sections:
        sections.append("""
    <section class="vacio">
      <p>Todavía no hay propiedades que coincidan con tus filtros.</p>
      <p class="vacio__ayuda">Con filtros estrechos esto es normal al principio — se van acumulando corrida tras corrida. Para ver más, sube <code>max_price</code> o añade zonas en <code>config.json</code>.</p>
    </section>""")

    if review:
        rows = sorted(review, key=lambda x: (x.get("first_seen", ""), int(x["id"])), reverse=True)
        cards = "".join(listing_row(x, new_ids, first_run) for x in rows)
        sections.append(f"""
    <section class="grupo grupo--revisar">
      <h2 class="grupo__dia">Por confirmar<span class="grupo__cuenta">{len(rows)}</span></h2>
      <p class="grupo__nota">ClasificadosOnline no divide Santurce ni Guaynabo por urbanización, así que estas caen en la zona correcta pero no necesariamente en el bloque que buscas. Verifica la dirección antes de ir.</p>
      <ul class="grupo__lista">{cards}
      </ul>
    </section>""")

    nuevas = len(new_ids) if not first_run else 0
    titular = "Primera carga" if first_run else (f"{nuevas} nueva{'s' if nuevas != 1 else ''}" if nuevas else "Nada nuevo")

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Propiedades · Puerto Rico</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,600&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --tinta: #0f2e2b;
    --losa: #163f3a;
    --linea: #2a5d55;
    --ocre: #e3a63c;
    --cal: #eaf0e9;
    --tenue: #9db4ad;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--tinta);
    color: var(--cal);
    font-family: "IBM Plex Sans", system-ui, sans-serif;
    -webkit-font-smoothing: antialiased;
  }}
  .envoltura {{ max-width: 46rem; margin: 0 auto; padding: 2rem 1.15rem 4rem; }}

  .cabecera {{ border-bottom: 1px solid var(--linea); padding-bottom: 1.4rem; margin-bottom: 2rem; }}
  .cabecera__marca {{
    font-family: Fraunces, Georgia, serif;
    font-weight: 600;
    font-size: clamp(2rem, 8vw, 2.9rem);
    line-height: 1.02;
    letter-spacing: -0.02em;
    margin: 0 0 0.65rem;
  }}
  .cabecera__marca em {{ font-style: italic; color: var(--ocre); }}
  .cabecera__estado {{
    font-family: "IBM Plex Mono", monospace;
    font-size: 0.76rem;
    text-transform: uppercase;
    letter-spacing: 0.09em;
    color: var(--tenue);
    margin: 0;
  }}
  .cabecera__estado b {{ color: var(--ocre); font-weight: 500; }}
  .cabecera__filtros {{
    font-size: 0.85rem; color: var(--tenue); margin: 0.75rem 0 0; line-height: 1.5;
  }}

  .grupo {{ margin-bottom: 2.4rem; }}
  .grupo__dia {{
    font-family: "IBM Plex Mono", monospace;
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--tenue);
    display: flex; align-items: center; gap: 0.7rem;
    margin: 0 0 0.6rem;
  }}
  .grupo__dia::after {{ content: ""; flex: 1; height: 1px; background: var(--linea); }}
  .grupo__cuenta {{ order: 3; color: var(--linea); }}
  .grupo__lista {{ list-style: none; margin: 0; padding: 0; }}

  .fila {{ border-bottom: 1px solid rgba(42, 93, 85, 0.5); }}
  .fila__enlace {{
    display: flex; align-items: baseline; gap: 1rem;
    padding: 0.95rem 0.6rem 0.95rem 0.85rem;
    color: inherit; text-decoration: none;
    border-left: 2px solid transparent;
    transition: background 140ms ease, border-color 140ms ease;
  }}
  .fila__enlace:hover, .fila__enlace:focus-visible {{
    background: var(--losa); border-left-color: var(--linea); outline: none;
  }}
  .fila--nueva .fila__enlace {{ border-left-color: var(--ocre); }}
  .fila__texto {{ flex: 1; min-width: 0; }}
  .fila__meta {{
    font-family: "IBM Plex Mono", monospace;
    font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.08em;
    color: var(--tenue); margin: 0 0 0.28rem;
  }}
  .fila--nueva .fila__meta::before {{ content: "nuevo · "; color: var(--ocre); }}
  .fila__titulo {{ font-size: 1rem; font-weight: 500; line-height: 1.3; margin: 0; }}
  .fila__detalle {{ font-size: 0.78rem; color: var(--tenue); margin: 0.3rem 0 0; }}
  .fila__precio {{
    font-family: "IBM Plex Mono", monospace;
    font-variant-numeric: tabular-nums;
    font-size: 0.95rem; white-space: nowrap; margin: 0; text-align: right;
  }}
  .fila--nueva .fila__precio {{ color: var(--ocre); }}

  .grupo--revisar {{ opacity: 0.92; }}
  .grupo--revisar .fila__precio, .grupo--revisar .grupo__dia {{ color: var(--tenue); }}
  .grupo__nota {{
    font-size: 0.82rem; color: var(--tenue); line-height: 1.55;
    margin: 0 0 0.9rem; padding-left: 0.85rem;
    border-left: 2px solid var(--linea);
  }}
  .vacio {{ border: 1px solid var(--linea); padding: 1.6rem; }}
  .vacio p {{ margin: 0 0 0.5rem; }}
  .vacio__ayuda {{ color: var(--tenue); font-size: 0.9rem; }}
  code {{ font-family: "IBM Plex Mono", monospace; color: var(--ocre); }}

  .pie {{
    border-top: 1px solid var(--linea); margin-top: 2.5rem; padding-top: 1.1rem;
    font-size: 0.78rem; color: var(--tenue); line-height: 1.6;
  }}
  .pie a {{ color: var(--tenue); }}
  @media (prefers-reduced-motion: reduce) {{ * {{ transition: none !important; }} }}
</style>
</head>
<body>
  <div class="envoltura">
    <header class="cabecera">
      <h1 class="cabecera__marca">Propiedades<br><em>Puerto Rico</em></h1>
      <p class="cabecera__estado"><b>{html.escape(titular)}</b> · {len(listings)} guardadas · {run_time.strftime('%I:%M %p').lstrip('0').lower()}</p>
      <p class="cabecera__filtros">{html.escape(filter_summary(config))}</p>
    </header>
    {''.join(sections)}
    <footer class="pie">
      Datos de <a href="https://www.clasificadosonline.com/" target="_blank" rel="noopener">ClasificadosOnline</a>.
      Los precios y detalles se muestran tal como los publicó el vendedor — confirma todo directamente con el corredor.
    </footer>
  </div>
</body>
</html>
"""


# ---------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true", help="save raw HTML of the first page")
    args = parser.parse_args()

    config = load_json(CONFIG_PATH, {})
    state = load_json(STATE_PATH, {})
    first_run = not state
    run_time = datetime.now(AST)
    stamp = run_time.isoformat(timespec="seconds")

    print(f"Corrida {stamp}")
    try:
        scraped = scrape(config, debug=args.debug)
    except requests.RequestException as err:
        print(f"! network error: {err}", file=sys.stderr)
        return 1

    if not scraped:
        print("! parsed 0 listings — the page layout may have changed.", file=sys.stderr)
        print("  run `python scraper.py --debug` and inspect data/debug_page.html", file=sys.stderr)
        return 1

    new_ids = set()
    for listing in scraped:
        record = state.get(listing["id"])
        if record is None:
            listing["first_seen"] = stamp
            new_ids.add(listing["id"])
        else:
            listing["first_seen"] = record.get("first_seen", stamp)
        listing["last_seen"] = stamp
        state[listing["id"]] = listing

    # Keep the archive from growing forever.
    keep = int(config.get("max_stored", 1500))
    if len(state) > keep:
        ordered = sorted(state.values(), key=lambda x: x.get("first_seen", ""), reverse=True)
        state = {x["id"]: x for x in ordered[:keep]}

    matched = []
    for listing in state.values():
        status = matches(listing, config)
        if status:
            matched.append({**listing, "_status": status})
    matched.sort(key=lambda x: (x.get("first_seen", ""), int(x["id"])), reverse=True)
    new_matched = {x["id"] for x in matched if x["id"] in new_ids}

    save_json(STATE_PATH, state)
    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(os.path.join(DOCS_DIR, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(render(matched, config, run_time, new_matched, first_run))
    save_json(os.path.join(DOCS_DIR, "listings.json"), matched)

    revisar = sum(1 for x in matched if x["_status"] == "revisar")
    print(f"  {len(scraped)} escaneadas · {len(new_ids)} nuevas · "
          f"{len(matched) - revisar} coinciden · {revisar} por confirmar")
    return 0


if __name__ == "__main__":
    sys.exit(main())
