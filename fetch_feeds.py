#!/usr/bin/env python3
"""Fare Radar curated deal feeds.

Reads public RSS/Atom feeds from deal sites run by people (error fares, sales,
premium cabins), keeps only posts about Vancouver or Manila, and writes:

  feeds.json               posts (headline + link only) and the status of each feed
  data/new_curated.json    posts not seen on earlier runs (for the email)
  data/seen_curated.json   ids of posts already seen

Only titles and links are stored. Each post links back to its own site.
A feed that fails is recorded in feeds.json and skipped; it never stops the run.
"""
import datetime as dt
import email.utils
import hashlib
import html
import json
import os
import re
import ssl
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(ROOT, "feeds.json")

FEEDS = [
    {"name": "Secret Flying", "url": "https://www.secretflying.com/feed/"},
    {"name": "The Flight Deal", "url": "https://www.theflightdeal.com/feed/"},
    {"name": "Fly4Free", "url": "https://www.fly4free.com/feed/"},
    {"name": "r/flightdeals", "url": "https://www.reddit.com/r/flightdeals/.rss"},
]
UA = "Mozilla/5.0 (compatible; fare-radar/1.0; personal flight deal tracker)"
KEEP_DAYS = 14
MAX_ITEMS = 150

HOME = re.compile(r"\b(vancouver|yvr|manila|mnl)\b", re.I)
ERROR = re.compile(r"error fare|mistake fare|pricing error|glitch|price error", re.I)
TAGS = re.compile(r"<[^>]+>")


def ctx():
    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        path = os.environ.get(var)
        if path and os.path.exists(path):
            return ssl.create_default_context(cafile=path)
    return ssl.create_default_context()


CTX = ctx()


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def clean(text):
    return " ".join(html.unescape(TAGS.sub(" ", text or "")).split())


def parse_date(text):
    text = (text or "").strip()
    if not text:
        return None
    try:
        d = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        d = None
    if d is None:
        try:
            d = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


def local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_feed(raw):
    """Return [{title, link, date, tags}] from RSS 2.0 or Atom bytes."""
    root = ET.fromstring(raw)
    items = []
    for el in root.iter():
        name = local(el.tag)
        if name not in ("item", "entry"):
            continue
        row = {"title": "", "link": "", "date": "", "tags": []}
        for ch in el:
            n = local(ch.tag)
            if n == "title":
                row["title"] = clean("".join(ch.itertext()))
            elif n == "link":
                row["link"] = (ch.get("href") or (ch.text or "")).strip() or row["link"]
            elif n in ("pubDate", "published", "updated", "date") and not row["date"]:
                row["date"] = (ch.text or "").strip()
            elif n == "category":
                tag = clean(ch.get("term") or ch.text or "")
                if tag:
                    row["tags"].append(tag)
        if row["title"] and row["link"].startswith("http"):
            items.append(row)
    return items


def fetch(url):
    last = None
    for i in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"})
            with urllib.request.urlopen(req, context=CTX, timeout=25) as r:
                return r.read()
        except Exception as e:
            last = e
            time.sleep(2)
    raise RuntimeError(str(last))


def run(feeds=FEEDS, fetcher=fetch, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    prev = load(OUT, {})
    old = {i["id"]: i for i in prev.get("items", [])}
    status, found = [], {}
    for feed in feeds:
        st = {"name": feed["name"], "url": feed["url"], "ok": False, "posts": 0, "matched": 0, "error": "",
              "checked_at": now.isoformat(timespec="seconds")}
        try:
            posts = parse_feed(fetcher(feed["url"]))
            st["ok"], st["posts"] = True, len(posts)
            for p in posts:
                if not HOME.search(p["title"] + " " + " ".join(p["tags"])):
                    continue
                d = parse_date(p["date"]) or now
                iid = hashlib.sha1(p["link"].encode()).hexdigest()[:16]
                found[iid] = {"id": iid, "source": feed["name"], "title": p["title"][:220], "link": p["link"],
                              "published": d.isoformat(timespec="seconds"), "error": bool(ERROR.search(p["title"]))}
                st["matched"] += 1
        except Exception as e:
            st["error"] = str(e)[:160]
            print(f"  {feed['name']}: FAILED ({st['error']})")
        else:
            print(f"  {feed['name']}: {st['posts']} posts, {st['matched']} about Vancouver or Manila")
        status.append(st)

    # keep recent posts from earlier runs, so a post does not vanish when the site drops it from its feed
    cutoff = (now - dt.timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
    items = {**{k: v for k, v in old.items() if v["published"] >= cutoff}, **found}
    items = sorted(items.values(), key=lambda i: i["published"], reverse=True)[:MAX_ITEMS]

    seen_path = os.path.join(DATA, "seen_curated.json")
    first_run = not os.path.exists(seen_path)
    seen = set(load(seen_path, []))
    new = [i for i in items if i["id"] not in seen]
    seen |= {i["id"] for i in items}

    save(OUT, {"generated_at": now.isoformat(timespec="seconds"), "feeds": status, "items": items})
    save(seen_path, sorted(seen)[-1000:])
    save(os.path.join(DATA, "new_curated.json"),
         {"generated_at": now.isoformat(timespec="seconds"), "first_run": first_run, "items": new})
    print(f"{len(items)} posts kept, {len(new)} new")
    return 0


if __name__ == "__main__":
    sys.exit(run())
