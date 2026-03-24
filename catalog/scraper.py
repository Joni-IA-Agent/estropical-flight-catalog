"""
estropical.com flight catalog scraper.

Phase A: Scrapes featured routes from the homepage Swiper carousel via HTTP.
Phase B: Uses Playwright to fill the flight search form for each origin×destination
         pair and captures prices from the results.

Output: catalog/routes_data.json
"""

import json
import re
import asyncio
import unicodedata
from datetime import date, timedelta
from pathlib import Path
import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "routes_config.json"
OUTPUT_PATH = BASE_DIR / "routes_data.json"

SITE_URL = "https://estropical.com"
# Polite delay between searches
SEARCH_DELAY_SECONDS = 5

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def build_route_key(origin_iata, destination_iata):
    return f"{origin_iata}-{destination_iata}"


def normalize_city(name):
    """Lowercase + strip accents for fuzzy city matching."""
    return unicodedata.normalize("NFKD", name.lower()).encode("ascii", "ignore").decode()


def build_city_iata_map(config):
    """Return {normalized_city_name: (iata, display_name)} for all known airports."""
    mapping = {}
    for dest in config["known_destinations"]:
        mapping[normalize_city(dest["name"])] = (dest["iata"], dest["name"])
    for orig in config["origins"]:
        mapping[normalize_city(orig["name"])] = (orig["iata"], orig["name"])
    return mapping


def extract_lowest_us_price(text, min_price=50):
    """Find the lowest US$ price in a block of text."""
    lowest = None
    for m in re.finditer(r"US\$\s*([\d,]+)|\$\s*([\d,]+)", text):
        raw = (m.group(1) or m.group(2)).replace(",", "")
        try:
            p = float(raw)
            if p >= min_price:
                if lowest is None or p < lowest:
                    lowest = p
        except ValueError:
            pass
    return lowest


# ---------------------------------------------------------------------------
# Phase A — plain HTTP scrape of the homepage carousel
# ---------------------------------------------------------------------------

def scrape_homepage_carousel(config):
    """
    Extract featured routes from the Swiper carousel via a plain HTTP GET.

    estropical.com uses JSF (server-side rendering), so the carousel HTML is
    present in the raw page source — no JavaScript execution needed.

    Card structure:
      <div class="swiper-slide">
        <a href="/es/idea/ID/slug">
          <img src="...">
          <h3>Miami</h3>
          <p>N Destinos  N Transportes</p>
          <p>... US$737 Por persona</p>
        </a>
      </div>
    """
    print("[Phase A] Scraping homepage carousel via HTTP...")
    routes = {}

    city_iata = build_city_iata_map(config)
    origin_iata = config["origins"][0]["iata"]
    origin_city = config["origins"][0]["name"]
    origin_set = {o["iata"] for o in config["origins"]}

    headers = {
        "User-Agent": BROWSER_UA,
        "Accept-Language": "es-BO,es;q=0.9,en;q=0.8",
    }

    try:
        resp = requests.get(SITE_URL, headers=headers, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"  HTTP fetch failed: {e}")
        return routes

    soup = BeautifulSoup(resp.text, "html.parser")
    slides = soup.select(".swiper-slide")
    print(f"  Found {len(slides)} carousel slides in page source.")

    for slide in slides:
        try:
            h3 = slide.find("h3")
            if not h3:
                continue
            dest_city_raw = h3.get_text(strip=True)
            if not dest_city_raw:
                continue

            # Look up IATA by fuzzy city name match
            dest_key = normalize_city(dest_city_raw)
            destination_iata = None
            for city_key, (iata, _) in city_iata.items():
                if city_key in dest_key or dest_key in city_key:
                    destination_iata = iata
                    break

            if not destination_iata:
                print(f"  No IATA mapping for: {dest_city_raw!r}")
                continue
            if destination_iata in origin_set:
                continue

            text = slide.get_text()
            price = extract_lowest_us_price(text)
            if not price:
                continue

            img_tag = slide.find("img")
            image_url = img_tag.get("src", "") if img_tag else ""
            if image_url and image_url.startswith("/"):
                image_url = SITE_URL + image_url

            a_tag = slide.find("a")
            search_url = a_tag.get("href", SITE_URL) if a_tag else SITE_URL
            if search_url and search_url.startswith("/"):
                search_url = SITE_URL + search_url

            key = build_route_key(origin_iata, destination_iata)
            if key not in routes or routes[key]["price"] > price:
                routes[key] = {
                    "origin_iata": origin_iata,
                    "destination_iata": destination_iata,
                    "origin_city": origin_city,
                    "destination_city": dest_city_raw,
                    "price": price,
                    "currency": "USD",
                    "image_url": image_url,
                    "search_url": search_url,
                }
                print(f"  Route found: {key} @ ${price}")
        except Exception as e:
            print(f"  Slide parse error: {e}")

    return routes


# ---------------------------------------------------------------------------
# Phase B — Playwright search form automation
# ---------------------------------------------------------------------------

async def search_one_route(page, origin, dest, existing_routes):
    """
    Fill the flight search form for a single origin→destination pair and
    return the lowest price found, or None if unavailable.
    """
    origin_iata = origin["iata"]
    origin_name = origin["name"]
    dest_iata = dest["iata"]
    dest_name = dest["name"]
    key = build_route_key(origin_iata, dest_iata)

    # Fresh page load for every search — PrimeFaces state gets stale
    try:
        await page.goto(SITE_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)  # Let JSF/PrimeFaces JS initialize
    except Exception as e:
        print(f"  Page load failed for {key}: {e}")
        return

    # Locate fields by the stable part of their JSF-generated IDs
    origin_loc = page.locator('[id*="startlocationOnlyFlight"]').first
    dest_loc   = page.locator('[id*="endlocationOnlyFlight"]').first
    date_loc   = page.locator('[id*="onlyFlightDeparture"]').first
    submit_loc = page.locator('[id*="startTrip"]').first

    # First suggestion item in the PrimeFaces autocomplete panel
    suggestion = page.locator('.ui-autocomplete-panel li').first

    try:
        # --- Origin ---
        await origin_loc.wait_for(state="visible", timeout=10000)
        await origin_loc.click()
        await origin_loc.fill("")
        # press_sequentially fires keydown/keyup events, triggering PrimeFaces AJAX
        await origin_loc.press_sequentially(origin_name[:8], delay=80)
        await page.wait_for_timeout(2000)

        try:
            await suggestion.wait_for(state="visible", timeout=6000)
            await suggestion.click()
        except PlaywrightTimeout:
            print(f"  No origin autocomplete for {origin_name!r} — aborting all searches from {origin_iata}")
            return "abort_origin"
        await page.wait_for_timeout(600)

        # --- Destination ---
        await dest_loc.click()
        await dest_loc.fill("")
        await dest_loc.press_sequentially(dest_name[:8], delay=80)
        await page.wait_for_timeout(2000)

        try:
            await suggestion.wait_for(state="visible", timeout=6000)
            await suggestion.click()
        except PlaywrightTimeout:
            print(f"  No destination autocomplete for {dest_name!r} — skipping {key}")
            return None
        await page.wait_for_timeout(600)

        # --- Departure date (45 days out, dd/mm/yyyy) ---
        future_date = (date.today() + timedelta(days=45)).strftime("%d/%m/%Y")
        if await date_loc.count():
            await date_loc.click()
            await date_loc.fill(future_date)
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(500)

        # --- Submit ---
        if not await submit_loc.count():
            print(f"  Submit button not found — skipping {key}")
            return None
        await submit_loc.click()

        # Wait for JSF AJAX to load results
        await page.wait_for_timeout(10000)

        # --- Parse prices ---
        body_text = await page.locator("body").inner_text()
        lowest = extract_lowest_us_price(body_text)

        if lowest:
            existing_routes[key] = {
                "origin_iata": origin_iata,
                "destination_iata": dest_iata,
                "origin_city": origin_name,
                "destination_city": dest_name,
                "price": lowest,
                "currency": "USD",
                "image_url": "",
                "search_url": page.url,
            }
            print(f"  Route found: {key} @ ${lowest}")
        else:
            print(f"  No price found for {key}")

    except Exception as e:
        print(f"  Error searching {key}: {e}")


async def search_all_routes(page, config, existing_routes):
    """Phase B: iterate every origin × destination pair not already in existing_routes."""
    for origin in config["origins"]:
        origin_iata = origin["iata"]
        print(f"\n[Phase B] Searching routes from {origin_iata} ({origin['name']})...")

        for dest in config["known_destinations"]:
            dest_iata = dest["iata"]
            key = build_route_key(origin_iata, dest_iata)

            if key in existing_routes:
                print(f"  Skipping {key} (already in catalog)")
                continue

            result = await search_one_route(page, origin, dest, existing_routes)
            if result == "abort_origin":
                break  # Origin autocomplete failed — skip remaining dests for this origin

            await asyncio.sleep(SEARCH_DELAY_SECONDS)


# ---------------------------------------------------------------------------
# Image fallback — HTTP-based (no Playwright needed)
# ---------------------------------------------------------------------------

def fill_missing_images(routes, config):
    """
    For routes missing an image_url, try to find one by fetching the homepage
    and matching images to destination names/IATAs.
    """
    missing = [k for k, v in routes.items() if not v["image_url"]]
    if not missing:
        return

    print(f"\nFilling images for {len(missing)} routes...")
    headers = {"User-Agent": BROWSER_UA, "Accept-Language": "es-BO,es;q=0.9"}
    iata_images = {}

    try:
        resp = requests.get(SITE_URL, headers=headers, timeout=30)
        soup = BeautifulSoup(resp.text, "html.parser")
        for img in soup.find_all("img", src=True):
            src = img["src"]
            alt = (img.get("alt") or "").upper()
            for dest in config["known_destinations"]:
                iata = dest["iata"]
                if iata in alt or dest["name"].upper() in alt or iata.lower() in src.lower():
                    iata_images[iata] = SITE_URL + src if src.startswith("/") else src
    except Exception as e:
        print(f"  Image fetch error: {e}")

    for key in missing:
        dest_iata = routes[key]["destination_iata"]
        routes[key]["image_url"] = iata_images.get(
            dest_iata,
            f"{SITE_URL}/javax.faces.resource/images/no-photo-XS.jpg",
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    config = load_config()
    all_routes = {}

    # Phase A: plain HTTP carousel scrape
    carousel_routes = scrape_homepage_carousel(config)
    all_routes.update(carousel_routes)
    print(f"\nPhase A complete: {len(all_routes)} routes found.\n")

    # Phase B: Playwright search form automation
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=BROWSER_UA,
            viewport={"width": 1280, "height": 800},
            locale="es-BO",
        )
        page = await context.new_page()

        await search_all_routes(page, config, all_routes)
        await browser.close()

    # Fill missing images without Playwright
    fill_missing_images(all_routes, config)

    routes_list = list(all_routes.values())
    print(f"\nTotal routes collected: {len(routes_list)}")

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(routes_list, f, ensure_ascii=False, indent=2)

    print(f"Saved to {OUTPUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
