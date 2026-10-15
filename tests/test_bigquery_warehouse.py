"""BigQuery backend logic without Google Cloud: dry-run checks and error classification.

A fake runner stands in for BigQueryRunner so the real BigQueryWarehouse code
paths (statement type, referenced tables, bytes cap, exception mapping) run
offline. The live path is checked by `python -m retail_agent doctor`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest
from google.api_core import exceptions as gexc
from google.cloud import bigquery

from retail_agent.data.warehouse import BigQueryWarehouse, QueryError

DATASET = "bigquery-public-data.thelook_ecommerce"


class FakeRunner:
    def __init__(self, *, dry=None, run=None):
        self.dry = dry or SimpleNamespace(
            statement_type="SELECT",
            referenced_tables=[SimpleNamespace(project="bigquery-public-data", dataset_id="thelook_ecommerce")],
            total_bytes_processed=1_000,
        )
        self.run = run
        self.configs = []

    def dry_run(self, sql):
        if isinstance(self.dry, Exception):
            raise self.dry
        return self.dry

    def execute_query(self, sql, job_config=None, timeout=None, max_results=None):
        self.configs.append(job_config)
        if isinstance(self.run, Exception):
            raise self.run
        df = pd.DataFrame({"n": [1, 2, 3]})
        df.attrs.update(total_rows=3, bytes_processed=1_000, cache_hit=False, job_id="job-1")
        return df


def make(runner: FakeRunner, max_bytes: int = 1_000_000) -> BigQueryWarehouse:
    wh = object.__new__(BigQueryWarehouse)  # skip the real client (needs credentials)
    wh._bq = bigquery
    wh.runner = runner
    wh.dataset = DATASET
    wh.max_bytes_billed = max_bytes
    wh.timeout_s = 5
    return wh


def test_happy_path_sets_bytes_cap_labels_and_timeout():
    runner = FakeRunner()
    result = make(runner).query("SELECT 1", labels={"user": "Tom@Corp", "turn": "abc"}, max_rows=10)
    assert result.rows == [[1], [2], [3]] and result.total_rows == 3 and result.job_id == "job-1"
    config = runner.configs[0]
    assert config.maximum_bytes_billed == 1_000_000
    assert config.labels == {"user": "tom_corp", "turn": "abc"}
    assert int(config.job_timeout_ms) == 5000  # the client stores it in API form (a string)


def test_dry_run_blocks_non_select():
    runner = FakeRunner(dry=SimpleNamespace(statement_type="DELETE", referenced_tables=[], total_bytes_processed=0))
    with pytest.raises(QueryError) as err:
        make(runner).query("x", labels={}, max_rows=1)
    assert err.value.kind == "invalid_sql" and not runner.configs


def test_dry_run_blocks_tables_outside_the_dataset():
    other = SimpleNamespace(project="my-project", dataset_id="private")
    runner = FakeRunner(dry=SimpleNamespace(statement_type="SELECT", referenced_tables=[other], total_bytes_processed=0))
    with pytest.raises(QueryError) as err:
        make(runner).query("x", labels={}, max_rows=1)
    assert err.value.kind == "invalid_sql"


def test_dry_run_estimate_over_cap_is_too_expensive():
    runner = FakeRunner(dry=SimpleNamespace(
        statement_type="SELECT", referenced_tables=[], total_bytes_processed=5_000_000_000))
    with pytest.raises(QueryError) as err:
        make(runner, max_bytes=1_000_000_000).query("x", labels={}, max_rows=1)
    assert err.value.kind == "too_expensive" and err.value.fixable_by_model


@pytest.mark.parametrize(
    "error, kind",
    [
        (gexc.BadRequest("Unrecognized name: stauts at [1:8]"), "invalid_sql"),
        (gexc.NotFound("Table not found"), "invalid_sql"),
        (gexc.Forbidden("Access Denied: billing not enabled"), "permission"),
        (gexc.Forbidden("Exceeded rate limits: rateLimitExceeded"), "unavailable"),
        (gexc.TooManyRequests("quota"), "unavailable"),
        (gexc.InternalServerError("backend error"), "unavailable"),
        (gexc.ServiceUnavailable("unavailable"), "unavailable"),
        (TimeoutError("read timed out"), "unavailable"),
    ],
)
def test_errors_are_classified(error, kind):
    with pytest.raises(QueryError) as err:
        make(FakeRunner(run=error)).query("SELECT 1", labels={}, max_rows=1)
    assert err.value.kind == kind


def test_syntax_errors_from_dry_run_go_back_to_the_model():
    runner = FakeRunner(dry=gexc.BadRequest("Syntax error: Unexpected keyword FROM at [1:10]"))
    with pytest.raises(QueryError) as err:
        make(runner).query("x", labels={}, max_rows=1)
    assert err.value.fixable_by_model and "Syntax error" in err.value.message


# --------------------------------------------------------------------------- credentials from GCP_SA_KEY_JSON


def _fake_key() -> str:
    import json

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return json.dumps({
        "type": "service_account", "project_id": "demo-project", "private_key_id": "k1", "private_key": pem,
        "client_email": "agent@demo-project.iam.gserviceaccount.com", "client_id": "1",
        "token_uri": "https://oauth2.googleapis.com/token",
    })


def test_service_account_key_is_used_in_memory(tmp_path, monkeypatch):
    import os

    from retail_agent.data.warehouse import service_account_credentials
    from retail_agent.settings import load_settings

    key = _fake_key()
    monkeypatch.setenv("GCP_SA_KEY_JSON", key)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    settings = load_settings(env_file=os.devnull)
    assert settings.gcp_service_account_json == key
    assert "PRIVATE KEY" not in repr(settings)
    assert list(tmp_path.iterdir()) == []  # nothing written
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ
    creds = service_account_credentials(key)
    assert creds.service_account_email == "agent@demo-project.iam.gserviceaccount.com"
    assert creds.project_id == "demo-project"


@pytest.mark.parametrize("raw", ["not json {", '{"type": "authorized_user"}', '{"type": "service_account"}'])
def test_unusable_service_account_key_is_a_clear_error(raw):
    from retail_agent.data.warehouse import service_account_credentials

    with pytest.raises(ValueError) as err:
        service_account_credentials(raw)
    assert "GCP_SA_KEY_JSON" in str(err.value)
    assert raw not in str(err.value)  # the key's content is never echoed
