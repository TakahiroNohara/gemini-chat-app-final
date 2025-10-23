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

# Check if DATABASE_URL is set by Render (Web service only)
# Worker service doesn't get DATABASE_URL during build phase
if [ -n "$DATABASE_URL" ]; then
    # Only run migrations if DATABASE_URL is properly set (Web service case)
    echo "📦 Running database migrations with configured DATABASE_URL..."
    flask db upgrade
    echo "✅ Database migrations completed"
else
    # Worker service build: skip migrations (they'll run via Web service)
    echo "⊙ Skipping database migrations (Worker build phase - no DATABASE_URL)"
fi

echo "✅ Build completed successfully!"
