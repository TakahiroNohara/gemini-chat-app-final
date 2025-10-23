# Gemini への引き継ぎプロンプト - Render Redis 接続問題

## 📋 タスク概要

Flask + RQ (Redis Queue) で実装した Deep Research 機能の Worker サービスを Render にデプロイ中。
Worker サービスが Redis に接続できず失敗している問題の解決支援をお願いします。

## 🔴 現在のエラー

```
2025-10-23 00:39:33,689 - __main__ - ERROR - ❌ Invalid REDIS_URL format: REDIS_URL must start with redis://, rediss://, or unix://. Got: 8f3702e2052f09628773a36a6d685b06
2025-10-23 00:39:33,689 - __main__ - ERROR - Full URL: 8f3702e2052f09628773a36a6d685b06
```

## 📊 プロジェクト構成

```
Flask Application (Web Service)
├── app/__init__.py: Flask 初期化、RQ キュー設定
├── services/tasks.py: バックグラウンドタスク定義
├── run_worker.py: RQ Worker スタートアップスクリプト（本課題の主体）
└── render.yaml: Render デプロイ設定

Render Services:
- gemini-chat-app (Web, PostgreSQL 接続 ✅)
- gemini-chat-worker (Web type, Python runtime)
  └─ この Worker が Redis に接続できず失敗中
- gemini-chat-db (PostgreSQL)
- gemini-chat-redis (Redis)
```

## 🔍 問題分析

### 根本原因
`render.yaml` で以下のように設定していますが：

```yaml
# Worker Service envVars
- key: REDIS_URL
  fromService:
    type: redis
    name: gemini-chat-redis
    property: connectionString
```

Render が返す値が不正：
- **期待値:** `redis://:password@host:6379` など
- **実際:** `8f3702e2052f09628773a36a6d685b06` (ハッシュのみ)

### 実装済みの改善（run_worker.py）

```python
def validate_redis_url_format(redis_url: Optional[str]) -> Tuple[bool, Optional[str]]:
    """Redis URL 形式を検証"""
    if not (redis_url.startswith("redis://") or redis_url.startswith("rediss://") or redis_url.startswith("unix://")):
        return False, f"Invalid format: {redis_url[:50]}"
    return True, None

def connect_to_redis(redis_url: str, max_retries: int = 5, initial_backoff: int = 2) -> Optional[Redis]:
    """リトライロジック付きで Redis に接続"""
    # 設定エラー（無効な URL）は即座に検出
    # ネットワークエラーは指数バックオフで リトライ
```

## 🎯 必要な支援

### 質問 1: Render の fromService について

**Render 公式ドキュメント調査:**
- `fromService` による環境変数展開は Free プランで対応しているか？
- Redis サービスの `connectionString` プロパティは存在するか？
- 他のプロパティ（例：`internalConnectionString`）を試すべきか？

### 質問 2: 代替ソリューション

現在の状況から以下の選択肢が考えられます：

**A) render.yaml の修正**
```yaml
# 案1: 別のプロパティを試す
fromService:
  type: redis
  name: gemini-chat-redis
  property: internalConnectionString  # または他の名前

# 案2: 直接 URL を環境変数として設定
- key: REDIS_URL
  value: "redis://..."  # ただし本番ではセキュリティリスク
```

**B) build.sh で環境変数を展開**
```bash
# Render が提供するサービス ID をもとに URL を構築
if [ -z "$REDIS_URL" ]; then
    REDIS_HOST="${REDIS_INTERNAL_HOST:-gemini-chat-redis}"
    REDIS_PORT="${REDIS_PORT:-6379}"
    REDIS_PASSWORD="${REDIS_PASSWORD:-}"
    export REDIS_URL="redis://:${REDIS_PASSWORD}@${REDIS_HOST}:${REDIS_PORT}"
fi
```

**C) run_worker.py で Render API を呼び出し**
```python
# Render の内部 API で Redis サービス情報を取得？
# （このアプローチの実現可能性を確認してください）
```

### 質問 3: 即座の解決策

ユーザーが Render ダッシュボードで手動設定する方法：

1. Redis サービスの接続文字列を確認するための正確な手順
2. Worker サービスに REDIS_URL を設定するステップ
3. 設定後の検証方法

## 📁 関連ファイル

- **render.yaml:** Render デプロイ設定
  - Line 66-71: Worker の REDIS_URL 設定
  - Line 105-109: Web の REDIS_URL 設定（Web は DATABASE_URL が自動設定されるため成功）

- **run_worker.py:** Worker スタートアップ（詳細なエラーハンドリング実装済み）
  - Line 21-29: URL 形式検証
  - Line 31-73: リトライロジック

- **REDIS_SETUP_INSTRUCTIONS.md:** ユーザー向けの手動設定ガイド

## ✅ 追加情報

- **Web サービス:** ✅ 正常に動作（PostgreSQL 接続成功）
- **Worker ビルド:** ✅ 成功（build.sh で SECRET_KEY・DATABASE_URL 対応済み）
- **Worker 実行:** ❌ Redis 接続失敗（エラーメッセージは正確に出力されている）

## 🚀 期待される成果物

Gemini の調査結果に基づいて以下を提供してください：

1. **Render の `fromService` 仕様の確認**
   - 正しい YAML 構文
   - プロパティ名の確認
   - Free プランでの制限の有無

2. **推奨ソリューション**
   - コード修正が必要な場合、具体的な実装例
   - Render 設定変更の場合、ステップバイステップガイド

3. **代替案の評価**
   - 各案のメリット・デメリット
   - 本番環境での推奨パターン

4. **トラブルシューティング**
   - 今後同様の問題が発生した場合の診断方法
   - Render サービス間の接続確認方法

---

## 📞 連絡先

このプロンプトについて質問があれば、プロジェクト内の REDIS_SETUP_INSTRUCTIONS.md 参照。

**重要:** Render は積極的に変更されているため、最新の公式ドキュメントの確認をお願いします。
