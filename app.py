# -*- coding: utf-8 -*-
"""
ドライブ先提案アプリ
====================
現在地・ドライブ圏内・目的を入力すると、
1. Gemini API で自由入力ワードをGoogle検索に最適化された類似キーワード群へ拡張
2. 現在地から緯度経度を特定し、locationRestriction（完全境界制限）でエリア外の店舗を完全遮断
3. Google Places API (New) で指定エリアの店舗および最新口コミ・写真（最大2枚）を取得
4. Google Distance Matrix API で現在地から各店舗への正確な車移動時間を計算
5. 到着予定時刻に基づき、閉店時間・ラストオーダー目安を算出
6. Gemini API で口コミや店名から代表メニュー3選＆価格を自動抽出し、ランキング化
7. 上位10件を表示し、ナビ案内やシェア機能を提供する。
"""

import streamlit as st
import sqlite3
import datetime
import math
import json
import hashlib
import urllib.parse
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

PURPOSE_DATA = {
    "ご飯": {
        "ジャンル": {
            "ラーメン": "ラーメン 中華そば つけ麺",
            "ハンバーガー": "ハンバーガー グルメバーガー",
            "レストラン・洋食": "レストラン 洋食 ハンバーグ",
            "和食・定食": "定食 和食 食堂",
            "居酒屋・深夜食堂": "居酒屋 深夜営業 居酒屋",
            "焼肉・肉料理": "焼肉 肉料理 ステーキ",
        }
    },
    "スイーツ": {
        "ジャンル": {
            "アイス・ジェラート": "アイスクリーム ジェラート パフェ",
            "クレープ": "クレープ ガレット",
            "アサイーボウル": "アサイーボウル スムージー カフェ",
            "ケーキ・パフェ": "ケーキ パフェ 喫茶店",
            "パン・パン屋": "パン屋 ベーカリー カフェ",
        }
    },
    "景色・観光": {
        "ジャンル": {
            "夜景・展望台": "夜景 展望台 展望デッキ",
            "海・ドライブコース": "海 沿岸 ドライブコース 砂浜",
            "山・自然・公園": "山 自然 公園 渓谷",
            "道の駅・ドライブイン": "道の駅 ドライブイン 観光施設",
        }
    }
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
# 自由入力ワードのAI拡張処理
# ------------------------------------------------------------
def expand_free_word_with_ai(free_word):
    if not client or not free_word.strip():
        return free_word

    prompt = f"""
    ユーザーがドライブアプリで目的地を探すために「{free_word}」と入力しました。
    Google Maps API (Places API) で検索する際にヒットしやすくなるよう、この言葉に関連する具体的な店舗ジャンルや特徴・類似キーワードをスペース区切りで5つ程度出力してください。

    【出力例】
    入力: エモいカフェ
    出力: 古民家カフェ レトロ喫茶 映えスイーツ 夜カフェ 雰囲気の良いカフェ

    入力: {free_word}
    出力:
    """

    try:
        response = client.models.generate_content(
            model="gemini-flash-latest",
            contents=prompt,
        )
        expanded_keywords = response.text.strip()
        return expanded_keywords if expanded_keywords else free_word
    except Exception:
        return free_word

# ------------------------------------------------------------
# 住所から緯度経度を取得するヘルパー関数
# ------------------------------------------------------------
def geocode_location(location_str):
    if "," in location_str and not any(c in location_str for c in ["県", "市", "区", "町"]):
        try:
            lat, lng = map(float, location_str.split(","))
            return lat, lng
        except Exception:
            pass

    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
        "X-Goog-FieldMask": "places.location"
    }
    body = {"textQuery": location_str, "maxResultCount": 1}

    try:
        r = requests.post(url, headers=headers, json=body, timeout=5)
        if r.status_code == 200:
            places = r.json().get("places", [])
            if places and "location" in places[0]:
                loc = places[0]["location"]
                return loc.get("latitude"), loc.get("longitude")
    except Exception:
        pass

    return None, None

# ------------------------------------------------------------
# Google Distance Matrix API 連携
# ------------------------------------------------------------
def get_routes_matrix(origin_str, destinations):
    if not GOOGLE_MAPS_API_KEY or not destinations:
        return {}

    url = "https://maps.googleapis.com/maps/api/distancematrix/json"
    
    dest_strs = []
    for d in destinations:
        loc = d.get("location")
        if loc and "latitude" in loc and "longitude" in loc:
            dest_strs.append(f"{loc['latitude']},{loc['longitude']}")
        else:
            dest_strs.append(d.get("address", d.get("name")))

    params = {
        "origins": origin_str,
        "destinations": "|".join(dest_strs),
        "key": GOOGLE_MAPS_API_KEY,
        "mode": "driving",
        "language": "ja"
    }

    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        
        if data.get("status") != "OK":
            return {}

        results = {}
        rows = data.get("rows", [])
        if not rows:
            return {}
            
        elements = rows[0].get("elements", [])
        for idx, element in enumerate(elements):
            if element.get("status") == "OK":
                seconds = element.get("duration", {}).get("value", 0)
                results[idx] = seconds

        return results
    except Exception:
        return {}

# ------------------------------------------------------------
# 営業時間・閉店時間・ラストオーダー判定
# ------------------------------------------------------------
def check_open_at_time_details(regular_opening_hours, open_now_fallback, arrival_dt):
    if not regular_opening_hours or "periods" not in regular_opening_hours:
        is_open = open_now_fallback if open_now_fallback is not None else True
        return is_open, "不明", "不明"

    periods = regular_opening_hours.get("periods", [])
    
    if len(periods) == 1:
        op = periods[0].get("open", {})
        if op.get("day") == 0 and op.get("hour") == 0 and op.get("minute") == 0 and "close" not in periods[0]:
            return True, "24時間営業", "なし（24H）"

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
            return True, "24時間営業", "なし（24H）"
            
        close_minutes = close_info.get("day", 0) * 24 * 60 + close_info.get("hour", 0) * 60 + close_info.get("minute", 0)
        
        if close_minutes <= open_minutes:
            close_minutes += 7 * 24 * 60
            
        if (open_minutes <= arrival_minutes < close_minutes) or \
           (open_minutes <= (arrival_minutes + 7 * 24 * 60) < close_minutes):
            
            c_hour = close_info.get("hour", 0)
            c_min = close_info.get("minute", 0)
            closing_time_str = f"{c_hour:02d}:{c_min:02d}"
            
            close_time_obj = datetime.time(c_hour, c_min)
            close_dt = datetime.datetime.combine(arrival_dt.date(), close_time_obj)
            lo_dt = close_dt - datetime.timedelta(minutes=30)
            last_order_str = lo_dt.strftime("%H:%M") + " 頃 (目安)"
            
            return True, closing_time_str, last_order_str
            
    return False, "営業時間外", "営業時間外"

# ------------------------------------------------------------
# Google Places API 検索 (エリア外店舗を絶対遮断)
# ------------------------------------------------------------
def search_places_google(location_str, radius_km, search_query_keyword):
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
            "places.location,"
            "places.photos,"
            "places.reviews"
        ),
    }

    body = {
        "textQuery": f"{search_query_keyword}",
        "pageSize": 20,
        "maxResultCount": 20
    }

    lat, lng = geocode_location(location_str)
    if lat is not None and lng is not None:
        safe_radius = min(float(radius_km * 1000), 50000.0)  # 最大50km制限
        # 🌟 locationRestriction を使って指定範囲外の遠方店舗（群馬など）を完全に遮断
        body["locationRestriction"] = {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": safe_radius
            }
        }
    else:
        body["textQuery"] = f"{location_str} {search_query_keyword}"

    try:
        r = requests.post(url, headers=headers, json=body, timeout=8)
        if r.status_code != 200:
            st.error(f"Google API エラー (ステータスコード: {r.status_code})")
            return []

        data = r.json()
        raw_places = data.get("places", [])

        places = []
        for p in raw_places:
            name = p.get("displayName", {}).get("text", "")
            rating = float(p.get("rating", 0.0))
            review_count = int(p.get("userRatingCount", 0))

            photos_data = p.get("photos", [])
            photo_urls = []
            for photo in photos_data[:2]:
                photo_name = photo.get("name")
                if photo_name:
                    url_img = f"https://places.googleapis.com/v1/{photo_name}/media?key={GOOGLE_MAPS_API_KEY}&maxHeightPx=400&maxWidthPx=400"
                    photo_urls.append(url_img)

            reviews = p.get("reviews", [])
            review_texts = []
            for rev in reviews[:5]:
                txt = rev.get("text", {}).get("text", "")
                if txt:
                    # 改行や特殊文字をシンプルにしてパースエラーを防ぐ
                    clean_txt = txt.replace("\n", " ").replace('"', '’')
                    review_texts.append(clean_txt)

            places.append({
                "google_id": p.get("id"),
                "name": name,
                "address": p.get("formattedAddress", ""),
                "rating": rating,
                "review_count": review_count,
                "maps_url": p.get("googleMapsUri", f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(name)}"),
                "regular_opening_hours": p.get("regularOpeningHours"),
                "open_now_fallback": p.get("currentOpeningHours", {}).get("openNow"),
                "location": p.get("location"),
                "photo_urls": photo_urls,
                "review_texts": " / ".join(review_texts)
            })

        return places

    except Exception as e:
        st.error(f"通信エラーが発生しました: {str(e)}")
        return []

# ------------------------------------------------------------
# メイン検索処理
# ------------------------------------------------------------
def run_search(location_str, radius_km, search_query_keyword, budget_filter, min_rating):
    if not client:
        st.error("GEMINI_API_KEY が読み込めていません。Secretsの設定を確認してください。")
        return []

    now = datetime.datetime.now(ZoneInfo("Asia/Tokyo"))

    raw_places = search_places_google(location_str, radius_km, search_query_keyword)
    if not raw_places:
        return []

    durations_map = get_routes_matrix(location_str, raw_places)

    open_places = []
    for idx, p in enumerate(raw_places):
        drive_seconds = durations_map.get(idx)
        if drive_seconds is not None and drive_seconds > 0:
            arrival_dt = now + datetime.timedelta(seconds=drive_seconds)
            drive_time_min = math.ceil(drive_seconds / 60)
        else:
            arrival_dt = now + datetime.timedelta(hours=(radius_km / 2) / 30)
            drive_time_min = None

        open_status, closing_time_str, last_order_str = check_open_at_time_details(
            p["regular_opening_hours"], p["open_now_fallback"], arrival_dt
        )

        if open_status is False:
            continue

        p["arrival_dt"] = arrival_dt
        p["drive_time_min"] = drive_time_min
        p["open_status"] = open_status
        p["closing_time_str"] = closing_time_str
        p["last_order_str"] = last_order_str
        open_places.append(p)

    if not open_places:
        return []

    filtered_places = [p for p in open_places if p["rating"] >= min_rating]
    if len(filtered_places) < 10:
        open_places.sort(key=lambda x: -x["rating"])
        filtered_places = open_places

    input_list_for_gemini = [
        {
            "id": idx,
            "name": p["name"],
            "rating": p["rating"],
            "review_count": p["review_count"],
            "address": p["address"],
            "reviews": p["review_texts"] if p["review_texts"] else "（特になし）"
        }
        for idx, p in enumerate(filtered_places)
    ]

    prompt = f"""
    あなたはドライブの目的地提案アシスタントです。
    以下の【営業中の店舗リスト（口コミデータ付き）】の中から、ドライブの目的地として特におすすめのスポットを厳選し、
    おすすめ順（ランキング順）に並び替えてJSONで出力してください。

    【検索条件】
    - 目的キーワード: {search_query_keyword}
    - 予算感の指定: {budget_filter}

    【営業中の店舗リスト】
    {json.dumps(input_list_for_gemini, ensure_ascii=False)}

    【出力ルール】
    - 入力リストに存在する店舗のみを使ってください。
    - 「reviews (口コミ)」や店名から、代表的な人気メニュー3選（価格目安もわかれば併記）を「popular_menu」の配列として必ず作成してください。口コミが少ない場合でも、店名や業態（ラーメン、カフェ、焼肉など）から想像される定番メニューを必ず補完出力してください。空にしてはいけません。
    - 各店舗について、予算目安（例: 1000〜2000円）と、ドライブで訪れるべき魅力を簡潔な「buzz_reason」として作成してください。
    - 以下のJSON配列フォーマットのみを出力してください。

    [
      {{
        "id": 0,
        "budget_name": "1000〜2000円",
        "popular_menu": ["人気ラーメン (850円)", "特製餃子 (450円)", "チャーシュー丼 (350円)"],
        "buzz_reason": "深夜まで大人気の行列ができるラーメン店です！"
      }}
    ]
    """

    ranked_indices = []
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
        pass

    # 万が一 JSON パースエラーが発生した場合の安全なバックアップマージ
    if not ranked_indices:
        ranked_indices = [
            {
                "id": idx,
                "budget_name": "1000〜2000円",
                "popular_menu": ["代表メニュー", "おすすめ料理"],
                "buzz_reason": "おすすめのスポットです！"
            }
            for idx, _ in enumerate(filtered_places)
        ]

    candidates = []
    top_items = ranked_indices[:10]

    for item in top_items:
        idx = item.get("id")
        if idx is None or idx >= len(filtered_places):
            continue

        base_info = filtered_places[idx]
        name = base_info["name"]
        rating = base_info["rating"]
        review_count = base_info["review_count"]

        # メニューが空の場合は業態から補完
        menu_list = item.get("popular_menu", [])
        if not menu_list:
            menu_list = ["定番おすすめメニュー", "人気商品"]

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
            "budget_name": item.get("budget_name", "1000〜2000円"),
            "arrival_dt": base_info["arrival_dt"],
            "drive_time_min": base_info["drive_time_min"],
            "open_status": base_info["open_status"],
            "closing_time_str": base_info["closing_time_str"],
            "last_order_str": base_info["last_order_str"],
            "address": base_info["address"],
            "maps_url": base_info["maps_url"],
            "buzz_reason": item.get("buzz_reason", "話題の注目スポットです！"),
            "photo_urls": base_info.get("photo_urls", []),
            "popular_menu": menu_list,
        })

    return candidates

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
        
        selected_keywords = []

        for category, cat_info in PURPOSE_DATA.items():
            genres = cat_info["ジャンル"]
            parent_key = f"parent_{category}"

            def on_parent_change(cat=category, g_keys=list(genres.keys())):
                is_checked = st.session_state[f"parent_{cat}"]
                for g_key in g_keys:
                    st.session_state[f"child_{cat}_{g_key}"] = is_checked

            def on_child_change(cat=category, g_keys=list(genres.keys())):
                all_checked = all(st.session_state.get(f"child_{cat}_{g_key}", False) for g_key in g_keys)
                st.session_state[f"parent_{cat}"] = all_checked

            if parent_key not in st.session_state:
                st.session_state[parent_key] = False

            with st.expander(category):
                parent_checked = st.checkbox(
                    f"**{category} (全選択)**",
                    key=parent_key,
                    on_change=on_parent_change,
                    args=(category, list(genres.keys()))
                )

                for genre_name, genre_keyword in genres.items():
                    child_key = f"child_{category}_{genre_name}"
                    if child_key not in st.session_state:
                        st.session_state[child_key] = parent_checked

                    c_indent, c_content = st.columns([0.15, 0.85])
                    with c_content:
                        child_checked = st.checkbox(
                            genre_name,
                            key=child_key,
                            on_change=on_child_change,
                            args=(category, list(genres.keys()))
                        )
                        if child_checked:
                            selected_keywords.append(genre_keyword)

        st.subheader("🔍 フリーワード入力")
        free_word = st.text_input(
            "こだわりキーワード (任意)",
            placeholder="例: 隠れ家, 夜カフェ, 映えスポット, 激辛"
        )

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
                with st.spinner("検索中..."):
                    combined_keywords = " ".join(selected_keywords)
                    
                    if free_word.strip():
                        expanded_free_word = expand_free_word_with_ai(free_word.strip())
                        combined_keywords = f"{combined_keywords} {expanded_free_word}".strip()

                    if not combined_keywords:
                        combined_keywords = "ドライブ スポット グルメ 観光"

                    results = run_search(
                        location_str,
                        radius_km,
                        combined_keywords,
                        budget_filter,
                        min_rating
                    )

                if not results:
                    st.info("条件に合う営業中のスポットが見つかりませんでした。目的を変更するか、検索範囲を広げて再試行してください。")

                for r in results:
                    with st.container(border=True):
                        cols = st.columns([3, 1])
                        with cols[0]:
                            st.subheader(r["name"])
                            
                            # 写真リストの表示 (最大2枚)
                            if r.get("photo_urls"):
                                img_cols = st.columns(min(len(r["photo_urls"]), 2))
                                for i, img_url in enumerate(r["photo_urls"][:2]):
                                    with img_cols[i]:
                                        st.image(img_url, use_container_width=True)

                            # ★ AIが口コミから抽出した人気メニューの表示
                            if r.get("popular_menu"):
                                st.write("🍽️ **おすすめ・人気メニュー**")
                                for item in r["popular_menu"]:
                                    st.write(f"- {item}")

                            if r["address"]:
                                st.write(f"📍 {r['address']}")
                            st.write(f"⭐ 評価: {r['rating']} ({r['review_count']}件)")
                            
                            if r["budget_name"]:
                                st.write(f"💰 予算目安: {r['budget_name']}")
                            
                            st.write(
                                f"🕒 到着予定: **{r['arrival_dt'].strftime('%H:%M')}**"
                            )
                            st.write(
                                f"⏳ 閉店時間: **{r['closing_time_str']}**（ラストオーダー：**{r['last_order_str']}**）"
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
