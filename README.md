# ドライブ先提案アプリ

## セットアップ手順

1. 必要なライブラリをインストール
   ```
   pip install -r requirements.txt
   ```

2. APIキーを設定
   ```
   cp .streamlit/secrets.toml.example .streamlit/secrets.toml
   ```
   `.streamlit/secrets.toml` を開いて、実際のAPIキーを入力してください。

   - **GOOGLE_MAPS_API_KEY(必須)**: Google Cloud Consoleで以下を有効化して取得
   - Places API (New) または Places API
   - **GEMINI_API_KEY(必須)**: Google AI Studioで取得
   - ※検索結果の厳選、ランキング化、予算感の推測、おすすめ理由の生成を行う中核機能として必須になります。
3. 起動
   ```
   streamlit run app.py
   ```

## 動作の注意点(必ず読んでください)

- **口コミ増加率について**: このアプリを実際に毎日使う(検索する)ことでデータが蓄積され、
  7日分たまって初めて「増加率」が表示されます。使い始めの1週間は「データ蓄積中」と出るのが正常です。
- **ホットペッパーAPIの検索範囲**: 最大3kmまでのため、遠方の店舗は予算情報が取得できないことがあります。
- **データの保存先**: `drive_app_data.db`というSQLiteファイルに保存されます。Streamlit Cloudの
  無料枠で運用する場合、再デプロイ時にリセットされる可能性があるので、本格運用する場合は
  外部DB(Supabase等)への切り替えを検討してください。
- **到着時刻の見積もり**: 現状は平均時速30km/hで簡易計算しています。より正確にしたい場合は
  Google Distance Matrix APIに差し替えてください(`estimate_arrival`関数を修正)。
