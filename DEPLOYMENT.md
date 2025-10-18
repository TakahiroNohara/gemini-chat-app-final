# Render デプロイガイド

このガイドでは、Gemini Chat AppをRenderにデプロイする手順を説明します。

## 事前準備

### 1. GitHubリポジトリの準備

```bash
# .envファイルから機密情報を削除（重要！）
# .env.exampleを参照して、APIキーなどは削除してください

# 変更をコミット
git add .
git commit -m "Prepare for Render deployment"
git push origin main
```

**重要:** `.env`ファイルに含まれるAPIキーは絶対にGitにプッシュしないでください！

### 2. 必要な情報を準備

以下の情報をRenderの環境変数として設定する必要があります:

- `GEMINI_API_KEY`: Google Gemini APIキー
- `GOOGLE_API_KEY`: Google Custom Search APIキー
- `GOOGLE_CSE_ID`: Custom Search Engine ID

## Renderでのデプロイ手順

### 方法1: render.yaml を使用（推奨）

1. **Renderにログイン**
   - https://render.com にアクセス
   - GitHubアカウントでサインイン

2. **New Blueprint Instance を作成**
   - Dashboard → "New" → "Blueprint"
   - GitHubリポジトリを選択
   - `render.yaml`が自動検出されます

3. **環境変数を設定**

   自動で以下のサービスが作成されます:
   - Web Service (gemini-chat-app)
   - PostgreSQL Database (gemini-chat-db)
   - Redis (gemini-chat-redis)

   **手動で設定が必要な環境変数:**
   - `GEMINI_API_KEY`: あなたのGemini APIキー
   - `GOOGLE_API_KEY`: あなたのGoogle Search APIキー
   - `GOOGLE_CSE_ID`: あなたのCustom Search Engine ID

4. **デプロイ実行**
   - "Apply" をクリック
   - ビルドとデプロイが自動的に開始されます

### 方法2: 手動セットアップ

#### Step 1: PostgreSQLデータベースを作成

1. Dashboard → "New" → "PostgreSQL"
2. Name: `gemini-chat-db`
3. Plan: Free または Starter
4. "Create Database" をクリック

#### Step 2: Redisを作成

1. Dashboard → "New" → "Redis"
2. Name: `gemini-chat-redis`
3. Plan: Free または Starter
4. "Create Redis" をクリック

#### Step 3: Web Serviceを作成

1. Dashboard → "New" → "Web Service"
2. GitHubリポジトリを接続
3. 以下の設定を入力:

**基本設定:**
- Name: `gemini-chat-app`
- Runtime: Python 3
- Build Command: `./build.sh`
- Start Command: `gunicorn -c gunicorn.conf.py wsgi:app`

**環境変数を追加:**

| Key | Value | 備考 |
|-----|-------|------|
| `SECRET_KEY` | (自動生成) | "Generate" ボタンを使用 |
| `FORCE_HTTPS` | `true` | HTTPS強制 |
| `SESSION_COOKIE_SECURE` | `true` | セキュアクッキー |
| `GEMINI_API_KEY` | (あなたのAPIキー) | 手動入力 |
| `GOOGLE_API_VERSION` | `v1beta` | |
| `DEFAULT_GEMINI_MODEL` | `gemini-2.5-flash` | |
| `FALLBACK_GEMINI_MODEL` | `gemini-2.5-pro` | |
| `USE_MOCK_GEMINI` | `false` | |
| `SEARCH_PROVIDER` | `google_cse` | |
| `GOOGLE_API_KEY` | (あなたのAPIキー) | 手動入力 |
| `GOOGLE_CSE_ID` | (あなたのCSE ID) | 手動入力 |
| `WEB_CONCURRENCY` | `2` | ワーカー数 |
| `DATABASE_URL` | (データベースから選択) | Internal Connection String |
| `REDIS_URL` | (Redisから選択) | Internal Connection String |

4. "Create Web Service" をクリック

## デプロイ後の確認

### 1. デプロイステータスを確認

- Renderダッシュボードで "Logs" タブを開く
- ビルドとデプロイのログを確認
- エラーがないかチェック

### 2. データベースマイグレーションを確認

ログに以下のメッセージが表示されることを確認:
```
📦 Running database migrations...
✅ Build completed successfully!
```

### 3. 管理者ユーザーを作成

デプロイ後、管理者ユーザーを作成する必要があります:

1. Renderダッシュボード → Web Service → "Shell" タブ
2. 以下のコマンドを実行:

```bash
python create_admin.py
```

3. プロンプトに従ってユーザー名とパスワードを入力

### 4. アプリケーションにアクセス

- Render が提供する URL (例: `https://gemini-chat-app.onrender.com`) にアクセス
- ログインページが表示されることを確認
- 作成した管理者アカウントでログイン

## トラブルシューティング

### ビルドエラー

**エラー:** `No module named 'psycopg2'`
- **解決策:** `requirements.txt` に `psycopg2-binary` が含まれているか確認

**エラー:** `SECRET_KEY environment variable must be set`
- **解決策:** 環境変数 `SECRET_KEY` を設定

### データベース接続エラー

**エラー:** `could not connect to server`
- **解決策:**
  1. PostgreSQL が正常に起動しているか確認
  2. `DATABASE_URL` 環境変数が正しく設定されているか確認
  3. Internal Connection String を使用しているか確認

### Redisエラー

- Redisが接続できない場合、アプリは自動的にメモリストレージにフォールバックします
- 機能は制限されますが、アプリは動作します
- ログで警告メッセージを確認: `Redis check failed -> fallback to memory`

### アプリケーションが起動しない

1. **ログを確認:**
   - Render Dashboard → Logs
   - エラーメッセージを確認

2. **環境変数を確認:**
   - すべての必須環境変数が設定されているか
   - APIキーが正しいか

3. **ビルドコマンドを確認:**
   - `build.sh` が実行可能か (`chmod +x build.sh`)
   - マイグレーションが成功したか

## 本番運用のヒント

### 1. 無料プランの制限

Renderの無料プランには以下の制限があります:
- 15分間アクセスがないとスリープ状態になる
- スリープ後の最初のアクセスは起動に時間がかかる
- 月間750時間まで

### 2. パフォーマンス最適化

有料プランにアップグレードすると:
- スリープなし
- より多くのCPU/メモリ
- 複数のワーカープロセス

### 3. モニタリング

- Renderダッシュボードでメトリクスを確認
- ログを定期的にチェック
- エラーアラートを設定

### 4. バックアップ

PostgreSQLデータベースは自動的にバックアップされますが:
- 重要なデータは別途バックアップを推奨
- データベースのエクスポート機能を使用

## 更新・再デプロイ

コードを更新した場合:

1. GitHubにプッシュ:
```bash
git add .
git commit -m "Update feature"
git push origin main
```

2. Renderで自動的に再デプロイが開始されます

手動で再デプロイする場合:
- Render Dashboard → "Manual Deploy" → "Deploy latest commit"

## サポート

問題が発生した場合:
1. このREADMEのトラブルシューティングセクションを確認
2. Renderのドキュメント: https://render.com/docs
3. プロジェクトのGitHub Issues

---

デプロイ成功を祈っています！ 🚀
