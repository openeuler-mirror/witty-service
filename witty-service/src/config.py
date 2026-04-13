from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    auth_token: str
    cors_origins: list[str]
    cors_credentials: bool
    cors_methods: list[str]
    cors_headers: list[str]


def get_settings() -> Settings:
    return Settings(
        auth_token=os.getenv("AUTH_TOKEN", "test-token"),
        cors_origins=["*"],
        cors_credentials=True,
        cors_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
        cors_headers=["*"],
    )
