output "job_id" {
  value = (
    var.create_dataflow_job
    ? google_dataflow_flex_template_job.event_processor[0].job_id
    : ""
  )
}
output "job_name" {
  value = (
    var.create_dataflow_job
    ? google_dataflow_flex_template_job.event_processor[0].name
    : "${var.resource_prefix}-event-processor" # placeholder for monitoring filter
  )
}
