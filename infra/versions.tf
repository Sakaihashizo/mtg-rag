# Terraform 本体とプロバイダのバージョン固定（2026-07-28）
#
# なぜ固定するか: プロバイダが上がると引数の意味が変わることがある。
# 「今日 plan が通った構成が明日も同じ」を保証するのが state と並ぶ再現性の柱。
#
# state はローカルファイル（infra/terraform.tfstate）。
# 一人プロジェクトでは S3 バックエンドは過剰＝必要になったら backend ブロックを足す。

terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # Aurora Serverless v2 の min_capacity = 0（scale-to-zero）と
      # seconds_until_auto_pause を扱える世代が要る
      version = ">= 5.90"
    }
  }
}

provider "aws" {
  region = var.region

  # 全リソースに共通タグを付ける。destroy の取りこぼし確認と、
  # 請求の内訳をこのプロジェクト単位で追うために効く
  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
    }
  }
}
