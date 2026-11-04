import os
from pathlib import Path
from dotenv import load_dotenv, dotenv_values
load_dotenv(Path(__file__).resolve().parents[1] / '.env')
VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN", "")
APP_SECRET = os.getenv("META_APP_SECRET", "")
ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID", "")
GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v23.0")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "")
GEMINI_STT_MODEL = os.getenv("GEMINI_STT_MODEL", "")
BACKEND_DIR = Path(__file__).resolve().parents[1]


def resolve_db_path(value):
    path = Path(value).expanduser()
    return str((path if path.is_absolute() else BACKEND_DIR / path).resolve())


# Relative paths are always backend-relative, never process-CWD-relative.
DB_PATH = resolve_db_path(os.getenv("DB_PATH", "nouba.db"))
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",")


def diagnostics():
    """Compare effective process settings with the file without exposing a key."""
    values = dotenv_values(Path(__file__).resolve().parents[1] / '.env')
    return {
        'model': GEMINI_MODEL,
        'key_configured': bool(GEMINI_API_KEY),
        'model_matches_env': GEMINI_MODEL == values.get('GEMINI_MODEL', ''),
        'key_matches_env': GEMINI_API_KEY == values.get('GEMINI_API_KEY', ''),
    }

# Local text extraction and remote speech recognition are independent.
LLM_PROVIDER = os.getenv('LLM_PROVIDER', 'ollama').strip().lower()
LLM_FALLBACK_PROVIDER = os.getenv('LLM_FALLBACK_PROVIDER', '').strip().lower()
OLLAMA_BASE_URL = os.getenv('OLLAMA_BASE_URL', 'http://127.0.0.1:11434').rstrip('/')
OLLAMA_MODEL = os.getenv('OLLAMA_MODEL', 'qwen3:1.7b')
OLLAMA_TIMEOUT_SECONDS = float(os.getenv('OLLAMA_TIMEOUT_SECONDS', '35'))
OLLAMA_NUM_CTX = int(os.getenv('OLLAMA_NUM_CTX', '1024'))
OLLAMA_NUM_PREDICT = int(os.getenv('OLLAMA_NUM_PREDICT', '180'))
OLLAMA_KEEP_ALIVE = os.getenv('OLLAMA_KEEP_ALIVE', '2m')
STT_PROVIDER = os.getenv('STT_PROVIDER', 'gemini').strip().lower()

# v2 uses hosted understanding when configured, with a strict per-turn budget.
CONVERSATION_CORE = os.getenv('CONVERSATION_CORE', 'v2').strip().lower()
CONVERSATION_PROVIDER = os.getenv('CONVERSATION_PROVIDER', 'auto').strip().lower()
CONVERSATION_MODEL = os.getenv('CONVERSATION_MODEL', '').strip()
CONVERSATION_GEMINI_MODEL = os.getenv('CONVERSATION_GEMINI_MODEL', 'gemini-3.6-flash').strip()
CONVERSATION_THINKING_LEVEL = os.getenv('CONVERSATION_THINKING_LEVEL', 'minimal').strip()
CONVERSATION_TIMEOUT_SECONDS = float(os.getenv('CONVERSATION_TIMEOUT_SECONDS', '6'))
