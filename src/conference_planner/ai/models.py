from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ToolDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    description: str = ""
    input_schema: dict[str, Any]


class ToolCall(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str | None = None
    name: str = Field(min_length=1)
    arguments: dict[str, Any]


class ChatMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_name: str | None = None
    tool_call_id: str | None = None


class ChatResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    prompt_tokens: int | None = None
    cached_prompt_tokens: int | None = None
    output_tokens: int | None = None
    model_name: str | None = None


class AvailableModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    supports_tools: bool | None = None
