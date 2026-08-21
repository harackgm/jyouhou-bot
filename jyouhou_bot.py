import os
import sqlite3
import hashlib
import requests
from bs4 import BeautifulSoup

TARGET_URL = "https://fishing-shop-jh.com/"
DEFAULT_LOGO_URL = "https://fishing-shop-jh.com/img/logo.png"
DB_PATH = "products.db"
LINE_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

def generate_item_key(title, url):
    """タイトルとURLの組み合わせから一意の識別キーを生成"""
    raw_str = f"{title.strip()}_{url.strip()}"
    return hashlib.md5(raw_str.encode('utf-8')).hexdigest()

def init_db():
    """DBの初期化および旧テーブル（URL単体キー）からのテーブル移行"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("PRAGMA table_info(notified_products)")
    columns = [col[1] for col in cursor.fetchall()]
    
    if columns and "item_key" not in columns:
        cursor.execute("ALTER TABLE notified_products RENAME TO old_notified_products")
        cursor.execute('''
            CREATE TABLE notified_products (
                item_key TEXT PRIMARY KEY,
                url TEXT,
                title TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute("DROP TABLE old_notified_products")
        conn.commit()
    else:
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS notified_products (
                item_key TEXT PRIMARY KEY,
                url TEXT,
                title TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
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

def is_notified(item_key):
    """タイトル＋URLのペアキーで既読判定"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT 1 FROM notified_products WHERE item_key = ?', (item_key,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def save_notified_bulk(items):
    """複数件を一括でDBに保存"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.executemany('INSERT OR IGNORE INTO notified_products (item_key, title, url) VALUES (?, ?, ?)',
                       [(item_key, title, url) for title, url, item_key in items])
    conn.commit()
    conn.close()

def save_notified(item_key, title, url):
    """単件を通知済みとしてDBに保存"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO notified_products (item_key, title, url) VALUES (?, ?, ?)', (item_key, title, url))
    conn.commit()
    conn.close()

def clean_image_url(raw_url):
    """PC版LINE対策：URLを完璧なHTTPS形式かつ高解像度URLに整える"""
    if not raw_url:
        return DEFAULT_LOGO_URL

    # プロトコル補正
    if raw_url.startswith("//"):
        raw_url = "https:" + raw_url
    elif raw_url.startswith("http://"):
        raw_url = raw_url.replace("http://", "https://", 1)

    # クエリパラメータ(?以降)を排除
    clean_url = raw_url.split('?')[0]

    # サムネイル画像(_th)の指定があれば除去して拡大画像URLにする
    if "_th." in clean_url:
        clean_url = clean_url.replace("_th.", ".")

    return clean_url

def extract_image_from_detail_page(detail_url, headers):
    """商品詳細ページ（2階層下）からルアーのメイン画像を直接抽出"""
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
    """1階層下がカテゴリ一覧の場合は、2階層下の個別商品ページへ潜って画像を確定取得"""
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
            print(f" -> 2階層下の個別商品ページへ移動: {deep_url}")
            img_url = extract_image_from_detail_page(deep_url, headers)
            if img_url:
                return clean_image_url(img_url)

    except Exception as e:
        print(f"画像検索巡回エラー ({target_url}): {e}")

    return DEFAULT_LOGO_URL

def send_flex_message(items):
    """LINE Flex Message (カルーセル) で配信（最大10件ずつ分割）"""
    if not LINE_ACCESS_TOKEN:
        print("エラー: LINE_CHANNEL_ACCESS_TOKEN が設定されていません。")
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
        for title, product_url, img_url in chunk:
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
            print(f"LINE通知送信失敗: {response.status_code} {response.text}")

def get_top_information_items(soup):
    """div.info エリアからINFORMATION内の全リンクを抽出"""
    info_div = soup.find("div", class_="info")
    if not info_div:
        print("[WARN] div.info が見つかりませんでした。")
        return []

    items = []
    seen_keys = set()

    for a_tag in info_div.find_all("a", href=True):
        href = a_tag["href"]
        text = a_tag.get_text(strip=True)

        if href and text and len(text) > 2:
            full_url = requests.compat.urljoin(TARGET_URL, href)
            item_key = generate_item_key(text, full_url)
            
            if item_key not in seen_keys:
                items.append((text, full_url, item_key))
                seen_keys.add(item_key)

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
    print(f"[DEBUG] 取得されたINFORMATION全件数: {len(all_items)}件")

    if not all_items:
        print("INFORMATION枠内に商品が見つかりませんでした。")
        return

    # 初回移行（DB件数0件）の場合は全件を一括既読登録して終了する
    if get_db_count() == 0:
        print(f"[INFO] 初回データベース初期化: 現存する{len(all_items)}件を既読登録します。")
        save_notified_bulk(all_items)
        print("[INFO] 初期化完了。次回から追加・更新分のみ通知します。")
        return

    # 全件チェックして未通知アイテムを確実に抽出
    new_items = []
    for title, url, item_key in all_items:
        if not is_notified(item_key):
            new_items.append((title, url, item_key))

    print(f"[DEBUG] 未通知の新規件数: {len(new_items)}件")

    if not new_items:
        print("INFORMATION内に新しい未通知商品はありませんでした。")
        return

    processed_items = []

    # 各アイテムを個別に安全処理
    for title, url, item_key in new_items:
        print(f"新着商品処理中: {title}")
        try:
            img_url = fetch_product_image_url(url, headers)
            print(f" -> 最終決定画像URL: {img_url}")
            processed_items.append((title, url, img_url))
            save_notified(item_key, title, url)
        except Exception as e:
            print(f"エラー発生 ({title}): {e}")
            # エラー時もロゴ指定でLINE通知対象に入れてスキップ防止
            processed_items.append((title, url, DEFAULT_LOGO_URL))
            save_notified(item_key, title, url)

    if processed_items:
        send_flex_message(processed_items)

if __name__ == "__main__":
    main()
