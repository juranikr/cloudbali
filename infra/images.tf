data "aws_cloudfront_cache_policy" "place_images" {
  name = "Managed-CachingOptimized"
}

resource "aws_s3_bucket" "place_images" {
  bucket        = "cloudbali-prod-place-images-${data.aws_caller_identity.current.account_id}"
  force_destroy = false
}

resource "aws_s3_bucket_public_access_block" "place_images" {
  bucket = aws_s3_bucket.place_images.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "place_images" {
  bucket = aws_s3_bucket.place_images.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "place_images" {
  bucket = aws_s3_bucket.place_images.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "place_images" {
  bucket = aws_s3_bucket.place_images.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_cors_configuration" "place_images" {
  bucket = aws_s3_bucket.place_images.id

  cors_rule {
    id              = "presigned-place-image-upload"
    allowed_headers = ["*"]
    allowed_methods = ["GET", "HEAD", "PUT"]
    allowed_origins = distinct(concat(
      [format("https://%s", aws_cloudfront_distribution.app.domain_name)],
      var.place_image_cors_origins,
    ))
    expose_headers  = ["ETag"]
    max_age_seconds = 3000
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "place_images" {
  bucket = aws_s3_bucket.place_images.id

  rule {
    id     = "clean-incomplete-and-old-versions"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }

  depends_on = [aws_s3_bucket_versioning.place_images]
}

resource "aws_cloudfront_origin_access_control" "place_images" {
  name                              = "cloudbali-prod-place-images-oac"
  description                       = "Private Cloudbali place images"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "place_images" {
  enabled             = true
  is_ipv6_enabled     = true
  comment             = "cloudbali-prod private place images"
  price_class         = "PriceClass_200"
  http_version        = "http2and3"
  wait_for_deployment = false

  origin {
    domain_name              = aws_s3_bucket.place_images.bucket_regional_domain_name
    origin_id                = "place-images-s3"
    origin_access_control_id = aws_cloudfront_origin_access_control.place_images.id
  }

  default_cache_behavior {
    target_origin_id       = "place-images-s3"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD", "OPTIONS"]
    compress               = true
    cache_policy_id        = data.aws_cloudfront_cache_policy.place_images.id
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}

data "aws_iam_policy_document" "place_images_bucket" {
  statement {
    sid     = "AllowCloudFrontReadOnly"
    effect  = "Allow"
    actions = ["s3:GetObject"]
    resources = [
      "${aws_s3_bucket.place_images.arn}/*",
    ]

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.place_images.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "place_images" {
  bucket = aws_s3_bucket.place_images.id
  policy = data.aws_iam_policy_document.place_images_bucket.json

  depends_on = [aws_s3_bucket_public_access_block.place_images]
}

resource "aws_iam_role_policy" "task_place_images" {
  name = "cloudbali-prod-place-images"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ListPlaceImages"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [aws_s3_bucket.place_images.arn]
        Condition = {
          StringLike = {
            "s3:prefix" = ["places/*"]
          }
        }
      },
      {
        Sid    = "ManagePlaceImageObjects"
        Effect = "Allow"
        Action = [
          "s3:DeleteObject",
          "s3:GetObject",
          "s3:PutObject",
        ]
        Resource = ["${aws_s3_bucket.place_images.arn}/places/*"]
      },
    ]
  })
}
