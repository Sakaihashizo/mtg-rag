# ECR と Lambda（2026-07-28）
#
# **鶏と卵**: Lambda（コンテナ形式）は「イメージが ECR に既に在ること」を要求する。
# 一方 ECR リポジトリは Terraform が作る。よって 1 回の apply では完結しない。
#
# 段階適用の手順:
#   1. var.lambda_image_tag = ""（既定）のまま apply
#      → ECR・Aurora・S3/CloudFront までが作られる（Lambda と API は count=0 でスキップ）
#   2. イメージを push
#        aws ecr get-login-password --region <region> | docker login --username AWS --password-stdin <ecr>
#        docker build -f deploy/Dockerfile.lambda -t <ecr>/mtg-rag:v1 .
#        docker push <ecr>/mtg-rag:v1
#   3. terraform.tfvars に lambda_image_tag = "v1" を書いて apply
#      → Lambda と API Gateway が作られる
#
# イメージは 3.35GB（2026-07-26 実測）。ECR ストレージ $0.10/GB-月 ≈ 月 $0.34。

variable "lambda_image_tag" {
  description = <<-EOT
    ECR に push 済みイメージのタグ。空のあいだは Lambda と API Gateway を作らない
    （上記の鶏と卵を段階適用で解くためのスイッチ）。
  EOT
  type        = string
  default     = ""
}

variable "lambda_memory_mb" {
  description = <<-EOT
    Lambda のメモリ。**CPU 割り当てと連動する**ので、コールドスタート
    （ローカル模擬で 7.5 秒・e5 モデルの読み込みが支配項）にも効く。
    ローカル模擬では 3008MB を使い切っていた＝3008 から始めて実測で調整する
    （deploy_checklist の⑨の項目）。
  EOT
  type        = number
  default     = 3008
}

resource "aws_ecr_repository" "app" {
  name         = var.project
  force_delete = true # destroy 時にイメージごと消す（再ビルドできる）

  image_scanning_configuration {
    scan_on_push = false # デモ用途では不要（有効にすると走査ごとに課金）
  }

  tags = { Name = var.project }
}

# 古いイメージを溜めない（ストレージ課金は GB 単位＝3.35GB のイメージが積もると効く）
resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "直近 3 世代だけ残す"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 3
      }
      action = { type = "expire" }
    }]
  })
}

resource "aws_lambda_function" "api" {
  count = var.lambda_image_tag == "" ? 0 : 1

  function_name = "${var.project}-api"
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.app.repository_url}:${var.lambda_image_tag}"

  memory_size = var.lambda_memory_mb
  # コールドスタート 7.5 秒（ローカル模擬）＋ Aurora 復帰 15 秒＋
  # 冷えたバッファの初回ベクトル検索 30 秒級を足しても収まる余白を取る
  timeout = 60

  # Lambda は VPC 外に置く（C 案の肝）。DB へは Data API（HTTPS）で届く＝
  # NAT も Interface エンドポイントも要らない
  environment {
    # 変数名は src/db.py の実装が正（2026-07-28 の障害から:
    # 当初 "dataapi"/DATAAPI_* と推測で書き、db.py の分岐 "data_api"/AURORA_* と
    # 食い違って Lambda が localhost の PostgreSQL を探しに行った。
    # 実装を読まずに名前を書かない）
    variables = {
      DB_BACKEND         = "data_api"
      AURORA_CLUSTER_ARN = aws_rds_cluster.main.arn
      AURORA_SECRET_ARN  = aws_rds_cluster.main.master_user_secret[0].secret_arn
      DB_NAME            = aws_rds_cluster.main.database_name
      ROUTER_BACKEND     = "nova"
      # 東京リージョンに us. プロファイルは無い（2026-07-28 実測: Nova が静かに
      # フォールバックしフィルタ全落ち）。東京の実在プロファイルは apac.
      NOVA_MODEL_ID      = "apac.amazon.nova-micro-v1:0"
      MODEL_CACHE_DIR    = "/opt/models"
      RAG_MODEL          = "SMALL_V2"
    }
  }

  tags = { Name = "${var.project}-api" }
}

# ログの保持期間（既定は無期限＝溜まると微額だが課金される）
resource "aws_cloudwatch_log_group" "lambda" {
  count             = var.lambda_image_tag == "" ? 0 : 1
  name              = "/aws/lambda/${var.project}-api"
  retention_in_days = 14
}
