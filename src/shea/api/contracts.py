from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """A user submitted chat message or command."""
    message: str = Field(..., description="The textual input from the user.")
    session_id: str | None = Field(None, description="Optional session identifier.")
    profile_id: str | None = Field(None, description="Optional profile identifier.")
    context_overrides: dict[str, Any] = Field(
        default_factory=dict, 
        description="Overrides for the session context."
        )


class ChatResponse(BaseModel):
    """A single atomic text response from the agent (for non-streaming interactions)."""
    text: str = Field(..., description="The response text.")
    session_id: str = Field(..., description="The interaction session ID.")
    task_id: str | None = None
    needs_confirmation: bool = False
    confirmation: dict[str, Any] | None = None
    state: str | None = None


class ErrorResponse(BaseModel):
    """Standardized API error response."""
    detail: str = Field(..., description="Error message.")
    code: str | None = Field(None, description="Optional application-specific error code.")


class MemoryView(BaseModel):
    """A flattened representation of a memory for the GUI."""
    id: str
    content: str
    source: str
    type: str
    created_at: str
    expires_at: str | None
    confidence: float


class CredentialView(BaseModel):
    """Safe credential metadata for the GUI — never includes secret material."""

    id: str
    profile_id: str
    name: str
    description: str | None = None
    allowed_tools: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str | None = None


class CredentialCreateRequest(BaseModel):
    """Create a credential. Secret is written to the secure store only."""

    name: str
    secret: str = Field(..., min_length=8)
    description: str | None = None
    profile_id: str = "default"
    allowed_tools: list[str] = Field(default_factory=lambda: ["*"])


class ModelSettingsView(BaseModel):
    provider: str | None = None
    base_url: str | None = None
    model_name: str | None = None
    configured: bool
    api_key_configured: bool


class ModelSettingsUpdate(BaseModel):
    provider: str = Field(..., min_length=1, max_length=64)
    base_url: str = Field(..., min_length=1, max_length=2048)
    model_name: str = Field(..., min_length=1, max_length=200)
    api_key: str = Field(default="", max_length=4096)


class TaskView(BaseModel):
    id: str
    session_id: str
    request_id: str
    state: str
    plan_id: str | None = None
    created_at: str
    updated_at: str


class ProfileView(BaseModel):
    id: str
    name: str
    preferences: dict[str, Any] = Field(default_factory=dict)
    context_rules: dict[str, str] = Field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None


class ProfileUpsertRequest(BaseModel):
    id: str
    name: str
    preferences: dict[str, Any] = Field(default_factory=dict)
    context_rules: dict[str, str] = Field(default_factory=dict)


class ConfirmRequest(BaseModel):
    task_id: str
    actor: str = "user"
    acknowledge: bool = True