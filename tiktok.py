"""TikTok fallback downloader.

yt-dlp's TikTok extractor answers status 10231 for videos TikTok refuses to
serve to this server's region (the page data says "cross_border_violation").
Those cannot be fixed by upgrading yt-dlp, so when it fails we resolve the
post through public resolvers and download the file from the CDN they return.

Resolvers are tried in order; every failure is logged with the reason so the
Render logs show exactly which route broke.
"""

import json
import logging
import os
import shutil
import time
import urllib.parse
import urllib.request

log = logging.getLogger("tiktok")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
CHUNK = 1 << 20
MAX_BYTES = 1900 * 1024 * 1024


def is_tiktok(url: str) -> bool:
    try:
        host = urllib.parse.urlsplit(url).netloc.lower()
    except Exception:
        return False
    return host.endswith("tiktok.com")


def _http(method, url, data=None, timeout=30, headers=None, binary=False):
    """curl_cffi (Chrome TLS) when installed, plain urllib otherwise."""
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


def _json(text: str, route: str) -> dict:
    t = (text or "").strip()
    if not t:
        raise RuntimeError(f"{route}: empty reply")
    try:
        return json.loads(t)
    except ValueError:
        raise RuntimeError(f"{route}: non-JSON reply ({t[:60]!r})")


def _tikwm(url: str) -> dict:
    d = _json(_http("POST", "https://www.tikwm.com/api/",
                    {"url": url, "hd": "1"}, timeout=25), "tikwm")
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


def _tikmate(url: str) -> dict:
    d = _json(_http("POST", "https://api.tikmate.app/api/lookup",
                    {"url": url}, timeout=25), "tikmate")
    if not d.get("success") or not d.get("token") or not d.get("id"):
        raise RuntimeError("tikmate: no video in reply")
    return {"url": f"https://tikmate.app/download/{d['token']}/{d['id']}.mp4?hd=1",
            "title": (d.get("author_name") or "").strip(),
            "uploader": d.get("author_id") or d.get("author_name") or ""}


RESOLVERS = (("tikwm", _tikwm), ("tikmate", _tikmate))


def _fetch_file(video_url: str, path: str):
    data = _http("GET", video_url, timeout=120, binary=True,
                 headers={"Referer": "https://www.tiktok.com/"})
    if len(data) < 5000 or data[4:8] != b"ftyp":
        raise RuntimeError("download did not return a video file")
    if len(data) > MAX_BYTES:
        raise RuntimeError("video is too large")
    with open(path, "wb") as f:
        f.write(data)


def download(url: str, workdir: str) -> dict:
    """Resolve and save a TikTok video. Returns {'title','filepath','uploader'}.

    Raises RuntimeError listing every route that failed.
    """
    path = os.path.join(workdir, "tiktok.mp4")
    failures = []
    for name, resolve in RESOLVERS:
        for attempt in (1, 2):
            try:
                info = resolve(url)
                _fetch_file(info["url"], path)
                log.info("tiktok fallback ok via %s", name)
                return {"title": (info["title"] or "TikTok video")[:120],
                        "filepath": path, "uploader": info["uploader"]}
            except Exception as e:
                msg = f"{name} try {attempt}: {e}"
                log.warning("tiktok fallback %s", msg)
                failures.append(msg)
                if os.path.exists(path):
                    os.remove(path)
                time.sleep(2)
    raise RuntimeError(
        "TikTok is refusing this video from my server's region and the "
        "backup downloaders are down right now. Try again in a few minutes. "
        "(" + "; ".join(failures[-2:])[:200] + ")")
