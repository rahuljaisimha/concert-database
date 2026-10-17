from concertdb.http import USER_AGENT, Fetcher


def test_sends_our_user_agent_not_the_library_default(tmp_path):
    """requests.Session starts with "python-requests/x.y", which many sites answer with 403."""
    f = Fetcher(tmp_path)
    assert f.session.headers["User-Agent"] == USER_AGENT


def test_per_request_user_agent_still_wins(tmp_path):
    seen = {}

    class FakeResponse:
        status_code, text, headers, url = 200, "ok", {}, "https://example.test/"

    class FakeSession:
        headers = {}

        def get(self, url, params=None, headers=None, timeout=None):
            seen.update(self.headers, **(headers or {}))
            return FakeResponse()

    f = Fetcher(tmp_path, session=FakeSession())
    f.get("pollstar", "https://example.test/", headers={"User-Agent": "custom"})
    assert seen["User-Agent"] == "custom"
