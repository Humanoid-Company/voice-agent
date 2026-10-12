"""Function schemas the GPT-Live Responses backend may call. Add new tools here and register their
handlers on the ToolExecutor (server/web_users.py, assistant.py)."""
from __future__ import annotations

from voice.options import LANGUAGE_OPTIONS, LIVE_VOICE_OPTIONS, SPEED_OPTIONS, STYLE_OPTIONS, VOICE_PERSONAS


def _fn(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    params: dict = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        params["required"] = required
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": params,
    }


LIVE_BACKEND_TOOLS: list[dict] = [
    _fn(
        "set_assistant_name",
        "Change the assistant's spoken name.",
        {"name": {"type": "string"}},
        required=["name"],
    ),
    _fn(
        "change_voice",
        "Change the voice (timbre). The conversation continues in the new voice with its full "
        "memory after a short pause. «чоловічий / жіночий» → pick one of that gender; «інший» → a "
        "different one; «спокійніший» → a calm one (Віллоу, Стоун) or use set_voice_style. Voices: "
        + ", ".join(f"{p.voice} ({p.label}, {'жіночий' if p.feminine else 'чоловічий'})" for p in VOICE_PERSONAS.values())
        + ".",
        {"voice": {"type": "string", "enum": list(LIVE_VOICE_OPTIONS)}},
        required=["voice"],
    ),
    _fn(
        "set_voice_style",
        "Change how the current voice speaks, applied right away: speed (slow/normal/fast) and "
        "style (calm/normal/expressive). For «говори повільніше / швидше / спокійніше / "
        "емоційніше». Omitted fields keep their value.",
        {
            "speed": {"type": "string", "enum": list(SPEED_OPTIONS)},
            "style": {"type": "string", "enum": list(STYLE_OPTIONS)},
        },
    ),
    _fn(
        "change_language",
        "Change conversation language immediately.",
        {"language": {"type": "string", "enum": list(LANGUAGE_OPTIONS)}},
        required=["language"],
    ),
    _fn(
        "end_conversation",
        "End the conversation and return to sleep. Call silently.",
        {},
    ),
]
