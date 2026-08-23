variable "aws_region" {
  type    = string
  default = "ap-northeast-2"
}
variable "vpc_id" {
  type    = string
  default = "vpc-0c1828bb31502023e"
}
variable "public_subnet_ids" {
  type    = list(string)
  default = ["subnet-099b1fa1d5bd71b89", "subnet-00db04d04efbf094c"]
}
variable "alb_security_group_id" {
  type    = string
  default = "sg-08ff7c5ceae900541"
}
variable "rds_security_group_id" {
  type    = string
  default = "sg-06b7e323d015509bb"
}
variable "alb_listener_arn" {
  type    = string
  default = "arn:aws:elasticloadbalancing:ap-northeast-2:155557574983:listener/app/tourmiddle-dev-alb/03e74b903e913c8a/0f198ab439bacb76"
}
variable "alb_dns_name" {
  type    = string
  default = "tourmiddle-dev-alb-295541249.ap-northeast-2.elb.amazonaws.com"
}
variable "ecs_cluster_name" {
  type    = string
  default = "tourmiddle-dev-cluster"
}
variable "desired_count" {
  type        = number
  description = "Keep at 0 until the first image is pushed."
  default     = 0
}
variable "app_name" {
  type    = string
  default = "PATRA"
}

variable "discovery_worker_command" {
  type        = list(string)
  description = "Fargate command for the durable curator worker; replaceable without changing the workflow contract."
  default     = ["python", "-m", "app.discovery_worker"]
}

variable "discovery_worker_mode" {
  type        = string
  description = "Worker mode passed through Step Functions so a fuller curator can replace candidate discovery later."
  default     = "candidate_discovery"
}

variable "curation_worker_command" {
  type        = list(string)
  description = "Fargate command for the multi-source, proposal-only curation worker."
  default     = ["python", "-m", "app.curation_worker"]
}

variable "operations_alert_email" {
  type        = string
  description = "Optional email subscription for discovery workflow alarms; AWS requires confirmation."
  default     = ""
}

variable "place_image_cors_origins" {
  type        = list(string)
  description = "Additional browser origins allowed to PUT presigned place images."
  default = [
    "http://127.0.0.1:5173",
    "http://localhost:5173",
  ]
}
