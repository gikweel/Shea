from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import cast
from urllib.parse import urlsplit

from shea.contracts.models import ModelResponse
from shea.credentials.ports import SecureStore
from shea.model.model_compatible import ModelCompatibleProvider
from shea.ports.model_provider import ModelProvider

_SETTINGS_KEY = "model-provider-settings"
_PROVIDER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,63}\Z")


def _is_ollama(provider: str) -> bool:
    return provider.casefold() == "ollama"


class SwitchableModelProvider:
    """Keep the runtime's model-provider reference stable while settings change."""

    def __init__(self, default_provider: ModelProvider) -> None:
        self._default_provider = default_provider
        self._provider = default_provider

    def use(self, provider: ModelProvider) -> None:
        self._provider = provider

    def reset(self) -> None:
        self._provider = self._default_provider

    def generate(self, prompt: str) -> ModelResponse:
        return self._provider.generate(prompt)

    def health(self) -> bool:
        return self._provider.health()

    def capabilities(self) -> frozenset[str]:
        return self._provider.capabilities()


@dataclass(frozen=True)
class _SavedSettings:
    provider: str
    base_url: str
    model_name: str
    api_key: str | None


@dataclass(frozen=True)
class PublicModelSettings:
    provider: str | None
    base_url: str | None
    model_name: str | None
    configured: bool
    api_key_configured: bool


class ModelSettingsService:
    """Persist model settings in the OS secure store and apply them to runtime."""

    def __init__(self, secure_store: SecureStore, provider: SwitchableModelProvider) -> None:
        self._secure_store = secure_store
        self._provider = provider
        self._saved: _SavedSettings | None = None

    def load_saved(self) -> None:
        raw = self._secure_store.get_secret(_SETTINGS_KEY)
        if raw is None:
            return
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("Saved model settings must be an object.")
            data = cast(dict[str, object], data)
            if not all(
                isinstance(data.get(field), str)
                for field in ("provider", "base_url", "model_name")
            ):
                raise ValueError("Saved model settings contain invalid fields.")
            api_key = data.get("api_key")
            if api_key is not None and not isinstance(api_key, str):
                raise ValueError("Saved model settings contain an invalid API key.")
            settings = _SavedSettings(
                provider=cast(str, data["provider"]),
                base_url=cast(str, data["base_url"]),
                model_name=cast(str, data["model_name"]),
                api_key=api_key,
            )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ValueError("Saved model settings are invalid.") from exc
        self._validate_provider(settings.provider)
        self._validate(settings.provider, settings.base_url, settings.model_name)
        if not _is_ollama(settings.provider) and not settings.api_key:
            raise ValueError("Saved provider settings are missing an API key.")
        self._apply(settings)

    def public_settings(self) -> PublicModelSettings:
        settings = self._saved
        return PublicModelSettings(
            provider=settings.provider if settings else None,
            base_url=settings.base_url if settings else None,
            model_name=settings.model_name if settings else None,
            configured=settings is not None,
            api_key_configured=bool(settings and settings.api_key),
        )

    def save(
        self,
        *,
        provider: str,
        base_url: str,
        model_name: str,
        api_key: str,
    ) -> PublicModelSettings:
        provider = self._validate_provider(provider)
        base_url = base_url.strip().rstrip("/")
        model_name = model_name.strip()
        self._validate(provider, base_url, model_name)

        key = api_key.strip() or None
        if _is_ollama(provider):
            key = None
        if (
            key is None
            and self._saved is not None
            and self._saved.provider == provider
            and self._saved.base_url == base_url
        ):
            key = self._saved.api_key
        if not _is_ollama(provider) and not key:
            raise ValueError("Enter an API key for this provider.")

        settings = _SavedSettings(
            provider=provider,
            base_url=base_url,
            model_name=model_name,
            api_key=key,
        )
        self._secure_store.set_secret(
            _SETTINGS_KEY,
            json.dumps(
                {
                    "provider": settings.provider,
                    "base_url": settings.base_url,
                    "model_name": settings.model_name,
                    "api_key": settings.api_key,
                }
            ),
        )
        self._apply(settings)
        return self.public_settings()

    def clear(self) -> PublicModelSettings:
        self._secure_store.delete_secret(_SETTINGS_KEY)
        self._saved = None
        self._provider.reset()
        return self.public_settings()

    def _apply(self, settings: _SavedSettings) -> None:
        provider = ModelCompatibleProvider(
            api_key=settings.api_key or "ollama",
            base_url=settings.base_url,
            model=settings.model_name,
            name=settings.provider,
            use_json_response_format=False,
        )
        self._provider.use(provider)
        self._saved = settings

    @staticmethod
    def _validate_provider(provider: str) -> str:
        provider = provider.strip()
        if not _PROVIDER_NAME.fullmatch(provider):
            raise ValueError(
                "Enter a provider name using letters, numbers, spaces, dots, "
                "underscores, or hyphens."
            )
        return provider

    @staticmethod
    def _validate(provider: str, base_url: str, model_name: str) -> None:
        if not model_name or len(model_name) > 200:
            raise ValueError("Enter a model name no longer than 200 characters.")
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Enter a valid HTTP or HTTPS provider URL.")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("The provider URL must not contain credentials or query values.")
        is_local = parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (_is_ollama(provider) and is_local):
            raise ValueError("Remote provider URLs must use HTTPS.")