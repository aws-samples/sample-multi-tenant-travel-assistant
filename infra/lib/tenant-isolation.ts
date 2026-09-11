import { CfnOutput, Duration, Stack } from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

export interface TenantIsolationProps {
  readonly tables: Record<string, dynamodb.Table>;
  /** The role that assumes this one — the backend's own execution role. */
  readonly assumedBy: iam.IRole;
}

/**
 * Row-level tenant isolation: **one** role, scoped at assume time by a session tag.
 *
 * `dynamodb:LeadingKeys` accepts policy variables, so the tenant arrives as a session tag
 * rather than being baked into a role per tenant. Onboarding a customer therefore stays a
 * *data* operation — role-per-tenant would make every new tenant an IAM deploy, which does
 * not reach thousands of tenants and is a fair objection to that shape.
 *
 * **What this defends against, precisely.** Not prompt injection: the model has no channel
 * to name a tenant (it is injected from a verified JWT on a path the model cannot reach) and
 * Cedar gates the action before the target runs. This defends against *our own code* being
 * wrong — a query built with the wrong prefix by a bug, a refactor, or an exploit is refused
 * by IAM regardless of intent.
 *
 * **This half is not an agentic problem.** Pooled multi-tenant SaaS has isolated rows this way
 * for years. It is conventional on purpose; the novel layer is Cedar and the interceptor above
 * it.
 *
 * **Caveat, stated rather than hidden:** a fully compromised backend can assume this role with
 * any tenant tag, because it serves every tenant. That is the pooled-tenancy trust boundary,
 * identical in any non-agentic SaaS, and the reason compliance-driven customers buy the silo
 * model. IAM bounds the blast radius of a bug; it does not make a pooled service unbreakable.
 */
export class TenantIsolation extends Construct {
  public readonly dataRole: iam.Role;
  /** Breaches when a request was served without tenant-scoped credentials. */
  public readonly scopingUnconfiguredAlarm: cloudwatch.Alarm;

  constructor(scope: Construct, id: string, props: TenantIsolationProps) {
    super(scope, id);

    const stackName = Stack.of(this).stackName;

    this.dataRole = new iam.Role(this, 'TenantDataRole', {
      roleName: `${stackName}-tenant-data`,
      description:
        'Assumed per request with a tenant session tag; LeadingKeys pins access to that tenant',
      // Only the backend may assume it, and **only with a tenant tag supplied.**
      //
      // A bare `assumedBy: principal` grants `sts:AssumeRole` unconditionally, and that was the
      // shape here. The statement below grants `sts:TagSession` and constrains the tag keys, but
      // `sts:TagSession` is only required *when tags are supplied* — so a call passing no tags
      // needed nothing but `sts:AssumeRole` and succeeded.
      //
      // The consequence was a confusing failure rather than an unsafe one: an untagged session
      // received credentials, then every DynamoDB call failed, because `aws:PrincipalTag/tenant`
      // was absent and `LeadingKeys` interpolated to a prefix matching nothing. Failing closed is
      // the important half, but it failed *late* and at the wrong layer, surfacing as data that
      // isn't there instead of an authorization error.
      //
      // `PrincipalWithConditions` is what attaches a condition to the generated trust statement;
      // adding one afterwards would leave the unconditional grant in place beside it, and an
      // allow is an allow.
      //
      // **`aws:RequestTag/tenant` rather than `aws:TagKeys`.** `aws:RequestTag/<key>` exists only
      // when that key is present in the call, so requiring it is what rejects an untagged assume.
      // `aws:TagKeys` enumerates which keys a request carries: the right tool for restricting
      // *which* tags may be set, as the `TagSession` statement below uses it, and the wrong one
      // for asserting that a particular key was set at all.
      assumedBy: new iam.PrincipalWithConditions(props.assumedBy.grantPrincipal, {
        StringLike: { 'aws:RequestTag/tenant': '*' },
      }),
    });

    // Explicit trust statement so `sts:TagSession` is granted alongside `sts:AssumeRole`, and so
    // the tag keys are constrained to exactly the four this design uses.
    this.dataRole.assumeRolePolicy?.addStatements(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        principals: [props.assumedBy.grantPrincipal],
        actions: ['sts:TagSession'],
        conditions: {
          // Restricting the *keys* means a caller cannot smuggle in extra tags that another
          // policy might key off later.
          //
          // **One is a boundary, three are labels.** `tenant` is the isolation control —
          // `LeadingKeys` interpolates it below, so its value decides what data is reachable.
          // `session_id`, `user` (the actor) and `subject` are **audit dimensions**: they ride on
          // the same session so CloudTrail can say which conversation, who acted, and for whom.
          // No policy keys off them.
          //
          // `user` is a **hash** of the traveler id, never the id. CloudTrail is retained for
          // years and shipped to SIEMs, so a per-person identifier accumulating there becomes a
          // person-tracking dataset by accident. The hash keeps what an audit needs — same
          // person, same value — and drops what it does not.
          'ForAllValues:StringEquals': {
            'aws:TagKeys': ['tenant', 'session_id', 'user', 'subject'],
          },
          // The tenant tag must actually be present — belt and braces with the code path, which
          // always sends one. Deliberately **not** required for the audit tags: a missing
          // conversation id degrades attribution, and failing the assume over it would trade a
          // working request for a tidier log line.
          StringLike: { 'aws:RequestTag/tenant': '*' },
        },
      }),
    );

    const tableArns = Object.values(props.tables).flatMap((table) => [
      table.tableArn,
      `${table.tableArn}/index/*`,
    ]);

    this.dataRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'dynamodb:GetItem',
          'dynamodb:BatchGetItem',
          'dynamodb:Query',
          'dynamodb:PutItem',
          'dynamodb:UpdateItem',
          'dynamodb:DeleteItem',
        ],
        resources: tableArns,
        conditions: {
          // **The isolation mechanism.** The partition key must equal this session's tenant.
          //
          // `ForAllValues` matters: without it, a request naming several partitions would pass if
          // *any* one matched.
          //
          // **`StringEquals`, not `StringLike` with a trailing wildcard — and the wildcard was a
          // real cross-tenant hole rather than a stylistic choice.** The condition read
          // `TENANT#${aws:PrincipalTag/tenant}*`, justified in a comment here on the grounds that
          // keys "must also cover composite forms". There are no composite forms: every partition
          // key in all six tables is exactly `TENANT#<id>`, which `repository.py` and
          // `models/common.py` both already said. So the wildcard protected nothing and cost the
          // boundary its precision, because `TenantId` is `^[a-z][a-z0-9_-]{1,31}$` — hyphens and
          // underscores are legal, so `globex` and `globex-eu` are both valid ids and
          // `TENANT#globex*` matches the second one.
          //
          // Proven with `iam:SimulatePrincipalPolicy` rather than argued, since the trust policy
          // (correctly) refuses to issue these credentials outside the backend. A session tagged
          // `globex` was **allowed** against `TENANT#globex-eu`, `TENANT#globex_eu` and
          // `TENANT#globexeu`, while `TENANT#initech` was an implicit deny — so the boundary held
          // only for tenants that happened not to share a prefix. Our two fixtures do not, which
          // is exactly why nothing caught it.
          //
          // `verify_isolation.py` layer 5 now asserts the operator is `StringEquals` and simulates
          // the collision, so a revert to `StringLike` fails the suite rather than reading fine.
          'ForAllValues:StringEquals': {
            'dynamodb:LeadingKeys': ['TENANT#${aws:PrincipalTag/tenant}'],
          },
        },
      }),
    );

    // Scan is absent from the action list above on purpose: a Scan has no partition key, so
    // `LeadingKeys` cannot constrain it. Any code needing a Scan would have to bypass this
    // role — which is exactly the signal we want if someone tries.

    /**
     * **An alarm for the one failure mode this control has: being switched off.**
     *
     * Everything above defends against a *wrong* query. None of it defends against the role not being
     * used at all. `backend/app/tenant_credentials.py` falls back to the backend's own broad
     * credentials when `TENANT_DATA_ROLE_ARN` is absent, which is correct for a local run and a
     * misconfiguration in a deployed one — and it fails *open* in effect, because every request keeps
     * succeeding with the isolation boundary gone. Nothing errors, so nothing draws attention.
     *
     * The metric name is a cross-language contract with `SCOPING_UNCONFIGURED_METRIC` in that module.
     * If they disagree this alarm watches a metric nobody emits and sits green forever, which is worse
     * than having no alarm at all.
     *
     * `treatMissingData: NOT_BREACHING` because the healthy state emits nothing: the counter only
     * appears when the fallback is taken. Sum over one minute with a threshold of zero means a single
     * unscoped request is enough to breach, which is the right sensitivity for a control being absent
     * rather than for a rate of errors.
     *
     * **No alarm action attached, and that is a real limitation rather than an oversight.** A sample
     * has nobody to page and provisioning an SNS topic plus a subscription would add a resource that
     * only makes sense with a human behind it. So this surfaces in the console and via the API and is
     * queryable in CI; it does not wake anyone. An adopter should attach an action, and that is the
     * single most valuable thing they can add here.
     */
    this.scopingUnconfiguredAlarm = new cloudwatch.Alarm(this, 'TenantScopingUnconfigured', {
      alarmName: `${stackName}-tenant-scoping-unconfigured`,
      alarmDescription:
        'The backend served a request without tenant-scoped credentials, so DynamoDB row-level ' +
        'isolation was not in force. Check TENANT_DATA_ROLE_ARN on the mock TMC function.',
      metric: new cloudwatch.Metric({
        namespace: 'MultiTenantTravel',
        metricName: 'TenantScopingUnconfigured',
        // `observe()` always adds a `service` dimension, and the mock TMC function sets
        // `POWERTOOLS_SERVICE_NAME: 'mock-tmc'`. An alarm on the undimensioned metric would never
        // match, because EMF publishes against the dimension set it was given.
        dimensionsMap: { service: 'mock-tmc' },
        statistic: cloudwatch.Stats.SUM,
        period: Duration.minutes(1),
      }),
      threshold: 0,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    new CfnOutput(this, 'TenantScopingAlarmName', {
      value: this.scopingUnconfiguredAlarm.alarmName,
      description: 'Breaches when a request is served without tenant-scoped credentials',
    });

    new CfnOutput(this, 'TenantDataRoleArn', {
      value: this.dataRole.roleArn,
      description: 'Assumed per request with a tenant tag; scopes DynamoDB to that tenant',
    });
  }
}
