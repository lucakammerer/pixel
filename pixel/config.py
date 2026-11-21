import os
from dataclasses import dataclass
from functools import cache

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    api_base: str
    api_key: str
    model: str
    db_path: str
    dry_run: bool
    poll_seconds: float
    quote_target_s: int
    quote_hard_s: int


@cache
def settings() -> Settings:
    load_dotenv()
    env = os.environ
    return Settings(
        api_base=env.get("PIXEL_API_BASE", "https://api.start-hack.5u.ai"),
        api_key=env.get("PIXEL_API_KEY", ""),
        model=env.get("PIXEL_MODEL", ""),
        db_path=env.get("PIXEL_DB", "data/pixel.sqlite3"),
        dry_run=env.get("PIXEL_DRY_RUN", "1") != "0",
        poll_seconds=float(env.get("PIXEL_POLL_SECONDS", "1.0")),
        quote_target_s=int(env.get("PIXEL_QUOTE_TARGET_S", "100")),
        quote_hard_s=int(env.get("PIXEL_QUOTE_HARD_S", "315")),
    )
