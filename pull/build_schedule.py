#!/usr/bin/env python3
"""
build_schedule.py — TEST: assemble ONE teacher's real classes from studio feeds,
no screenshots. Renders each studio's booking page in a headless browser so the
JS widgets load, then:
  - Momence studios  -> read the schedule API + paginate every page
  - Mindbody studios -> parse the rendered DOM *and* every iframe (branded-web
                        loads the schedule inside cross-origin go.mindbodyonline
                        iframes, so we must walk frames, not just the top page)
Extracts the target teacher, flags substitute classes, converts to Melbourne
time, and writes pull_out/<teacher>_schedule.json + a readable summary.

For any studio that yields zero matches it dumps the raw HTML/JSON it saw, so a
parser gap is debuggable from the same artifact. Run in CI (network open) — see
the build workflow. This is the prototype the production pull grows from.
"""
import asyncio, json, re, datetime, html as H
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from playwright.async_api import async_playwright

# ---- test target -----------------------------------------------------------
TEACHER = "Janita Doelken"
TZ = datetime.timezone(datetime.timedelta(hours=10))  # AEST (Melbourne winter)
# NOTE for production: use zoneinfo("Australia/Melbourne") for DST safety.

STUDIOS = {
    "within-south-yarra":   {"url": "https://www.withinretreat.com.au/timetable", "platform": "mindbody", "name": "Within"},
    "grass-roots-st-kilda": {"url": "https://www.grassrootsyoga.com/booknow",     "platform": "momence",  "name": "Grass Roots"},
    "happy-melon-armadale": {"url": "https://happymelon.com.au/timetable/",        "platform": "mindbody", "name": "Happy Melon"},
}

OUT = Path(__file__).parent / "pull_out"


def melb(iso):
    return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ)


def time_range(start, end):
    s = start.strftime("%-I:%M")
    e = end.strftime("%-I:%M %p") if end else ""
    return f"{s}\u2013{e}".strip("\u2013")


def matches(name):
    return name and TEACHER.lower() in str(name).lower()


# ---- Momence ---------------------------------------------------------------
async def momence(sid, conf, page, captured):
    """Find the sessions API call, then paginate every page via the browser."""
    sessions_url = next((u for u in captured if "host-schedule/sessions" in u), None)
    if not sessions_url:
        return [], {"error": "sessions endpoint not seen", "seen": captured}
    pr = urlparse(sessions_url)
    q = parse_qs(pr.query)
    size = int(q.get("pageSize", ["20"])[0])
    rows, total, pno = [], None, 0
    while True:
        q["page"] = [str(pno)]
        u = urlunparse(pr._replace(query=urlencode(q, doseq=True)))
        r = await page.request.get(u)
        d = await r.json()
        rows += d.get("payload", [])
        total = d.get("pagination", {}).get("totalCount", len(rows))
        pno += 1
        if pno * size >= total or pno > 40:
            break
    out = []
    for s in rows:
        names = [s.get("teacher")] + [
            (t.get("name") if isinstance(t, dict) else t) for t in (s.get("additionalTeachers") or [])
        ]
        if not any(matches(n) for n in names):
            continue
        st, en = melb(s["startsAt"]), melb(s["endsAt"])
        orig = s.get("originalTeacher")
        out.append({
            "studio": conf["name"], "studio_id": sid,
            "day": st.strftime("%A"), "date": st.strftime("%Y-%m-%d"),
            "start": st.strftime("%H:%M"), "time": time_range(st, en),
            "class": s.get("sessionName", "").strip(),
            "sub": bool(orig and orig != s.get("teacher")),
            "_start_dt": st.isoformat(),
        })
    return out, {"pages": pno, "total_sessions": total}


# ---- Mindbody (healcode + branded-web) -------------------------------------
def parse_bw(htmls, conf, sid):
    """Parse bw-session blocks out of one or more HTML strings."""
    out = []
    seen = set()
    blob = "\n".join(htmls)
    for m in re.finditer(r'class="bw-session\b.*?(?=class="bw-session\b|class="bw-widget__day"|\Z)', blob, re.S):
        blk = m.group(0)
        dt = re.search(r'hc_starttime"\s+datetime="([0-9T:\-]+)"', blk)
        et = re.search(r'hc_endtime"\s+datetime="([0-9T:\-]+)"', blk)
        nm = re.search(r'bw-session__name">(.*?)</div>', blk, re.S)
        st = re.search(r'bw-session__staff"[^>]*>(.*?)</div>', blk, re.S)
        if not (dt and st):
            continue
        staff_raw = st.group(1)
        if not matches(staff_raw):
            continue
        start = datetime.datetime.fromisoformat(dt.group(1))
        end = datetime.datetime.fromisoformat(et.group(1)) if et else None
        name = ""
        if nm:
            n = re.sub(r'<span class="bw-session__type"[^>]*>.*?</span>', "", nm.group(1), flags=re.S)
            name = H.unescape(re.sub(r"<[^>]+>", "", n)).strip()
        sub = "bw-session__sub" in staff_raw or "substitute" in staff_raw.lower()
        key = (start.isoformat(), name)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "studio": conf["name"], "studio_id": sid,
            "day": start.strftime("%A"), "date": start.strftime("%Y-%m-%d"),
            "start": start.strftime("%H:%M"), "time": time_range(start, end),
            "class": name, "sub": sub, "_start_dt": start.isoformat(),
        })
    return out


async def mindbody(sid, conf, page):
    htmls = [await page.content()]
    for fr in page.frames:
        try:
            htmls.append(await fr.content())
        except Exception:
            pass
    rows = parse_bw(htmls, conf, sid)
    return rows, {"frames": len(page.frames), "raw_html": htmls}


# ---- driver ----------------------------------------------------------------
async def run_studio(sid, conf, browser):
    page = await browser.new_page()
    captured = []
    page.on("response", lambda r: captured.append(r.url))
    try:
        await page.goto(conf["url"], wait_until="networkidle", timeout=60000)
    except Exception:
        pass
    await page.wait_for_timeout(6000)
    if conf["platform"] == "momence":
        rows, meta = await momence(sid, conf, page, captured)
    else:
        rows, meta = await mindbody(sid, conf, page)
    # diagnostics: if nothing found, dump what we saw
    if not rows:
        if conf["platform"] == "momence":
            (OUT / f"{sid}__DEBUG_urls.json").write_text(json.dumps(captured, indent=2))
        else:
            for i, h in enumerate(meta.get("raw_html", [])):
                (OUT / f"{sid}__DEBUG_html{i}.html").write_text(h, encoding="utf-8")
    await page.close()
    print(f"{conf['name']:<12} {len(rows)} class(es) for {TEACHER}  {({k:v for k,v in meta.items() if k!='raw_html'})}")
    return rows


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    allrows = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for sid, conf in STUDIOS.items():
            try:
                allrows += await run_studio(sid, conf, browser)
            except Exception as e:
                print(f"{conf['name']}: FAILED — {e}")
        await browser.close()
    allrows.sort(key=lambda r: r["_start_dt"])
    slug = TEACHER.lower().replace(" ", "-")
    (OUT / f"{slug}_schedule.json").write_text(json.dumps(allrows, indent=2), encoding="utf-8")
    print(f"\n=== {TEACHER}: {len(allrows)} classes total (Melbourne time) ===")
    for r in allrows:
        tag = "  (substitute)" if r["sub"] else ""
        print(f"  {r['day'][:3]} {r['date']}  {r['time']:<16} {r['class']:<22} @ {r['studio']}{tag}")
    print(f"\nWritten: {OUT}/{slug}_schedule.json")


if __name__ == "__main__":
    asyncio.run(main())
