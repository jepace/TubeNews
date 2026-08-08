"""Shared pytest fixtures for the TubeNews suite."""
import pytest


@pytest.fixture(autouse=True)
def _disable_supadata_budget(monkeypatch):
    """Keep the Supadata credit meter out of the way of unrelated tests.

    ``fetch_transcript`` reserves a credit before every call. Left enabled, the
    suite would write a counter into the real ``state/`` directory and then trip
    the daily cap partway through, failing whichever transcript tests happened to
    run last. A limit of 0 disables metering and short-circuits before any I/O.

    Tests that exercise the budget itself set their own limit, which takes effect
    after this fixture and therefore wins.
    """
    import TubeNews
    monkeypatch.setitem(TubeNews._daemon_config, "supadata_daily_limit", 0)
