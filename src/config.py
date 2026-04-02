from pydantic import Field, PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
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
    llm_provider: str = Field(default="deepseek")
    openai_api_key: str = Field(...)
    openai_base_url: str = Field(...)
    openai_default_model: str = Field(default="deepseek-chat")
    openai_fast_model: str = Field(default="deepseek-chat")

    # ── Embedding（DashScope / Qwen） ──
    embedding_api_key: str = Field(...)
    embedding_base_url: str = Field(default="https://dashscope.aliyuncs.com/compatible-mode/v1")
    embedding_model: str = Field(default="text-embedding-v4")
    embedding_dim: int = Field(default=1024)

    # ── Rerank（DashScope / Qwen gte-rerank-v2） ──
    rerank_api_key: str = Field(...)
    rerank_model: str = Field(default="gte-rerank-v2")

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

    # ── RAG ──
    rag_default_strategy: str = Field(default="hybrid")
    rag_working_dir: str = Field(default="./rag_storage")

    # ── 调度 ──
    max_concurrent_analyses: int = Field(default=20, ge=1, le=100)
    offline_trigger_minutes: int = Field(default=5, ge=1)

    # ── Token 配额 ──
    default_monthly_tokens: int = Field(default=40_000_000)
    quota_warning_threshold: float = Field(default=0.8, ge=0.0, le=1.0)


settings = Settings()
