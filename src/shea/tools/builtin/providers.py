from __future__ import annotations

from shea.audio.adapters import get_default_audio_synthesizer
from shea.audio.ports import AudioSynthesizer
from shea.security.filesystem_policy import FilesystemPolicy
from shea.security.network_policy import NetworkPolicy
from shea.tools.builtin.audio import register_audio_tools
from shea.tools.builtin.filesystem import register_filesystem_tools
from shea.tools.builtin.http_fetch import register_http_fetch_tool
from shea.tools.registry import ToolRegistry
from shea.verification.verifier import VerifierRegistry


class BuiltinFilesystemProvider:
    """In-tree filesystem.read / filesystem.write provider."""

    name = "shea.builtin.filesystem"

    def __init__(self, policy: FilesystemPolicy) -> None:
        self._policy = policy

    def register(
        self,
        registry: ToolRegistry,
        *,
        verifier_registry: VerifierRegistry | None = None,
    ) -> None:
        register_filesystem_tools(
            registry,
            self._policy,
            verifier_registry=verifier_registry,
        )


class BuiltinHttpFetchProvider:
    """In-tree http.fetch provider (optional)."""

    name = "shea.builtin.http_fetch"

    def __init__(self, policy: NetworkPolicy) -> None:
        self._policy = policy

    def register(
        self,
        registry: ToolRegistry,
        *,
        verifier_registry: VerifierRegistry | None = None,
    ) -> None:
        # verifier_registry unused — fetch has no independent verifier yet
        del verifier_registry
        register_http_fetch_tool(registry, self._policy)


class BuiltinAudioProvider:
    """In-tree audio.speak provider."""

    name = "shea.builtin.audio"

    def __init__(self, synthesizer: AudioSynthesizer | None = None) -> None:
        self._synthesizer = (
            synthesizer if synthesizer is not None else get_default_audio_synthesizer()
        )

    def register(
        self,
        registry: ToolRegistry,
        *,
        verifier_registry: VerifierRegistry | None = None,
    ) -> None:
        register_audio_tools(registry, self._synthesizer)
