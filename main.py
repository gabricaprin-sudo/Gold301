import os
import time
import json
import re
import html
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
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL = os.getenv("TELEGRAM_CHANNEL", "@AndriaGold")
URL = os.getenv("GOLD_URL", "https://edahabapp.com/")
API_KEY = os.getenv("API_KEY")

if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required!")

getcontext().prec = 28

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
last_data = None
sent_close_msg = False
sent_open_msg = False
fail_count = 0
yesterday_close = {}

# =====================
# CACHE
# =====================
cached_data = None
cache_timestamp = None
CACHE_TTL = 300  # 5 minutes

# =====================
# REQUESTS SESSION
# =====================
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "en-US,en;q=0.9",
})

# =====================
# PERSISTENCE
# =====================
STATE_FILE = "gold_state.json"

def load_state():
    """Load persistent state from file"""
    global yesterday_close, sent_open_msg
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
                yesterday_close = state.get("yesterday_close", {})
                sent_open_msg = state.get("sent_open_msg", False)
                log.info("State loaded from file")
    except Exception as e:
        log.error(f"Failed to load state: {e}")
        yesterday_close = {}
        sent_open_msg = False

def save_state():
    """Save persistent state to file"""
    try:
        state = {
            "yesterday_close": yesterday_close,
            "sent_open_msg": sent_open_msg,
            "saved_at": datetime.now(egypt_tz).isoformat()
        }
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"Failed to save state: {e}")

# =====================
# CLEAN DECIMAL
# =====================
def D(x):
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x).replace(",", "").strip())

# =====================
# DAILY STATS
# =====================
daily_high = {}
daily_low = {}
daily_sums = {}

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
# PRIMARY SOURCE
# =====================
def get_snapshot_primary():
    """Extract gold prices from prices-dashboard wire:snapshot"""
    try:
        response = session.get(
            "https://edahabapp.com/prices-dashboard",
            timeout=(5, 10)
        )
        response.raise_for_status()

        match = re.search(
            r'wire:snapshot="([^"]+)"',
            response.text
        )

        if not match:
            log.warning("No wire:snapshot found in primary source")
            return {}

        snapshot = html.unescape(match.group(1))
        obj = json.loads(snapshot)

        prices = obj["data"]["goldPrices"][0]

        data = {}

        data["الذهب عيار 24"] = {
            "sell": str(prices["24"][0]["ask"]),
            "buy": str(prices["24"][0]["bid"])
        }

        data["الذهب عيار 21"] = {
            "sell": str(prices["21"][0]["ask"]),
            "buy": str(prices["21"][0]["bid"])
        }

        data["الذهب عيار 18"] = {
            "sell": str(prices["18"][0]["ask"]),
            "buy": str(prices["18"][0]["bid"])
        }

        data["الذهب عيار 14"] = {
            "sell": str(prices["14"][0]["ask"]),
            "buy": str(prices["14"][0]["bid"])
        }

        data["الجنيه الذهب"] = str(obj["data"]["goldPound"])
        data["الأوقية العالمية"] = str(obj["data"]["goldOunce"])

        gram24 = Decimal(data["الذهب عيار 24"]["sell"])
        ounce = Decimal(data["الأوقية العالمية"])

        gold_dollar = (
            gram24 * Decimal("31.1034768")
        ) / ounce

        data["دولار الصاغة"] = f"{gold_dollar:.2f}"

        return data

    except Exception as e:
        log.warning(f"Primary source failed: {e}")
        return {}

# =====================
# BACKUP SOURCE
# =====================
def get_snapshot_backup(retries=3):
    """Extract gold prices from main page (backup source)"""
    global fail_count

    for attempt in range(retries):
        try:
            if attempt > 0:
                sleep_time = 2 ** attempt + random.uniform(0, 2)
                log.info(f"Retry attempt {attempt + 1}, waiting {sleep_time:.1f}s...")
                time.sleep(sleep_time)
            else:
                time.sleep(2 + random.randint(0, 3))

            html_text = session.get(URL, timeout=(5, 10)).text
            soup = BeautifulSoup(html_text, "html.parser")
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
                log.warning("No data extracted from backup page")
                continue

            fail_count = 0
            return data

        except Exception as e:
            fail_count += 1
            log.error(f"Backup attempt {attempt + 1}/{retries} failed: {e}")

    log.error("All backup snapshot attempts failed")
    return {}

# =====================
# UNIFIED SNAPSHOT (WITH CACHE)
# =====================
def get_snapshot():
    """Try primary source first, then backup, then cache"""
    global cached_data, cache_timestamp

    # Try primary source
    data = get_snapshot_primary()
    if data:
        log.info("PRIMARY SOURCE ✓")
        cached_data = data
        cache_timestamp = time.time()
        return data

    log.warning("PRIMARY FAILED → trying backup")

    # Try backup source
    data = get_snapshot_backup()
    if data:
        log.info("BACKUP SOURCE ✓")
        cached_data = data
        cache_timestamp = time.time()
        return data

    log.warning("BACKUP FAILED → trying cache")

    # Use cached data if still valid
    if cached_data and cache_timestamp:
        age = time.time() - cache_timestamp
        if age < CACHE_TTL:
            log.info(f"USING CACHE (age: {age:.0f}s)")
            return cached_data
        else:
            log.warning(f"Cache expired (age: {age:.0f}s)")

    log.error("ALL SOURCES FAILED + NO VALID CACHE")
    return {}

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
            resp = session.post(url, data={
                "chat_id": CHANNEL,
                "text": msg,
                "parse_mode": "HTML",
                "reply_markup": json.dumps(keyboard)
            }, timeout=(5, 10))

            if resp.status_code == 200:
                log.info("Message sent successfully")
                return True
            else:
                log.warning(f"Telegram API returned {resp.status_code}: {resp.text}")
                time.sleep(2 ** attempt)

        except Exception as e:
            log.error(f"Send attempt {attempt + 1}/{retries} failed: {e}")
            time.sleep(2 ** attempt)

    log.error("Failed to send message after all retries")
    return False

# =====================
# MESSAGE FORMATTING
# =====================
KARAT_ORDER = [
    "الذهب عيار 24",
    "الذهب عيار 21",
    "الذهب عيار 18",
    "الذهب عيار 14",
]

def format_prices(title, data):
    """Unified formatter for both open and update messages"""
    msg = f"{title}\n\n"
    msg += "━━━━━━━━━━━━━━\n"

    # عيارات الدهب بالترتيب المحدد
    for karat in KARAT_ORDER:
        if karat in data and isinstance(data[karat], dict):
            v = data[karat]
            msg += f"🔸 <b>{karat}:</b>\n"
            msg += f"🟢 بيع: {v['sell']} | 🔴 شراء: {v['buy']}\n"
            msg += "──────────────\n"

    # أي عيارات تانية مش في الترتيب
    for k, v in data.items():
        if isinstance(v, dict) and "عيار" in k and k not in KARAT_ORDER:
            msg += f"🔸 <b>{k}:</b>\n"
            msg += f"🟢 بيع: {v['sell']} | 🔴 شراء: {v['buy']}\n"
            msg += "──────────────\n"

    msg += "━━━━━━━━━━━━━━\n"

    # باقي البيانات
    for k, v in data.items():
        if not isinstance(v, dict) or "عيار" not in k:
            msg += f"📌 {k}: <b>{v}</b>\n"

    msg += "━━━━━━━━━━━━━━"
    return msg

def format_open_msg(data):
    return format_prices("☀️ <b>افتتاح سوق الذهب</b>", data)

def format_msg(data):
    return format_prices("💎 <b>تحديث لحظي للذهب</b>", data)

def format_close_msg(data):
    if not data:
        return "🌙 <b>إغلاق سوق الذهب اليوم</b>\n\nلا توجد بيانات متاحة لليوم.\n\n❤️ شكراً لمتابعتكم\n💎 نلقاكم 10 صباحاً"

    msg = "🌙 <b>إغلاق سوق الذهب اليوم</b>\n\n"
    msg += "📊 <b>آخر سعر قبل الإغلاق:</b>\n"
    msg += "━━━━━━━━━━━━━━\n"

    for karat in KARAT_ORDER:
        if karat in data and isinstance(data[karat], dict):
            v = data[karat]
            msg += f"🔸 <b>{karat}:</b>\n"
            msg += f"🟢 بيع: {v['sell']} | 🔴 شراء: {v['buy']}\n"

            if karat in daily_high and karat in daily_low:
                msg += f"📈 أعلى: {daily_high[karat]['sell']} ({daily_high[karat]['time']})\n"
                msg += f"📉 أقل: {daily_low[karat]['sell']} ({daily_low[karat]['time']})\n"

            avg_sell, avg_buy = get_avg(karat)
            if avg_sell is not None:
                msg += f"📊 متوسط: {avg_sell:.2f}\n"

            if karat in yesterday_close:
                y_sell = D(yesterday_close[karat]["sell"])
                c_sell = D(v["sell"])
                change = pct_change(c_sell, y_sell)
                if change is not None:
                    arrow = "⬆️" if change > 0 else "⬇️" if change < 0 else "➖"
                    msg += f"{arrow} مقارنة بأمس: {change:+.2f}%\n"

            msg += "──────────────\n"

    # أي عيارات تانية
    for k, v in data.items():
        if isinstance(v, dict) and "عيار" in k and k not in KARAT_ORDER:
            msg += f"🔸 <b>{k}:</b>\n"
            msg += f"🟢 بيع: {v['sell']} | 🔴 شراء: {v['buy']}\n"
            msg += "──────────────\n"

    # باقي البيانات
    for k, v in data.items():
        if not isinstance(v, dict) or "عيار" not in k:
            msg += f"📌 {k}: <b>{v}</b>\n"

    msg += "━━━━━━━━━━━━━━\n"
    msg += "\n❤️ شكراً لمتابعتكم\n💎 نلقاكم 10 صباحاً"
    return msg

# =====================
# GOLD DATA COMPARISON
# =====================
def gold_changed(current, previous):
    if not previous:
        return True

    current_gold = {k: v for k, v in current.items() if isinstance(v, dict) and "عيار" in k}
    previous_gold = {k: v for k, v in previous.items() if isinstance(v, dict) and "عيار" in k}

    if set(current_gold.keys()) != set(previous_gold.keys()):
        return True

    for k in current_gold:
        if (current_gold[k].get("sell") != previous_gold[k].get("sell") or
            current_gold[k].get("buy") != previous_gold[k].get("buy")):
            return True

    return False

# =====================
# MAIN LOOP
# =====================
def loop():
    global last_data, sent_close_msg, sent_open_msg, yesterday_close

    load_state()

    while True:
        try:
            now = datetime.now(egypt_tz)
            hour = now.hour
            log.info(f"Current time: {now.strftime('%I:%M %p')} (hour: {hour})")

            # Market hours: 10 AM to 12 AM (midnight)
            if 10 <= hour < 24:
                sent_close_msg = False

                if not sent_open_msg:
                    log.info("Market opened → sending opening message")
                    data = get_snapshot()

                    if data:
                        reset_daily_stats()
                        update_stats(data)
                        send(format_open_msg(data))
                        last_data = data
                        sent_open_msg = True
                        save_state()
                        log.info("Opening message sent successfully")
                    else:
                        log.warning("No data available at market open, retrying in 30s")
                        time.sleep(30)
                        continue

                data = get_snapshot()

                if not data:
                    time.sleep(30)
                    continue

                update_stats(data)

                if last_data is None:
                    send(format_msg(data))
                    last_data = data
                    time.sleep(10)
                    continue

                if gold_changed(data, last_data):
                    log.info("Gold prices changed → sending update")
                    send(format_msg(data))
                    last_data = data
                else:
                    log.info("No price change detected")

                time.sleep(10)

            else:
                if not sent_close_msg:
                    if last_data:
                        yesterday_close = {}
                        for k, v in last_data.items():
                            if isinstance(v, dict):
                                yesterday_close[k] = {"sell": v["sell"], "buy": v["buy"]}
                        save_state()

                        send(format_close_msg(last_data))
                        log.info("Close message sent")
                    else:
                        log.warning("No last_data available for close message")
                        send(format_close_msg(None))

                    sent_close_msg = True
                    sent_open_msg = False
                    save_state()

                time.sleep(60)

        except Exception as e:
            log.error(f"Loop error: {e}")
            time.sleep(5)

# =====================
# API ENDPOINTS
# =====================
@app.route("/api")
def api():
    key = request.args.get("key")
    if not API_KEY or key != API_KEY:
        return jsonify({"error": "unauthorized"}), 403
    data = get_snapshot()
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
        "yesterday_close": yesterday_close,
        "cache_age": round(time.time() - cache_timestamp, 1) if cache_timestamp else None
    })

@app.route("/")
def home():
    return "💎 Live Gold System Running Securely"

# =====================
# START
# =====================
if __name__ == "__main__":
    Thread(target=loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
