# 入力変数（2026-07-28）
#
# 実値は terraform.tfvars に置く（.gitignore 済み＝アカウント固有の値を push しない）。
# 雛形は terraform.tfvars.example を参照。

variable "project" {
  description = "リソース名の接頭辞。destroy の取りこぼし確認にも使う"
  type        = string
  default     = "mtg-rag"
}

variable "region" {
  description = <<-EOT
    デプロイ先リージョン。既定は東京（費用の机上計算が東京基準・利用者も日本）。
    注意: Bedrock Nova の検証は us-east-1 で行った（2026-07-09/26）。
    東京で Nova Micro を叩けるかは未確認＝deploy_checklist の⑩の項目。
    叩けない場合の選択肢は (a) cross-region inference profile
    (b) Lambda ごと us-east-1 に置く。
  EOT
  type        = string
  default     = "ap-northeast-1"
}

variable "db_name" {
  description = "Aurora の初期データベース名"
  type        = string
  default     = "mtgrag"
}

variable "db_master_username" {
  description = "Aurora のマスターユーザー名（パスワードは AWS が生成し Secrets Manager が保管する）"
  type        = string
  default     = "mtgadmin"
}

variable "aurora_max_acu" {
  description = <<-EOT
    Aurora Serverless v2 の最大 ACU。min は 0 固定（scale-to-zero）。
    デモ流量では 2 で足りる想定。索引構築やデータ搬入のときだけ上げてもよい
    （上げても「使った分だけ」課金＝上限を上げること自体は無料）。
  EOT
  type        = number
  default     = 2
}

variable "aurora_auto_pause_seconds" {
  description = <<-EOT
    最終アクセスから何秒で自動停止するか（300〜86400）。
    **この値は課金に直結する**（2026-07-26 に本人指摘で判明）: Aurora は
    タイムアウトぶん「使っていないのに起きたまま」課金される＝起こすたびに余韻が付く。
      普段・放置   → 300（最短。余韻を最小に）
      作業する日   → 3600（どうせ使うので余韻が無駄にならない・15 秒待ちも消える）
      面接週など   → min_capacity を 0 でなく 0.5 にして寝かせない（$1.8/日）
    切り替えは terraform.tfvars の 1 行を変えて apply。
  EOT
  type        = number
  default     = 300
}
