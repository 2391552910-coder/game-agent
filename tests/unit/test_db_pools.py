from src.core.infrastructure.db import (
    async_session_factory,
    engine,
    event_admission_engine,
    event_admission_session_factory,
)


def test_event_admission_uses_a_dedicated_database_engine_and_pool() -> None:
    assert event_admission_engine is not engine
    assert async_session_factory.kw["bind"] is engine
    assert event_admission_session_factory.kw["bind"] is event_admission_engine
