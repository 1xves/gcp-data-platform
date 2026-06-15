variable "project_id" { type = string }
variable "region" { type = string }
variable "network_name" { type = string }
variable "subnet_cidr" { type = string }
variable "secondary_ranges" {
  type = list(object({
    range_name    = string
    ip_cidr_range = string
  }))
  default = []
}
