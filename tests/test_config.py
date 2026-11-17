import pytest

from app.config import Settings


@pytest.mark.parametrize("variable", ["AI_TRANSLATION_MODEL", "ALEMAI_TRANSLATION_MODEL"])
def test_alemai_settings_load_dotenv(tmp_path, monkeypatch, variable):
    for name in ("AI_TRANSLATION_MODEL", "ALEMAI_TRANSLATION_MODEL", "AI_PROVIDER", "ALEMAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    config = tmp_path / ".env"
    config.write_text(f"AI_PROVIDER=alemai\n{variable}=gemma-4-test\nALEMAI_API_KEY=test-only\n")
    loaded = Settings(_env_file=config)
    assert loaded.alemai_enabled
    assert loaded.alemai_translation_model == "gemma-4-test"
    assert loaded.alemai_api_key.get_secret_value() == "test-only"
    assert "test-only" not in repr(loaded)


@pytest.mark.parametrize("suffix", ["/v1", "/v1/", "/v1/chat/completions", "/v1/chat/completions/"])
def test_alemai_api_root_accepts_full_endpoint(suffix):
    config = Settings(alemai_base_url="https://example.com" + suffix, _env_file=None)
    assert config.alemai_api_root == "https://example.com/v1"
