"""
Deep History Studio — researcher.
Pulls REAL source text for a topic (Wikipedia REST API, free, no key)
and writes a source digest to memory/sources/<topic>.json.
The writer may only narrate from this digest. No digest = no episode.
Stdlib only.
"""
import json
import os
import re
import time
import urllib.parse
import urllib.request

from config import WIKI_MAX_PAGES, WIKI_EXTRACT_CHARS, SOURCES_DIR

UA = {"User-Agent": "DeepHistoryStudio/1.0 (educational video research)"}
SEARCH_API = "https://en.wikipedia.org/w/api.php?action=query&list=search&format=json&srlimit={limit}&srsearch={q}"
EXTRACT_API = "https://en.wikipedia.org/w/api.php?action=query&prop=extracts&explaintext=1&format=json&titles={title}"


def http_json(url, tries=3):
    err = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            err = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"research: fetch failed: {url} -> {err}")


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "topic").lower()).strip("-") or "topic"


def search_pages(topic, limit):
    """Find the most relevant Wikipedia articles for the topic."""
    q = urllib.parse.quote(topic)
    data = http_json(SEARCH_API.format(limit=limit, q=q))
    hits = []
    for item in (data.get("query", {}).get("search") or []):
        title = item.get("title", "")
        snippet = re.sub(r"<[^>]+>", "", item.get("snippet", ""))
        if title:
            hits.append({"title": title, "hint": snippet})
    return hits


def page_extract(title):
    """Full plain-text extract of one Wikipedia article."""
    t = urllib.parse.quote(title)
    data = http_json(EXTRACT_API.format(title=t))
    pages = data.get("query", {}).get("pages", {})
    for _, page in pages.items():
        return page.get("extract", "") or ""
    return ""


def clean(text):
    """Strip wiki cruft so the writer gets clean prose."""
    text = re.sub(r"\n=+\s*.+?\s*=+\n", "\n\n", text)   # section headers
    text = re.sub(r"\[\d+\]", "", text)                  # [12] citations
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def build_digest(topic):
    """Search + fetch + clean. Returns a digest dict (never guesses facts)."""
    print(f"[research] topic: {topic}")
    hits = search_pages(topic, WIKI_MAX_PAGES + 2)
    if not hits:
        raise RuntimeError(f"research: no Wikipedia pages found for '{topic}'")
    sources = []
    for hit in hits[:WIKI_MAX_PAGES]:
        title = hit["title"]
        try:
            raw = page_extract(title)
        except Exception as e:
            print(f"[research] {title} failed: {e}")
            continue
        text = clean(raw)[:WIKI_EXTRACT_CHARS]
        if len(text) < 400:
            continue  # stub page — useless as a source
        sources.append({"title": title, "url": f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}", "text": text})
        print(f"[research] + {title} ({len(text)} chars)")
        time.sleep(1)  # polite
    if not sources:
        raise RuntimeError(f"research: all pages failed/too short for '{topic}'")
    digest = {
        "topic": topic,
        "generated_at": int(time.time()),
        "source_count": len(sources),
        "rule": "The narrator may ONLY state facts present in these source texts. If sources disagree or are silent, say so on screen.",
        "sources": sources,
    }
    return digest


def digest_path(topic):
    os.makedirs(SOURCES_DIR, exist_ok=True)
    return os.path.join(SOURCES_DIR, f"{slugify(topic)}.json")


def research_topic(topic, force=False):
    """Research (or reuse cached) sources for a topic. Returns (path, digest)."""
    path = digest_path(topic)
    if not force and os.path.exists(path):
        with open(path) as f:
            print(f"[research] cached digest: {path}")
            return path, json.load(f)
    digest = build_digest(topic)
    with open(path, "w") as f:
        json.dump(digest, f, indent=1)
    print(f"[research] digest saved: {path} ({digest['source_count']} sources)")
    return path, digest


def load_digest(topic):
    path = digest_path(topic)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python researcher.py <topic>")
        raise SystemExit(1)
    _, d = research_topic(sys.argv[1], force=True)
    print(f"[research] done: {d['source_count']} sources for '{sys.argv[1]}'")
