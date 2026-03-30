from src.database.session import DatabaseSessionManager


def test_sqlite_database_session_manager_uses_expanded_pool_defaults(tmp_path):
    manager = DatabaseSessionManager(f"sqlite:///{tmp_path / 'pool_defaults.db'}")
    try:
        assert manager.engine.pool.size() == 64
        assert getattr(manager.engine.pool, "_max_overflow") == 128
        assert getattr(manager.engine.pool, "_timeout") == 120
    finally:
        manager.engine.dispose()
