#!/usr/bin/env bash
# Render build script

set -o errexit  # Exit on error

echo "🔧 Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

echo "📦 Running database migrations..."
# Flask-Migrateを使用してデータベースをマイグレーション
# Render Worker service doesn't auto-generate SECRET_KEY or DATABASE_URL, so provide temporary ones for build
if [ -z "$SECRET_KEY" ]; then
    export SECRET_KEY="temporary-build-key-$(date +%s)"
    echo "⚙️ Using temporary SECRET_KEY for build..."
fi

if [ -z "$DATABASE_URL" ]; then
    # Use SQLite for Worker service build (PostgreSQL will be available at runtime)
    export DATABASE_URL="sqlite:///./instance/build.db"
    echo "⚙️ Using temporary SQLite DATABASE_URL for build (Worker service)..."
fi

# マイグレーション実行
# （Web service: Render自動設定で実行 / Worker service: dummy URLで Flask初期化のみ実行）
flask db upgrade
echo "✅ Database initialization completed"

echo "✅ Build completed successfully!"
