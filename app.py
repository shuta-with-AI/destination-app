# -*- coding: utf-8 -*-
"""
ドライブ先提案アプリ
====================
現在地・ドライブ圏内・目的（細分化チェックボックス対応）を入力すると、
1. Google Places API (New) で指定エリアの店舗および最新口コミを取得
2. Google Distance Matrix API で現在地から各店舗への正確な車移動時間を計算
3. 店舗ごとに異なる到着予定時刻に基づき、営業状況（24時間・深夜営業対応）を判定
4. Gemini API で口コミから代表メニュー3選＆価格を自動抽出し、ランキング化
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

# 目的の細分化データ構造
PURPOSE_DATA = {
    "ご飯": {
        "デフォルト": "レストラン グルメ ラーメン 牛丼 居酒屋 深夜営業 食事",
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
        "デフォルト": "スイーツ カフェ 夜カフェ",
        "ジャンル": {
            "アイス・ジェラート": "アイスクリーム ジェラート パフェ",
            "クレープ": "クレープ ガレット",
            "アサイーボウル": "アサイーボウル スムージー カフェ",
            "ケーキ・パフェ": "ケーキ パフェ 喫茶店",
            "パン・パン屋": "パン屋 ベーカリー カフェ",
        }
    },
    "景色・観光": {
        "デフォルト": "絶景 展望 景色 観光スポット 夜景",
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
# Google Distance Matrix API 連携 (移動時間計算)
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
# 高度な営業時間判定
# ------------------------------------------------------------
def check_open_at_time(regular_opening_hours, open_now_fallback, arrival_dt):
    if not regular_opening_hours or "periods" not in regular_opening_hours:
        return open_now_fallback if open_now_fallback is not None else True

    periods = regular_opening_hours.get("periods", [])
    
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
# Google Places API 検索 (写真・口コミ取得)
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
            body["textQuery"] = f"{location_str} {search_query_keyword}"
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
            for photo in photos_data[:4]:
                photo_name = photo.get("name")
                if photo_name:
                    url_img = f"https://places.googleapis.com/v1/{photo_name}/media?key={GOOGLE_MAPS_API_KEY}&maxHeightPx=400&maxWidthPx=400"
                    photo_urls.append(url_img)

            reviews = p.get("reviews", [])
            review_texts = []
            for rev in reviews[:5]:
                txt = rev.get("text", {}).get("text", "")
                if txt:
                    review_texts.append(txt)

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
def run_search(location_str, radius_km, search_query_keyword, main_purpose_label, budget_filter, min_rating):
    if not client:
        st.error("GEMINI_API_KEY が読み込めていません。Secretsの設定を確認してください。")
        return []

    now = datetime.datetime.now(ZoneInfo("Asia/Tokyo"))

    # STEP 1: Google Places API から候補店舗を検索
    raw_places = search_places_google(location_str, radius_km, search_query_keyword)
    if not raw_places:
        return []

    # STEP 2: Distance Matrix API で現在地から各店舗への個別移動時間(秒)を取得
    durations_map = get_routes_matrix(location_str, raw_places)

    # STEP 3: 店舗ごとの個別の到着予定時間を計算 & 営業中店舗のフィルタリング
    open_places = []
    for idx, p in enumerate(raw_places):
        drive_seconds = durations_map.get(idx)
        if drive_seconds is not None and drive_seconds > 0:
            arrival_dt = now + datetime.timedelta(seconds=drive_seconds)
            drive_time_min = math.ceil(drive_seconds / 60)
        else:
            arrival_dt = now + datetime.timedelta(hours=(radius_km / 2) / 30)
            drive_time_min = None

        open_status = check_open_at_time(p["regular_opening_hours"], p["open_now_fallback"], arrival_dt)

        if open_status is False:
            continue

        p["arrival_dt"] = arrival_dt
        p["drive_time_min"] = drive_time_min
        p["open_status"] = open_status
        open_places.append(p)

    if not open_places:
        return []

    filtered_places = [p for p in open_places if p["rating"] >= min_rating]
    if len(filtered_places) < 10:
        open_places.sort(key=lambda x: -x["rating"])
        filtered_places = open_places

    # STEP 4: Gemini に評価・ランキング・おすすめ理由・口コミからの人気メニュー抽出を行わせる
    input_list_for_gemini = [
        {
            "id": idx,
            "name": p["name"],
            "rating": p["rating"],
            "review_count": p["review_count"],
            "address": p["address"],
            "reviews": p["review_texts"]
        }
        for idx, p in enumerate(filtered_places)
    ]

    prompt = f"""
    あなたはドライブの目的地提案アシスタントです。
    以下の【営業中の店舗リスト（口コミデータ付き）】の中から、ドライブの目的地として特におすすめのスポットを厳選し、
    おすすめ順（ランキング順）に並び替えてJSONで出力してください。

    【検索条件】
    - 目的: {main_purpose_label} ({search_query_keyword})
    - 予算感の指定: {budget_filter}

    【営業中の店舗リスト】
    {json.dumps(input_list_for_gemini, ensure_ascii=False)}

    【出力ルール】
    - 入力リストに存在する店舗のみを使ってください（架空の店舗を捏造しないでください）。
    - 提供された「reviews (口コミ)」のテキストを分析し、その店舗で人気の具体的メニュー名や商品名（できれば口コミ内の価格も）を「popular_menu」として最大3つ抽出してください。
    - 各店舗について、予算目安（例: 1000〜2000円）と、ドライブで訪れるべき魅力を簡潔な「buzz_reason」として作成してください。
    - 以下のJSON配列フォーマットのみを出力してください。

    [
      {{
        "id": 0,
        "budget_name": "1000〜2000円",
        "popular_menu": ["人気メニューA (850円)", "人気メニューB (450円)"],
        "buzz_reason": "地元で大人気のスポットです！"
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
        ranked_indices = [{"id": idx, "budget_name": "", "popular_menu": [], "buzz_reason": "おすすめスポットです！"} for idx, _ in enumerate(filtered_places)]

    # STEP 5: データのマージと整形
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
            "photo_urls": base_info.get("photo_urls", []),
            "popular_menu": item.get("popular_menu", []),
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
        main_purpose = st.selectbox("カテゴリを選択", list(PURPOSE_DATA.keys()))

        # ★ 選択されたカテゴリに応じて細分化チェックボックスを表示
        selected_genres = []
        genre_dict = PURPOSE_DATA[main_purpose]["ジャンル"]
        
        st.markdown("**さらに絞り込む (複数選択可)**")
        for genre_name in genre_dict.keys():
            if st.checkbox(genre_name, key=f"chk_{main_purpose}_{genre_name}"):
                selected_genres.append(genre_name)

        # 検索キーワードの構築
        if selected_genres:
            keywords_list = [genre_dict[g] for g in selected_genres]
            search_query_keyword = " ".join(keywords_list)
        else:
            search_query_keyword = PURPOSE_DATA[main_purpose]["デフォルト"]

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
                    results = run_search(
                        location_str,
                        radius_km,
                        search_query_keyword,
                        main_purpose,
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
                            
                            # 写真リストの表示 (最大4枚)
                            if r.get("photo_urls"):
                                img_cols = st.columns(min(len(r["photo_urls"]), 4))
                                for i, img_url in enumerate(r["photo_urls"][:4]):
                                    with img_cols[i]:
                                        st.image(img_url, use_container_width=True)

                            # 口コミからAIが抽出した人気メニューの表示
                            if r.get("popular_menu"):
                                st.write("🍽️ **AIが口コミから見つけた人気メニュー**")
                                for item in r["popular_menu"]:
                                    st.write(f"- {item}")

                            if r["address"]:
                                st.write(f"📍 {r['address']}")
                            st.write(f"⭐ 評価: {r['rating']} ({r['review_count']}件)")
                            
                            if r["budget_name"]:
                                st.write(f"💰 予算目安: {r['budget_name']}")
                            
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
