from hl_bot.config import load_config


def test_default_is_dry_run_without_keys(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("HL_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("HL_ACCOUNT_ADDRESS", raising=False)
    monkeypatch.delenv("HL_ENABLE_LIVE", raising=False)
    monkeypatch.delenv("HL_DRY_RUN", raising=False)
    cfg = load_config("config.toml")
    assert cfg.dry_run is True
    assert cfg.enable_live is False
    assert cfg.private_key is None
    assert cfg.symbols == ("BTC", "ETH", "SOL", "HYPE")


def test_live_flag_still_requires_enable_env(monkeypatch) -> None:
    monkeypatch.delenv("HL_ENABLE_LIVE", raising=False)
    cfg = load_config("config.toml", cli_live=True)
    assert cfg.dry_run is False
    assert cfg.enable_live is False
    try:
        cfg.require_live_ready()
        raise AssertionError("should have blocked live")
    except RuntimeError as exc:
        assert "HL_ENABLE_LIVE" in str(exc)
