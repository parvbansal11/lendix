"""Environment configuration. Demo identities are synthetic, not authentication."""
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMO_USERS = [
    {"id": "risk_head", "name": "Mira Vaid", "role": "risk_head"},
    {"id": "ops", "name": "Neel Saran", "role": "ops"},
    {"id": "borrower", "name": "Synthetic borrower", "role": "borrower"},
]


@dataclass
class Settings:
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""), repr=False)
    model_fast: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL_FAST", ""))
    model_deep: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL_DEEP", ""))
    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", "var/lendix.db"))
    output_dir: str = field(default_factory=lambda: os.getenv("OUTPUT_DIR", "out"))
    policy_path: str = field(default_factory=lambda: os.getenv("CREDIT_POLICY_PATH", str(ROOT / "config/credit_policy.yaml")))
    env: str = field(default_factory=lambda: os.getenv("ENV", "production"))
    public_base_url: str = field(default_factory=lambda: os.getenv("PUBLIC_BASE_URL", "").rstrip("/"))
    max_llm_calls_per_day: int = field(default_factory=lambda: int(os.getenv("MAX_LLM_CALLS_PER_DAY", "500")))
    cooling_off_days: int = field(default_factory=lambda: int(os.getenv("COOLING_OFF_DAYS", "1")))
    demo_skip_cooling: bool = field(default_factory=lambda: os.getenv("DEMO_SKIP_COOLING_OFF", "0") == "1")
    nonilion_allow_unverified: bool = field(default_factory=lambda: os.getenv("NONILION_DEV_ALLOW_UNVERIFIED", "0") == "1")
    pairing_code: str = field(default_factory=lambda: os.getenv("NONILION_PAIRING_CODE", ""), repr=False)
    bot_token: str = field(default_factory=lambda: os.getenv("NONILION_BOT_TOKEN", ""), repr=False)

    def validate(self):
        if not self.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is required; set it in the environment before starting Lendix.")
        if not self.model_fast or not self.model_deep:
            raise RuntimeError("GEMINI_MODEL_FAST and GEMINI_MODEL_DEEP must be set to available model IDs.")
        if self.max_llm_calls_per_day < 1 or self.cooling_off_days < 1:
            raise RuntimeError("MAX_LLM_CALLS_PER_DAY and COOLING_OFF_DAYS must be positive.")
