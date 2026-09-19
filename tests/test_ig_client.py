import pytest
from trading_ig import IGService

from oil_pair.ig_client import IGClient
from oil_pair.settings import IGCredentials


@pytest.fixture
def client():
    return IGClient(IGCredentials(username="u", password="p", api_key="k", acc_number="n"))


def test_call_reraises_unrelated_exceptions_without_retrying(client):
    calls = []

    def fn():
        calls.append(1)
        raise ValueError("some other unrelated error")

    with pytest.raises(ValueError):
        client._call(fn)

    assert len(calls) == 1


@pytest.mark.parametrize(
    "acc_type, expected_base_url",
    [
        ("demo", "https://demo-api.ig.com/gateway/deal"),
        ("live", "https://api.ig.com/gateway/deal"),
    ],
)
def test_login_uses_acc_type_from_credentials(monkeypatch, acc_type, expected_base_url):
    # Real incident risk: acc_type used to be hardcoded to "demo" in
    # ig_client.py regardless of what the credentials said, so there was no
    # code path that could ever reach IG's live API. Confirm it now flows
    # through from IGCredentials.acc_type instead.
    monkeypatch.setattr(IGService, "create_session", lambda self, version="2": None)
    client = IGClient(IGCredentials(username="u", password="p", api_key="k", acc_number="n", acc_type=acc_type))

    client.login()

    assert client.service.BASE_URL == expected_base_url
