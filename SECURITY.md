# Security

## Reporting a vulnerability

**Please do not open a public issue.** This sample's subject is tenant isolation, so a reproduction
case is very often a working cross-tenant probe. Posting one publicly hands it to anyone who has
deployed this code before they have a chance to pull the fix.

Report it privately through the repository's
[Security tab](https://github.com/oreokeb/multi-tenant-travel-assistant/security) using
**Report a vulnerability**. This opens an advisory visible only to you and the maintainers. If you
hear nothing within a couple of weeks, please follow up rather than assume it was received.

Useful things to include, roughly in order of how much they help:

- Which boundary you crossed. The eight probed boundaries are listed in `README.md` and exercised by
  `scripts/verify_isolation.py`, so naming the row is usually enough to locate the control.
- Whether you crossed it as an authenticated user of another tenant, an unauthenticated caller, or by
  manipulating a token.
- Whether `scripts/verify_isolation.py` still passes against your deployment. If it passes while you
  have a working bypass, the probe has a gap and that is a second, separate finding.

## What is in scope

The code in this repository, and any deployment of it that you control.

**Please do not probe someone else's deployment**, including any hosted demo linked from the README.
Deploy your own with `./deploy.sh` and attack that. It is your account, your data, and your bill.

## What is already known and deliberate

Some findings a scanner will report here are recorded decisions rather than oversights, and a report
about them will be closed as such:

- **13 `cdk-nag` rules are suppressed**, each with its reasoning written at the suppression site in
  `infra/lib/nag-suppressions.ts`. Read the note before reporting one. `AwsSolutions-CFR1`
  (geo restriction), `AwsSolutions-IAM4` and `IAM5` (managed policies and wildcards on the
  demo's roles) are the ones most often flagged.
- **The CloudFront distribution uses the default certificate**, whose TLS policy is fixed at TLSv1
  and cannot be raised without bringing your own domain and certificate. The code sets
  `minimumProtocolVersion` anyway and CloudFront ignores it; this is documented in
  `infra/lib/frontend-hosting.ts` rather than deleted, because the setting is the thing a reader
  expects to find.
- **AWS WAF is opt-in and off by default**, which is why `cdk-nag` reports `AwsSolutions-CFR2` as a
  warning on a default deploy. The web ACL is fully implemented in `infra/lib/edge-protection.ts`;
  deploy with `./deploy.sh --waf` or `TRAVEL_WAF=true` to attach it. A default deploy is not
  unprotected: the API Gateway throttle on `/v1/*` is always on precisely because the WAF is not, and
  it bounds model spend rather than request volume. See `infra/lib/conversation-api.ts`.
- **The demo users share a password held in SSM Parameter Store**, readable by anyone with access to
  the deploying account. It exists so a reader can sign in as several personas.

## This is sample code

It is written to be read, and to demonstrate multi-tenant isolation patterns on Amazon Bedrock
AgentCore. It is not a supported product, it carries no availability commitment, and nobody is
watching a pager for it.

If you take patterns from here into production, the parts that need your own judgement first are the
suppressions listed above, the demo authentication flow, and the fact that the seeded tenants and
travelers are fixtures rather than a real customer model.
