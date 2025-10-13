import logging
from typing import List, Dict, Any, Optional
import google.generativeai as genai
from google.api_core.exceptions import InternalServerError

logger = logging.getLogger("gemini_chat_app.gemini")


class GeminiFallbackError(Exception):
    pass


def _cfg_safety():
    return {"temperature": 0.3, "top_p": 0.9, "top_k": 40}


class GeminiClient:
    def __init__(self, primary_model: str, fallback_model: str, api_key: Optional[str]):
        if not api_key:
            raise ValueError("GEMINI_API_KEY is required")
        genai.configure(api_key=api_key)
        self.primary_model = primary_model
        self.fallback_model = fallback_model

    def chat(self, messages: List[Dict[str, str]], user_message: str, requested_model: str = ""):
        order = [requested_model, self.primary_model, self.fallback_model] if requested_model else [self.primary_model, self.fallback_model]
        last_err = None
        for model_name in order:
            try:
                model = genai.GenerativeModel(model_name)
                conv = [{"role": m.get("role","user"), "parts": [m.get("content","")]} for m in (messages or [])]
                conv.append({"role": "user", "parts": [user_message]})
                resp = model.generate_content(conv, generation_config=_cfg_safety())
                text = (resp.text or "").strip()
                if not text:
                    raise InternalServerError("Empty response")
                return text, model_name
            except Exception as e:
                logger.warning(f"Gemini error on {model_name}: {e}")
                last_err = e
        raise GeminiFallbackError(str(last_err))

    def summarize_with_citations(self, query: str, search_results: List[Dict[str, Any]], requested_model: str = "") -> Dict[str, Any]:
        order = [requested_model, self.primary_model, self.fallback_model] if requested_model else [self.primary_model, self.fallback_model]
        sources = "\n".join(
            [f"[{i+1}] {r['title']} — {r['url']}\nSnippet: {r.get('snippet','')}" for i, r in enumerate(search_results)]
        )
        prompt = f"""
You are a careful research assistant. Summarize the web results in Japanese with numbered citations like [1], [2].
If sources conflict, mention it explicitly.

# Query
{query}

# Web Results
{sources}

# Output (Japanese):
- 5〜9行で要約
- 信頼できる情報のみ引用
- 最後に「参考: [1] タイトル, [2] タイトル...」
"""
        last_err = None
        for model_name in order:
            try:
                model = genai.GenerativeModel(model_name)
                resp = model.generate_content(prompt, generation_config=_cfg_safety())
                text = (resp.text or "").strip()
                if not text:
                    raise InternalServerError("Empty response")
                citations = [
                    {"index": i+1, "title": r["title"], "url": r["url"], "snippet": r.get("snippet","")}
                    for i, r in enumerate(search_results)
                ]
                return {"summary": text, "citations": citations, "used_model": model_name}
            except Exception as e:
                last_err = e
        raise GeminiFallbackError(str(last_err))

    def analyze_conversation(self, conversation: List[Dict[str, Any]]) -> Dict[str, Any]:
        order = [self.primary_model, self.fallback_model]
        text_dump = "\n".join([f"{m.get('role','user')}: {m.get('content','')}" for m in conversation])
        prompt = f"""
以下の会話を分析し、JSON形式で出力してください。
出力:
{{"topics":[], "keywords":[], "summary":"...", "action_items":[]}}
会話ログ:
{text_dump}
"""
        import json, re
        last_err = None
        for model_name in order:
            try:
                model = genai.GenerativeModel(model_name)
                resp = model.generate_content(prompt, generation_config=_cfg_safety())
                txt = (resp.text or "").strip()
                m = re.search(r"\{.*\}", txt, re.S)
                return json.loads(m.group(0)) if m else json.loads(txt)
            except Exception as e:
                last_err = e
        raise GeminiFallbackError(str(last_err))
