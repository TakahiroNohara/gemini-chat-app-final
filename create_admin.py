# create_admin.py
from app import create_app
from app.models import db, User
from flask_bcrypt import generate_password_hash

app = create_app()

with app.app_context():
    username = input("管理者ユーザー名: ").strip()
    password = input("パスワード: ").strip()

    if User.query.filter_by(username=username).first():
        print("⚠️ そのユーザー名は既に存在します。")
    else:
        hashed = generate_password_hash(password).decode('utf-8')
        admin = User(username=username, password=hashed, is_admin=True)
        db.session.add(admin)
        db.session.commit()
        print(f"✅ 管理者アカウント '{username}' を作成しました。")
