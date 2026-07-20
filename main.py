import os
import time
import json
import hashlib
import requests
import random
import logging
from bs4 import BeautifulSoup
from decimal import Decimal, getcontext
from flask import Flask, jsonify, request
from threading import Thread
from datetime import datetime, timedelta
import pytz

# =====================
# FLASK APP
# =====================
app = Flask(__name__)

# =====================
# CONFIG (SECURE)
# =====================
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8165343576:AAHjfPZpUUUDvWk3WbC1XocQ_MGQ1aESLT0")
CHANNEL = os.getenv("TELEGRAM_CHANNEL", "@AndriaGold")
URL = os.getenv("GOLD_URL", "https://edahabapp.com/")
API_KEY = os.getenv("API_KEY")

getcontext().prec = 28

# =====================
# SMART ALERTS CONFIG
# =====================
ALERT_PCT_THRESHOLD = float(os.getenv("ALERT_PCT_THRESHOLD", "0.5"))
ALERT_VALUE_THRESHOLD = float(os.getenv("ALERT_VALUE_THRESHOLD", "5.0"))
ALERT_COOLDOWN_MINUTES = int(os.getenv("ALERT_COOLDOWN", "5"))

# =====================
# LOGGING
# =====================
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

# =====================
# TIMEZONE
# =====================
egypt_tz = pytz.timezone("Africa/Cairo")

# =====================
# STATE
# =====================
last_hash = None
last_data = None
last_gold_hash = None  # Track only gold price changes
sent_close_msg = False
sent_open_msg = False
fail_count = 0
MAX_FAILS = 5

# =====================
# DAILY STATS
# =====================
daily_high = {}
daily_low = {}
daily_sums = {}
yesterday_close = {}

# =====================
# SMART ALERTS STATE
# =====================
last_alert_time = {}

# =====================
# CLEAN DECIMAL
# =====================
def D(x):
    return Decimal(x.replace(",", "").strip())

# =====================
# STATS HELPERS
# =====================
def reset_daily_stats():
    global daily_high, daily_low, daily_sums
    daily_high = {}
    daily_low = {}
    daily_sums = {}
    log.info("Daily stats reset")

def update_stats(data):
    global daily_high, daily_low, daily_sums
    now_str = datetime.now(egypt_tz).strftime("%I:%M %p")

    for k, v in data.items():
        if not isinstance(v, dict):
            continue
        sell = D(v["sell"])
        buy = D(v["buy"])

        if k not in daily_high or sell > daily_high[k]["sell"]:
            daily_high[k] = {"sell": sell, "buy": buy, "time": now_str}

        if k not in daily_low or sell < daily_low[k]["sell"]:
            daily_low[k] = {"sell": sell, "buy": buy, "time": now_str}

        if k not in daily_sums:
            daily_sums[k] = {"sell_sum": Decimal("0"), "buy_sum": Decimal("0"), "count": 0}
        daily_sums[k]["sell_sum"] += sell
        daily_sums[k]["buy_sum"] += buy
        daily_sums[k]["count"] += 1

def get_avg(k):
    if k not in daily_sums or daily_sums[k]["count"] == 0:
        return None, None
    s = daily_sums[k]
    avg_sell = s["sell_sum"] / s["count"]
    avg_buy = s["buy_sum"] / s["count"]
    return avg_sell, avg_buy

def pct_change(current, previous):
    if previous is None or previous == 0:
        return None
    return ((current - previous) / previous) * 100

# =====================
# SMART ALERTS
# =====================
def can_send_alert(karat):
    global last_alert_time
    now = datetime.now(egypt_tz)
    if karat not in last_alert_time:
        return True
    cooldown = timedelta(minutes=ALERT_COOLDOWN_MINUTES)
    return now - last_alert_time[karat] >= cooldown

def record_alert(karat):
    global last_alert_time
    last_alert_time[karat] = datetime.now(egypt_tz)

def is_gold_karat(k):
    """Check if key is a gold karat (not currency/ounce)"""
    return "عيار" in k

def get_gold_hash(data):
    """Get hash of only gold karat prices"""
    gold_data = {k: v for k, v in data.items() if isinstance(v, dict) and is_gold_karat(k)}
    if not gold_data:
        return None
    return hashlib.md5(str(dict(sorted(gold_data.items()))).encode()).hexdigest()

def check_alerts(data, previous_data):
    alerts = []
    if not previous_data:
        return alerts

    # Only check gold karats, ignore currency/ounce
    for k, v in data.items():
        if not isinstance(v, dict):
            continue
        if not is_gold_karat(k):
            continue
        if k not in previous_data or not isinstance(previous_data[k], dict):
            continue

        current_sell = D(v["sell"])
        previous_sell = D(previous_data[k]["sell"])

        change = pct_change(current_sell, previous_sell)
        if change is not None and abs(change) >= ALERT_PCT_THRESHOLD:
            if can_send_alert(k):
                direction = "⬆️ ارتفاع" if change > 0 else "⬇️ انخفاض"
                emoji = "🚀" if change > 0 else "📉"
                msg = emoji + " <b>تنبيه!</b>\n\n🔸 <b>" + k + "</b>\n" + direction + ": <b>" + str(round(change, 2)) + "%</b>\n💰 السعر: " + str(current_sell) + "\n📊 السابق: " + str(previous_sell) + "\n⏰ " + datetime.now(egypt_tz).strftime("%I:%M %p")
                alerts.append(msg)
                record_alert(k)
                continue

        diff = float(current_sell - previous_sell)
        if abs(diff) >= ALERT_VALUE_THRESHOLD:
            if can_send_alert(k):
                direction = "⬆️ ارتفاع" if diff > 0 else "⬇️ انخفاض"
                emoji = "💹" if diff > 0 else "🔻"
                msg = emoji + " <b>تنبيه!</b>\n\n🔸 <b>" + k + "</b>\n" + direction + ": <b>" + str(round(diff, 2)) + " جنيه</b>\n💰 السعر: " + str(current_sell) + "\n📊 السابق: " + str(previous_sell) + "\n⏰ " + datetime.now(egypt_tz).strftime("%I:%M %p")
                alerts.append(msg)
                record_alert(k)

    return alerts

# =====================
# SNAPSHOT
# =====================
def get_snapshot(retries=3):
    global fail_count

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }

    for attempt in range(retries):
        try:
            time.sleep(2 + random.randint(0, 3))

            html = requests.get(URL, headers=headers, timeout=10).text
            soup = BeautifulSoup(html, "html.parser")
            items = soup.find_all("div", class_="price-item")

            data = {}
            gram_24 = None
            ounce = None

            for item in items:
                title = item.find("span", class_="font-medium")
                nums = item.find_all("span", class_="number-font")

                if not title or len(nums) == 0:
                    continue

                name = title.text.strip()

                if "عيار" in name and len(nums) >= 2:
                    sell = D(nums[0].text)
                    buy = D(nums[1].text)
                    data[name] = {"buy": str(buy), "sell": str(sell)}

                    if "24" in name:
                        gram_24 = sell

                if "أوقية" in name or "ounce" in name.lower():
                    ounce = D(nums[0].text)
                    data["الأوقية العالمية"] = str(ounce)

                if "USD" in name or "الدولار" in name:
                    data["الدولار الأمريكي"] = str(D(nums[0].text))

            if gram_24 and ounce:
                gold_dollar = (gram_24 * Decimal("31.1034768")) / ounce
                data["دولار الصاغة"] = f"{gold_dollar:.2f}"

            if not data:
                log.warning("No data extracted from page")
                time.sleep(3)
                continue

            sorted_data = dict(sorted(data.items()))
            page_hash = hashlib.md5(str(sorted_data).encode()).hexdigest()

            fail_count = 0
            return data, page_hash

        except Exception as e:
            fail_count += 1
            log.error(f"Attempt {attempt + 1}/{retries} failed: {e}")
            time.sleep(3)

    log.error("All snapshot attempts failed")
    return {}, None

# =====================
# TELEGRAM SEND
# =====================
def send(msg, retries=3):
    if not TOKEN:
        log.error("TELEGRAM_BOT_TOKEN not set")
        return False

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    keyboard = {
        "inline_keyboard": [
            [
                {"text": "🌐 الموقع", "url": "https://andriagold.netlify.app/"},
                {"text": "📢 القناة", "url": "https://t.me/AndreaGold"}
            ]
        ]
    }

    for attempt in range(retries):
        try:
            resp = requests.post(url, data={
                "chat_id": CHANNEL,
                "text": msg,
                "parse_mode": "HTML",
                "reply_markup": json.dumps(keyboard)
            }, timeout=10)

            if resp.status_code == 200:
                log.info("Message sent successfully")
                return True
            else:
                log.warning(f"Telegram API returned {resp.status_code}: {resp.text}")
                time.sleep(2)

        except Exception as e:
            log.error(f"Send attempt {attempt + 1}/{retries} failed: {e}")
            time.sleep(2)

    log.error("Failed to send message after all retries")
    return False

# =====================
# FORMAT
# =====================
def format_msg(data):
    msg = "💎 <b>تحديث لحظي للذهب</b>\n\n━━━━━━━━━━━━━━\n"

    for k, v in data.items():
        if isinstance(v, dict):
            msg += f"🔸 <b>{k}</b>\n🟢 بيع: {v['sell']} | 🔴 شراء: {v['buy']}\n──────────────\n"
        else:
            msg += f"📌 {k}: <b>{v}</b>\n"

    return msg + "━━━━━━━━━━━━━━\n"

# =====================
# FORMAT CLOSE (WITH STATS)
# =====================
def format_close_msg(data):
    msg = "🌙 <b>إغلاق سوق الذهب اليوم</b>\n\n"

    if data:
        msg += "📊 <b>آخر سعر قبل الإغلاق:</b>\n━━━━━━━━━━━━━━\n"

        for k, v in data.items():
            if isinstance(v, dict):
                msg += f"🔸 <b>{k}</b>\n🟢 بيع: {v['sell']} | 🔴 شراء: {v['buy']}\n"

                if k in daily_high and k in daily_low:
                    msg += f"📈 أعلى: {daily_high[k]['sell']} ({daily_high[k]['time']})\n"
                    msg += f"📉 أقل: {daily_low[k]['sell']} ({daily_low[k]['time']})\n"

                avg_sell, avg_buy = get_avg(k)
                if avg_sell is not None:
                    msg += f"📊 متوسط: {avg_sell:.2f}\n"

                if k in yesterday_close:
                    y_sell = D(yesterday_close[k]["sell"])
                    c_sell = D(v["sell"])
                    change = pct_change(c_sell, y_sell)
                    if change is not None:
                        arrow = "⬆️" if change > 0 else "⬇️" if change < 0 else "➖"
                        msg += f"{arrow} مقارنة بأمس: {change:+.2f}%\n"

                msg += "──────────────\n"
            else:
                msg += f"📌 {k}: <b>{v}</b>\n"

        msg += "━━━━━━━━━━━━━━\n"

    msg += "\n❤️ شكراً لمتابعتكم\n💎 نلقاكم 10 صباحاً"
    return msg

# =====================
# LOOP
# =====================
def loop():
    global last_hash, last_data, sent_close_msg, sent_open_msg, yesterday_close

    while True:
        try:
            now = datetime.now(egypt_tz)
            hour = now.hour
            log.info(f"Current hour: {hour}")

            if 10 <= hour < 24:
                sent_close_msg = False

                if not sent_open_msg:
                    log.info("Market opened → forcing first send")
                    data, page_hash = get_snapshot()

                    if data:
                        reset_daily_stats()
                        update_stats(data)
                        send(format_msg(data))
                        last_hash = page_hash
                        last_data = data
                        sent_open_msg = True
                    else:
                        log.warning("No data yet after open")
                        time.sleep(30)
                        continue

                data, page_hash = get_snapshot()

                if not data:
                    time.sleep(10)
                    continue

                update_stats(data)

                if last_hash is None:
                    send(format_msg(data))
                    last_hash = page_hash
                    last_data = data
                    time.sleep(10)
                    continue

                # Check if gold prices changed (not currency/ounce)
                current_gold_hash = get_gold_hash(data)

                if current_gold_hash and current_gold_hash != last_gold_hash:
                    # ====== SMART ALERTS ======
                    alerts = check_alerts(data, last_data)
                    for alert_msg in alerts:
                        send(alert_msg)
                        time.sleep(1)
                    # ==========================
                    send(format_msg(data))
                    last_gold_hash = current_gold_hash

                # Always update full data and hash
                last_hash = page_hash
                last_data = data

                time.sleep(10)

            else:
                if not sent_close_msg:
                    if last_data:
                        yesterday_close = {}
                        for k, v in last_data.items():
                            if isinstance(v, dict):
                                yesterday_close[k] = {"sell": v["sell"], "buy": v["buy"]}

                    send(format_close_msg(last_data))
                    sent_close_msg = True

                sent_open_msg = False
                time.sleep(60)

        except Exception as e:
            log.error(f"Loop error: {e}")
            time.sleep(5)

# =====================
# API (SECURE)
# =====================
@app.route("/api")
def api():
    key = request.args.get("key")

    if key != API_KEY:
        return jsonify({"error": "unauthorized"}), 403

    data, _ = get_snapshot()
    return jsonify(data)

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "last_data": bool(last_data),
        "fail_count": fail_count,
        "sent_open": sent_open_msg,
        "sent_close": sent_close_msg,
        "daily_high": {k: {"sell": str(v["sell"]), "time": v["time"]} for k, v in daily_high.items()},
        "daily_low": {k: {"sell": str(v["sell"]), "time": v["time"]} for k, v in daily_low.items()}
    })

@app.route("/")
def home():
    return "💎 Live Gold System Running Secure"

# =====================
# START
# =====================
if __name__ == "__main__":
    Thread(target=loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
