#!/bin/bash
# populate-secrets.sh
#
# Run this IMMEDIATELY while the service_role key is still in your clipboard.
# Reads from pbpaste (macOS clipboard) — no key ever touches a file or terminal history.
#
# Prerequisites: gcloud auth login done, project is project-6db0f664-1423-47cb-86d

set -euo pipefail

PROJECT="project-6db0f664-1423-47cb-86d"

echo "=== Supabase Secret Population ==="
echo ""

# Validate clipboard has something that looks like a JWT
CLIPBOARD_PREVIEW=$(pbpaste | head -c 30)
if [[ "$CLIPBOARD_PREVIEW" != eyJ* ]]; then
  echo "ERROR: Clipboard does not start with 'eyJ' — doesn't look like a JWT."
  echo "       Go back to Supabase → API Keys (Legacy) → Copy the service_role key, then re-run."
  exit 1
fi
echo "✓ Clipboard looks like a JWT (starts with: ${CLIPBOARD_PREVIEW}...)"
echo ""

echo "→ Populating stg-supabase-service-role-key from clipboard..."
gcloud secrets versions add stg-supabase-service-role-key \
  --data-file=<(pbpaste) \
  --project="$PROJECT"
echo "  ✓ Done"
echo ""

echo "→ Populating stg-supabase-url..."
gcloud secrets versions add stg-supabase-url \
  --data-file=<(printf %s "https://gdiuwayqjrejwosuxmel.supabase.co") \
  --project="$PROJECT"
echo "  ✓ Done"
echo ""

echo "=== Both secrets populated ==="
echo ""
echo "Verify with:"
echo "  gcloud secrets versions list stg-supabase-service-role-key --project=$PROJECT"
echo "  gcloud secrets versions list stg-supabase-url --project=$PROJECT"
echo ""
echo "Next: paste the output back to Claude — the Terraform re-apply will be driven from there."
echo ""
echo "If you need to run Terraform manually, the correct pattern is:"
echo "  cd \$(git rev-parse --show-toplevel)/infrastructure/terraform"
echo "  terraform plan \\"
echo "    -var-file=staging.tfvars \\"
echo "    -var 'billing_account_id=01E8E3-A357F5-EE7849' \\"
echo "    -out=tf-bridge-final.plan"
echo "  terraform apply tf-bridge-final.plan"
echo ""
echo "NOTE: -var after -var-file wins (last assignment takes precedence)."
echo "      This is the only safe way to pass the real billing ID without committing it."
