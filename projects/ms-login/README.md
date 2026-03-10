# Microsoft OAuth Proxy

A centralized authentication proxy that handles Microsoft OAuth 2.0 login and issues JWTs to downstream services. Acts as a lightweight SSO gateway — downstream services only need to verify JWT signatures, never touching OAuth directly.

## Architecture

```
User → Downstream Service (no session)
         │ 302 redirect
         ▼
       OAuth Proxy (/auth/login?redirect=callback_url)
         │ Microsoft OAuth (Authorization Code Flow)
         │ Extract email from Graph API
         │ Sign JWT (HS256, 30s expiry)
         ▼
       HTML auto-POST → Downstream Service (/auth/callback)
         │ Verify JWT, establish session
         ▼
       User sees protected content
```

## Deployment (Docker + nginx-proxy)

The service runs as a Docker container behind nginx-proxy with automatic HTTPS via Let's Encrypt.

```bash
# 1. Copy and configure environment
cp .env.example .env
vim .env   # Fill in AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, secrets

# 2. Build and start the container
docker compose up -d --build

# 3. Verify
docker compose logs -f
```

The `docker-compose.yml` uses `S_DOMAIN` (set in `/etc/environment` on the server) for nginx-proxy integration:
- `VIRTUAL_HOST=ms-login.${S_DOMAIN}`
- `LETSENCRYPT_HOST=ms-login.${S_DOMAIN}`

## Azure AD App Registration Setup

1. Go to [Azure Portal](https://portal.azure.com/) → **Microsoft Entra ID** → **App registrations**
2. Click **New registration**
3. Configure:
   - **Name**: `OAuth Proxy` (or any name)
   - **Supported account types**: "Accounts in any organizational directory and personal Microsoft accounts"
   - **Redirect URI**: Web — `https://ms-login.<your-domain>/auth/callback`
4. Note the **Application (client) ID**
5. Go to **Certificates & secrets** → **New client secret** — copy the value
6. **API permissions**: `openid`, `profile`, `email`, `User.Read`

## Downstream Service Integration

Your downstream service needs:

1. **Shared secret** — same `AUTH_SHARED_SECRET` as the proxy
2. **Redirect to login** — when user is unauthenticated:
   ```
   302 → https://ms-login.<domain>/auth/login?redirect=https://your-service.<domain>/auth/callback
   ```
3. **Receive callback** — `POST /auth/callback` with `token` in form body:
   ```js
   const payload = jwt.verify(req.body.token, AUTH_SHARED_SECRET);
   // payload = { email, displayName, iat, exp }
   req.session.user = { email: payload.email, displayName: payload.displayName };
   ```

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `S_DOMAIN` | Base domain for nginx-proxy (e.g., `ai.jingtao.fun`) | ✅ |
| `AZURE_CLIENT_ID` | Azure AD App client ID | ✅ |
| `AZURE_CLIENT_SECRET` | Azure AD App client secret | ✅ |
| `AUTH_SHARED_SECRET` | JWT signing secret (shared with downstream) | ✅ |
| `SESSION_SECRET` | Express session secret | ✅ |
| `AZURE_REDIRECT_URI` | OAuth callback URL | Defaults to `https://ms-login.<S_DOMAIN>/auth/callback` |
| `ALLOWED_CALLBACKS` | Comma-separated whitelist of callback URLs | Recommended |
| `NOTE_APP_CALLBACK_URL` | Default callback URL | Optional |

## Routes

| Route | Description |
|-------|-------------|
| `GET /` | Landing page |
| `GET /auth/login?redirect=URL` | Initiates Microsoft OAuth, redirects back to URL after auth |
| `GET /auth/callback` | Receives OAuth code from Microsoft |
| `GET /logout` | Clears proxy session |

## Security

- **Callback whitelist**: Only URLs matching `ALLOWED_CALLBACKS` are accepted as redirect targets
- **Short-lived JWT**: 30-second expiry — tokens are one-time-use for the POST redirect
- **Server-side secrets**: Client secret and shared secret never reach the browser
- **HTTPS only**: Secure cookies in production mode

---

[← Back to Projects](../)
