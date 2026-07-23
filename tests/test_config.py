from backend.config import Settings


def test_google_api_key_alias_is_accepted():
    settings = Settings(_env_file=None, GOOGLE_API_KEY="google-key")

    assert settings.gemini_api_key == "google-key"
