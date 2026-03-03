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

## Quick Deploy to Azure

```bash
# 1. Copy and fill in your Azure AD credentials
cp .env.azure.example .env.azure
vim .env.azure

# 2. Deploy (creates resource group, plan, webapp, configures env, deploys code)
./deploy-azure.sh --env .env.azure
```

Or fully non-interactive:

```bash
AZURE_CLIENT_ID=xxx AZURE_CLIENT_SECRET=xxx ./deploy-azure.sh --app-name my-auth --location westus2
```

## Azure AD App Registration Setup

1. Go to [Azure Portal](https://portal.azure.com/) → **Azure Active Directory** → **App registrations**
2. Click **New registration**
3. Configure:
   - **Name**: `OAuth Proxy` (or any name)
   - **Supported account types**: "Accounts in any organizational directory and personal Microsoft accounts"
   - **Redirect URI**: Web — `https://<app-name>.azurewebsites.net/auth/callback`
4. Note the **Application (client) ID**
5. Go to **Certificates & secrets** → **New client secret** — copy the value
6. **API permissions**: `openid`, `profile`, `email`, `User.Read`

## Downstream Service Integration

Your downstream service needs:

1. **Shared secret** — same `AUTH_SHARED_SECRET` as the proxy
2. **Redirect to login** — when user is unauthenticated:
   ```
   302 → https://<proxy>.azurewebsites.net/auth/login?redirect=https://your-service.com/auth/callback
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
| `AZURE_CLIENT_ID` | Azure AD App client ID | ✅ |
| `AZURE_CLIENT_SECRET` | Azure AD App client secret | ✅ |
| `AUTH_SHARED_SECRET` | JWT signing secret (shared with downstream) | ✅ |
| `ALLOWED_CALLBACKS` | Comma-separated whitelist of callback URLs | Recommended |
| `NOTE_APP_CALLBACK_URL` | Default callback URL | Optional |
| `SESSION_SECRET` | Express session secret | Auto-generated |
| `AZURE_REDIRECT_URI` | OAuth callback URL (auto-set by deploy script) | Auto-set |

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
