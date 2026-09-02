"""Auto-migration adds new model columns to an existing (older) SQLite DB."""
import importlib

from sqlalchemy import inspect, text


def test_add_missing_column(tmp_path, monkeypatch):
    monkeypatch.setenv("BB_DATA_DIR", str(tmp_path / "data"))
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    import app.models  # noqa: F401

    # Create the schema, then simulate an OLD db by dropping a newer column.
    db.init_db()
    with db.engine.begin() as conn:
        cols = {c["name"] for c in inspect(db.engine).get_columns("app_setting")}
        assert "account_cooldown_days" in cols
        # Rebuild app_setting without account_cooldown_days to mimic a pre-upgrade DB.
        conn.execute(text("ALTER TABLE app_setting DROP COLUMN account_cooldown_days"))

    cols_after_drop = {c["name"] for c in inspect(db.engine).get_columns("app_setting")}
    assert "account_cooldown_days" not in cols_after_drop

    # Re-running init_db must re-add it (additive migration), no crash.
    db.init_db()
    cols_final = {c["name"] for c in inspect(db.engine).get_columns("app_setting")}
    assert "account_cooldown_days" in cols_final
