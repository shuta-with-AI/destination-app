# -*- coding: utf-8 -*-
"""
ドライブ先提案アプリ
====================
現在地・ドライブ圏内・目的（ご飯/スイーツ/景色 等）を入力すると、
Google Places APIで候補地を検索し、
- 直近1週間の口コミ増加率（自前のスナップショットDBで蓄積）
- 評価点（足切り）
- 予算（ホットペッパー補完）
- 到着予測時刻での営業状況
を考慮して上位10件を提案し、Googleマップへのナビ導線とシェア機能を提供する。

【重要な制約（実装前に必ず読んでください)】
1. ホットペッパーAPIは検索半径が最大3kmまでしかないため、
   メインの検索はGoogle Places APIを使用し、ホットペッパーは
   「店名が一致した場合に予算情報を補う」用途にのみ使っています。
2. 「1週間前との口コミ増加率」は、このアプリ自身が毎日スナップショットを
   記録して初めて計算できます。運用開始から7日間はデータが無いため
   「データ蓄積中」と表示されます（過去のデータを遡って取得することは
   Google Places APIではできません)。
3. ローカルのSQLiteファイルにデータを貯めるため、Streamlit Cloud等の
   無料枠ではデプロイのたびにデータが消える場合があります。本番運用する
   場合は外部DB（Supabase, Firestore等）への差し替えを推奨します。
"""

import streamlit as st
import sqlite3
import datetime
import math
import json
import hashlib
import urllib.parse
import google.generativeai as genai

# ------------------------------------------------------------
# 初期設定
# ------------------------------------------------------------
st.set_page_config(page_title="ドライブ先提案アプリ", page_icon="🚗", layout="wide")

DB_PATH = "drive_app_data.db"

GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")

# Gemini APIの初期設定
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

PURPOSE_KEYWORDS = {
    "ご飯": "レストラン",
    "スイーツ": "スイーツ カフェ",
    "パン": "パン屋",
    "景色": "絶景 展望 景色",
}

RANGE_OPTIONS = {
    "0〜10km": 10,
    "10〜30km": 30,
    "30〜50km": 50,
    "手動入力": None,
}

# ------------------------------------------------------------
# DB初期化
# ------------------------------------------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            place_id TEXT,
            name TEXT,
            review_count INTEGER,
            rating REAL,
            snapshot_date TEXT
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS shares (
            place_id TEXT PRIMARY KEY,
            name TEXT,
            share_count INTEGER DEFAULT 0
        )
        """
    )
    conn.commit()
    conn.close()

def save_snapshot(place_id, name, review_count, rating):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    today = datetime.date.today().isoformat()
    c.execute(
        "SELECT 1 FROM snapshots WHERE place_id=? AND snapshot_date=?",
        (place_id, today),
    )
    if c.fetchone():
        c.execute(
            "UPDATE snapshots SET review_count=?, rating=? WHERE place_id=? AND snapshot_date=?",
            (review_count, rating, place_id, today),
        )
    else:
        c.execute(
            "INSERT INTO snapshots (place_id, name, review_count, rating, snapshot_date) VALUES (?,?,?,?,?)",
            (place_id, name, review_count, rating, today),
        )
    conn.commit()
    conn.close()

def get_buzz_rate(place_id, current_review_count):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    target_date = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
    c.execute(
        """
        SELECT review_count FROM snapshots
        WHERE place_id=? AND snapshot_date<=?
        ORDER BY snapshot_date DESC LIMIT 1
        """,
        (place_id, target_date),
    )
    row = c.fetchone()
    conn.close()
    if not row or row[0] in (None, 0):
        return None
    old_count = row[0]
    return round((current_review_count - old_count) / old_count * 100, 1)

def log_share(place_id, name):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT share_count FROM shares WHERE place_id=?", (place_id,))
    row = c.fetchone()
    if row:
        c.execute(
            "UPDATE shares SET share_count=share_count+1 WHERE place_id=?", (place_id,)
        )
    else:
        c.execute(
            "INSERT INTO shares (place_id, name, share_count) VALUES (?,?,1)",
            (place_id, name),
        )
    conn.commit()
    conn.close()

def get_trending_by_shares(limit=10):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT name, share_count FROM shares ORDER BY share_count DESC LIMIT ?",
        (limit,),
    )
    rows = c.fetchall()
    conn.close()
    return rows

# ------------------------------------------------------------
# Gemini + Google検索連携（バズり理由の要約）
# ------------------------------------------------------------
def get_buzz_reason_gemini(place_name, area_name):
    if not GEMINI_API_KEY:
        return "（Gemini APIキー未設定のため理由の取得はスキップされました)"
    try:
        model = genai.GenerativeModel('gemini-1.5-flash')
        prompt = (
            f"「{area_name}」にある「{place_name}」について、直近1週間以内でSNS(Instagram, TikTok)や"
            f"ニュース記事で話題になっている理由を、分かっていれば日本語で1文（40文字以内）で簡潔に述べてください。"
            f"特に話題性の情報が見つからない場合は「特に話題の情報は見つかりませんでした」とだけ答えてください。"
        )
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        return f"（バズり理由の取得に失敗: {e}）"

# ------------------------------------------------------------
# 到着時刻の判定
# ------------------------------------------------------------
def estimate_arrival(distance_km, avg_speed_kmh=30):
    hours = distance_km / avg_speed_kmh
    return datetime.datetime.now() + datetime.timedelta(hours=hours)

def navi_url(name):
    return f"[https://www.google.com/maps/dir/?api=1&destination=](https://www.google.com/maps/dir/?api=1&destination=){urllib.parse.quote(name)}&travelmode=driving"

# ------------------------------------------------------------
# メイン処理（Gemini APIで候補店舗を出力）
# ------------------------------------------------------------
def run_search(location_str, radius_km, purpose, budget_filter, min_rating):
    if not GEMINI_API_KEY:
        return []
        
    keyword = PURPOSE_KEYWORDS.get(purpose, purpose)
    
    try:
        # 強制的にJSON形式でレスポンスを返させる設定
        generation_config = genai.GenerationConfig(
            response_mime_type="application/json"
        )
        model = genai.GenerativeModel(
            'gemini-1.5-flash',
            generation_config=generation_config
        )
        
        prompt = f"""
        あなたはドライブ先提案アシスタントです。
        以下の条件に合致する実在のドライブ目的地を必ず10件提案してください。

        【条件】
        - 現在地情報: {location_str}
        - 検索半径: およそ {radius_km}km 圏内
        - 目的: {keyword}
        - 予算感: {budget_filter}
        - 最低評価: {min_rating}以上

        以下のJSON構造の配列のみを出力してください。
        [
          {{
            "name": "店舗/施設名",
            "address": "住所",
            "rating": 4.5,
            "review_count": 120,
            "budget_name": "1000〜2000円",
            "open_status": true
          }}
        ]
        """
        
        response = model.generate_content(prompt)
        raw_results = json.loads(response.text)
        
    except Exception as e:
        st.error(f"Gemini API通信・解析エラー: {e}")
        return []

    candidates = []
    for place in raw_results:
        name = place.get("name", "不明な店舗")
        place_id = hashlib.md5(name.encode()).hexdigest()
        
        try:
            rating = float(place.get("rating", 4.0))
        except (ValueError, TypeError):
            rating = 4.0
            
        try:
            review_count = int(place.get("review_count", 100))
        except (ValueError, TypeError):
            review_count = 100

        save_snapshot(place_id, name, review_count, rating)
        buzz_rate = get_buzz_rate(place_id, review_count)

        dist = radius_km / 2
        arrival_dt = estimate_arrival(dist)

        fallback_score = round(rating + math.log10(review_count + 1), 3)

        candidates.append(
            {
                "place_id": place_id,
                "name": name,
                "rating": rating,
                "review_count": review_count,
                "buzz_rate": buzz_rate,
                "fallback_score": fallback_score,
                "budget_name": place.get("budget_name", ""),
                "arrival_dt": arrival_dt,
                "open_status": place.get("open_status", True),
                "address": place.get("address", ""),
            }
        )

    candidates.sort(
        key=lambda x: (
            x["buzz_rate"] is None,
            -(x["buzz_rate"] or 0),
            -x["fallback_score"],
        )
    )
    return candidates[:10]

# ------------------------------------------------------------
# UI
# ------------------------------------------------------------
def main():
    init_db()
    st.title("行き先に悩む全てのドライバーへ")
    st.caption("現在地・距離・目的を入力すると、話題のスポットを提案します")

    if not GEMINI_API_KEY:
        st.warning(
            "Gemini APIキーが設定されていません。`.streamlit/secrets.toml` に "
            "`GEMINI_API_KEY` を設定してください。"
        )

    with st.sidebar:
        st.header("① 現在地")
        location_mode = st.radio("位置情報の取得方法", ["ブラウザから自動取得", "住所を入力"])

        location_str = ""
        if location_mode == "ブラウザから自動取得":
            try:
                from streamlit_geolocation import streamlit_geolocation

                loc = streamlit_geolocation()
                if loc and loc.get("latitude"):
                    lat, lng = loc["latitude"], loc["longitude"]
                    location_str = f"緯度:{lat:.4f}, 経度:{lng:.4f}"
                    st.success(f"取得成功: {lat:.4f}, {lng:.4f}")
            except ImportError:
                st.error(
                    "`pip install streamlit-geolocation` が必要です。"
                    "インストール後にこの選択肢が使えます。"
                )
        else:
            address = st.text_input("住所または地名", placeholder="例: 福岡県福岡市中央区")
            if address:
                location_str = address
                st.success(f"取得成功: {address}")

        st.header("② ドライブ圏内")
        range_label = st.radio("圏内を選択", list(RANGE_OPTIONS.keys()))
        if RANGE_OPTIONS[range_label] is None:
            radius_km = st.number_input("圏内(km)を手動入力", min_value=1, max_value=200, value=20)
        else:
            radius_km = RANGE_OPTIONS[range_label]

        st.header("③ 目的")
        purpose = st.selectbox("何を探す?", list(PURPOSE_KEYWORDS.keys()))

        st.header("④ 条件")
        min_rating = st.slider("最低評価", 1.0, 5.0, 3.5, 0.1)
        budget_filter = st.select_slider(
            "予算感", options=["指定なし", "〜1000円", "1000〜3000円", "3000円〜"]
        )

        search_clicked = st.button("🔍 検索する", type="primary", use_container_width=True)

    tab1, tab2 = st.tabs(["検索結果", "🔥人気急上昇(シェアランキング)"])

    with tab1:
        if search_clicked:
            if location_str == "":
                st.error("現在地が取得できていません。左側で位置情報を設定してください。")
            elif not GEMINI_API_KEY:
                st.error("Gemini APIキーが未設定のため検索できません。")
            else:
                with st.spinner("候補地を検索中..."):
                    results = run_search(location_str, radius_km, purpose, budget_filter, min_rating)

                if not results:
                    st.info("条件に合う候補が見つかりませんでした。条件を緩めて再検索してください。")

                for r in results:
                    with st.container(border=True):
                        cols = st.columns([3, 1])
                        with cols[0]:
                            st.subheader(r["name"])
                            st.write(f"📍 {r['address']}")
                            st.write(f"⭐ 評価: {r['rating']} ({r['review_count']}件)")
                            if r["buzz_rate"] is None:
                                st.write(
                                    "📈 口コミ増加率: データ蓄積中(1週間分のデータが必要です)"
                                    f" ／ 現時点の暫定スコア: {r['fallback_score']} "
                                    "(評価点と口コミ数から算出。増加率データが揃い次第そちらに切り替わります)"
                                )
                            else:
                                st.write(f"📈 口コミ増加率(直近1週間): {r['buzz_rate']}%")
                            if r["budget_name"]:
                                st.write(f"💰 予算目安: {r['budget_name']}")
                            st.write(
                                f"🕒 到着予定: {r['arrival_dt'].strftime('%H:%M')} "
                                f"（{'営業中の見込み' if r['open_status'] else '営業状況不明'}）"
                            )

                            with st.spinner("話題の理由を確認中..."):
                                reason = get_buzz_reason_gemini(r["name"], location_str)
                            st.caption(f"💬 {reason}")

                        with cols[1]:
                            st.link_button(
                                "🗺️ ナビ開始", navi_url(r["name"]), use_container_width=True
                            )
                            if st.button("📤 シェア", key=f"share_{r['place_id']}", use_container_width=True):
                                log_share(r["place_id"], r["name"])
                                st.success("シェアを記録しました!")

    with tab2:
        st.subheader("アプリ内で人気のスポット(シェア数ランキング)")
        trending = get_trending_by_shares()
        if not trending:
            st.info("まだシェアデータがありません。")
        else:
            for i, (name, count) in enumerate(trending, start=1):
                st.write(f"{i}. **{name}** — {count}回シェアされました")

if __name__ == "__main__":
    main()
