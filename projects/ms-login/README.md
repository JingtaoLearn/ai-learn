# Microsoft Login Demo

A demo web application that implements Microsoft Account sign-in using OAuth 2.0 Authorization Code flow with MSAL (Microsoft Authentication Library).

## Features

- Sign in with any Microsoft account (personal, work, or school)
- View profile information (display name, email, account ID)
- Profile photo display (when available)
- Session-based authentication
- Clean UI with Tailwind CSS

## Architecture

- **Backend**: Node.js + Express
- **Auth Library**: @azure/msal-node (Confidential Client)
- **OAuth Flow**: Authorization Code flow
- **User Data**: Microsoft Graph API (`/me` endpoint)
- **Templates**: EJS
- **Styling**: Tailwind CSS (CDN)

## Azure App Registration Setup

1. Go to [Azure Portal](https://portal.azure.com/) > **Azure Active Directory** > **App registrations**
2. Click **New registration**
3. Configure:
   - **Name**: `MS Login Demo` (or any name)
   - **Supported account types**: "Accounts in any organizational directory and personal Microsoft accounts"
   - **Redirect URI**: Web — `https://ms-login.ai.jingtao.fun/auth/callback`
4. After creation, note the **Application (client) ID**
5. Go to **Certificates & secrets** > **New client secret** — copy the secret value
6. Go to **API permissions** > ensure these are listed:
   - `openid`
   - `profile`
   - `email`
   - `User.Read`

## Environment Variables

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

| Variable | Description |
|----------|-------------|
| `AZURE_CLIENT_ID` | Application (client) ID from Azure |
| `AZURE_CLIENT_SECRET` | Client secret value |
| `AZURE_REDIRECT_URI` | OAuth callback URL (defaults to `https://ms-login.ai.jingtao.fun/auth/callback`) |
| `SESSION_SECRET` | Random string for session encryption |

## Local Development

```bash
npm install
cp .env.example .env
# Edit .env with your Azure credentials
npm start
```

The app runs on `http://localhost:3000`.

## Docker Deployment

```bash
cp .env.example .env
# Edit .env with your Azure credentials
docker compose up -d
```

The container exposes port 3000 and integrates with nginx-proxy for automatic HTTPS via the `VIRTUAL_HOST` and `LETSENCRYPT_HOST` environment variables.

## Routes

| Route | Description |
|-------|-------------|
| `GET /` | Landing page with sign-in button |
| `GET /auth/login` | Initiates Microsoft OAuth redirect |
| `GET /auth/callback` | Handles OAuth callback |
| `GET /profile` | Shows logged-in user info |
| `GET /logout` | Clears session, redirects to `/` |

---

[← Back to Projects](../)
