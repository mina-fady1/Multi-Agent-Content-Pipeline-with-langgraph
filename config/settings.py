import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env file from project root
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

class Settings:
    BASE_DIR: Path = BASE_DIR
    
    # API Keys
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", "")
    TAVILY_API_KEY: str = os.getenv("TAVILY_API_KEY", "")
    
    # Provider & Model Defaults
    # Auto-detect: if GEMINI_API_KEY is present, default to gemini, else ollama
    DEFAULT_PROVIDER: str = os.getenv(
        "LLM_PROVIDER", 
        "gemini" if (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")) else "ollama"
    )
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "qwen3.5:4b-q4_K_M")
    OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

    # File Paths
    KB_FILE: Path = BASE_DIR / "KB.json"
    SOURCES_FILE: Path = BASE_DIR / "sources.json"
    BRAND_VOICE_FILE: Path = BASE_DIR / "brand-voice.md"
    MARKETING_RULES_FILE: Path = BASE_DIR / "marketing-rules.md"
    BANNED_WORDS_FILE: Path = BASE_DIR / "banned-words.txt"
    RUBRIC_FILE: Path = BASE_DIR / "rubric.md"
    DEFAULT_PLAN_FILE: Path = BASE_DIR / "plan.json"

    # Standard paid text USD per million, verified 2026-10-21.
    # https://ai.google.dev/gemini-api/docs/pricing
    GEMINI_TEXT_PRICING = {
        "gemini-2.5-flash": (0.30, 2.50),
        "gemini-2.5-flash-lite": (0.10, 0.40),
    }

    @classmethod
    def get_kb_path(cls) -> Path:
        if cls.KB_FILE.exists():
            return cls.KB_FILE
        alt = cls.BASE_DIR / "kb.json"
        if alt.exists():
            return alt
        return cls.KB_FILE

settings = Settings()
