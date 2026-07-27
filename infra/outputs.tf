# 出力（apply 後に必要になる値）— 2026-07-28
#
# 機微な値は sensitive = true を付ける（terraform output では伏せられ、
# 明示的に取り出したときだけ見える）。state 自体が機微情報を含むので
# .gitignore 済み＝push しない。

output "aurora_cluster_arn" {
  description = "Data API を叩くときに指定するクラスタ ARN（db.py の Data API 面で使う）"
  value       = aws_rds_cluster.main.arn
}

output "aurora_secret_arn" {
  description = "DB 認証情報のシークレット ARN（Data API はこれで認証する）"
  value       = aws_rds_cluster.main.master_user_secret[0].secret_arn
}

output "aurora_endpoint" {
  description = "TCP 直結する場合のエンドポイント（C 案では使わない・将来 B 案へ戻すとき用）"
  value       = aws_rds_cluster.main.endpoint
}

output "database_name" {
  description = "初期データベース名"
  value       = aws_rds_cluster.main.database_name
}

output "import_bucket" {
  description = "搬入 CSV を置く S3 バケット（aws_s3 拡張が読む）"
  value       = aws_s3_bucket.import.bucket
}

output "engine_version" {
  description = "実際に選ばれた PostgreSQL バージョン（16.3 以上であること）"
  value       = aws_rds_cluster.main.engine_version
}

# db.py の Data API 面に渡す環境変数の雛形。
# apply 後に `terraform output -raw dataapi_env` で取り出して .env へ流し込める
output "dataapi_env" {
  description = "Data API 用の環境変数（そのまま .env に貼れる形）"
  sensitive   = true
  value       = <<-EOT
    DB_BACKEND=data_api
    AURORA_CLUSTER_ARN=${aws_rds_cluster.main.arn}
    AURORA_SECRET_ARN=${aws_rds_cluster.main.master_user_secret[0].secret_arn}
    DB_NAME=${aws_rds_cluster.main.database_name}
    AWS_DEFAULT_REGION=${var.region}
  EOT
}

# ── 段階2（lambda_image_tag を設定した後）に出てくる値 ──────────────

output "ecr_repository_url" {
  description = "イメージの push 先（docker push するときに使う）"
  value       = aws_ecr_repository.app.repository_url
}

output "api_endpoint" {
  description = "API のエンドポイント（/search /ask /health はこの下）"
  value       = var.lambda_image_tag == "" ? "(未作成: lambda_image_tag を設定して apply)" : aws_apigatewayv2_api.main[0].api_endpoint
}

output "demo_url" {
  description = "★動く URL（README に載せる面接の実弾）"
  value       = "https://${aws_cloudfront_distribution.frontend.domain_name}"
}

output "frontend_bucket" {
  description = "static/index.html を置くバケット（aws s3 sync の宛先）"
  value       = aws_s3_bucket.frontend.bucket
}
