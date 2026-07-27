# IAM（2026-07-28）
#
# 方針: **Lambda には鍵を持たせない**。実行ロールで権限を与える＝
# .env の AWS_ACCESS_KEY_ID/SECRET を本番に置く必要が無くなる
# （ローカル開発は従来どおり .env の鍵・本番はロール、と経路で分ける）。

# ── Lambda 実行ロール ────────────────────────────────────────────
data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${var.project}-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

# CloudWatch Logs への書き込み（AWS 管理ポリシー）
resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "lambda_runtime" {
  # Data API で SQL を投げる（C 案の DB 接続経路）
  statement {
    sid = "DataApi"
    actions = [
      "rds-data:ExecuteStatement",
      "rds-data:BatchExecuteStatement",
      "rds-data:BeginTransaction",
      "rds-data:CommitTransaction",
      "rds-data:RollbackTransaction",
    ]
    resources = [aws_rds_cluster.main.arn]
  }

  # Data API は Secret ARN 経由で認証する＝シークレットの読み取りが要る
  statement {
    sid       = "ReadDbSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_rds_cluster.main.master_user_secret[0].secret_arn]
  }

  # 本番ルーター（Nova Micro）。モデル ID をワイルドカードにしないのは、
  # 上位モデルを誤って叩いて単価が跳ねる事故を権限側でも塞ぐため
  # （$0.00013/クエリ の Micro に対し Pro は 23 倍）
  statement {
    sid     = "InvokeNovaMicro"
    actions = ["bedrock:InvokeModel"]
    resources = [
      "arn:aws:bedrock:*::foundation-model/amazon.nova-micro-v1:0",
      "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*.amazon.nova-micro-v1:0",
    ]
  }
}

resource "aws_iam_role_policy" "lambda_runtime" {
  name   = "${var.project}-lambda-runtime"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda_runtime.json
}

# ── Aurora が S3 から CSV を読むためのロール（データ搬入・aws_s3 拡張）──────
data "aws_iam_policy_document" "rds_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["rds.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "aurora_s3_import" {
  name               = "${var.project}-aurora-s3-import"
  assume_role_policy = data.aws_iam_policy_document.rds_assume.json
}

data "aws_iam_policy_document" "aurora_s3_import" {
  statement {
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.import.arn, "${aws_s3_bucket.import.arn}/*"]
  }
}

resource "aws_iam_role_policy" "aurora_s3_import" {
  name   = "${var.project}-aurora-s3-import"
  role   = aws_iam_role.aurora_s3_import.id
  policy = data.aws_iam_policy_document.aurora_s3_import.json
}

# クラスタにロールを紐付ける（これをしないと aws_s3.table_import_from_s3 が権限で落ちる）
resource "aws_rds_cluster_role_association" "s3_import" {
  db_cluster_identifier = aws_rds_cluster.main.id
  feature_name          = "s3Import"
  role_arn              = aws_iam_role.aurora_s3_import.arn
}
