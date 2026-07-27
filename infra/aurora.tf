# Aurora Serverless v2（PostgreSQL + pgvector・Data API 型）— 2026-07-28
#
# 設計の要点（docs/ai/architecture_serverless.md より）:
#   - **min 0 ACU（scale-to-zero）**＝使わない時間のコンピュート課金がゼロ。
#     成立条件は「RDS Proxy を置かない」こと（Proxy は接続を維持して pause を妨げる）。
#   - **エンジンは PostgreSQL 16.3 以上**（auto-pause 要件 16.3+ ∧ Data API 対応 16.1+ の交点）。
#   - **Data API を有効化**（enable_http_endpoint）＝Lambda を VPC 外に置ける＝
#     NAT も Interface エンドポイントも要らない、というコスト構造の根っこ。
#   - パスワードは **AWS に生成・保管させる**（manage_master_user_password）。
#     Secrets Manager のシークレットが自動で作られ、Data API はその ARN で認証する。
#     ＝平文のパスワードが state にも .env にも載らない。
#
# 課金の目安（東京・2026-07-11 の料金表）:
#   コンピュート $0.15/ACU時（停止中はゼロ）/ ストレージ $0.12/GB月 /
#   I/O $0.24/100万 / Data API $0.35/100万リクエスト（初年度 月100万まで無料枠）

resource "aws_db_subnet_group" "aurora" {
  name       = "${var.project}-aurora"
  subnet_ids = aws_subnet.private[*].id

  tags = { Name = "${var.project}-aurora" }
}

# エンジンのバージョン選定（2026-07-28 実測で修正）。
# 要件は「16.3 以上」（auto-pause 要件 16.3+ ∧ Data API 対応 16.1+ の交点）だが、
# **AWS は古いマイナーを廃止する**ため 16.3/16.4/16.6 は東京に既に存在しなかった
# （最初の plan がここで落ちた＝data source を使っていても候補リストが推測だと
#  同じ穴に落ちる、という実例）。boto3 で実在版を列挙して現行のものに差し替えた。
# 2026-07-28 時点の東京の実在版: 16.8 / 16.9 / 16.10 / 16.11 / 16.13
# （"-limitless" 付きは Aurora Limitless Database 用の別物なので選ばない）
# 新しい順に並べ、将来また廃止されても次の候補へ落ちるようにしてある。
data "aws_rds_engine_version" "postgresql" {
  engine             = "aurora-postgresql"
  preferred_versions = ["16.13", "16.11", "16.10", "16.9", "16.8"]
}

resource "aws_rds_cluster" "main" {
  cluster_identifier = "${var.project}-aurora"
  engine             = "aurora-postgresql"
  # Serverless v2 は engine_mode = "provisioned"（直感に反するが仕様。
  # 旧 Serverless v1 の "serverless" とは別物で、v2 は provisioned の上に
  # serverlessv2_scaling_configuration を重ねる形）
  engine_mode     = "provisioned"
  engine_version  = data.aws_rds_engine_version.postgresql.version
  database_name   = var.db_name
  master_username = var.db_master_username

  # パスワードは AWS が生成し Secrets Manager が保管する（平文を持たない）
  manage_master_user_password = true

  # Data API（HTTPS で SQL を投げる公式 API）。C 案の前提
  enable_http_endpoint = true

  db_subnet_group_name   = aws_db_subnet_group.aurora.name
  vpc_security_group_ids = [aws_security_group.aurora.id]

  serverlessv2_scaling_configuration {
    min_capacity             = 0 # scale-to-zero（Proxy 非使用が条件）
    max_capacity             = var.aurora_max_acu
    seconds_until_auto_pause = var.aurora_auto_pause_seconds
  }

  # デモ用途なので最小限に。バックアップは DB サイズ 100% まで無料枠
  backup_retention_period = 1
  skip_final_snapshot     = true
  # 誤操作での消失を防ぐならここを true にする。ただし true だと
  # terraform destroy が失敗する＝「消せることがコスト管理」の方針と衝突するため false
  deletion_protection = false

  tags = { Name = "${var.project}-aurora" }
}

resource "aws_rds_cluster_instance" "main" {
  identifier         = "${var.project}-aurora-1"
  cluster_identifier = aws_rds_cluster.main.id
  # Serverless v2 のインスタンスクラスは固定文字列
  instance_class = "db.serverless"
  engine         = aws_rds_cluster.main.engine
  engine_version = aws_rds_cluster.main.engine_version

  # 追加費用のかかる監視は既定で入れない（要るときに上げる）
  performance_insights_enabled = false

  tags = { Name = "${var.project}-aurora-1" }
}

# 搬入用の S3 バケット（CSV の一時置き場）。
# 取り込みが終われば中身は消してよい（月 0.4 円級だが、置きっぱなしにしない習慣として）
resource "aws_s3_bucket" "import" {
  bucket        = "${var.project}-import-${data.aws_caller_identity.current.account_id}"
  force_destroy = true # destroy 時に中身ごと消す（搬入 CSV は再生成できる）

  tags = { Name = "${var.project}-import" }
}

resource "aws_s3_bucket_public_access_block" "import" {
  bucket                  = aws_s3_bucket.import.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

data "aws_caller_identity" "current" {}
