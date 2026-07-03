###############################################################################
# Filestore — managed NFS for shared reference data
#
# Purpose: a single NFSv3 volume mountable by many readers simultaneously —
# Dataflow workers (large reference datasets as side inputs) and GKE pods
# (ReadOnlyMany PersistentVolume). This is the cloud-managed equivalent of an
# on-prem NAS file server: one writer refreshes the dataset, N workers mount
# read-only, no per-worker GCS download fan-out.
#
# When to use this instead of the GCS side-input pattern (transforms/enrich.py):
#   - Reference data too large to re-download per worker (multi-GB)
#   - Many workers cold-starting at once (GCS egress fan-out at autoscale time)
#   - Consumers that expect a POSIX filesystem path, not an object API
#
# When NOT to use it:
#   - Staging / cost-sensitive environments. BASIC_HDD bills for the full
#     provisioned 1 TiB minimum (~$204/month in us-central1) whether used or
#     not. This module is therefore gated behind var.enable_filestore = false
#     by default — see the root module wiring.
#
# Networking: Filestore attaches directly to the VPC (DIRECT_PEERING) and is
# reachable only from that network — no public endpoint exists. NFSv3 traffic
# stays on the private network, consistent with the "no public IPs on data
# workers" constraint in architecture/system-design.md §1.3.
###############################################################################

resource "google_filestore_instance" "nfs" {
  name     = "${var.resource_prefix}-reference-nfs"
  project  = var.project_id
  location = var.zone # BASIC tiers are zonal; co-locate with GKE/Dataflow workers
  tier     = var.tier

  file_shares {
    name        = var.share_name
    capacity_gb = var.capacity_gb # 1024 is the BASIC_HDD floor — cannot go lower
  }

  networks {
    network      = var.network_name # network NAME (not self_link) per API contract
    modes        = ["MODE_IPV4"]
    connect_mode = "DIRECT_PEERING"
  }

  labels = var.labels
}
