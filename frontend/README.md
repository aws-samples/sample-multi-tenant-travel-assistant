# Frontend

The React and TypeScript single-page application for the travel assistant. It authenticates through
Cognito, keeps OAuth tokens behind the conversation API's `HttpOnly` session cookie, streams agent
turns, and renders the typed card contract from `shared/generated/cards.ts`.

## Local checks

```bash
npm ci
npm test
npm run build
npm run lint
```

For local UI development, Vite proxies `/v1` to `VITE_API_TARGET`. The default target is an example
URL, so set it to a deployed conversation API before testing authenticated flows.

Do not deploy this directory independently. Run `../deploy.sh` from the repository root so the API,
identity callbacks, CloudFront distribution, and frontend bundle are updated together.
