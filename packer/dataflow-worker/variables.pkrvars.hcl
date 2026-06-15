# Packer variable values for the dataflow-worker image build.
# Pass this file explicitly: packer build -var-file=variables.pkrvars.hcl dataflow-worker.pkr.hcl

artifact_registry_repo   = "data-platform-images"
gcp_project_id           = "YOUR_PROJECT_ID"
artifact_registry_region = "us-central1"
image_tag                = "1.0.0"
