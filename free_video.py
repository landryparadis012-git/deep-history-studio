"""Free, no-key B-roll discovery for Deep History Studio.

Search adapters only; callers must explicitly choose assets and render them.
Unknown/unspecified rights are rejected. Search responses are cached for 24h.
No network calls happen at import time.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

UA = {"User-Agent": "DeepHistoryStudio/1.0 (free-media-search; contact via GitHub)"}
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
ARCHIVE_SEARCH = "https://archive.org/advancedsearch.php"
ARCHIVE_META = "https://archive.org/metadata"
VIDEO_EXTS = (".mp4", ".webm", ".ogv", ".ogg")
MAX_DOWNLOAD_BYTES = 80 * 1024 * 1024
CACHE_TTL_SECONDS = 24 * 60 * 60


def _json_request(url: str, timeout: int = 25) -> dict[str, Any]:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("provider returned a non-object JSON response")
    return payload


def _allowed_commons_license(short_name: str, license_url: str) -> bool:
    label = (short_name or "").strip().lower()
    url = (license_url or "").strip().lower()
    if label in {"cc0", "cc0 1.0", "cc by 4.0", "cc by-sa 4.0",
                 "cc by 3.0", "cc by-sa 3.0", "public domain"}:
        return True
    return any(token in url for token in (
        "creativecommons.org/publicdomain/zero/",
        "creativecommons.org/licenses/by/",
        "creativecommons.org/licenses/by-sa/",
        "creativecommons.org/publicdomain/mark/",
    ))


def _allowed_archive_license(url: str) -> bool:
    value = (url or "").strip().lower()
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or parsed.hostname != "creativecommons.org":
        return False
    return any(token in parsed.path for token in (
        "/licenses/by/", "/licenses/by-sa/",
        "/publicdomain/zero/", "/publicdomain/mark/",
    ))


def _relevance(keyword: str, title: str) -> float:
    terms = {t for t in re.findall(r"[a-z0-9]{3,}", keyword.lower())}
    if not terms:
        return 0.0
    words = set(re.findall(r"[a-z0-9]{3,}", (title or "").lower()))
    return len(terms & words) / len(terms)


def search_wikimedia(keyword: str, limit: int = 5, timeout: int = 25) -> list[dict[str, Any]]:
    params = {
        "action": "query", "format": "json", "formatversion": 2,
        "generator": "search", "gsrsearch": f"filetype:video {keyword}",
        "gsrnamespace": 6, "gsrlimit": min(max(limit * 4, 10), 50),
        "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata|metadata",
    }
    payload = _json_request(COMMONS_API + "?" + urllib.parse.urlencode(params), timeout)
    pages = (payload.get("query") or {}).get("pages") or []
    results = []
    for page in pages:
        info = ((page.get("imageinfo") or [{}])[0])
        if not info.get("url") or not str(info.get("mime", "")).startswith("video/"):
            continue
        meta = info.get("extmetadata") or {}
        license_name = (meta.get("LicenseShortName") or {}).get("value", "")
        license_url = (meta.get("LicenseUrl") or {}).get("value", "")
        # Do not interpret absent/unspecified metadata as permission.
        if not _allowed_commons_license(re.sub(r"<[^>]+>", "", license_name),
                                        license_url):
            continue
        title = re.sub(r"\.[^.]+$", "", str(page.get("title", "")).removeprefix("File:"))
        score = _relevance(keyword, title)
        if score < 0.5:
            continue
        results.append({
            "provider": "Wikimedia Commons",
            "id": str(page.get("pageid", "")),
            "title": title,
            "creator": re.sub(r"<[^>]+>", "", (meta.get("Artist") or {}).get("value", "Unknown")),
            "license": re.sub(r"<[^>]+>", "", license_name).strip(),
            "license_url": license_url,
            "source_url": info.get("descriptionurl") or "",
            "download_url": info["url"],
            "mime": info.get("mime", ""),
            "width": info.get("width"),
            "height": info.get("height"),
            "duration": info.get("duration"),
            "size": info.get("size"),
            "relevance": score,
        })
    return sorted(results, key=lambda x: (x["relevance"], x.get("height") or 0), reverse=True)[:limit]


def search_internet_archive(keyword: str, limit: int = 5, timeout: int = 25) -> list[dict[str, Any]]:
    params = {
        "q": f"mediatype:movies AND ({keyword})",
        "fl[]": ["identifier", "title", "creator", "licenseurl"],
        "rows": min(max(limit * 3, 10), 30), "output": "json",
    }
    query = urllib.parse.urlencode(params, doseq=True)
    payload = _json_request(ARCHIVE_SEARCH + "?" + query, timeout)
    docs = ((payload.get("response") or {}).get("docs") or [])
    results = []
    for doc in docs:
        license_url = doc.get("licenseurl") or ""
        if not _allowed_archive_license(license_url):
            continue
        identifier = doc.get("identifier")
        if not identifier:
            continue
        meta = _json_request(f"{ARCHIVE_META}/{urllib.parse.quote(identifier)}", timeout)
        files = [f for f in (meta.get("files") or [])
                 if str(f.get("name", "")).lower().endswith(VIDEO_EXTS)]
        if not files:
            continue
        files.sort(key=lambda f: (f.get("source") == "original",
                                  int(f.get("size") or 0)), reverse=True)
        chosen = files[0]
        server, directory = meta.get("server"), meta.get("dir")
        if not server or not directory:
            continue
        title = (meta.get("metadata") or {}).get("title") or doc.get("title") or identifier
        score = _relevance(keyword, title)
        if score < 0.5:
            continue
        results.append({
            "provider": "Internet Archive", "id": identifier, "title": title,
            "creator": (meta.get("metadata") or {}).get("creator") or doc.get("creator") or "Unknown",
            "license": "Creative Commons (verify item terms)",
            "license_url": license_url,
            "source_url": f"https://archive.org/details/{identifier}",
            "download_url": f"https://{server}{directory}/{urllib.parse.quote(chosen['name'])}",
            "mime": "", "width": chosen.get("width"), "height": chosen.get("height"),
            "duration": chosen.get("length"), "size": chosen.get("size"),
            "relevance": score,
        })
        if len(results) >= limit:
            break
    return sorted(results, key=lambda x: x["relevance"], reverse=True)[:limit]


def search_free_videos(keyword: str, cache_dir: str = "memory/free_video_cache",
                       limit: int = 5, timeout: int = 25) -> list[dict[str, Any]]:
    """Search both providers with a 24h disk cache; failures are isolated."""
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    key = re.sub(r"[^a-z0-9]+", "-", keyword.lower()).strip("-")[:80] or "topic"
    path = cache / f"{key}-{limit}.json"
    try:
        cached = json.loads(path.read_text())
        if time.time() - float(cached.get("saved_at", 0)) < CACHE_TTL_SECONDS:
            return cached.get("results", [])
    except (OSError, ValueError, TypeError):
        pass
    results = []
    provider_succeeded = False
    for provider in (search_wikimedia, search_internet_archive):
        try:
            results.extend(provider(keyword, limit=limit, timeout=timeout))
            provider_succeeded = True
        except Exception as exc:
            print(f"[free-video] {provider.__name__} unavailable: {exc}")
    results.sort(key=lambda x: x.get("relevance", 0), reverse=True)
    results = results[:limit]
    # Cache valid empty searches, but never cache a total provider outage.
    if provider_succeeded:
        path.write_text(json.dumps({"saved_at": time.time(), "results": results}, indent=2))
    return results


def download_video(asset: dict[str, Any], destination: str,
                   max_bytes: int = MAX_DOWNLOAD_BYTES, timeout: int = 90) -> str:
    """Download a verified candidate from an approved provider host."""
    url = str(asset.get("download_url") or "")
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    allowed = (host == "upload.wikimedia.org" or host == "archive.org"
               or host.endswith(".archive.org"))
    if parsed.scheme != "https" or not allowed:
        raise ValueError("refusing non-HTTPS or unapproved media host")
    req = urllib.request.Request(url, headers=UA)
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".part")
    total = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response, temp.open("wb") as out:
            content_type = response.headers.get("Content-Type", "").lower()
            if content_type and not content_type.startswith("video/") and "octet-stream" not in content_type:
                raise ValueError(f"unexpected media content type: {content_type}")
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("video exceeds configured download size limit")
                out.write(chunk)
        if total == 0:
            raise ValueError("empty video download")
        os.replace(temp, target)
        return str(target)
    except Exception:
        try:
            temp.unlink()
        except OSError:
            pass
        raise
