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
    MIN_CHAPTERS, MAX_CHAPTERS, IMAGES_PER_CHAPTER,
)
from researcher import load_digest

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
        "- Alternate chapter modes between \"first_person\" and \"overview\".\n"
        "- Each chapter has exactly {bpc} beats.\n"
        "- Each beat: narration of 2-4 sentences written to be SPOKEN; "
        "caption of at most 38 characters (an on-screen line, no ending "
        "punctuation); image_prompt describing ONE simple stick-figure "
        "scene that illustrates the narration (simple shapes, 1-3 figures "
        "max, one clear action).\n"
        "- The whole narration should total {wmin}-{wmax} words "
        "(about {smin}-{smax} minutes of speech at a calm pace).\n"
        "- Chapters flow like a documentary: an opening that drops the "
        "viewer straight into the world, 3-5 middle chapters that move "
        "through the topic with concrete details from the sources, and a "
        "closing chapter that leaves the viewer with something to think "
        "about.\n\n"
        "OUTPUT: Return ONLY valid JSON (no markdown fences, no commentary) "
        "matching exactly this shape:\n"
        "{{\n"
        "  \"title\": \"episode title\",\n"
        "  \"description\": \"YouTube description with source credit\",\n"
        "  \"hashtags\": [\"#history\"],\n"
        "  \"chapters\": [\n"
        "    {{\n"
        "      \"chapter_title\": \"string\",\n"
        "      \"mode\": \"first_person\",\n"
        "      \"beats\": [\n"
        "        {{\n"
        "          \"narration\": \"string\",\n"
        "          \"caption\": \"short on-screen line\",\n"
        "          \"image_prompt\": \"stick figure scene\"\n"
        "        }}\n"
        "      ]\n"
        "    }}\n"
        "  ]\n"
        "}}\n"
        "Source URLs to credit in the description: {urls}"
    ).format(
        topic=topic,
        sources_text=sources_text,
        min_ch=MIN_CHAPTERS,
        max_ch=MAX_CHAPTERS,
        bpc=IMAGES_PER_CHAPTER,
        wmin=target_words_min,
        wmax=target_words_max,
        smin=TARGET_SECONDS_MIN // 60,
        smax=TARGET_SECONDS_MAX // 60,
        urls=", ".join(s["url"] for s in digest["sources"]),
    )


# ---------------- model calls ----------------

def call_groq(model, prompt, key, tries=2):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_completion_tokens": GROQ_MAX_COMPLETION_TOKENS,
        "temperature": 0.7,
    }).encode()
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(
                GROQ_API, data=body,
                headers={**UA, "Content-Type": "application/json",
                         "Authorization": f"Bearer {key}"},
            )
            with urllib.request.urlopen(req, timeout=180) as r:
                data = json.loads(r.read().decode())
            return data["choices"][0]["message"]["content"]
        except Exception as e:
            last = e
            msg = str(e)
            if "429" in msg or "rate" in msg.lower():
                time.sleep(10)
            else:
                time.sleep(3)
    raise RuntimeError(f"groq {model} failed: {last}")


def call_gemini(prompt, key, tries=2):
    url = f"{GEMINI_API}/{GEMINI_MODEL}:generateContent?key={key}"
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": GROQ_MAX_COMPLETION_TOKENS,
                             "temperature": 0.7},
    }).encode()
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(
                url, data=body,
                headers={**UA, "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=180) as r:
                data = json.loads(r.read().decode())
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            last = e
            time.sleep(4)
    raise RuntimeError(f"gemini failed: {last}")


# ---------------- JSON repair + validation ----------------

def parse_llm_json(raw):
    """Tolerant JSON extraction: strips fences, finds the outermost object."""
    if not raw:
        raise ValueError("empty response")
    raw = raw.replace("```json", "```")
    if "```" in raw:
        parts = raw.split("```")
        for part in parts:
            s = part.strip()
            if s.startswith("{"):
                raw = s
                break
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object found in response")
    candidate = raw[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        # last-resort fixes for common LLM JSON sins
        fixed = candidate
        fixed = re.sub(r",\s*([}\]])", r"\1", fixed)              # trailing commas
        fixed = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", fixed)  # control chars
        return json.loads(fixed)


def word_count(script):
    """Total spoken narration words across all chapters/beats."""
    total = 0
    for ch in script["chapters"]:
        for b in ch["beats"]:
            total += len((b.get("narration") or "").split())
    return total


def validate_script(script, topic):
    """Tolerant validation: repair what can be repaired, fail on stubs."""
    if not isinstance(script, dict):
        raise ValueError("script is not a dict")
    if not isinstance(script.get("chapters"), list) or not script["chapters"]:
        raise ValueError("script has no chapters")

    for i, ch in enumerate(script["chapters"]):
        ch.setdefault("chapter_title", f"Part {i + 1}")
        mode = ch.setdefault("mode", "overview")
        if mode not in ("first_person", "overview"):
            ch["mode"] = "overview"
        if not isinstance(ch.get("beats"), list):
            ch["beats"] = []
        for beat in ch["beats"]:
            beat.setdefault("narration", "")
            beat.setdefault("caption", "")
            beat.setdefault(
                "image_prompt",
                f"history scene illustrating the topic {topic}",
            )
            beat["caption"] = str(beat["caption"])[:38]
            beat["narration"] = str(beat["narration"]).strip()
            beat["image_prompt"] = str(beat["image_prompt"]).strip()
        ch["beats"] = [b for b in ch["beats"] if b["narration"]]

    # drop empty chapters
    script["chapters"] = [ch for ch in script["chapters"] if ch["beats"]]
    if not script["chapters"]:
        raise ValueError("all chapters empty after validation")

    script.setdefault("title", topic)
    script.setdefault(
        "description",
        f"{topic} — a Deep History Studio episode.",
    )
    script.setdefault("hashtags", ["#history"])
    wc = word_count(script)
    if wc < 300:
        raise ValueError(f"script is a stub: only {wc} words (need >=300)")
    return script


def write_script(topic, keys, episode_dir, force=False):
    """Digest -> validated script. Idempotent: existing script.json reused."""
    os.makedirs(episode_dir, exist_ok=True)
    path = os.path.join(episode_dir, "script.json")
    if not force and os.path.exists(path):
        with open(path) as f:
            s = json.load(f)
        print(f"[writer] cached script: {path} ({word_count(s)} words)")
        return path, s

    digest = load_digest(topic)
    if not digest:
        raise RuntimeError(
            f"writer: no research digest for '{topic}' — run researcher first"
        )

    prompt = writer_prompt(topic, digest)

    # Groq chain first, Gemini fallback
    s = None
    for model in GROQ_MODELS:
        key = keys.get("groq", "")
        if not key:
            break
        try:
            print(f"[writer] trying Groq {model}")
            raw = call_groq(model, prompt, key)
            s = validate_script(parse_llm_json(raw), topic)
            break
        except Exception as e:
            print(f"[writer] groq {model} unusable: {e}")
    if s is None and keys.get("gemini"):
        print("[writer] falling back to Gemini")
        raw = call_gemini(prompt, keys["gemini"])
        s = validate_script(parse_llm_json(raw), topic)
    if s is None:
        raise RuntimeError("writer: no provider produced a valid script")

    wc = word_count(s)
    if not (TARGET_SECONDS_MIN * WORDS_PER_SECOND * 0.7 <= wc
            <= TARGET_SECONDS_MAX * WORDS_PER_SECOND * 1.4):
        print(f"[writer] warn: word count {wc} outside target range "
              f"({TARGET_SECONDS_MIN}-{TARGET_SECONDS_MAX}s), continuing")

    with open(path, "w") as f:
        json.dump(s, f, indent=1)
    print(f"[writer] saved {path}: {wc} words, {len(s['chapters'])} chapters")
    return path, s


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python writer.py <topic> <episode_dir>")
        raise SystemExit(1)
    keys = {
        "groq": os.environ.get("GROQ_API_KEY", ""),
        "gemini": os.environ.get("GEMINI_API_KEY", ""),
    }
    _, s = write_script(sys.argv[1], keys, sys.argv[2])
    print(f"[writer] done: {word_count(s)} words, {len(s['chapters'])} chapters")
