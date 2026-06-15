#!/bin/bash
echo "=== PROJECTS ==="
gcloud projects list --format="table(projectId,name,projectNumber)" --quiet

PROJECTS=$(gcloud projects list --format="value(projectId)" --quiet)

for PROJECT_ID in $PROJECTS; do
  echo ""
  echo "========================================================"
  echo "AUDITING PROJECT: $PROJECT_ID"
  echo "========================================================"
  
  echo ""
  echo "--- 2 & 8. Compute Instances ---"
  gcloud compute instances list --format="table(name,zone,status,machineType,lastStartTimestamp)" --project="$PROJECT_ID" --quiet 2>/dev/null || echo "API disabled or error."
  
  echo ""
  echo "--- 3. Dataflow Jobs (us-central1, active) ---"
  gcloud dataflow jobs list --region=us-central1 --status=active --project="$PROJECT_ID" --quiet 2>/dev/null || echo "No active jobs or API disabled."
  
  echo ""
  echo "--- 4. Redis Instances (us-central1 & us-east1) ---"
  gcloud redis instances list --region=us-central1 --project="$PROJECT_ID" --quiet 2>/dev/null || echo "API disabled or no instances in us-central1."
  gcloud redis instances list --region=us-east1 --project="$PROJECT_ID" --quiet 2>/dev/null || echo "API disabled or no instances in us-east1."
  
  echo ""
  echo "--- 5. GKE Clusters ---"
  gcloud container clusters list --project="$PROJECT_ID" --quiet 2>/dev/null || echo "API disabled or no clusters."
  
  echo ""
  echo "--- 6. Vertex AI Endpoints (us-central1) ---"
  gcloud ai endpoints list --region=us-central1 --project="$PROJECT_ID" --quiet 2>/dev/null || echo "API disabled or no endpoints."
  
  echo ""
  echo "--- 7. Cloud SQL Instances ---"
  gcloud sql instances list --project="$PROJECT_ID" --quiet 2>/dev/null || echo "API disabled or no SQL instances."
done
