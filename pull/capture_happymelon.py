#!/usr/bin/env python3
"""
capture_happymelon.py — discover Happy Melon's "Schedules V2" data API.

Happy Melon runs Mindbody's newest branded-web widget (React Server Components).
The class list isn't in the page on load — it's fetched from an API only after a
date is selected. So this script loads the timetable, finds the schedule iframes,
and actively clicks through the date tabs / next arrows to trigger those fetches,
recording every Mindbody request + response body. From the capture we identify the
real endpoint (URL + any POST/GraphQL body) and then write a clean V2 normalizer —
same approach that made Momence and healcode work.

Output: pull/hm_out/  (response bodies + a manifest of every Mindbody call seen)
"""
import asyncio, json, re
from pathlib import Path
from playwright.async_api import async_playwright

URL = "https://happymelon.com.au/timetable/"
OUT = Path(__file__).parent / "hm_out"
# domains whose traffic we care about (the V2 widget + its data gateway)
KEEP = ("mindbodyonline.com", "mindbody.io", "prod-mkt-gateway", "gateway")

captured = []  # {url, method, status, ct, bytes, post, file}


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    requests_log = []

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()

        def want(u):
            return any(k in u for k in KEEP)

        async def on_response(resp):
            u = resp.url
            if not want(u):
                return
            try:
                body = await resp.body()
            except Exception:
                body = b""
            ct = resp.headers.get("content-type", "")
            # keep anything that looks like data (json, graphql) or is non-trivial
            interesting = ("json" in ct.lower() or "graphql" in u.lower()
                           or any(w in u.lower() for w in ("schedule", "class", "session", "appointment"))
                           or len(body) > 400)
            if not interesting:
                return
            i = len(captured)
            fn = f"resp{i:02d}.bin"
            try:
                (OUT / fn).write_bytes(body)
            except Exception:
                pass
            req = resp.request
            captured.append({
                "url": u, "method": req.method, "status": resp.status,
                "content_type": ct, "bytes": len(body),
                "post_data": (req.post_data or "")[:4000], "file": fn,
            })

        page.on("response", lambda r: asyncio.create_task(on_response(r)))
        page.on("request", lambda r: requests_log.append({"url": r.url, "method": r.method})
                 if want(r.url) else None)

        try:
            await page.goto(URL, wait_until="networkidle", timeout=60000)
        except Exception:
            pass
        await page.wait_for_timeout(6000)

        # Walk every schedule iframe and click through its date controls.
        frames = [f for f in page.frames if "go.mindbodyonline.com/book/widgets/schedules" in (f.url or "")]
        print(f"schedule frames found: {len(frames)}")
        for fi, fr in enumerate(frames):
            # try several ways to advance the date so the widget fetches each day
            for attempt in range(8):
                clicked = False
                for sel in ['button[aria-label*="next" i]', '[aria-label*="next day" i]',
                            '[role="tab"]', 'button:has-text("Next")', '.fc-next-button',
                            'button[aria-label*="forward" i]']:
                    try:
                        loc = fr.locator(sel)
                        n = await loc.count()
                        if n:
                            # for tabs, click the attempt-th one; for arrows, click first
                            idx = min(attempt, n - 1) if 'tab' in sel else 0
                            await loc.nth(idx).click(timeout=2000)
                            clicked = True
                            await page.wait_for_timeout(2200)
                            break
                    except Exception:
                        continue
                if not clicked:
                    break
            print(f"  frame {fi}: interaction done ({fr.url[:90]})")

        await page.wait_for_timeout(2000)
        await browser.close()

    (OUT / "manifest.json").write_text(json.dumps(captured, indent=2), encoding="utf-8")
    (OUT / "requests_all.json").write_text(json.dumps(requests_log, indent=2), encoding="utf-8")
    print(f"\ncaptured {len(captured)} Mindbody data responses")
    for c in captured:
        flag = "  <-- has 'Janita'" if False else ""
        print(f"  {c['bytes']:>7}  {c['method']:<4} {c['status']}  {c['url'][:120]}")
    # quick scan: which files mention a class/teacher
    print("\nfiles mentioning 'Janita' or 'startDateTime':")
    for c in captured:
        try:
            t = (OUT / c["file"]).read_text("utf-8", "replace")
        except Exception:
            continue
        if "Janita" in t or "startDateTime" in t or "classDescription" in t.lower():
            print(f"  {c['file']}  ({c['url'][:90]})")
    print(f"\nOutput: {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
