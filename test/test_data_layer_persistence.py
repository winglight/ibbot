from types import SimpleNamespace
from typing import Optional

from src.data_layer import persistence


class _FakeBucketsAPI:
    def __init__(self):
        self.created = None

    def find_buckets(self):
        return SimpleNamespace(buckets=[])

    def create_bucket(self, *, bucket_name: str, org_id: str):
        self.created = (bucket_name, org_id)
        return SimpleNamespace(name=bucket_name)


class _FakeOrganizationsAPI:
    def __init__(self, expected_name: str, org_id: str):
        self._expected_name = expected_name
        self._org_id = org_id

    def find_organizations(self):
        return SimpleNamespace(
            orgs=[SimpleNamespace(name=self._expected_name, id=self._org_id)]
        )


class _FakeWriteAPI:
    def write(self, *args, **kwargs):  # pragma: no cover - helper stub
        return None


class _FakeClient:
    def __init__(self, *, url: str, token: Optional[str], org: str):
        self._bucket_api = _FakeBucketsAPI()
        self._org_api = _FakeOrganizationsAPI(expected_name=org, org_id="org-id")

    def write_api(self):
        return _FakeWriteAPI()

    def buckets_api(self):
        return self._bucket_api

    def organizations_api(self):
        return self._org_api


def test_influx_backend_creates_bucket_when_missing(monkeypatch, caplog):
    monkeypatch.setattr(persistence, "InfluxDBClient", _FakeClient)

    with caplog.at_level("INFO"):
        backend = persistence.InfluxPersistenceBackend(
            url="http://localhost", bucket="missing-bucket", org="test-org"
        )

    bucket_api = backend._client.buckets_api()
    assert bucket_api.created == ("missing-bucket", "org-id")
    assert any(
        "Created InfluxDB bucket missing-bucket" in record.message
        for record in caplog.records
    )
