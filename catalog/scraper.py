"""
estropical.com flight catalog scraper.

Phase A: Scrapes featured routes from the homepage Swiper carousel.
Phase B: Simulates flight searches for each origin in routes_config.json
         to discover additional routes and prices.

Output: catalog/routes_data.json
"""

import json
import re
import time
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "routes_config.json"
OUTPUT_PATH = BASE_DIR / "routes_data.json"

SITE_URL = "https://estropical.com"
# Delay between searches to be polite to the server
SEARCH_DELAY_SECONDS = 4


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def build_route_key(origin_iata, destination_iata):
    return f"{origin_iata}-{destination_iata}"


def parse_price(price_text):
    """Extract numeric price from strings like '$737', 'desde $737', '737 USD'."""
    match = re.search(r"[\d,]+(?:\.\d+)?", price_text.replace(",", ""))
    if match:
        return float(match.group().replace(",", ""))
    return None


async def scrape_homepage_carousel(page, config):
    """Phase A: extract featured routes from the homepage carousel."""
    print("[Phase A] Scraping homepage carousel...")
    routes = {}

    await page.goto(SITE_URL, wait_until="networkidle", timeout=60000)

    # Wait for Swiper carousel items to appear
    try:
        await page.wait_for_selector(".swiper-slide", timeout=15000)
    except PlaywrightTimeout:
        print("  Warning: Swiper slides not found on homepage.")
        return routes

    slides = await page.query_selector_all(".swiper-slide")
    print(f"  Found {len(slides)} carousel slides.")

    for slide in slides:
        try:
            # Try to extract IATA codes and price from each slide
            text = await slide.inner_text()

            # Look for IATA codes in parentheses, e.g. "(VVI)" or "(MIA)"
            iata_matches = re.findall(r"\(([A-Z]{3})\)", text)
            price_match = re.search(r"\$\s*([\d,]+)", text)

            if len(iata_matches) >= 2 and price_match:
                origin_iata = iata_matches[0]
                destination_iata = iata_matches[1]
                price = float(price_match.group(1).replace(",", ""))

                # Try to get destination image
                img = await slide.query_selector("img")
                image_url = await img.get_attribute("src") if img else ""
                if image_url and image_url.startswith("/"):
                    image_url = SITE_URL + image_url

                # Try to get a link
                link = await slide.query_selector("a")
                search_url = await link.get_attribute("href") if link else SITE_URL
                if search_url and search_url.startswith("/"):
                    search_url = SITE_URL + search_url

                # Extract city names from text (lines before/after IATA)
                lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                origin_city = lines[0] if lines else origin_iata
                destination_city = lines[1] if len(lines) > 1 else destination_iata

                key = build_route_key(origin_iata, destination_iata)
                if key not in routes or routes[key]["price"] > price:
                    routes[key] = {
                        "origin_iata": origin_iata,
                        "destination_iata": destination_iata,
                        "origin_city": origin_city,
                        "destination_city": destination_city,
                        "price": price,
                        "currency": "USD",
                        "image_url": image_url,
                        "search_url": search_url,
                    }
                    print(f"  Route found: {key} @ ${price}")
        except Exception as e:
            print(f"  Slide parse error: {e}")
            continue

    return routes


async def search_routes_for_origin(page, origin, destinations, existing_routes):
    """Phase B: simulate a search for each origin and scrape results."""
    origin_iata = origin["iata"]
    origin_name = origin["name"]
    print(f"\n[Phase B] Searching routes from {origin_iata} ({origin_name})...")

    try:
        await page.goto(SITE_URL, wait_until="networkidle", timeout=60000)
    except Exception as e:
        print(f"  Failed to load homepage for {origin_iata}: {e}")
        return

    # Try to find the origin input field (flight search form)
    origin_selectors = [
        "input[id*='departure']",
        "input[id*='origin']",
        "input[id*='startlocation']",
        "input[placeholder*='rigen']",
        "input[placeholder*='alida']",
        "input[name*='departure']",
    ]

    origin_input = None
    for sel in origin_selectors:
        try:
            origin_input = await page.wait_for_selector(sel, timeout=3000)
            if origin_input:
                break
        except PlaywrightTimeout:
            continue

    if not origin_input:
        print(f"  Could not find origin input for {origin_iata}. Skipping search phase.")
        return

    # Type origin city name and wait for autocomplete
    try:
        await origin_input.triple_click()
        await origin_input.type(origin_name, delay=80)
        await page.wait_for_timeout(2000)

        # Select first autocomplete suggestion
        suggestion_selectors = [
            "li.ui-autocomplete-item",
            ".ui-autocomplete-item",
            "[id*='autocomplete'] li",
            ".suggestions li",
        ]
        for sel in suggestion_selectors:
            suggestions = await page.query_selector_all(sel)
            if suggestions:
                await suggestions[0].click()
                await page.wait_for_timeout(1000)
                break
    except Exception as e:
        print(f"  Autocomplete error for {origin_iata}: {e}")
        return

    # For each destination, search and scrape the result
    for dest in destinations:
        dest_iata = dest["iata"]
        dest_name = dest["name"]
        key = build_route_key(origin_iata, dest_iata)

        # Skip if we already have this route from the homepage
        if key in existing_routes:
            print(f"  Skipping {key} (already found on homepage)")
            continue

        try:
            dest_selectors = [
                "input[id*='arrival']",
                "input[id*='destination']",
                "input[placeholder*='estino']",
                "input[placeholder*='llegada']",
                "input[name*='arrival']",
            ]

            dest_input = None
            for sel in dest_selectors:
                try:
                    dest_input = await page.query_selector(sel)
                    if dest_input:
                        break
                except Exception:
                    continue

            if not dest_input:
                continue

            await dest_input.triple_click()
            await dest_input.type(dest_name, delay=80)
            await page.wait_for_timeout(2000)

            # Click first suggestion
            for sel in suggestion_selectors:
                suggestions = await page.query_selector_all(sel)
                if suggestions:
                    await suggestions[0].click()
                    await page.wait_for_timeout(800)
                    break

            # Try to submit the search
            submit_selectors = [
                "button[id*='startTrip']",
                "button[type='submit']",
                "input[type='submit']",
                "button.search-btn",
                "button[class*='search']",
            ]
            submitted = False
            for sel in submit_selectors:
                btn = await page.query_selector(sel)
                if btn:
                    await btn.click()
                    submitted = True
                    break

            if not submitted:
                continue

            # Wait for results to render (JSF AJAX update)
            await page.wait_for_timeout(5000)

            # Try to extract price from results
            price_selectors = [
                "[class*='price']",
                "[class*='fare']",
                "[class*='tarifa']",
                "[class*='precio']",
            ]
            lowest_price = None
            image_url = ""

            for sel in price_selectors:
                price_elements = await page.query_selector_all(sel)
                for el in price_elements:
                    text = await el.inner_text()
                    p = parse_price(text)
                    if p and p > 50:  # sanity check: flights cost more than $50
                        if lowest_price is None or p < lowest_price:
                            lowest_price = p

            if lowest_price:
                # Try to get a destination image from results
                img_elements = await page.query_selector_all("img[src*='destination'], img[src*='destino'], .result img")
                if img_elements:
                    src = await img_elements[0].get_attribute("src")
                    if src:
                        image_url = SITE_URL + src if src.startswith("/") else src

                search_url = page.url if page.url != SITE_URL else f"{SITE_URL}/?from={origin_iata}&to={dest_iata}"

                existing_routes[key] = {
                    "origin_iata": origin_iata,
                    "destination_iata": dest_iata,
                    "origin_city": origin_name,
                    "destination_city": dest_name,
                    "price": lowest_price,
                    "currency": "USD",
                    "image_url": image_url,
                    "search_url": search_url,
                }
                print(f"  Route found: {key} @ ${lowest_price}")

        except Exception as e:
            print(f"  Error searching {key}: {e}")

        await page.wait_for_timeout(SEARCH_DELAY_SECONDS * 1000)


async def scrape_destination_images(page, routes, config):
    """
    Fallback: for any route missing an image_url, try to find a destination
    image from the homepage or assign a placeholder.
    """
    # Build a map of destination IATA -> image URL from homepage images
    iata_images = {}
    try:
        await page.goto(SITE_URL, wait_until="networkidle", timeout=60000)
        imgs = await page.query_selector_all("img[src]")
        for img in imgs:
            src = await img.get_attribute("src") or ""
            alt = (await img.get_attribute("alt") or "").upper()
            for dest in config["known_destinations"]:
                iata = dest["iata"]
                name_upper = dest["name"].upper()
                if iata in alt or name_upper in alt or iata.lower() in src.lower():
                    if src.startswith("/"):
                        src = SITE_URL + src
                    iata_images[iata] = src
    except Exception as e:
        print(f"  Image fallback error: {e}")

    for key, route in routes.items():
        if not route["image_url"]:
            dest_iata = route["destination_iata"]
            if dest_iata in iata_images:
                routes[key]["image_url"] = iata_images[dest_iata]
            else:
                # Use a generic travel image placeholder
                routes[key]["image_url"] = f"https://estropical.com/img/destinations/{dest_iata.lower()}.jpg"


async def main():
    config = load_config()
    all_routes = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (compatible; CatalogBot/1.0; +https://estropical.com)",
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()

        # Phase A: homepage carousel
        carousel_routes = await scrape_homepage_carousel(page, config)
        all_routes.update(carousel_routes)
        print(f"\nPhase A complete: {len(all_routes)} routes found.\n")

        # Phase B: search simulation
        for origin in config["origins"]:
            await search_routes_for_origin(page, origin, config["known_destinations"], all_routes)

        # Fill in missing images
        await scrape_destination_images(page, all_routes, config)

        await browser.close()

    routes_list = list(all_routes.values())
    print(f"\nTotal routes collected: {len(routes_list)}")

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(routes_list, f, ensure_ascii=False, indent=2)

    print(f"Saved to {OUTPUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
