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
    """商品詳細ページを開き、メイン商品画像のURLを取得する"""
    try:
        res = requests.get(product_url, headers=headers, timeout=10)
        res.encoding = res.apparent_encoding
        soup = BeautifulSoup(res.text, "html.parser")
        
        # OGP画像優先
        og_img = soup.find("meta", property="og:image")
        if og_img and og_img.get("content"):
            return og_img["content"]
        
        # カラーミーショップ標準のメイン商品画像枠
        img_tag = soup.select_one(".product_image img, .img_box img, #product_image img")
        if img_tag and img_tag.get("src"):
            return requests.compat.urljoin(product_url, img_tag["src"])
            
    except Exception as e:
        print(f"画像取得エラー ({product_url}): {e}")
    
    return None

def send_flex_message(items):
    """LINE Messaging API (Flex Message / Carousel) で画像付き通知"""
    if not LINE_ACCESS_TOKEN:
        print("エラー: LINE_CHANNEL_ACCESS_TOKEN が設定されていません。")
        return

    url = "https://api.line.me/v2/bot/message/broadcast"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_ACCESS_TOKEN}"
    }

    # 各商品のカルーセルバブル構築
    bubbles = []
    for title, product_url, img_url in items:
        # デフォルト画像の設定（万が一画像が取れなかった場合）
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
                        "text": title,
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

    # 送信用ペイロード構築
    payload = {
        "messages": [
            {
                "type": "flex",
                "altText": f"城峰釣具店 新着・再入荷通知 ({len(items)}件)",
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

def main():
    init_db()
    print("城峰釣具店 (INFORMATION) の巡回チェックを開始します...")
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        response = requests.get(TARGET_URL, headers=headers, timeout=10)
        response.encoding = response.apparent_encoding
    except Exception as e:
        print(f"Webサイトの取得に失敗しました: {e}")
        return

    soup = BeautifulSoup(response.text, "html.parser")
    
    # INFORMATION エリアの探索（カラーミーショップ標準構成に対応）
    info_area = soup.find(id="top_info") or soup.select_one(".info_box, .information_box, .info_area")
    
    if not info_area:
        # INFORMATIONの見出し文字列を探してその親要素を取得
        for h2 in soup.find_all(["h2", "h3", "div"]):
            if "INFORMATION" in h2.get_text():
                info_area = h2.parent
                break

    if not info_area:
        print("INFORMATIONエリアが見つかりませんでした。トップページ全体から取得します。")
        info_area = soup

    keywords = ["NEW", "新入荷", "再入荷", "予約", "ご予約", "販売", "入荷"]
    new_items = []

    # INFORMATION内の全リンクをチェック
    for a_tag in info_area.find_all("a", href=True):
        href = a_tag["href"]
        text = a_tag.get_text(strip=True)
        
        # キーワードのチェックまたは特定リンク構造の判定
        if any(kw in text for kw in keywords) or "pid=" in href:
            full_url = requests.compat.urljoin(TARGET_URL, href)
            if not is_notified(full_url):
                new_items.append((text, full_url))

    if not new_items:
        print("新着・再入荷商品はありませんでした。")
        return

    # LINE Flex Messageは一度に最大10件まで送信可能
    target_items = new_items[:10]
    processed_items = []

    for title, url in target_items:
        print(f"商品画像を取得中: {title}")
        img_url = fetch_product_image_url(url, headers)
        processed_items.append((title, url, img_url))
        save_notified(url)

    send_flex_message(processed_items)

if __name__ == "__main__":
    main()
