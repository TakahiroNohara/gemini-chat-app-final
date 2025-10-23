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

# Check if this is a Worker service build by looking at START_COMMAND or service name
# Worker service has startCommand: "python run_worker.py"
# Web service has startCommand: "gunicorn -c gunicorn.conf.py wsgi:app"
if [ "$RUN_COMMAND" = "python run_worker.py" ] || [ "$RUN_WORKER" = "true" ]; then
    # Worker service build: skip migrations (they'll run via Web service only)
    echo "⊙ Skipping database migrations (Worker build phase)"
elif [ -n "$DATABASE_URL" ]; then
    # Web service build: run migrations
    echo "📦 Running database migrations with configured DATABASE_URL..."
    flask db upgrade
    echo "✅ Database migrations completed"
else
    # No DATABASE_URL and not a worker: skip migrations
    echo "⊙ Skipping database migrations (no DATABASE_URL)"
fi

echo "✅ Build completed successfully!"
