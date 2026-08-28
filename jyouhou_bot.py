import os
import sqlite3
import hashlib
import requests
import re
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta

TARGET_URL = "https://fishing-shop-jh.com/"
DEFAULT_LOGO_URL = "https://fishing-shop-jh.com/img/logo.png"
DB_PATH = "products.db"
LINE_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

# --- 安全装置の設定 ---
MAX_NOTIFY_LIMIT = 20  # 大量検知時の事故防止ガード（20件に引き上げ）
MAX_TRACK_LIMIT = 50   # DBに記憶しておく件数

# --- 日本時間(JST)の設定 ---
JST = timezone(timedelta(hours=+9), 'JST')

def log(msg):
    """JSTのタイムスタンプ付きでログを出力する"""
    now = datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{now}] {msg}")

def generate_item_key(title, url):
    """タイトルとURLの組み合わせから一意の識別キーを生成"""
    raw_str = f"{title.strip()}_{url.strip()}"
    return hashlib.md5(raw_str.encode('utf-8')).hexdigest()

def init_db():
    """DBの初期化（上積み検知用のスナップショットテーブル）"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS snapshot (
            rank INTEGER PRIMARY KEY,
            item_key TEXT,
            title TEXT,
            url TEXT
        )
    ''')
    conn.commit()
    conn.close()

def get_previous_snapshot():
    """前回の監視リスト（キーの配列）を順位順に取得"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT item_key FROM snapshot ORDER BY rank ASC')
    keys = [row[0] for row in cursor.fetchall()]
    conn.close()
    return keys

def save_snapshot(items):
    """今回の監視リストをDBに上書き保存"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM snapshot')
    for rank, (title, url, item_key) in enumerate(items):
        cursor.execute('INSERT INTO snapshot (rank, item_key, title, url) VALUES (?, ?, ?, ?)', 
                       (rank, item_key, title, url))
    conn.commit()
    conn.close()

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
        log(f"[ERROR] 詳細ページ画像取得エラー ({detail_url}): {e}")
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
        log(f"[ERROR] 画像検索巡回エラー ({target_url}): {e}")

    return DEFAULT_LOGO_URL

def send_flex_message(items):
    """LINE Flex Message 送信"""
    if not LINE_ACCESS_TOKEN:
        log("[ERROR] LINE_CHANNEL_ACCESS_TOKEN が設定されていません。")
        return

    # 1通のメッセージに含めるアイテム数を5件に変更
    chunk_size = 5 
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
            log(f"[INFO] LINE画像付きFlex通知送信成功 ({len(chunk)}件)")
        else:
            log(f"[ERROR] LINE通知送信失敗: {response.status_code} {response.text}")

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
    log("[INFO] サマリー通知を送信しました。")

def get_top_information_items(soup):
    """div.info エリアから全テキストをシンプルかつ確実に取得（上限MAX_TRACK_LIMIT件）"""
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
    init_db()
    log("城峰釣具店 (INFORMATION監視) を開始します...")

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

    # 現在のサイトの最新リストを取得
    all_items = get_top_information_items(soup)
    log(f"[DEBUG] 監視対象とする上位最新データ件数: {len(all_items)}件")

    if not all_items:
        log("[INFO] INFORMATION枠内に商品が見つかりませんでした。")
        return

    # DBから前回のリストを取得
    prev_keys = get_previous_snapshot()

    if not prev_keys:
        log(f"[INFO] 初回データベース初期化: 最新{len(all_items)}件を記憶します。")
        save_snapshot(all_items)
        log("[INFO] 初期化完了。次回から追加・更新分のみ通知します。")
        return

    new_items = []
    existing_items_in_curr = []
    
    # 1. 完全な新規追加の検知と、既存アイテムの相対位置チェック準備
    for i, (title, url, item_key) in enumerate(all_items):
        if item_key not in prev_keys:
            # 前回どこにも存在しなかった完全な新規
            new_items.append((title, url, item_key, i))
            log(f"[DEBUG] 新規追加検知: {title}")
        else:
            prev_rank = prev_keys.index(item_key)
            existing_items_in_curr.append((i, prev_rank, title, url, item_key))

    # 2. 上積み（再浮上）検知：相対的な順位の「追い抜き」を判定
    for idx, (curr_rank, prev_rank, title, url, item_key) in enumerate(existing_items_in_curr):
        # 自分より現在の順位が「下」にあるアイテムたちの、過去の順位を取得
        rest_prev_ranks = [item[1] for item in existing_items_in_curr[idx+1:]]
        if rest_prev_ranks:
            min_prev_in_rest = min(rest_prev_ranks)
            # 過去に自分より上位だったアイテムをごぼう抜きして上にいる = 明示的に上に積まれた
            if prev_rank > min_prev_in_rest:
                new_items.append((title, url, item_key, curr_rank))
                log(f"[DEBUG] 再浮上（上積み）検知: {title} (前回{prev_rank}位 -> 今回{curr_rank}位)")

    # サイトの表示順（上から順）に通知を整理
    new_items = sorted(new_items, key=lambda x: x[3])

    log(f"[DEBUG] 検知された新規・再入荷件数: {len(new_items)}件")

    if new_items:
        if len(new_items) > MAX_NOTIFY_LIMIT:
            log(f"[WARN] 検知が{len(new_items)}件と多いため、LINE通知枠保護により個別送信をスキップし、サマリー通知を送信します。")
            send_summary_message(new_items)
        else:
            processed_items = []
            for title, url, item_key, rank in new_items:
                log(f"[INFO] 新着商品処理中: {title}")
                try:
                    img_url = fetch_product_image_url(url, headers)
                    processed_items.append((title, url, img_url, item_key))
                except Exception as e:
                    log(f"[ERROR] エラー発生 ({title}): {e}")
                    processed_items.append((title, url, DEFAULT_LOGO_URL, item_key))

            if processed_items:
                send_flex_message(processed_items)
    else:
        log("[INFO] INFORMATION内に新しい未通知商品はありませんでした。")

    # 処理がすべて終わったら、現在の状態を次回の比較用（スナップショット）としてDBに上書き保存
    save_snapshot(all_items)
    log("--- 処理が正常に完了しました ---")

if __name__ == "__main__":
    main()
