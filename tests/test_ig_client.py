import pytest

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
