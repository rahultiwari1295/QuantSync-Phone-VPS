"""Polite HTTP for exchange and news sites: one warmed session per site, a minimum gap between calls, retries with
backoff, and block detection (an HTML page where JSON was expected is a failure, never data)."""
from __future__ import annotations

import csv
import io
import json
import threading
import time
import xml.etree.ElementTree as ET

import requests

UA = ("Mozilla/5.0 (Linux; Android 14; CPH2723) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/128.0.0.0 Mobile Safari/537.36")
SITES = {
    "nse": {"warm": "https://www.nseindia.com/", "gap": 1.2,
            "headers": {"User-Agent": UA, "Accept": "application/json, text/plain, */*", "Accept-Language": "en-IN,en;q=0.9",
                        "Referer": "https://www.nseindia.com/"}},
    "nsearch": {"warm": None, "gap": 0.6, "headers": {"User-Agent": UA, "Referer": "https://www.nseindia.com/"}},
    "bse": {"warm": None, "gap": 0.8, "headers": {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                                                  "Origin": "https://www.bseindia.com", "Referer": "https://www.bseindia.com/"}},
    "yahoo": {"warm": None, "gap": 0.4, "headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "Accept": "application/json, text/plain, */*"}},
    "web": {"warm": None, "gap": 0.3, "headers": {"User-Agent": UA}},
}


class Blocked(Exception):
    pass


class Net:
    def __init__(self, timeout: float = 20):
        self.timeout = timeout
        self._s: dict[str, requests.Session] = {}
        self._last: dict[str, float] = {}
        self._warm_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def session(self, site: str) -> requests.Session:
        cfg = SITES.get(site, SITES["web"])
        s = self._s.get(site)
        if s is None:
            s = requests.Session()
            s.headers.update(cfg["headers"])
            self._s[site] = s
        if cfg["warm"] and time.time() - self._warm_at.get(site, 0) > 600:  # NSE needs fresh cookies every few minutes
            try:
                s.get(cfg["warm"], timeout=self.timeout)
                self._warm_at[site] = time.time()
            except requests.RequestException:
                pass
        return s

    def _wait(self, site: str):
        gap = SITES.get(site, SITES["web"])["gap"]
        with self._lock:
            d = time.time() - self._last.get(site, 0)
            if d < gap:
                time.sleep(gap - d)
            self._last[site] = time.time()

    def get(self, url: str, site: str = "web", tries: int = 3, **kw) -> requests.Response:
        err = None
        for i in range(tries):
            self._wait(site)
            try:
                r = self.session(site).get(url, timeout=self.timeout, **kw)
                if r.status_code in (401, 403) and site == "nse":
                    self._warm_at[site] = 0  # re-warm cookies, then retry
                    raise Blocked(f"{r.status_code}")
                if r.status_code == 429 or r.status_code >= 500:
                    raise Blocked(f"{r.status_code}")
                r.raise_for_status()
                return r
            except (requests.RequestException, Blocked) as e:
                err = e
                time.sleep(1.5 * (i + 1))
        raise Blocked(f"{url}: {err}")

    def json(self, url: str, site: str = "web", **kw):
        r = self.get(url, site, **kw)
        t = r.text.lstrip()
        if not t or t[0] not in "[{":
            raise Blocked(f"{url}: not JSON (got {t[:40]!r})")
        return json.loads(t)

    def csv(self, url: str, site: str = "web") -> list[dict]:
        r = self.get(url, site)
        txt = r.content.decode("utf-8-sig", errors="replace")
        if txt.lstrip().startswith("<"):
            raise Blocked(f"{url}: HTML instead of CSV")
        return [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in csv.DictReader(io.StringIO(txt))]

    def rss(self, url: str) -> list[dict]:
        r = self.get(url, "web")
        body = r.content
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            root = ET.fromstring(body.decode("utf-8", "replace").encode("ascii", "xmlcharrefreplace"))
        out = []
        for it in root.iter():
            tag = it.tag.split("}")[-1]
            if tag not in ("item", "entry"):
                continue
            g = lambda n: next((c for c in it if c.tag.split("}")[-1] == n), None)
            link = g("link")
            href = (link.get("href") if link is not None and link.get("href") else (link.text if link is not None else "")) or ""
            out.append({"title": (g("title").text if g("title") is not None else "") or "",
                        "link": href.strip(),
                        "summary": ((g("description") if g("description") is not None else g("summary")) or ET.Element("x")).text or "",
                        "published": ((g("pubDate") if g("pubDate") is not None else g("updated")) or ET.Element("x")).text or ""})
        return out

    def bytes(self, url: str, site: str = "web", max_mb: int = 25) -> bytes:
        r = self.get(url, site, stream=True)
        buf, n = io.BytesIO(), 0
        for ch in r.iter_content(65536):
            n += len(ch)
            if n > max_mb * 1024 * 1024:
                raise Blocked(f"{url}: larger than {max_mb} MB")
            buf.write(ch)
        return buf.getvalue()
