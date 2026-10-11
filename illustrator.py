"""
Deep History Studio — illustrator.
Renders each beat's image_prompt into a stick-figure-style image via
Cloudflare Workers AI (flux-1-schnell). Style comes from config STYLE_SUFFIX
so every image shares one visual identity. Budget: ~40 images/run.
Stdlib only.
"""
import base64
import json
import os
import time
import urllib.error
import urllib.request

from config import IMAGE_MODEL, IMAGE_STEPS, STYLE_SUFFIX

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36"}


def cf_url(account_id):
    return (f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
            f"/ai/run/{IMAGE_MODEL}")


def extract_image(payload):
    """Workers AI returns a JSON envelope with base64 in result.image.
    Accept raw PNG bytes too, in case a model streams the image directly."""
    if isinstance(payload, (bytes, bytearray)):
        if payload[:8].startswith(b"\x89PNG"):
            return bytes(payload)
        payload = json.loads(payload.decode("utf-8", errors="replace"))
    if not isinstance(payload, dict):
        raise RuntimeError("image response is not JSON")
    if payload.get("success") is False:
        errs = payload.get("errors") or []
        raise RuntimeError(f"cloudflare error: {errs}")
    result = payload.get("result") or {}
    b64 = result.get("image") or result.get("image_url") or ""
    if isinstance(b64, str) and b64.startswith("data:image"):
        b64 = b64.split(",", 1)[-1]
    if not b64:
        raise RuntimeError(f"no image field in response keys={list(result)}")
    return base64.b64decode(b64)


def generate_image(account_id, token, prompt, out_path, tries=3):
    """One flux-1-schnell call -> PNG on disk. Raises on hard failure.

    Cloudflare's schema for flux-1-schnell allows ONLY prompt (<=2048 chars)
    and steps (<=8). width/height/negative_prompt are rejected with HTTP 400.
    Output is a square image; the renderer crops it to the vertical frame.
    """
    full_prompt = f"{prompt}. {STYLE_SUFFIX}"[:2048]
    body = {
        "prompt": full_prompt,
        "steps": IMAGE_STEPS,
    }
    data = json.dumps(body).encode()
    last_err = None
    for i in range(tries):
        try:
            req = urllib.request.Request(
                cf_url(account_id), data=data,
                headers={**UA, "Content-Type": "application/json",
                         "Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(req, timeout=180) as r:
                raw = r.read()
            img = extract_image(raw)
            if len(img) < 1000:
                raise RuntimeError(f"image too small ({len(img)} bytes)")
            with open(out_path, "wb") as f:
                f.write(img)
            return
        except urllib.error.HTTPError as e:
            # read the API error body: it names the offending field
            try:
                detail = e.read().decode("utf-8", errors="replace")[:400]
            except Exception:
                detail = ""
            last_err = f"HTTP {e.code}: {detail}"
            if e.code in (429, 402):
                raise DailyLimitError(f"cloudflare quota: {last_err}") from e
            if e.code == 400:
                # permanent for this payload -> no point retrying
                raise RuntimeError(f"illustrator: bad request: {last_err}") from e
            time.sleep(3 * (i + 1))
        except DailyLimitError:
            raise
        except Exception as e:
            last_err = e
            msg = str(e)
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
