"""分发模块的请求/响应 schema。"""

from typing import Any

from pydantic import BaseModel, Field


class EntryForm(BaseModel):
    screening_id: str = Field(min_length=1, max_length=64)
    film_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=120)
    starts_at: str = Field(min_length=1, max_length=40)
    ends_at: str = Field(min_length=1, max_length=40)


class SignForm(BaseModel):
    territory: str = Field(min_length=1, max_length=40)
    medium: str = Field(min_length=1, max_length=40)
    entries: list[EntryForm]


class ConfirmForm(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=120)


class DeliveryAttemptForm(BaseModel):
    error: str | None = Field(default=None, max_length=500)
