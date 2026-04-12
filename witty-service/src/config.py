from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    auth_token: str


def get_settings() -> Settings:
    return Settings(auth_token=os.getenv("AUTH_TOKEN", ""))
