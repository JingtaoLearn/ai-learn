#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Microsoft OAuth Proxy — Azure App Service One-Click Deploy
# ============================================================
# Usage:
#   ./deploy-azure.sh                    # Interactive (prompts for missing values)
#   ./deploy-azure.sh --env .env.azure   # Load config from file
#
# Prerequisites:
#   - Azure CLI installed and logged in (az login)
#   - Azure AD App Registration created with:
#       * Supported account types: Any org + personal
#       * API permissions: openid, profile, email, User.Read
#
# What this script does:
#   1. Creates Resource Group (if needed)
#   2. Creates App Service Plan (B1 Linux)
#   3. Creates Web App (Node 20)
#   4. Configures all environment variables
#   5. Deploys the code via zip deploy
#   6. Verifies the deployment
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------- Defaults ----------
APP_NAME="${APP_NAME:-ms-login-jingtao}"
RESOURCE_GROUP="${RESOURCE_GROUP:-OpenClaw}"
LOCATION="${LOCATION:-eastus}"
SKU="${SKU:-B1}"
RUNTIME="${RUNTIME:-NODE:20-lts}"

# ---------- Load env file if provided ----------
ENV_FILE=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --env) ENV_FILE="$2"; shift 2 ;;
    --app-name) APP_NAME="$2"; shift 2 ;;
    --resource-group) RESOURCE_GROUP="$2"; shift 2 ;;
    --location) LOCATION="$2"; shift 2 ;;
    --sku) SKU="$2"; shift 2 ;;
    --help|-h)
      echo "Usage: $0 [--env .env.azure] [--app-name NAME] [--resource-group RG] [--location REGION] [--sku SKU]"
      echo ""
      echo "Environment variables (set in file or export):"
      echo "  AZURE_CLIENT_ID        - Azure AD App Registration client ID (required)"
      echo "  AZURE_CLIENT_SECRET    - Azure AD App Registration client secret (required)"
      echo "  AUTH_SHARED_SECRET     - Shared secret for JWT signing (auto-generated if missing)"
      echo "  ALLOWED_CALLBACKS      - Comma-separated whitelist of callback URLs"
      echo "  NOTE_APP_CALLBACK_URL  - Default callback URL for downstream services"
      exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

if [[ -n "$ENV_FILE" && -f "$ENV_FILE" ]]; then
  echo "📄 Loading config from $ENV_FILE"
  set -a
  source "$ENV_FILE"
  set +a
fi

# ---------- Validate required values ----------
if [[ -z "${AZURE_CLIENT_ID:-}" ]]; then
  read -rp "🔑 Azure AD Client ID: " AZURE_CLIENT_ID
fi
if [[ -z "${AZURE_CLIENT_SECRET:-}" ]]; then
  read -rsp "🔐 Azure AD Client Secret: " AZURE_CLIENT_SECRET
  echo ""
fi

# Auto-generate secrets if not provided
if [[ -z "${AUTH_SHARED_SECRET:-}" ]]; then
  AUTH_SHARED_SECRET=$(openssl rand -base64 32)
  echo "🔧 Generated AUTH_SHARED_SECRET (save this for downstream services):"
  echo "   $AUTH_SHARED_SECRET"
fi
if [[ -z "${SESSION_SECRET:-}" ]]; then
  SESSION_SECRET=$(openssl rand -base64 24)
fi

# ---------- Check Azure CLI ----------
echo ""
echo "🔍 Checking Azure CLI..."
if ! command -v az &>/dev/null; then
  echo "❌ Azure CLI not found. Install: https://learn.microsoft.com/en-us/cli/azure/install-azure-cli"
  exit 1
fi

ACCOUNT=$(az account show --query '{name:name, id:id}' -o tsv 2>/dev/null || true)
if [[ -z "$ACCOUNT" ]]; then
  echo "❌ Not logged in. Run: az login"
  exit 1
fi
echo "   ✅ Logged in: $ACCOUNT"

# ---------- Derived values ----------
WEBAPP_URL="https://${APP_NAME}.azurewebsites.net"
REDIRECT_URI="${WEBAPP_URL}/auth/callback"
PLAN_NAME="${APP_NAME}-plan"

echo ""
echo "📋 Deployment Configuration:"
echo "   App Name:       $APP_NAME"
echo "   Resource Group: $RESOURCE_GROUP"
echo "   Location:       $LOCATION"
echo "   SKU:            $SKU"
echo "   URL:            $WEBAPP_URL"
echo "   Redirect URI:   $REDIRECT_URI"
echo ""
read -rp "Continue? [Y/n] " CONFIRM
if [[ "${CONFIRM:-Y}" =~ ^[Nn] ]]; then
  echo "Aborted."
  exit 0
fi

# ---------- Step 1: Resource Group ----------
echo ""
echo "📁 Step 1/5: Resource Group"
if az group show --name "$RESOURCE_GROUP" -o none 2>/dev/null; then
  echo "   ✅ Already exists"
else
  echo "   Creating $RESOURCE_GROUP in $LOCATION..."
  az group create --name "$RESOURCE_GROUP" --location "$LOCATION" -o none
  echo "   ✅ Created"
fi

# ---------- Step 2: App Service Plan ----------
echo ""
echo "📐 Step 2/5: App Service Plan"
if az appservice plan show --name "$PLAN_NAME" --resource-group "$RESOURCE_GROUP" -o none 2>/dev/null; then
  echo "   ✅ Already exists, updating SKU to $SKU..."
  az appservice plan update --name "$PLAN_NAME" --resource-group "$RESOURCE_GROUP" --sku "$SKU" -o none
else
  echo "   Creating $PLAN_NAME ($SKU, Linux)..."
  az appservice plan create --name "$PLAN_NAME" --resource-group "$RESOURCE_GROUP" --sku "$SKU" --is-linux --location "$LOCATION" -o none
  echo "   ✅ Created"
fi

# ---------- Step 3: Web App ----------
echo ""
echo "🌐 Step 3/5: Web App"
if az webapp show --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" -o none 2>/dev/null; then
  echo "   ✅ Already exists"
else
  echo "   Creating $APP_NAME..."
  az webapp create --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" --plan "$PLAN_NAME" --runtime "$RUNTIME" -o none
  echo "   ✅ Created"
fi

# Set startup command
az webapp config set --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" --startup-file "node app.js" -o none

# ---------- Step 4: Configure Environment Variables ----------
echo ""
echo "⚙️  Step 4/5: Environment Variables"
az webapp config appsettings set \
  --name "$APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --settings \
    AZURE_CLIENT_ID="$AZURE_CLIENT_ID" \
    AZURE_CLIENT_SECRET="$AZURE_CLIENT_SECRET" \
    AZURE_REDIRECT_URI="$REDIRECT_URI" \
    AUTH_SHARED_SECRET="$AUTH_SHARED_SECRET" \
    SESSION_SECRET="$SESSION_SECRET" \
    NOTE_APP_CALLBACK_URL="${NOTE_APP_CALLBACK_URL:-}" \
    ALLOWED_CALLBACKS="${ALLOWED_CALLBACKS:-}" \
    NODE_ENV="production" \
    WEBSITES_PORT="3000" \
    SCM_DO_BUILD_DURING_DEPLOYMENT="true" \
  -o none
echo "   ✅ Configured"

# ---------- Step 5: Deploy Code ----------
echo ""
echo "🚀 Step 5/5: Deploying Code"
DEPLOY_ZIP=$(mktemp /tmp/ms-login-deploy-XXXXX.zip)
cd "$SCRIPT_DIR"
zip -r "$DEPLOY_ZIP" . -x "node_modules/*" ".env" ".env.*" ".azure/*" "deploy-azure.sh" "*.md" ".git/*" >/dev/null 2>&1
echo "   Zip created: $(du -h "$DEPLOY_ZIP" | cut -f1)"

az webapp deployment source config-zip \
  --name "$APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --src "$DEPLOY_ZIP" \
  -o none

rm -f "$DEPLOY_ZIP"
echo "   ✅ Deployed"

# ---------- Verify ----------
echo ""
echo "⏳ Waiting for app to start..."
sleep 15

HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 "$WEBAPP_URL/" 2>/dev/null || echo "000")
if [[ "$HTTP_CODE" == "200" ]]; then
  echo "✅ Deployment successful!"
else
  echo "⚠️  App returned HTTP $HTTP_CODE (may still be starting, check in 30s)"
fi

echo ""
echo "============================================================"
echo "🎉 Done!"
echo ""
echo "   URL:          $WEBAPP_URL"
echo "   Login:        $WEBAPP_URL/auth/login"
echo "   Redirect URI: $REDIRECT_URI"
echo ""
echo "⚠️  Don't forget to add this Redirect URI to your Azure AD"
echo "   App Registration → Authentication → Web:"
echo "   $REDIRECT_URI"
echo ""
echo "🔑 AUTH_SHARED_SECRET (use this in downstream services):"
echo "   $AUTH_SHARED_SECRET"
echo "============================================================"
