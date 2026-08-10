import os
from typing import Literal
from uuid import UUID

from pydantic import Field, PostgresDsn, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── App ──
    env: str = Field(default="development")
    log_level: str = Field(default="INFO")
    app_workers: int = Field(default=1)
    cors_allowed_origins: list[str] = Field(default=["http://localhost:8000"])

    # ── LLM ──
    llm_provider_source: str = Field(default="env")
    llm_provider: str = Field(default="deepseek")
    openai_api_key: str = Field(...)
    openai_base_url: str = Field(...)
    openai_default_model: str = Field(default="deepseek-chat")
    openai_fast_model: str = Field(default="deepseek-chat")

    # ── Embedding（Ollama / Qwen） ──
    embedding_enabled: bool = Field(default=True)
    embedding_api_key: str = Field(default="")
    embedding_base_url: str = Field(default="http://localhost:11434")
    embedding_model: str = Field(default="qwen3-embedding:4b")
    embedding_dim: int = Field(default=1024)

    # ── Rerank（Ollama / Qwen3 Reranker） ──
    rerank_enabled: bool = Field(default=True)
    rerank_api_key: str = Field(default="")
    rerank_base_url: str = Field(default="http://localhost:11434")
    rerank_model: str = Field(default="dengcao/Qwen3-Reranker-4B:Q4_K_M")
    rerank_max_concurrency: int = Field(default=1, ge=1, le=16)

    # ── PostgreSQL ──
    postgres_dsn: PostgresDsn = Field(...)

    # ── Neo4j ──
    neo4j_uri: str = Field(default="bolt://localhost:7687")
    neo4j_username: str = Field(default="neo4j")
    neo4j_password: str = Field(...)
    neo4j_database: str = Field(default="neo4j")

    # ── Redis ──
    redis_url: str = Field(default="redis://localhost:6379/0")

    # ── Milvus ──
    milvus_uri: str = Field(default="http://localhost:19530")
    milvus_user: str = Field(default="root")
    milvus_password: str = Field(default="")
    milvus_db_name: str = Field(default="lightrag")

    # ── Game DB ──
    game_db_dsn: PostgresDsn | None = Field(default=None)
    game_data_source: str = Field(default="")
    robotgateway_base_url: str | None = Field(default=None)
    robotgateway_snapshot_api_key: str | None = Field(default=None)
    robotgateway_snapshot_timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)

    # ── RAG ──
    rag_default_strategy: str = Field(default="hybrid")
    rag_working_dir: str = Field(default="./rag_storage")
    gather_context_enable_dynamic_rag: bool = Field(default=False)
    lightrag_llm_max_async: int = Field(default=1, ge=1)
    lightrag_chunk_token_size: int = Field(default=512, ge=1)
    lightrag_chunk_overlap_token_size: int = Field(default=256, ge=0)
    lightrag_vector_cosine_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    lightrag_chunk_top_k: int = Field(default=50, ge=1, le=200)
    rag_exact_match_enabled: bool = Field(default=True)
    rag_exact_match_top_k: int = Field(default=5, ge=1, le=20)

    # ── 调度 ──
    max_concurrent_analyses: int = Field(default=20, ge=1, le=100)
    offline_trigger_minutes: int = Field(default=5, ge=1)

    # ── RobotGateway Callback ──
    robotgateway_callback_url: str | None = Field(default=None)
    robotgateway_callback_timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)
    robotgateway_callback_api_key: str | None = Field(default=None)

    # ── Auto Chat ──
    auto_chat_base_url: str | None = Field(default=None)
    auto_chat_timeout_seconds: float = Field(default=45.0, gt=0, le=60.0)
    auto_chat_deadline_safety_seconds: float = Field(default=10.0, ge=0, le=45.0)

    # ── LLM Gateway shared / v1 runtime ──
    llm_gateway_v1_enabled: bool = Field(default=False)
    llm_gateway_v2_enabled: bool = Field(default=False)
    llm_gateway_app_secrets: dict[str, str] = Field(default_factory=dict)
    llm_gateway_app_gateways: dict[str, list[str]] = Field(default_factory=dict)
    llm_gateway_app_tenants: dict[str, str] = Field(default_factory=dict)
    llm_gateway_timestamp_tolerance_ms: int = Field(default=300_000, ge=1)
    llm_gateway_idempotency_ttl_seconds: int = Field(default=86_400, ge=60)
    llm_gateway_decision_url: str | None = Field(default=None)
    llm_gateway_decision_app_id: str | None = Field(default=None)
    llm_gateway_decision_app_secret: str | None = Field(default=None)
    llm_gateway_decision_timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)
    llm_gateway_decision_max_retries: int = Field(default=1, ge=0, le=5)
    llm_gateway_simple_chat_timeout_seconds: float = Field(default=3.0, gt=0, le=3.0)
    llm_gateway_control_url: str | None = Field(default=None)
    llm_gateway_control_app_id: str | None = Field(default=None)
    llm_gateway_control_app_secret: str | None = Field(default=None)
    llm_gateway_control_timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)
    llm_gateway_control_max_retries: int = Field(default=1, ge=0, le=5)
    llm_gateway_hosted_chat_queue_size: int = Field(default=100, ge=1, le=10_000)
    llm_gateway_hosted_chat_state_ttl_seconds: int = Field(default=300, ge=30, le=86_400)
    llm_gateway_hosted_chat_max_state_entries: int = Field(default=10_000, ge=1, le=1_000_000)
    llm_gateway_event_worker_enabled: bool = Field(default=True)
    llm_gateway_event_stream_key: str = Field(default="llm-gateway:events")
    llm_gateway_event_consumer_group: str = Field(default="myagent2")
    llm_gateway_event_worker_block_ms: int = Field(default=1000, ge=100, le=30_000)
    llm_gateway_event_retry_idle_ms: int = Field(default=30_000, ge=1000, le=3_600_000)

    # ── LLM Gateway v2 runtime ──
    llm_gateway_v2_max_event_batch_size: int = Field(default=100, ge=1, le=1_000)
    llm_gateway_v2_max_decision_ttl_ms: int = Field(default=30_000, ge=1, le=3_600_000)
    llm_gateway_v2_event_max_attempts: int = Field(default=5, ge=1, le=100)
    llm_gateway_v2_decision_max_attempts: int = Field(default=5, ge=1, le=100)
    llm_gateway_v2_retry_base_ms: int = Field(default=1_000, ge=1, le=3_600_000)
    llm_gateway_v2_retry_max_ms: int = Field(default=300_000, ge=1, le=3_600_000)
    llm_gateway_v2_claim_ttl_ms: int = Field(default=30_000, ge=1, le=3_600_000)
    llm_gateway_v2_agent_timeout_seconds: float = Field(default=60.0, gt=0, le=300.0)
    llm_gateway_v2_rag_mode: Literal["naive", "hybrid", "mix"] = "naive"
    llm_gateway_v2_rag_top_k: int = Field(default=10, ge=1, le=200)
    llm_gateway_v2_rag_chunk_top_k: int = Field(default=10, ge=1, le=200)
    llm_gateway_v2_rag_max_entity_tokens: int = Field(default=1_500, ge=256, le=30_000)
    llm_gateway_v2_rag_max_relation_tokens: int = Field(default=2_500, ge=256, le=30_000)
    llm_gateway_v2_rag_max_total_tokens: int = Field(default=6_000, ge=512, le=30_000)
    llm_gateway_v2_rag_context_max_tokens: int = Field(default=6_000, ge=256, le=30_000)
    llm_gateway_v2_poll_ms: int = Field(default=250, ge=1, le=60_000)
    llm_gateway_v2_event_max_parallelism: int = Field(default=4, ge=1, le=64)
    llm_gateway_v2_decision_max_parallelism: int = Field(default=4, ge=1, le=64)
    llm_gateway_v2_shutdown_grace_seconds: int = Field(default=10, ge=1, le=300)
    llm_gateway_v2_readiness_timeout_seconds: int = Field(default=3, ge=1, le=60)
    llm_gateway_v2_readiness_cache_seconds: int = Field(default=5, ge=1, le=300)

    # ── Token 配额 ──
    default_monthly_tokens: int = Field(default=40_000_000)
    quota_warning_threshold: float = Field(default=0.8, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_llm_gateway_v2(self) -> "Settings":
        if self.llm_gateway_v2_retry_base_ms > self.llm_gateway_v2_retry_max_ms:
            raise ValueError("llm_gateway_v2_retry_base_ms must not exceed llm_gateway_v2_retry_max_ms")

        if not self.llm_gateway_v2_enabled:
            return self

        v2_app_ids = set(self.llm_gateway_app_gateways)
        if not v2_app_ids:
            raise ValueError("llm_gateway_app_gateways must define at least one v2 AppId")

        for app_id, gateway_ids in self.llm_gateway_app_gateways.items():
            if not app_id.strip():
                raise ValueError("llm_gateway_app_gateways contains an empty v2 AppId")

            inbound_secret = self.llm_gateway_app_secrets.get(app_id)
            if inbound_secret is None or not inbound_secret.strip():
                raise ValueError(f"v2 AppId {app_id!r} must have a non-empty secret")
            if not gateway_ids:
                raise ValueError(f"v2 AppId {app_id!r} must have a non-empty gateway allowlist")

            for gateway_id in gateway_ids:
                if not gateway_id.strip():
                    raise ValueError(f"v2 AppId {app_id!r} contains an empty gatewayId")
                tenant_id = self.llm_gateway_app_tenants.get(gateway_id)
                if tenant_id is None or not tenant_id.strip():
                    raise ValueError(f"v2 gatewayId {gateway_id!r} must have a tenant mapping")
                try:
                    UUID(tenant_id)
                except (ValueError, AttributeError) as exc:
                    raise ValueError(f"v2 gatewayId {gateway_id!r} tenant must be a valid UUID") from exc

        decision_url = self.llm_gateway_decision_url
        if decision_url is None or not decision_url.strip():
            raise ValueError("llm_gateway_decision_url must be non-empty when v2 is enabled")

        decision_app_id = self.llm_gateway_decision_app_id
        if decision_app_id is None or not decision_app_id.strip():
            raise ValueError("llm_gateway_decision_app_id must be non-empty when v2 is enabled")
        if decision_app_id in v2_app_ids:
            raise ValueError("llm_gateway_decision_app_id must differ from every v2 inbound AppId")

        decision_secret = self.llm_gateway_decision_app_secret
        if decision_secret is None or not decision_secret.strip():
            raise ValueError("llm_gateway_decision_app_secret must be non-empty when v2 is enabled")
        inbound_secrets = {self.llm_gateway_app_secrets[app_id] for app_id in v2_app_ids}
        if decision_secret in inbound_secrets:
            raise ValueError("llm_gateway_decision_app_secret must differ from every v2 inbound secret")

        return self


def load_settings() -> Settings:
    env = os.environ.get("ENV", "development").strip().lower()
    if env == "test":
        return Settings(_env_file=None)
    return Settings(_env_file=".env")


settings = load_settings()
