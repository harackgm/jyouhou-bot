import os
import sqlite3
import hashlib
import requests
import re
from bs4 import BeautifulSoup

TARGET_URL = "https://fishing-shop-jh.com/"
DEFAULT_LOGO_URL = "https://fishing-shop-jh.com/img/logo.png"
DB_PATH = "products.db"
LINE_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

# --- 安全装置の設定 ---
MAX_NOTIFY_LIMIT = 10  # 大量検知時の事故防止ガード
MAX_TRACK_LIMIT = 50   # DBに記憶しておく件数

def generate_item_key(title, url):
    """タイトルとURLの組み合わせから一意の識別キーを生成"""
    raw_str = f"{title.strip()}_{url.strip()}"
    return hashlib.md5(raw_str.encode('utf-8')).hexdigest()

def init_db():
    """DBの初期化（順位カラムの自動追加対応）"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS notified_products (
            item_key TEXT PRIMARY KEY,
            url TEXT,
            title TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    try:
        cursor.execute('ALTER TABLE notified_products ADD COLUMN rank INTEGER DEFAULT 999')
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()

def get_db_count():
    """DB内の総件数を取得"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM notified_products')
    count = cursor.fetchone()[0]
    conn.close()
    return count

def get_notified_record(item_key):
    """DBから特定のアイテムの順位（rank）を取得"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT rank FROM notified_products WHERE item_key = ?', (item_key,))
    result = cursor.fetchone()
    conn.close()
    return result

def sync_active_items(active_items):
    """現在の上位リストに合わせてDBの「順位」を更新し、圏外を忘却する"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    for current_rank, (title, url, item_key) in enumerate(active_items):
        cursor.execute('UPDATE notified_products SET rank = ?, title = ?, url = ? WHERE item_key = ?', 
                       (current_rank, title, url, item_key))
        if cursor.rowcount == 0:
            cursor.execute('INSERT INTO notified_products (item_key, title, url, rank) VALUES (?, ?, ?, ?)', 
                           (item_key, title, url, current_rank))
            
    active_keys = [item_key for _, _, item_key in active_items]
    if active_keys:
        placeholders = ','.join(['?'] * len(active_keys))
        cursor.execute(f'DELETE FROM notified_products WHERE item_key NOT IN ({placeholders})', tuple(active_keys))
        deleted_count = cursor.rowcount
    else:
        cursor.execute('DELETE FROM notified_products')
        deleted_count = cursor.rowcount
        
    conn.commit()
    conn.close()
    
    if deleted_count > 0:
        print(f"[INFO] 圏外に押し出された古いデータ {deleted_count} 件をDBから整理しました。")

def clean_image_url(raw_url):
    """画像URL整形"""
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
    """商品詳細ページから画像を抽出"""
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
        print(f"詳細ページ画像取得エラー ({detail_url}): {e}")
    return None

def fetch_product_image_url(target_url, headers):
    """階層を辿って商品画像を取得"""
    try:
        res = requests.get(target_url, headers=headers, timeout=10)
        res.encoding = res.apparent_encoding
        soup = BeautifulSoup(res.text, "html.parser")

        img_url = extract_image_from_detail_page(target_url, headers)
        if img_url:
            return clean_image_url(img_url)

        first_product_a = soup.select_one(".product_list a, .item_list a, .product_data a, table.product a, .info_detail a, a[href*='pid=']")
        if first_product_a and first_product_a.get("href"):
            deep_url = requests.compat.urljoin(target_url, first_product_a["href"])
            img_url = extract_image_from_detail_page(deep_url, headers)
            if img_url:
                return clean_image_url(img_url)

    except Exception as e:
        print(f"画像検索巡回エラー ({target_url}): {e}")

    return DEFAULT_LOGO_URL

def send_flex_message(items):
    """LINE Flex Message 送信"""
    if not LINE_ACCESS_TOKEN:
        print("[ERROR] LINE_CHANNEL_ACCESS_TOKEN が設定されていません。")
        return

    chunk_size = 10
    for i in range(0, len(items), chunk_size):
        chunk = items[i:i + chunk_size]
        url = "https://api.line.me/v2/bot/message/broadcast"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LINE_ACCESS_TOKEN.strip()}"
        }

        bubbles = []
        for title, product_url, img_url, _ in chunk:
            display_title = title.strip() if (title and title.strip()) else "新着・再入荷情報"
            display_img = img_url if img_url else DEFAULT_LOGO_URL

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
                    "contents": [
                        {
                            "type": "text",
                            "text": display_title,
                            "weight": "bold",
                            "size": "md",
                            "wrap": True
                        }
                    ]
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

        payload = {
            "messages": [
                {
                    "type": "flex",
                    "altText": f"城峰釣具店 INFORMATION新着通知 ({len(chunk)}件)",
                    "contents": {
                        "type": "carousel",
                        "contents": bubbles
                    }
                }
            ]
        }

        response = requests.post(url, headers=headers, json=payload)
        if response.status_code == 200:
            print(f"LINE画像付きFlex通知送信成功 ({len(chunk)}件)")
        else:
            print(f"[ERROR] LINE通知送信失敗: {response.status_code} {response.text}")

def send_summary_message(new_items):
    """大量更新時にメッセージ枠を守りつつ概要だけを1通で通知する"""
    if not LINE_ACCESS_TOKEN:
        return
    
    url = "https://api.line.me/v2/bot/message/broadcast"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_ACCESS_TOKEN.strip()}"
    }
    
    sample_texts = "\n".join([f"・{title}" for title, _, _, _ in new_items[:5]])
    more_text = f"\n...他 {len(new_items) - 5}件" if len(new_items) > 5 else ""
    
    text = (f"⚠️ 【城峰釣具店】更新アラート ⚠️\n"
            f"一気に {len(new_items)} 件の新着・再入荷が検知されました。\n"
            f"※大量通知防止ガードが作動したため、個別画像通知をスキップしました。\n\n"
            f"【更新内容の一部】\n{sample_texts}{more_text}\n\n"
            f"▼詳細はサイトをご確認ください\n{TARGET_URL}")
            
    payload = {
        "messages": [{"type": "text", "text": text}]
    }
    requests.post(url, headers=headers, json=payload)
    print("[INFO] サマリー通知を送信しました。")

def get_top_information_items(soup):
    """INFORMATIONエリア（div.info直下のdl）から正確にアイテム情報を取得"""
    info_area = soup.find("div", class_="info")
    if not info_area:
        print("[WARN] div.info が見つかりませんでした。")
        return []

    dl_tag = info_area.find("dl", class_="info_detail")
    if not dl_tag:
        print("[WARN] info_detail の dlタグが見つかりませんでした。")
        return []

    items = []
    seen_keys = set()

    # 各行（<strong>または<a>が含まれるまとまり）を安全に解析するため、子要素のaタグを基準に走査
    for a_tag in dl_tag.find_all("a", href=True):
        href = a_tag["href"]
        link_text = a_tag.get_text(" ", strip=True)
        
        if not href or len(link_text) <= 2:
            continue

        # aタグが含まれている親の strong タグ、またはその周辺のテキスト（NEWや予約など）を結合
        parent_strong = a_tag.find_parent("strong")
        if parent_strong:
            # strongタグ全体のテキストを取得（例: "NEW 【ロデオクラフト USSA 26シグネイチャー】"）
            full_title = re.sub(r'\s+', ' ', parent_strong.get_text(" ", strip=True)).strip()
        else:
            # 万が一構造が違う場合のフォールバック
            full_title = link_text

        full_url = requests.compat.urljoin(TARGET_URL, href)
        item_key = generate_item_key(full_title, full_url)
        
        if item_key not in seen_keys:
            items.append((full_title, full_url, item_key))
            seen_keys.add(item_key)
            
            if len(items) >= MAX_TRACK_LIMIT:
                break

    return items

def main():
    init_db()
    print("城峰釣具店 (INFORMATION監視) を開始します...")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        response = requests.get(TARGET_URL, headers=headers, timeout=10)
        response.encoding = response.apparent_encoding
        soup = BeautifulSoup(response.text, "html.parser")
    except Exception as e:
        print(f"Webサイトの取得に失敗しました: {e}")
        return

    all_items = get_top_information_items(soup)
    print(f"[DEBUG] 監視対象とする上位最新データ件数: {len(all_items)}件")

    if not all_items:
        print("INFORMATION枠内に商品が見つかりませんでした。")
        return

    if get_db_count() == 0:
        print(f"[INFO] 初回データベース初期化: 最新{len(all_items)}件を既読登録します。")
        sync_active_items(all_items)
        print("[INFO] 初期化完了。次回から追加・更新分のみ通知します。")
        return

    new_items = []
    for current_rank, (title, url, item_key) in enumerate(all_items):
        record = get_notified_record(item_key)
        
        if not record:
            new_items.append((title, url, item_key, current_rank))
        else:
            previous_rank = record[0]
            if previous_rank is None:
                previous_rank = 999
                
            if current_rank <= 4 and current_rank < previous_rank:
                if current_rank == 0 or (previous_rank - current_rank >= 2):
                    print(f"[DEBUG] 浮上検知: {title} (前回{previous_rank}位 -> 今回{current_rank}位)")
                    new_items.append((title, url, item_key, current_rank))

    print(f"[DEBUG] 検知された新規・再入荷件数: {len(new_items)}件")

    if new_items:
        if len(new_items) > MAX_NOTIFY_LIMIT:
            print(f"[WARN] 検知が{len(new_items)}件と多いため、LINE通知枠保護により個別送信をスキップし、サマリー通知を送信します。")
            send_summary_message(new_items)
        else:
            processed_items = []
            for title, url, item_key, rank in new_items:
                print(f"新着商品処理中: {title}")
                try:
                    img_url = fetch_product_image_url(url, headers)
                    processed_items.append((title, url, img_url, item_key))
                except Exception as e:
                    print(f"エラー発生 ({title}): {e}")
                    processed_items.append((title, url, DEFAULT_LOGO_URL, item_key))

            if processed_items:
                send_flex_message(processed_items)
    else:
        print("INFORMATION内に新しい未通知商品はありませんでした。")

    sync_active_items(all_items)
    print("--- 処理が正常に完了しました ---")

if __name__ == "__main__":
    main()
