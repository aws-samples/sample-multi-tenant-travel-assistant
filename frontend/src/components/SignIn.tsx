/**
 * The signed-out screen.
 *
 * A link, not a fetch: the OAuth flow is a browser redirect and the API sets the httpOnly cookie on
 * the way back. There is no form here because there is no password in the browser to collect — the
 * hosted identity provider owns that.
 */
import { loginUrl } from '../lib/api';
import { Logomark } from './Brand';
import { ArrowRightIcon } from './icons';

export function SignIn() {
  return (
    <main className="signin">
      <div className="signin-card">
        <Logomark size={56} />
        <h1 className="signin-title">Travel Assistant</h1>
        <p className="signin-sub">
          Corporate travel, arranged in conversation — for whoever you are traveling as.
        </p>
        <a className="btn primary signin-btn" href={loginUrl()}>
          Sign in
          <ArrowRightIcon size={18} />
        </a>
      </div>
      <p className="signin-foot eyebrow">Secured by your organization's identity provider</p>
    </main>
  );
}

export function SessionUnavailable({ onRetry }: { onRetry: () => void }) {
  return (
    <main className="signin">
      <div className="signin-card" role="alert">
        <Logomark size={56} />
        <h1 className="signin-title">Travel Assistant</h1>
        <p className="signin-sub">
          The assistant could not check your session. Check your connection and try again.
        </p>
        <button type="button" className="btn primary signin-btn" onClick={onRetry}>
          Try again
        </button>
      </div>
    </main>
  );
}
