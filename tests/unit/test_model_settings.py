from __future__ import annotations

import pytest

from shea.contracts.models import ModelResponse
from shea.model.model_compatible import ModelCompatibleProvider
from shea.model.settings import ModelSettingsService, SwitchableModelProvider


class MemorySecureStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set_secret(self, credential_id: str, secret: str) -> None:
        self.values[credential_id] = secret

    def get_secret(self, credential_id: str) -> str | None:
        return self.values.get(credential_id)

    def delete_secret(self, credential_id: str) -> None:
        self.values.pop(credential_id, None)


class StubProvider:
    def generate(self, prompt: str) -> ModelResponse:
        return ModelResponse(content=prompt)

    def health(self) -> bool:
        return True

    def capabilities(self) -> frozenset[str]:
        return frozenset({"chat"})


def test_saving_model_settings_hides_key_and_applies_provider() -> None:
    secure_store = MemorySecureStore()
    runtime_provider = SwitchableModelProvider(StubProvider())
    settings = ModelSettingsService(secure_store, runtime_provider)

    result = settings.save(
        provider="openai-compatible",
        base_url="https://api.example.com/v1",
        model_name="example-model",
        api_key="personal-api-key",
    )

    assert result.provider == "openai-compatible"
    assert result.base_url == "https://api.example.com/v1"
    assert result.model_name == "example-model"
    assert result.configured is True
    assert result.api_key_configured is True
    assert "personal-api-key" not in str(result)
    active_provider = runtime_provider._provider
    assert isinstance(active_provider, ModelCompatibleProvider)
    assert active_provider.base_url == "https://api.example.com/v1"
    assert active_provider.model == "example-model"


def test_local_ollama_does_not_require_an_api_key() -> None:
    settings = ModelSettingsService(MemorySecureStore(), SwitchableModelProvider(StubProvider()))

    result = settings.save(
        provider="ollama",
        base_url="http://127.0.0.1:11434/v1",
        model_name="llama3.2",
        api_key="",
    )

    assert result.configured is True
    assert result.api_key_configured is False


def test_custom_provider_name_uses_openai_compatible_provider() -> None:
    settings = ModelSettingsService(
        MemorySecureStore(), SwitchableModelProvider(StubProvider())
    )

    result = settings.save(
        provider="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        model_name="vendor/custom-model",
        api_key="personal-api-key",
    )

    assert result.provider == "OpenRouter"
    active_provider = settings._provider._provider
    assert isinstance(active_provider, ModelCompatibleProvider)
    assert active_provider.name == "OpenRouter"
    assert active_provider.model == "vendor/custom-model"


def test_saved_settings_reload_from_secure_store_and_clear_restores_default() -> None:
    secure_store = MemorySecureStore()
    initial_provider = SwitchableModelProvider(StubProvider())
    ModelSettingsService(secure_store, initial_provider).save(
        provider="openai-compatible",
        base_url="https://api.example.com/v1",
        model_name="example-model",
        api_key="personal-api-key",
    )

    restarted_provider = SwitchableModelProvider(StubProvider())
    restarted_service = ModelSettingsService(secure_store, restarted_provider)
    restarted_service.load_saved()

    assert restarted_service.public_settings().api_key_configured is True
    active_provider = restarted_provider._provider
    assert isinstance(active_provider, ModelCompatibleProvider)
    assert active_provider.api_key == "personal-api-key"

    restarted_service.clear()
    assert isinstance(restarted_provider._provider, StubProvider)


def test_remote_provider_requires_https_and_rejects_embedded_credentials() -> None:
    settings = ModelSettingsService(MemorySecureStore(), SwitchableModelProvider(StubProvider()))

    with pytest.raises(ValueError, match="HTTPS"):
        settings.save(
            provider="openai-compatible",
            base_url="http://api.example.com/v1",
            model_name="example-model",
            api_key="personal-api-key",
        )

    with pytest.raises(ValueError, match="credentials"):
        settings.save(
            provider="openai-compatible",
            base_url="https://user:password@api.example.com/v1",
            model_name="example-model",
            api_key="personal-api-key",
        )
