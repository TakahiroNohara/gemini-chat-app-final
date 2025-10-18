#!/usr/bin/env bash
# Render build script

set -o errexit  # Exit on error

echo "🔧 Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

echo "📦 Running database migrations..."
# Flask-Migrateを使用してデータベースをマイグレーション
flask db upgrade

echo "✅ Build completed successfully!"
