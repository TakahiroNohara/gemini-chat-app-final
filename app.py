from googleapiclient.discovery import build
import os
import google.generativeai as genai
from flask import Flask, render_template, request, jsonify, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_bcrypt import Bcrypt
from dotenv import load_dotenv

# 初期設定
load_dotenv()
app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///database.db"

# データベース、認証、パスワードハッシュ化の初期化
db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"

# Gemini APIキーの設定
try:
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
except Exception as e:
    print(f"APIキーの設定でエラー: {e}")

# --- ウェブ検索を実行する関数 ---
def perform_web_search(query):
    """Google Custom Search APIを使ってウェブ検索を実行し、結果を整形して返す"""
    try:
        # .envファイルからAPIキーと検索エンジンIDを読み込む
        api_key = os.environ["GOOGLE_API_KEY"]
        search_engine_id = os.environ["SEARCH_ENGINE_ID"]
        
        # 検索サービスを構築
        service = build("customsearch", "v1", developerKey=api_key)
        
        # 検索を実行（num=3で上位3件を取得）
        res = service.cse().list(q=query, cx=search_engine_id, num=3).execute()
        
        # 検索結果が存在するかチェック
        if 'items' not in res:
            return "ウェブ検索で関連する情報が見つかりませんでした。"
        
        # 結果を分かりやすく整形
        search_results_text = ""
        for item in res['items']:
            search_results_text += f"タイトル: {item['title']}\n"
            search_results_text += f"スニペット: {item.get('snippet', 'N/A')}\n"
            search_results_text += f"URL: {item['link']}\n---\n"
        
        return search_results_text.strip()

    except Exception as e:
        print(f"ウェブ検索でエラーが発生しました: {e}")
        return "ウェブ検索中にエラーが発生しました。"

# --- データベースモデル定義 ---

# ユーザーモデル
class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(20), unique=True, nullable=False)
    password = db.Column(db.String(60), nullable=False)
    messages = db.relationship('Message', backref='author', lazy=True)

# 会話履歴モデル
class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.Text, nullable=False)
    sender = db.Column(db.String(10), nullable=False) # 'user' or 'model'
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

# Flask-Loginがユーザー情報をロードするための関数
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- ルート (URLの定義) ---

# チャットページ (ログイン必須)
@app.route("/")
@login_required
def index():
    messages = Message.query.filter_by(user_id=current_user.id).all()
    return render_template("index.html", messages=messages)

# チャットAPI (ログイン必須)
@app.route("/chat", methods=["POST"])
@login_required
def chat():
    user_message = request.json["message"]
    model_name = request.json.get("model", "models/gemini-flash-latest")
    web_search = request.json.get("web_search", False)

    # ユーザーのメッセージをDBに保存
    db.session.add(Message(content=user_message, sender='user', user_id=current_user.id))
    
    try:
        model = genai.GenerativeModel(model_name)
        ai_reply = ""

        # ★★★ ここからが変更点 ★★★
        if web_search:
            print(f"ウェブ検索を実行します: '{user_message}'") # ターミナルで確認用
            
            # ステップ3で作成した関数を呼び出してウェブ検索を実行
            search_results = perform_web_search(user_message)
            
            # Geminiに渡すための新しいプロンプトを作成
            prompt_for_gemini = f"""ウェブ検索の結果は以下の通りです。
---
{search_results}
---
上記の情報に**基づいて**、次の質問に簡潔に答えてください: {user_message}"""
            
            # 検索結果を含むプロンプトで回答を生成
            response = model.generate_content(prompt_for_gemini)
            ai_reply = response.text
        else:
            # ウェブ検索がオフの場合は、今まで通り直接質問を投げる
            response = model.generate_content(user_message)
            ai_reply = response.text
        # ★★★ 変更点ここまで ★★★

        # AIの返信をDBに保存
        db.session.add(Message(content=ai_reply, sender='model', user_id=current_user.id))
        db.session.commit()

        return jsonify({"reply": ai_reply})
    except Exception as e:
        db.session.rollback()
        print(f"エラー: {e}")
        return jsonify({"error": str(e)}), 500

# 新規登録ページ
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        
        if User.query.filter_by(username=username).first():
            return render_template("register.html", error="このユーザー名は既に使用されています。")

        hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
        new_user = User(username=username, password=hashed_password)
        db.session.add(new_user)
        db.session.commit()
        
        login_user(new_user)
        return redirect(url_for("index"))
    return render_template("register.html")

# ログインページ
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        user = User.query.filter_by(username=username).first()
        if user and bcrypt.check_password_hash(user.password, password):
            login_user(user)
            return redirect(url_for("index"))
        else:
            return render_template("login.html", error="ユーザー名またはパスワードが正しくありません。")
    return render_template("login.html")

# ログアウト
@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)