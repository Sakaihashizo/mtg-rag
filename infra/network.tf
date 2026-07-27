# ネットワーク（2026-07-28）— C 案の肝は「繋ぎ装置を置かないこと」
#
# 設計の正本: docs/ai/architecture_serverless.md「本番アーキテクチャ（C案・Data API 型）」
#
# ここで**意図的に作らないもの**（作ると月額が跳ねる。2026-07-11 の料金表精査の結論）:
#   - NAT Gateway            … Lambda が VPC 外なので不要
#   - Interface VPC エンドポイント … 稼働と無関係に毎時課金（3 本で月 $30.66）
#   - RDS Proxy              … auto-pause と非互換＋最低 8 ACU の課金床（月 $146）
# これらを置かないことで、放置時の固定費が月 $232 → $1 前後になる。
#
# 作るもの（VPC・サブネット・SG・ルートテーブルはいずれも無料）:
#   - VPC と 2 つの private サブネット（Aurora は 2 AZ 以上を要求する）
#   - Aurora 用のセキュリティグループ
#   - **S3 Gateway エンドポイント**（Gateway 型は公式に無料。Interface 型と違い時間課金なし）
#     用途: データ搬入で aws_s3 拡張が S3 から CSV を読む経路
#     （Data API はレスポンス 1MiB 上限なので大量搬入には使わない）

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "main" {
  cidr_block           = "10.20.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "${var.project}-vpc" }
}

# Aurora は最低 2 つの AZ にまたがるサブネットグループを要求する。
# インターネットへの経路は持たせない（private のまま＝Data API は AWS 内部を通る）
resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index)
  availability_zone = data.aws_availability_zones.available.names[count.index]

  tags = { Name = "${var.project}-private-${count.index}" }
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.project}-private" }
}

resource "aws_route_table_association" "private" {
  count          = length(aws_subnet.private)
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# S3 Gateway エンドポイント（無料）。aws_s3 拡張での搬入に要る
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]

  tags = { Name = "${var.project}-s3-gateway" }
}

# Aurora のセキュリティグループ。
# Data API 経由のアクセスは AWS のサービス側から来るためインバウンド規則は不要
# （Lambda は VPC 外＝直接 TCP で繋がない）。将来 TCP 直結（B 案）へ戻すときだけ
# ここに ingress を足す＝そのときは architecture_serverless.md の B 案の節を読む。
resource "aws_security_group" "aurora" {
  name        = "${var.project}-aurora"
  description = "Aurora Serverless v2 (Data API only; no inbound TCP by design)"
  vpc_id      = aws_vpc.main.id

  egress {
    description = "allow all outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.project}-aurora" }
}
