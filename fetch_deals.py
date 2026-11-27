#!/usr/bin/env python3
"""Fare Radar fetcher.

Pulls the cheapest recently found round-trip fares from each home airport
(Travelpayouts / Aviasales Data API), flags routes priced well below the
usual fare for that route, and writes:

  deals.json            everything the Fare Radar page shows
  data/history.json     daily lowest price per route (90 days), for "lowest seen"
  data/new_deals.json   deals that are new or cheaper since the last run (for alerts)

The API token is read from TP_TOKEN. In a Claude cloud session the network
proxy adds the token itself, so TP_TOKEN can be left unset there.
"""
import datetime as dt
import json
import os
import ssl
import statistics
import sys
import time
import urllib.parse
import urllib.request

ORIGINS = ["YVR", "MNL"]
API = "https://api.travelpayouts.com/aviasales/v3/get_latest_prices"
CITIES_URL = "https://api.travelpayouts.com/data/en/cities.json"
COUNTRIES_URL = "https://api.travelpayouts.com/data/en/countries.json"

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(ROOT, "deals.json")

MIN_SAMPLES = 4      # fares needed on a route before we trust its "usual" price
DEAL_PCT = 20        # % below usual to count as a deal
HOT_PCT = 35
ERROR_PCT = 55
HISTORY_DAYS = 90

MIDDLE_EAST = {"AE", "QA", "SA", "OM", "BH", "KW", "JO", "IL", "LB", "IQ", "IR", "YE", "SY"}
NORTH_AMERICA = {"US", "CA"}


def ssl_context():
    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        path = os.environ.get(var)
        if path and os.path.exists(path):
            return ssl.create_default_context(cafile=path)
    return ssl.create_default_context()


CTX = ssl_context()


def get_json(url, tries=4):
    headers = {"Accept": "application/json", "User-Agent": "fare-radar/1.0"}
    token = os.environ.get("TP_TOKEN")
    if token:
        headers["X-Access-Token"] = token
    err = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, context=CTX, timeout=60) as r:
                return json.load(r)
        except Exception as e:  # network hiccup: back off and retry
            err = e
            time.sleep(2 ** i)
    raise RuntimeError(f"GET {url} failed: {err}")


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


def places():
    """City code -> [city, country, region]. Cached for 30 days."""
    cache = os.path.join(DATA, "places.json")
    if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 30 * 86400:
        return load(cache, {})
    countries = {c["code"]: c.get("name") or c["code"] for c in get_json(COUNTRIES_URL)}
    out = {}
    for c in get_json(CITIES_URL):
        cc = c.get("country_code") or ""
        out[c["code"]] = [c.get("name") or c["code"], countries.get(cc, cc), region(cc, c.get("time_zone") or "")]
    save(cache, out)
    return out


def region(cc, tz):
    if cc in MIDDLE_EAST:
        return "Middle East"
    if cc in NORTH_AMERICA:
        return "Oceania & Pacific" if tz.startswith("Pacific/") else "North America"
    top = tz.split("/")[0]
    return {
        "Asia": "Asia", "Indian": "Asia", "Europe": "Europe", "Atlantic": "Europe",
        "America": "Latin America", "Africa": "Africa",
        "Australia": "Oceania & Pacific", "Pacific": "Oceania & Pacific",
    }.get(top, "Other")


def fetch_fares(extra):
    """All cached fares matching `extra` (origin / destination / one_way), paged."""
    rows, page = [], 1
    while page <= 10:
        q = urllib.parse.urlencode(dict(extra, currency="usd", period_type="year",
                                        limit=1000, sorting="price", page=page))
        d = get_json(f"{API}?{q}")
        if not d.get("success"):
            raise RuntimeError(f"{extra} page {page}: {d.get('error')}")
        batch = d.get("data") or []
        rows += batch
        if len(batch) < 1000:
            break
        page += 1
    return rows


def fetch_origin(origin):
    return fetch_fares({"origin": origin, "one_way": "false"})


def collect_oneways(rows, key, tomorrow, exclude):
    """code -> one-way fares, economy only, departing from tomorrow on."""
    out = {}
    for x in rows:
        if x.get("trip_class") != 0 or not x.get("actual", True):
            continue
        day = x["depart_date"][:10]
        code = x[key]
        if day < tomorrow or code == exclude:
            continue
        out.setdefault(code, []).append(
            {"date": day, "price": x["value"], "stops": x.get("number_of_changes", 0),
             "gate": x.get("gate") or "", "found": x.get("found_at") or ""})
    return out


def nights_between(a, b):
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


def oneway_section(rt_all, place, tomorrow):
    """For each destination with a one-way fare out of a home airport, find the
    cheapest outbound + return pair (2 to 60 nights) and compare it with the
    cheapest round trip of similar length (within 10 nights)."""
    result = []
    for origin in ORIGINS:
        outs = collect_oneways(fetch_fares({"origin": origin, "one_way": "true"}), "destination", tomorrow, origin)
        backs = collect_oneways(fetch_fares({"destination": origin, "one_way": "true"}), "origin", tomorrow, origin)
        for dest, o_list in outs.items():
            o_list.sort(key=lambda f: (f["price"], f["date"]))
            b_list = sorted(backs.get(dest, []), key=lambda f: (f["price"], f["date"]))
            combo = None
            for a in o_list:
                for b in b_list:
                    n = nights_between(a["date"], b["date"])
                    if 2 <= n <= 60:
                        total = a["price"] + b["price"]
                        if combo is None or total < combo["total"]:
                            combo = {"total": total, "out_price": a["price"], "ret_price": b["price"],
                                     "out_date": a["date"], "ret_date": b["date"], "nights": n}
            rts = rt_all.get((origin, dest), [])
            ref = None
            if combo:
                sims = [f["price"] for f in rts if abs(nights_between(f["depart"], f["ret"]) - combo["nights"]) <= 10]
                ref = min(sims) if sims else None
            saves = round((1 - combo["total"] / ref) * 100) if combo and ref else None
            city, country, reg = place.get(dest, [dest, "", "Other"])
            result.append({
                "origin": origin, "dest": dest, "city": city, "country": country, "region": reg,
                "out": {k: o_list[0][k] for k in ("date", "price", "stops", "found")},
                "back": ({k: b_list[0][k] for k in ("date", "price", "stops", "found")} if b_list else None),
                "combo": combo, "rt_ref": ref, "rt_best": min((f["price"] for f in rts), default=None),
                "saves": saves, "beats": saves is not None and saves >= 5,
            })
    return result


def main():
    now = dt.datetime.now(dt.timezone.utc)
    today = now.date().isoformat()
    tomorrow = (now + dt.timedelta(days=1)).date().isoformat()
    place = places()
    history = load(os.path.join(DATA, "history.json"), {})
    watch = load(os.path.join(ROOT, "config.json"), {}).get("watch", [])
    previous = {(r["origin"], r["dest"]): r for r in load(OUT, {}).get("routes", []) if r.get("deal") or r.get("watch_hit")}

    routes = []
    rt_all = {}
    for origin in ORIGINS:
        by_dest = {}
        for x in fetch_origin(origin):
            if x.get("trip_class") != 0 or not x.get("actual", True) or not x.get("return_date"):
                continue
            dep, ret = x["depart_date"][:10], x["return_date"][:10]
            if dep < tomorrow or ret < dep or x["destination"] == origin:
                continue
            by_dest.setdefault(x["destination"], []).append(
                {"depart": dep, "ret": ret, "price": x["value"], "stops": x.get("number_of_changes", 0),
                 "gate": x.get("gate") or "", "found": x.get("found_at") or ""})

        for dest, fares in by_dest.items():
            fares.sort(key=lambda f: (f["price"], f["depart"]))
            rt_all[(origin, dest)] = fares
            best = fares[0]
            n = len(fares)
            typical = statistics.median(f["price"] for f in fares) if n >= MIN_SAMPLES else None
            pct = round((1 - best["price"] / typical) * 100) if typical else 0

            key = f"{origin}-{dest}"
            h = history.setdefault(key, {})
            h[today] = min(best["price"], h.get(today, best["price"]))
            low90 = min(h.values())

            deal = None
            if typical and pct >= DEAL_PCT:
                deal = "error" if pct >= ERROR_PCT else "hot" if pct >= HOT_PCT else "deal"

            options, seen = [], set()
            for f in fares:
                if (f["depart"], f["ret"]) in seen:
                    continue
                seen.add((f["depart"], f["ret"]))
                options.append({k: f[k] for k in ("depart", "ret", "price", "stops")})
                if len(options) == 6:
                    break

            city, country, reg = place.get(dest, [dest, "", "Other"])
            routes.append({
                "origin": origin, "dest": dest, "city": city, "country": country, "region": reg,
                "price": best["price"], "typical": typical, "pct": pct, "n": n, "deal": deal,
                "depart": best["depart"], "ret": best["ret"], "stops": best["stops"],
                "gate": best["gate"], "found": best["found"], "low90": low90, "options": options,
                "watch_hit": any(w.get("dest") == dest and w.get("origin", "any") in ("any", origin)
                                 and (w.get("max_usd") is None or best["price"] <= w["max_usd"]) for w in watch),
            })

    cutoff = (now - dt.timedelta(days=HISTORY_DAYS)).date().isoformat()
    history = {k: {d: p for d, p in v.items() if d >= cutoff} for k, v in history.items()}
    history = {k: v for k, v in history.items() if v}

    new = []
    for r in routes:
        if not (r["deal"] or r["watch_hit"]):
            continue
        prev = previous.get((r["origin"], r["dest"]))
        if prev is None or r["price"] <= prev["price"] * 0.95:
            new.append(r)
    new.sort(key=lambda r: -r["pct"])

    oneways = oneway_section(rt_all, place, tomorrow)

    save(OUT, {"generated_at": now.isoformat(timespec="seconds"), "origins": ORIGINS,
               "currency": "USD", "source": "Travelpayouts (Aviasales) Data API", "routes": routes,
               "oneways": oneways})
    save(os.path.join(DATA, "history.json"), history)
    save(os.path.join(DATA, "new_deals.json"), {"generated_at": now.isoformat(timespec="seconds"),
                                                "first_run": not previous, "deals": new})

    deals = [r for r in routes if r["deal"]]
    print(f"{len(routes)} routes, {len(deals)} deals, {len(new)} new or cheaper since last run")
    wins = [o for o in oneways if o["beats"]]
    print(f"{len(oneways)} one-way routes, {sum(1 for o in oneways if o['combo'])} with a usable pair, "
          f"{len(wins)} pairs beat the round trip")
    for o in sorted(wins, key=lambda o: -o["saves"]):
        c = o["combo"]
        print(f"  {o['origin']}-{o['dest']} {o['city']}: ${c['out_price']}+${c['ret_price']}=${c['total']} "
              f"({c['out_date']}..{c['ret_date']}) vs round trip ${o['rt_ref']}, saves {o['saves']}%")
    for r in sorted(deals, key=lambda r: -r["pct"])[:15]:
        print(f"  {r['origin']}-{r['dest']:<4} {r['city'][:18]:<18} ${r['price']:>5} vs ${r['typical']:>7} "
              f"-{r['pct']}%  {r['depart']}..{r['ret']}  n={r['n']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
