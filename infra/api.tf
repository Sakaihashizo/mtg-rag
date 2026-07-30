# API Gateway（2026-07-28）
#
# HTTP API（v2）を使う。REST API（v1）より安く（$1.00 vs $3.50 / 100万リクエスト）、
# Lambda プロキシ統合には機能も十分。デモ流量では実質ゼロ円。
#
# usage plan（API キー毎クォータ）は PHASE2 §4 の「上限を切った従量は実質固定費」の
# 実装だが、**それは B2B 成立時の話**＝デモ段階では要らない（キー無しで公開し、
# スロットリングだけ掛ける）。必要になったら v1 へ移すか、Lambda 側で数える。

resource "aws_apigatewayv2_api" "main" {
  count = var.lambda_image_tag == "" ? 0 : 1

  name          = "${var.project}-api"
  protocol_type = "HTTP"

  # フロントは CloudFront から配られる＝別オリジンになるので CORS が要る
  cors_configuration {
    allow_origins = ["*"] # デモは公開前提。B2B 化するときにドメインで絞る
    allow_methods = ["GET", "POST", "OPTIONS"]
    allow_headers = ["content-type"]
    max_age       = 3600
  }

  tags = { Name = "${var.project}-api" }
}

resource "aws_apigatewayv2_integration" "lambda" {
  count = var.lambda_image_tag == "" ? 0 : 1

  api_id                 = aws_apigatewayv2_api.main[0].id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api[0].invoke_arn
  payload_format_version = "2.0" # Mangum が読む形式（deploy/lambda_handler.py）
  timeout_milliseconds   = 30000
}

# 全パスを Lambda へ流す（ルーティングは FastAPI 側が持つ＝
# 経路の定義を二箇所に分けない）
resource "aws_apigatewayv2_route" "proxy" {
  count = var.lambda_image_tag == "" ? 0 : 1

  api_id    = aws_apigatewayv2_api.main[0].id
  route_key = "ANY /{proxy+}"
  target    = "integrations/${aws_apigatewayv2_integration.lambda[0].id}"
}

resource "aws_apigatewayv2_stage" "default" {
  count = var.lambda_image_tag == "" ? 0 : 1

  api_id      = aws_apigatewayv2_api.main[0].id
  name        = "$default"
  auto_deploy = true

  # 暴走とレート事故の歯止め（DDoS 対策の一次防衛。Shield Standard は自動・無料）。
  # 2026-07-31 に 10/20 → 2/5 へ引き下げ（README に URL を公開した日の蓋の締め直し）:
  # 人力のデモ利用は 2 req/s に届かない＝正規利用者の速度で天井を切る。
  # スクリプトによる全力連打の理論天井が約 1/5（Lambda+Nova 費で日額百ドル級→数十ドル級）に。
  default_route_settings {
    throttling_burst_limit = 5
    throttling_rate_limit  = 2
  }

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api[0].arn
    format = jsonencode({
      requestId = "$context.requestId"
      status    = "$context.status"
      path      = "$context.path"
      latency   = "$context.responseLatency"
    })
  }
}

resource "aws_cloudwatch_log_group" "api" {
  count             = var.lambda_image_tag == "" ? 0 : 1
  name              = "/aws/apigateway/${var.project}"
  retention_in_days = 14
}

# API Gateway が Lambda を呼べるようにする
resource "aws_lambda_permission" "api" {
  count = var.lambda_image_tag == "" ? 0 : 1

  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api[0].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.main[0].execution_arn}/*/*"
}
