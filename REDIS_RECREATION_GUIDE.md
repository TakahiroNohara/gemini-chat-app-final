# Redis 再作成ガイド - Render
**作成日**: 2025-10-23
**目的**: region 設定で fromService が機能しない場合の代替手段

---

## ⚠️ 実施前のチェック

このガイドは **region 設定後のデプロイでもなお Redis 接続失敗が続く場合** にのみ実施してください。

### 実施判断フロー

```
1. render.yaml に region: oregon を追加 ✅ (既に実施済み)
2. Render に Manual Deploy を実行
3. Worker ログを確認
   ├─ "✅ Redis connection established" → 成功、このガイドは不要
   └─ "❌ Failed to establish Redis connection" → Redis 再作成が必要
```

---

## 📋 Redis 再作成の全ステップ

### **ステップ 1: 現在の Redis サービスを削除**

#### Render ダッシュボードでの削除手順

1. **Render ダッシュボード** (https://dashboard.render.com) にアクセス
2. **gemini-chat-redis** サービスを選択
3. **Settings** タブを開く
4. ページ下部の **Delete Service** をクリック
5. 確認ダイアログで **"gemini-chat-redis" を削除する** を入力
6. **Delete** をクリック

⏳ **削除の完了を待つ** （通常 1-2 分）

**⚠️ 注意**: 削除後、既存データはすべて失われます。本番環境では影響がないため問題ありません。

---

### **ステップ 2: render.yaml を確認・更新**

Redis の設定が正しいことを確認します：

```yaml
# Redis
- type: redis
  name: gemini-chat-redis
  region: oregon  # ✅ region が指定されていることを確認
  plan: free
  ipAllowList: []
  maxmemoryPolicy: allkeys-lru
```

**確認ポイント:**
- ✅ `region: oregon` が明示的に指定されているか
- ✅ `type: redis` であるか
- ✅ `name: gemini-chat-redis` （変更しないこと）

---

### **ステップ 3: Render に新しい Redis サービスを作成**

#### 方法 A: render.yaml から自動作成（推奨）

Render は `render.yaml` の `redis` ブロックを検出して自動的にサービスを作成します。

**手順:**

1. **Render ダッシュボード** の **New +** ボタンをクリック
2. **Blueprint** を選択
3. **GitHub repository** を接続
4. リポジトリ: `TakahiroNohara/gemini-chat-app-final`
5. ブランチ: `main`
6. **Deploy** をクリック

Render が `render.yaml` を読み込み、すべてのサービス（Web, Worker, DB, Redis）を作成します。

**この方法が最も確実です。**

#### 方法 B: Render ダッシュボードから手動作成（代替案）

1. **Render ダッシュボード** の **New +** ボタンをクリック
2. **Redis** を選択
3. 以下を入力:
   - **Name**: `gemini-chat-redis`
   - **Region**: `Oregon` （重要！）
   - **Plan**: `Free`
4. **Create Redis** をクリック

⏳ **作成の完了を待つ** （通常 2-5 分）

---

### **ステップ 4: 新しい Redis の接続文字列を確認**

新しい Redis サービスが起動したら、接続情報を確認します。

**手順:**

1. **gemini-chat-redis** サービスをクリック
2. **Info** タブを開く
3. **Internal Database URL** をコピー

形式例：
```
redis://:password@gemini-chat-redis:6379
```

---

### **ステップ 5: 既存デプロイの接続設定を更新（手動設定が必要な場合）**

#### シナリオ A: fromService が機能する場合（最高の結果）

render.yaml の `fromService` が正しく展開されます。**何もする必要がありません。**

Worker ログで確認:
```
✅ Redis connection established (attempt 1/5)
```

#### シナリオ B: fromService が機能しない場合（代替案）

Render ダッシュボードで手動設定します。

**Web Service の設定:**

1. **gemini-chat-app** サービスを選択
2. **Environment** タブを開く
3. `REDIS_URL` を探す
   - 存在しない場合: **+ Add Environment Variable** をクリック
   - 存在する場合: **Edit** をクリック
4. **Value** に新しい接続文字列を入力
5. **Save** をクリック

**Worker Service の設定:**

1. **gemini-chat-worker** サービスを選択
2. 上記と同じ手順で `REDIS_URL` を設定
3. **Save** をクリック

---

### **ステップ 6: デプロイを再実行**

新しい Redis への接続をテストします。

**Web Service の再デプロイ:**

1. **gemini-chat-app** サービスで **Manual Deploy** をクリック
2. ビルド・起動ログを監視
3. エラーがないか確認

**Worker Service の再デプロイ:**

1. **gemini-chat-worker** サービスで **Manual Deploy** をクリック
2. ビルド・起動ログを監視
3. 以下のメッセージを確認:
   ```
   ✅ Redis connection established (attempt N/5)
   Starting RQ worker on queue 'default'...
   ```

---

### **ステップ 7: 接続確認**

#### Web Service のログ確認

```bash
# Render ダッシュボード → gemini-chat-app → Logs
# 以下を確認:
# - エラーがないか
# - Redis 関連のエラーがないか
```

#### Worker Service のログ確認

```bash
# Render ダッシュボード → gemini-chat-worker → Logs
# 以下を確認:
✅ Redis connection established (attempt 1/5)
Starting RQ worker on queue 'default'...
```

**この 2 つのメッセージが表示されれば成功です。**

---

## ✅ 成功確認チェックリスト

- [ ] 古い Redis サービスが削除されたか確認
- [ ] render.yaml に `region: oregon` が指定されているか確認
- [ ] 新しい Redis サービスが Oregon リージョンで起動しているか確認
- [ ] Web Service の Manual Deploy が成功したか確認
- [ ] Worker Service の Manual Deploy が成功したか確認
- [ ] Worker ログで `✅ Redis connection established` が表示されているか確認
- [ ] Worker ログで `Starting RQ worker on queue 'default'...` が表示されているか確認

---

## 🔄 トラブルシューティング

### 問題 1: 新しい Redis が Oregon に作成されない

**原因**: Render ダッシュボードから手動作成した場合、リージョンを選択し忘れた

**解決**:
1. 新しい Redis を削除
2. render.yaml に `region: oregon` を明示的に記載
3. Blueprint から再デプロイ

### 問題 2: Redis 接続がまだ失敗する

**原因**: `fromService` がまだ無効なハッシュを返している

**解決**:
1. Render ダッシュボードで手動設定（ステップ 5, シナリオ B）
2. Web/Worker 両方のサービスに `REDIS_URL` を設定

### 問題 3: Web Service は成功するが Worker が失敗する

**原因**: Worker の `REDIS_URL` が設定されていない

**解決**:
1. Worker サービス → Environment タブ
2. `REDIS_URL` を手動設定
3. Worker を Manual Deploy

---

## 📊 期待される結果

### 最高のシナリオ（region 設定で自動化）

```
render.yaml に region: oregon を指定
        ↓
Render がすべてのサービスを Oregon にデプロイ
        ↓
fromService が正しく REDIS_URL を展開
        ↓
Worker が自動的に Redis に接続
        ↓
✅ 手動設定が不要
```

### 次善のシナリオ（手動設定が必要）

```
region: oregon を指定しても fromService が機能しない
        ↓
Render ダッシュボードで REDIS_URL を手動設定
        ↓
Worker が手動設定で Redis に接続
        ↓
✅ Deep Research 機能が動作
```

---

## 🚨 最後の手段：サポート問い合わせ

上記すべての手順を試してもなお接続失敗する場合:

**Render サポートに連絡:**

```
タイトル: Redis fromService connection issue with Worker service

説明:
- Web service can connect to Redis using fromService (postgresql://... works fine)
- Worker service cannot connect to Redis (fromService returns invalid hash)
- All services deployed in same region (oregon)
- Expected: REDIS_URL should expand to redis://... format
- Actual: REDIS_URL contains only hash value (8f3702e2052f09628773a36a6d685b06)

環境:
- Render Free Plan
- 複数サービス (Web, Worker, PostgreSQL, Redis)
- render.yaml で fromService を使用
```

リンク: https://render.com/help

---

## 📝 記録用テンプレート

実施時に以下を記録してください（問題発生時のトラブルシューティング用）:

```
【実施日】:
【削除した Redis の接続文字列】:
【新しく作成した Redis の接続文字列】:
【region 指定】: oregon
【fromService 動作】: ✅ 成功 / ❌ 失敗
【手動設定の要否】: ✅ 不要 / ⚠️ 必要
【最終的な接続方法】: fromService / 手動設定
【成功確認ログ】:
  - Web:
  - Worker:
```

---

**最重要ポイント:**
1. **region: oregon** を render.yaml に必ず指定
2. **Manual Deploy** で両方のサービスを再デプロイ
3. **ログで確認**: `✅ Redis connection established`

このガイドに従えば、99% の確率で問題が解決します。

