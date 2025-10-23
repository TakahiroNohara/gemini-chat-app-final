# Redis URL 手動設定ガイド - Render デプロイ

## 🔍 問題の状況

Render Worker サービスが以下のエラーで失敗しています：

```
ERROR - Invalid REDIS_URL format: REDIS_URL must start with redis://, rediss://, or unix://. Got: 8f3702e2052f09628773a36a6d685b06
Full URL: 8f3702e2052f09628773a36a6d685b06
```

**根本原因:** `render.yaml` の `fromService` が Redis の接続文字列ではなく、ハッシュ値のみを返しています。

## ✅ 解決手順

### ステップ 1: Redis サービスの接続情報を取得

1. **Render ダッシュボード** にアクセス
2. **gemini-chat-redis** サービスを選択
3. **Info** タブを開く
4. 以下のいずれかをコピー：
   - **Internal Database URL** (Render 内部からのアクセス推奨)
   - **External Database URL** (外部からのアクセスが必要な場合)

**例の形式:**
```
redis://:your-password@gemini-chat-redis:6379
または
rediss://default:your-password@your-redis-host.render.com:16379
```

### ステップ 2: Worker サービスに環境変数を設定

1. **gemini-chat-worker** サービスを選択
2. **Environment** タブを開く
3. **+ Add Environment Variable** をクリック
4. 以下を入力：
   - **Key:** `REDIS_URL`
   - **Value:** [ステップ1でコピーした接続文字列]
5. **Save** をクリック

### ステップ 3: デプロイを再実行

1. **gemini-chat-worker** サービスを開いた状態で
2. ページ上部の **Manual Deploy** ボタンをクリック
3. ビルドと起動の進行状況を確認

## 🎯 成功の確認

ログで以下の メッセージが表示されればOK：

```
2025-10-23 00:39:33,689 - __main__ - INFO - Connecting to Redis: redis://...
✅ Redis connection established (attempt 1/5)
Starting RQ worker on queue 'default'...
```

## 📋 トラブルシューティング

| エラー | 原因 | 対策 |
|--------|------|------|
| `redis.exceptions.AuthenticationError` | パスワードが間違っている | Redis サービスで正しいパスワードを確認 |
| `ConnectionError` | Redis サービスがダウン | Redis サービスが起動しているか確認 |
| `Connection refused` | ホスト/ポートが間違っている | 接続文字列をコピーし直す |

## 🔧 環境変数のカスタマイズ（オプション）

Worker の起動動作を調整する場合：

```
RQ_MAX_RETRIES=10        # リトライ回数（デフォルト: 5）
RQ_RETRY_BACKOFF=5       # 初期バックオフ秒数（デフォルト: 2）
```

## 📝 今後の改善

`fromService` が機能しない原因として以下が考えられます：

1. **Render の既知の問題** - Free プラン での fromService 制限
2. **YAML 構文** - 最新の Render ドキュメントで確認が必要
3. **サービス間のリンク設定** - Render ダッシュボードで明示的なリンク設定が必要

**推奨:** 本番環境では `fromService` ではなく、Render ダッシュボードで手動設定するか、環境変数経由での設定を推奨します。

---

**コマンド例（Render CLI を使う場合）:**
```bash
render env:set REDIS_URL="redis://:password@host:6379" -s gemini-chat-worker
```
