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

def get_information_items_strictly(soup):
    """INFORMATION（スクロール枠）のみにピンポイントで絞り込み"""
    items = []
    
    # 1. marquee タグを最優先検索
    info_area = soup.find("marquee")
    
    # 2. marquee がない場合、"INFORMATION" 直後のテーブル/スクロール枠をピンポイント取得
    if not info_area:
        for tag in soup.find_all(["td", "div"]):
            # おすすめ商品やRECOMMENDの要素は除外
            if "INFORMATION" in tag.get_text() and "RECOMMEND" not in tag.get_text():
                # 内部に pid= リンクがあればその領域を採用
                if tag.find("a", href=lambda h: h and "pid=" in h):
                    info_area = tag
                    break

    if not info_area:
        print("警告: INFORMATION エリアを特定できませんでした。")
        return []

    # エリア内の a タグ（商品リンク）を上から順番に取得
    for a_tag in info_area.find_all("a", href=True):
        href = a_tag["href"]
        text = a_tag.get_text(strip=True)
        
        # 商品リンクかつ、テキストが存在し「おすすめ」等の見出しでないもの
        if "pid=" in href and text and len(text) > 2:
            full_url = requests.compat.urljoin(TARGET_URL, href)
            items.append((text, full_url))

    return items

def main():
    init_db()
    print("城峰釣具店 (INFORMATIONピンポイント巡回) を開始します...")
    
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

    raw_items = get_information_items_strictly(soup)

    if not raw_items:
        print("INFORMATION枠内に新着商品が見つかりませんでした。")
        return

    # 上からの掲載順を保ちつつ重複を除外
    unique_items = []
    seen_urls = set()
    for title, url in raw_items:
        if url not in seen_urls:
            unique_items.append((title, url))
            seen_urls.add(url)

    # 未通知の商品（最新の上から順）
    new_items = []
    for title, url in unique_items:
        if not is_notified(url):
            new_items.append((title, url))

    if not new_items:
        print("INFORMATION内に未通知の商品はありませんでした。")
        return

    # 上部10件
    target_items = new_items[:10]
    processed_items = []

    for title, url in target_items:
        print(f"INFORMATION対象商品を取得中: {title}")
        img_url = fetch_product_image_url(url, headers)
        processed_items.append((title, url, img_url))
        save_notified(url)

    send_flex_message(processed_items)

if __name__ == "__main__":
    main()
