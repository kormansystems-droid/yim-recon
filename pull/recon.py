#!/usr/bin/env python3
"""
recon.py — one-time capture of what each studio's booking widget actually fetches.

Why this exists: the three feeds (two Mindbody, one Momence) render via JavaScript,
so their real API endpoints and JSON shapes aren't visible in static HTML. This
opens each page in a real browser, records every JSON response the widgets load
(across iframes too), and saves the rendered HTML + a screenshot. From those
captures the per-platform normalizers get written against real data, not guesses.

Run in CI (network is open there): see .github/workflows/recon.yml
Output lands in pull/recon_out/ and is uploaded as a workflow artifact to download.

This is throwaway: once the normalizers exist, the production pull hits the
discovered endpoints directly (plain HTTP, no browser) and recon isn't run again.
"""

import asyncio, json
from pathlib import Path
from playwright.async_api import async_playwright

# The pages a member would open to see/book classes. Recon captures whatever they load.
SITES = {
    "within-south-yarra":     "https://www.withinretreat.com.au/timetable",
    "warrior-one":            "https://warrioroneyoga.com.au/book-my-mat/",
    "grass-roots-st-kilda":   "https://www.grassrootsyoga.com/booknow",
    "happy-melon-armadale":   "https://happymelon.com.au/timetable/",
    # Here Yoga: exact timetable page unconfirmed — homepage may not load the widget.
    # If this capture comes back empty, we update the URL to the real timetable page.
    "here-yoga-port-melbourne": "https://www.hereyoga.com.au/",
}

OUT = Path(__file__).parent / "recon_out"


async def capture(site_id, url, browser):
    page = await browser.new_page()
    captured = []

    async def on_response(resp):
        try:
            ct = resp.headers.get("content-type", "")
            if "json" not in ct.lower() and not resp.url.lower().endswith(".json"):
                return
            txt = (await resp.body()).decode("utf-8", "replace")
            json.loads(txt)  # keep only things that really parse as JSON
            captured.append({"url": resp.url, "status": resp.status, "ct": ct,
                             "len": len(txt), "body": txt})
        except Exception:
            pass  # non-JSON, binary, or cross-origin opaque — ignore

    page.on("response", on_response)
    try:
        await page.goto(url, wait_until="networkidle", timeout=60000)
    except Exception:
        pass  # networkidle can time out on chatty widgets; we still capture what loaded
    await page.wait_for_timeout(5000)  # let lazy widgets settle

    (OUT / f"{site_id}__page.html").write_text(await page.content(), encoding="utf-8")
    try:
        await page.screenshot(path=str(OUT / f"{site_id}__page.png"), full_page=True)
    except Exception:
        pass

    manifest = []
    for i, c in enumerate(captured):
        fn = f"{site_id}__resp{i:02d}.json"
        (OUT / fn).write_text(c["body"], encoding="utf-8")
        manifest.append({"url": c["url"], "status": c["status"],
                         "content_type": c["ct"], "bytes": c["len"], "file": fn})
    (OUT / f"{site_id}__manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")

    await page.close()
    print(f"{site_id}: {len(captured)} JSON responses captured")


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for sid, url in SITES.items():
            try:
                await capture(sid, url, browser)
            except Exception as e:
                print(f"{sid}: FAILED — {e}")
        await browser.close()
    print(f"\nDone. Inspect/download: {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
