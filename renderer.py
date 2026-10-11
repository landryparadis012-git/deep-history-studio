"""
Deep History Studio — renderer.
Per beat: image + narration -> one video segment (Ken Burns zoom/pan,
burned-in caption). Then all segments concat into the final episode mp4.
Segment-per-beat keeps renders resumable and the concat lossless.
Stdlib + ffmpeg (preinstalled on GitHub runners).
"""
import json
import os
import subprocess

from config import (
    WIDTH, HEIGHT, FPS, KEN_BURNS, AUDIO_BITRATE,
    CAPTIONS, CAPTION_MAX_CHARS, CAPTION_FONT, CAPTION_FONTSIZE,
    EPISODES_DIR,
)

AUDIO_RATE = 44100


def run(cmd):
    """Run a command, raise with stderr on failure."""
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"renderer: cmd failed: {' '.join(cmd[:6])}...\n{p.stderr[-1500:]}")


def wrap_caption(text, limit=CAPTION_MAX_CHARS):
    """Wrap a caption into <=2 lines for on-screen display."""
    words = (text or "").strip().split()
    if not words:
        return ""
    lines, cur = [], ""
    for w in words:
        if len(cur) + len(w) + 1 <= limit:
            cur = (cur + " " + w).strip()
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return "\n".join(lines[:2])


def esc(text):
    """Escape text for the ffmpeg drawtext filter."""
    return (text.replace("\\", "\\\\").replace(":", "\\:")
                .replace("'", "\u2019").replace("%", "\\%"))


def segment_filter(idx, caption):
    """Ken Burns + caption filtergraph for one segment."""
    if KEN_BURNS:
        # zoompan: slow zoom in on even shots, out on odd — gentle variety
        direction = "zoom+0.0006" if idx % 2 == 0 else \
            "if(lte(zoom,1.0),1.0,max(1.001,zoom-0.0006))"
        z = (
            f"zoompan=z='{direction}':"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d={FPS * 60}:s={WIDTH}x{HEIGHT}:fps={FPS}"
        )
        parts = [
            f"scale={WIDTH * 2}:{HEIGHT * 2}",   # upscale first = smoother Ken Burns
            z,
            "format=yuv420p",
        ]
    else:
        parts = [
            f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase",
            f"crop={WIDTH}:{HEIGHT}",
            "format=yuv420p",
        ]
    if CAPTIONS and caption:
        cap = esc(wrap_caption(caption))
        if cap:
            parts.append(
                f"drawtext=fontfile={CAPTION_FONT}:text='{cap}':"
                f"fontcolor=white:fontsize={CAPTION_FONTSIZE}:"
                f"borderw=3:bordercolor=black:"
                f"x=(w-text_w)/2:y=h-180:line_spacing=8"
            )
    return ",".join(parts)


def probe_audio_duration(path):
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True,
    )
    try:
        return float(p.stdout.strip())
    except ValueError:
        return 0.0


def make_segment(shot, seg_path, pad_silence=0.6):
    """Image + narration mp3 -> one segment. Audio duration drives length."""
    idx = shot["index"]
    img = shot["image"]
    aud = shot["audio"]
    if not os.path.exists(img):
        raise RuntimeError(f"renderer: missing image {img}")
    if not os.path.exists(aud):
        raise RuntimeError(f"renderer: missing audio {aud}")

    if os.path.exists(seg_path):
        return  # idempotent

    dur = probe_audio_duration(aud)
    if dur <= 0:
        raise RuntimeError(f"renderer: unreadable audio {aud}")
    total = dur + pad_silence

    aresample = f"anullsrc=channel_layout=stereo:sample_rate={AUDIO_RATE}"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-loop", "1", "-i", img,
        "-i", aud,
        "-f", "lavfi", "-i", aresample,
        "-t", f"{total:.3f}",
        "-filter_complex",
        f"[0:v]{segment_filter(idx, shot.get('caption', ''))}[v];"
        f"[1:a]apad,aresample={AUDIO_RATE}[a1];"
        f"[a1][2:a]amix=inputs=2:duration=first:dropout_transition=0[a]",
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "aac", "-b:a", AUDIO_BITRATE,
        "-r", str(FPS),
        seg_path,
    ]
    run(cmd)
    print(f"[renderer] seg {idx}: {total:.1f}s")


def concat_segments(seg_paths, out_path):
    """Losslessly concat same-format segments via the concat demuxer."""
    list_path = out_path + ".txt"
    with open(list_path, "w") as f:
        for p in seg_paths:
            f.write(f"file '{os.path.abspath(p)}'\n")
    run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", list_path,
        "-c", "copy", out_path,
    ])
    os.remove(list_path)


def render_episode(episode_dir):
    """All shots -> final episode mp4 in EPISODES_DIR. Resumable."""
    with open(os.path.join(episode_dir, "shotlist.json")) as f:
        shots = json.load(f)
    with open(os.path.join(episode_dir, "script.json")) as f:
        script = json.load(f)
    flat = []
    for ch in script["chapters"]:
        for beat in ch["beats"]:
            flat.append(beat.get("caption", ""))
    for shot in shots:
        shot.setdefault("caption", flat[shot["index"] - 1])

    seg_dir = os.path.join(episode_dir, "segments")
    os.makedirs(seg_dir, exist_ok=True)

    seg_paths = []
    for shot in shots:
        seg = os.path.join(seg_dir, f"seg_{shot['index']:03d}.mp4")
        make_segment(shot, seg)
        seg_paths.append(seg)

    final = os.path.join(
        EPISODES_DIR,
        f"episode_{os.path.basename(os.path.normpath(episode_dir))}.mp4",
    )
    concat_segments(seg_paths, final)

    p = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration,size",
         "-of", "default=noprint_wrappers=1", final],
        capture_output=True, text=True,
    )
    print(f"[renderer] final: {final}")
    print(f"[renderer] {p.stdout.strip()}")
    return final


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python renderer.py <episode_dir>")
        raise SystemExit(1)
    render_episode(sys.argv[1])
