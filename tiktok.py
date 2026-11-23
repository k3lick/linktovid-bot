"""TikTok fallback downloader.

yt-dlp returns status 10231 for videos TikTok refuses to serve to this
server's region (the page data reports "cross_border_violation"). Those
cannot be fixed by upgrading yt-dlp, so we resolve the post through public
resolvers and pull the file from the CDN URL they return.

Resolvers are tried in order, each with retries, and every failure is logged
so the host logs show which route broke. The returned URL is validated as a
real mp4 before we hand it back.
"""

import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request

log = logging.getLogger("tiktok")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
MAX_BYTES = 1900 * 1024 * 1024


def is_tiktok(url: str) -> bool:
    try:
        host = urllib.parse.urlsplit(url).netloc.lower()
    except Exception:
        return False
    return host.endswith("tiktok.com")


def _req(method, url, data=None, timeout=30, headers=None, binary=False):
    hdrs = {"User-Agent": UA}
    hdrs.update(headers or {})
    try:
        from curl_cffi import requests as creq
        fn = creq.post if method == "POST" else creq.get
        kw = {"impersonate": "chrome", "timeout": timeout, "headers": hdrs}
        if data is not None:
            kw["data"] = data
        r = fn(url, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"http {r.status_code}")
        return r.content if binary else r.text
    except ImportError:
        pass
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return raw if binary else raw.decode("utf-8", "replace")


def _json(text, route):
    t = (text or "").strip()
    if not t:
        raise RuntimeError(f"{route}: empty reply")
    try:
        return json.loads(t)
    except ValueError:
        raise RuntimeError(f"{route}: non-JSON reply ({t[:50]!r})")


def _tikwm(url):
    d = _json(_req("POST", "https://www.tikwm.com/api/",
                   {"url": url, "hd": "1"}, timeout=45), "tikwm")
    if d.get("code") != 0:
        raise RuntimeError("tikwm: " + str(d.get("msg") or "failed"))
    data = d.get("data") or {}
    play = data.get("hdplay") or data.get("play") or ""
    if not play:
        raise RuntimeError("tikwm: no video in reply")
    if not play.startswith("http"):
        play = "https://www.tikwm.com" + play
    return {"url": play, "title": (data.get("title") or "").strip(),
            "uploader": (data.get("author") or {}).get("unique_id") or ""}


def _ssstik(url):
    page = _req("GET", "https://ssstik.io/en", timeout=25)
    m = re.search(r"s_tt\s*=\s*['\"]([^'\"]+)", page)
    if not m:
        raise RuntimeError("ssstik: no token")
    html = _req("POST", "https://ssstik.io/abc?url=dl",
                {"id": url, "locale": "en", "tt": m.group(1)},
                timeout=35,
                headers={"Referer": "https://ssstik.io/en",
                         "HX-Request": "true",
                         "HX-Current-URL": "https://ssstik.io/en"})
    links = re.findall(r'https://[^"\'\s<>]+?\.mp4[^"\'\s<>]*', html)
    if not links:
        raise RuntimeError("ssstik: no mp4 in reply")
    return {"url": links[0].replace("&amp;", "&"), "title": "", "uploader": ""}


def _tikmate(url):
    d = _json(_req("POST", "https://api.tikmate.app/api/lookup",
                   {"url": url}, timeout=25), "tikmate")
    if not d.get("success") or not d.get("token") or not d.get("id"):
        raise RuntimeError("tikmate: no video in reply")
    return {"url": f"https://tikmate.app/download/{d['token']}/{d['id']}.mp4?hd=1",
            "title": (d.get("author_name") or "").strip(),
            "uploader": d.get("author_name") or ""}


RESOLVERS = (("tikwm", _tikwm), ("ssstik", _ssstik), ("tikmate", _tikmate))


def _fetch(video_url, path):
    data = _req("GET", video_url, timeout=150, binary=True,
                headers={"Referer": "https://www.tiktok.com/",
                         "Accept": "*/*"})
    if len(data) < 5000 or data[4:8] != b"ftyp":
        raise RuntimeError(f"not a video ({len(data)} bytes)")
    if len(data) > MAX_BYTES:
        raise RuntimeError("video too large")
    with open(path, "wb") as f:
        f.write(data)
    return len(data)


def download(url, workdir):
    """Resolve and save a TikTok video: {'title','filepath','uploader'}."""
    path = os.path.join(workdir, "tiktok.mp4")
    failures = []
    for name, resolve in RESOLVERS:
        for attempt in (1, 2):
            try:
                info = resolve(url)
                size = _fetch(info["url"], path)
                log.info("tiktok fallback ok via %s (%d bytes)", name, size)
                return {"title": (info["title"] or "TikTok video")[:120],
                        "filepath": path, "uploader": info["uploader"]}
            except Exception as e:
                failures.append(f"{name}#{attempt}: {e}")
                log.warning("tiktok fallback %s#%d failed: %s", name, attempt, e)
                if os.path.exists(path):
                    os.remove(path)
                time.sleep(2)
    raise RuntimeError(
        "TikTok won't serve this video to my server (it's region-locked), and "
        "the backup downloaders all refused: " + " | ".join(failures[-3:])[:220])
