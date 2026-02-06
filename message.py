from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from bs4 import BeautifulSoup
import sqlite3
import time
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
import time
import os
from selenium import webdriver
import threading
import re
import json
from datetime import datetime
RESUME_FILE = "last_user_id.txt"  # ← 再開用ファイル
current_date = None  # ← 追加：日付ヘッダの状態保持
def _find_chat_scroll_container(driver):
    """
    チャットのスクロール対象となる要素を探す。
    見つからなければ None（その場合は window スクロールで代替）。
    """
    selectors = [
        "#messages-container-v2",          # 既存のメッセージコンテナ
        ".chat-area", ".chat-body", ".message-body",
        "div[data-role='message-container']",
    ]
    for sel in selectors:
        try:
            elem = driver.find_element(By.CSS_SELECTOR, sel)
            return elem
        except Exception:
            continue
    return None  # フォールバックは window でスクロール

def _wait_messages_drawn(driver, timeout=15):
    """
    メッセージ群が最低限描画されるのを待つ。
    """
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#messages-container-v2 > div"))
        )
    except TimeoutException:
        pass
    time.sleep(0.5)

def scroll_chat_to_top(driver, max_loops=60, stable_rounds=3, sleep_per_loop=0.5):
    """
    チャット欄を“最上位まで”スクロールして、Lazy Loadで過去メッセージをすべて出す。
    ・ループごとに scrollTop=0 を実行（window フォールバックあり）
    ・メッセージ要素数が一定回数連続で増えなくなったら終了
    """
    container = _find_chat_scroll_container(driver)
    get_count_js = "return document.querySelectorAll('#messages-container-v2 > div').length;"

    def _get_count():
        try:
            return driver.execute_script(get_count_js)
        except Exception:
            # CSSが一致しなければ find_elements にフォールバック
            try:
                return len(driver.find_elements(By.CSS_SELECTOR, "#messages-container-v2 > div"))
            except Exception:
                return -1

    # 初期描画待ち
    _wait_messages_drawn(driver)

    same_count_streak = 0
    last_count = _get_count()

    for _ in range(max_loops):
        try:
            if container:
                driver.execute_script("arguments[0].scrollTop = 0;", container)
            else:
                # コンテナが取れない場合は window スクロールで代替
                driver.execute_script("window.scrollTo(0, 0);")
            # スクロールイベントを明示的に発火（必要なUI向け）
            driver.execute_script("window.dispatchEvent(new Event('scroll'));")
        except StaleElementReferenceException:
            # 要素が差し替わった場合は取り直し
            container = _find_chat_scroll_container(driver)

        time.sleep(sleep_per_loop)

        count = _get_count()
        if count <= 0:
            # まだDOMが安定してない可能性。少し待って続行
            time.sleep(0.3)
            continue

        if count == last_count:
            same_count_streak += 1
        else:
            same_count_streak = 0
            last_count = count

        # 連続で増加が止まったら取得完了とみなす
        if same_count_streak >= stable_rounds:
            break

    # 最後に短く待ってDOM安定
    time.sleep(0.3)

# メッセージテーブル作成
def initialize_message_table():
    conn = sqlite3.connect("lstep_users.db")
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            sender_name TEXT,     -- ★ 新規追加
            sender TEXT,
            message TEXT,
            time_sent TEXT
        )
    ''')
    conn.commit()
    conn.close()

# メッセージ保存
def save_message(user_id, sender, sender_name, message, time_sent):
    conn = sqlite3.connect("lstep_users.db")
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO messages (user_id, sender, sender_name, message, time_sent)
        VALUES (?, ?, ?, ?, ?)
    ''', (user_id, sender, sender_name, message, time_sent))
    conn.commit()
    conn.close()

# 追加: ブロックから送信者名を推定するヘルパ
def _extract_sender_name_from_block(block):
    """
    各メッセージブロック内から送信者名を取る。
    まずは tooltip-container（staff_name_show）配下の「送信者：」行を見る。
    無ければ汎用セレクタや画像alt/titleにフォールバック。
    """
    # ★ スクショの構造に合わせた最優先セレクタ
    cand = block.select_one(
        ".tooltip-container.staff_name_show span.underline.cursor-pointer"
    )
    if cand:
        txt = cand.get_text(strip=True)
        if txt:
            return txt

    # 「送信者：」テキストを含む行から隣の <span> を拾う保険
    label_div = None
    for div in block.select(".tooltip-container.staff_name_show div"):
        if "送信者" in div.get_text():
            label_div = div
            break
    if label_div:
        span = label_div.select_one("span.underline.cursor-pointer")
        if span:
            txt = span.get_text(strip=True)
            if txt:
                return txt

    # 既存の汎用候補（UI差異に備えたバックアップ）
    cand_selectors = [
        ".sender-name", ".name", ".user-name", ".member-name",
        "[data-role='sender-name']", "[data-testid='sender-name']",
        ".header .name", ".bubble .name",
    ]
    for sel in cand_selectors:
        elem = block.select_one(sel)
        if elem:
            txt = elem.get_text(strip=True)
            if txt:
                return txt

    # アイコンの代替テキストに名前がある場合
    img = block.select_one("img[alt]") or block.select_one("img[title]")
    if img:
        txt = (img.get("alt") or img.get("title") or "").strip()
        if txt:
            return txt
    return None
def restart_driver_with_ui(driver, logger):
    logger.message.emit("🔁 ドライバーを再起動します…")

    try:
        new_driver = webdriver.Chrome()
        new_driver.get("https://step.lme.jp/")

        # ▼▼▼ ログインフォーム自動入力（最初の処理と同じ） ▼▼▼
        try:
            logger.message.emit("🟡 再ログイン画面でID・パスワードを自動入力しています…")

            wait = WebDriverWait(new_driver, 20)

            login_id = wait.until(
                EC.presence_of_element_located((By.ID, "email_login"))
            )
            login_pw = wait.until(
                EC.presence_of_element_located((By.ID, "password_login"))
            )

            login_id.clear()
            login_id.send_keys("miomama0605@gmail.com")

            login_pw.clear()
            login_pw.send_keys("20250606@Mio")

            logger.message.emit("🟡 自動入力完了。ログイン操作は手動で行ってください。")

        except Exception as e:
            logger.message.emit(f"⚠️ 再ログイン時の自動入力に失敗しました: {e}")

        # ▲▲▲ 自動入力ここまで ▲▲▲


        # --- UIゲート：ログイン完了をユーザーに確認させる ---
        proceed_event = threading.Event()
        cancel_event = threading.Event()

        instructions = (
            "1) 新しく開いたブラウザでログインを完了させてください。\n"
            "2) ログイン後、このポップアップの［続行］を押してください。\n"
            "※［キャンセル］を押すと処理を中断します。"
        )

        logger.open_gate.emit("再ログインが必要です", instructions, proceed_event, cancel_event)

        # どちらかの操作を待つ
        while True:
            if proceed_event.wait(timeout=0.1):
                break
            if cancel_event.is_set():
                logger.message.emit("🛑 ユーザーによりキャンセルされました。")
                return None

        logger.message.emit("🔄 再ログイン完了。処理を再開します。")
        return new_driver

    except Exception as e:
        logger.message.emit(f"❌ driver 再起動失敗: {e}")
        return None

# =========================
# ✅ time_sent を必ず YYYY-MM-DD HH:MM:SS に正規化
# =========================
def update_user_friend_value(user_id: int, friend_value_json: str):
    conn = sqlite3.connect("lstep_users.db")
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET friend_value = ? WHERE id = ?",
        (friend_value_json, user_id),
    )
    conn.commit()
    conn.close()


def _extract_friend_value_json(soup: BeautifulSoup) -> str:
    try:
        values = {}
        friend_info = soup.select_one("#friend-info")
        if not friend_info:
            return "{}"

        blocks = friend_info.select(r"div.mt-\[20px\], div.border-b")
        for block in blocks:
            label_elem = block.select_one("p")
            if not label_elem:
                continue

            label = label_elem.get_text(" ", strip=True)
            if not label:
                continue

            value = ""
            value_elem = block.select_one("span, input, textarea")
            if value_elem:
                if value_elem.name in {"input", "textarea"}:
                    value = (value_elem.get("value") or "").strip()
                else:
                    value = value_elem.get_text(" ", strip=True)
            else:
                value_container = label_elem.find_next_sibling("div")
                if value_container:
                    value = value_container.get_text(" ", strip=True)

            values[label] = value

        return json.dumps(values, ensure_ascii=False) if values else "{}"
    except Exception:
        return "{}"
def _wait_friend_info_ready(driver, timeout=10) -> bool:
    """
    友だち情報パネルの描画完了を待つ。
    #friend-info が存在し、内部のラベル(p)が最低1つ出るまで待機する。
    """
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#friend-info"))
        )
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#friend-info p"))
        )
        return True
    except TimeoutException:
        return False

def normalize_time_sent(current_date: str, time_sent_raw: str):
    """
    current_date: 'YYYY-MM-DD' or None
    time_sent_raw: 例 '01/21 15:43' / '15:43' / '2025-01-21 01/21 15:43'
    """

    if not time_sent_raw:
        return None

    raw = time_sent_raw.strip()

    # ① rawに「YYYY-MM-DD ... HH:MM」が入っているパターン（最優先で救う）
    m_full = re.search(r"(\d{4})-(\d{2})-(\d{2}).*?(\d{1,2}):(\d{2})", raw)
    if m_full:
        y, mo, d, hh, mm = map(int, m_full.groups())
        return f"{y:04d}-{mo:02d}-{d:02d} {hh:02d}:{mm:02d}:00"

    # ② rawが "MM/DD HH:MM" のパターン
    # → 日付側は無視して時刻だけ使う
    m_time = re.search(r"(\d{1,2}):(\d{2})", raw)
    if not m_time:
        return None

    hh = int(m_time.group(1))
    mm = int(m_time.group(2))

    # current_date があるならそれを使う（本命）
    if current_date:
        return f"{current_date} {hh:02d}:{mm:02d}:00"

    # current_date が無いなら詰み（年も日付も確定できない）
    return None

# 各ユーザーのチャット履歴を取得
def scrape_messages(driver, logger, base_url="https://step.lme.jp"):


    # 再開ポイント読込
    resume_from = 0
    if os.path.exists(RESUME_FILE):
        try:
            resume_from = int(open(RESUME_FILE).read().strip())
            print(f"🔁 再開モード: user_id {resume_from} 以降から処理します。")
        except:
            pass

    conn = sqlite3.connect("lstep_users.db")
    cursor = conn.cursor()
    cursor.execute('SELECT id, href FROM users ORDER BY id ASC')
    users = cursor.fetchall()
    conn.close()


    for user_id, href in users:
        if user_id < resume_from:
            continue

        print(f"🟡 ユーザーID {user_id} のチャットを取得中…")

        # ================================
        #   ドライバー停止検知（ここが追加）
        # ================================
        def _safe_get(url):
            nonlocal driver
            try:
                driver.get(url)
                return True
            except Exception as e:
                logger.message.emit(f"⚠️ driver 応答なし → 再起動します: {e}")

                new_driver = restart_driver_with_ui(driver, logger)
                if new_driver:
                    try:
                        driver.quit()
                    except:
                        pass

                    driver = new_driver
                    try:
                        driver.get(url)
                        return True
                    except:
                        logger.message.emit("❌ 再起動後も driver.get に失敗")
                        return False
                else:
                    return False

        # URL get
        ok = _safe_get(base_url + href)
        if not ok:
            print("⚠️ このユーザーをスキップして続行します。")
            continue

        # チャットボタンもセーフ実行
        try:
            chat_button = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "a.btn-sns-line-my-page"))
            )
            chat_button.click()
            time.sleep(3)
        except Exception as e:
            print(f"⚠️ チャットページ遷移失敗: {e}")
            update_user_friend_value(user_id, "{}")
            continue

        if not _wait_friend_info_ready(driver, timeout=12):
            logger.message.emit(
                f"⚠️ friend-info の描画待機がタイムアウト: user_id={user_id}"
            )
            update_user_friend_value(user_id, "{}")
            continue
        # friend_value はチャットページで取得して毎回上書き
        soup_friend = BeautifulSoup(driver.page_source, "html.parser")
        friend_value_json = _extract_friend_value_json(soup_friend)
        update_user_friend_value(user_id, friend_value_json)


        # =========================
        #   以下は既存処理そのまま
        # =========================

        # ページ担当者名
        sender_name_page = None
        try:
            soup = BeautifulSoup(driver.page_source, "html.parser")
            sn_elem = soup.select_one("span.underline.cursor-pointer")
            if sn_elem:
                sender_name_page = sn_elem.text.strip()
        except:
            pass

        # 全メッセージ読み込み
        scroll_chat_to_top(driver)
        soup = BeautifulSoup(driver.page_source, "html.parser")
        message_blocks = soup.select("#messages-container-v2 > div")
        # print(f"🧩 message_blocks count = {len(message_blocks)}")
        # print(soup.select_one("#messages-container-v2"))

        current_date = None  # ✅ ユーザーごとにリセット

        for block in message_blocks:

            # =========================
            # ✅ 日付ヘッダを見つけたら current_date 更新（※continueしない！）
            # =========================
            date_header = block.select_one(".time-center")
            if date_header:
                raw = date_header.get_text(strip=True)  # 例: 2025年04月02日(水)
                m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", raw)
                if m:
                    y = int(m.group(1))
                    mo = int(m.group(2))
                    d = int(m.group(3))
                    current_date = f"{y:04d}-{mo:02d}-{d:02d}"
                # ❌ continueしない（このブロックにメッセージが入っているため）

            # =========================
            # ✅ 送信者判定
            # =========================
            sender = "you" if block.select_one(".you") else "me" if block.select_one(".me") else None
            if not sender:
                continue

            # =========================
            # ✅ メッセージ本文＆時刻取得
            # =========================
            msg_div = block.select_one(".message")
            time_div = block.select_one(".time-send")
            if not (msg_div and time_div):
                continue

            text = msg_div.get_text(separator="\n").strip()

            # time-send は「01/21 15:43」みたいな形式
            time_sent_raw = time_div.get_text(strip=True)

            time_sent = normalize_time_sent(current_date, time_sent_raw)
            if not time_sent:
                print(f"⚠ time_sent parse failed: raw={repr(time_sent_raw)} current_date={current_date}")
                continue

            # =========================
            # ✅ 送信者名取得
            # =========================
            sender_name_msg = _extract_sender_name_from_block(block)

            if sender == "me":
                name_to_save = sender_name_msg or sender_name_page
            else:
                name_to_save = sender_name_msg or None

            # =========================
            # ✅ ログ出力 + DB保存
            # =========================
            print(f"[user_id={user_id}] {sender} {name_to_save} {time_sent} : {text[:50]}")
            save_message(user_id, sender, name_to_save, text, time_sent)

        # 再開ポイント更新
        with open(RESUME_FILE, "w") as f:
            f.write(str(user_id))

    print("🎉 全メッセージ取得が完了しました！")
    if os.path.exists(RESUME_FILE):
        os.remove(RESUME_FILE)
