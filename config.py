"""
Deep History Studio — series configuration.
Every creative knob lives here. Change values freely; the engine reads them.
"""

# ---------------- identity ----------------
STUDIO_NAME = "Deep History Studio"
ENGINE_VERSION = "v1"

# ---------------- voice (edge-tts) ----------------
# Chill documentary narrator. Swap freely:
#   en-US-GuyNeural        warm, conversational chill (default)
#   en-US-ChristopherNeural deep & steady (v1 voice)
#   en-GB-RyanNeural       British documentary vibe
VOICE = "en-US-GuyNeural"
# Slightly slower than default reads as "chill"
VOICE_RATE = "-8%"
VOICE_PITCH = "-2Hz"

# ---------------- episode shape ----------------
# Target runtime in SECONDS (6-10 min episodes; one topic per episode)
TARGET_SECONDS_MIN = 360
TARGET_SECONDS_MAX = 600
# Words per second for this voice at the rate above (measured ~2.6 wps)
WORDS_PER_SECOND = 2.6
# Chapters per episode: intro, 3-5 body chapters, closer
MIN_CHAPTERS = 5
MAX_CHAPTERS = 7

# ---------------- visual style ----------------
# The stick-figure documentary look. This string is appended to EVERY image
# prompt so all images share one visual identity. Do not remove the
# consistent elements (line weight, paper, single accent color).
STYLE_SUFFIX = (
    "minimalist hand-drawn stick figure illustration, black ink lines on "
    "aged parchment paper background, simple stick figures with expressive "
    "poses, thin consistent line weight, one muted terracotta red accent "
    "color, subtle paper texture, educational history diagram style, "
    "clean composition, no text, no letters, no words"
)

# Negative guidance where the provider supports it
IMAGE_NEGATIVE = "text, letters, words, captions, watermark, realistic photo, photorealistic, color photograph"

# ---------------- captions ----------------
CAPTIONS = True
CAPTION_MAX_CHARS = 38          # wrap point for on-screen lines
CAPTION_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
CAPTION_FONTSIZE = 34

# ---------------- rendering ----------------
WIDTH, HEIGHT = 720, 1280       # vertical
FPS = 24
KEN_BURNS = True                # slow zoom/pan on every image
VIDEO_BITRATE = "900k"          # keeps 6-10 min under Telegram's 50MB
AUDIO_BITRATE = "96k"

# ---------------- writing model chain (Groq primary) ----------------
GROQ_MODELS = [
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3-32b",
]
GROQ_MAX_COMPLETION_TOKENS = 6000   # v1 lesson: default ~1024 truncates JSON
GEMINI_MODEL = "gemini-flash-latest"

# ---------------- research ----------------
# Wikipedia REST API is free, no key. These page fetches per topic cap the
# context size we hand the writer.
WIKI_MAX_PAGES = 4
WIKI_EXTRACT_CHARS = 6000

# ---------------- images ----------------
IMAGE_MODEL = "@cf/black-forest-labs/flux-1-schnell"
IMAGE_STEPS = 4                  # schnell is 1-4 steps; 4 = best quality
IMAGES_PER_CHAPTER = 6           # ~30-42 images per episode

# ---------------- series memory ----------------
MAX_EPISODES_PER_TOPIC = 1       # one episode per queued topic
STATE_PATH = "memory/state.json"
TOPICS_PATH = "memory/topics.json"
SOURCES_DIR = "memory/sources"
EPISODES_DIR = "episodes"

# ---------------- delivery ----------------
TELEGRAM_MB_LIMIT = 48           # send as file if under; artifact fallback above

# ---------------- OpsHub integration ----------------
HUB_EMIT = True                  # fire content.episode_ready on success
