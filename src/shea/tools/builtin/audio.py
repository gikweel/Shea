from __future__ import annotations

from shea.audio.ports import AudioSynthesizer
from shea.contracts.models import ToolRequest, ToolResponse
from shea.tools.registry import ToolRegistry


def register_audio_tools(registry: ToolRegistry, synthesizer: AudioSynthesizer) -> None:
    def handle_speak(request: ToolRequest) -> ToolResponse:
        text = request.arguments.get("text")
        if not isinstance(text, str) or not text.strip():
            return ToolResponse(success=False, error="Argument 'text' must be a string.", data=None)

        try:
            synthesizer.synthesize(text)
            return ToolResponse(success=True, data="Speech synthesized successfully.", error=None)
        except Exception as exc:
            return ToolResponse(success=False, error=str(exc), data=None)

    registry.register_with_schema(
        name="audio.speak",
        capabilities=frozenset({"audio.speak"}),
        handler=handle_speak,
        description="Speak text out loud to the user using text-to-speech.",
        argument_schema={"text": {"type": "string", "required": True, "min_length": 1}},
    )
