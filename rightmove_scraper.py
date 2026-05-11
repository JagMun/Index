#!/usr/bin/env python3
"""
Rightmove development-potential scraper — mini version.

Finds up to 10 properties listed under £200,000 that show signs of
development potential, then stops.

Usage:
    python rightmove_scraper.py [locationIdentifier]

    locationIdentifier  Rightmove location token, e.g. REGION%5E61227
                        (copy from the URL after searching on rightmove.co.uk)
                        Defaults to a broad North-of-England region.

Requires:
    ANTHROPIC_API_KEY environment variable
    pip install requests beautifulsoup4 anthropic
"""

import csv
import json
import re
import sys
import time
from typing import Optional

import anthropic
import requests
from bs4 import BeautifulSoup

# ── config ────────────────────────────────────────────────────────────────────

TARGET      = 10
MAX_PRICE   = 200_000
MIN_SCORE   = 5        # Claude score (1-10) threshold for inclusion
DELAY       = 2.0      # seconds between HTTP requests
PAGE_SIZE   = 24       # Rightmove results per page

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}

# Pre-filter: if none of these appear in the description, skip Claude entirely
DEV_KEYWORDS = [
    "development potential", "planning permission", "stpp",
    "subject to planning", "building plot", "barn conversion",
    "renovation", "modernisation", "modernization", "requires updating",
    "needs updating", "needs work", "requires work", "potential to extend",
    "extension potential", "outbuilding", "plot of land", "large plot",
    "development opportunity", "habitable", "uninhabitable",
    "investment potential", "improvement potential", "scope to extend",
    "land to the", "land to rear", "detached garage", "paddock",
    "agricultural", "conversion potential",
]

# ── helpers ───────────────────────────────────────────────────────────────────

def fetch(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text


def extract_json_model(html: str) -> Optional[dict]:
    """Pull window.jsonModel embedded in Rightmove pages."""
    m = re.search(r"window\.jsonModel\s*=\s*(\{.+?\})\s*;?\s*</script>",
                  html, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    return None


def parse_search_page(html: str) -> list[dict]:
    """Return property stubs (id, url, price, address, summary) from a results page."""
    model = extract_json_model(html)
    if not model:
        return []

    results = []
    for prop in model.get("properties", []):
        price = prop.get("price", {}).get("amount")
        if not isinstance(price, (int, float)) or price > MAX_PRICE:
            continue
        pid = prop.get("id")
        results.append({
            "id":      pid,
            "url":     f"https://www.rightmove.co.uk/properties/{pid}",
            "title":   prop.get("propertyTypeFullDescription", ""),
            "price":   int(price),
            "address": prop.get("displayAddress", ""),
            "summary": prop.get("summary", ""),
        })
    return results


def fetch_description(prop_url: str) -> str:
    """Fetch the full listing description from a property detail page."""
    html = fetch(prop_url)

    # Prefer the embedded JSON model — most stable across Rightmove redesigns
    model = extract_json_model(html)
    if model:
        for path in [
            ["propertyData", "description", "text"],
            ["description", "text"],
        ]:
            node = model
            for key in path:
                node = node.get(key) if isinstance(node, dict) else None
            if isinstance(node, str) and node.strip():
                return node.strip()

    # Fallback: CSS selectors (Rightmove tweaks class names occasionally)
    soup = BeautifulSoup(html, "html.parser")
    for sel in [
        "div[data-testid='description']",
        "div.OD0O7",
        "div.STW0Z",
        "div._2nk2x",
        "div.property-description",
    ]:
        el = soup.select_one(sel)
        if el:
            return el.get_text(separator=" ", strip=True)

    return ""


def has_dev_keywords(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in DEV_KEYWORDS)


def claude_assess(client: anthropic.Anthropic, listing: dict) -> tuple[str, int]:
    """
    Ask Claude to score development potential.
    Returns (one-sentence assessment, score 1–10).
    """
    description = listing.get("description") or listing.get("summary", "")
    prompt = f"""You are a UK property development analyst.

Property:    {listing['title']}
Address:     {listing['address']}
Price:       £{listing['price']:,}

Description:
{description[:2500]}

Assess development potential — extensions, loft/garage conversions, subdivision,
barn conversions, new builds on land, planning gain, etc. Draw on your knowledge
of UK planning policy (NPPF, permitted development rights, local plan context).

Reply in exactly this format, nothing else:
ASSESSMENT: <one concise sentence>
SCORE: <integer 1-10>"""

    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip()

    assessment, score = "", 0
    for line in text.splitlines():
        if line.startswith("ASSESSMENT:"):
            assessment = line.removeprefix("ASSESSMENT:").strip()
        elif line.startswith("SCORE:"):
            m = re.search(r"\d+", line)
            if m:
                score = int(m.group())

    return assessment, score

# ── main ──────────────────────────────────────────────────────────────────────

def main(location_id: str = "REGION%5E61227") -> None:
    """
    Default location is Yorkshire & The Humber (REGION%5E61227), a region with
    plenty of sub-£200k stock.  Pass any Rightmove locationIdentifier on the
    command line to search elsewhere.
    """
    client  = anthropic.Anthropic()
    results: list[dict] = []
    seen:    set[str]   = set()
    page = 0

    print(f"Rightmove scraper  |  max price £{MAX_PRICE:,}  |  target {TARGET} properties")
    print(f"Location ID: {location_id}")
    print("─" * 64)

    while len(results) < TARGET:
        search_url = (
            f"https://www.rightmove.co.uk/property-for-sale/find.html"
            f"?locationIdentifier={location_id}"
            f"&maxPrice={MAX_PRICE}"
            f"&index={page * PAGE_SIZE}"
            f"&sortType=6"
        )
        print(f"\nPage {page + 1}  ({len(results)}/{TARGET} found so far)")

        try:
            html = fetch(search_url)
        except Exception as exc:
            print(f"  Search page error: {exc}")
            break

        listings = parse_search_page(html)
        if not listings:
            print("  No listings returned — check the locationIdentifier or try another area.")
            break

        print(f"  {len(listings)} properties under £{MAX_PRICE:,} on this page")

        for listing in listings:
            if len(results) >= TARGET:
                break
            if listing["url"] in seen:
                continue
            seen.add(listing["url"])

            label = f"{listing['address']}  £{listing['price']:,}"
            print(f"  › {label}", end="  ", flush=True)

            time.sleep(DELAY)
            try:
                description = fetch_description(listing["url"])
            except Exception as exc:
                print(f"[fetch error: {exc}]")
                continue

            # Fall back to the short summary if detail page gave nothing
            listing["description"] = description or listing["summary"]

            if not has_dev_keywords(listing["description"]):
                print("[no dev keywords]")
                continue

            print("[keywords ✓ → asking Claude]", end="  ", flush=True)
            time.sleep(DELAY)

            try:
                assessment, score = claude_assess(client, listing)
            except Exception as exc:
                print(f"[Claude error: {exc}]")
                continue

            if score >= MIN_SCORE:
                listing["assessment"] = assessment
                listing["score"]      = score
                results.append(listing)
                print(f"SCORE {score}/10 ✓  ({len(results)}/{TARGET})")
                print(f"      {assessment}")
            else:
                print(f"score {score}/10 — below threshold, skipping")

        page += 1
        time.sleep(DELAY)

    # ── results ───────────────────────────────────────────────────────────────

    print(f"\n{'═' * 64}")
    print(f"Finished — {len(results)} properties with development potential\n")

    for i, r in enumerate(results, 1):
        print(f"{i:2}.  {r['address']}")
        print(f"      Price: £{r['price']:,}   Score: {r['score']}/10")
        print(f"      {r['assessment']}")
        print(f"      {r['url']}")
        print()

    if results:
        outfile = "rightmove_results.csv"
        with open(outfile, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f, fieldnames=["address", "price", "score", "assessment", "url"]
            )
            writer.writeheader()
            for r in results:
                writer.writerow({k: r[k] for k in writer.fieldnames})
        print(f"Results saved → {outfile}")


if __name__ == "__main__":
    loc = sys.argv[1] if len(sys.argv) > 1 else "REGION%5E61227"
    main(loc)
