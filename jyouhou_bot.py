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

def get_information_items_strictly(headers):
    """城峰釣具店INFORMATION（スクロール枠）内部のリンクのみを確実に上から順に取得"""
    res = requests.get(TARGET_URL, headers=headers, timeout=10)
    res.encoding = res.apparent_encoding
    soup = BeautifulSoup(res.text, "html.parser")

    target_soup = None

    # 1. marquee タグ（スクロール枠）を最優先で取得
    marquee = soup.find("marquee")
    if marquee:
        target_soup = marquee

    # 2. iframe で埋め込まれている場合
    if not target_soup:
        for iframe in soup.find_all("iframe"):
            src = iframe.get("src", "")
            if "info" in src or "top" in src:
                iframe_url = requests.compat.urljoin(TARGET_URL, src)
                iframe_res = requests.get(iframe_url, headers=headers, timeout=10)
                iframe_res.encoding = iframe_res.apparent_encoding
                target_soup = BeautifulSoup(iframe_res.text, "html.parser")
                break

    # 3. marqueeがない場合、"INFORMATION" 領域内の ul / td を厳密限定
    if not target_soup:
        for element in soup.find_all(["marquee", "div", "td"]):
            # RECOMMENDやRECOMMENDATIONなどの単語が含まれている親ブロックは絶対除外
            element_text = element.get_text()
            if "INFORMATION" in element_text and "RECOMMEND" not in element_text:
                if element.find("a", href=lambda h: h and "pid=" in h):
                    target_soup = element
                    break

    if not target_soup:
        print("INFORMATION エリアの取得に失敗しました。")
        return []

    items = []
    # 掲載順（HTMLの登場順＝上から順）通りに商品リンクのみを取得
    for a_tag in target_soup.find_all("a", href=True):
        href = a_tag["href"]
        text = a_tag.get_text(strip=True)
        
        # pid= を含み、カテゴリや無効な文字列でないものを抽出
        if "pid=" in href and text and not text.startswith("http") and len(text) > 2:
            full_url = requests.compat.urljoin(TARGET_URL, href)
            items.append((text, full_url))

    return items

def main():
    init_db()
    print("城峰釣具店 (INFORMATION構造厳密判定) を開始します...")
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        raw_items = get_information_items_strictly(headers)
    except Exception as e:
        print(f"Webサイトの取得に失敗しました: {e}")
        return

    if not raw_items:
        print("INFORMATION枠内に商品が見つかりませんでした。")
        return

    # 上からの並び順を保ったまま重複を除外
    unique_items = []
    seen_urls = set()
    for title, url in raw_items:
        if url not in seen_urls:
            unique_items.append((title, url))
            seen_urls.add(url)

    # 未通知の商品のみを抽出
    new_items = []
    for title, url in unique_items:
        if not is_notified(url):
            new_items.append((title, url))

    if not new_items:
        print("INFORMATION内に未通知の商品はありませんでした。")
        return

    # 最上部の最新10件を対象
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
