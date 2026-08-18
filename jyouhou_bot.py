import os
import sqlite3
import requests
from bs4 import BeautifulSoup

TARGET_URL = "https://fishing-shop-jh.com/"
DEFAULT_LOGO_URL = "https://fishing-shop-jh.com/img/logo.png"
DB_PATH = "products.db"
LINE_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS notified_products (
            url TEXT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def is_notified(url):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT 1 FROM notified_products WHERE url = ?', (url,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def save_notified(url):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO notified_products (url) VALUES (?)', (url,))
    conn.commit()
    conn.close()

def fetch_product_image_url(target_url, headers):
    """商品詳細ページおよびカテゴリ一覧ページから最適な画像を安全に抽出"""
    try:
        res = requests.get(target_url, headers=headers, timeout=10)
        res.encoding = res.apparent_encoding
        soup = BeautifulSoup(res.text, "html.parser")
        
        img_url = None

        # 1. OGP画像 (og:image) を優先取得
        og_img = soup.find("meta", property="og:image")
        if og_img and og_img.get("content"):
            content = og_img["content"]
            # デフォルトロゴ以外の固有画像であれば採用
            if "logo" not in content.lower():
                img_url = content

        # 2. 商品詳細ページのメイン画像枠から検索
        if not img_url:
            img_tag = soup.select_one(".product_image img, .img_box img, #product_image img, .product-img img")
            if img_tag and img_tag.get("src"):
                img_url = requests.compat.urljoin(target_url, img_tag["src"])

        # 3. カテゴリ・一覧ページの場合：ページ内にある最初の「商品一覧サムネイル画像」を取得
        if not img_url:
            # カラーミーショップ等の一覧用画像セレクター
            list_img_tag = soup.select_one(".product_list img, .item_list img, .product_data img, table.product img, .info_detail img")
            if list_img_tag and list_img_tag.get("src"):
                img_url = requests.compat.urljoin(target_url, list_img_tag["src"])

        # 4. それでも見つからない場合：ページ内の「upload」や「product」を含む最初のimgタグを探す
        if not img_url:
            for img in soup.find_all("img", src=True):
                src = img["src"]
                if "upload" in src or "product" in src or "shop" in src:
                    if "logo" not in src.lower() and "icon" not in src.lower():
                        img_url = requests.compat.urljoin(target_url, src)
                        break

        # 画像URLが補正できたらHTTPS化して返却
        if img_url:
            if img_url.startswith("http://"):
                img_url = img_url.replace("http://", "https://", 1)
            return img_url

    except Exception as e:
        print(f"画像取得エラー ({target_url}): {e}")
    
    # 最終的に画像が見つからない場合は店舗ロゴを使用
    return DEFAULT_LOGO_URL

def send_flex_message(items):
    """LINE Flex Message (カルーセル) で画像付き通知を配信（最大10件ずつ分割）"""
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
    seen_urls = set()

    for a_tag in info_div.find_all("a", href=True):
        href = a_tag["href"]
        text = a_tag.get_text(strip=True)

        if href and text and len(text) > 2:
            full_url = requests.compat.urljoin(TARGET_URL, href)
            if full_url not in seen_urls:
                items.append((text, full_url))
                seen_urls.add(full_url)

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

    new_items = []
    for title, url in all_items:
        if not is_notified(url):
            new_items.append((title, url))

    print(f"[DEBUG] 未通知の新規件数: {len(new_items)}件")

    if not new_items:
        print("INFORMATION内に新しい未通知商品はありませんでした。")
        return

    processed_items = []

    for title, url in new_items:
        print(f"新着商品処理中: {title}")
        img_url = fetch_product_image_url(url, headers)
        print(f" -> 取得画像URL: {img_url}")
        processed_items.append((title, url, img_url))
        save_notified(url)

    send_flex_message(processed_items)

if __name__ == "__main__":
    main()
