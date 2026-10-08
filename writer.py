"""
Deep History Studio — writer.
Turns a research digest into a validated chaptered script JSON.

Script shape (v1's tolerant validation, grown up):
{
  "title": str,
  "description": str,            # YouTube description, includes source credit
  "hashtags": [str, ...],
  "chapters": [
    {
      "chapter_title": str,
      "mode": "first_person" | "overview",   # the v-two signature voice switch
      "beats": [
        {
          "narration": str,        # 2-4 sentences, facts ONLY from sources
          "caption": str,          # <= 38 chars, on-screen line
          "image_prompt": str      # the stick-figure scene to illustrate
        }
      ]
    }
  ]
}
Stdlib only.
"""
import json
import os
import re
import time
import urllib.request

from config import (
    GROQ_MODELS, GROQ_MAX_COMPLETION_TOKENS, GEMINI_MODEL,
    TARGET_SECONDS_MIN, TARGET_SECONDS_MAX, WORDS_PER_SECOND,
    MIN_CHAPTERS, MAX_CHAPTERS, IMAGES_PER_CHAPTER, STYLE_SUFFIX, EPISODES_DIR,
)
from researcher import slugify, load_digest

GROQ_API = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta/models"

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36"}


# ---------------- prompt ----------------

def writer_prompt(topic, digest):
    src_blocks = []
    for s in digest["sources"]:
        src_blocks.append(f"### SOURCE: {s['title']} ({s['url']})\n{s['text']}")
    sources_text = "\n\n".join(src_blocks)

    target_words_min = int(TARGET_SECONDS_MIN * WORDS_PER_SECOND)
    target_words_max = int(TARGET_SECONDS_MAX * WORDS_PER_SECOND)
    total_beats = MAX_CHAPTERS * IMAGES_PER_CHAPTER

    # Single-brace placeholders only; all literal JSON braces are doubled.
    return (
        "You are the head writer of a chill, fascinating history documentary "
        "channel. Episodes are vertical-video documentaries told in a calm, "
        "curious voice. The signature style alternates between FIRST PERSON "
        "(you speak as an ordinary person living the moment being described — "
        "\"I wake before dawn...\") and OVERVIEW (the wider historical view).\n\n"
        "TOPIC: {topic}\n\n"
        "RESEARCH SOURCES (your ONLY factual material — do not invent dates, "
        "names, numbers, or quotes that are not in these texts; if sources "
        "disagree or are silent on something, the narration must say so "
        "plainly):\n"
        "{sources_text}\n\n"
        "TASK: Write ONE episode of a series on this topic.\n"
        "- {min_ch}-{max_ch} chapters, each with a chapter_title.\n"
        "- Total narration {wmin}-{wmax} words (about {smin}-{smax} minutes).\n"
        "- Each chapter has exactly {bpb} beats.\n"
        "- Every beat: narration (2-4 sentences), caption (max 38 chars, a "
        "punchy on-screen line), image_prompt (a SINGLE scene of simple "
        "stick figures illustrating that beat — describe the scene, poses, "
        "props, setting era details).\n"
        "- Voice modes: the intro chapter and at least two body chapters use "
        "\"first_person\"; the rest use \"overview\". Switch at chapter level.\n"
        "- NO modern slang, no listicle energy. Calm, specific, human.\n"
        "- The final chapter lands on a quiet reflection + a line pointing "
        "to the next episode of the series.\n"
        "- description: 2-3 sentences + 'Sources: Wikipedia' credit.\n\n"
        "Respond with ONLY valid JSON, exactly this shape:\n"
        "{{\"title\": str, \"description\": str, \"hashtags\": [str], "
        "\"chapters\": [{{\"chapter_title\": str, \"mode\": "
        "\"first_person\"|\"overview\", \"beats\": [{{\"narration\": str, "
        "\"caption\": str, \"image_prompt\": str}}]}}]}}\n"
        "Total beats must be about {tb}."
    ).format(
        topic=topic, sources_text=sources_text,
        min_ch=MIN_CHAPTERS, max_ch=MAX_CHAPTERS,
        wmin=target_words_min, wmax=target_words_max,
        smin=TARGET_SECONDS_MIN // 60, smax=TARGET_SECONDS_MAX // 60,
        bpb=IMAGES_PER_CHAPTER, tb=total_beats,
    )


# ---------------- model calls ----------------

def call_groq(api_key, prompt, model):
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a rigorous, factual history documentary writer who outputs only valid JSON."},
            {"role": "user", "content": prompt},
        ],
        "max_completion_tokens": GROQ_MAX_COMPLETION_TOKENS,
        "temperature": 0.6,
    }
    req = urllib.request.Request(
        GROQ_API,
        data=json.dumps(body).encode(),
        headers={**UA, "Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        data = json.loads(r.read().decode())
    return data["choices"][0]["message"]["content"]


def call_gemini(api_key, prompt):
    url = f"{GEMINI_API}/{GEMINI_MODEL}:generateContent?key={api_key}"
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.6},
    }
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={**UA, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        data = json.loads(r.read().decode())
    parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts if isinstance(p.get("text"), str))


def generate_script(api_keys, prompt):
    """Groq chain first, Gemini fallback. Returns raw text of the winning call."""
    last_err = None
    groq_key = api_keys.get("groq")
    if groq_key:
        for model in GROQ_MODELS:
            for attempt in range(2):
                try:
                    print(f"[writer] groq {model} attempt {attempt + 1}")
                    return call_groq(groq_key, prompt, model)
                except Exception as e:
                    last_err = f"groq {model}: {e}"
                    print(f"[writer] {last_err}")
                    time.sleep(2 * (attempt + 1))
    gemini_key = api_keys.get("gemini")
    if gemini_key:
        for attempt in range(2):
            try:
                print(f"[writer] gemini fallback attempt {attempt + 1}")
                return call_gemini(gemini_key, prompt)
            except Exception as e:
                last_err = f"gemini: {e}"
                print(f"[writer] {last_err}")
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"writer: all models failed — last error: {last_err}")


# ---------------- validation ----------------

def extract_json(text):
    """Tolerant JSON extraction from an LLM reply."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        text = m.group(0)
    return json.loads(text)


def validate_script(raw):
    """Tolerant shape check: fail only on truly broken scripts (v1 lesson)."""
    if not isinstance(raw, dict):
        raise ValueError("script is not a JSON object")
    chapters = raw.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise ValueError("script has no chapters")

    clean_chapters = []
    total_words = 0
    for ch in chapters:
        if not isinstance(ch, dict):
            continue
        beats = ch.get("beats")
        if not isinstance(beats, list):
            continue
        clean_beats = []
        for b in beats:
            if not isinstance(b, dict):
                continue
            narr = (b.get("narration") or "").strip()
            if not narr:
                continue
            clean_beats.append({
                "narration": narr,
                "caption": (b.get("caption") or narr[:38]).strip()[:38],
                "image_prompt": (b.get("image_prompt") or narr[:120]).strip(),
            })
        if not clean_beats:
            continue
        clean_chapters.append({
            "chapter_title": (ch.get("chapter_title") or "Chapter").strip(),
            "mode": ch.get("mode") if ch.get("mode") in ("first_person", "overview") else "overview",
            "beats": clean_beats,
        })
        total_words += sum(len(b["narration"].split()) for b in clean_beats)

    if len(clean_chapters) < 3 or total_words < 600:
        raise ValueError(
            f"script too broken: {len(clean_chapters)} chapters, {total_words} words"
        )

    expected = TARGET_SECONDS_MIN * WORDS_PER_SECOND
    print(f"[writer] validated: {len(clean_chapters)} chapters, "
          f"{total_words} words (~{total_words / WORDS_PER_SECOND:.0f}s "
          f"narration, target >= {expected:.0f}s)")

    return {
        "title": (raw.get("title") or "Untitled Episode").strip(),
        "description": (raw.get("description") or "").strip(),
        "hashtags": [str(h) for h in (raw.get("hashtags") or [])][:10],
        "chapters": clean_chapters,
        "word_count": total_words,
        "est_seconds": round(total_words / WORDS_PER_SECOND),
    }


# ---------------- entry ----------------

def write_script(topic, api_keys, episode_dir):
    """Research -> prompt -> generate -> validate. Idempotent per episode dir."""
    digest = load_digest(topic)
    if digest is None:
        raise RuntimeError(f"writer: no research digest for '{topic}' — run researcher first")

    os.makedirs(episode_dir, exist_ok=True)
    script_path = os.path.join(episode_dir, "script.json")
    if os.path.exists(script_path):
        print(f"[writer] script already exists: {script_path} (idempotent skip)")
        with open(script_path) as f:
            return script_path, json.load(f)

    prompt = writer_prompt(topic, digest)
    raw_text = generate_script(api_keys, prompt)
    script = validate_script(extract_json(raw_text))
    script["topic"] = topic
    script["sources"] = [{"title": s["title"], "url": s["url"]} for s in digest["sources"]]

    with open(script_path, "w") as f:
        json.dump(script, f, indent=1)
    print(f"[writer] script saved: {script_path} — '{script['title']}'")
    return script_path, script


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("usage: python writer.py <topic> <episode_dir>")
        raise SystemExit(1)
    keys = {
        "groq": os.environ.get("GROQ_API_KEY", ""),
        "gemini": os.environ.get("GEMINI_API_KEY", ""),
    }
    _, s = write_script(sys.argv[1], keys, sys.argv[2])
    print(f"[writer] done: {s['word_count']} words, {len(s['chapters'])} chapters")
