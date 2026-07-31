# -*- coding: utf-8 -*-
"""
ドライブ先提案アプリ
====================
現在地・ドライブ圏内・目的を入力すると、
1. Google Places API (New) で指定エリアの店舗を検索
2. Google Routes API で現在地から各店舗への正確な車移動時間を一括計算
3. 店舗ごとに異なる到着予定時刻に基づき、営業状況（24時間・深夜営業対応）を判定
4. Gemini API で営業確定店舗をランキング化し、おすすめ理由（buzz_reason）や予算感を生成
5. 上位10件を表示し、ナビ案内やシェア機能を提供する。
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
# DB処理 (SQLite)
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
# Google Routes API 連携 (移動時間計算)
# ------------------------------------------------------------
def get_routes_matrix(origin_str, destinations):
    """
    Routes API (computeRouteMatrix) を使い、現在地から各目的地への走行時間(秒)を一括取得
    """
    if not GOOGLE_MAPS_API_KEY or not destinations:
        return {}

    url = "https://routes.googleapis.com/distanceMatrix/v1:computeRouteMatrix"

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
        "X-Goog-FieldMask": "originIndex,destinationIndex,duration,distanceMeters,status"
    }

    # 出発地 (現在地)
    if "," in origin_str and not any(c in origin_str for c in ["県", "市", "区", "町"]):
        try:
            lat, lng = map(float, origin_str.split(","))
            origin_waypoint = {"waypoint": {"location": {"latLng": {"latitude": lat, "longitude": lng}}}}
        except Exception:
            origin_waypoint = {"waypoint": {"address": origin_str}}
    else:
        origin_waypoint = {"waypoint": {"address": origin_str}}

    # 目的地リスト
    dest_waypoints = []
    for d in destinations:
        loc = d.get("location")
        if loc and "latitude" in loc and "longitude" in loc:
            dest_waypoints.append({"waypoint": {"location": {"latLng": {"latitude": loc["latitude"], "longitude": loc["longitude"]}}}})
        else:
            dest_waypoints.append({"waypoint": {"address": d.get("address", d.get("name"))}})

    body = {
        "origins": [origin_waypoint],
        "destinations": dest_waypoints,
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE"
    }

    try:
        r = requests.post(url, headers=headers, json=body, timeout=10)
        if r.status_code != 200:
            return {}

        results = {}
        for item in r.json():
            dest_idx = item.get("destinationIndex")
            duration_str = item.get("duration", "0s")  # 例: "1250s"
            seconds = int(duration_str.replace("s", "")) if duration_str.endswith("s") else 0
            results[dest_idx] = seconds

        return results
    except Exception:
        return {}

# ------------------------------------------------------------
# 高度な営業時間判定
# ------------------------------------------------------------
def check_open_at_time(regular_opening_hours, open_now_fallback, arrival_dt):
    """
    到着予定時刻(arrival_dt)に営業しているかを判定する
    （24時間営業・日またぎ・週またぎの深夜営業にも完全対応）
    """
    if not regular_opening_hours or "periods" not in regular_opening_hours:
        return open_now_fallback if open_now_fallback is not None else True

    periods = regular_opening_hours.get("periods", [])
    
    # 24時間営業判定
    if len(periods) == 1:
        op = periods[0].get("open", {})
        if op.get("day") == 0 and op.get("hour") == 0 and op.get("minute") == 0 and "close" not in periods[0]:
            return True

    python_weekday = arrival_dt.weekday()
    target_day = (python_weekday + 1) % 7
    arrival_minutes = target_day * 24 * 60 + arrival_dt.hour * 60 + arrival_dt.minute
    
    for period in periods:
        open_info = period.get("open", {})
        close_info = period.get("close", {})
        
        if not open_info:
            continue
            
        open_minutes = open_info.get("day", 0) * 24 * 60 + open_info.get("hour", 0) * 60 + open_info.get("minute", 0)
        
        if not close_info:
            return True
            
        close_minutes = close_info.get("day", 0) * 24 * 60 + close_info.get("hour", 0) * 60 + close_info.get("minute", 0)
        
        if close_minutes <= open_minutes:
            close_minutes += 7 * 24 * 60
            
        if (open_minutes <= arrival_minutes < close_minutes) or \
           (open_minutes <= (arrival_minutes + 7 * 24 * 60) < close_minutes):
            return True
            
    return False

# ------------------------------------------------------------
# Google Places API 検索
# ------------------------------------------------------------
def search_places_google(location_str, radius_km, purpose):
    """
    Google Places API (New) で周辺店舗を直検索 (座標情報を含む)
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
            "places.googleMapsUri,"
            "places.location"  # ★ Routes API用に座標を取得
        ),
    }

    keyword = PURPOSE_KEYWORDS.get(purpose, purpose)

    body = {
        "textQuery": f"{keyword}",
        "maxResultCount": 20
    }

    if "," in location_str and not any(c in location_str for c in ["県", "市", "区", "町"]):
        try:
            lat_str, lng_str = location_str.split(",")
            lat, lng = float(lat_str), float(lng_str)
            safe_radius = min(float(radius_km * 1000), 50000.0)
            body["locationBias"] = {
                "circle": {
                    "center": {"latitude": lat, "longitude": lng},
                    "radius": safe_radius
                }
            }
        except Exception:
            body["textQuery"] = f"{location_str} {keyword}"
    else:
        body["textQuery"] = f"{location_str} {keyword}"

    try:
        r = requests.post(url, headers=headers, json=body, timeout=8)
        if r.status_code != 200:
            st.error(f"Google API エラー (ステータスコード: {r.status_code})")
            st.json(r.json())
            return []

        data = r.json()
        raw_places = data.get("places", [])

        places = []
        for p in raw_places:
            name = p.get("displayName", {}).get("text", "")
            rating = float(p.get("rating", 0.0))
            review_count = int(p.get("userRatingCount", 0))

            places.append({
                "google_id": p.get("id"),
                "name": name,
                "address": p.get("formattedAddress", ""),
                "rating": rating,
                "review_count": review_count,
                "maps_url": p.get("googleMapsUri", f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(name)}"),
                "regular_opening_hours": p.get("regularOpeningHours"),
                "open_now_fallback": p.get("currentOpeningHours", {}).get("openNow"),
                "location": p.get("location")  # {'latitude': ..., 'longitude': ...}
            })

        return places

    except Exception as e:
        st.error(f"通信エラーが発生しました: {str(e)}")
        return []

# ------------------------------------------------------------
# メイン検索処理
# ------------------------------------------------------------
def run_search(location_str, radius_km, purpose, budget_filter, min_rating):
    if not client:
        st.error("GEMINI_API_KEY が読み込めていません。Secretsの設定を確認してください。")
        return []

    now = datetime.datetime.now(ZoneInfo("Asia/Tokyo"))

    # STEP 1: Google Places API から候補店舗を検索
    raw_places = search_places_google(location_str, radius_km, purpose)
    if not raw_places:
        return []

    # STEP 2: Routes API で現在地から各店舗への個別移動時間(秒)を取得
    durations_map = get_routes_matrix(location_str, raw_places)

    # STEP 3: 店舗ごとの個別の到着予定時間を計算 & 営業中店舗のフィルタリング
    open_places = []
    for idx, p in enumerate(raw_places):
        # Routes APIから時間取得できなければ平均30km/hの簡易計算でフォールバック
        drive_seconds = durations_map.get(idx)
        if drive_seconds is not None and drive_seconds > 0:
            arrival_dt = now + datetime.timedelta(seconds=drive_seconds)
            drive_time_min = math.ceil(drive_seconds / 60)
        else:
            arrival_dt = now + datetime.timedelta(hours=(radius_km / 2) / 30)
            drive_time_min = None

        # 到着時刻での営業判定
        open_status = check_open_at_time(p["regular_opening_hours"], p["open_now_fallback"], arrival_dt)

        if open_status is False:
            continue

        p["arrival_dt"] = arrival_dt
        p["drive_time_min"] = drive_time_min
        p["open_status"] = open_status
        open_places.append(p)

    if not open_places:
        return []

    # 最低評価でのフィルタリング（条件が厳しすぎる場合の自動補正）
    filtered_places = [p for p in open_places if p["rating"] >= min_rating]
    if len(filtered_places) < 3:
        filtered_places = open_places

    # STEP 4: Gemini に評価・ランキング・おすすめ理由・予算感を生成させる
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
    - 入力リストに存在する店舗のみを使ってください（架空の店舗を捏造しないでください）。
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

    # STEP 5: データのマージと整形
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
            "arrival_dt": base_info["arrival_dt"],
            "drive_time_min": base_info["drive_time_min"],
            "open_status": base_info["open_status"],
            "address": base_info["address"],
            "maps_url": base_info["maps_url"],
            "buzz_reason": item.get("buzz_reason", "話題の注目スポットです！"),
        })

    return candidates[:10]

# ------------------------------------------------------------
# UI 画面構成
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
                with st.spinner("Google Maps & Routesから移動時間と営業状況を取得し、Geminiで厳選中..."):
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
                            
                            # 所要時間表示の組み立て
                            time_info = f"車で約{r['drive_time_min']}分" if r["drive_time_min"] is not None else "到着予定"
                            st.write(
                                f"🕒 到着予定: {r['arrival_dt'].strftime('%H:%M')} （{time_info} / 営業中の見込み）"
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
