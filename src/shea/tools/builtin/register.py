from __future__ import annotations

from shea.security.filesystem_policy import FilesystemPolicy
from shea.security.network_policy import NetworkPolicy
from shea.tools.builtin.providers import (
    BuiltinAudioProvider,
    BuiltinFilesystemProvider,
    BuiltinHttpFetchProvider,
)
from shea.tools.provider import load_tools
from shea.tools.registry import ToolRegistry
from shea.verification.verifier import VerifierRegistry


def register_builtin_tools(
    registry: ToolRegistry,
    *,
    filesystem_policy: FilesystemPolicy,
    network_policy: NetworkPolicy | None = None,
    verifier_registry: VerifierRegistry | None = None,
    include_http_fetch: bool = False,
    include_audio: bool = False,
) -> None:
    """Register Phase 9 builtin tools via ToolProvider."""
    providers: list[BuiltinFilesystemProvider | BuiltinHttpFetchProvider | BuiltinAudioProvider] = [
        BuiltinFilesystemProvider(filesystem_policy),
    ]
    if include_http_fetch:
        if network_policy is None:
            raise ValueError("network_policy is required when include_http_fetch=True")
        providers.append(BuiltinHttpFetchProvider(network_policy))
    if include_audio:
        providers.append(BuiltinAudioProvider())

    load_tools(registry, providers, verifier_registry=verifier_registry)

    # Register the system.reply tool for conversational output
    from shea.tools.builtin.interaction import register_interaction_tools

    register_interaction_tools(registry)

    # Register the system.browser_open tool for opening URLs
    from shea.tools.builtin.browser import register_browser_tools

    register_browser_tools(registry)
