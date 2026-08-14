import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.config import Settings

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
REQUIRED_SETTINGS = {
    "openai_api_key": "test-openai-key",
    "openai_base_url": "https://llm.invalid/v1",
    "postgres_dsn": "postgresql+asyncpg://test:test@postgres.invalid/test",
    "neo4j_password": "test-neo4j-password",
}
VALID_V2_SETTINGS = {
    **REQUIRED_SETTINGS,
    "llm_gateway_v2_enabled": True,
    "llm_gateway_app_secrets": {
        "v2-events": "test-inbound-secret",
        "v1-only": "test-v1-only-secret",
    },
    "llm_gateway_app_gateways": {"v2-events": ["gateway-v2"]},
    "llm_gateway_app_tenants": {
        "gateway-v2": "00000000-0000-0000-0000-000000000001",
        "gateway-v1": "tenant-v1-compatible",
    },
    "llm_gateway_decision_url": "https://gateway.invalid/api/v1/hosting/llm/decision",
    "llm_gateway_decision_app_id": "v2-decisions",
    "llm_gateway_decision_app_secret": "test-outbound-secret",
}

_MYAGENT_ENV_PREFIXES = (
    "AUTO_CHAT_",
    "OPENAI_",
    "LLM_",
    "EMBEDDING_",
    "RERANK_",
    "POSTGRES_",
    "NEO4J_",
    "REDIS_",
    "MILVUS_",
    "GAME_",
    "ROBOTGATEWAY_",
    "RAG_",
    "LIGHTRAG_",
    "GATHER_CONTEXT_",
    "MAX_CONCURRENT_",
    "OFFLINE_TRIGGER_",
    "DEFAULT_MONTHLY_",
    "QUOTA_WARNING_",
)
_MYAGENT_ENV_NAMES = {"ENV", "LOG_LEVEL", "APP_WORKERS", "CORS_ALLOWED_ORIGINS"}
_V2_ENV_NAMES = {name.upper() for name in VALID_V2_SETTINGS if name.startswith("llm_gateway_")}
_V2_ENV_NAMES.add("LLM_GATEWAY_V2_FORCE_SKILLS")
_SAFE_TEST_PROCESS_ENVIRONMENT = {
    "ENV": "test",
    "OPENAI_API_KEY": "test-openai-key",
    "OPENAI_BASE_URL": "https://llm.invalid/v1",
    "POSTGRES_DSN": "postgresql+asyncpg://test:test@postgres.invalid/test",
    "NEO4J_PASSWORD": "test-neo4j-password",
}
_SAFE_LLM_GATEWAY_TEST_ENVIRONMENT = {
    "LLM_GATEWAY_V1_ENABLED": "true",
    "LLM_GATEWAY_V2_ENABLED": "false",
    "LLM_GATEWAY_APP_SECRETS": "{}",
    "LLM_GATEWAY_APP_GATEWAYS": "{}",
    "LLM_GATEWAY_APP_TENANTS": "{}",
}
_POLLUTED_GATEWAY_SECRET = "host-real-shape-secret-must-not-survive"


def _clean_subprocess_environment(**overrides: str) -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if name.upper() not in _MYAGENT_ENV_NAMES
        and not name.upper().startswith(_MYAGENT_ENV_PREFIXES)
    }
    environment.update(overrides)
    environment["PYTHONPATH"] = str(REPOSITORY_ROOT)
    return environment


def _run_config_import(tmp_path: Path, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; "
                "from src.config import settings; "
                "print(json.dumps({'env': settings.env, 'logLevel': settings.log_level}))"
            ),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_conftest_environment(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    environment = _clean_subprocess_environment(
        ENV="production",
        LLM_GATEWAY_V2_ENABLED="true",
        LLM_GATEWAY_APP_SECRETS=json.dumps({"production-events": _POLLUTED_GATEWAY_SECRET}),
        LLM_GATEWAY_APP_GATEWAYS=json.dumps({"production-events": ["production-gateway"]}),
        LLM_GATEWAY_APP_TENANTS=json.dumps(
            {"production-gateway": "11111111-1111-1111-1111-111111111111"}
        ),
        LLM_GATEWAY_DECISION_URL="https://production-gateway.example.com/api/decision",
        LLM_GATEWAY_DECISION_APP_ID="production-decisions",
        LLM_GATEWAY_DECISION_APP_SECRET=_POLLUTED_GATEWAY_SECRET,
        LLM_GATEWAY_UNKNOWN_HOST_VALUE=_POLLUTED_GATEWAY_SECRET,
    )
    script = (
        "import json, os, runpy, sys; "
        "runpy.run_path(sys.argv[1]); "
        "gateway = {key: value for key, value in os.environ.items() "
        "if key.startswith('LLM_GATEWAY_')}; "
        "print(json.dumps({'ENV': os.environ.get('ENV'), 'gateway': gateway}, sort_keys=True))"
    )
    return subprocess.run(
        [sys.executable, "-c", script, str(REPOSITORY_ROOT / "tests" / "conftest.py")],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(autouse=True)
def _remove_inherited_settings_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    safe_names = set(_SAFE_TEST_PROCESS_ENVIRONMENT) | set(_SAFE_LLM_GATEWAY_TEST_ENVIRONMENT)
    for name in tuple(os.environ):
        upper_name = name.upper()
        is_myagent_setting = (
            upper_name in _MYAGENT_ENV_NAMES
            or upper_name.startswith(_MYAGENT_ENV_PREFIXES)
        )
        if is_myagent_setting and upper_name not in safe_names:
            monkeypatch.delenv(name, raising=False)
    for name in _V2_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_global_test_environment_uses_fixed_safe_placeholders() -> None:
    actual = {name: os.environ.get(name) for name in _SAFE_TEST_PROCESS_ENVIRONMENT}

    assert actual == _SAFE_TEST_PROCESS_ENVIRONMENT


def test_conftest_removes_inherited_llm_gateway_environment(tmp_path: Path) -> None:
    result = _run_conftest_environment(tmp_path)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "ENV": "test",
        "gateway": _SAFE_LLM_GATEWAY_TEST_ENVIRONMENT,
    }
    assert _POLLUTED_GATEWAY_SECRET not in result.stdout
    assert _POLLUTED_GATEWAY_SECRET not in result.stderr


def test_test_environment_does_not_read_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("LOG_LEVEL=DOTENV_WAS_READ\n", encoding="utf-8")
    environment = _clean_subprocess_environment(
        ENV="test",
        OPENAI_API_KEY="test-openai-key",
        OPENAI_BASE_URL="https://llm.invalid/v1",
        POSTGRES_DSN="postgresql+asyncpg://test:test@postgres.invalid/test",
        NEO4J_PASSWORD="test-neo4j-password",
    )

    result = _run_config_import(tmp_path, environment)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"env": "test", "logLevel": "INFO"}


@pytest.mark.parametrize("missing_name", ["OPENAI_API_KEY", "POSTGRES_DSN", "NEO4J_PASSWORD"])
def test_production_missing_required_setting_fails(tmp_path: Path, missing_name: str) -> None:
    required = {
        "OPENAI_API_KEY": "production-openai-key",
        "OPENAI_BASE_URL": "https://llm.invalid/v1",
        "POSTGRES_DSN": "postgresql+asyncpg://app:password@postgres.invalid/app",
        "NEO4J_PASSWORD": "production-neo4j-password",
    }
    required.pop(missing_name)
    environment = _clean_subprocess_environment(ENV="production", **required)

    result = _run_config_import(tmp_path, environment)

    assert result.returncode != 0
    assert missing_name.lower() in result.stderr.lower()


def test_v2_defaults_and_v1_defaults_are_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_GATEWAY_V1_ENABLED", raising=False)
    monkeypatch.delenv("LLM_GATEWAY_HOSTED_CHAT_FIXED_REPLY", raising=False)
    config = Settings(_env_file=None, **REQUIRED_SETTINGS)

    assert config.llm_gateway_v1_enabled is False
    assert config.llm_gateway_v2_enabled is False
    assert config.embedding_enabled is True
    assert config.llm_gateway_app_gateways == {}
    assert config.llm_gateway_v2_max_event_batch_size == 100
    assert config.llm_gateway_v2_max_decision_ttl_ms == 30_000
    assert config.llm_gateway_v2_event_max_attempts == 5
    assert config.llm_gateway_v2_decision_max_attempts == 5
    assert config.llm_gateway_v2_retry_base_ms == 1_000
    assert config.llm_gateway_v2_retry_max_ms == 300_000
    assert config.llm_gateway_v2_claim_ttl_ms == 30_000
    assert config.llm_gateway_v2_agent_timeout_seconds == 60.0
    assert config.llm_gateway_v2_force_skills == ()
    assert config.llm_gateway_v2_poll_ms == 250
    assert config.llm_gateway_v2_shutdown_grace_seconds == 10
    assert config.llm_gateway_v2_readiness_timeout_seconds == 3
    assert config.llm_gateway_v2_readiness_cache_seconds == 5
    assert config.auto_chat_base_url is None
    assert config.auto_chat_timeout_seconds == 45.0
    assert not hasattr(config, "auto_chat_deadline_safety_seconds")
    assert config.llm_gateway_simple_chat_timeout_seconds == 3.0
    assert config.llm_gateway_hosted_chat_fixed_reply is None


def test_v2_force_skills_accepts_comma_separated_env_value() -> None:
    config = Settings(
        _env_file=None,
        **REQUIRED_SETTINGS,
        llm_gateway_v2_force_skills="paper_plane_auto_schedule, darts_auto_schedule",
    )

    assert config.llm_gateway_v2_force_skills == (
        "paper_plane_auto_schedule",
        "darts_auto_schedule",
    )


@pytest.mark.parametrize(
    "force_skills",
    [
        "paper_plane_auto_schedule,unknown_skill",
        "paper_plane_auto_schedule,paper_plane_auto_schedule",
    ],
)
def test_v2_force_skills_rejects_unknown_or_duplicate_skills(force_skills: str) -> None:
    with pytest.raises(ValidationError, match="force_skills"):
        Settings(
            _env_file=None,
            **REQUIRED_SETTINGS,
            llm_gateway_v2_force_skills=force_skills,
        )


def test_auto_chat_configuration_accepts_internal_service_url() -> None:
    config = Settings(
        _env_file=None,
        **REQUIRED_SETTINGS,
        auto_chat_base_url="http://192.168.1.50:8000/",
        auto_chat_timeout_seconds=40,
        llm_gateway_simple_chat_timeout_seconds=2.5,
    )

    assert config.auto_chat_base_url == "http://192.168.1.50:8000/"
    assert config.auto_chat_timeout_seconds == 40
    assert config.llm_gateway_simple_chat_timeout_seconds == 2.5


def test_hosted_chat_fixed_reply_configuration_is_available() -> None:
    config = Settings(
        _env_file=None,
        **REQUIRED_SETTINGS,
        llm_gateway_hosted_chat_fixed_reply="Gateway 固定回复",
    )

    assert config.llm_gateway_hosted_chat_fixed_reply == "Gateway 固定回复"


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("auto_chat_timeout_seconds", 0),
        ("auto_chat_timeout_seconds", 61),
        ("llm_gateway_simple_chat_timeout_seconds", 0),
        ("llm_gateway_simple_chat_timeout_seconds", 3.1),
    ],
)
def test_auto_chat_numeric_limits(field_name: str, invalid_value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            **REQUIRED_SETTINGS,
            **{field_name: invalid_value},
        )


def test_v2_disabled_accepts_v1_only_identity_and_non_uuid_tenant() -> None:
    config = Settings(
        _env_file=None,
        **REQUIRED_SETTINGS,
        llm_gateway_app_secrets={"v1-events": "test-v1-secret"},
        llm_gateway_app_tenants={"gateway-v1": "tenant-v1-compatible"},
    )

    assert config.llm_gateway_v2_enabled is False
    assert config.llm_gateway_app_secrets == {"v1-events": "test-v1-secret"}


def test_v2_enabled_accepts_extra_v1_only_identity() -> None:
    config = Settings(_env_file=None, **VALID_V2_SETTINGS)

    assert set(config.llm_gateway_app_secrets) == {"v2-events", "v1-only"}
    assert set(config.llm_gateway_app_gateways) == {"v2-events"}
    assert config.llm_gateway_app_tenants["gateway-v1"] == "tenant-v1-compatible"


@pytest.mark.parametrize(
    ("override", "expected_fragment"),
    [
        ({"llm_gateway_app_gateways": {}}, "app_gateways"),
        ({"llm_gateway_app_gateways": {"v2-events": []}}, "allowlist"),
        ({"llm_gateway_app_gateways": {"   ": ["gateway-v2"]}}, "empty v2 AppId"),
        ({"llm_gateway_app_gateways": {"v2-events": ["   "]}}, "empty gatewayId"),
        (
            {"llm_gateway_app_secrets": {"v2-events": "   ", "v1-only": "test-v1-only-secret"}},
            "secret",
        ),
        (
            {
                "llm_gateway_app_gateways": {"missing-secret": ["gateway-v2"]},
            },
            "secret",
        ),
        ({"llm_gateway_app_tenants": {}}, "tenant"),
        ({"llm_gateway_app_tenants": {"gateway-v2": "not-a-uuid"}}, "UUID"),
        ({"llm_gateway_decision_url": None}, "decision_url"),
        ({"llm_gateway_decision_url": "   "}, "decision_url"),
        ({"llm_gateway_decision_app_id": None}, "decision_app_id"),
        ({"llm_gateway_decision_app_id": "   "}, "decision_app_id"),
        ({"llm_gateway_decision_app_secret": None}, "decision_app_secret"),
        ({"llm_gateway_decision_app_secret": "   "}, "decision_app_secret"),
        ({"llm_gateway_decision_app_id": "v2-events"}, "must differ"),
        ({"llm_gateway_decision_app_secret": "test-inbound-secret"}, "must differ"),
    ],
)
def test_v2_enabled_rejects_invalid_identity_configuration(
    override: dict[str, object], expected_fragment: str
) -> None:
    values = {**VALID_V2_SETTINGS, **override}

    with pytest.raises(ValidationError, match=expected_fragment):
        Settings(_env_file=None, **values)


@pytest.mark.parametrize(
    "field_name",
    [
        "llm_gateway_v2_max_event_batch_size",
        "llm_gateway_v2_max_decision_ttl_ms",
        "llm_gateway_v2_event_max_attempts",
        "llm_gateway_v2_decision_max_attempts",
        "llm_gateway_v2_retry_base_ms",
        "llm_gateway_v2_retry_max_ms",
        "llm_gateway_v2_claim_ttl_ms",
        "llm_gateway_v2_agent_timeout_seconds",
        "llm_gateway_v2_poll_ms",
        "llm_gateway_v2_shutdown_grace_seconds",
        "llm_gateway_v2_readiness_timeout_seconds",
        "llm_gateway_v2_readiness_cache_seconds",
    ],
)
def test_v2_numeric_limits_must_be_positive(field_name: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **VALID_V2_SETTINGS, **{field_name: 0})


def test_v2_retry_base_must_not_exceed_retry_max() -> None:
    with pytest.raises(ValidationError, match="retry_base_ms"):
        Settings(
            _env_file=None,
            **VALID_V2_SETTINGS,
            llm_gateway_v2_retry_base_ms=2_001,
            llm_gateway_v2_retry_max_ms=2_000,
        )


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("llm_gateway_v2_max_event_batch_size", 1_001),
        ("llm_gateway_v2_max_decision_ttl_ms", 3_600_001),
        ("llm_gateway_v2_event_max_attempts", 101),
        ("llm_gateway_v2_decision_max_attempts", 101),
        ("llm_gateway_v2_retry_base_ms", 3_600_001),
        ("llm_gateway_v2_retry_max_ms", 3_600_001),
        ("llm_gateway_v2_claim_ttl_ms", 3_600_001),
        ("llm_gateway_v2_agent_timeout_seconds", 301),
        ("llm_gateway_v2_poll_ms", 60_001),
        ("llm_gateway_v2_shutdown_grace_seconds", 301),
        ("llm_gateway_v2_readiness_timeout_seconds", 61),
        ("llm_gateway_v2_readiness_cache_seconds", 301),
    ],
)
def test_v2_numeric_limits_have_operational_upper_bounds(field_name: str, invalid_value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **VALID_V2_SETTINGS, **{field_name: invalid_value})
