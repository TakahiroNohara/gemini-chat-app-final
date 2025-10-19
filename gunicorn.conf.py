# gunicorn.conf.py
import os

# ★ここだけ変更：Render が渡す $PORT を使う
bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"

workers = int(os.getenv("WEB_CONCURRENCY", "2"))  # CPUに応じて調整
worker_class = "sync"
timeout = 300  # 5分 - 長い検索・要約処理に対応
graceful_timeout = 300

log_dir = os.path.join(os.getenv("FLASK_INSTANCE_PATH", "instance"), "logs")
os.makedirs(log_dir, exist_ok=True)

errorlog = os.path.join(log_dir, "gunicorn-error.log")
accesslog = os.path.join(log_dir, "gunicorn-access.log")
loglevel = "info"
