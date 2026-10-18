"""
Deep History Studio — pipeline orchestrator.
Subcommands (v1 pattern):
  write   <episode> [--topic X]  -> pick next topic, research, write script
  produce <episode>              -> images + narration + render
  notify  <episode>              -> Telegram delivery + hub event
Memory commits only on success (handled by the workflow). Idempotent steps.
Stdlib only.
"""
import argparse
import glob
import json
import os
import sys
import time
import urllib.request

import hub
from config import (
    STATE_PATH, TOPICS_PATH, EPISODES_DIR, TELEGRAM_MB_LIMIT, HUB_EMIT,
    STUDIO_NAME, ENGINE_VERSION, LANGUAGE,
)
LANGUAGE_ECHO = LANGUAGE
from researcher import research_topic, slugify
from writer import write_script, word_count
from illustrator import illustrate_episode, write_shotlist, DailyLimitError
from narrator import narrate_episode
from renderer import render_episode

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


# ---------------- state ----------------

def load_state():
    os.makedirs("memory", exist_ok=True)
    s = {}
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            s = json.load(f)
    s.setdefault("current_episode", None)
    s.setdefault("current_topic", None)
    s.setdefault("episode_counter", 0)
    return s


def save_state(s):
    with open(STATE_PATH, "w") as f:
        json.dump(s, f, indent=1)


# ---------------- topic queue ----------------

def load_topics():
    """Load the queue. Missing file -> create template. Empty -> loud error."""
    if not os.path.exists(TOPICS_PATH):
        os.makedirs(os.path.dirname(TOPICS_PATH) or ".", exist_ok=True)
        with open(TOPICS_PATH, "w") as f:
            json.dump({"topics": [], "done": []}, f, indent=1)
    with open(TOPICS_PATH) as f:
        data = json.load(f)
    topics = [t for t in data.get("topics", []) if isinstance(t, str) and t.strip()]
    done = data.setdefault("done", [])
    pending = [t for t in topics if t not in done]
    if not pending:
        raise RuntimeError(
            "memory/topics.json has no queued topics. "
            "Add a topic string to the 'topics' list and re-run."
        )
    return data, pending


def mark_done(topic):
    with open(TOPICS_PATH) as f:
        data = json.load(f)
    if topic not in data.setdefault("done", []):
        data["done"].append(topic)
    data["topics"] = [t for t in data.get("topics", []) if t != topic]
    with open(TOPICS_PATH, "w") as f:
        json.dump(data, f, indent=1)


# ---------------- episode dir ----------------

def episode_dir_for(ep_id):
    return os.path.join(EPISODES_DIR, ep_id)


def find_episode_dir(ep_id):
    if ep_id and os.path.isdir(episode_dir_for(ep_id)):
        return episode_dir_for(ep_id)
    if ep_id:
        matches = sorted(glob.glob(os.path.join(EPISODES_DIR, f"{ep_id}*")))
        return matches[0] if matches else None
    return None


def latest_episode_dir():
    """Newest episode folder on disk (fallback when no id/state is given)."""
    if not os.path.isdir(EPISODES_DIR):
        return None
    dirs = [
        d for d in glob.glob(os.path.join(EPISODES_DIR, "*"))
        if os.path.isdir(d)
        and os.path.exists(os.path.join(d, "script.json"))
    ]
    if not dirs:
        return None
    return max(dirs, key=os.path.getmtime)


def resolve_episode_dir(args):
    """--episode > state.current_episode > newest episode on disk."""
    state = load_state()
    ep_dir = find_episode_dir(args.episode_id)
    if not ep_dir:
        ep_dir = find_episode_dir(state.get("current_episode"))
    if not ep_dir:
        ep_dir = latest_episode_dir()
    return ep_dir, state


# ---------------- telegram ----------------

def tg(method, payload):
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if tok.lower().startswith("bot"):
        tok = tok[3:].strip()
    if not tok:
        raise RuntimeError("no TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{tok}/{method}"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={**UA, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def send_video(path, caption):
    size_mb = os.path.getsize(path) / (1024 * 1024)
    if size_mb > TELEGRAM_MB_LIMIT:
        raise RuntimeError(
            f"video is {size_mb:.1f}MB > {TELEGRAM_MB_LIMIT}MB limit"
        )
    boundary = f"----dhs{int(time.time())}"
    boundary_bytes = boundary.encode()
    fields = [("chat_id", os.environ.get("TELEGRAM_CHAT_ID", "")),
              ("caption", caption)]
    with open(path, "rb") as f:
        content = b""
        for name, val in fields:
            content += b"--" + boundary_bytes + b"\r\n"
            content += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            content += (val or "").encode() + b"\r\n"
        content += b"--" + boundary_bytes + b"\r\n"
        content += (b'Content-Disposition: form-data; name="video"; '
                    b'filename="episode.mp4"\r\n'
                    b"Content-Type: video/mp4\r\n\r\n")
        content += f.read() + b"\r\n"
        content += b"--" + boundary_bytes + b"--\r\n"
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{tok_for_bot()}/sendVideo",
        data=content,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": UA["User-Agent"],
        },
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read().decode())


def send_message(text):
    return tg("sendMessage", {
        "chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
        "text": text,
    })


def tok_for_bot():
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if tok.lower().startswith("bot"):
        tok = tok[3:].strip()
    return tok


# ---------------- hub ----------------

def hub_emit(event, payload):
    """One contract for every studio: hub.py signs and posts schema_version 1,
    falling back to the legacy unsigned /emit shape on older hubs."""
    if not HUB_EMIT:
        return False
    body = dict(payload)
    series = (body.pop("series_id", None) or body.get("topic")
              or body.get("title") or "series")
    return hub.emit(event, series_id=series, **body)


# ---------------- commands ----------------

def cmd_write(args):
    state = load_state()
    topic = args.topic or hub.trigger_topic()
    if topic:
        print(f"[pipeline] topic from {hub.why()}: {topic}")
    else:
        _, pending = load_topics()
        topic = pending[0]

    if args.episode_id:
        ep_id = args.episode_id
    else:
        ep_id = f"ep{state['episode_counter'] + 1:03d}-{slugify(topic)[:40]}"
    ep_dir = episode_dir_for(ep_id)
    os.makedirs(ep_dir, exist_ok=True)
    print(f"[pipeline] episode topic: {topic} -> {ep_id}")

    research_topic(topic)  # writes memory/sources/<topic>.json
    _, script = write_script(topic, args.keys, ep_dir, force=args.force)

    state["current_episode"] = ep_id
    state["current_topic"] = topic
    save_state(state)
    print(f"[pipeline] write complete: {ep_id} "
          f"({len(script['chapters'])} chapters, {word_count(script)} words)")
    return ep_id


def cmd_produce(args):
    ep_dir, state = resolve_episode_dir(args)
    if not ep_dir:
        raise RuntimeError("produce: unknown episode (run write first)")
    ep_id = os.path.basename(ep_dir)
    topic = state.get("current_topic")

    with open(os.path.join(ep_dir, "script.json")) as f:
        script = json.load(f)

    cf_acct = os.environ["CLOUDFLARE_ACCOUNT_ID"]
    cf_tok = os.environ["CLOUDFLARE_API_TOKEN"]

    try:
        illustrate_episode(script, ep_dir, cf_acct, cf_tok)
        _, shots = write_shotlist(script, ep_dir)
        narrate_episode(ep_dir, shots=shots)
        final = render_episode(ep_dir)
    except DailyLimitError as e:
        print(f"[pipeline] DAILY LIMIT — {e}")
        print("[pipeline] resume after UTC midnight: just re-run the workflow")
        sys.exit(42)  # distinct exit code: quota stop, not a bug

    print(f"[pipeline] produce complete: {final}")
    return ep_dir, final


def cmd_notify(args):
    ep_dir, state = resolve_episode_dir(args)
    if not ep_dir:
        raise RuntimeError("notify: unknown episode")
    ep_id = os.path.basename(ep_dir)
    topic = state.get("current_topic") or ep_id

    final = os.path.join(
        EPISODES_DIR,
        f"episode_{os.path.basename(os.path.normpath(ep_dir))}.mp4",
    )
    if not os.path.exists(final):
        raise RuntimeError(f"notify: missing video {final}")

    with open(os.path.join(ep_dir, "script.json")) as f:
        script = json.load(f)
    tags = " ".join(script.get("hashtags", []))
    caption = f"🏛 {script.get('title', topic)}\n\n{tags}"

    delivered = "telegram"
    try:
        send_video(final, caption)
        print("[pipeline] Telegram video sent")
    except Exception as e:
        print(f"[pipeline] Telegram delivery failed: {e}")

    hub.emit_series_state(
        series_id=topic or ep_id,
        episode_number=int(state.get("episode_counter", 0) or 0),
        finished=False,
        title=script.get("title", topic),
        engine_version=ENGINE_VERSION,
        language=LANGUAGE_ECHO,
    )
    hub_emit("content.episode_ready", {
        "studio": STUDIO_NAME,
        "engine": ENGINE_VERSION,
        "episode": ep_id,
        "topic": topic,
        "title": script.get("title", topic),
        "description": script.get("description", ""),
        "hashtags": script.get("hashtags", []),
        "file": final,
        "duration_min": round(script.get("duration_sec", 0) / 60, 1),
    })
    return delivered


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["write", "produce", "notify"])
    ap.add_argument("episode", nargs="?", default=None)
    ap.add_argument("--episode", dest="episode_id", default=None)
    ap.add_argument("--topic", default=None)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    # --episode wins; positional fallback; produce/notify resolve later anyway
    if not args.episode_id:
        args.episode_id = args.episode

    if args.command == "write":
        keys = {
            "groq": os.environ.get("GROQ_API_KEY", ""),
            "gemini": os.environ.get("GEMINI_API_KEY", ""),
        }
        args.keys = keys
        cmd_write(args)
    elif args.command == "produce":
        cmd_produce(args)
    elif args.command == "notify":
        cmd_notify(args)


if __name__ == "__main__":
    main()
