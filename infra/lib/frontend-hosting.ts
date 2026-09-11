import { createHash } from 'node:crypto';
import * as fs from 'node:fs';
import * as path from 'node:path';

import { CfnOutput, Duration, RemovalPolicy, Stack } from 'aws-cdk-lib';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

// Same idiom as the other constructs: `__dirname` is `dist/lib` after `tsc`, so the hop count differs.
const REPO_ROOT = path.resolve(__dirname, __dirname.includes('dist') ? '../../..' : '../..');

/** Inline `<script>` bodies in the SPA's entry document, excluding anything with a `src`. */
const INLINE_SCRIPT = /<script(?![^>]*\ssrc=)[^>]*>([\s\S]*?)<\/script>/g;

/**
 * CSP hashes for the SPA's inline scripts, computed from the source `index.html`.
 *
 * **Derived rather than pasted, because a pasted hash silently stops matching.** The one inline script
 * here sets the theme before first paint, so a dark-mode user never sees a white flash. Any edit to it
 * — even whitespace — changes the hash, and a stale literal would mean the browser refuses to run it.
 * Reading the file at synth time means the policy and the script cannot drift apart.
 *
 * **Hashing the source is sound because Vite copies the inline script verbatim into `dist/index.html`.**
 * Verified by hashing both: identical. Hashing the build output instead would be more direct and is
 * not available, since `cdk synth` does not require the frontend to have been built.
 *
 * The alternative was `'unsafe-inline'` on `script-src`, which would have made the whole policy
 * decorative: it re-permits exactly the injected-script execution CSP exists to stop.
 */
function inlineScriptHashes(): string[] {
  const entry = path.join(REPO_ROOT, 'frontend/index.html');
  const html = fs.readFileSync(entry, 'utf8');
  const hashes = [...html.matchAll(INLINE_SCRIPT)].map(
    ([, body]) => `'sha256-${createHash('sha256').update(body, 'utf8').digest('base64')}'`,
  );
  if (hashes.length === 0) {
    // Not an error worth failing synth over, but worth refusing to guess: an empty list here would
    // quietly emit a policy that blocks nothing it was meant to allow.
    throw new Error(
      `found no inline <script> in ${entry} — if the theme script moved to a file, drop this helper ` +
        'and the hash from the policy rather than shipping a hash that matches nothing',
    );
  }
  return hashes;
}

/**
 * Static hosting for the SPA: a private bucket behind CloudFront.
 *
 * **The bucket is not public, and CloudFront reaches it through Origin Access Control** rather than
 * the older public-read or website-endpoint arrangements. A public bucket would work and is what most
 * quick guides show; it also means the objects are reachable without the distribution, so a caching
 * or header policy set here could be bypassed entirely.
 *
 * **The API is served from this same distribution, and that is not an optimization — it is what makes
 * the cookie work at all.**
 *
 * The obvious arrangement serves the SPA here and lets it call the API Gateway URL directly. It fails,
 * and only a real browser reveals how: the session cookie is set on `execute-api.amazonaws.com` with
 * `SameSite=Strict`, so the browser refuses to send it on any request originating from the CloudFront
 * origin — they are different sites by definition. The login completes, the cookie is stored, and
 * every subsequent request is unauthenticated. `curl` cannot find this, because `curl` has no
 * same-site policy; the flow passed every scripted check before a headless Chromium ran it.
 *
 * The two ways out are to weaken the cookie to `SameSite=None` or to make the API same-origin. The
 * second is strictly better and is the entire point of a BFF: one origin means no CORS, no
 * credentialed-request rules, and `SameSite=Strict` keeps its full CSRF value rather than being traded
 * away for a cross-origin convenience.
 *
 * So a `/v1/*` behavior forwards to the API Gateway origin. **The path prefix is deliberately the
 * API's own stage name**, so nothing has to rewrite the URI — a CloudFront Function to strip an
 * `/api` prefix would be a moving part with no purpose.
 *
 * **Constructed before the conversation API**, because the API needs this distribution's domain as its
 * origin and its OAuth callback host. Nothing here refers back to the API's construct — the origin is
 * named by hostname, resolved from a prop — which keeps the dependency one-directional instead of a
 * cycle CloudFormation would reject.
 */
export interface FrontendHostingProps {
  /**
   * Hostname of the conversation API's API Gateway stage, without scheme or path.
   *
   * A hostname rather than the construct, so this stays independent of it: the API needs *this*
   * distribution's domain, and passing the construct both ways would be a cycle.
   */
  readonly apiDomainName: string;
  /** Stage name, which is also the path prefix routed to the API. */
  readonly apiStage: string;

  /**
   * WAF web ACL ARN to attach, or omitted for no ACL.
   *
   * An ARN rather than the construct so this stays a leaf: the ACL knows nothing about what it
   * protects, and this construct needs only the string.
   */
  readonly webAclArn?: string;

  /** Shared access-log destination, owned by `Storage`. */
  readonly logBucket: s3.IBucket;
}

export class FrontendHosting extends Construct {
  public readonly bucket: s3.Bucket;
  public readonly distribution: cloudfront.Distribution;
  /** Origin the SPA is served from — the conversation API's allowed CORS origin. */
  public readonly origin: string;

  constructor(scope: Construct, id: string, props: FrontendHostingProps) {
    super(scope, id);

    const stack = Stack.of(this);

    this.bucket = new s3.Bucket(this, 'SiteBucket', {
      bucketName: `multi-tenant-travel-frontend-${stack.account}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      // Who read what from the bucket directly. Should be nobody — CloudFront reaches it through
      // Origin Access Control — which is precisely what makes an entry here worth seeing.
      //
      // **The log bucket belongs to `Storage`, not here**, because it is shared with the policy-docs
      // and audit buckets and `Storage` is built first — a log destination has to exist before its
      // sources.
      serverAccessLogsBucket: props.logBucket,
      serverAccessLogsPrefix: 'site-bucket/',
      // A sample must leave nothing behind. `autoDeleteObjects` provisions a custom-resource Lambda,
      // which is noise in a repo people read — so the bucket is emptied by `cleanup.sh` instead.
      removalPolicy: RemovalPolicy.DESTROY,
    });

    /**
     * Security response headers, and **the CSP is the one that closes a real gap.**
     *
     * The session cookie is `HttpOnly`, `Secure` and `SameSite=Strict`, and every POST is checked
     * against an Origin allowlist. Those stop a token being read by script and stop a cross-site
     * request carrying the cookie. Neither stops script that is already running *on this origin* from
     * using the session, which is what an XSS foothold gives an attacker. CSP is the control for that
     * case, and its absence was recorded as an open finding in the threat model.
     *
     * Every directive below is the narrowest value the app actually runs with, established by reading
     * the built output rather than by guessing:
     *
     *   * `script-src` is `'self'` plus one hash. There is exactly one inline script and it is
     *     hashed rather than blanket-allowed.
     *   * `style-src` needs `fonts.googleapis.com` because the entry document links a Google Fonts
     *     stylesheet. It does **not** need `'unsafe-inline'`: Vite extracts component CSS into a
     *     linked file, and React's `style={{…}}` sets DOM properties rather than emitting a `style`
     *     attribute, which CSP does not govern.
     *   * `font-src` needs `fonts.gstatic.com`, where that stylesheet's `@font-face` files live.
     *   * `img-src` needs `data:` for the inline SVG favicon.
     *   * `connect-src` is `'self'` alone, which is only possible because the API is same-origin
     *     through this distribution. The Cognito hop is a top-level redirect rather than a fetch, so
     *     it needs no entry here — the same-origin design that made `SameSite=Strict` viable also
     *     keeps this directive tight.
     *   * `frame-ancestors`, `object-src` and `base-uri` are closed outright; nothing here embeds,
     *     plugs in, or rewrites relative URLs.
     *
     * Applied to both behaviours. On the API the CSP itself is inert — a JSON body has no script to
     * govern — but `Strict-Transport-Security` and `X-Content-Type-Options` are not, and one policy is
     * easier to keep honest than two that drift.
     */
    const securityHeaders = new cloudfront.ResponseHeadersPolicy(this, 'SecurityHeaders', {
      responseHeadersPolicyName: `${stack.stackName}-security-headers`,
      comment: 'CSP and transport headers for the SPA and the same-origin API',
      securityHeadersBehavior: {
        contentSecurityPolicy: {
          contentSecurityPolicy: [
            "default-src 'self'",
            `script-src 'self' ${inlineScriptHashes().join(' ')}`,
            "style-src 'self' https://fonts.googleapis.com",
            "font-src 'self' https://fonts.gstatic.com",
            "img-src 'self' data:",
            "connect-src 'self'",
            "frame-ancestors 'none'",
            "form-action 'self'",
            "base-uri 'self'",
            "object-src 'none'",
          ].join('; '),
          override: true,
        },
        contentTypeOptions: { override: true },
        frameOptions: { frameOption: cloudfront.HeadersFrameOption.DENY, override: true },
        referrerPolicy: {
          referrerPolicy: cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
          override: true,
        },
        // **`includeSubdomains` off, deliberately.** This distribution serves from a shared
        // `*.cloudfront.net` suffix, so asserting HSTS for subdomains would be asserting it for
        // hostnames this stack does not own. `preload` is omitted for the same reason.
        strictTransportSecurity: {
          accessControlMaxAge: Duration.days(365),
          includeSubdomains: false,
          override: true,
        },
      },
    });

    this.distribution = new cloudfront.Distribution(this, 'Distribution', {
      defaultBehavior: {
        // `withOriginAccessControl` is the current mechanism; `S3Origin` with an OAI is the
        // deprecated one that still appears in most examples.
        origin: origins.S3BucketOrigin.withOriginAccessControl(this.bucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        // The bundle is fingerprinted, so it can be cached hard; `index.html` is handled by the
        // error-response mapping below and by the deploy script's cache headers.
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
        compress: true,
        responseHeadersPolicy: securityHeaders,
      },
      defaultRootObject: 'index.html',
      /**
       * **No `errorResponses`, and the omission is deliberate — the usual SPA mapping is wrong on
       * this distribution.**
       *
       * The reflex for a single-page app is to map 404 and 403 to `/index.html` with a 200, so that
       * client-side deep links resolve instead of hitting the bucket. Two reasons not to here.
       *
       * The first is that `CustomErrorResponses` is a property of the **distribution**, not of a cache
       * behavior — CloudFormation offers no per-behavior equivalent — and it applies to statuses
       * returned by *any* origin. The conversation API shares this distribution (see
       * `additionalBehaviors` below, which is load-bearing for the session cookie), so that mapping
       * silently rewrites the API's own errors: the CSRF refusal at `403` and `not found` at `404`
       * would both reach the browser as `200` carrying `index.html`. The origin still refuses the
       * request, so this is not a bypass — but the client cannot tell a refusal from a success, and it
       * defeats a deliberate choice in the BFF, which answers `404` rather than `403` for an
       * unavailable document precisely so the status does not confirm the document exists. Collapsing
       * both to `200` erases that distinction.
       *
       * The second is that **this SPA has no client-side routes to rescue.** There is no router
       * dependency and no `pushState`; the bundle is `index.html`, `assets/*` and two SVGs. So the
       * mapping was covering a case that cannot arise while breaking one that does. A missing
       * fingerprinted asset now surfaces as the error it is rather than as HTML with a success status.
       *
       * **If you add client-side routing, do not add `errorResponses` back.** Attach a CloudFront
       * Function on `defaultBehavior` instead, rewriting extensionless URIs to `/index.html`. A
       * function association is scoped to the behavior it is attached to, which is the property this
       * distribution needs and `errorResponses` cannot give.
       */
      // North America and Europe. The cheapest class that covers the demo's audience; a global
      // distribution for a sample is cost with no reader benefit.
      priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
      comment: 'SPA + conversation API (one origin, so the session cookie works)',
      /**
       * **Set, and CloudFront ignores it — which is worth recording rather than deleting.**
       *
       * `minimumProtocolVersion` only takes effect with a *custom* certificate. This distribution uses
       * the default `*.cloudfront.net` domain — a sample should not require a reader to own a domain
       * — and that domain's TLS policy is fixed by CloudFront, so the property is inert here.
       *
       * That is why `AwsSolutions-CFR4` cannot be fixed at this layer and is acknowledged instead.
       * Left in place so that adding a custom domain later inherits the right floor rather than the
       * default.
       */
      minimumProtocolVersion: cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
      /**
       * Access logs, to the shared bucket `Storage` owns. A prefix per source keeps CloudFront's logs
       * out of the S3 access logs' key space.
       */
      logBucket: props.logBucket,
      logFilePrefix: 'cloudfront/',
      // Query strings are not logged: the OAuth `code` and `state` transit `/v1/auth/callback` as
      // query parameters, and logging them would put single-use credentials in a bucket for 90 days.
      logIncludesCookies: false,
      // Rate limiting and AWS managed rules. **One ACL covers both the SPA and the API** precisely
      // because the same-origin design made them one surface — see `edge-protection.ts` for why a
      // second regional ACL on the API Gateway stage would inspect nothing.
      webAclId: props.webAclArn,
      additionalBehaviors: {
        /**
         * The conversation API, same-origin. **Load-bearing for the session cookie** — see the class
         * comment: a `SameSite=Strict` cookie set on `execute-api` is never sent from this origin.
         */
        [`/${props.apiStage}/*`]: {
          origin: new origins.HttpOrigin(props.apiDomainName, {
            protocolPolicy: cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
            // The default 30s would cut a turn off mid-answer: the agent is often still working at
            // that point, and streaming means the response is legitimately open for minutes.
            readTimeout: Duration.seconds(60),
            keepaliveTimeout: Duration.seconds(60),
          }),
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
          // **Never cache, and forward everything.** A cached conversation response would serve one
          // traveler's answer to another; `ALL_VIEWER_EXCEPT_HOST_HEADER` is required because API
          // Gateway rejects a forwarded `Host` that is not its own — the cookie has to reach the
          // origin, and `Host` must not.
          cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
          originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
          allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
          // Compression must be off: CloudFront buffers in order to compress, which converts a
          // working stream into one flush at the end — the same silent failure as a misconfigured
          // integration, arriving at the last hop.
          compress: false,
          // Same policy as the SPA. A response headers policy only appends headers, so it does not
          // buffer and does not disturb the stream the way `compress` would.
          responseHeadersPolicy: securityHeaders,
        },
      },
    });

    this.origin = `https://${this.distribution.distributionDomainName}`;

    // Read by the SPA build so the bundle points at the deployed API, and by `deploy.sh` so it knows
    // where to sync and what to invalidate. SSM rather than an export, which would lock.
    const parameters: Record<string, string> = {
      bucket: this.bucket.bucketName,
      'distribution-id': this.distribution.distributionId,
      origin: this.origin,
    };
    for (const [name, value] of Object.entries(parameters)) {
      new ssm.StringParameter(
        this,
        `Param${name.replace(/(^|-)(\w)/g, (_, __, c) => c.toUpperCase())}`,
        {
          parameterName: `/multi-tenant-travel/frontend/${name}`,
          stringValue: value,
          description: `Frontend: ${name}`,
        },
      );
    }

    new CfnOutput(this, 'SiteUrl', {
      value: this.origin,
      description: 'The deployed SPA',
    });
  }
}
