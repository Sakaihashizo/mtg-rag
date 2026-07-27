# 静的フロント（S3 + CloudFront）— 2026-07-28
#
# static/index.html を配る。Lambda コンテナには static を載せない設計
# （api_server の mount は「ディレクトリが在るときだけ」に条件化済み・2026-07-26）。
#
# バケットは非公開のまま CloudFront だけが読めるようにする（OAC＝Origin Access Control）。
# 「S3 を公開にしてバケットポリシーで穴を開ける」旧作法は使わない。
#
# 費用: CloudFront は無料枠（月 1TB 転送・1,000 万リクエスト）が大きく、
# デモ流量では実質ゼロ。S3 も index.html 1 枚なら誤差。

resource "aws_s3_bucket" "frontend" {
  bucket        = "${var.project}-frontend-${data.aws_caller_identity.current.account_id}"
  force_destroy = true

  tags = { Name = "${var.project}-frontend" }
}

resource "aws_s3_bucket_public_access_block" "frontend" {
  bucket                  = aws_s3_bucket.frontend.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_cloudfront_origin_access_control" "frontend" {
  name                              = "${var.project}-frontend"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "frontend" {
  enabled             = true
  default_root_object = "index.html"
  comment             = "${var.project} demo frontend"
  # 北米・欧州・アジアの安い階級に絞る（全世界配信は不要）
  price_class = "PriceClass_200"

  origin {
    domain_name              = aws_s3_bucket.frontend.bucket_regional_domain_name
    origin_id                = "s3-frontend"
    origin_access_control_id = aws_cloudfront_origin_access_control.frontend.id
  }

  default_cache_behavior {
    target_origin_id       = "s3-frontend"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    # AWS 管理のキャッシュポリシー CachingOptimized（固定 ID）
    cache_policy_id = "658327ea-f89d-4fab-a63d-7e88639e58f6"
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true # 独自ドメインを使わない＝証明書代ゼロ
  }

  tags = { Name = "${var.project}-frontend" }
}

# CloudFront だけがバケットを読めるようにする
data "aws_iam_policy_document" "frontend" {
  statement {
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.frontend.arn}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.frontend.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  policy = data.aws_iam_policy_document.frontend.json
}
