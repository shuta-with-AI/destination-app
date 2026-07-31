# -*- coding: utf-8 -*-
"""
ドライブ先提案アプリ
====================
現在地・ドライブ圏内・目的（ご飯/スイーツ/景色 等）を入力すると、
Gemini APIで候補地を検索し、Google Places APIで詳細情報を補完して、
- 評価点（足切り）
- 予算
- 到着予測時刻での営業状況
を考慮して上位10件を提案し、Googleマップへのナビ導線とシェア機能を提供する。
"""

import streamlit as st
import sqlite3
import datetime
import math
import json
import hashlib
import urllib.parse
import traceback
import requests
from google import genai
from google.genai import types
from zoneinfo import ZoneInfo

# ------------------------------------------------------------
# 初期設定
# ------------------------------------------------------------
st.set_page_config(page_title="ドライブ先提案アプリ", page_icon="🚗", layout="wide")

DB_PATH = "drive_app_data.db"

GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")
GOOGLE_MAPS_API_KEY = st.secrets.get("GOOGLE_MAPS_API_KEY", "")

# 新標準クライアントの初期化
client = None
if GEMINI_API_KEY:
    client = genai.Client(api_key=GEMINI_API_KEY)

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
# 到着時刻の判定 & Google Places API連携
# ------------------------------------------------------------
def estimate_arrival(distance_km, avg_speed_kmh=30):
    now = datetime.datetime.now(ZoneInfo("Asia/Tokyo"))
    hours = distance_km / avg_speed_kmh
    return now + datetime.timedelta(hours=hours)

# ------------------------------------------------------------
# 到着時刻の判定 & Google Places API連携
# ------------------------------------------------------------
def check_open_at_time(regular_opening_hours, open_now_fallback, arrival_dt):
    if not regular_opening_hours or "periods" not in regular_opening_hours:
        # スケジュールデータがない場合は現在の営業状態を採用（判定不能で弾かれるのを防ぐ）
        return open_now_fallback

    python_weekday = arrival_dt.weekday()
    target_day = (python_weekday + 1) % 7
    arrival_time_int = arrival_dt.hour * 100 + arrival_dt.minute

    for period in regular_opening_hours.get("periods", []):
        open_info = period.get("open", {})
        close_info = period.get("close", {})

        if open_info.get("day") == target_day:
            open_time = open_info.get("hour", 0) * 100 + open_info.get("minute", 0)
            if not close_info:
                return True

            close_time = close_info.get("hour", 0) * 100 + close_info.get("minute", 0)

            if close_time < open_time:
                if arrival_time_int >= open_time or arrival_time_int < close_time:
                    return True
            else:
                if open_time <= arrival_time_int < close_time:
                    return True

    return False

def get_place_info(query):
    if not GOOGLE_MAPS_API_KEY:
        return None

    url = "https://places.googleapis.com/v1/places:searchText"

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
        "X-Goog-FieldMask": (
            "places.displayName,"
            "places.formattedAddress,"
            "places.rating,"
            "places.userRatingCount,"
            "places.regularOpeningHours,"
            "places.currentOpeningHours.openNow,"
            "places.googleMapsUri"
        ),
    }

    body = {"textQuery": query}

    try:
        r = requests.post(url, headers=headers, json=body, timeout=5)
        if r.status_code != 200:
            return None

        data = r.json()
        if "places" not in data or not data["places"]:
            return None

        p = data["places"][0]

        return {
            "name": p.get("displayName", {}).get("text", query),
            "address": p.get("formattedAddress", ""),
            "rating": p.get("rating", 0.0),
            "review_count": p.get("userRatingCount", 0),
            "regular_opening_hours": p.get("regularOpeningHours"),
            "open_now": p.get("currentOpeningHours", {}).get("openNow"),
            "maps_url": p.get("googleMapsUri", f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(query)}")
        }
    except Exception:
        return None

def run_search(location_str, radius_km, purpose, budget_filter, min_rating):
    if not client:
        st.error("GEMINI_API_KEY が読み込めていません。Secretsの設定を確認してください。")
        return []
        
    keyword = PURPOSE_KEYWORDS.get(purpose, purpose)
    
    prompt = f"""
    あなたはドライブ先提案アシスタントです。
    以下の条件に合致する実在のドライブ目的地を20件提案してください。

    【条件】
    - 現在地情報: {location_str}
    - 検索半径: およそ {radius_km}km 圏内
    - 目的: {keyword}

    以下のJSON構造の配列のみを出力してください。
    [
      {{
        "name": "店舗名",
        "budget_name": "1000〜2000円",
        "buzz_reason": "SNSやテレビで話題のスポット"  
      }}
    ]
    """

    try:
        response = client.models.generate_content(
            model="gemini-flash-latest",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        raw_results = json.loads(response.text)
    except Exception as e:
        st.error(f"エラーが発生しました: {type(e)}")
        return []

    candidates = []
    for place in raw_results:
        # 地域名を補完して精度を上げる
        query = f"{location_str} {place.get('name', '')}"
        google = get_place_info(query)
        
        if google is None:
            continue

        rating = float(google["rating"])
        # 最低評価フィルターチェック
        if rating < min_rating:
            continue

        dist = radius_km / 2
        arrival_dt = estimate_arrival(dist)

        open_status = check_open_at_time(
            google["regular_opening_hours"], 
            google["open_now"], 
            arrival_dt
        )

        # 確実に「営業時間外(False)」であるものだけを除外
        if open_status is False:
            continue

        name = google["name"]
        review_count = int(google["review_count"])
        address = google["address"]
        maps_url = google["maps_url"]

        place_id = hashlib.md5(name.encode()).hexdigest()
        save_snapshot(place_id, name, review_count, rating)
        buzz_rate = get_buzz_rate(place_id, review_count)
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
                "open_status": open_status,
                "address": address,
                "maps_url": maps_url,
                "buzz_reason": place.get("buzz_reason", "話題の注目スポットです！"),
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
            "Gemini APIキーが設定されていません。`Secrets` に "
            "`GEMINI_API_KEY` を設定してください。"
        )

    with st.sidebar:
        st.header("① 現在地")
        location_mode = st.radio("位置情報の取得方法", ["ブラウザから自動取得", "住所を入力"])

        location_str = ""
        if location_mode == "ブラウザから自動取得":
            try:
                from streamlit_geolocation import streamlit_geolocation

                st.info("💡 ボタンを押してブラウザの位置情報利用を許可してください。")
                loc = streamlit_geolocation()
                if loc and loc.get("latitude") and loc.get("longitude"):
                    lat, lng = loc["latitude"], loc["longitude"]
                    location_str = f"緯度:{lat:.4f}, 経度:{lng:.4f}"
                    st.success(f"取得成功: {lat:.4f}, {lng:.4f}")
                else:
                    st.warning("位置情報がまだ取得できていません。「住所を入力」もお試しください。")
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
            if not location_str:
                st.error("現在地が取得できていません。左側の設定で現在地（住所または自動取得）を指定してください。")
            elif not GEMINI_API_KEY:
                st.error("Gemini APIキーが未設定のため検索できません。")
            else:
                with st.spinner("到着時に営業中の候補地を検索中..."):
                    results = run_search(location_str, radius_km, purpose, budget_filter, min_rating)

                if not results:
                    st.info("到着予定時刻に営業しているスポットが見つかりませんでした。時間帯や検索範囲を変更して再検索してください。")

                for r in results:
                    with st.container(border=True):
                        cols = st.columns([3, 1])
                        with cols[0]:
                            st.subheader(r["name"])
                            if r["address"]:
                                st.write(f"📍 {r['address']}")
                            st.write(f"⭐ 評価: {r['rating']} ({r['review_count']}件)")
                            
                            if r["budget_name"]:
                                st.write(f"💰 予算目安: {r['budget_name']}")
                            st.write(
                                f"🕒 到着予定: {r['arrival_dt'].strftime('%H:%M')} （営業中）"
                            )
                            st.caption(f"💬 {r['buzz_reason']}")

                        with cols[1]:
                            st.link_button(
                                "🗺️ ナビ開始",
                                r["maps_url"],
                                use_container_width=True
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
