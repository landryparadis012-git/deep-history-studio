"""
Deep History Studio — narrator.
Turns each beat's narration into an mp3 segment via edge-tts (free).
One audio file per beat so image/audio/caption stay in sync.
"""
import asyncio
import json
import os

import edge_tts

from config import VOICE, VOICE_RATE, VOICE_PITCH


async def _speak(text, out_path, rate, pitch, tries=3):
    last = None
    for i in range(tries):
        try:
            tts = edge_tts.Communicate(text, VOICE, rate=rate, pitch=pitch)
            await tts.save(out_path)
            if os.path.exists(out_path) and os.path.getsize(out_path) > 500:
                return
            raise RuntimeError("edge-tts produced empty/short audio")
        except Exception as e:
            last = e
            await asyncio.sleep(2 * (i + 1))
    raise RuntimeError(f"narrator: failed for '{text[:40]}...': {last}")


def load_shots(episode_dir, shots=None):
    """Shotlist + script narrations merged by index.

    IMPORTANT: shotlists produced by illustrator.write_shotlist carry the
    image/audio paths but NO narration text, and the pipeline passes such a
    shotlist straight to narrate_episode(). Narration must therefore be
    merged in here, filling any missing beat from script.json.
    """
    if shots is None:
        with open(os.path.join(episode_dir, "shotlist.json")) as f:
            shots = json.load(f)
    with open(os.path.join(episode_dir, "script.json")) as f:
        script = json.load(f)
    flat = []
    for ch in script["chapters"]:
        for beat in ch["beats"]:
            flat.append(beat.get("narration", ""))
    for shot in shots:
        if not (shot.get("narration") or "").strip():
            idx = shot.get("index", 0)
            if 1 <= idx <= len(flat):
                shot["narration"] = flat[idx - 1]
    return shots


def narrate_episode(episode_dir, shots=None):
    """Narrate every beat. Idempotent: existing mp3s are skipped."""
    # always merge narrations from script.json (shotlists lack the text)
    shots = load_shots(episode_dir, shots=shots)

    os.makedirs(os.path.join(episode_dir, "audio"), exist_ok=True)

    async def run_all():
        for shot in shots:
            idx = shot["index"]
            out_path = shot["audio"]
            if os.path.exists(out_path) and os.path.getsize(out_path) > 500:
                continue
            text = (shot.get("narration") or "").strip()
            if not text:
                raise RuntimeError(f"narrator: beat {idx} has empty narration")
            await _speak(text, out_path, VOICE_RATE, VOICE_PITCH)
            print(f"[narrator] {idx}/{len(shots)} done")

    asyncio.run(run_all())
    print(f"[narrator] done: {len(shots)} audio segments ({VOICE})")
    return shots
