from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from flask_login import UserMixin

db = SQLAlchemy()


class User(db.Model, UserMixin):
    __tablename__ = "user"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(100), nullable=False)
    is_admin = db.Column(db.Boolean, nullable=False, default=False)

    def __repr__(self):
        return f"<User {self.username}>"


class Conversation(db.Model):
    __tablename__ = "conversation"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(100), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    is_pinned = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    summary = db.Column(db.Text, nullable=True)  # ✅ AIによる会話要約を保存

    user = db.relationship("User", backref=db.backref("conversations", lazy=True))

    def __repr__(self):
        return f"<Conversation {self.title}>"


class Message(db.Model):
    __tablename__ = "message"
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.Text, nullable=False)
    sender = db.Column(db.String(10), nullable=False)
    conversation_id = db.Column(db.Integer, db.ForeignKey("conversation.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    conversation = db.relationship("Conversation", backref=db.backref("messages", lazy=True))

    def __repr__(self):
        return f"<Message {self.sender} {self.content[:20]}>"


class Announcement(db.Model):
    __tablename__ = "announcement"
    id = db.Column(db.Integer, primary_key=True)
    message = db.Column(db.Text, nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Announcement {self.id}>"


class ResearchJob(db.Model):
    """
    Deep Research ジョブの状態管理
    """
    __tablename__ = "research_job"
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.String(100), unique=True, nullable=False)  # RQジョブID
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    conversation_id = db.Column(db.Integer, db.ForeignKey("conversation.id"), nullable=True)
    query = db.Column(db.Text, nullable=False)  # 元のクエリ
    status = db.Column(db.String(20), nullable=False, default="pending")  # pending, decomposing, searching, synthesizing, completed, failed
    phase = db.Column(db.String(20), nullable=True)  # 現在のフェーズ
    progress_message = db.Column(db.String(200), nullable=True)  # 進捗メッセージ
    result_report = db.Column(db.Text, nullable=True)  # 最終レポート（Markdown）
    sub_queries = db.Column(db.Text, nullable=True)  # JSON形式のサブクエリリスト
    sources_count = db.Column(db.Integer, nullable=True)  # 収集したソース数
    error_message = db.Column(db.Text, nullable=True)  # エラーメッセージ
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship("User", backref=db.backref("research_jobs", lazy=True))
    conversation = db.relationship("Conversation", backref=db.backref("research_jobs", lazy=True))

    def __repr__(self):
        return f"<ResearchJob {self.task_id} status={self.status}>"

