"""Verifier evidence paths written from the project folder, and the read-only fetch_url worker tool."""
import pytest

from orchestrator import Store
from orchestrator.config import load_config
from orchestrator.hardware import resolve_in_workspace
from orchestrator.providers import MockProvider
from orchestrator.store import StoreError
from orchestrator.tools import ToolBox, ToolError, html_to_text
from orchestrator.verification import Verifier


# ------------------------------------------------------------------ paths

def test_project_relative_workspace_paths_resolve_inside_the_workspace(tmp_path):
    ws = tmp_path / "workspace"
    (ws / "research").mkdir(parents=True)
    (ws / "research" / "r.md").write_text("report", encoding="utf-8")
    want = (ws / "research" / "r.md").resolve()
    assert resolve_in_workspace(ws, "research/r.md") == want
    assert resolve_in_workspace(ws, "workspace/research/r.md") == want          # what workers tend to write
    assert resolve_in_workspace(ws, "./workspace/research/r.md") == want
    assert resolve_in_workspace(ws, "workspace\\research\\r.md") == want        # Windows-style separators


def test_a_real_nested_folder_named_like_the_workspace_still_wins(tmp_path):
    ws = tmp_path / "workspace"
    (ws / "workspace").mkdir(parents=True)
    (ws / "workspace" / "x.md").write_text("nested", encoding="utf-8")
    assert resolve_in_workspace(ws, "workspace/x.md") == (ws / "workspace" / "x.md").resolve()


def test_new_files_written_with_the_prefix_do_not_nest(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    box = ToolBox(ws, "w")
    assert box.call("write_file", {"path": "workspace/notes/a.md", "content": "hi"})["ok"]
    assert (ws / "notes" / "a.md").read_text(encoding="utf-8") == "hi"
    assert not (ws / "workspace").exists()


def test_escaping_the_workspace_is_still_refused(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    for bad in ("../secret.txt", "workspace/../../secret.txt"):
        with pytest.raises(StoreError):
            resolve_in_workspace(ws, bad)


def test_verifier_sees_a_file_named_from_the_project_folder(tmp_path):
    ws = tmp_path / "workspace"
    (ws / "research").mkdir(parents=True)
    (ws / "research" / "report.md").write_text("# Sync report\nUse the audio clock.", encoding="utf-8")
    store = Store(tmp_path / "t.db")
    try:
        cfg = load_config(None)
        v = Verifier(store, MockProvider([]), cfg, ws, ["va"])
        t = store.create_ticket(dict(title="Report", assignee="research_manager", worker_role="worker",
                                     context="c", acceptance_criteria=["a"],
                                     return_spec={"format": "path", "description": "d"}), "top_manager")
        ev = v.evidence(t, {"path": "workspace/research/report.md"})
        assert "Use the audio clock." in ev
        assert "no artifact files found" not in ev
    finally:
        store.close()


# -------------------------------------------------------------------- web

PAGE = b"""<!doctype html><html><head><title>Quartz &amp; Clocks</title><style>.x{}</style>
<script>alert('ignore me')</script></head><body><nav>menu</nav>
<h1>UQuartzClockHandle</h1><p>GetEstimatedClockRunTime returns seconds.</p><ul><li>StartClock</li></ul>
</body></html>"""


def fake_fetch(pages):
    calls = []

    def fetch(url):
        calls.append(url)
        if url not in pages:
            raise OSError("HTTP Error 404: Not Found")
        return pages[url]
    fetch.calls = calls
    return fetch


def box_with(tmp_path, pages, public=lambda h: True, **kw):
    return ToolBox(tmp_path, "w", fetch=fake_fetch(pages), is_public_host=public, **kw)


def test_fetch_url_returns_readable_text_and_marks_it_untrusted(tmp_path):
    url = "https://dev.epicgames.com/doc"
    box = box_with(tmp_path, {url: (url, "text/html; charset=utf-8", PAGE)})
    r = box.call("fetch_url", {"url": url})
    assert r["ok"], r
    assert r["title"] == "Quartz & Clocks"
    assert "GetEstimatedClockRunTime returns seconds." in r["text"]
    assert "StartClock" in r["text"]
    assert "alert" not in r["text"] and "menu" not in r["text"]       # scripts and nav dropped
    assert "untrusted" in r and r["next_offset"] is None


def test_fetch_url_pages_long_documents(tmp_path):
    url = "https://example.com/long"
    body = ("word " * 6000).encode()          # 30k chars: two pages of 20k
    box = box_with(tmp_path, {url: (url, "text/plain", body)})
    first = box.call("fetch_url", {"url": url})
    assert first["ok"] and first["next_offset"] == len(first["text"])
    second = box.call("fetch_url", {"url": url, "offset": first["next_offset"]})
    assert second["ok"] and second["offset"] == first["next_offset"]
    assert second["next_offset"] is None
    assert len(first["text"]) + len(second["text"]) == first["total_chars"]


@pytest.mark.parametrize("url", ["http://example.com", "file:///etc/passwd", "ftp://x.org/a",
                                 "https://user:pw@example.com/", "not a url"])
def test_fetch_url_refuses_non_https_and_credentials(tmp_path, url):
    box = box_with(tmp_path, {})
    r = box.call("fetch_url", {"url": url})
    assert not r["ok"]
    assert box.fetch.calls == []                                         # never reached the network


def test_fetch_url_refuses_private_hosts(tmp_path):
    box = box_with(tmp_path, {}, public=lambda h: False)
    r = box.call("fetch_url", {"url": "https://192.168.1.1/admin"})
    assert not r["ok"] and "not a public" in r["error"]
    assert box.fetch.calls == []


def test_real_host_check_blocks_local_addresses():
    from orchestrator.tools import _public_host
    assert not _public_host("localhost")
    assert not _public_host("127.0.0.1")
    assert not _public_host("169.254.169.254")                         # cloud metadata endpoint
    assert not _public_host("10.0.0.5")


def test_fetch_url_rejects_binary_content_and_reports_http_errors(tmp_path):
    url = "https://example.com/file.zip"
    box = box_with(tmp_path, {url: (url, "application/zip", b"PK\x03\x04")})
    r = box.call("fetch_url", {"url": url})
    assert not r["ok"] and "not a text page" in r["error"]
    r = box.call("fetch_url", {"url": "https://example.com/missing"})
    assert not r["ok"] and "404" in r["error"]


def test_fetch_url_can_be_disabled_and_is_then_not_advertised(tmp_path):
    box = box_with(tmp_path, {}, can_fetch_web=False)
    assert "fetch_url" not in box.describe()
    r = box.call("fetch_url", {"url": "https://example.com"})
    assert not r["ok"] and "disabled" in r["error"]
    assert "fetch_url" in ToolBox(tmp_path, "w").describe()


def test_config_default_allows_web_fetch():
    assert load_config(None)["policies"]["allow_web_fetch"] is True


def test_html_to_text_handles_entities_and_blocks():
    title, text = html_to_text("<title> A&lt;B </title><div>one</div><div>two&nbsp;three</div>")
    assert title == "A<B"
    assert "one" in text and "two\xa0three" in text
    assert text.index("one") < text.index("two")


def test_code_workers_can_check_docs_too(tmp_path):
    from orchestrator.codework import CodeSpec, CodeToolBox
    url = "https://dev.epicgames.com/doc"
    spec = CodeSpec(["src/**"], ["true"])
    box = CodeToolBox(tmp_path / "ws", "w", root=tmp_path, spec=spec,
                      fetch=fake_fetch({url: (url, "text/html", PAGE)}), is_public_host=lambda h: True)
    assert "fetch_url" in box.describe()
    assert box.call("fetch_url", {"url": url})["ok"]
    off = CodeToolBox(tmp_path / "ws", "w", root=tmp_path, spec=spec, can_fetch_web=False)
    assert "fetch_url" not in off.describe()
    assert not off.call("fetch_url", {"url": url})["ok"]
