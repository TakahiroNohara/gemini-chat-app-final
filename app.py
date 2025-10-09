import os
import google.generativeai as genai
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

try:
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
except Exception as e:
    print(f"APIキーの設定でエラー: {e}")
    # アプリを続行させない
    # exit() 

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/chat", methods=["POST"])
def chat():
    message = request.json["message"]
    model_name = request.json.get("model", "gemini-1.5-flash-latest")

    try:
        model = genai.GenerativeModel(model_name)
        response = model.generate_content(message)
        return jsonify({"reply": response.text})
    except Exception as e:
        # エラー内容をターミナルに表示
        print(f"エラー: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True)
