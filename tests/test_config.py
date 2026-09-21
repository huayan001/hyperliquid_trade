from hl_bot.config import BotConfig, load_config


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


def test_funding_exit_defaults_split_by_strategy() -> None:
    cfg = BotConfig()
    assert cfg.trend.funding_exit_enabled is False
    assert cfg.mean_reversion.funding_exit_enabled is True
    assert cfg.mean_reversion.funding_annual_exit == 0.50
    assert cfg.trend.funding_annual_exit > cfg.trend.funding_annual_warn


def test_trend_starter_defaults_and_toml() -> None:
    cfg = load_config("config.toml")
    assert cfg.trend.starter_enabled is True
    assert abs(cfg.trend.starter_frac - 0.35) < 1e-12
    assert cfg.trend.starter_symbols == ()
