#!/usr/bin/env bash
# Render build script

set -o errexit  # Exit on error

echo "🔧 Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

echo "📦 Running database migrations..."
# Flask-Migrateを使用してデータベースをマイグレーション
# Render Worker service doesn't auto-generate SECRET_KEY, so provide temporary one for build
if [ -z "$SECRET_KEY" ]; then
    export SECRET_KEY="temporary-build-key-$(date +%s)"
    echo "⚙️ Using temporary SECRET_KEY for build..."
fi

# DATABASE_URL が設定されている場合のみマイグレーション実行
# （Web service: Renderが自動設定 / Worker service: ビルド時は未設定）
if [ -n "$DATABASE_URL" ]; then
    flask db upgrade
    echo "✅ Database migrations completed"
else
    echo "⊙ DATABASE_URL not set - skipping migrations (Web service will handle this)"
fi

echo "✅ Build completed successfully!"
