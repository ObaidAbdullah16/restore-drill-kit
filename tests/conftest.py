import os

import pytest

PROD = os.environ.get("PG_PROD_DSN")          # e.g. postgresql://postgres:postgres@localhost:5433/shop
SANDBOX = os.environ.get("PG_SANDBOX_DSN")    # e.g. postgresql://postgres:postgres@localhost:5434/postgres

needs_pg = pytest.mark.skipif(not (PROD and SANDBOX), reason="set PG_PROD_DSN and PG_SANDBOX_DSN to run database tests")


@pytest.fixture
def aws_env(monkeypatch):
    for k in ("AWS_PROFILE", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
