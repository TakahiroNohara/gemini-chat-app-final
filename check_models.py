import os
import google.generativeai as genai
from dotenv import load_dotenv

try:
    load_dotenv()
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("エラー: APIキーが設定されていません。")
        exit()

    genai.configure(api_key=api_key)

    print("\nチャットに利用可能なモデル一覧:")
    print("---------------------------------")
    found = False
    for m in genai.list_models():
      if 'generateContent' in m.supported_generation_methods:
        print(m.name)
        found = True

    if not found:
        print("利用可能なモデルが見つかりませんでした。")
    print("---------------------------------\n")

except Exception as e:
    print(f"エラーが発生しました: {e}")