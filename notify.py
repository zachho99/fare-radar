#!/usr/bin/env python3
"""Email new Fare Radar deals through Gmail.

Reads data/new_deals.json (written by fetch_deals.py) and config.json, and
emails the deals worth hearing about. Needs these environment variables
(GitHub repository secrets):

  GMAIL_USER          the Gmail address that sends the email
  GMAIL_APP_PASSWORD  a Gmail app password for that address (not your normal password)
  ALERT_TO            where to send alerts (defaults to GMAIL_USER)
  SITE_URL            optional link to the Fare Radar page

Run with --dry-run to print the email instead of sending it.
"""
import html
import json
import os
import smtplib
import sys
import urllib.parse
from email.message import EmailMessage

ROOT = os.path.dirname(os.path.abspath(__file__))
RATES = {"CAD": 1.39, "PHP": 58.5}
LOCAL = {"YVR": "CAD", "MNL": "PHP"}
SYM = {"CAD": "CA$", "PHP": "₱"}
LABEL = {"error": "Possible error fare", "hot": "Hot deal", "deal": "Deal"}


def load(name, default):
    try:
        with open(os.path.join(ROOT, name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def local_price(r):
    cur = LOCAL.get(r["origin"])
    if not cur:
        return ""
    v = r["price"] * RATES[cur]
    v = round(v / 10) * 10 if cur == "PHP" else round(v)
    return f" (about {SYM[cur]}{v:,.0f})"


def gflights(r):
    q = f"Flights from {r['origin']} to {r['city']} on {r['depart']} through {r['ret']}"
    return "https://www.google.com/travel/flights?q=" + urllib.parse.quote(q)


def worth_sending(r, cfg):
    if r.get("watch_hit"):
        return True
    return r["deal"] in cfg.get("types", ["error", "hot", "deal"]) and r["pct"] >= cfg.get("min_pct", 30)


def main():
    dry = "--dry-run" in sys.argv
    cfg = load("config.json", {})
    new = load("data/new_deals.json", {})
    if new.get("first_run") and not dry:
        print("First run: saving a baseline, no email sent.")
        return 0
    deals = [r for r in new.get("deals", []) if worth_sending(r, cfg)]
    if not deals:
        print("No new deals worth an email.")
        return 0

    best = deals[0]
    subject = (f"Fare Radar: {len(deals)} new deal{'s' if len(deals) > 1 else ''}, "
               f"best {best['origin']} to {best['city']} US${best['price']}"
               + (f" (-{best['pct']}%)" if best["pct"] > 0 else ""))
    site = os.environ.get("SITE_URL", "")

    text = [subject, ""]
    rows = []
    for r in deals:
        stops = "nonstop" if r["stops"] == 0 else f"{r['stops']} stop{'s' if r['stops'] > 1 else ''}"
        kind = LABEL.get(r["deal"], "On your watchlist")
        usual = f"{r['pct']}% under the usual US${r['typical']:.0f}" if r.get("typical") and r["pct"] > 0 else "under your watchlist price"
        line = (f"{r['origin']} to {r['city']}, {r['country']}: US${r['price']}{local_price(r)} round trip, "
                f"{usual}. {r['depart']} to {r['ret']}, {stops}. {kind}.")
        text += [line, gflights(r), ""]
        rows.append(
            f"<tr><td style='padding:10px 0;border-top:1px solid #dde3ea'>"
            f"<b>{html.escape(r['origin'])} to {html.escape(r['city'])}</b> "
            f"<span style='color:#56657a'>{html.escape(r['country'])}</span><br>"
            f"<span style='font-size:20px;font-weight:700'>US${r['price']}</span>{html.escape(local_price(r))} "
            f"<span style='color:#0b7468;font-weight:700'>{html.escape(usual)}</span><br>"
            f"{html.escape(r['depart'])} to {html.escape(r['ret'])}, {stops} &middot; {kind}<br>"
            f"<a href='{html.escape(gflights(r))}'>Check on Google Flights</a></td></tr>")
    if site:
        text.append(f"All deals: {site}")
    text.append("Prices are the cheapest fares travelers recently found and can change. Confirm before booking.")

    body = ("<div style='font-family:Arial,sans-serif;font-size:14px;color:#0e1a2b;max-width:560px'>"
            f"<h2 style='margin:0 0 8px'>{len(deals)} new flight deal{'s' if len(deals) > 1 else ''}</h2>"
            f"<table style='width:100%;border-collapse:collapse'>{''.join(rows)}</table>"
            + (f"<p><a href='{html.escape(site)}'>See every deal on Fare Radar</a></p>" if site else "")
            + "<p style='color:#56657a;font-size:12px'>Prices are the cheapest fares travelers recently found "
              "and can change. Confirm before booking.</p></div>")

    if dry:
        print("\n".join(text))
        return 0

    user = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    to = os.environ.get("ALERT_TO") or user
    if not user or not password:
        print("GMAIL_USER or GMAIL_APP_PASSWORD is not set, so no email was sent.")
        return 0

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"Fare Radar <{user}>"
    msg["To"] = to
    msg.set_content("\n".join(text))
    msg.add_alternative(body, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
        s.login(user, password.replace(" ", ""))
        s.send_message(msg)
    print(f"Emailed {len(deals)} deals.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
