import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / '.env')
VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN", "")
APP_SECRET = os.getenv("META_APP_SECRET", "")
ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID", "")
GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v23.0")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "")
GEMINI_STT_MODEL = os.getenv("GEMINI_STT_MODEL", "")
DB_PATH = os.getenv("DB_PATH", "nouba.db")
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",")
