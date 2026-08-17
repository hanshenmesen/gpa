"""GPA configuration constants."""
import os
from pathlib import Path

from gpa.runtime_config import env_float, env_int, env_path, user_data_path

# Project root
PROJECT_ROOT = Path(__file__).parent.parent

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

# A source checkout keeps data nearby for development. A regular wheel install
# must not try to write inside site-packages, so it uses the OS user-data area.
DEFAULT_STORAGE_DIR = (
    PROJECT_ROOT / "storage"
    if (PROJECT_ROOT / "pyproject.toml").is_file()
    else user_data_path("GPA")
)
STORAGE_DIR = env_path("GPA_STORAGE_DIR", DEFAULT_STORAGE_DIR, base=PROJECT_ROOT)
WORKFLOWS_DIR = STORAGE_DIR / "workflows"
MODELS_CACHE_DIR = env_path(
    "GPA_MODELS_CACHE_DIR",
    Path.home() / ".cache" / "gpa" / "models",
    base=PROJECT_ROOT,
)

# Ensure dirs exist
WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
MODELS_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# HuggingFace model IDs
HF_GUI_DETECTOR = "Salesforce/GPA-GUI-Detector"
HF_ICON_CLIP = "openai/clip-vit-base-patch32"   # IconCLIP base (likaixin/IconClip-ViT-B-32 fine-tune)
HF_SENTENCE_E5 = "intfloat/multilingual-e5-small"

# LLM API
LLM_API_KEY = os.environ.get("GPA_LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("GPA_LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.environ.get("GPA_LLM_MODEL", "gpt-5.5")
# Optional per-modality overrides.  Empty values deliberately fall back to
# GPA_LLM_MODEL so existing deployments keep exactly the same behaviour.
LLM_TEXT_MODEL = os.environ.get("GPA_LLM_TEXT_MODEL", "")
LLM_VISION_MODEL = os.environ.get("GPA_LLM_VISION_MODEL", "")
LLM_TEXT_FALLBACK_MODEL = os.environ.get("GPA_LLM_TEXT_FALLBACK_MODEL", "")
LLM_VISION_FALLBACK_MODEL = os.environ.get("GPA_LLM_VISION_FALLBACK_MODEL", "")
LLM_REQUEST_TIMEOUT_SECONDS = env_float(
    "GPA_LLM_TIMEOUT_SECONDS", 60.0, minimum=5.0, maximum=600.0
)
LLM_CLIENT_MAX_RETRIES = env_int(
    "GPA_LLM_MAX_RETRIES", 1, minimum=0, maximum=5
)

# UI Parser
UI_PARSER_BACKEND = os.environ.get("GPA_UI_PARSER_BACKEND", "builtin")
YOLO_CONF = 0.05
YOLO_IOU = 0.7
YOLO_IMGSZ = 1280
KNN_K = 8
UI_PARSE_CACHE_SIZE = env_int("GPA_UI_PARSE_CACHE_SIZE", 16, minimum=0, maximum=1024)

# SMC
SMC_N_PARTICLES = 500
SMC_ESS_TARGET = 0.6
SMC_MAX_STEPS = 30
SMC_EARLY_STOP_CONF = 0.85
SMC_TOP_K_CANDIDATES = 5

# Locality weight
SIGMA_LOC_MIN = 30.0   # px
SIGMA_LOC_MAX = 2000.0

# Confidence / readiness
DIRECT_MATCH_MIN_SCORE = 0.9
DIRECT_MATCH_MAX_ENTROPY = 0.5
READINESS_THRESHOLD = 0.5
MAX_RETRIES = 5
MAX_RETRIES_LIMIT = 50
RETRY_SLEEP = 1.0       # seconds

# Scale prior
SCALE_PRIOR_WEIGHT = 0.5    # weight for identity component
SCALE_SIGMA = 0.2           # log-space std

# Spatial confidence
SPATIAL_RBASE = 50.0        # px
SPATIAL_ALPHA = 0.2

# Precheck
PRECHECK_LOOKAHEAD = 2      # steps ahead to precompute
PRECHECK_MIN_CONF = 0.7     # min confidence to use cached result

# Screenshot
SCREENSHOT_SCALE = 2.0      # retina display scale factor
