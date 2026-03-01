# Azure Cross-Tenant Authentication

This VM (ailearn) accesses Azure resources across different Azure AD tenants using a **Service Principal**.

## Architecture

```
ailearn VM                          Target Tenant
(Tenant A)                          (Tenant B)
                                    
  az login                          App Registration
  --service-principal  ──────────>  "ailearn-vm-access"
  --tenant <target>                   │
                                      ▼
                                    Resource Group: OpenClaw (westus2)
                                    Role: Contributor
                                    Subscription: Visual Studio Enterprise
```

## Credentials

| Field | Value |
|-------|-------|
| App Name | `ailearn-vm-access` |
| Client ID | `e15a7f81-879a-427e-b511-b0b9f6bcd5fe` |
| Tenant ID | `67d5df2b-ac7b-415a-afec-7af97214dc74` |
| Subscription | `71aa052b-688e-467d-a17b-7658d330ec56` (Visual Studio Enterprise) |
| Resource Group | `OpenClaw` (westus2) |
| Role | Contributor |
| Secret Location | `/etc/openclaw-azure.env` (root-only, chmod 600) |

## Login

```bash
# Read secret and login
AZURE_CLIENT_SECRET=$(sudo cat /etc/openclaw-azure.env | grep AZURE_CLIENT_SECRET | cut -d= -f2) \
  && az login --service-principal \
    -u e15a7f81-879a-427e-b511-b0b9f6bcd5fe \
    -p "$AZURE_CLIENT_SECRET" \
    --tenant 67d5df2b-ac7b-415a-afec-7af97214dc74
```

## Setup Steps (for reference)

These steps were completed on 2026-03-01:

1. **Install Azure CLI** on VM: `sudo bash vm/scripts/05-install-azure-cli.sh`
2. **Create App Registration** in target tenant (Azure Portal → Azure AD → App registrations)
3. **Create Client Secret** for the app registration
4. **Store secret** on VM: `/etc/openclaw-azure.env` (chmod 600, root-only)
5. **Assign role** in target tenant: Resource Group → IAM → Add role assignment → Contributor → select the service principal

## Secret Rotation

When the client secret expires:
1. Create a new secret in Azure Portal (App Registration → Certificates & secrets)
2. Update `/etc/openclaw-azure.env` on the VM:
   ```bash
   echo "AZURE_CLIENT_SECRET=new_secret_value" | sudo tee /etc/openclaw-azure.env
   sudo chmod 600 /etc/openclaw-azure.env
   ```
3. Re-login with `az login --service-principal ...`

## Why Not Managed Identity?

This VM and the target resources are in **different Azure AD tenants**. Managed Identity only works within the same tenant. Service Principal with cross-tenant app registration is the standard solution.
