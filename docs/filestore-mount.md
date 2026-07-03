# Filestore NFS — Mount Runbook

**Module:** `infrastructure/terraform/modules/filestore/`
**Purpose:** one managed NFSv3 share on the VPC (`<prefix>-reference-nfs`) that many workers mount simultaneously — the cloud equivalent of a NAS file server. Used for large, read-mostly reference datasets (user-profile enrichment data, model artifacts) where per-worker GCS downloads would fan out badly at autoscale time.

**Cost warning (read first):** BASIC_HDD bills the full provisioned 1 TiB floor (~$204/month in us-central1) whether you store 1 GB or 1 TB. Staging keeps `enable_filestore = false`; the instance exists only while a demo or workload needs it. Tearing it down deletes the data — Filestore is a cache/working set here, never the source of truth (GCS `reference_data` bucket is).

---

## 1. Provision

```bash
cd infrastructure/terraform

terraform apply \
  -var-file=staging.tfvars \
  -var 'billing_account_id=<REAL_BILLING_ID>' \
  -var 'enable_filestore=true'

# Grab the mount source (format: <ip>:/<share>, e.g. 10.x.x.x:/reference)
terraform output filestore_mount_source
```

Provisioning takes ~5 minutes. The instance is zonal (`us-central1-a` by default — same zone as the GKE node pool; keep NFS clients in-zone to avoid cross-zone latency and egress).

## 2. Mount from a GCE VM / Dataflow worker (Linux)

The client must be on the same VPC (`<prefix>-vpc`). No firewall rule is needed for same-network DIRECT_PEERING access.

```bash
sudo apt-get update && sudo apt-get install -y nfs-common

sudo mkdir -p /mnt/reference

# Replace 10.x.x.x with `terraform output filestore_mount_source`
sudo mount -t nfs -o ro,hard,timeo=600,retrans=3,vers=3 10.x.x.x:/reference /mnt/reference

df -h /mnt/reference   # verify
```

Mount options, and why:

| Option | Reason |
|--------|--------|
| `ro` | Workers are readers. Only the refresh job mounts `rw`. |
| `hard` | Retry NFS ops indefinitely rather than corrupt reads on a blip. Use `hard` + `timeo` (not `soft`) for data integrity. |
| `timeo=600,retrans=3` | 60s major timeout — tolerates zonal hiccups without instantly erroring. |
| `vers=3` | Filestore BASIC serves NFSv3 only. |

Persist across reboots in `/etc/fstab`:

```
10.x.x.x:/reference /mnt/reference nfs ro,hard,timeo=600,retrans=3,vers=3 0 0
```

## 3. Mount from GKE (PersistentVolume, ReadOnlyMany)

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: reference-nfs
spec:
  capacity:
    storage: 1Ti
  accessModes: ["ReadOnlyMany"]
  nfs:
    server: 10.x.x.x        # terraform output filestore_mount_source (ip part)
    path: /reference
    readOnly: true
  mountOptions: ["hard", "timeo=600", "retrans=3", "vers=3"]
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: reference-nfs
  namespace: data-platform
spec:
  accessModes: ["ReadOnlyMany"]
  storageClassName: ""      # bind to the static PV above, not a dynamic class
  resources:
    requests:
      storage: 1Ti
  volumeName: reference-nfs
```

Then in the pod spec: `volumeMounts: [{name: reference, mountPath: /mnt/reference, readOnly: true}]`.

## 4. Populate / refresh the dataset

One writer, many readers. The refresh job (VM or k8s CronJob) mounts `rw` and syncs from the GCS source-of-truth bucket:

```bash
sudo mount -t nfs -o rw,hard,timeo=600,retrans=3,vers=3 10.x.x.x:/reference /mnt/reference-rw

# Atomic swap pattern: write to a temp dir, then rename — readers never see
# a half-written dataset (rename is atomic on a single NFS export).
gsutil -m rsync -r gs://<project>-reference-data /mnt/reference-rw/.staging-$(date +%s)
mv /mnt/reference-rw/.staging-* /mnt/reference-rw/current-$(date +%Y%m%d)
ln -sfn current-$(date +%Y%m%d) /mnt/reference-rw/latest
```

Readers always open `/mnt/reference/latest/...`.

## 5. Relationship to the existing GCS side-input pattern

`pipelines/dataflow/transforms/enrich.py` loads reference data from GCS per worker. That remains the default: it is serverless, versioned, and $0 when idle. Filestore replaces it only when the dataset outgrows per-worker download (multi-GB) or a consumer needs a POSIX path. The decision boundary is documented in `modules/filestore/main.tf`.

## 6. Teardown

```bash
terraform apply -var-file=staging.tfvars \
  -var 'billing_account_id=<REAL_BILLING_ID>' \
  -var 'enable_filestore=false'
```

Unmount clients first (`sudo umount /mnt/reference`) — a deleted export under an active `hard` mount leaves processes in uninterruptible sleep until reboot.

**Note:** the cost-guard does not tear Filestore down automatically (its teardown targets Dataflow/Cloud Run/Feature Store/GKE). The billing budget alerts will catch a forgotten instance at the 25% threshold ($12.50) within ~2 days of provisioning. If Filestore ever becomes a standing fixture, add a `_scale_down_filestore` step to the cost-guard.
