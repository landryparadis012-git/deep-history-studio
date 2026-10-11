"""
Deep History Studio — illustrator.
Renders each beat's image_prompt into a stick-figure-style image via
Cloudflare Workers AI (flux-1-schnell). Style comes from config STYLE_SUFFIX
so every image shares one visual identity. Budget: ~40 images/run.
Stdlib only.
"""
import json
import os
import time
import urllib.request

from config import IMAGE_MODEL, IMAGE_STEPS, STYLE_SUFFIX, WIDTH, HEIGHT

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36"}


def cf_url(account_id):
    return (f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
            f"/ai/run/{IMAGE_MODEL}")


def generate_image(account_id, token, prompt, out_path, tries=3):
    """One flux-1-schnell call -> PNG on disk. Raises on hard failure."""
    body = {
        "prompt": f"{prompt}. {STYLE_SUFFIX}",
        "steps": IMAGE_STEPS,
        "width": WIDTH,
        "height": HEIGHT,
    }
    # flux-1-schnell on Workers AI rejects a negative prompt field on some
    # versions; IMAGE_NEGATIVE lives in config for a future provider swap.
    data = json.dumps(body).encode()
    last_err = None
    for i in range(tries):
        try:
            req = urllib.request.Request(
                cf_url(account_id), data=data,
                headers={**UA, "Content-Type": "application/json",
                         "Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(req, timeout=120) as r:
                img = r.read()
            if len(img) < 1000:
                raise RuntimeError(f"image too small ({len(img)} bytes)")
            with open(out_path, "wb") as f:
                f.write(img)
            return
        except Exception as e:
            last_err = e
            msg = str(e)
            # confirmed daily-quota shapes (v1 lessons): stop for the day
            if "429" in msg or "402" in msg:
                raise DailyLimitError(f"cloudflare quota: {msg}") from e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"illustrator: failed after {tries} tries: {last_err}")


class DailyLimitError(Exception):
    """Confirmed Cloudflare daily-quota error -> auto-resume after UTC reset."""


def save_shotlist(script, episode_dir):
    """Flatten script chapters/beats into an ordered shot list."""
    shots = []
    idx = 0
    for ch in script["chapters"]:
        for beat in ch["beats"]:
            idx += 1
            shots.append({
                "index": idx,
                "chapter_title": ch["chapter_title"],
                "mode": ch["mode"],
                "caption": beat.get("caption", ""),
                "image_prompt": beat.get("image_prompt", ""),
                "image": os.path.join(episode_dir, "images", f"img_{idx:03d}.png"),
                "audio": os.path.join(episode_dir, "audio", f"aud_{idx:03d}.mp3"),
            })
    return shots


def write_shotlist(script, episode_dir):
    shots = save_shotlist(script, episode_dir)
    path = os.path.join(episode_dir, "shotlist.json")
    with open(path, "w") as f:
        json.dump(shots, f, indent=1)
    return path, shots


def illustrate_episode(script, episode_dir, account_id, token):
    """Render every beat's image. Idempotent: existing PNGs are skipped."""
    img_dir = os.path.join(episode_dir, "images")
    os.makedirs(img_dir, exist_ok=True)
    shots = []
    idx = 0
    total = sum(len(ch["beats"]) for ch in script["chapters"])
    for ci, ch in enumerate(script["chapters"]):
        for bi, beat in enumerate(ch["beats"]):
            idx += 1
            name = f"img_{idx:03d}.png"
            out_path = os.path.join(img_dir, name)
            if os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
                continue
            prompt = beat.get("image_prompt", "") or ch["chapter_title"]
            print(f"[illustrator] {idx}/{total}: {prompt[:70]}...")
            generate_image(account_id, token, prompt, out_path)
    print(f"[illustrator] done: {total} beats checked")
    return


if __name__ == "__main__":
    import sys
    acct = os.environ["CLOUDFLARE_ACCOUNT_ID"]
    tok = os.environ["CLOUDFLARE_API_TOKEN"]
    with open(sys.argv[2] + "/script.json") as f:
        script = json.load(f)
    illustrate_episode(script, sys.argv[1], sys.argv[2], acct, tok)
