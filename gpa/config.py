"""GPA configuration constants."""
from pathlib import Path
import os

# Project root
PROJECT_ROOT = Path(__file__).parent.parent
STORAGE_DIR = PROJECT_ROOT.parent / "storage"
WORKFLOWS_DIR = STORAGE_DIR / "workflows"
MODELS_CACHE_DIR = Path.home() / ".cache" / "gpa" / "models"

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

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

# UI Parser
UI_PARSER_BACKEND = os.environ.get("GPA_UI_PARSER_BACKEND", "builtin")
YOLO_CONF = 0.05
YOLO_IOU = 0.7
YOLO_IMGSZ = 1280
KNN_K = 8
UI_PARSE_CACHE_SIZE = int(os.environ.get("GPA_UI_PARSE_CACHE_SIZE", "16"))

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
