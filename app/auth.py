# app/auth.py
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_user, logout_user, login_required
from flask_bcrypt import Bcrypt
from .models import db, User

auth_bp = Blueprint("auth", __name__)
bcrypt = Bcrypt()

@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()
        user = User.query.filter_by(username=username).first()
        if user and bcrypt.check_password_hash(user.password, password):
            login_user(user)
            if user.is_admin:
                return redirect(url_for("core.admin_dashboard"))
            else:
                return redirect(url_for("core.chat"))
        else:
            return render_template("login.html", error="ユーザー名またはパスワードが違います。")
    return render_template("login.html")

@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()
        if not username or not password:
            return render_template("register.html", error="ユーザー名とパスワードは必須です。")
        if User.query.filter_by(username=username).first():
            return render_template("register.html", error="そのユーザー名は既に使用されています。")
        hashed = bcrypt.generate_password_hash(password).decode("utf-8")
        u = User(username=username, password=hashed, is_admin=False)
        db.session.add(u)
        db.session.commit()
        flash("登録しました。ログインしてください。", "success")
        return redirect(url_for("auth.login"))
    return render_template("register.html")

@auth_bp.route("/logout", methods=["GET"])
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))

@auth_bp.route("/admin_login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()
        user = User.query.filter_by(username=username).first()
        if user and user.is_admin and bcrypt.check_password_hash(user.password, password):
            login_user(user)
            return redirect(url_for("core.admin_dashboard"))
        else:
            return render_template("admin_login.html", error="管理者権限がないか、認証に失敗しました。")
    return render_template("admin_login.html")

# Blueprintエクスポート
__all__ = ["auth_bp", "bcrypt"]



