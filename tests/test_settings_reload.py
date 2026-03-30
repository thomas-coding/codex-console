from src.config import settings as settings_module


def test_get_settings_reloads_defaults_after_database_becomes_ready(monkeypatch):
    monkeypatch.setattr(settings_module, "_settings", None)
    monkeypatch.setattr(settings_module, "_settings_loaded_from_db", False)

    ready = {"value": False}
    monkeypatch.setattr(settings_module, "_database_session_ready", lambda: ready["value"])
    monkeypatch.setattr(settings_module, "init_default_settings", lambda: None)

    def fake_load_settings():
        data = {name: defn.default_value for name, defn in settings_module.SETTING_DEFINITIONS.items()}
        data.update(
            {
                "registration_browser_profile_enabled": True,
                "registration_browser_first_enabled": True,
                "registration_browser_headless": False,
                "registration_browser_persistent_profile_dir": "/app/.browser-profiles",
            }
        )
        return data

    monkeypatch.setattr(settings_module, "_load_settings_from_db", fake_load_settings)

    settings_before_db = settings_module.get_settings()
    assert settings_before_db.registration_browser_first_enabled is False
    assert settings_module._settings_loaded_from_db is False

    ready["value"] = True
    settings_after_db = settings_module.get_settings()

    assert settings_after_db.registration_browser_profile_enabled is True
    assert settings_after_db.registration_browser_first_enabled is True
    assert settings_after_db.registration_browser_headless is False
    assert settings_after_db.registration_browser_persistent_profile_dir == "/app/.browser-profiles"
    assert settings_module._settings_loaded_from_db is True
