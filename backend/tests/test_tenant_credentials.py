"""Actor and subject stay distinct in the backend audit session."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from fastapi import Request

from app import tenant_credentials
from app.dependencies import get_repository


class FakeSts:
    def __init__(self):
        self.calls: list[dict] = []

    def assume_role(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "Credentials": {
                "AccessKeyId": "access",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
                "Expiration": datetime.now(UTC) + timedelta(hours=1),
            }
        }


def test_assume_role_tags_actor_and_subject_separately(monkeypatch):
    sts = FakeSts()
    monkeypatch.setenv(tenant_credentials.ROLE_ARN_VAR, "arn:aws:iam::123456789012:role/data")
    monkeypatch.setattr(tenant_credentials.boto3, "client", lambda service: sts)

    tenant_credentials._assume("globex", "session-1", "trv_arranger", "trv_traveler")

    tags = {tag["Key"]: tag["Value"] for tag in sts.calls[0]["Tags"]}
    assert tags == {
        "tenant": "globex",
        "session_id": "session-1",
        "user": tenant_credentials.hashed_user("trv_arranger"),
        "subject": tenant_credentials.hashed_user("trv_traveler"),
    }
    assert sts.calls[0]["TransitiveTagKeys"] == ["tenant"]


def test_credential_cache_key_includes_the_subject(monkeypatch):
    calls: list[tuple[str | None, str | None]] = []

    def fake_assume(tenant_id, session_id=None, actor_id=None, subject_id=None):
        calls.append((actor_id, subject_id))
        return {
            "AccessKeyId": "access",
            "SecretAccessKey": "secret",
            "SessionToken": "token",
            "Expiration": datetime.now(UTC) + timedelta(hours=1),
        }

    tenant_credentials.clear_cache()
    monkeypatch.setattr(tenant_credentials, "_assume", fake_assume)

    tenant_credentials._credentials("globex", "session-1", "trv_arranger", "trv_a")
    tenant_credentials._credentials("globex", "session-1", "trv_arranger", "trv_b")

    assert calls == [("trv_arranger", "trv_a"), ("trv_arranger", "trv_b")]
    tenant_credentials.clear_cache()


def test_repository_factory_receives_actor_and_subject_separately():
    captured: list[tuple] = []

    def factory(*args):
        captured.append(args)
        return "scoped"

    app = SimpleNamespace(
        state=SimpleNamespace(repository="shared", scoped_repository_factory=factory)
    )
    request = Request(
        {
            "type": "http",
            "app": app,
            "headers": [
                (b"x-tenant-id", b"globex"),
                (b"x-session-id", b"session-1"),
                (b"x-actor-id", b"trv_arranger"),
                (b"x-traveler-id", b"trv_traveler"),
            ],
        }
    )

    assert get_repository(request) == "scoped"
    assert captured == [("globex", "session-1", "trv_arranger", "trv_traveler")]


def test_scoping_metric_name_matches_the_alarm_in_cdk():
    """The alarm and the emitter must name the same metric.

    `SCOPING_UNCONFIGURED_METRIC` is what the backend emits when row-scoping is
    unconfigured; `infra/lib/tenant-isolation.ts` alarms on that metric name. They cross
    a language boundary with only a comment holding them together, and the failure is
    silent in the worst direction: rename one side and the alarm watches a metric nobody
    emits, sitting green while the isolation control is off.
    """
    source = (Path(__file__).resolve().parents[2] / "infra/lib/tenant-isolation.ts").read_text()
    assert f"metricName: '{tenant_credentials.SCOPING_UNCONFIGURED_METRIC}'" in source


def test_unconfigured_scoping_emits_the_alarm_metric(monkeypatch):
    """Taking the unscoped fallback must emit the counter, not only log.

    The log line already existed and was not detection: every request kept succeeding,
    so nothing surfaced. The metric is what makes the condition alarmable.
    """
    monkeypatch.delenv(tenant_credentials.ROLE_ARN_VAR, raising=False)
    emitted: list[str] = []
    monkeypatch.setattr(tenant_credentials, "count", lambda name, **_: emitted.append(name))
    monkeypatch.setattr(tenant_credentials.boto3, "resource", lambda *_a, **_k: "unscoped")

    assert tenant_credentials.scoped_dynamodb("globex") == "unscoped"
    assert emitted == [tenant_credentials.SCOPING_UNCONFIGURED_METRIC]


def test_scoped_path_does_not_emit_the_alarm_metric(monkeypatch):
    """The healthy path must stay silent, or the alarm is meaningless.

    `treatMissingData: NOT_BREACHING` on the alarm depends on this: the counter exists
    only when the fallback is taken, so emitting it on the normal path would breach
    continuously.
    """
    monkeypatch.setenv(
        tenant_credentials.ROLE_ARN_VAR, "arn:aws:iam::123456789012:role/tenant-data"
    )
    tenant_credentials.clear_cache()
    emitted: list[str] = []
    monkeypatch.setattr(tenant_credentials, "count", lambda name, **_: emitted.append(name))
    monkeypatch.setattr(tenant_credentials.boto3, "client", lambda *_a, **_k: FakeSts())
    monkeypatch.setattr(tenant_credentials.boto3, "resource", lambda *_a, **_k: "scoped")

    assert tenant_credentials.scoped_dynamodb("globex") == "scoped"
    assert emitted == []
