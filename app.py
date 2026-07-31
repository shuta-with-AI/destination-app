# -*- coding: utf-8 -*-
"""
ドライブ先提案アプリ
====================
現在地・ドライブ圏内・目的を入力すると、
1. Google Places API で指定エリアの店舗を検索し、到着時刻の営業状況でフィルタリング
2. Gemini API で営業確定店舗をランキング化・おすすめ理由（buzz_reason）を生成
3. 上位10件を表示し、ナビ案内やシェア機能を提供する。
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

# Client初期化
client = None
if GEMINI_API_KEY:
    client = genai.Client(api_key=GEMINI_API_KEY)

PURPOSE_KEYWORDS = {
    "ご飯": "レストラン グルメ 人気店",
    "スイーツ": "スイーツ カフェ",
    "パン": "パン屋 ベーカリー",
    "景色": "絶景 展望 景色 観光スポット",
}

RANGE_OPTIONS = {
    "0〜10km": 10,
    "10〜30km": 30,
    "30〜50km": 50,
    "手動入力": None,
}

# ------------------------------------------------------------
# DB処理
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
# 営業時間判定 & Google Places API連携
# ------------------------------------------------------------
def estimate_arrival(distance_km, avg_speed_kmh=30):
    now = datetime.datetime.now(ZoneInfo("Asia/Tokyo"))
    hours = distance_km / avg_speed_kmh
    return now + datetime.timedelta(hours=hours)

def check_open_at_time(regular_opening_hours, open_now_fallback, arrival_dt):
    """
    到着予定時刻(arrival_dt)に営業しているかを判定する
    戻り値: True(営業中), False(営業時間外), None(判定不能)
    """
    if not regular_opening_hours or "periods" not in regular_opening_hours:
        # スケジュール情報がない場合は、現在の営業フラグを代用（TrueまたはNone）
        return open_now_fallback if open_now_fallback is not None else True

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

def search_open_places_google(location_str, radius_km, purpose, arrival_dt):
    """
    Google Places API (New) で周辺店舗を直検索
    """
    if not GOOGLE_MAPS_API_KEY:
        st.error("GOOGLE_MAPS_API_KEY が未設定です。")
        return []

    url = "https://places.googleapis.com/v1/places:searchText"

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
        "X-Goog-FieldMask": (
            "places.id,"
            "places.displayName,"
            "places.formattedAddress,"
            "places.rating,"
            "places.userRatingCount,"
            "places.regularOpeningHours,"
            "places.currentOpeningHours.openNow,"
            "places.googleMapsUri"
        ),
    }

    keyword = PURPOSE_KEYWORDS.get(purpose, purpose)

    body = {
        "textQuery": f"{keyword}",
        "maxResultCount": 20
    }

    # 緯度・経度の場合は locationBias を設定して精度を高める
    if "," in location_str and not any(c in location_str for c in ["県", "市", "区", "町"]):
        try:
            lat_str, lng_str = location_str.split(",")
            lat, lng = float(lat_str), float(lng_str)
            body["locationBias"] = {
                "circle": {
                    "center": {"latitude": lat, "longitude": lng},
                    "radius": float(radius_km * 1000)
                }
            }
        except Exception:
            body["textQuery"] = f"{location_str} {keyword}"
    else:
        body["textQuery"] = f"{location_str} {keyword}"

    try:
        r = requests.post(url, headers=headers, json=body, timeout=8)
        if r.status_code != 200:
            return []

        data = r.json()
        raw_places = data.get("places", [])

        open_places = []
        for p in raw_places:
            hours = p.get("regularOpeningHours")
            open_now_fallback = p.get("currentOpeningHours", {}).get("openNow")

            open_status = check_open_at_time(hours, open_now_fallback, arrival_dt)

            # 確実に「営業外(False)」の時のみスキップ
            if open_status is False:
                continue

            name = p.get("displayName", {}).get("text", "")
            rating = float(p.get("rating", 0.0))
            review_count = int(p.get("userRatingCount", 0))

            open_places.append({
                "google_id": p.get("id"),
                "name": name,
                "address": p.get("formattedAddress", ""),
                "rating": rating,
                "review_count": review_count,
                "maps_url": p.get("googleMapsUri", f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(name)}"),
                "open_status": open_status
            })

        return open_places

    except Exception:
        return []

# ------------------------------------------------------------
# メイン検索処理
# ------------------------------------------------------------
def run_search(location_str, radius_km, purpose, budget_filter, min_rating):
    if not client:
        st.error("GEMINI_API_KEY が読み込めていません。Secretsの設定を確認してください。")
        return []

    dist = radius_km / 2
    arrival_dt = estimate_arrival(dist)

    # STEP 1: Google Places API から店舗リストを取得
    google_results = search_open_places_google(location_str, radius_km, purpose, arrival_dt)

    if not google_results:
        return []

    # 最低評価でのフィルタリング（該当店舗が少なすぎる場合は最低評価基準を自動調整）
    filtered_places = [p for p in google_results if p["rating"] >= min_rating]
    if len(filtered_places) < 3:
        # 最低評価に届く候補が少なすぎる場合はGoogle検索結果全体を使用
        filtered_places = google_results

    # STEP 2: Gemini に評価・ランキング・紹介文を生成させる
    input_list_for_gemini = [
        {
            "id": idx,
            "name": p["name"],
            "rating": p["rating"],
            "review_count": p["review_count"],
            "address": p["address"]
        }
        for idx, p in enumerate(filtered_places)
    ]

    prompt = f"""
    あなたはドライブの目的地提案アシスタントです。
    以下の【営業中の店舗リスト】の中から、ドライブの目的地として特におすすめのスポットを厳選し、
    おすすめ順（ランキング順）に並び替えてJSONで出力してください。

    【検索条件】
    - 目的: {purpose}
    - 予算感の指定: {budget_filter}

    【営業中の店舗リスト】
    {json.dumps(input_list_for_gemini, ensure_ascii=False)}

    【出力ルール】
    - 入力リストに存在する店舗のみを使ってください。
    - 各店舗について、予算目安（例: 1000〜2000円）と、ドライブで訪れるべき魅力を簡潔な「buzz_reason」として作成してください。
    - 以下のJSON配列フォーマットのみを出力してください。

    [
      {{
        "id": 0,
        "budget_name": "1000〜2000円",
        "buzz_reason": "地元の絶品グルメが楽しめる人気スポットです！"
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
        ranked_indices = json.loads(response.text)
    except Exception:
        ranked_indices = [{"id": idx, "budget_name": "", "buzz_reason": "おすすめスポットです！"} for idx, _ in enumerate(filtered_places)]

    # STEP 3: データマージ
    candidates = []
    for item in ranked_indices:
        idx = item.get("id")
        if idx is None or idx >= len(filtered_places):
            continue

        base_info = filtered_places[idx]
        name = base_info["name"]
        rating = base_info["rating"]
        review_count = base_info["review_count"]

        place_id = hashlib.md5(name.encode()).hexdigest()
        save_snapshot(place_id, name, review_count, rating)
        buzz_rate = get_buzz_rate(place_id, review_count)
        fallback_score = round(rating + math.log10(review_count + 1), 3)

        candidates.append({
            "place_id": place_id,
            "name": name,
            "rating": rating,
            "review_count": review_count,
            "buzz_rate": buzz_rate,
            "fallback_score": fallback_score,
            "budget_name": item.get("budget_name", ""),
            "arrival_dt": arrival_dt,
            "open_status": base_info["open_status"],
            "address": base_info["address"],
            "maps_url": base_info["maps_url"],
            "buzz_reason": item.get("buzz_reason", "話題の注目スポットです！"),
        })

    return candidates[:10]

# ------------------------------------------------------------
# UI
# ------------------------------------------------------------
def main():
    init_db()
    st.title("行き先に悩む全てのドライバーへ")
    st.caption("現在地・距離・目的を入力すると、営業中の話題のスポットを提案します")

    if not GEMINI_API_KEY or not GOOGLE_MAPS_API_KEY:
        st.warning(
            "APIキーが未設定です。`Secrets` に `GEMINI_API_KEY` および "
            "`GOOGLE_MAPS_API_KEY` を設定してください。"
        )

    with st.sidebar:
        st.header("① 現在地")
        location_mode = st.radio("位置情報の取得方法", ["ブラウザから自動取得", "住所を入力"])

        location_str = ""
        if location_mode == "ブラウザから自動取得":
            try:
                from streamlit_geolocation import streamlit_geolocation

                st.info("💡 下のボタンを押して現在地を許可してください。")
                loc = streamlit_geolocation()
                if loc and loc.get("latitude") and loc.get("longitude"):
                    lat, lng = loc["latitude"], loc["longitude"]
                    location_str = f"{lat:.6f},{lng:.6f}"
                    st.success(f"取得成功: {lat:.4f}, {lng:.4f}")
                else:
                    st.caption("取得できない場合は「住所を入力」をお試しください。")
            except ImportError:
                st.error(
                    "`pip install streamlit-geolocation` が必要です。"
                    "インストール後にこの選択肢が使えます。"
                )
        else:
            address = st.text_input("住所または地名", placeholder="例: 福岡県福岡市中央区")
            if address:
                location_str = address
                st.success(f"指定完了: {address}")

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
                st.error("現在地が取得できていません。左側の設定で現在地（住所入力または自動取得）を指定してください。")
            elif not GEMINI_API_KEY or not GOOGLE_MAPS_API_KEY:
                st.error("APIキーが未設定のため検索できません。")
            else:
                with st.spinner("Google Mapsから営業中スポットを取得し、Geminiで厳選中..."):
                    results = run_search(location_str, radius_km, purpose, budget_filter, min_rating)

                if not results:
                    st.info("条件に合う営業中のスポットが見つかりませんでした。目的を変更するか、検索範囲を広げて再試行してください。")

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
                                f"🕒 到着予定: {r['arrival_dt'].strftime('%H:%M')} （営業中の見込み）"
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
