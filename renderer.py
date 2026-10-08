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
    WIDTH, HEIGHT, FPS, KEN_BURNS, VIDEO_BITRATE, AUDIO_BITRATE,
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


def segment_filter(n_beats_total, idx, caption):
    """Ken Burns + caption filtergraph for one segment."""
    # zoompan: slow zoom in on even shots, out on odd — gentle variety
    direction = "zoom+0.0006" if idx % 2 == 0 else "if(lte(zoom,1.0),1.0,max(1.001,zoom-0.0006))"
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

    # measure narration duration
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", aud],
        capture_output=True, text=True,
    )
    try:
        dur = float(p.stdout.strip())
    except ValueError:
        dur = 5.0
    total = max(2.5, dur + pad_silence)  # minimum on-screen time per shot

    vf = segment_filter(0, idx, shot.get("caption", ""))
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-i", img,
        "-i", aud,
        "-filter_complex",
        f"[0:v]{vf}[v];[1:a]apad=pad_dur={pad_silence},aresample={AUDIO_RATE}[a]",
        "-map", "[v]", "-map", "[a]",
        "-t", f"{total:.2f}",
        "-r", str(FPS),
        "-c:v", "libx264", "-preset", "medium", "-b:v", VIDEO_BITRATE,
        "-c:a", "aac", "-b:a", AUDIO_BITRATE,
        "-pix_fmt", "yuv420p",
        "-shortest",
        seg_path,
    ]
    run(cmd)


def concat_segments(seg_paths, out_path):
    """Concat demuxer with re-encode — safest across mixed segment durations."""
    lst = os.path.join(os.path.dirname(out_path), "concat.txt")
    with open(lst, "w") as f:
        for sp in seg_paths:
            f.write(f"file '{os.path.abspath(sp)}'\n")
    run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst,
        "-c:v", "libx264", "-preset", "medium", "-b:v", VIDEO_BITRATE,
        "-c:a", "aac", "-b:a", AUDIO_BITRATE,
        "-pix_fmt", "yuv420p", "-r", str(FPS),
        "-movflags", "+faststart",
        out_path,
    ])
    os.remove(lst)


def render_episode(episode_dir):
    """shots.json -> segments -> final episode mp4. Returns final path."""
    shots_path = os.path.join(episode_dir, "shots.json")
    with open(shots_path) as f:
        shots = json.load(f)
    if not shots:
        raise RuntimeError("renderer: empty shotlist")

    seg_dir = os.path.join(episode_dir, "segments")
    os.makedirs(seg_dir, exist_ok=True)
    seg_paths = []
    for s in shots:
        seg_path = os.path.join(seg_dir, f"seg_{s['index']:03d}.mp4")
        make_segment(s, seg_path)
        seg_paths.append(seg_path)

    os.makedirs(EPISODES_DIR, exist_ok=True)
    final = os.path.join(EPISODES_DIR, f"episode_{os.path.basename(os.path.normpath(episode_dir))}.mp4")
    concat_segments(seg_paths, final)

    # report final size + duration
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration,size",
         "-of", "default=noprint_wrappers=1", final],
        capture_output=True, text=True,
    )
    print(f"[renderer] final: {final}\n[renderer] {p.stdout.strip()}")
    return final
