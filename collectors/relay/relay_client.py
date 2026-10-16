#!/usr/bin/env python3
"""
relay_client.py — run on a machine with a home IP (your Mac, or the 192.168.29.17 box) when NSE blocks the VPS.

    QS_VPS=https://quantsync-api.duckdns.org:8443 QS_RELAY_TOKEN=... python3 relay_client.py

Every few minutes: asks the VPS which URLs it could not fetch, fetches them from here (with NSE cookies),
and pushes the raw body back. The VPS runs the same validation on it as on any other route.
Needs: pip install requests
"""
import base64, os, sys, time
import requests

VPS = os.environ.get("QS_VPS", "").rstrip("/")
TOKEN = os.environ.get("QS_RELAY_TOKEN", "")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
H = {"User-Agent": UA, "Accept": "application/json, text/plain, */*", "Referer": "https://www.nseindia.com/"}


def main(loop: bool = True, every: int = 300) -> None:
    if not VPS or not TOKEN:
        sys.exit("set QS_VPS and QS_RELAY_TOKEN")
    s = requests.Session(); s.headers.update(H)
    while True:
        try:
            tasks = requests.get(f"{VPS}/api/relay/tasks", headers={"x-relay-token": TOKEN}, timeout=20).json().get("tasks", [])
            if tasks:
                s.get("https://www.nseindia.com/", timeout=20)          # cookies
            for t in tasks:
                time.sleep(1.5)
                r = s.get(t["url"], timeout=30)
                body = {"source": t["source"], "key": t["key"], "url": t["url"], "status": r.status_code,
                        "body_b64": base64.b64encode(r.content).decode()}
                requests.post(f"{VPS}/api/relay/ingest", json=body, headers={"x-relay-token": TOKEN}, timeout=60)
                print(time.strftime("%H:%M:%S"), t["source"], t["key"], r.status_code, len(r.content), flush=True)
        except Exception as e:
            print("relay error:", e, flush=True)
        if not loop:
            return
        time.sleep(every)


if __name__ == "__main__":
    main(loop="--once" not in sys.argv)
