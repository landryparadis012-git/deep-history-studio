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
import re
import sys
import time
import urllib.request

from config import (
    STATE_PATH, TOPICS_PATH, EPISODES_DIR, TELEGRAM_MB_LIMIT, HUB_EMIT,
    STUDIO_NAME, ENGINE_VERSION,
)
from researcher import research_topic, slugify
from writer import write_script
from illustrator import illustrate_episode, save_shotlist, DailyLimitError
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
    with open(TOPICS_PATH) as f:
        data = json.load(f)
    topics = [t for t in data.get("topics", []) if isinstance(t, str) and t.strip()]
    done = data.setdefault("done", [])
    return data, [t for t in topics if t not in done]


def mark_done(topic):
    with open(TOPICS_PATH) as f:
        data = json.load(f)
    if topic not in data.setdefault("done", []):
        data["done"].append(topic)
    data["topics"] = [t for t in data.get("topics", []) if t != topic or t in data["done"] and False]
    data["topics"] = [t for t in data.get("topics", []) if t != topic]
    with open(TOPICS_PATH, "w") as f:
        json.dump(data, f, indent=1)


# ---------------- episode dir ----------------

def episode_dir_for(ep_id):
    return os.path.join(EPISODES_DIR, ep_id)


def find_episode_dir(ep_id):
    if os.path.isdir(episode_dir_for(ep_id)):
        return episode_dir_for(ep_id)
    matches = sorted(glob.glob(os.path.join(EPISODES_DIR, f"{ep_id}*")))
    return matches[0] if matches else None


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
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode())


def tg_report(text):
    """Failure reporter — never raises (v1 pattern)."""
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not chat:
        return
    try:
        tg("sendMessage", {"chat_id": chat, "text": text[:3500]})
    except Exception as e:
        print(f"[tg] report failed: {e}")


# ---------------- hub event ----------------

def hub_emit_episode(script, final_path, duration_s):
    url = os.environ.get("HUB_URL", "").strip()
    secret = os.environ.get("HUB_WEBHOOK_SECRET", "").strip()
    if not HUB_EMIT or not url or not secret:
        return
    import hashlib
    import hmac
    ev = {
        "schema_version": 1,
        "event_id": f"history-{slugify(script['title'])}-ready",
        "type": "content.episode_ready",
        "source": "studio",
        "series_id": "deep-history",
        "channel_name": STUDIO_NAME,
        "episode_number": int(script.get("episode_number", 0)),
        "title": script["title"],
        "hook_type": "documentary",
        "duration_sec": int(duration_s),
        "occurred_at": int(time.time()),
        "engine_version": ENGINE_VERSION,
    }
    try:
        body = json.dumps(ev).encode()
        mac = hmac.new(secret.encode(), body, hashlib.sha256)
        req = urllib.request.Request(
            url.rstrip("/") + "/ingest", data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "x-hub-signature-256": "sha256=" + mac.hexdigest()},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            print(f"[hub] content.episode_ready -> {r.status}")
    except Exception as e:
        print(f"[hub] emit failed (non-fatal): {e}")


# ---------------- subcommands ----------------

def cmd_write(ep_id, topic=None):
    state = load_state()
    data, pending = load_topics()

    if topic is None:
        # resume interrupted topic first, else take next queued
        topic = state.get("current_topic") or (pending[0] if pending else None)
    if not topic:
        print("PIPELINE FAILED: no topics queued — add one to memory/topics.json")
        raise SystemExit(1)

    state["current_topic"] = topic
    if not state.get("current_episode"):
        state["episode_counter"] = state.get("episode_counter", 0) + 1
        state["current_episode"] = ep_id
    save_state(state)

    print(f"[pipeline] episode {ep_id} | topic: {topic}")

    # 1) research (cached per topic)
    research_topic(topic)

    # 2) script
    api_keys = {
        "groq": os.environ.get("GROQ_API_KEY", ""),
        "gemini": os.environ.get("GEMINI_API_KEY", ""),
    }
    edir = episode_dir_for(ep_id)
    _, script = write_script(topic, api_keys, edir)
    script["episode_number"] = state["episode_counter"]
    with open(os.path.join(edir, "script.json"), "w") as f:
        json.dump(script, f, indent=1)
    print(f"[pipeline] write complete: '{script['title']}'")


def cmd_produce(ep_id):
    edir = find_episode_dir(ep_id)
    if not edir:
        print(f"PIPELINE FAILED: no episode dir for {ep_id}")
        raise SystemExit(1)
    with open(os.path.join(edir, "script.json")) as f:
        script = json.load(f)

    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    if not account_id or not token:
        print("PIPELINE FAILED: CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN missing")
        raise SystemExit(1)

    # 3) images (raises DailyLimitError on quota — handled by workflow)
    shots = illustrate_episode(script, edir, account_id, token)
    save_shotlist(edir, shots)

    # 4) narration
    shots = narrate_episode(edir)

    # 5) render
    final = render_episode(edir)
    print(f"[pipeline] produce complete: {final}")


def cmd_notify(ep_id):
    edir = find_episode_dir(ep_id)
    if not edir:
        print(f"PIPELINE FAILED: no episode dir for {ep_id}")
        raise SystemExit(1)
    with open(os.path.join(edir, "script.json")) as f:
        script = json.load(f)
    finals = sorted(glob.glob(os.path.join(EPISODES_DIR, "episode_*.mp4")))
    final = finals[-1] if finals else None
    if not final or not os.path.exists(final):
        print("PIPELINE FAILED: no final mp4 found")
        raise SystemExit(1)

    size_mb = os.path.getsize(final) / (1024 * 1024)
    dur = 0
    try:
        p = os.popen(
            f"ffprobe -v error -show_entries format=duration "
            f"-of default=noprint_wrappers=1:nokey=1 '{final}'"
        )
        dur = float(p.read().strip() or 0)
    except Exception:
        pass

    state = load_state()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if chat:
        meta = (f"🏛 {script['title']}\n\n{script.get('description', '')}\n\n"
                f"⏱ ~{dur / 60:.0f} min | {size_mb:.0f} MB | ep {script.get('episode_number', '?')}\n"
                f"{' '.join('#' + h.lstrip('#') for h in script.get('hashtags', []))}")
        if size_mb <= TELEGRAM_MB_LIMIT:
            with open(final, "rb") as f:
                data = f.read()
            boundary = "----DsBoundary7MA4YWxkTrZu0gW"
            parts = [
                f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{chat}\r\n'.encode(),
                f'--{boundary}\r\nContent-Disposition: form-data; name="caption"\r\n\r\n'.encode()
                + meta.encode() + b"\r\n",
                f'--{boundary}\r\nContent-Disposition: form-data; name="supports_streaming"\r\n\r\ntrue\r\n'.encode(),
                (f'--{boundary}\r\nContent-Disposition: form-data; name="video"; '
                 f'filename="{os.path.basename(final)}"\r\nContent-Type: video/mp4\r\n\r\n').encode(),
                data, f"\r\n--{boundary}--\r\n".encode(),
            ]
            tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
            if tok.lower().startswith("bot"):
                tok = tok[3:].strip()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{tok}/sendVideo",
                data=b"".join(parts),
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )
            with urllib.request.urlopen(req, timeout=900) as r:
                resp = json.loads(r.read().decode())
            if not resp.get("ok"):
                raise RuntimeError(f"telegram sendVideo rejected: {resp}")
            print("[deliver] video sent to Telegram")
        else:
            print(f"[deliver] {size_mb:.0f}MB > {TELEGRAM_MB_LIMIT}MB — uploaded as workflow artifact instead")
            tg_report(f"📦 {script['title']} is {size_mb:.0f}MB — too big for Telegram. "
                      f"Download it from this workflow run's Artifacts section.")

    # 6) ONLY on full success: close the topic, emit to OpsHub
    hub_emit_episode(script, final, dur)
    topic = state.get("current_topic")
    if topic:
        mark_done(topic)
    state["current_topic"] = None
    state["current_episode"] = None
    save_state(state)
    print(f"[pipeline] episode complete — '{script['title']}' delivered")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["write", "produce", "notify"])
    ap.add_argument("--episode", required=True)
    ap.add_argument("--topic", default=None)
    args = ap.parse_args()
    try:
        if args.command == "write":
            cmd_write(args.episode, args.topic)
        elif args.command == "produce":
            cmd_produce(args.episode)
        elif args.command == "notify":
            cmd_notify(args.episode)
    except DailyLimitError as e:
        # drop the autoresume flag -> listener/workflow retries after UTC reset
        os.makedirs("memory", exist_ok=True)
        reset = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(
            (int(time.time() // 86400) + 1) * 86400))
        with open("memory/autoresume.flag", "w") as f:
            f.write(reset)
        tg_report(f"🛑 Cloudflare daily image quota reached. Resets {reset} UTC. "
                  f"Retrying is automatic — nothing is lost.")
        raise SystemExit(2)
    except Exception as e:
        tg_report(f"🧱 DEEP HISTORY STUDIO FAILED\n\n{type(e).__name__}: {e}")
        raise
