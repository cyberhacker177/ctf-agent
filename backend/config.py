"""Pydantic Settings — credentials from .env file + environment variables."""

from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    platform: str = "ctfd"
    # CTFd
    ctfd_url: str = "http://localhost:8000"
    ctfd_user: str = "admin"
    ctfd_pass: str = "admin"
    ctfd_token: str = ""

    htb_event_id: int | None = None
    htb_token: str = ""
    htb_cookie: str = ""
    htb_user: str = ""
    htb_pass: str = ""
    htb_mode: str = "auto"
    htb_login_path: str = "/auth/login"
    htb_login_url: str = "https://account.hackthebox.com/api/v1/auth/login"
    htb_captcha_token: str = ""
    challenge_policy_file: str = "challenge-policy.yml"

    # API Keys
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    # Google tooling commonly calls this GOOGLE_API_KEY; accept both spellings.
    gemini_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    )

    # Provider-specific (optional, for Bedrock/Azure/Zen fallback)
    aws_region: str = "us-east-1"
    aws_bearer_token: str = ""
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    opencode_zen_api_key: str = ""

    # Infra
    sandbox_image: str = "ctf-sandbox"
    max_concurrent_challenges: int = 10
    max_attempts_per_challenge: int = 3
    container_memory_limit: str = "16g"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}
