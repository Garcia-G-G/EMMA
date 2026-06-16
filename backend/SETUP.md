# Emma backend — setup & deploy (Prompt 31)

A FastAPI app: Realtime WS proxy + session gate + OAuth + Stripe + dashboard. The
OpenAI master key lives **only** here (server-side); the browser never sees it.

## Run locally

```sh
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export OPENAI_API_KEY=sk-...        # your master key (server-side only)
uvicorn backend.app:app --reload    # from the repo root: uvicorn backend.app:app
# → http://localhost:8000  (landing), /dashboard, /health
```

With no captcha/OAuth/Stripe secrets set, the app runs in **dev mode**: the captcha
check passes, OAuth/Stripe routes return clear "no configurado" errors, and the demo
WS proxy works against OpenAI with your key (a real voice session spends a few cents).

## The 5 external setups (for the live product)

| # | Service | What to create | Env vars |
|---|---|---|---|
| 1 | **OpenAI** | (already have) master key | `OPENAI_API_KEY` |
| 2 | **Cloudflare Turnstile** | a widget → site + secret keys | `CLOUDFLARE_TURNSTILE_SECRET` |
| 3 | **Google OAuth** | OAuth client (web), redirect `…/auth/google/callback` | `GOOGLE_OAUTH_CLIENT_ID` / `…_SECRET` |
| 3 | **GitHub OAuth** | OAuth App, callback `…/auth/github/callback` | `GITHUB_OAUTH_CLIENT_ID` / `…_SECRET` |
| 4 | **Stripe** (test mode) | 2 products (Pro $9, Team $29) → price ids; a webhook → secret | `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_PRO`, `STRIPE_PRICE_TEAM` |
| 5 | **Fly.io** | account + `flyctl` | (deploy target) |

Plus: `JWT_SECRET`, `SESSION_SECRET` (random 32+ chars), `MONTHLY_BUDGET_USD`,
`PUBLIC_URL`, `DATABASE_URL`.

## Deploy to Fly (Part E)

```sh
cd backend
fly launch --no-deploy            # uses fly.toml
fly secrets set OPENAI_API_KEY=… JWT_SECRET=… SESSION_SECRET=… \
  GOOGLE_OAUTH_CLIENT_ID=… GOOGLE_OAUTH_CLIENT_SECRET=… \
  GITHUB_OAUTH_CLIENT_ID=… GITHUB_OAUTH_CLIENT_SECRET=… \
  STRIPE_SECRET_KEY=… STRIPE_WEBHOOK_SECRET=… STRIPE_PRICE_PRO=… STRIPE_PRICE_TEAM=… \
  CLOUDFLARE_TURNSTILE_SECRET=… MONTHLY_BUDGET_USD=50 PUBLIC_URL=https://emma.tudominio.com
fly deploy
fly certs add emma.tudominio.com   # Let's Encrypt
curl https://emma.tudominio.com/health   # → {"status":"ok"}
```

Point the Stripe webhook + the OAuth redirect URIs at `$PUBLIC_URL`. Then the live
DoD smoke works: demo → login → test card `4242 4242 4242 4242` → dashboard updates.

## What's verified now (no accounts)

`pytest backend/tests -q` covers the session gate (captcha bypass, IP rate limit,
budget 503), JWT issue/decode + tamper, proxy cost accounting, OAuth cookie
upsert/`current_user`, Stripe webhook plan upgrade/downgrade, and the auth-gated
dashboard (401 for anon). The live URL, real OAuth login, and a real Stripe charge
need the accounts above.
