"""Where the agent runtime lives — resolved from SSM, not deployment-shell state.

The runtime ARN belongs to the AgentCore CLI's CloudFormation stack, which deploys after the
application stack, so it cannot be a direct CloudFormation reference. Passing it as a synth-time
environment variable would allow a later deploy from a different shell to replace a working value
with an empty string.

So the value now comes from Parameter Store, which is where every other cross-stack value in this
sample already comes from (`/multi-tenant-travel/backend/api-url`,
`/multi-tenant-travel/guardrails/*`, `/multi-tenant-travel/model/inference-profile-arn`). The
properties that matter:

* **A deploy cannot erase it.** The parameter is written by `scripts/publish_agent_refs.py` from the
  agent stack's own outputs, so it survives any number of `cdk deploy` runs from any shell.
* **A missing value is loud and names the fix**, rather than surfacing as a 404 from a service the
  reader has no reason to suspect.
* **The env var still wins when set**, so a local run or a deliberate experiment can pin a different
  runtime without touching Parameter Store — the same precedence `model/load.py` uses for
  guardrails.

Read once per container: this changes only on a deploy, and a parameter read per turn would add
latency to the conversational path for a constant.
"""

from __future__ import annotations

import logging
import os

import boto3

log = logging.getLogger("travel.conversation")

REGION = os.environ.get("AWS_REGION", "us-east-1")

RUNTIME_ARN_VAR = "RUNTIME_ARN"

RUNTIME_ARN_PARAM = "/multi-tenant-travel/agent/runtime-arn"

_ssm = None
# `None` means "not looked up yet"; a resolved absence is cached as `""` so a missing
# parameter costs one call per container rather than one per request.
_cache: dict[str, str] = {}


class AgentReferenceUnavailable(RuntimeError):
    """The runtime reference could not be read from its authoritative source."""


class AgentNotDeployed(AgentReferenceUnavailable):
    """The agent runtime is not reachable because nothing has published its ARN.

    A distinct type so the request handler can answer 503 with an explanation instead of letting an
    empty ARN reach AgentCore and returning its 404 — which is the failure this module exists to
    prevent being confusing.
    """


def _client():
    global _ssm
    if _ssm is None:
        _ssm = boto3.client("ssm", region_name=REGION)
    return _ssm


def _resolve(env_var: str, parameter: str) -> str:
    """The env var if set, else the SSM parameter, else empty.

    Values and genuine `ParameterNotFound` absences are cached per container. Other failures remain
    retryable: a throttled or interrupted first read must not pin a warm Lambda container into
    returning 503 for the rest of its lifetime.
    """
    override = os.environ.get(env_var)
    if override:
        return override

    if parameter in _cache:
        return _cache[parameter]

    client = _client()
    try:
        value = client.get_parameter(Name=parameter)["Parameter"]["Value"]
    except client.exceptions.ParameterNotFound:
        value = ""
    except Exception as error:  # noqa: BLE001 — service/network failures are retryable
        log.warning("could not read %s from SSM (%s)", parameter, type(error).__name__)
        raise AgentReferenceUnavailable(
            f"could not read {parameter} from Parameter Store"
        ) from error

    _cache[parameter] = value
    return value


def runtime_arn() -> str:
    """The agent runtime to invoke. Raises `AgentNotDeployed` if nothing has published one.

    **Raising beats returning empty**, because an empty ARN is accepted by boto3 and rejected by the
    service as a 404 that mentions neither the ARN nor this deployment. One clear exception here
    replaces a confusing error two layers away.
    """
    found = _resolve(RUNTIME_ARN_VAR, RUNTIME_ARN_PARAM)
    if not found:
        raise AgentNotDeployed(
            f"no agent runtime configured: {RUNTIME_ARN_PARAM} is unset in Parameter Store and "
            f"{RUNTIME_ARN_VAR} is not in the environment. Run `./deploy.sh` from the repository "
            "root; it deploys the agent and publishes the runtime reference."
        )
    return found
