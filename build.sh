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
    echo "⚙️ Using temporary SECRET_KEY for migrations..."
fi
flask db upgrade

echo "✅ Build completed successfully!"
