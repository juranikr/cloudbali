provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      Project     = "cloudbali"
      Environment = "prod"
      ManagedBy   = "terraform"
    }
  }
}
