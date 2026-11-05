from orchestrator.director import Director


def test_top_has_keepalive_and_watchdog():
    assert hasattr(Director, "_heartbeats") and hasattr(Director, "_watchdog")
