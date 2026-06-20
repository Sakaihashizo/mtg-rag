# MTG RAG 用 PostgreSQL イメージ
# pgvector/pgvector:pg18 をベースに pg_store_plans（PGDG apt に PG18 版が無いため
# ソースビルド）を追加する。pg_stat_statements / auto_explain は base に同梱済み。
# 観測性の有効化（shared_preload_libraries 等）は docker-compose.yml の command で行う。
FROM pgvector/pgvector:pg18

USER root
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        build-essential postgresql-server-dev-18 git ca-certificates; \
    git clone --depth 1 https://github.com/ossc-db/pg_store_plans.git /tmp/psp; \
    cd /tmp/psp; \
    make USE_PGXS=1; \
    make USE_PGXS=1 install; \
    cd /; rm -rf /tmp/psp; \
    apt-get purge -y --auto-remove build-essential postgresql-server-dev-18 git; \
    rm -rf /var/lib/apt/lists/*
USER postgres
