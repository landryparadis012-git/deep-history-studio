"""One OpsHub contract shared by every studio: feed + trigger. Stdlib only.

FEED
  POST {HUB_URL|OPSHUB_URL}/ingest with schema_version 1 events, signed with
  x-hub-signature-256 when HUB_WEBHOOK_SECRET is set. If that route errors, the
  same event is retried against the older unsigned {OPSHUB_URL}/emit shape, so
  both generations of the hub keep working. Emitting is never fatal.

TRIGGER
  A repository_dispatch of type `opshub.produce` starts a run. Workflows pass
  github.event.client_payload through HUB_PAYLOAD (JSON). Supported keys:
      topic         free-text brief for the next episode
      new_series    true -> abandon the current arc and start a fresh one
      max_episodes  override how long the series runs
      reason        free text, echoed into the log

CLI
  python hub.py state [--state-file memory/series_state.json]
  python hub.py emit <type> --series <id> [--episode N] [--extra '{"k":"v"}']
"""
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.request

SCHEMA_VERSION = 1
UA = "studio-hub/1"


# ---------------------------------------------------------------- helpers
# Turkish letters survive into series ids as ASCII, so "Toprağın Mirası"
# becomes "topragin-mirasi" instead of "topra-n-miras".
_TR = str.maketrans({"ç": "c", "ğ": "g", "ı": "i", "İ": "i", "ö": "o",
                     "ş": "s", "ü": "u", "Ç": "C", "Ğ": "G", "Ö": "O",
                     "Ş": "S", "Ü": "U"})


def _slug(value, default="series"):
    text = (value or "").translate(_TR).lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-") or default


def _base():
    raw = os.environ.get("HUB_URL") or os.environ.get("OPSHUB_URL") or ""
    return raw.strip().rstrip("/")


def _secret():
    return (os.environ.get("HUB_WEBHOOK_SECRET")
            or os.environ.get("OPSHUB_SECRET") or "").strip()


def channel():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if repo:
        return repo.split("/")[-1]
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.basename(here) or "studio"


def _post(url, body, secret, legacy=False):
    headers = {"Content-Type": "application/json", "User-Agent": UA}
    if secret and not legacy:
        headers["x-hub-signature-256"] = "sha256=" + hmac.new(
            secret.encode(), body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, resp.read()[:200].decode("utf-8", "replace")


# ---------------------------------------------------------------- feed
def emit(event_type, *, series_id, episode_number=None, event_id=None, **fields):
    """Send one event to the hub. Returns True when the hub accepted it."""
    base = _base()
    if not base:
        print("[hub] no HUB_URL/OPSHUB_URL set - not feeding the hub")
        return False
    ev = {
        "schema_version": SCHEMA_VERSION,
        "type": event_type,
        "source": "studio",
        "series_id": _slug(series_id),
        "channel_name": channel(),
        "occurred_at": int(time.time()),
    }
    if episode_number is not None:
        ev["episode_number"] = int(episode_number)
    ev["event_id"] = event_id or (
        f"studio-{ev['series_id']}-{event_type.split('.')[-1]}"
        f"-ep{ev.get('episode_number', 0)}")
    ev.update({k: v for k, v in fields.items() if v is not None})
    body = json.dumps(ev, ensure_ascii=False).encode()

    first = None
    try:
        status, resp = _post(base + "/ingest", body, _secret())
        print(f"[hub] {event_type} -> /ingest {status} {resp}")
        return True
    except Exception as e:
        first = e

    try:  # legacy hub generation: unsigned, event name in the body
        legacy = {"event": event_type, **ev}
        status, resp = _post(base + "/emit",
                             json.dumps(legacy, ensure_ascii=False).encode(),
                             "", legacy=True)
        print(f"[hub] {event_type} -> /emit (legacy) {status} {resp}")
        return True
    except Exception as second:
        print(f"[hub] emit failed (non-fatal): /ingest {first} | /emit {second}")
        return False


def emit_episode_ready(*, series_id, episode_number, title="", hook_type="",
                       engine_version="", **extra):
    return emit("content.episode_ready", series_id=series_id,
                episode_number=episode_number, title=title,
                hook_type=hook_type, engine_version=engine_version, **extra)


def emit_series_state(*, series_id, episode_number, finished=False,
                      max_episodes=None, title="", last_hook="", **extra):
    ok = emit("content.series_state", series_id=series_id,
              episode_number=episode_number, title=title,
              max_episodes=max_episodes, finished=bool(finished),
              last_hook=last_hook, **extra)
    if finished:
        emit("content.series_finished", series_id=series_id,
             episode_number=episode_number, **extra)
    return ok


# ---------------------------------------------------------------- trigger
def trigger():
    """client_payload of a repository_dispatch, or {} for a manual run."""
    raw = (os.environ.get("HUB_PAYLOAD") or "").strip()
    if not raw or raw == "null":
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        print("[hub] HUB_PAYLOAD is not JSON - ignoring")
        return {}
    return data if isinstance(data, dict) else {}


def trigger_topic(default=""):
    t = trigger().get("topic") or trigger().get("brief") or os.environ.get("TOPIC") or ""
    return str(t).strip() or default


def trigger_flag(name):
    value = trigger().get(name)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def series_length(default):
    for candidate in (trigger().get("max_episodes"), os.environ.get("MAX_EPISODES")):
        try:
            if candidate:
                return int(candidate)
        except Exception:
            continue
    return int(default)


def why():
    """Short human reason for the log when a run was triggered by the hub."""
    payload = trigger()
    if not payload:
        return "manual"
    return str(payload.get("reason") or payload.get("source") or "hub dispatch")[:80]


# ---------------------------------------------------------------- state CLI
def read_state(path="memory/series_state.json"):
    for candidate in (path, "memory/state.json"):
        if os.path.exists(candidate):
            try:
                with open(candidate) as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
            except Exception:
                return {}
    return {}


def _max_episodes_from_pipeline(path="pipeline.py", default=None):
    try:
        with open(path) as f:
            found = re.search(r"MAX_EPISODES\s*=\s*(\d+)", f.read())
        if found:
            return int(found.group(1))
    except Exception:
        pass
    return default


def state_snapshot(state=None, path="memory/series_state.json",
                   pipeline_path="pipeline.py"):
    st = state if state is not None else read_state(path)
    return {
        "series_id": st.get("series_title") or st.get("title") or "series",
        "episode_number": st.get("episode_number") or st.get("episode_counter") or 0,
        "finished": bool(st.get("finished")),
        "max_episodes": _max_episodes_from_pipeline(pipeline_path),
        "title": st.get("title") or st.get("series_title") or "",
        "last_hook": st.get("last_hook") or "",
    }


def main(argv):
    if not argv or argv[0] not in ("state", "emit"):
        print(__doc__)
        return 2
    if argv[0] == "state":
        path = "memory/series_state.json"
        if "--state-file" in argv:
            path = argv[argv.index("--state-file") + 1]
        snap = state_snapshot(path=path)
        if not os.path.exists(path) and not os.path.exists("memory/state.json"):
            print("hub: no state file yet - nothing to sync")
            return 0
        emit_series_state(**snap, engine_version=_engine())
        return 0

    event_type = argv[1] if len(argv) > 1 else ""
    opts = {}
    i = 2
    while i < len(argv) - 1:
        opts[argv[i].lstrip("-")] = argv[i + 1]
        i += 2
    extra = {}
    if opts.get("extra"):
        try:
            extra = json.loads(opts["extra"])
        except Exception:
            print("hub: --extra is not JSON, ignoring")
    ok = emit(event_type, series_id=opts.get("series", "series"),
              episode_number=opts.get("episode"),
              engine_version=_engine(), **extra)
    return 0 if ok else 1


def _engine():
    for candidate in ("ENGINE_VERSION", "VERSION"):
        try:
            with open("pipeline.py") as f:
                found = re.search(candidate + r"\s*=\s*[\"']([^\"']+)", f.read())
            if found:
                return found.group(1)
        except Exception:
            pass
    return ""


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
