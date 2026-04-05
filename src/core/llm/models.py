"""LLM Provider 数据模型。

用于负载均衡器的内部传递和 Admin API 的 CRUD 操作。
"""

from datetime import datetime

from pydantic import BaseModel, Field


class LLMProviderConfig(BaseModel):
    """内部使用的 provider 配置（含 api_key），供 balancer 使用。"""

    id: str
    name: str
    provider: str
    model: str
    api_key: str
    base_url: str
    weight: int = Field(gt=0)
    model_type: str = "default"


class LLMProviderCreate(BaseModel):
    """创建 provider 请求体。"""

    name: str = Field(min_length=1, max_length=100)
    provider: str = Field(min_length=1, max_length=50)
    model: str = Field(min_length=1, max_length=100)
    api_key: str = Field(min_length=1, max_length=500)
    base_url: str = Field(min_length=1, max_length=500)
    weight: int = Field(default=1, gt=0)
    model_type: str = Field(default="default", pattern=r"^(default|fast)$")


class LLMProviderUpdate(BaseModel):
    """更新 provider 请求体（全部可选）。"""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    api_key: str | None = Field(default=None, min_length=1, max_length=500)
    base_url: str | None = Field(default=None, min_length=1, max_length=500)
    weight: int | None = Field(default=None, gt=0)
    is_active: bool | None = None
    model_type: str | None = Field(default=None, pattern=r"^(default|fast)$")


class LLMProviderResponse(BaseModel):
    """API 响应（隐藏 api_key）。"""

    id: str
    name: str
    provider: str
    model: str
    base_url: str
    weight: int
    is_active: bool
    model_type: str
    created_at: datetime
    updated_at: datetime
