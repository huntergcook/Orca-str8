from orchestrator.top import TopManager


def test_top_has_keepalive_and_watchdog():
    assert hasattr(TopManager, "_heartbeats") and hasattr(TopManager, "_watchdog")
