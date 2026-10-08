"""
Deep History Studio — narrator.
edge-tts narration per beat. One mp3 per beat keeps the render pipeline
simple and resumable. Chill pacing comes from config VOICE_RATE/PITCH.
edge-tts is installed from requirements.txt (not stdlib).
"""
import asyncio
import json
import os

import edge_tts

from config import VOICE, VOICE_RATE, VOICE_PITCH, EPISODES_DIR


async def _speak(text, out_path):
    tts = edge_tts.Communicate(text, VOICE, rate=VOICE_RATE, pitch=VOICE_PITCH)
    await tts.save(out_path)


def narrate_beat(text, out_path, tries=3):
    last_err = None
    for i in range(tries):
        try:
            asyncio.run(_speak(text, out_path))
            if os.path.getsize(out_path) > 1000:
                return
        except Exception as e:
            last_err = e
            import time
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"narrator: TTS failed for {out_path}: {last_err}")


def narrate_episode(episode_dir):
    """Reads shots.json, produces audio/audio_NNN.mp3 for each beat."""
    shots_path = os.path.join(episode_dir, "shots.json")
    with open(shots_path) as f:
        shots = json.load(f)
    audio_dir = os.path.join(episode_dir, "audio")
    os.makedirs(audio_dir, exist_ok=True)
    for s in shots:
        out_path = os.path.join(audio_dir, f"audio_{s['index']:03d}.mp3")
        s["audio"] = out_path
        if os.path.exists(out_path):
            continue  # idempotent
        narrate_beat(s["narration"], out_path)
    with open(shots_path, "w") as f:
        json.dump(shots, f, indent=1)
    print(f"[narrator] done: {len(shots)} audio segments ({VOICE})")
    return shots
