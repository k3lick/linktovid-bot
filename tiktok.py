"""TikTok fallback downloader.

yt-dlp's TikTok extractor answers status 10231 ("Video not available") for a
lot of perfectly public videos, because TikTok gates its web API behind
signatures and rotates them. When that happens we resolve the same post
through the tikwm.com API, which hands back a direct CDN URL, and download
that instead. Used only as a fallback so normal TikTok links keep using
yt-dlp.
"""

import json
import os
import shutil
import time
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
API = "https://www.tikwm.com/api/"
TIMEOUT = 30
CHUNK = 1 << 20


def is_tiktok(url: str) -> bool:
    try:
        host = urllib.parse.urlsplit(url).netloc.lower()
    except Exception:
        return False
    return host.endswith("tiktok.com")


def _absolute(u: str) -> str:
    if not u:
        return ""
    if u.startswith("http"):
        return u
    return "https://www.tikwm.com" + (u if u.startswith("/") else "/" + u)


def _post_api(url: str, tries: int = 3) -> dict:
    """POST to the resolver with retries; curl_cffi first when available."""
    payload = {"url": url, "hd": "1"}
    last = None
    for attempt in range(tries):
        try:
            try:
                from curl_cffi import requests as creq
                r = creq.post(API, data=payload, impersonate="chrome",
                              timeout=45)
                return json.loads(r.text)
            except ImportError:
                pass
            body = urllib.parse.urlencode(payload).encode()
            req = urllib.request.Request(
                API, data=body,
                headers={"User-Agent": UA,
                         "Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            last = e
            log_msg = f"tiktok resolver attempt {attempt + 1} failed: {e}"
            print(log_msg)
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"TikTok resolver unreachable: {last}")


def download(url: str, workdir: str) -> dict:
    """Resolve a TikTok post and save its video into workdir.

    Returns {'title', 'filepath', 'uploader'}. Raises RuntimeError with a
    readable message when the post has no video.
    """
    payload = _post_api(url)

    if payload.get("code") != 0:
        raise RuntimeError(payload.get("msg") or "TikTok resolve failed")

    data = payload.get("data") or {}
    play = _absolute(data.get("hdplay") or data.get("play")
                     or (data.get("play_addr") or {}).get("url") or "")
    if not play:
        raise RuntimeError(
            "That TikTok post has no video to download (photo slideshow or "
            "private post).")

    path = os.path.join(workdir, "tiktok.mp4")
    dl = urllib.request.Request(
        play, headers={"User-Agent": UA, "Referer": "https://www.tiktok.com/"})
    with urllib.request.urlopen(dl, timeout=120) as r, open(path, "wb") as f:
        shutil.copyfileobj(r, f, CHUNK)

    if os.path.getsize(path) == 0:
        raise RuntimeError("TikTok returned an empty file")

    author = (data.get("author") or {}).get("unique_id") or ""
    title = (data.get("title") or "").strip() or "TikTok video"
    return {"title": title[:120], "filepath": path, "uploader": author}
