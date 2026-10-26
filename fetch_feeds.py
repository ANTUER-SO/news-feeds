#!/usr/bin/env python3
"""
Holt die RSS-Feeds und schreibt sie aufbereitet in feeds.json.
Läuft per GitHub Actions alle 15 Minuten. Nur Python-Standardbibliothek.

Scheitert ein Feed, bleiben seine letzten erfolgreichen Daten erhalten
(aus der vorherigen feeds.json), nur das Feld "error" wird gesetzt.
"""
import html
import json
import re
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.request import Request, urlopen

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "feeds.json")
PREVIOUS = Path(sys.argv[2]) if len(sys.argv) > 2 else OUT
TIMEOUT = 20

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

FEEDS = {
    "textil": {"url": "https://www.textilwirtschaft.de/feed", "limit": 5, "og_images": True},
    "ntv":    {"url": "https://www.n-tv.de/rss", "limit": 5},
    "trends": {"url": "https://trends.google.com/trending/rss?geo=DE", "limit": 15},
}


def fetch(url, max_bytes=5_000_000):
    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/xml, text/xml, text/html;q=0.9, */*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.5",
    })
    with urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read(max_bytes)


def local(tag):
    return tag.rsplit("}", 1)[-1].split(":")[-1]


def child_text(el, name):
    for c in el:
        if local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def first_paragraph(desc, max_len=300):
    p = re.split(r"</p>", desc, maxsplit=1, flags=re.I)[0]
    p = re.sub(r"<[^>]+>", " ", p)
    p = re.sub(r"\s+", " ", html.unescape(p)).strip()
    return p if len(p) <= max_len else p[: max_len - 1].rsplit(" ", 1)[0] + " …"


def find_image(item, desc):
    for c in item:
        name, url = local(c.tag), c.get("url")
        if name == "enclosure" and url and (c.get("type") or "image/").startswith("image"):
            return url
        if name in ("content", "thumbnail") and url and not (c.get("type") or "").startswith(("video", "audio")):
            return url
        if name == "picture" and c.text:          # Google Trends: ht:picture
            return c.text.strip()
    m = re.search(r"""<img[^>]+src=["']([^"']+)""", desc, re.I)
    return html.unescape(m.group(1)) if m else ""


def iso_date(value):
    try:
        return parsedate_to_datetime(value).isoformat()
    except Exception:
        return ""


def parse_feed(raw):
    root = ET.fromstring(raw)
    items = []
    for item in root.iter():
        if local(item.tag) != "item":
            continue
        desc = child_text(item, "description")
        items.append({
            "title": html.unescape(child_text(item, "title")),
            "link": child_text(item, "link"),
            "summary": first_paragraph(desc),
            "image": find_image(item, desc),
            "date": iso_date(child_text(item, "pubDate")),
            "traffic": child_text(item, "approx_traffic"),
        })
    return items


OG = [
    re.compile(r"""<meta[^>]+property=["']og:image["'][^>]+content=["']([^"']+)""", re.I),
    re.compile(r"""<meta[^>]+content=["']([^"']+)["'][^>]+property=["']og:image["']""", re.I),
]


def og_image(url):
    try:
        page = fetch(url, max_bytes=300_000).decode("utf-8", "ignore")
        for pat in OG:
            m = pat.search(page)
            if m:
                return html.unescape(m.group(1))
    except Exception:
        pass
    return ""


def load_feed(name, cfg):
    items = parse_feed(fetch(cfg["url"]))[: cfg["limit"]]
    if not items:
        raise ValueError("Feed enthält keine Einträge")
    if cfg.get("og_images"):
        missing = [it for it in items if not it["image"] and it["link"]]
        with ThreadPoolExecutor(max_workers=5) as pool:
            for it, img in zip(missing, pool.map(lambda i: og_image(i["link"]), missing)):
                it["image"] = img
    return items


def main():
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        previous = json.loads(PREVIOUS.read_text(encoding="utf-8")).get("feeds", {})
    except Exception:
        previous = {}

    result, failures = {}, 0
    for name, cfg in FEEDS.items():
        try:
            items = load_feed(name, cfg)
            result[name] = {"items": items, "updated": now, "error": None}
            print(f"✓ {name}: {len(items)} Einträge")
        except Exception as e:
            failures += 1
            old = previous.get(name, {})
            result[name] = {
                "items": old.get("items", []),
                "updated": old.get("updated"),
                "error": f"{type(e).__name__}: {e}",
            }
            print(f"✗ {name}: {result[name]['error']}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"generated": now, "feeds": result}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"→ {OUT} geschrieben")
    # Nur fehlschlagen, wenn gar nichts geklappt hat (dann schickt GitHub eine Mail)
    sys.exit(1 if failures == len(FEEDS) else 0)


if __name__ == "__main__":
    main()
