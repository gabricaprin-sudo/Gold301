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
from flask import Flask, jsonify, request, render_template_string
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
last_source = "unknown"

# =====================
# CACHE
# =====================
cached_data = None
cache_timestamp = None
CACHE_TTL = 300

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
# PRICE FORMATTER
# =====================
def format_price(value):
    try:
        return str(int(float(value)))
    except (ValueError, TypeError):
        return str(value)

def calc_spread(data):
    try:
        if "الذهب عيار 24" in data and isinstance(data["الذهب عيار 24"], dict):
            sell = float(data["الذهب عيار 24"].get("sell", 0))
            buy = float(data["الذهب عيار 24"].get("buy", 0))
            spread = sell - buy
            return str(int(spread))
    except (ValueError, TypeError, KeyError):
        pass
    return "--"

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
        sell = D(v.get("sell", 0))
        buy = D(v.get("buy", 0))

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

        log.info(f"Primary source keys: {list(obj.keys())}")

        if "data" not in obj or "goldPrices" not in obj["data"]:
            log.warning("Unexpected primary source structure")
            return {}

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

        data["الجنيه الذهب"] = str(obj["data"].get("goldPound", "--"))
        data["الأوقية العالمية"] = str(obj["data"].get("goldOunce", "--"))

        gram24 = Decimal(data["الذهب عيار 24"]["sell"])
        ounce = Decimal(data["الأوقية العالمية"])

        gold_dollar = (gram24 * Decimal("31.1034768")) / ounce
        data["دولار الصاغة"] = f"{gold_dollar:.2f}"

        return data

    except Exception as e:
        log.warning(f"Primary source failed: {e}")
        return {}

# =====================
# BACKUP SOURCE
# =====================
def get_snapshot_backup(retries=3):
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
            usd_rate = None

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
                    usd_rate = D(nums[0].text)
                    data["الدولار الأمريكي"] = str(usd_rate)

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
# UNIFIED SNAPSHOT
# =====================
def get_snapshot():
    global cached_data, cache_timestamp, last_source

    data = get_snapshot_primary()
    if data:
        log.info("PRIMARY SOURCE")
        last_source = "primary"
        cached_data = data
        cache_timestamp = time.time()
        return data

    log.warning("PRIMARY FAILED - trying backup")

    data = get_snapshot_backup()
    if data:
        log.info("BACKUP SOURCE")
        last_source = "backup"
        cached_data = data
        cache_timestamp = time.time()
        return data

    log.warning("BACKUP FAILED - trying cache")

    if cached_data and cache_timestamp:
        age = time.time() - cache_timestamp
        if age < CACHE_TTL:
            log.info(f"USING CACHE (age: {age:.0f}s)")
            last_source = "cache"
            return cached_data
        else:
            log.warning(f"Cache expired (age: {age:.0f}s)")

    log.error("ALL SOURCES FAILED + NO VALID CACHE")
    last_source = "failed"
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

EXTRAS_ORDER = [
    "الجنيه الذهب",
    "الأوقية العالمية",
    "دولار الصاغة",
    "الدولار الأمريكي",
    "الفجوة السعرية",
]

def format_prices(title, data):
    msg = f"{title}\n\n"
    msg += "━━━━━━━━━━━━━━\n"

    for karat in KARAT_ORDER:
        if karat in data and isinstance(data[karat], dict):
            v = data[karat]
            msg += f"🔸 <b>{karat}:</b>\n"
            msg += f"🟢 بيع: {format_price(v.get('sell', '--'))} | 🔴 شراء: {format_price(v.get('buy', '--'))}\n"
            msg += "──────────────\n"

    for k, v in data.items():
        if isinstance(v, dict) and "عيار" in k and k not in KARAT_ORDER:
            msg += f"🔸 <b>{k}:</b>\n"
            msg += f"🟢 بيع: {format_price(v.get('sell', '--'))} | 🔴 شراء: {format_price(v.get('buy', '--'))}\n"
            msg += "──────────────\n"

    msg += "━━━━━━━━━━━━━━\n"

    for key in EXTRAS_ORDER:
        if key == "الفجوة السعرية":
            spread = calc_spread(data)
            msg += f"📌 {key}: <b>{spread}</b>\n"
        elif key in data:
            msg += f"📌 {key}: <b>{data[key]}</b>\n"

    for k, v in data.items():
        if not isinstance(v, dict) and k not in EXTRAS_ORDER and "عيار" not in k:
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
            msg += f"🟢 بيع: {format_price(v.get('sell', '--'))} | 🔴 شراء: {format_price(v.get('buy', '--'))}\n"

            if karat in daily_high and karat in daily_low:
                msg += f"📈 أعلى: {format_price(daily_high[karat]['sell'])} ({daily_high[karat]['time']})\n"
                msg += f"📉 أقل: {format_price(daily_low[karat]['sell'])} ({daily_low[karat]['time']})\n"

            avg_sell, avg_buy = get_avg(karat)
            if avg_sell is not None:
                msg += f"📊 متوسط: {format_price(avg_sell)}\n"

            if karat in yesterday_close:
                y_sell = D(yesterday_close[karat].get("sell", 0))
                c_sell = D(v.get("sell", 0))
                change = pct_change(c_sell, y_sell)
                if change is not None:
                    arrow = "⬆️" if change > 0 else "⬇️" if change < 0 else "➖"
                    msg += f"{arrow} مقارنة بأمس: {change:+.2f}%\n"

            msg += "──────────────\n"

    for k, v in data.items():
        if isinstance(v, dict) and "عيار" in k and k not in KARAT_ORDER:
            msg += f"🔸 <b>{k}:</b>\n"
            msg += f"🟢 بيع: {format_price(v.get('sell', '--'))} | 🔴 شراء: {format_price(v.get('buy', '--'))}\n"
            msg += "──────────────\n"

    msg += "━━━━━━━━━━━━━━\n"

    for key in EXTRAS_ORDER:
        if key == "الفجوة السعرية":
            spread = calc_spread(data)
            msg += f"📌 {key}: <b>{spread}</b>\n"
        elif key in data:
            msg += f"📌 {key}: <b>{data[key]}</b>\n"

    for k, v in data.items():
        if not isinstance(v, dict) and k not in EXTRAS_ORDER and "عيار" not in k:
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

            if 10 <= hour < 24:
                sent_close_msg = False

                if not sent_open_msg:
                    log.info("Market opened - sending opening message")
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
                    log.info("Gold prices changed - sending update")
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
                                yesterday_close[k] = {"sell": v.get("sell", 0), "buy": v.get("buy", 0)}
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
# HTML TEMPLATE
# =====================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>💎 أسعار الذهب اللحظية</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            min-height: 100vh;
            color: #fff;
            padding: 20px;
        }
        .container {
            max-width: 500px;
            margin: 0 auto;
        }
        .header {
            text-align: center;
            padding: 30px 0;
        }
        .header h1 {
            font-size: 2rem;
            margin-bottom: 10px;
            background: linear-gradient(45deg, #ffd700, #ffed4a);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        .header p {
            color: #888;
            font-size: 0.9rem;
        }
        .status {
            text-align: center;
            margin-bottom: 20px;
            padding: 10px;
            border-radius: 10px;
            font-size: 0.85rem;
        }
        .status.live {
            background: rgba(0, 255, 0, 0.1);
            color: #4ade80;
            border: 1px solid rgba(74, 222, 128, 0.3);
        }
        .status.cache {
            background: rgba(255, 193, 7, 0.1);
            color: #fbbf24;
            border: 1px solid rgba(251, 191, 36, 0.3);
        }
        .status.offline {
            background: rgba(255, 0, 0, 0.1);
            color: #f87171;
            border: 1px solid rgba(248, 113, 113, 0.3);
        }
        .card {
            background: rgba(255, 255, 255, 0.05);
            border-radius: 15px;
            padding: 20px;
            margin-bottom: 15px;
            border: 1px solid rgba(255, 255, 255, 0.1);
            backdrop-filter: blur(10px);
        }
        .karat-title {
            font-size: 1.2rem;
            font-weight: bold;
            margin-bottom: 15px;
            color: #ffd700;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .prices-row {
            display: flex;
            justify-content: space-between;
            gap: 10px;
        }
        .price-box {
            flex: 1;
            padding: 15px;
            border-radius: 10px;
            text-align: center;
        }
        .price-box.sell {
            background: rgba(34, 197, 94, 0.15);
            border: 1px solid rgba(34, 197, 94, 0.3);
        }
        .price-box.buy {
            background: rgba(239, 68, 68, 0.15);
            border: 1px solid rgba(239, 68, 68, 0.3);
        }
        .price-label {
            font-size: 0.8rem;
            margin-bottom: 5px;
            opacity: 0.8;
        }
        .price-value {
            font-size: 1.5rem;
            font-weight: bold;
        }
        .price-box.sell .price-value { color: #4ade80; }
        .price-box.buy .price-value { color: #f87171; }
        .divider {
            height: 1px;
            background: rgba(255, 255, 255, 0.1);
            margin: 15px 0;
        }
        .extra-data {
            display: flex;
            flex-direction: column;
            gap: 10px;
        }
        .extra-item {
            display: flex;
            justify-content: space-between;
            padding: 10px;
            background: rgba(255, 255, 255, 0.03);
            border-radius: 8px;
        }
        .extra-label { color: #888; font-size: 0.9rem; }
        .extra-value { font-weight: bold; color: #ffd700; }
        .footer {
            text-align: center;
            padding: 20px;
            color: #666;
            font-size: 0.8rem;
        }
        .loading {
            text-align: center;
            padding: 50px;
            font-size: 1.2rem;
            color: #888;
        }
        .error {
            text-align: center;
            padding: 50px;
            color: #f87171;
        }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
        .live-dot {
            display: inline-block;
            width: 8px;
            height: 8px;
            background: #4ade80;
            border-radius: 50%;
            animation: pulse 2s infinite;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>💎 أسعار الذهب</h1>
            <p>تحديث لحظي - مصر</p>
        </div>

        <div id="status" class="status offline">
            ⏳ جاري التحميل...
        </div>

        <div id="content">
            <div class="loading">جاري تحميل الأسعار...</div>
        </div>

        <div class="footer">
            <p>آخر تحديث: <span id="lastUpdate">--</span></p>
            <p style="margin-top: 5px;">⚡ يتم التحديث تلقائياً كل 10 ثواني</p>
        </div>
    </div>

    <script>
        let lastData = null;

        function formatPrice(value) {
            return Math.floor(parseFloat(value)).toLocaleString('ar-EG');
        }

        function calcSpread(data) {
            if (data["الذهب عيار 24"] && typeof data["الذهب عيار 24"] === 'object') {
                const sell = parseFloat(data["الذهب عيار 24"].sell);
                const buy = parseFloat(data["الذهب عيار 24"].buy);
                return Math.floor(sell - buy);
            }
            return '--';
        }

        function renderData(data, source, updatedAt) {
            const content = document.getElementById('content');
            const status = document.getElementById('status');
            const lastUpdate = document.getElementById('lastUpdate');

            if (source === 'live') {
                status.className = 'status live';
                status.innerHTML = '<span class="live-dot"></span> متصل مباشرة';
            } else if (source === 'cache') {
                status.className = 'status cache';
                status.innerHTML = '⚡ من الكاش';
            } else {
                status.className = 'status offline';
                status.innerHTML = '❌ غير متصل';
            }

            if (updatedAt) {
                const date = new Date(updatedAt);
                lastUpdate.textContent = date.toLocaleString('ar-EG');
            }

            let html = '';

            const karats = ['الذهب عيار 24', 'الذهب عيار 21', 'الذهب عيار 18', 'الذهب عيار 14'];

            karats.forEach(karat => {
                if (data[karat] && typeof data[karat] === 'object') {
                    html += `
                        <div class="card">
                            <div class="karat-title">🔸 ${karat}</div>
                            <div class="prices-row">
                                <div class="price-box sell">
                                    <div class="price-label">🟢 سعر البيع</div>
                                    <div class="price-value">${formatPrice(data[karat].sell)}</div>
                                </div>
                                <div class="price-box buy">
                                    <div class="price-label">🔴 سعر الشراء</div>
                                    <div class="price-value">${formatPrice(data[karat].buy)}</div>
                                </div>
                            </div>
                        </div>
                    `;
                }
            });

            const extrasOrder = [
                { key: 'الجنيه الذهب', label: 'الجنيه الذهب' },
                { key: 'الأوقية العالمية', label: 'الأوقية العالمية' },
                { key: 'دولار الصاغة', label: 'دولار الصاغة' },
                { key: 'الدولار الأمريكي', label: 'الدولار الأمريكي' },
                { key: 'spread', label: 'الفجوة السعرية', value: calcSpread(data) }
            ];

            let extrasHtml = '';
            extrasOrder.forEach(item => {
                let value;
                if (item.key === 'spread') {
                    value = item.value;
                } else if (data[item.key]) {
                    value = data[item.key];
                } else {
                    return;
                }
                extrasHtml += `
                    <div class="extra-item">
                        <span class="extra-label">📌 ${item.label}</span>
                        <span class="extra-value">${value}</span>
                    </div>
                `;
            });

            if (extrasHtml) {
                html += `<div class="card"><div class="extra-data">${extrasHtml}</div></div>`;
            }

            content.innerHTML = html;
        }

        async function fetchPrices() {
            try {
                const response = await fetch('/api/prices');
                const result = await response.json();

                if (result.data && Object.keys(result.data).length > 0) {
                    lastData = result.data;
                    renderData(result.data, result.source, result.updated_at);
                } else if (lastData) {
                    renderData(lastData, 'offline', null);
                } else {
                    document.getElementById('content').innerHTML = 
                        '<div class="error">❌ لا توجد بيانات متاحة حالياً</div>';
                }
            } catch (error) {
                console.error('Fetch error:', error);
                if (lastData) {
                    renderData(lastData, 'offline', null);
                } else {
                    document.getElementById('content').innerHTML = 
                        '<div class="error">❌ فشل الاتصال بالخادم</div>';
                }
            }
        }

        fetchPrices();
        setInterval(fetchPrices, 10000);
    </script>
</body>
</html>
"""

# =====================
# API ENDPOINTS
# =====================
@app.route("/")
def home():
    return render_template_string(HTML_TEMPLATE)

@app.route("/api/prices")
def api_prices():
    global cached_data

    if cached_data:
        return jsonify({
            "data": cached_data,
            "source": last_source,
            "updated_at": datetime.fromtimestamp(cache_timestamp, egypt_tz).isoformat() if cache_timestamp else None
        })

    data = get_snapshot()
    return jsonify({
        "data": data,
        "source": last_source,
        "updated_at": datetime.now(egypt_tz).isoformat()
    })

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
        "last_source": last_source,
        "daily_high": {k: {"sell": str(v["sell"]), "time": v["time"]} for k, v in daily_high.items()},
        "daily_low": {k: {"sell": str(v["sell"]), "time": v["time"]} for k, v in daily_low.items()},
        "yesterday_close": yesterday_close,
        "cache_age": round(time.time() - cache_timestamp, 1) if cache_timestamp else None
    })

# =====================
# START
# =====================
if __name__ == "__main__":
    Thread(target=loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
