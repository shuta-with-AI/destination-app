# -*- coding: utf-8 -*-
"""
ドライブ先提案アプリ
====================
現在地・ドライブ圏内・目的を入力すると、
1. 現在地と現在時刻を取得
2. Google Places API (Text Search New) でページネーションを行い、エリア内の全該当店舗（最大60件）を取得
3. Google Distance Matrix API で車移動時間・到着予定時刻を計算し、ラストオーダー/閉店時間に間に合うか厳密判定
4. 営業中の全店舗を評価（Rating）順にランキング化し、トップ10を選出
5. Gemini API でトップ10店舗の口コミから代表メニュー3選と魅力を自動抽出・要約
6. ナビ案内、インスタ検索、シェア機能を提供する。
"""

import streamlit as st
import sqlite3
import datetime
import math
import json
import hashlib
import urllib.parse
import requests
import time
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
            "アサイーボウル": "アサイーボウル スムージー",
            "ケーキ・パフェ": "ケーキ パフェ 喫茶店",
            "パン・パン屋": "パン屋 ベーカリー",
        }
    },
    "景色・観光": {
        "ジャンル": {
            "夜景・展望台": "夜景 展望台 展望デッキ",
            "海・ドライブコース": "海 沿岸 ドライブコース 砂浜",
            "山・自然・公園": "山 自然 公園 渓谷",
            "道の駅・ドライブイン": "道の駅 ドライブイン",
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
    c.execute("SELECT 1 FROM snapshots WHERE place_id=? AND snapshot_date=?", (place_id, today))
    if c.fetchone():
        c.execute("UPDATE snapshots SET review_count=?, rating=? WHERE place_id=? AND snapshot_date=?", (review_count, rating, place_id, today))
    else:
        c.execute("INSERT INTO snapshots (place_id, name, review_count, rating, snapshot_date) VALUES (?,?,?,?,?)", (place_id, name, review_count, rating, today))
    conn.commit()
    conn.close()

def get_buzz_rate(place_id, current_review_count):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    target_date = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
    c.execute("SELECT review_count FROM snapshots WHERE place_id=? AND snapshot_date<=? ORDER BY snapshot_date DESC LIMIT 1", (place_id, target_date))
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
        c.execute("UPDATE shares SET share_count=share_count+1 WHERE place_id=?", (place_id,))
    else:
        c.execute("INSERT INTO shares (place_id, name, share_count) VALUES (?,?,1)", (place_id, name))
    conn.commit()
    conn.close()

def get_trending_by_shares(limit=10):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT name, share_count FROM shares ORDER BY share_count DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    return rows

# ------------------------------------------------------------
# AI拡張処理 & 位置情報ジオコーディング
# ------------------------------------------------------------
def expand_free_word_with_ai(free_word):
    if not client or not free_word.strip():
        return free_word
    prompt = f"ドライブの目的地を探す検索キーワード「{free_word}」に関連する、具体的な店舗ジャンルや特徴をスペース区切りで5つ出力してください。"
    try:
        response = client.models.generate_content(model="gemini-1.5-flash", contents=prompt)
        expanded = response.text.strip()
        return expanded if expanded else free_word
    except Exception:
        return free_word

def geocode_location(location_str):
    if "," in location_str and not any(c in location_str for c in ["県", "市", "区", "町"]):
        try:
            lat, lng = map(float, location_str.split(","))
            return lat, lng
        except Exception:
            pass
    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {"Content-Type": "application/json", "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY, "X-Goog-FieldMask": "places.location"}
    body = {"textQuery": location_str, "maxResultCount": 1}
    try:
        r = requests.post(url, headers=headers, json=body, timeout=5)
        if r.status_code == 200:
            places = r.json().get("places", [])
            if places and "location" in places[0]:
                return places[0]["location"]["latitude"], places[0]["location"]["longitude"]
    except Exception:
        pass
    return None, None

# ------------------------------------------------------------
# Google Distance Matrix API (ルート車移動時間計算)
# ------------------------------------------------------------
def get_routes_matrix(origin_str, destinations):
    if not GOOGLE_MAPS_API_KEY or not destinations: return {}
    url = "https://maps.googleapis.com/maps/api/distancematrix/json"
    dest_strs = []
    for d in destinations:
        loc = d.get("location")
        if loc and "latitude" in loc and "longitude" in loc:
            dest_strs.append(f"{loc['latitude']},{loc['longitude']}")
        else:
            dest_strs.append(d.get("address", d.get("name")))
            
    results = {}
    # Distance Matrix APIの1回あたりリクエスト上限（25件）を考慮し分割処理
    chunk_size = 25
    for i in range(0, len(dest_strs), chunk_size):
        chunk = dest_strs[i:i + chunk_size]
        params = {"origins": origin_str, "destinations": "|".join(chunk), "key": GOOGLE_MAPS_API_KEY, "mode": "driving", "language": "ja"}
        try:
            r = requests.get(url, params=params, timeout=10)
            data = r.json()
            if data.get("status") == "OK":
                rows = data.get("rows", [])
                if rows:
                    for idx, element in enumerate(rows[0].get("elements", [])):
                        if element.get("status") == "OK":
                            results[i + idx] = element.get("duration", {}).get("value", 0)
        except Exception:
            pass
    return results

# ------------------------------------------------------------
# 営業時間・到着予定時刻・ラストオーダー判定
# ------------------------------------------------------------
def check_open_at_time_details(regular_opening_hours, open_now_fallback, arrival_dt):
    if not regular_opening_hours or "periods" not in regular_opening_hours:
        if open_now_fallback is not None: return open_now_fallback, "不明", "不明"
        return True, "情報なし", "情報なし"
        
    periods = regular_opening_hours.get("periods", [])
    if len(periods) == 1 and periods[0].get("open", {}).get("day") == 0 and periods[0].get("open", {}).get("hour") == 0 and "close" not in periods[0]:
        return True, "24時間営業", "なし（24H）"
    
    arrival_minutes = ((arrival_dt.weekday() + 1) % 7) * 24 * 60 + arrival_dt.hour * 60 + arrival_dt.minute
    for period in periods:
        open_info = period.get("open", {})
        close_info = period.get("close", {})
        if not open_info: continue
        open_minutes = open_info.get("day", 0) * 24 * 60 + open_info.get("hour", 0) * 60 + open_info.get("minute", 0)
        if not close_info: return True, "24時間営業", "なし（24H）"
        close_minutes = close_info.get("day", 0) * 24 * 60 + close_info.get("hour", 0) * 60 + close_info.get("minute", 0)
        if close_minutes <= open_minutes: close_minutes += 7 * 24 * 60
            
        if (open_minutes <= arrival_minutes < close_minutes) or (open_minutes <= (arrival_minutes + 7 * 24 * 60) < close_minutes):
            c_hour, c_min = close_info.get("hour", 0), close_info.get("minute", 0)
            close_time_obj = datetime.time(c_hour, c_min)
            close_dt = datetime.datetime.combine(arrival_dt.date(), close_time_obj)
            lo_dt = close_dt - datetime.timedelta(minutes=30)
            
            # 到着時刻がラストオーダー目安を過ぎていないかも判定
            if arrival_dt > lo_dt and (close_minutes - arrival_minutes) < 30:
                return False, f"{c_hour:02d}:{c_min:02d}", "LO終了"
                
            return True, f"{c_hour:02d}:{c_min:02d}", lo_dt.strftime("%H:%M") + " 頃 (目安)"
            
    return False, "営業時間外", "営業時間外"

# ------------------------------------------------------------
# Google Places API (Text Search) 全件全取得 (最大60件)
# ------------------------------------------------------------
def search_places_text_all(location_str, radius_km, search_query_keyword):
    if not GOOGLE_MAPS_API_KEY:
        st.error("GOOGLE_MAPS_API_KEY が未設定です。")
        return []
        
    lat, lng = geocode_location(location_str)
    st.info(f"🔎 1. 座標確認: lat={lat}, lng={lng}")
    if lat is None or lng is None:
        st.error("現在地の取得に失敗しました。")
        return []

    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
        "X-Goog-FieldMask": (
            "places.id,places.displayName,places.formattedAddress,places.rating,"
            "places.userRatingCount,places.regularOpeningHours,"
            "places.currentOpeningHours.openNow,places.googleMapsUri,"
            "places.location,places.photos,places.reviews,places.types,nextPageToken"
        ),
    }
    
    body = {
        "textQuery": search_query_keyword,
        "maxResultCount": 20,
        "locationRestriction": {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": min(radius_km * 1000, 50000)
            }
        }
    }
    
    st.info(f"🔎 2. リクエスト送信中... クエリ: 「{search_query_keyword}」 / 半径: {radius_km}km")

    try:
        r = requests.post(url, headers=headers, json=body, timeout=10)
        st.info(f"🔎 3. APIレスポンスステータス: {r.status_code}")
        
        if r.status_code != 200:
            st.error(f"APIエラー詳細: {r.text}")
            return []
            
        res_data = r.json()
        raw_places = res_data.get("places", [])
        st.info(f"🔎 4. API取得成功件数（生の取得数）: {len(raw_places)}件")
        
        # 取得できた店名をいくつか表示してみる
        if raw_places:
            sample_names = [p.get("displayName", {}).get("text", "") for p in raw_places[:3]]
            st.write(f"取得サンプル: {sample_names}")
            
    except Exception as e:
        st.error(f"通信例外が発生しました: {e}")
        return []

    # 以下フィルタリング処理...
    unwanted_name_keywords = ["ホテル", "hotel", "旅館", "宿", "シネマ", "cinema", "映画館", "マクドナルド", "すき家", "吉野家"]
    
    places = []
    seen_ids = set()
    for p in raw_places:
        pid = p.get("id")
        if not pid or pid in seen_ids: continue
        seen_ids.add(pid)

        name_check = p.get("displayName", {}).get("text", "")
        if any(x in name_check.lower() for x in unwanted_name_keywords): continue

        review_texts = [rev.get("text", {}).get("text", "").replace("\n", " ") for rev in p.get("reviews", [])[:5] if rev.get("text", {}).get("text")]
        
        places.append({
            "google_id": pid,
            "name": name_check,
            "address": p.get("formattedAddress", ""),
            "rating": float(p.get("rating", 0)),
            "review_count": int(p.get("userRatingCount", 0)),
            "maps_url": p.get("googleMapsUri", ""),
            "regular_opening_hours": p.get("regularOpeningHours"),
            "open_now_fallback": p.get("currentOpeningHours", {}).get("openNow"),
            "location": p.get("location"),
            "photo_urls": [],
            "review_texts": " / ".join(review_texts)
        })
    return places

# ------------------------------------------------------------
# メイン処理 (全件取得 -> 時間判定 -> 評価順ソート -> Gemini)
# ------------------------------------------------------------
def run_search(location_str, radius_km, search_query_keyword, budget_filter, min_rating):
    if not client:
        st.error("GEMINI_API_KEY が未設定です。")
        return []

    # 1. 現在時刻の取得
    now = datetime.datetime.now(ZoneInfo("Asia/Tokyo"))
    
    # 2. 圏内から全対象店舗を取得（20件制限の撤廃）
    raw_places = search_places_text_all(location_str, radius_km, search_query_keyword)
    st.caption(f"エリア内ヒット件数: {len(raw_places)} 件")
    if not raw_places: return []

    # 3. ルート車移動時間の計算とラストオーダー・営業中の厳密判定
    durations_map = get_routes_matrix(location_str, raw_places)
    open_places = []
    
    for idx, p in enumerate(raw_places):
        if p["rating"] < min_rating: continue
        
        drive_seconds = durations_map.get(idx, (radius_km / 2) / 30 * 3600)
        arrival_dt = now + datetime.timedelta(seconds=drive_seconds)
        
        open_status, closing_time_str, last_order_str = check_open_at_time_details(
            p["regular_opening_hours"], p["open_now_fallback"], arrival_dt
        )
        
        if open_status:
            p["arrival_dt"] = arrival_dt
            p["drive_time_min"] = math.ceil(drive_seconds / 60)
            p["closing_time_str"] = closing_time_str
            p["last_order_str"] = last_order_str
            # スコアリング（評価 + 口コミ数の確からしさ）
            p["score"] = p["rating"] + math.log10(max(p["review_count"], 1))
            open_places.append(p)

    st.caption(f"営業中かつ条件合致件数: {len(open_places)} 件")
    if not open_places: return []

    # 4. 評価の高い順（スコア順）にソートしてトップ10を厳選
    open_places.sort(key=lambda x: x["score"], reverse=True)
    top_10_places = open_places[:10]

    # 5. Gemini API を使用して、トップ10店舗の口コミからメニューと魅力を抽出
    input_list_for_gemini = [
        {"google_id": p["google_id"], "name": p["name"], "reviews": p["review_texts"] if p["review_texts"] else "特になし"}
        for p in top_10_places
    ]

    prompt = f"""
    あなたはドライブの目的地提案アシスタントです。
    以下の【店舗リスト（口コミデータ付き）】の各店舗について、代表メニューと魅力を抽出し、JSONフォーマットで出力してください。

    【予算感の指定】: {budget_filter}
    【店舗リスト】
    {json.dumps(input_list_for_gemini, ensure_ascii=False)}

    【出力ルール】
    - リスト内のすべての店舗（google_id）に対してデータを作成してください。
    - 「reviews (口コミ)」や店名から、代表的な人気メニュー3選（価格目安もわかれば併記）を「popular_menu」の配列として作成。
    - 予算感の指定と口コミを元に、予算目安（例: 1000〜2000円）を作成。
    - ドライブで訪れるべき魅力を簡潔な「buzz_reason」として作成。
    - 以下のJSON配列フォーマットのみを出力してください（Markdown表記は不要）。
    [
      {{
        "google_id": "入力されたgoogle_id",
        "budget_name": "1000〜2000円",
        "popular_menu": ["人気ラーメン (850円)", "特製餃子 (450円)", "チャーシュー丼 (350円)"],
        "buzz_reason": "深夜まで大人気の行列ができるお店です！"
      }}
    ]
    """

    gemini_map = {}
    try:
        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        gemini_results = json.loads(response.text)
        gemini_map = {item["google_id"]: item for item in gemini_results if "google_id" in item}
    except Exception:
        pass

    # ベースデータとGeminiの抽出結果をマージ
    candidates = []
    for p in top_10_places:
        g_data = gemini_map.get(p["google_id"], {})
        place_id = p["google_id"] or hashlib.md5(p["name"].encode()).hexdigest()
        save_snapshot(place_id, p["name"], p["review_count"], p["rating"])
        
        candidates.append({
            "place_id": place_id,
            "name": p["name"],
            "rating": p["rating"],
            "review_count": p["review_count"],
            "buzz_rate": get_buzz_rate(place_id, p["review_count"]),
            "budget_name": g_data.get("budget_name", "予算目安不明"),
            "arrival_dt": p["arrival_dt"],
            "drive_time_min": p["drive_time_min"],
            "closing_time_str": p["closing_time_str"],
            "last_order_str": p["last_order_str"],
            "address": p["address"],
            "maps_url": p["maps_url"],
            "buzz_reason": g_data.get("buzz_reason", "話題の注目スポットです！"),
            "photo_urls": p["photo_urls"],
            "popular_menu": g_data.get("popular_menu", ["定番おすすめメニュー", "人気商品"]),
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
        st.warning("APIキーが未設定です。`Secrets` に `GEMINI_API_KEY` および `GOOGLE_MAPS_API_KEY` を設定してください。")

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
                    location_str = f"{loc['latitude']:.6f},{loc['longitude']:.6f}"
                    st.success("取得成功")
            except ImportError:
                st.error("`pip install streamlit-geolocation` が必要です。")
        else:
            address = st.text_input("住所または地名", placeholder="例: 福岡県福岡市中央区")
            if address:
                location_str = address
                st.success(f"指定完了: {address}")

        st.header("② ドライブ圏内")
        range_label = st.radio("圏内を選択", list(RANGE_OPTIONS.keys()))
        radius_km = st.number_input("圏内(km)を手動入力", min_value=1, max_value=200, value=20) if RANGE_OPTIONS[range_label] is None else RANGE_OPTIONS[range_label]

        st.header("③ 目的")
        selected_keywords = []
        for category, cat_info in PURPOSE_DATA.items():
            genres = cat_info["ジャンル"]
            with st.expander(category):
                for genre_name, genre_keyword in genres.items():
                    if st.checkbox(genre_name, key=f"chk_{genre_name}"):
                        selected_keywords.append(genre_keyword)

        st.subheader("🔍 フリーワード入力")
        free_word = st.text_input("こだわりキーワード (任意)", placeholder="例: 隠れ家, 夜カフェ, 激辛")

        st.header("④ 条件")
        min_rating = st.slider("最低評価", 1.0, 5.0, 3.5, 0.1)
        budget_filter = st.select_slider("予算感", options=["指定なし", "〜1000円", "1000〜3000円", "3000円〜"])

        search_clicked = st.button("🔍 検索する", type="primary", use_container_width=True)

    tab1, tab2 = st.tabs(["検索結果", "🔥人気急上昇(シェアランキング)"])

    with tab1:
        if search_clicked:
            if not location_str:
                st.error("現在地を指定してください。")
            else:
                with st.spinner("圏内のスポットを検索＆ルート計算中..."):
                    keyword_parts = []
                    if selected_keywords:
                        keyword_parts.append(selected_keywords[0].split()[0])
                    if free_word.strip():
                        keyword_parts.append(expand_free_word_with_ai(free_word.strip()))
                    
                    combined_keywords = " ".join(keyword_parts).strip()
                    if not combined_keywords:
                        combined_keywords = "ドライブ スポット グルメ"

                    results = run_search(location_str, radius_km, combined_keywords, budget_filter, min_rating)

                if not results:
                    st.info("条件に合う営業中のスポットが見つかりませんでした。目的を変更するか最低評価を下げる・範囲を広げて再試行してください。")

                for r in results:
                    with st.container(border=True):
                        cols = st.columns([3, 1])
                        with cols[0]:
                            st.subheader(r["name"])
                            
                            if r.get("photo_urls"):
                                img_cols = st.columns(min(len(r["photo_urls"]), 2))
                                for i, img_url in enumerate(r["photo_urls"][:2]):
                                    with img_cols[i]: st.image(img_url, use_container_width=True)

                            st.write("🍽️ **おすすめ・人気メニュー**")
                            for item in r["popular_menu"]: st.write(f"- {item}")

                            st.write(f"📍 {r['address']}")
                            st.write(f"⭐ 評価: {r['rating']} ({r['review_count']}件)")
                            st.write(f"💰 予算目安: {r['budget_name']}")
                            st.write(f"🚗 所要時間目安: 約 {r['drive_time_min']} 分 (到着予定: {r['arrival_dt'].strftime('%H:%M')})")
                            st.write(f"⏳ 閉店時間: **{r['closing_time_str']}**（LO: **{r['last_order_str']}**）")
                            st.caption(f"💬 {r['buzz_reason']}")

                        with cols[1]:
                            st.link_button("🗺️ ナビ開始", r["maps_url"], use_container_width=True)
                            insta_url = f"https://www.instagram.com/explore/search/keyword/?q={urllib.parse.quote(r['name'])}"
                            st.link_button("📸 インスタで探す", insta_url, use_container_width=True)
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
