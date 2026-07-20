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

app = Flask(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8165343576:AAHjfPZpUUUDvWk3WbC1XocQ_MGQ1aESLT0")
CHANNEL = os.getenv("TELEGRAM_CHANNEL", "@AndriaGold")
URL = os.getenv("GOLD_URL", "https://edahabapp.com/")
API_KEY = os.getenv("API_KEY")

getcontext().prec = 28

ALERT_CONFIG = {
    "pct_threshold": float(os.getenv("ALERT_PCT_THRESHOLD", "0.5")),
    "value_threshold": float(os.getenv("ALERT_VALUE_THRESHOLD", "5.0")),
    "rapid_pct_threshold": float(os.getenv("ALERT_RAPID_PCT", "1.5")),
    "rapid_time_window": int(os.getenv("ALERT_RAPID_MINUTES", "10")),
    "cooldown_minutes": int(os.getenv("ALERT_COOLDOWN", "5")),
    "priority_karat": ["عيار 24", "عيار 21", "عيار 18"],
    "enable_pct_alert": os.getenv("ENABLE_PCT_ALERT", "true").lower() == "true",
    "enable_value_alert": os.getenv("ENABLE_VALUE_ALERT", "true").lower() == "true",
    "enable_rapid_alert": os.getenv("ENABLE_RAPID_ALERT", "true").lower() == "true",
    "enable_high_low_alert": os.getenv("ENABLE_HIGH_LOW_ALERT", "true").lower() == "true",
}

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

egypt_tz = pytz.timezone("Africa/Cairo")

last_hash = None
last_data = None
sent_close_msg = False
sent_open_msg = False
fail_count = 0
MAX_FAILS = 5

daily_high = {}
daily_low = {}
daily_sums = {}
yesterday_close = {}

alert_history = {}
last_alert_time = {}
price_history = {}

def D(x):
    return Decimal(x.replace(",", "").strip())

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

def add_price_to_history(karat, sell_price):
    global price_history
    now = datetime.now(egypt_tz)
    if karat not in price_history:
        price_history[karat] = []
    price_history[karat].append((now, sell_price))
    cutoff = now - timedelta(minutes=ALERT_CONFIG["rapid_time_window"] * 2)
    price_history[karat] = [(t, p) for t, p in price_history[karat] if t > cutoff]

def can_send_alert(karat, alert_type):
    global last_alert_time
    now = datetime.now(egypt_tz)
    key = f"{karat}_{alert_type}"
    if key not in last_alert_time:
        return True
    cooldown = timedelta(minutes=ALERT_CONFIG["cooldown_minutes"])
    return now - last_alert_time[key] >= cooldown

def record_alert(karat, alert_type, value):
    global alert_history, last_alert_time
    now = datetime.now(egypt_tz)
    key = f"{karat}_{alert_type}"
    if karat not in alert_history:
        alert_history[karat] = []
    alert_history[karat].append({
        "time": now.strftime("%I:%M %p"),
        "type": alert_type,
        "value": float(value)
    })
    last_alert_time[key] = now

def check_pct_alert(karat, current_sell, previous_sell):
    if not ALERT_CONFIG["enable_pct_alert"]:
        return None
    change = pct_change(current_sell, previous_sell)
    if change is None:
        return None
    threshold = ALERT_CONFIG["pct_threshold"]
    if abs(change) >= threshold:
        if not can_send_alert(karat, "pct"):
            return None
        direction = "⬆️ ارتفاع" if change > 0 else "⬇️ انخفاض"
        emoji = "🚀" if change > 0 else "📉"
        msg = emoji + " <b>تنبيه تغير نسبي!</b>\n\n🔸 <b>" + karat + "</b>\n" + direction + ": <b>" + f"{change:+.2f}%" + "</b>\n💰 السعر الحالي: " + str(current_sell) + "\n📊 السعر السابق: " + str(previous_sell) + "\n⏰ " + datetime.now(egypt_tz).strftime("%I:%M %p")
        record_alert(karat, "pct", change)
        return msg
    return None

def check_value_alert(karat, current_sell, previous_sell):
    if not ALERT_CONFIG["enable_value_alert"]:
        return None
    diff = float(current_sell - previous_sell)
    threshold = ALERT_CONFIG["value_threshold"]
    if abs(diff) >= threshold:
        if not can_send_alert(karat, "value"):
            return None
        direction = "⬆️ ارتفاع" if diff > 0 else "⬇️ انخفاض"
        emoji = "💹" if diff > 0 else "🔻"
        msg = emoji + " <b>تنبيه تغير قيمي!</b>\n\n🔸 <b>" + karat + "</b>\n" + direction + ": <b>" + f"{diff:+.2f} جنيه" + "</b>\n💰 السعر الحالي: " + str(current_sell) + "\n📊 السعر السابق: " + str(previous_sell) + "\n⏰ " + datetime.now(egypt_tz).strftime("%I:%M %p")
        record_alert(karat, "value", diff)
        return msg
    return None

def check_rapid_change_alert(karat, current_sell):
    if not ALERT_CONFIG["enable_rapid_alert"]:
        return None
    if karat not in price_history or len(price_history[karat]) < 2:
        return None
    window = timedelta(minutes=ALERT_CONFIG["rapid_time_window"])
    now = datetime.now(egypt_tz)
    old_prices = [(t, p) for t, p in price_history[karat] if now - t <= window]
    if not old_prices:
        return None
    oldest_price = old_prices[0][1]
    change = pct_change(current_sell, oldest_price)
    if change is None:
        return None
    threshold = ALERT_CONFIG["rapid_pct_threshold"]
    if abs(change) >= threshold:
        if not can_send_alert(karat, "rapid"):
            return None
        direction = "🚀 صاروخي" if change > 0 else "🔥 انهيار"
        msg = "⚡ <b>تنبيه تغير سريع!</b>\n\n🔸 <b>" + karat + "</b>\n" + direction + ": <b>" + f"{change:+.2f}%" + "</b> خلال " + str(ALERT_CONFIG["rapid_time_window"]) + " دقيقة\n💰 السعر الحالي: " + str(current_sell) + "\n📊 السعر قبل: " + str(oldest_price) + "\n⏰ " + datetime.now(egypt_tz).strftime("%I:%M %p")
        record_alert(karat, "rapid", change)
        return msg
    return None

def check_high_low_alert(karat, current_sell):
    if not ALERT_CONFIG["enable_high_low_alert"]:
        return None
    if karat not in daily_high or karat not in daily_low:
        return None
    alerts = []
    if current_sell > daily_high[karat]["sell"]:
        if can_send_alert(karat, "new_high"):
            msg = "🏆 <b>أعلى مستوى جديد لليوم!</b>\n\n🔸 <b>" + karat + "</b>\n💰 السعر: <b>" + str(current_sell) + "</b>\n📈 السابق: " + str(daily_high[karat]["sell"]) + " (" + daily_high[karat]["time"] + ")\n⏰ " + datetime.now(egypt_tz).strftime("%I:%M %p")
            record_alert(karat, "new_high", float(current_sell))
            alerts.append(msg)
    if current_sell < daily_low[karat]["sell"]:
        if can_send_alert(karat, "new_low"):
            msg = "🎯 <b>أقل مستوى جديد لليوم!</b>\n\n🔸 <b>" + karat + "</b>\n💰 السعر: <b>" + str(current_sell) + "</b>\n📉 السابق: " + str(daily_low[karat]["sell"]) + " (" + daily_low[karat]["time"] + ")\n⏰ " + datetime.now(egypt_tz).strftime("%I:%M %p")
            record_alert(karat, "new_low", float(current_sell))
            alerts.append(msg)
    return alerts if alerts else None

def run_smart_alerts(data, previous_data):
    all_alerts = []
    for k, v in data.items():
        if not isinstance(v, dict):
            continue
        is_priority = any(pk in k for pk in ALERT_CONFIG["priority_karat"])
        if not is_priority:
            continue
        current_sell = D(v["sell"])
        add_price_to_history(k, current_sell)
        if previous_data and k in previous_data and isinstance(previous_data[k], dict):
            previous_sell = D(previous_data[k]["sell"])
            alert = check_pct_alert(k, current_sell, previous_sell)
            if alert:
                all_alerts.append(alert)
            alert = check_value_alert(k, current_sell, previous_sell)
            if alert:
                all_alerts.append(alert)
        alert = check_rapid_change_alert(k, current_sell)
        if alert:
            all_alerts.append(alert)
        alerts = check_high_low_alert(k, current_sell)
        if alerts:
            all_alerts.extend(alerts)
    return all_alerts

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

def format_msg(data):
    msg = "💎 <b>تحديث لحظي للذهب</b>\n\n━━━━━━━━━━━━━━\n"
    for k, v in data.items():
        if isinstance(v, dict):
            msg += "🔸 <b>" + k + "</b>\n🟢 بيع: " + v["sell"] + " | 🔴 شراء: " + v["buy"] + "\n──────────────\n"
        else:
            msg += "📌 " + k + ": <b>" + v + "</b>\n"
    return msg + "━━━━━━━━━━━━━━\n"

def format_close_msg(data):
    msg = "🌙 <b>إغلاق سوق الذهب اليوم</b>\n\n"
    if data:
        msg += "📊 <b>آخر سعر قبل الإغلاق:</b>\n━━━━━━━━━━━━━━\n"
        for k, v in data.items():
            if isinstance(v, dict):
                msg += "🔸 <b>" + k + "</b>\n🟢 بيع: " + v["sell"] + " | 🔴 شراء: " + v["buy"] + "\n"
                if k in daily_high and k in daily_low:
                    msg += "📈 أعلى: " + str(daily_high[k]["sell"]) + " (" + daily_high[k]["time"] + ")\n"
                    msg += "📉 أقل: " + str(daily_low[k]["sell"]) + " (" + daily_low[k]["time"] + ")\n"
                avg_sell, avg_buy = get_avg(k)
                if avg_sell is not None:
                    msg += "📊 متوسط: " + f"{avg_sell:.2f}" + "\n"
                if k in yesterday_close:
                    y_sell = D(yesterday_close[k]["sell"])
                    c_sell = D(v["sell"])
                    change = pct_change(c_sell, y_sell)
                    if change is not None:
                        arrow = "⬆️" if change > 0 else "⬇️" if change < 0 else "➖"
                        msg += arrow + " مقارنة بأمس: " + f"{change:+.2f}%" + "\n"
                msg += "──────────────\n"
            else:
                msg += "📌 " + k + ": <b>" + v + "</b>\n"
        msg += "━━━━━━━━━━━━━━\n"
    msg += "\n❤️ شكراً لمتابعتكم\n💎 نلقاكم 10 صباحاً"
    return msg

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
                if page_hash != last_hash:
                    alerts = run_smart_alerts(data, last_data)
                    for alert_msg in alerts:
                        send(alert_msg)
                        time.sleep(1)
                    send(format_msg(data))
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
        "daily_low": {k: {"sell": str(v["sell"]), "time": v["time"]} for k, v in daily_low.items()},
        "alert_config": {
            "pct_threshold": ALERT_CONFIG["pct_threshold"],
            "value_threshold": ALERT_CONFIG["value_threshold"],
            "rapid_pct_threshold": ALERT_CONFIG["rapid_pct_threshold"],
            "cooldown_minutes": ALERT_CONFIG["cooldown_minutes"],
        },
        "alert_history": {k: v[-5:] if v else [] for k, v in alert_history.items()}
    })

@app.route("/alerts")
def alerts_info():
    return jsonify({
        "config": ALERT_CONFIG,
        "history": alert_history,
        "last_alert_time": {k: v.strftime("%I:%M %p") for k, v in last_alert_time.items()}
    })

@app.route("/")
def home():
    return "💎 Live Gold System Running with Smart Alerts"

if __name__ == "__main__":
    Thread(target=loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))    "rapid_time_window": int(os.getenv("ALERT_RAPID_MINUTES", "10")),
    "cooldown_minutes": int(os.getenv("ALERT_COOLDOWN", "5")),
    "priority_karat": ["عيار 24", "عيار 21", "عيار 18"],
    "enable_pct_alert": os.getenv("ENABLE_PCT_ALERT", "true").lower() == "true",
    "enable_value_alert": os.getenv("ENABLE_VALUE_ALERT", "true").lower() == "true",
    "enable_rapid_alert": os.getenv("ENABLE_RAPID_ALERT", "true").lower() == "true",
    "enable_high_low_alert": os.getenv("ENABLE_HIGH_LOW_ALERT", "true").lower() == "true",
}

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
alert_history = {}
last_alert_time = {}
price_history = {}

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
# SMART ALERTS ENGINE
# =====================
def add_price_to_history(karat, sell_price):
    global price_history
    now = datetime.now(egypt_tz)
    if karat not in price_history:
        price_history[karat] = []
    price_history[karat].append((now, sell_price))
    cutoff = now - timedelta(minutes=ALERT_CONFIG["rapid_time_window"] * 2)
    price_history[karat] = [(t, p) for t, p in price_history[karat] if t > cutoff]

def can_send_alert(karat, alert_type):
    global last_alert_time
    now = datetime.now(egypt_tz)
    key = f"{karat}_{alert_type}"
    if key not in last_alert_time:
        return True
    cooldown = timedelta(minutes=ALERT_CONFIG["cooldown_minutes"])
    return now - last_alert_time[key] >= cooldown

def record_alert(karat, alert_type, value):
    global alert_history, last_alert_time
    now = datetime.now(egypt_tz)
    key = f"{karat}_{alert_type}"
    if karat not in alert_history:
        alert_history[karat] = []
    alert_history[karat].append({
        "time": now.strftime("%I:%M %p"),
        "type": alert_type,
        "value": float(value)
    })
    last_alert_time[key] = now

def check_pct_alert(karat, current_sell, previous_sell):
    if not ALERT_CONFIG["enable_pct_alert"]:
        return None
    change = pct_change(current_sell, previous_sell)
    if change is None:
        return None
    threshold = ALERT_CONFIG["pct_threshold"]
    if abs(change) >= threshold:
        if not can_send_alert(karat, "pct"):
            return None
        direction = "⬆️ ارتفاع" if change > 0 else "⬇️ انخفاض"
        emoji = "🚀" if change > 0 else "📉"
        msg = emoji + " <b>تنبيه تغير نسبي!</b>\n\n🔸 <b>" + karat + "</b>\n" + direction + ": <b>" + f"{change:+.2f}%" + "</b>\n💰 السعر الحالي: " + str(current_sell) + "\n📊 السعر السابق: " + str(previous_sell) + "\n⏰ " + datetime.now(egypt_tz).strftime("%I:%M %p")
        record_alert(karat, "pct", change)
        return msg
    return None

def check_value_alert(karat, current_sell, previous_sell):
    if not ALERT_CONFIG["enable_value_alert"]:
        return None
    diff = float(current_sell - previous_sell)
    threshold = ALERT_CONFIG["value_threshold"]
    if abs(diff) >= threshold:
        if not can_send_alert(karat, "value"):
            return None
        direction = "⬆️ ارتفاع" if diff > 0 else "⬇️ انخفاض"
        emoji = "💹" if diff > 0 else "🔻"
        msg = emoji + " <b>تنبيه تغير قيمي!</b>\n\n🔸 <b>" + karat + "</b>\n" + direction + ": <b>" + f"{diff:+.2f} جنيه" + "</b>\n💰 السعر الحالي: " + str(current_sell) + "\n📊 السعر السابق: " + str(previous_sell) + "\n⏰ " + datetime.now(egypt_tz).strftime("%I:%M %p")
        record_alert(karat, "value", diff)
        return msg
    return None

def check_rapid_change_alert(karat, current_sell):
    if not ALERT_CONFIG["enable_rapid_alert"]:
        return None
    if karat not in price_history or len(price_history[karat]) < 2:
        return None
    window = timedelta(minutes=ALERT_CONFIG["rapid_time_window"])
    now = datetime.now(egypt_tz)
    old_prices = [(t, p) for t, p in price_history[karat] if now - t <= window]
    if not old_prices:
        return None
    oldest_price = old_prices[0][1]
    change = pct_change(current_sell, oldest_price)
    if change is None:
        return None
    threshold = ALERT_CONFIG["rapid_pct_threshold"]
    if abs(change) >= threshold:
        if not can_send_alert(karat, "rapid"):
            return None
        direction = "🚀 صاروخي" if change > 0 else "🔥 انهيار"
        msg = "⚡ <b>تنبيه تغير سريع!</b>\n\n🔸 <b>" + karat + "</b>\n" + direction + ": <b>" + f"{change:+.2f}%" + "</b> خلال " + str(ALERT_CONFIG["rapid_time_window"]) + " دقيقة\n💰 السعر الحالي: " + str(current_sell) + "\n📊 السعر قبل: " + str(oldest_price) + "\n⏰ " + datetime.now(egypt_tz).strftime("%I:%M %p")
        record_alert(karat, "rapid", change)
        return msg
    return None

def check_high_low_alert(karat, current_sell):
    if not ALERT_CONFIG["enable_high_low_alert"]:
        return None
    if karat not in daily_high or karat not in daily_low:
        return None
    alerts = []
    if current_sell > daily_high[karat]["sell"]:
        if can_send_alert(karat, "new_high"):
            msg = "🏆 <b>أعلى مستوى جديد لليوم!</b>\n\n🔸 <b>" + karat + "</b>\n💰 السعر: <b>" + str(current_sell) + "</b>\n📈 السابق: " + str(daily_high[karat]["sell"]) + " (" + daily_high[karat]["time"] + ")\n⏰ " + datetime.now(egypt_tz).strftime("%I:%M %p")
            record_alert(karat, "new_high", float(current_sell))
            alerts.append(msg)
    if current_sell < daily_low[karat]["sell"]:
        if can_send_alert(karat, "new_low"):
            msg = "🎯 <b>أقل مستوى جديد لليوم!</b>\n\n🔸 <b>" + karat + "</b>\n💰 السعر: <b>" + str(current_sell) + "</b>\n📉 السابق: " + str(daily_low[karat]["sell"]) + " (" + daily_low[karat]["time"] + ")\n⏰ " + datetime.now(egypt_tz).strftime("%I:%M %p")
            record_alert(karat, "new_low", float(current_sell))
            alerts.append(msg)
    return alerts if alerts else None

def run_smart_alerts(data, previous_data):
    all_alerts = []
    for k, v in data.items():
        if not isinstance(v, dict):
            continue
        is_priority = any(pk in k for pk in ALERT_CONFIG["priority_karat"])
        if not is_priority:
            continue
        current_sell = D(v["sell"])
        add_price_to_history(k, current_sell)
        if previous_data and k in previous_data and isinstance(previous_data[k], dict):
            previous_sell = D(previous_data[k]["sell"])
            alert = check_pct_alert(k, current_sell, previous_sell)
            if alert:
                all_alerts.append(alert)
            alert = check_value_alert(k, current_sell, previous_sell)
            if alert:
                all_alerts.append(alert)
        alert = check_rapid_change_alert(k, current_sell)
        if alert:
            all_alerts.append(alert)
        alerts = check_high_low_alert(k, current_sell)
        if alerts:
            all_alerts.extend(alerts)
    return all_alerts

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
            msg += "🔸 <b>" + k + "</b>\n🟢 بيع: " + v["sell"] + " | 🔴 شراء: " + v["buy"] + "\n──────────────\n"
        else:
            msg += "📌 " + k + ": <b>" + v + "</b>\n"
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
                msg += "🔸 <b>" + k + "</b>\n🟢 بيع: " + v["sell"] + " | 🔴 شراء: " + v["buy"] + "\n"
                if k in daily_high and k in daily_low:
                    msg += "📈 أعلى: " + str(daily_high[k]["sell"]) + " (" + daily_high[k]["time"] + ")\n"
                    msg += "📉 أقل: " + str(daily_low[k]["sell"]) + " (" + daily_low[k]["time"] + ")\n"
                avg_sell, avg_buy = get_avg(k)
                if avg_sell is not None:
                    msg += "📊 متوسط: " + f"{avg_sell:.2f}" + "\n"
                if k in yesterday_close:
                    y_sell = D(yesterday_close[k]["sell"])
                    c_sell = D(v["sell"])
                    change = pct_change(c_sell, y_sell)
                    if change is not None:
                        arrow = "⬆️" if change > 0 else "⬇️" if change < 0 else "➖"
                        msg += arrow + " مقارنة بأمس: " + f"{change:+.2f}%" + "\n"
                msg += "──────────────\n"
            else:
                msg += "📌 " + k + ": <b>" + v + "</b>\n"
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
                if page_hash != last_hash:
                    alerts = run_smart_alerts(data, last_data)
                    for alert_msg in alerts:
                        send(alert_msg)
                        time.sleep(1)
                    send(format_msg(data))
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
        "daily_low": {k: {"sell": str(v["sell"]), "time": v["time"]} for k, v in daily_low.items()},
        "alert_config": {
            "pct_threshold": ALERT_CONFIG["pct_threshold"],
            "value_threshold": ALERT_CONFIG["value_threshold"],
            "rapid_pct_threshold": ALERT_CONFIG["rapid_pct_threshold"],
            "cooldown_minutes": ALERT_CONFIG["cooldown_minutes"],
        },
        "alert_history": {k: v[-5:] if v else [] for k, v in alert_history.items()}
    })

@app.route("/alerts")
def alerts_info():
    return jsonify({
        "config": ALERT_CONFIG,
        "history": alert_history,
        "last_alert_time": {k: v.strftime("%I:%M %p") for k, v in last_alert_time.items()}
    })

@app.route("/")
def home():
    return "💎 Live Gold System Running with Smart Alerts"

# =====================
# START
# =====================
if __name__ == "__main__":
    Thread(target=loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))    "pct_threshold": float(os.getenv("ALERT_PCT_THRESHOLD", "0.5")),  # 0.5% default
    
    # قيمة التغير المطلق (مثلاً 5 جنيه)
    "value_threshold": float(os.getenv("ALERT_VALUE_THRESHOLD", "5.0")),  # 5 EGP default
    
    # نسبة التغير السريع (خلال فترة قصيرة)
    "rapid_pct_threshold": float(os.getenv("ALERT_RAPID_PCT", "1.5")),  # 1.5% in short time
    
    # فترة التغير السريع (بالدقايق)
    "rapid_time_window": int(os.getenv("ALERT_RAPID_MINUTES", "10")),  # 10 minutes
    
    # أقل وقت بين تنبيهين (بالدقايق) - عشان متهوش القناة
    "cooldown_minutes": int(os.getenv("ALERT_COOLDOWN", "5")),  # 5 minutes
    
    # عيارات مهمة (هنبعت تنبيهات ليها بس)
    "priority_karat": ["عيار 24", "عيار 21", "عيار 18"],
    
    # تفعيل/تعطيل أنواع التنبيهات
    "enable_pct_alert": os.getenv("ENABLE_PCT_ALERT", "true").lower() == "true",
    "enable_value_alert": os.getenv("ENABLE_VALUE_ALERT", "true").lower() == "true",
    "enable_rapid_alert": os.getenv("ENABLE_RAPID_ALERT", "true").lower() == "true",
    "enable_high_low_alert": os.getenv("ENABLE_HIGH_LOW_ALERT", "true").lower() == "true",
    "enable_open_close_alert": os.getenv("ENABLE_OPEN_CLOSE_ALERT", "true").lower() == "true",
}

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
alert_history = {}  # {karat: [(timestamp, type, value), ...]}
last_alert_time = {}  # {karat: datetime}
price_history = {}  # {karat: [(timestamp, sell_price), ...]} - للتغير السريع

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
# SMART ALERTS ENGINE
# =====================
def add_price_to_history(karat, sell_price):
    """نضيف السعر للتاريخ عشان نتابع التغيرات السريعة"""
    global price_history
    now = datetime.now(egypt_tz)
    
    if karat not in price_history:
        price_history[karat] = []
    
    price_history[karat].append((now, sell_price))
    
    # نمسح البيانات القديمة (أكبر من الـ window المحددة)
    cutoff = now - timedelta(minutes=ALERT_CONFIG["rapid_time_window"] * 2)
    price_history[karat] = [(t, p) for t, p in price_history[karat] if t > cutoff]

def can_send_alert(karat, alert_type):
    """نتأكد إن الـ cooldown عدى"""
    global last_alert_time
    now = datetime.now(egypt_tz)
    
    key = f"{karat}_{alert_type}"
    if key not in last_alert_time:
        return True
    
    cooldown = timedelta(minutes=ALERT_CONFIG["cooldown_minutes"])
    if now - last_alert_time[key] >= cooldown:
        return True
    
    return False

def record_alert(karat, alert_type, value):
    """نسجل التنبيه"""
    global alert_history, last_alert_time
    
    now = datetime.now(egypt_tz)
    key = f"{karat}_{alert_type}"
    
    if karat not in alert_history:
        alert_history[karat] = []
    
    alert_history[karat].append({
        "time": now.strftime("%I:%M %p"),
        "type": alert_type,
        "value": float(value)
    })
    
    last_alert_time[key] = now

def check_pct_alert(karat, current_sell, previous_sell):
    """تنبيه نسبة التغير"""
    if not ALERT_CONFIG["enable_pct_alert"]:
        return None
    
    change = pct_change(current_sell, previous_sell)
    if change is None:
        return None
    
    threshold = ALERT_CONFIG["pct_threshold"]
    
    if abs(change) >= threshold:
        if not can_send_alert(karat, "pct"):
            return None
        
        direction = "⬆️ ارتفاع" if change > 0 else "⬇️ انخفاض"
        emoji = "🚀" if change > 0 else "📉"
        
        msg = f"""{emoji} <b>تنبيه تغير نسبي!</b>

🔸 <b>{karat}</b>
{direction}: <b>{change:+.2f}%</b>
💰 السعر الحالي: {current_sell}
📊 السعر السابق: {previous_sell}
⏰ {datetime.now(egypt_tz).strftime("%I:%M %p")}"""
        
        record_alert(karat, "pct", change)
        return msg
    
    return None

def check_value_alert(karat, current_sell, previous_sell):
    """تنبيه التغير المطلق"""
    if not ALERT_CONFIG["enable_value_alert"]:
        return None
    
    diff = float(current_sell - previous_sell)
    threshold = ALERT_CONFIG["value_threshold"]
    
    if abs(diff) >= threshold:
        if not can_send_alert(karat, "value"):
            return None
        
        direction = "⬆️ ارتفاع" if diff > 0 else "⬇️ انخفاض"
        emoji = "💹" if diff > 0 else "🔻"
        
        msg = f"""{emoji} <b>تنبيه تغير قيمي!</b>

🔸 <b>{karat}</b>
{direction}: <b>{diff:+.2f} جنيه</b>
💰 السعر الحالي: {current_sell}
📊 السعر السابق: {previous_sell}
⏰ {datetime.now(egypt_tz).strftime("%I:%M %p")}"""
        
        record_alert(karat, "value", diff)
        return msg
    
    return None

def check_rapid_change_alert(karat, current_sell):
    """تنبيه التغير السريع"""
    if not ALERT_CONFIG["enable_rapid_alert"]:
        return None
    
    if karat not in price_history or len(price_history[karat]) < 2:
        return None
    
    window = timedelta(minutes=ALERT_CONFIG["rapid_time_window"])
    now = datetime.now(egypt_tz)
    
    # ندور على أول سعر في الفترة
    old_prices = [(t, p) for t, p in price_history[karat] if now - t <= window]
    if not old_prices:
        return None
    
    oldest_price = old_prices[0][1]
    change = pct_change(current_sell, oldest_price)
    
    if change is None:
        return None
    
    threshold = ALERT_CONFIG["rapid_pct_threshold"]
    
    if abs(change) >= threshold:
        if not can_send_alert(karat, "rapid"):
            return None
        
        direction = "🚀 صاروخي" if change > 0 else "🔥 انهيار"
        
        msg = f"""⚡ <b>تنبيه تغير سريع!</b>

🔸 <b>{karat}</b>
{direction}: <b>{change:+.2f}%</b> خلال {ALERT_CONFIG["rapid_time_window"]} دقيقة
💰 السعر الحالي: {current_sell}
📊 السعر قبل: {oldest_price}
⏰ {datetime.now(egypt_tz).strftime("%I:%M %p")}"""
        
        record_alert(karat, "rapid", change)
        return msg
    
    return None

def check_high_low_alert(karat, current_sell):
    """تنبيه اختراق أعلى/أقل مستوى"""
    if not ALERT_CONFIG["enable_high_low_alert"]:
        return None
    
    if karat not in daily_high or karat not in daily_low:
        return None
    
    alerts = []
    
    # أعلى مستوى جديد
    if current_sell > daily_high[karat]["sell"]:
        if can_send_alert(karat, "new_high"):
            msg = f"""🏆 <b>أعلى مستوى جديد لليوم!</b>

🔸 <b>{karat}</b>
💰 السعر: <b>{current_sell}</b>
📈 السابق: {daily_high[karat]["sell"]} ({daily_high[karat]["time"]})
⏰ {datetime.now(egypt_tz).strftime("%I:%M %p")}"""
            record_alert(karat, "new_high", float(current_sell))
            alerts.append(msg)
    
    # أقل مستوى جديد
    if current_sell < daily_low[karat]["sell"]:
        if can_send_alert(karat, "new_low"):
            msg = f"""🎯 <b>أقل مستوى جديد لليوم!</b>

🔸 <b>{karat}</b>
💰 السعر: <b>{current_sell}</b>
📉 السابق: {daily_low[karat]["sell"]} ({daily_low[karat]["time"]})
⏰ {datetime.now(egypt_tz).strftime("%I:%M %p")}"""
            record_alert(karat, "new_low", float(current_sell))
            alerts.append(msg)
    
    return alerts if alerts else None

def run_smart_alerts(data, previous_data):
    """نشغل كل التنبيهات"""
    all_alerts = []
    
    for k, v in data.items():
        if not isinstance(v, dict):
            continue
        
        # نتأكد إن العيار مهم
        is_priority = any(pk in k for pk in ALERT_CONFIG["priority_karat"])
        if not is_priority:
            continue
        
        current_sell = D(v["sell"])
        
        # نضيف للتاريخ
        add_price_to_history(k, current_sell)
        
        # لو فيه بيانات سابقة
        if previous_data and k in previous_data and isinstance(previous_data[k], dict):
            previous_sell = D(previous_data[k]["sell"])
            
            # تنبيه نسبي
            alert = check_pct_alert(k, current_sell, previous_sell)
            if alert:
                all_alerts.append(alert)
            
            # تنبيه قيمي
            alert = check_value_alert(k, current_sell, previous_sell)
            if alert:
                all_alerts.append(alert)
        
        # تنبيه تغير سريع
        alert = check_rapid_change_alert(k, current_sell)
        if alert:
            all_alerts.append(alert)
        
        # تنبيه أعلى/أقل
        alerts = check_high_low_alert(k, current_sell)
        if alerts:
            all_alerts.extend(alerts)
    
    return all_alerts

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
    msg = "💎 <b>تحديث لحظي للذهب</b>\\n\\n━━━━━━━━━━━━━━\\n"

    for k, v in data.items():
        if isinstance(v, dict):
            msg += f"🔸 <b>{k}</b>\\n🟢 بيع: {v['sell']} | 🔴 شراء: {v['buy']}\\n──────────────\\n"
        else:
            msg += f"📌 {k}: <b>{v}</b>\\n"

    return msg + "━━━━━━━━━━━━━━\\n"

# =====================
# FORMAT CLOSE (WITH STATS)
# =====================
def format_close_msg(data):
    msg = "🌙 <b>إغلاق سوق الذهب اليوم</b>\\n\\n"

    if data:
        msg += "📊 <b>آخر سعر قبل الإغلاق:</b>\\n━━━━━━━━━━━━━━\\n"

        for k, v in data.items():
            if isinstance(v, dict):
                msg += f"🔸 <b>{k}</b>\\n🟢 بيع: {v['sell']} | 🔴 شراء: {v['buy']}\\n"

                if k in daily_high and k in daily_low:
                    msg += f"📈 أعلى: {daily_high[k]['sell']} ({daily_high[k]['time']})\\n"
                    msg += f"📉 أقل: {daily_low[k]['sell']} ({daily_low[k]['time']})\\n"

                avg_sell, avg_buy = get_avg(k)
                if avg_sell is not None:
                    msg += f"📊 متوسط: {avg_sell:.2f}\\n"

                if k in yesterday_close:
                    y_sell = D(yesterday_close[k]["sell"])
                    c_sell = D(v["sell"])
                    change = pct_change(c_sell, y_sell)
                    if change is not None:
                        arrow = "⬆️" if change > 0 else "⬇️" if change < 0 else "➖"
                        msg += f"{arrow} مقارنة بأمس: {change:+.2f}%\\n"

                msg += "──────────────\\n"
            else:
                msg += f"📌 {k}: <b>{v}</b>\\n"

        msg += "━━━━━━━━━━━━━━\\n"

    msg += "\\n❤️ شكراً لمتابعتكم\\n💎 نلقاكم 10 صباحاً"
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

                if page_hash != last_hash:
                    # ====== SMART ALERTS ======
                    alerts = run_smart_alerts(data, last_data)
                    
                    # نبعت التنبيهات الأول
                    for alert_msg in alerts:
                        send(alert_msg)
                        time.sleep(1)  # نبطأ شوية عشان التيليجرام
                    
                    # بعدين نبعت التحديث العادي
                    send(format_msg(data))
                    
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
        "daily_low": {k: {"sell": str(v["sell"]), "time": v["time"]} for k, v in daily_low.items()},
        "alert_config": {
            "pct_threshold": ALERT_CONFIG["pct_threshold"],
            "value_threshold": ALERT_CONFIG["value_threshold"],
            "rapid_pct_threshold": ALERT_CONFIG["rapid_pct_threshold"],
            "cooldown_minutes": ALERT_CONFIG["cooldown_minutes"],
        },
        "alert_history": {k: v[-5:] if v else [] for k, v in alert_history.items()}  # آخر 5 تنبيهات
    })

@app.route("/alerts")
def alerts_info():
    """صفحة تعرض معلومات التنبيهات"""
    return jsonify({
        "config": ALERT_CONFIG,
        "history": alert_history,
        "last_alert_time": {k: v.strftime("%I:%M %p") for k, v in last_alert_time.items()}
    })

@app.route("/")
def home():
    return "💎 Live Gold System Running with Smart Alerts"

# =====================
# START
# =====================
if __name__ == "__main__":
    Thread(target=loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
'''

print("Code prepared successfully!")
print(f"Total lines: {len(enhanced_code.splitlines())}")
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

                if page_hash != last_hash:
                    send(format_msg(data))
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
