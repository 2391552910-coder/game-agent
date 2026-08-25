from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_production_compose_contains_application_lifecycle_services() -> None:
    compose = (PROJECT_ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")

    for service_name in (
        "myagent-api:",
        "myagent-migrate:",
        "myagent-rag-import:",
        "ollama:",
        "ollama-init:",
        "postgres:",
        "redis:",
        "neo4j:",
        "milvus:",
        "etcd:",
        "minio:",
    ):
        assert f"  {service_name}" in compose

    assert "service_completed_successfully" in compose
    assert "qwen3-embedding:4b" not in compose


def test_production_dockerfile_uses_frozen_lockfile_install() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM python:3.12-bookworm AS builder" in dockerfile
    assert "apt-get install --no-install-recommends -y build-essential libpq-dev" not in dockerfile
    assert "COPY --from=ghcr.io/astral-sh/uv:0.10.11 /uv /uvx /bin/" in dockerfile
    assert "0.10.11-python3.12-bookworm-slim" not in dockerfile
    assert "COPY resources ./resources" in dockerfile
    assert " /app/resources /app/resources" in dockerfile
    assert "uv sync --frozen" in dockerfile
    assert "uv.lock" in dockerfile
    assert "src.api.main:app" in dockerfile


def test_container_initializers_are_non_interactive_and_configurable() -> None:
    ollama_init = (PROJECT_ROOT / "scripts/docker/ollama-init.sh").read_text(encoding="utf-8")
    rag_import = (PROJECT_ROOT / "scripts/docker/rag-import.sh").read_text(encoding="utf-8")

    assert "OLLAMA_MODELS" in ollama_init
    assert "/api/pull" in ollama_init
    assert "RAG_IMPORT_INPUT" in rag_import
    assert "import_game_scene_rag.py" in rag_import


def test_production_env_does_not_set_optional_postgres_dsn_to_empty() -> None:
    env_template = (PROJECT_ROOT / ".env.prod.example").read_text(encoding="utf-8")

    assert "GAME_DB_DSN=" not in env_template
