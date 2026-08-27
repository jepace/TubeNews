"""Shared pytest fixtures for the TubeNews suite."""
import pytest


@pytest.fixture(autouse=True)
def _isolate_supadata_budget(tmp_path_factory, monkeypatch):
    """Keep the Supadata credit meter out of the way of unrelated tests.

    ``fetch_transcript`` reserves a credit before every call and consults a
    persisted backoff marker, both stored under ``STATE_ROOT``. Two things go
    wrong without isolation:

    * Tests that never patch ``STATE_ROOT`` write counters into the repo's real
      ``state/`` directory.
    * A backoff written by one test blocks every later test that calls
      ``fetch_transcript``, since the vendor backoff deliberately applies even
      when metering is disabled.

    Point ``STATE_ROOT`` at a per-test temp directory and zero the limits.
    Tests that patch ``STATE_ROOT`` themselves run after this and win; tests
    that exercise the budget set their own limits, which likewise take effect
    after this fixture.
    """
    import TubeNews
    monkeypatch.setattr(TubeNews, "STATE_ROOT", tmp_path_factory.mktemp("state"))
    monkeypatch.setitem(TubeNews._daemon_config, "supadata_daily_limit", 0)
    monkeypatch.setitem(TubeNews._daemon_config, "supadata_monthly_limit", 0)
