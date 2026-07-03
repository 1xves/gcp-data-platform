output "instance_name" {
  description = "Filestore instance name."
  value       = google_filestore_instance.nfs.name
}

output "ip_address" {
  description = "Reserved IP of the NFS endpoint on the VPC (first address of the instance)."
  value       = google_filestore_instance.nfs.networks[0].ip_addresses[0]
}

output "share_name" {
  description = "Exported NFS share name."
  value       = google_filestore_instance.nfs.file_shares[0].name
}

output "mount_source" {
  description = "Ready-to-use NFS mount source: <ip>:/<share>. See docs/filestore-mount.md for the full mount command."
  value       = "${google_filestore_instance.nfs.networks[0].ip_addresses[0]}:/${google_filestore_instance.nfs.file_shares[0].name}"
}
