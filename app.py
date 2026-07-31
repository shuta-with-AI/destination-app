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
import requests
import sqlite3
import datetime
import math

# ------------------------------------------------------------
# 初期設定
# ------------------------------------------------------------
st.set_page_config(page_title="目的地ここに決めた！", page_icon="🚗", layout="wide")

DB_PATH = "drive_app_data.db"

GOOGLE_MAPS_API_KEY = st.secrets.get("GOOGLE_MAPS_API_KEY", "")
HOTPEPPER_API_KEY = st.secrets.get("HOTPEPPER_API_KEY", "")
GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")

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
    # 同じ店・同じ日はUPDATE、無ければINSERT
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
    """7日前に近いスナップショットと比較して口コミ増加率(%)を返す。データが無ければNone"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    target_date = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
    # target_date以前で一番新しいスナップショットを使う
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
# Google Places API
# ------------------------------------------------------------
def search_places(lat, lng, radius_km, keyword):
    """Text Search (Legacy)で候補地を検索"""
    url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    params = {
        "query": keyword,
        "location": f"{lat},{lng}",
        "radius": min(radius_km * 1000, 50000),  # APIの上限は50km
        "key": GOOGLE_MAPS_API_KEY,
        "language": "ja",
    }
    try:
        res = requests.get(url, params=params, timeout=10)
        res.raise_for_status()
        return res.json().get("results", [])
    except Exception as e:
        st.error(f"Google Places検索でエラーが発生しました: {e}")
        return []


def get_place_details(place_id):
    url = "https://maps.googleapis.com/maps/api/place/details/json"
    params = {
        "place_id": place_id,
        "fields": "name,rating,user_ratings_total,opening_hours,price_level,geometry,formatted_address",
        "key": GOOGLE_MAPS_API_KEY,
        "language": "ja",
    }
    try:
        res = requests.get(url, params=params, timeout=10)
        res.raise_for_status()
        return res.json().get("result", {})
    except Exception as e:
        st.warning(f"詳細情報の取得に失敗しました: {e}")
        return {}


def geocode_address(address):
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"address": address, "key": GOOGLE_MAPS_API_KEY, "language": "ja"}
    try:
        res = requests.get(url, params=params, timeout=10)
        res.raise_for_status()
        results = res.json().get("results", [])
        if results:
            loc = results[0]["geometry"]["location"]
            return loc["lat"], loc["lng"]
    except Exception as e:
        st.error(f"住所の変換に失敗しました: {e}")
    return None, None


# ------------------------------------------------------------
# ホットペッパー（予算の補完情報のみ・検索範囲3km制約に注意)
# ------------------------------------------------------------
def hotpepper_budget_lookup(name, lat, lng):
    if not HOTPEPPER_API_KEY:
        return None
    url = "https://webservice.recruit.co.jp/hotpepper/gourmet/v1/"
    params = {
        "key": HOTPEPPER_API_KEY,
        "lat": lat,
        "lng": lng,
        "range": 5,  # 3000m固定（APIの最大値)
        "keyword": name,
        "format": "json",
        "count": 1,
    }
    try:
        res = requests.get(url, params=params, timeout=10)
        res.raise_for_status()
        shops = res.json().get("results", {}).get("shop", [])
        if shops:
            return shops[0].get("budget", {}).get("name")
    except Exception:
        return None
    return None


# ------------------------------------------------------------
# Gemini + Google検索連携（バズり理由の要約のみ・上位10件に限定してコスト抑制)
# ------------------------------------------------------------
def get_buzz_reason_gemini(place_name, area_name):
    if not GEMINI_API_KEY:
        return "（Gemini APIキー未設定のため理由の取得はスキップされました)"
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=GEMINI_API_KEY)
        grounding_tool = types.Tool(google_search=types.GoogleSearch())
        config = types.GenerateContentConfig(tools=[grounding_tool])
        prompt = (
            f"「{area_name}」にある「{place_name}」について、直近1週間以内でSNS(Instagram, TikTok)や"
            f"ニュース記事で話題になっている理由を、分かっていれば日本語で1文（40文字以内）で簡潔に述べてください。"
            f"特に話題性の情報が見つからない場合は「特に話題の情報は見つかりませんでした」とだけ答えてください。"
        )
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=config,
        )
        return response.text.strip()
    except Exception as e:
        return f"（バズり理由の取得に失敗: {e}）"


# ------------------------------------------------------------
# 到着時刻・営業状況の判定
# ------------------------------------------------------------
def estimate_arrival(distance_km, avg_speed_kmh=30):
    """簡易見積もり:平均時速30km/hのドライブとして到着時刻を計算(信号や渋滞は考慮しない簡易値)"""
    hours = distance_km / avg_speed_kmh
    return datetime.datetime.now() + datetime.timedelta(hours=hours)


def is_open_at(opening_hours, arrival_dt):
    """Google Places opening_hours.periods を見て到着時刻に営業しているか判定"""
    if not opening_hours or "periods" not in opening_hours:
        return None  # 情報なし
    weekday = arrival_dt.weekday()  # 月=0 ... 日=6 → Googleは日=0始まりなので変換
    google_day = (weekday + 1) % 7
    arrival_hm = arrival_dt.strftime("%H%M")
    for period in opening_hours["periods"]:
        open_info = period.get("open", {})
        close_info = period.get("close")
        if open_info.get("day") == google_day:
            open_time = open_info.get("time", "0000")
            if close_info:
                close_time = close_info.get("time", "2359")
                if open_time <= arrival_hm <= close_time:
                    return True
            else:
                # close情報が無い = 24時間営業
                return True
    return False


def distance_km_between(lat1, lng1, lat2, lng2):
    """ハーバサイン距離(km)。到着時刻の簡易見積もり用"""
    R = 6371
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def navi_url(lat, lng):
    return f"https://www.google.com/maps/dir/?api=1&destination={lat},{lng}&travelmode=driving"


# ------------------------------------------------------------
# メイン処理
# ------------------------------------------------------------
def run_search(lat, lng, radius_km, purpose, budget_filter, min_rating):
    keyword = PURPOSE_KEYWORDS.get(purpose, purpose)
    raw_results = search_places(lat, lng, radius_km, keyword)

    candidates = []
    for place in raw_results[:20]:  # 詳細取得のAPIコール数を抑えるため上位20件に限定
        place_id = place.get("place_id")
        details = get_place_details(place_id)
        if not details:
            continue

        name = details.get("name", "不明な店舗")
        rating = details.get("rating")
        review_count = details.get("user_ratings_total")
        price_level = details.get("price_level")
        geometry = details.get("geometry", {}).get("location", {})
        opening_hours = details.get("opening_hours", {})

        if rating is None or review_count is None:
            continue
        if rating < min_rating:
            continue  # 評価の足切り

        # スナップショット保存(毎回の検索で今日の分を記録・上書き)
        save_snapshot(place_id, name, review_count, rating)
        buzz_rate = get_buzz_rate(place_id, review_count)

        # 到着予測時刻と営業判定
        p_lat, p_lng = geometry.get("lat"), geometry.get("lng")
        dist = distance_km_between(lat, lng, p_lat, p_lng) if p_lat else radius_km
        arrival_dt = estimate_arrival(dist)
        open_status = is_open_at(opening_hours, arrival_dt)
        if open_status is False:
            continue  # 到着時に閉まっている店は除外

        # 予算(ホットペッパーで補完)
        budget_name = hotpepper_budget_lookup(name, lat, lng)

        # 増加率データが無い間の暫定スコア(評価点 + log10(口コミ数))
        # 口コミ数をそのまま使うと件数の多い店が支配してしまうため、対数で緩やかにしている
        fallback_score = round(rating + math.log10(review_count + 1), 3)

        candidates.append(
            {
                "place_id": place_id,
                "name": name,
                "rating": rating,
                "review_count": review_count,
                "buzz_rate": buzz_rate,
                "fallback_score": fallback_score,
                "price_level": price_level,
                "budget_name": budget_name,
                "lat": p_lat,
                "lng": p_lng,
                "arrival_dt": arrival_dt,
                "open_status": open_status,
                "address": details.get("formatted_address", ""),
            }
        )

    # 増加率データがある店はその値で優先順位を決め、
    # まだ無い店(データ蓄積中)は暫定スコア(評価+口コミ数)で並べる
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
    st.title("🚗 ドライブ先提案アプリ")
    st.caption("現在地・ドライブ圏内・目的を入力すると、話題のスポットを提案します")

    if not GOOGLE_MAPS_API_KEY:
        st.warning(
            "Google Maps APIキーが設定されていません。`.streamlit/secrets.toml` に "
            "`GOOGLE_MAPS_API_KEY` を設定してください。"
        )

    with st.sidebar:
        st.header("① 現在地")
        location_mode = st.radio("位置情報の取得方法", ["ブラウザから自動取得", "住所を入力"])

        lat, lng = None, None
        if location_mode == "ブラウザから自動取得":
            try:
                from streamlit_geolocation import streamlit_geolocation

                loc = streamlit_geolocation()
                if loc and loc.get("latitude"):
                    lat, lng = loc["latitude"], loc["longitude"]
                    st.success(f"取得成功: {lat:.4f}, {lng:.4f}")
            except ImportError:
                st.error(
                    "`pip install streamlit-geolocation` が必要です。"
                    "インストール後にこの選択肢が使えます。"
                )
        else:
            address = st.text_input("住所または地名", placeholder="例: 福岡県福岡市中央区")
            if address:
                lat, lng = geocode_address(address)
                if lat:
                    st.success(f"取得成功: {lat:.4f}, {lng:.4f}")

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
            if lat is None or lng is None:
                st.error("現在地が取得できていません。左側で位置情報を設定してください。")
            elif not GOOGLE_MAPS_API_KEY:
                st.error("Google Maps APIキーが未設定のため検索できません。")
            else:
                with st.spinner("候補地を検索中..."):
                    results = run_search(lat, lng, radius_km, purpose, budget_filter, min_rating)

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
                                    f" ／ 現時点の暫定スコア: {r['fallback_score']}"
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
                                reason = get_buzz_reason_gemini(r["name"], purpose)
                            st.caption(f"💬 {reason}")

                        with cols[1]:
                            st.link_button(
                                "🗺️ ナビ開始", navi_url(r["lat"], r["lng"]), use_container_width=True
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
