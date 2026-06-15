output "feature_store_id" { value = google_vertex_ai_featurestore.platform.id }
output "endpoint_id" { value = var.enable_online_endpoint ? google_vertex_ai_endpoint.churn_risk[0].id : null }
output "endpoint_name" { value = var.enable_online_endpoint ? google_vertex_ai_endpoint.churn_risk[0].name : null }
