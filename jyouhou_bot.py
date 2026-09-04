import os
import sqlite3
import hashlib
import requests
import re
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta

TARGET_URL = "https://fishing-shop-jh.com/"
DEFAULT_LOGO_URL = "https://fishing-shop-jh.com/img/logo.png"
PRE_ANNOUNCEMENT_IMAGE_URL = "https://raw.githubusercontent.com/harackgm/jyouhou-bot/main/Jzyunbi.jpg"

# ブログ用のRSSフィードURL
BLOG_RSS_URL = "https://rssblog.ameba.jp/jyouhou-since1957/rss20.xml"

DB_PATH = "products.db" 
LINE_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

# --- 安全装置の設定 ---
MAX_NOTIFY_LIMIT = 24  
MAX_TRACK_LIMIT = 50   

# --- 日本時間(JST)の設定 ---
JST = timezone(timedelta(hours=+9), 'JST')

def log(msg):
    now = datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{now}] {msg}")

def generate_item_key(title, url):
    raw_str = f"{title.strip()}_{url.strip()}"
    return hashlib.md5(raw_str.encode('utf-8')).hexdigest()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # 商品用のDB
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS snapshot_v3 (
            rank INTEGER PRIMARY KEY,
            item_key TEXT,
            title TEXT,
            url TEXT,
            status TEXT
        )
    ''')
    cursor.execute('SELECT COUNT(*) FROM snapshot_v3')
    if cursor.fetchone()[0] == 0:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='snapshot_v2'")
        if cursor.fetchone():
            cursor.execute('SELECT rank, item_key, title, url FROM snapshot_v2')
            for r in cursor.fetchall():
                cursor.execute('INSERT INTO snapshot_v3 (rank, item_key, title, url, status) VALUES (?, ?, ?, ?, ?)', 
                               (r[0], r[1], r[2], r[3], 'full'))
            log("[INFO] データベース構造をv3に安全にアップグレードしました。")

    # システム状態管理
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS system_status (
            id INTEGER PRIMARY KEY,
            is_limited INTEGER
        )
    ''')
    cursor.execute('INSERT OR IGNORE INTO system_status (id, is_limited) VALUES (1, 0)')
    
    # 【新規】ブログ専用のDBテーブル作成
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS blog_snapshot (
            rank INTEGER PRIMARY KEY,
            item_key TEXT,
            title TEXT,
            url TEXT
        )
    ''')
    
    conn.commit()
    conn.close()

def get_system_status():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT is_limited FROM system_status WHERE id = 1')
    res = cursor.fetchone()
    conn.close()
    return res[0] if res else 0

def set_system_status(is_limited):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('UPDATE system_status SET is_limited = ? WHERE id = 1', (is_limited,))
    conn.commit()
    conn.close()

# --- 商品データDB操作 ---
def get_previous_snapshot():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT item_key, rank, status FROM snapshot_v3 ORDER BY rank ASC')
    data = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}
    conn.close()
    return data

def save_snapshot(snapshot_data):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM snapshot_v3')
    for rank, (title, url, item_key, status) in enumerate(snapshot_data):
        cursor.execute('INSERT INTO snapshot_v3 (rank, item_key, title, url, status) VALUES (?, ?, ?, ?, ?)', 
                       (rank, item_key, title, url, status))
    conn.commit()
    conn.close()

# --- ブログデータDB操作 ---
def get_previous_blog_snapshot():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT item_key FROM blog_snapshot ORDER BY rank ASC')
    keys = [row[0] for row in cursor.fetchall()]
    conn.close()
    return keys

def save_blog_snapshot(blog_items):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM blog_snapshot')
    for rank, (title, url, item_key) in enumerate(blog_items):
        cursor.execute('INSERT INTO blog_snapshot (rank, item_key, title, url) VALUES (?, ?, ?, ?)', 
                       (rank, item_key, title, url))
    conn.commit()
    conn.close()

# --- 画像・情報取得系 ---
def clean_image_url(raw_url):
    if not raw_url:
        return DEFAULT_LOGO_URL
    if raw_url.startswith("//"):
        raw_url = "https:" + raw_url
    elif raw_url.startswith("http://"):
        raw_url = raw_url.replace("http://", "https://", 1)
    clean_url = raw_url.split('?')[0]
    if "_th." in clean_url:
        clean_url = clean_url.replace("_th.", ".")
    return clean_url

def extract_image_from_detail_page(detail_url, headers):
    try:
        res = requests.get(detail_url, headers=headers, timeout=10)
        res.encoding = res.apparent_encoding
        soup = BeautifulSoup(res.text, "html.parser")

        og_img = soup.find("meta", property="og:image")
        if og_img and og_img.get("content"):
            content = og_img["content"]
            if "logo" not in content.lower() and "icon" not in content.lower():
                return content

        img_tag = soup.select_one(".product_image img, .img_box img, #product_image img, .product-img img")
        if img_tag and img_tag.get("src"):
            return requests.compat.urljoin(detail_url, img_tag["src"])

        for img in soup.find_all("img", src=True):
            src = img["src"].lower()
            if any(k in src for k in ["upload", "product", "goods", "item"]):
                if not any(k in src for k in ["logo", "icon", "banner", "instagram", "facebook", "twitter", "line"]):
                    return requests.compat.urljoin(detail_url, img["src"])
    except Exception as e:
        log(f"[ERROR] 詳細ページ画像取得エラー ({detail_url}): {e}")
    return None

def fetch_product_details(target_url, headers):
    img_url = DEFAULT_LOGO_URL
    price_text = ""
    try:
        res = requests.get(target_url, headers=headers, timeout=10)
        res.encoding = res.apparent_encoding
        soup = BeautifulSoup(res.text, "html.parser")

        price_elem = soup.select_one(".item_price, .price, .sales_price")
        if price_elem:
            price_text = price_elem.get_text(strip=True)

        extracted_img = extract_image_from_detail_page(target_url, headers)
        if extracted_img:
            img_url = clean_image_url(extracted_img)
        else:
            first_product_a = soup.select_one(".product_list a, .item_list a, .product_data a, table.product a, .info_detail a, a[href*='pid=']")
            if first_product_a and first_product_a.get("href"):
                deep_url = requests.compat.urljoin(target_url, first_product_a["href"])
                deep_img = extract_image_from_detail_page(deep_url, headers)
                if deep_img:
                    img_url = clean_image_url(deep_img)
    except Exception as e:
        log(f"[ERROR] 詳細情報取得エラー ({target_url}): {e}")

    return img_url, price_text

def check_is_pre_announcement(url, headers):
    try:
        res = requests.get(url, headers=headers, timeout=10)
        res.encoding = res.apparent_encoding
        text = res.text
        if "現在、この商品は扱っておりません。" in text or "該当する商品がありません" in text:
            return True
        return False
    except Exception as e:
        log(f"[ERROR] 準備中チェックエラー ({url}): {e}")
        return True

def get_latest_blog_posts():
    """RSSを使用してブログの最新記事情報を安全・軽量に取得する"""
    try:
        res = requests.get(BLOG_RSS_URL, timeout=10)
        res.raise_for_status()
        root = ET.fromstring(res.text)
        items = []
        for item in root.findall('.//item'):
            title = item.find('title').text
            url = item.find('link').text
            key = generate_item_key(title, url)
            items.append((title, url, key))
            if len(items) >= MAX_TRACK_LIMIT:
                break
        return items
    except Exception as e:
        log(f"[ERROR] ブログRSS取得エラー: {e}")
        return []

# --- LINE通知系 ---
def send_flex_message(items):
    if not LINE_ACCESS_TOKEN:
        log("[ERROR] LINE_CHANNEL_ACCESS_TOKEN が設定されていません。")
        return False

    is_recovery = get_system_status()
    total_items = len(items)
    chunk_size = 12 if total_items > 15 else 5

    url = "https://api.line.me/v2/bot/message/broadcast"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_ACCESS_TOKEN.strip()}"
    }

    messages_payload = []

    if is_recovery == 1:
        messages_payload.append({
            "type": "text",
            "text": "【システム通知】\n月間のLINE通信制限がリセットされたため、保留されていた新着情報をお届けします。"
        })

    for i in range(0, total_items, chunk_size):
        chunk = items[i:i + chunk_size]
        bubbles = []
        for title, product_url, img_url, item_key, price_text in chunk:
            display_title = title.strip() if (title and title.strip()) else "新着・再入荷情報"
            display_img = img_url if img_url else DEFAULT_LOGO_URL

            body_contents = [
                {
                    "type": "text",
                    "text": display_title,
                    "size": "md",
                    "wrap": True,
                    "weight": "bold"
                }
            ]
            if price_text:
                body_contents.append({
                    "type": "text",
                    "text": price_text,
                    "size": "sm",
                    "color": "#ff0000",
                    "weight": "bold",
                    "margin": "md"
                })

            bubble = {
                "type": "bubble",
                "hero": {
                    "type": "image",
                    "url": display_img,
                    "size": "full",
                    "aspectRatio": "4:3",
                    "aspectMode": "cover"
                },
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": body_contents
                },
                "footer": {
                    "type": "box",
                    "layout": "vertical",
                    "spacing": "sm",
                    "contents": [
                        {
                            "type": "button",
                            "style": "primary",
                            "color": "#1DB954",
                            "action": {
                                "type": "uri",
                                "label": "ページを開く",
                                "uri": product_url
                            }
                        }
                    ]
                }
            }
            bubbles.append(bubble)

        flex_msg = {
            "type": "flex",
            "altText": f"城峰釣具店 新着通知 ({len(chunk)}件)",
            "contents": {
                "type": "carousel",
                "contents": bubbles
            }
        }
        messages_payload.append(flex_msg)

    payload = {"messages": messages_payload}
    
    response = requests.post(url, headers=headers, json=payload)
    if response.status_code == 200:
        log(f"[INFO] LINE画像付きFlex通知送信成功 (計{total_items}件 / カルーセル数:{len(messages_payload)})")
        set_system_status(0)
        return True
    elif response.status_code == 429:
        log("[ERROR] 今月分のLINE通知上限（200通）に到達しました。翌月まで通知は送信されません。")
        set_system_status(1)
        return False
    else:
        log(f"[ERROR] LINE通知送信失敗: {response.status_code} {response.text}")
        return False

def send_summary_message(new_items):
    if not LINE_ACCESS_TOKEN:
        return False
    
    is_recovery = get_system_status()
    url = "https://api.line.me/v2/bot/message/broadcast"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_ACCESS_TOKEN.strip()}"
    }
    
    sample_texts = "\n".join([f"・{title}" for title, _, _, _, _ in new_items[:5]])
    more_text = f"\n...他 {len(new_items) - 5}件" if len(new_items) > 5 else ""
    
    recovery_text = "【システム通知】\n月間のLINE通信制限がリセットされたため、保留されていた情報をお届けします。\n\n" if is_recovery == 1 else ""
    
    text = (f"{recovery_text}⚠️ 【城峰釣具店】更新アラート ⚠️\n"
            f"一気に {len(new_items)} 件の更新が検知されました。\n"
            f"※大量通知防止ガードが作動したため、個別画像通知をスキップしました。\n\n"
            f"【更新内容の一部】\n{sample_texts}{more_text}\n\n"
            f"▼詳細はサイトをご確認ください\n{TARGET_URL}")
            
    payload = {
        "messages": [{"type": "text", "text": text}]
    }
    response = requests.post(url, headers=headers, json=payload)
    if response.status_code == 200:
        log("[INFO] サマリー通知を送信しました。")
        set_system_status(0)
        return True
    elif response.status_code == 429:
        log("[ERROR] 今月分のLINE通知上限（200通）に到達しました。翌月まで通知は送信されません。")
        set_system_status(1)
        return False
    else:
        log(f"[ERROR] サマリー通知送信失敗: {response.status_code} {response.text}")
        return False

def get_top_information_items(soup):
    info_div = soup.find("div", class_="info")
    if not info_div:
        log("[WARN] div.info が見つかりませんでした。")
        return []

    items = []
    seen_keys = set()

    for a_tag in info_div.find_all("a", href=True):
        href = a_tag["href"]
        full_title = re.sub(r'\s+', ' ', a_tag.get_text(strip=True))

        if not href or len(full_title) <= 2:
            continue

        full_url = requests.compat.urljoin(TARGET_URL, href)
        item_key = generate_item_key(full_title, full_url)
        
        if item_key not in seen_keys:
            items.append((full_title, full_url, item_key))
            seen_keys.add(item_key)
            if len(items) >= MAX_TRACK_LIMIT:
                break
    return items

def main():
    current_hour = datetime.now(JST).hour
    if 0 <= current_hour < 8:
        log("深夜帯（0:00〜7:59）のため、サーバー負荷軽減と深夜通知防止のため監視をスキップします。")
        return

    init_db()

    # ==========================================
    # 1. ブログ監視（現在はテストモード：記憶のみでLINE通知なし）
    # ==========================================
    log("城峰釣具店ブログ (RSS監視) を開始します...")
    blog_items = get_latest_blog_posts()
    
    if blog_items:
        log(f"[DEBUG] ブログ最新データ件数: {len(blog_items)}件")
        prev_blog_keys = get_previous_blog_snapshot()
        
        if not prev_blog_keys:
            log(f"[INFO] ブログDB初期化: 現在の最新{len(blog_items)}件を記憶します。")
            save_blog_snapshot(blog_items)
            log("[INFO] ブログの初期化完了。次回から新着を検知します。")
        else:
            new_blogs = []
            for b_title, b_url, b_key in blog_items:
                if b_key not in prev_blog_keys:
                    new_blogs.append((b_title, b_url, b_key))
            
            if new_blogs:
                log(f"[INFO] 【テストモード】ブログの新着が {len(new_blogs)}件 ありますが、安全確認期間のためLINE通知せず記憶だけ行います。")
                save_blog_snapshot(blog_items)
            else:
                log("[INFO] ブログの新しい記事はありませんでした。")

    # ==========================================
    # 2. 商品監視（通常稼働）
    # ==========================================
    log("城峰釣具店 商品情報 (INFORMATION監視) を開始します...")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        response = requests.get(TARGET_URL, headers=headers, timeout=10)
        response.encoding = response.apparent_encoding
        soup = BeautifulSoup(response.text, "html.parser")
    except Exception as e:
        log(f"[ERROR] Webサイトの取得に失敗しました: {e}")
        return

    all_items = get_top_information_items(soup)
    log(f"[DEBUG] 商品情報 監視対象データ件数: {len(all_items)}件")

    if not all_items:
        log("[INFO] INFORMATION枠内に商品が見つかりませんでした。")
        return

    prev_snapshot = get_previous_snapshot()

    if not prev_snapshot:
        log(f"[INFO] データベース初期化: 現在の最新{len(all_items)}件をすべて本掲載扱いとして記憶します。")
        initial_data = [(title, url, item_key, 'full') for _, (title, url, item_key) in enumerate(all_items)]
        save_snapshot(initial_data)
        log("[INFO] 初期化完了。次回から追加・更新分のみ通知します。")
        return

    items_to_notify = []
    next_snapshot_data = []
    existing_full_items = []
    
    for curr_rank, (title, url, item_key) in enumerate(all_items):
        if item_key not in prev_snapshot:
            is_pre = check_is_pre_announcement(url, headers)
            status = 'pre' if is_pre else 'full'
            notify_type = 'pre_new' if is_pre else 'full_new'
            
            items_to_notify.append((title, url, item_key, curr_rank, notify_type))
            next_snapshot_data.append((title, url, item_key, status))
            log(f"[DEBUG] 新規追加検知: {title} (状態: {status})")
            
        else:
            prev_rank, prev_status = prev_snapshot[item_key]
            
            if prev_status == 'pre':
                is_pre = check_is_pre_announcement(url, headers)
                if is_pre:
                    next_snapshot_data.append((title, url, item_key, 'pre'))
                else:
                    notify_type = 'full_upgrade'
                    items_to_notify.append((title, url, item_key, curr_rank, notify_type))
                    next_snapshot_data.append((title, url, item_key, 'full'))
                    log(f"[DEBUG] 本掲載への昇格を検知: {title}")
            else:
                next_snapshot_data.append((title, url, item_key, 'full'))
                existing_full_items.append((curr_rank, prev_rank, title, url, item_key))

    for idx, (curr_rank, prev_rank, title, url, item_key) in enumerate(existing_full_items):
        rest_prev_ranks = [item[1] for item in existing_full_items[idx+1:]]
        if rest_prev_ranks:
            min_prev_in_rest = min(rest_prev_ranks)
            if prev_rank > min_prev_in_rest:
                items_to_notify.append((title, url, item_key, curr_rank, 'full_resurface'))
                log(f"[DEBUG] 再浮上（上積み）検知: {title} (前回{prev_rank}位 -> 今回{curr_rank}位)")

    items_to_notify = sorted(items_to_notify, key=lambda x: x[3])
    log(f"[DEBUG] 抽出された商品通知対象: {len(items_to_notify)}件")

    notify_success = True

    if items_to_notify:
        if len(items_to_notify) > MAX_NOTIFY_LIMIT:
            log(f"[WARN] 検知が{len(items_to_notify)}件と多いため、LINE通知枠保護により個別送信をスキップし、サマリー通知を送信します。")
            notify_success = send_summary_message(items_to_notify)
        else:
            processed_items = []
            for title, url, item_key, rank, notify_type in items_to_notify:
                log(f"[INFO] 通知処理中: {title} (タイプ: {notify_type})")
                
                if notify_type == 'pre_new':
                    display_title = f"【事前告知(写真待)】 {title}"
                    img_url = PRE_ANNOUNCEMENT_IMAGE_URL
                    price_text = "価格: 準備中"
                elif notify_type == 'full_upgrade':
                    display_title = f"【本掲載開始！】 {title}"
                    img_url, price_text = fetch_product_details(url, headers)
                else:
                    display_title = title
                    img_url, price_text = fetch_product_details(url, headers)
                    
                processed_items.append((display_title, url, img_url, item_key, price_text))

            if processed_items:
                notify_success = send_flex_message(processed_items)
    else:
        log("[INFO] 新しい未通知商品、または本掲載への昇格はありませんでした。")

    if notify_success:
        save_snapshot(next_snapshot_data)
        log("--- 処理が正常に完了しました ---")
    else:
        log("[WARN] LINE通知に失敗したため、次回の再試行のためにデータベースの更新をスキップしました。")

if __name__ == "__main__":
    main()
