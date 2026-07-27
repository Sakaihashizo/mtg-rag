# infra/ — Terraform によるインフラ定義

MTG RAG のクラウド構成（C 案・Data API 型）をコードで定義する。
設計の正本は `docs/ai/architecture_serverless.md`、工程表は `docs/ai/ROADMAP.md`、
検収項目は `docs/ai/deploy_checklist.md`。

## 何のためにコード化するのか

1. **消せる**。`terraform destroy` で作ったもの全部が消える＝**消し忘れによる課金の温床を断つ**
2. **押す前に見える**。`terraform plan` が「何が作られ・変わり・消えるか」を実行前に一覧で出す
3. **記録になる**。このディレクトリを読めば、何がどう繋がって存在しているかが分かる
   （コンソールで手作業すると、後から読み返すものが残らない）
4. 冪等——「あるべき姿」を書くので、何度実行しても同じ状態に収束する
   （このプロジェクトの導出列と同じ思想）

## 使い方

```bash
cd infra
terraform init      # 初回のみ。プロバイダを取得する（$0）
terraform plan      # 差分を見る。**何も作らない**（$0）
terraform apply     # 実際に作る。**ここで課金が始まる＝本人 GO 必須**
terraform destroy   # 全部消す（課金を止める確実な手段）
```

**家訓**: `apply` と `destroy` は外向きアクションなので、実行したら
WORKLOG に「本人 GO（時刻）」を明記する。

## ファイルの構成（予定）

| ファイル | 中身 |
|---|---|
| `versions.tf` | Terraform とプロバイダのバージョン固定 |
| `variables.tf` | リージョン・プロジェクト名などの入力 |
| `budget.tf` | **最初に置く**。予算アラート（検知器を先に） |
| `iam.tf` | Lambda 実行ロール・Aurora の S3 読み取りロール |
| `network.tf` | VPC・サブネット・SG（**繋ぎ装置は置かない**＝C 案の肝） |
| `aurora.tf` | Aurora Serverless v2（min 0 ACU・pgvector・Data API） |
| `lambda.tf` | Lambda（コンテナ）・ECR |
| `api.tf` | API Gateway（usage plan 込み） |
| `frontend.tf` | S3 + CloudFront |
| `outputs.tf` | 動く URL・DB エンドポイント等 |

## state ファイルについて

`terraform.tfstate` は「今どうなっているか」の記録。**これを失うと Terraform が現実を
見失う**（作ったものを認識できなくなる）ので消さないこと。
DB のエンドポイント等の機微情報を含むため **`.gitignore` 済み＝push しない**。

当面はローカルファイルで運用する。チーム開発なら S3 バックエンドに置くのが定石だが、
一人プロジェクトでは過剰（必要になったら `versions.tf` に backend を足す）。

## 現状

- [x] Terraform v1.15.8 を `~/bin` に導入（2026-07-26・チェックサム照合済み）
- [ ] **IAM 権限の拡張が先**。現行の鍵（IAM ユーザー `claude`）は Bedrock 専用で、
      iam / rds / lambda / budgets は**読み取りすら拒否**される（2026-07-26 実測）。
      「IAM を Terraform で作る」には IAM の権限が要る＝**最初の権限だけは人間が手で与える**。
- [ ] `.tf` の記述
- [ ] `plan` で内容確認（$0）→ 本人 GO →`apply`
