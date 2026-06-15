# dataflow-worker Packer Template

Builds a pre-warmed Docker image for Dataflow Flex Template workers by layering pinned pipeline dependencies (`apache-beam[gcp]==2.55.0`, `google-cloud-bigquery`, `google-cloud-pubsub`, `fastavro`, `google-cloud-aiplatform`) on top of Google's official `python310-template-launcher-base`, then pushing the result to GCP Artifact Registry — eliminating cold-start package installation on every job launch.

## Prerequisites

- `gcloud auth configure-docker us-central1-docker.pkg.dev` — authenticates Docker to Artifact Registry
- `packer` >= 1.9.0 installed and on `$PATH`
- Docker daemon running locally

## Usage

```bash
# 1. Install the docker plugin declared in the template
packer init .

# 2. Build and push (edit variables.pkrvars.hcl first to set your project ID)
packer build -var-file=variables.pkrvars.hcl dataflow-worker.pkr.hcl
```

## Output

The pushed image (`dataflow-worker:<image_tag>`) contains all pipeline dependencies byte-compiled and ready to import. Dataflow Flex Template jobs reference this image via `--sdk-container-image`, cutting worker startup time by removing the pip-install step from the critical path.
