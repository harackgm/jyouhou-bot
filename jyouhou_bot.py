import os
import sqlite3
import requests
from bs4 import BeautifulSoup

TARGET_URL = "https://fishing-shop-jh.com/"
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

def fetch_product_image_url(product_url, headers):
    """商品詳細ページからメイン画像を抽出"""
    try:
        res = requests.get(product_url, headers=headers, timeout=10)
        res.encoding = res.apparent_encoding
        soup = BeautifulSoup(res.text, "html.parser")
        
        img_url = None
        # OGP画像を優先取得
        og_img = soup.find("meta", property="og:image")
        if og_img and og_img.get("content"):
            img_url = og_img["content"]
        else:
            # カラーミーショップ標準の商品画像枠
            img_tag = soup.select_one(".product_image img, .img_box img, #product_image img, .product-img img")
            if img_tag and img_tag.get("src"):
                img_url = requests.compat.urljoin(product_url, img_tag["src"])
        
        if img_url and img_url.startswith("http://"):
            img_url = img_url.replace("http://", "https://", 1)
            
        return img_url

    except Exception as e:
        print(f"画像取得エラー ({product_url}): {e}")
    
    return None

def send_flex_message(items):
    """LINE Flex Message (カルーセル) で画像付き通知を配信"""
    if not LINE_ACCESS_TOKEN:
        print("エラー: LINE_CHANNEL_ACCESS_TOKEN が設定されていません。")
        return

    url = "https://api.line.me/v2/bot/message/broadcast"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_ACCESS_TOKEN}"
    }

    bubbles = []
    for title, product_url, img_url in items:
        display_title = title.strip() if (title and title.strip()) else "新着・再入荷商品"
        display_img = img_url if img_url else "https://fishing-shop-jh.com/img/logo.png"

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
                            "label": "商品ページを開く",
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
                "altText": f"城峰釣具店 INFORMATION新着通知 ({len(items)}件)",
                "contents": {
                    "type": "carousel",
                    "contents": bubbles
                }
            }
        ]
    }

    response = requests.post(url, headers=headers, json=payload)
    if response.status_code == 200:
        print("LINE画像付きFlex通知送信成功")
    else:
        print(f"LINE通知送信失敗: {response.status_code} {response.text}")

def get_information_items(soup, headers):
    """INFORMATIONのスクロール枠内から上から順に（最新順で）商品リンクを取得"""
    items = []
    
    # INFORMATION枠（marquee または特定のスクロールエリア）を探索
    info_container = soup.find("marquee")
    
    if not info_container:
        # marqueeタグがない場合、INFORMATION表記のあるブロック内のスクロール要素を探す
        for element in soup.find_all(["div", "td", "section"]):
            if "INFORMATION" in element.get_text():
                # 内部にあるリンク付きタグを探索
                info_container = element
                break

    if not info_container:
        info_container = soup

    # 上から順に <a> タグを取得
    for a_tag in info_container.find_all("a", href=True):
        href = a_tag["href"]
        text = a_tag.get_text(strip=True)
        
        # 商品詳細ページ（pid=を含む）で、テキストが存在するもの
        if "pid=" in href and text:
            full_url = requests.compat.urljoin(TARGET_URL, href)
            # 全体のトップページや無効URLを除外
            if full_url != TARGET_URL:
                items.append((text, full_url))
                
    return items

def main():
    init_db()
    print("城峰釣具店 (INFORMATION上部・最新順) の巡回チェックを開始します...")
    
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

    raw_items = get_information_items(soup, headers)

    if not raw_items:
        print("INFORMATION枠内に商品が見つかりませんでした。")
        return

    # 取得順（＝ページの掲載順＝最新順）を維持したまま、重複URLを除外
    unique_items = []
    seen_urls = set()
    for title, url in raw_items:
        if url not in seen_urls:
            unique_items.append((title, url))
            seen_urls.add(url)

    # 未通知の商品のみを抽出（上から順）
    new_items = []
    for title, url in unique_items:
        if not is_notified(url):
            new_items.append((title, url))

    if not new_items:
        print("INFORMATION内に新着・未通知の商品はありませんでした。")
        return

    # 最新の上位10件を対象
    target_items = new_items[:10]
    processed_items = []

    for title, url in target_items:
        print(f"INFORMATION最新商品を取得中: {title}")
        img_url = fetch_product_image_url(url, headers)
        processed_items.append((title, url, img_url))
        save_notified(url)

    send_flex_message(processed_items)

if __name__ == "__main__":
    main()
