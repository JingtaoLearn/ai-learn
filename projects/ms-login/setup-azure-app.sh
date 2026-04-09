#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Microsoft OAuth Proxy — Azure AD App Registration Setup
# ============================================================
# Creates an Azure AD App Registration with the required
# OAuth config, client secret, service principal, and API
# permissions. Outputs a ready-to-use .env file.
#
# Usage:
#   ./setup-azure-app.sh                         # Interactive
#   ./setup-azure-app.sh --app-name my-proxy     # Custom name
#   ./setup-azure-app.sh --redirect-uri https://my-domain.com/auth/callback
#
# Prerequisites:
#   - Azure CLI installed and logged in (az login)
#   - Sufficient Azure AD permissions to create App Registrations
#
# What this script does:
#   1. Creates App Registration (multi-tenant + personal accounts)
#   2. Creates Service Principal
#   3. Adds API permissions (openid, profile, email, User.Read)
#   4. Grants admin consent for delegated permissions
#   5. Creates client secret (2-year validity, Azure max)
#   6. Generates .env file with all required config
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------- Defaults ----------
APP_NAME="${APP_NAME:-MS-Login-Proxy}"
REDIRECT_URI="${REDIRECT_URI:-https://ms-login.ai.jingtao.fun/auth/callback}"
SECRET_YEARS="${SECRET_YEARS:-2}"
ENV_OUTPUT="${ENV_OUTPUT:-${SCRIPT_DIR}/.env}"

# Microsoft Graph API ID (constant)
MS_GRAPH_API="00000003-0000-0000-c000-000000000000"

# Permission IDs for Microsoft Graph delegated scopes
PERM_OPENID="37f7f235-527c-4136-accd-4a02d197296e"     # openid
PERM_PROFILE="14dad69e-099b-42c9-810b-d002981feec1"     # profile
PERM_EMAIL="64a6cdd6-aab1-4aaf-94b8-3cc8405e90d0"       # email
PERM_USER_READ="e1fe6dd8-ba31-4d61-89e7-88639da4683d"   # User.Read

# ---------- Parse args ----------
while [[ $# -gt 0 ]]; do
  case $1 in
    --app-name)      APP_NAME="$2"; shift 2 ;;
    --redirect-uri)  REDIRECT_URI="$2"; shift 2 ;;
    --secret-years)  SECRET_YEARS="$2"; shift 2 ;;
    --env-output)    ENV_OUTPUT="$2"; shift 2 ;;
    --help|-h)
      echo "Usage: $0 [OPTIONS]"
      echo ""
      echo "Options:"
      echo "  --app-name NAME       App Registration display name (default: MS-Login-Proxy)"
      echo "  --redirect-uri URI    OAuth callback URL (default: https://ms-login.ai.jingtao.fun/auth/callback)"
      echo "  --secret-years N      Client secret validity in years (default: 2, Azure max)"
      echo "  --env-output PATH     Output .env file path (default: ./.env)"
      echo "  -h, --help            Show this help"
      exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# ---------- Preflight checks ----------
echo "🔍 Checking Azure CLI..."
if ! command -v az &>/dev/null; then
  echo "❌ Azure CLI not found. Install: https://aka.ms/install-azure-cli"
  exit 1
fi

ACCOUNT=$(az account show --query 'user.name' -o tsv 2>/dev/null || true)
if [[ -z "$ACCOUNT" ]]; then
  echo "❌ Not logged in. Run: az login"
  exit 1
fi
echo "   ✅ Logged in as: $ACCOUNT"

echo ""
echo "📋 Configuration:"
echo "   App Name:     $APP_NAME"
echo "   Redirect URI: $REDIRECT_URI"
echo "   Secret:       ${SECRET_YEARS}-year validity"
echo "   Output:       $ENV_OUTPUT"
echo ""

if [[ -f "$ENV_OUTPUT" ]]; then
  echo "⚠️  $ENV_OUTPUT already exists!"
  read -rp "   Overwrite? [y/N] " CONFIRM
  if [[ ! "${CONFIRM:-N}" =~ ^[Yy] ]]; then
    echo "Aborted."
    exit 0
  fi
fi

read -rp "Continue? [Y/n] " CONFIRM
if [[ "${CONFIRM:-Y}" =~ ^[Nn] ]]; then
  echo "Aborted."
  exit 0
fi

# ---------- Step 1: Create App Registration ----------
echo ""
echo "📝 Step 1/5: Creating App Registration..."
APP_ID=$(az ad app create \
  --display-name "$APP_NAME" \
  --sign-in-audience AzureADandPersonalMicrosoftAccount \
  --web-redirect-uris "$REDIRECT_URI" \
  --query appId -o tsv)

if [[ -z "$APP_ID" ]]; then
  echo "❌ Failed to create App Registration"
  exit 1
fi
echo "   ✅ App ID: $APP_ID"

# ---------- Step 2: Create Service Principal ----------
echo ""
echo "🔧 Step 2/5: Creating Service Principal..."
SP_ID=$(az ad sp create --id "$APP_ID" --query id -o tsv 2>/dev/null || true)
if [[ -z "$SP_ID" ]]; then
  # SP might already exist
  SP_ID=$(az ad sp show --id "$APP_ID" --query id -o tsv 2>/dev/null || true)
fi

if [[ -z "$SP_ID" ]]; then
  echo "❌ Failed to create Service Principal"
  exit 1
fi
echo "   ✅ SP Object ID: $SP_ID"

# ---------- Step 3: Add API Permissions ----------
echo ""
echo "🔑 Step 3/5: Adding API permissions (openid, profile, email, User.Read)..."
az ad app permission add \
  --id "$APP_ID" \
  --api "$MS_GRAPH_API" \
  --api-permissions \
    "${PERM_OPENID}=Scope" \
    "${PERM_PROFILE}=Scope" \
    "${PERM_EMAIL}=Scope" \
    "${PERM_USER_READ}=Scope" \
  2>/dev/null
echo "   ✅ Permissions added"

# ---------- Step 4: Grant Admin Consent ----------
echo ""
echo "✅ Step 4/5: Granting admin consent..."
# Small delay to let Azure propagate the SP
sleep 3

az ad app permission grant \
  --id "$APP_ID" \
  --api "$MS_GRAPH_API" \
  --scope "openid profile email User.Read" \
  -o none 2>/dev/null || {
    echo "   ⚠️  Auto-consent failed. You may need to grant consent manually in Azure Portal:"
    echo "      Azure Portal → App registrations → $APP_NAME → API permissions → Grant admin consent"
  }
echo "   ✅ Consent granted"

# ---------- Step 5: Create Client Secret ----------
echo ""
echo "🔐 Step 5/5: Creating client secret (${SECRET_YEARS}-year validity)..."
CLIENT_SECRET=$(az ad app credential reset \
  --id "$APP_ID" \
  --append \
  --years "$SECRET_YEARS" \
  --query password -o tsv)

if [[ -z "$CLIENT_SECRET" ]]; then
  echo "❌ Failed to create client secret"
  exit 1
fi
echo "   ✅ Secret created (valid for ${SECRET_YEARS} years)"

# ---------- Generate secrets ----------
AUTH_SHARED_SECRET=$(openssl rand -base64 32)
SESSION_SECRET=$(openssl rand -base64 24)

# ---------- Write .env ----------
echo ""
echo "📄 Writing $ENV_OUTPUT..."
cat > "$ENV_OUTPUT" << EOF
# Azure AD App Registration
# App: $APP_NAME
# Created: $(date -u +%Y-%m-%dT%H:%M:%SZ)
AZURE_CLIENT_ID=$APP_ID
AZURE_CLIENT_SECRET=$CLIENT_SECRET
AZURE_REDIRECT_URI=$REDIRECT_URI

# JWT shared secret (share with downstream services)
AUTH_SHARED_SECRET=$AUTH_SHARED_SECRET

# Express session
SESSION_SECRET=$SESSION_SECRET

# Downstream service config
NOTE_APP_CALLBACK_URL=https://note.ai.jingtao.fun/auth/callback
ALLOWED_CALLBACKS=https://note.ai.jingtao.fun/auth/callback,https://auth-demo.ai.jingtao.fun/auth/callback
EOF

chmod 600 "$ENV_OUTPUT"
echo "   ✅ Written (chmod 600)"

# ---------- Summary ----------
SECRET_EXPIRY=$(date -u -d "+${SECRET_YEARS} years" +%Y-%m-%d 2>/dev/null || date -u -v+${SECRET_YEARS}y +%Y-%m-%d 2>/dev/null || echo "~$(date -u +%Y | xargs -I{} echo "{}+${SECRET_YEARS}" | bc)-$(date -u +%m-%d)")

echo ""
echo "============================================================"
echo "🎉 Azure AD App Registration complete!"
echo ""
echo "   App Name:          $APP_NAME"
echo "   App (Client) ID:   $APP_ID"
echo "   Redirect URI:      $REDIRECT_URI"
echo "   Secret Expires:    ~$SECRET_EXPIRY"
echo ""
echo "   .env written to:   $ENV_OUTPUT"
echo ""
echo "Next steps:"
echo "   cd $(dirname "$ENV_OUTPUT")"
echo "   docker compose up -d --build"
echo ""
echo "⚠️  AUTH_SHARED_SECRET has been regenerated."
echo "   Update downstream services (.env) to match:"
echo "   AUTH_SHARED_SECRET=$AUTH_SHARED_SECRET"
echo "============================================================"
