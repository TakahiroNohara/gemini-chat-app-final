# (既存のコードは省略)

# チャットAPI (ログイン必須)
@app.route("/chat", methods=["POST"])
@login_required
def chat():
    user_message = request.json["message"]
    model_name = request.json.get("model", "models/gemini-flash-latest")
    web_search = request.json.get("web_search", False) # ★ここを追加

    # ユーザーのメッセージをDBに保存
    db.session.add(Message(content=user_message, sender='user', user_id=current_user.id))
    
    try:
        model = genai.GenerativeModel(model_name)
        
        # ★ウェブ検索機能のダミー実装
        ai_reply = ""
        if web_search:
            ai_reply += "【ウェブ検索を実行しました（ダミー）】\n"
            ai_reply += f"AI: 「{user_message}」についてウェブで検索し、回答を生成します。\n"
            # ここに実際の検索API呼び出しロジックと、その結果をGeminiに渡す処理が入る
            # 現時点では、Geminiに直接ユーザーメッセージを渡す
            response = model.generate_content(user_message)
            ai_reply += response.text
        else:
            response = model.generate_content(user_message)
            ai_reply = response.text

        # AIの返信をDBに保存
        db.session.add(Message(content=ai_reply, sender='model', user_id=current_user.id))
        db.session.commit()

        return jsonify({"reply": ai_reply})
    except Exception as e:
        db.session.rollback()
        print(f"エラー: {e}")
        return jsonify({"error": str(e)}), 500

# (既存のコードは省略)