"""
Generates a Meta Ads Travel (flights) catalog XML feed from routes_data.json.

Meta required fields for the flights vertical:
  flight_id, description, name, price, url, image_url,
  origin_airport, destination_airport, currency

Output: output/flights-catalog.xml
"""

import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent
ROUTES_PATH = BASE_DIR / "routes_data.json"
OUTPUT_DIR = BASE_DIR.parent / "output"
OUTPUT_PATH = OUTPUT_DIR / "flights-catalog.xml"

FEED_TITLE = "estropical.com - Vuelos"
FEED_LINK = "https://estropical.com"


def load_routes():
    if not ROUTES_PATH.exists():
        raise FileNotFoundError(
            f"routes_data.json not found at {ROUTES_PATH}. Run scraper.py first."
        )
    with open(ROUTES_PATH, encoding="utf-8") as f:
        return json.load(f)


def build_search_url(origin_iata, destination_iata, base_search_url):
    """
    Return the best URL for the route. Use the scraped URL if available,
    otherwise build a fallback pointing to the estropical.com homepage
    (since the site doesn't expose clean search URLs).
    """
    if base_search_url and base_search_url != FEED_LINK:
        return base_search_url
    return FEED_LINK


def format_price(price, currency):
    """Format price as '737.00 USD' as required by Meta."""
    return f"{price:.2f} {currency}"


def generate_xml(routes):
    # Atom feed namespace
    ET.register_namespace("", "http://www.w3.org/2005/Atom")
    ET.register_namespace("g", "http://base.google.com/ns/1.0")

    feed = ET.Element(
        "feed",
        attrib={
            "xmlns": "http://www.w3.org/2005/Atom",
            "xmlns:g": "http://base.google.com/ns/1.0",
        },
    )

    ET.SubElement(feed, "title").text = FEED_TITLE
    ET.SubElement(feed, "link", attrib={"href": FEED_LINK})
    ET.SubElement(feed, "updated").text = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    for route in routes:
        origin_iata = route.get("origin_iata", "")
        destination_iata = route.get("destination_iata", "")
        origin_city = route.get("origin_city", origin_iata)
        destination_city = route.get("destination_city", destination_iata)
        price = route.get("price", 0)
        currency = route.get("currency", "USD")
        image_url = route.get("image_url", "")
        search_url = build_search_url(
            origin_iata, destination_iata, route.get("search_url", "")
        )

        flight_id = f"{origin_iata}-{destination_iata}"
        name = f"{origin_city} \u2192 {destination_city}"
        description = f"Vuelos desde {origin_city} ({origin_iata}) a {destination_city} ({destination_iata}). Mejor precio desde {format_price(price, currency)}."

        if not origin_iata or not destination_iata or price <= 0:
            print(f"  Skipping incomplete route: {flight_id}")
            continue

        entry = ET.SubElement(feed, "entry")

        # Meta flight catalog required fields
        ET.SubElement(entry, "g:id").text = flight_id
        ET.SubElement(entry, "title").text = name
        ET.SubElement(entry, "g:description").text = description
        ET.SubElement(entry, "g:link").text = search_url
        ET.SubElement(entry, "g:image_link").text = image_url
        ET.SubElement(entry, "g:price").text = format_price(price, currency)
        ET.SubElement(entry, "g:currency").text = currency
        ET.SubElement(entry, "g:origin_airport").text = origin_iata
        ET.SubElement(entry, "g:destination_airport").text = destination_iata

        # Optional enrichment fields
        ET.SubElement(entry, "g:origin_city").text = origin_city
        ET.SubElement(entry, "g:destination_city").text = destination_city
        ET.SubElement(entry, "g:availability").text = "in stock"

    return feed


def indent_xml(elem, level=0):
    """Add pretty-print indentation to the XML tree."""
    indent = "\n" + "  " * level
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = indent + "  "
        if not elem.tail or not elem.tail.strip():
            elem.tail = indent
        for child in elem:
            indent_xml(child, level + 1)
        if not child.tail or not child.tail.strip():
            child.tail = indent
    else:
        if level and (not elem.tail or not elem.tail.strip()):
            elem.tail = indent


def main():
    routes = load_routes()
    print(f"Loaded {len(routes)} routes from {ROUTES_PATH}")

    feed = generate_xml(routes)
    indent_xml(feed)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    tree = ET.ElementTree(feed)
    ET.indent(tree, space="  ")  # Python 3.9+

    with open(OUTPUT_PATH, "wb") as f:
        f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        tree.write(f, encoding="utf-8", xml_declaration=False)

    entry_count = len(feed.findall("{http://www.w3.org/2005/Atom}entry"))
    print(f"Generated {entry_count} catalog entries.")
    print(f"Saved to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
