from types import SimpleNamespace as NS

import pytest

from kabu import jquants
from kabu.jquants import JQuants, PlanError


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {}), headers))
        status, body = self.responses.pop(0)
        return NS(status_code=status, json=lambda: body, text=str(body))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(jquants.time, "sleep", lambda s: None)


def test_pagination_and_header():
    s = FakeSession([(200, {"data": [{"Code": "72030"}], "pagination_key": "k1"}), (200, {"data": [{"Code": "67580"}]})])
    jq = JQuants(api_key="k", rpm=10000, session=s)
    rows = jq.valuation(date_="2026-11-10")
    assert [r["Code"] for r in rows] == ["72030", "67580"]
    assert s.calls[0][2] == {"x-api-key": "k"}
    assert s.calls[1][1]["pagination_key"] == "k1"
    assert s.calls[0][0].endswith("/v2/equities/valuation")


def test_retry_on_429_then_success():
    s = FakeSession([(429, {"message": "rate"}), (200, {"data": [{"Date": "2026-11-10"}]})])
    assert JQuants(api_key="k", rpm=10000, session=s).margin_interest(code="7203") == [{"Date": "2026-11-10"}]
    assert s.calls[0][1]["code"] == "72030"


def test_plan_error_on_403():
    s = FakeSession([(403, {"message": "This API is not available on your subscription plan."})])
    with pytest.raises(PlanError, match="subscription plan"):
        JQuants(api_key="k", rpm=10000, session=s).short_ratio(date_="2026-11-10")


def test_code_helpers():
    assert jquants.code5("7203") == "72030" and jquants.code4("72030") == "7203" and jquants.code4("285A0") == "285A"
    assert jquants.num("") is None and jquants.num("1.5") == 1.5
