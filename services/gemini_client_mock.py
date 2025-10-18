# services/gemini_client_mock.py
# 一時的なモッククライアント（開発・テスト用）
import time
from typing import List, Dict, Any, Tuple

class GeminiFallbackError(Exception):
    pass

class GeminiClient:
    def __init__(self, primary_model: str, fallback_model: str, api_key: str = None, api_version: str = None):
        self.primary_model = primary_model
        self.fallback_model = fallback_model
        self.api_key = api_key
        self.api_version = api_version or "v1"
        print(f"[MOCK] GeminiClient initialized with {primary_model}")

    def chat(self, messages: List[Dict[str, str]], user_message: str, requested_model: str = "") -> Tuple[str, str]:
        """モックレスポンスを返す"""
        time.sleep(0.5)  # 遅延をシミュレート

        # ユーザーメッセージに応じたモックレスポンス
        if "こんにちは" in user_message or "hello" in user_message.lower():
            reply = "こんにちは！Gemini（モック版）です。現在、実際のAPIに接続できないため、テスト用のレスポンスを返しています。"
        elif "天気" in user_message:
            reply = "申し訳ございませんが、現在Gemini APIに接続できないため、天気情報を取得できません。これはモックレスポンスです。"
        else:
            reply = f"あなたのメッセージ「{user_message}」を受け取りました。\n\n現在、Gemini APIに接続できないため、これはテスト用のモックレスポンスです。実際のAPIキーと接続を確認してください。"

        return reply, self.primary_model

    def analyze_conversation(self, messages: List[Dict[str, str]]) -> Dict[str, Any]:
        """会話分析のモック"""
        time.sleep(0.3)
        return {
            "summary": "これはモック要約です。実際のGemini APIに接続すると、適切な要約が生成されます。",
            "model": self.primary_model
        }

    def summarize_with_citations(self, query: str, search_results: List[Dict[str, str]], requested_model: str = "") -> Dict[str, Any]:
        """検索結果要約のモック"""
        time.sleep(0.5)
        return {
            "answer": "モックレスポンス: 検索機能は現在利用できません。Gemini APIへの接続を確認してください。",
            "model": self.primary_model
        }
