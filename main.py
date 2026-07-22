import os
import time
import json
import hashlib
import requests
import random
import logging
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple
from bs4 import BeautifulSoup
from decimal import Decimal, getcontext
from flask import Flask, jsonify, request
from threading import Thread, Lock
from datetime import datetime, timedelta
from functools import wraps
from enum import Enum, auto
import pytz

# =====================
# CONFIGURATION
# =====================
class Config:
    """Centralized configuration with environment variable fallbacks."""
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8165343576:AAHjfPZpUUUDvWk3WbC1XocQ_MGQ1aESLT0")
    TELEGRAM_CHANNEL = os.getenv("TELEGRAM_CHANNEL", "@AndriaGold")
    PRIMARY_URL = os.getenv("PRIMARY_URL", "https://edahabapp.com/prices-dashboard")
    FALLBACK_URL = os.getenv("FALLBACK_URL", "https://edahabapp.com/")
    API_KEY = os.getenv("API_KEY")
    PORT = int(os.environ.get("PORT", 5000))
    MAX_RETRIES = 3
    PRIMARY_TIMEOUT = 8
    FALLBACK_TIMEOUT = 10
    LOOP_INTERVAL = 10
    MARKET_OPEN_HOUR = 10
    MARKET_CLOSE_HOUR = 24

# =====================
# ENUMS
# =====================
class SourceMode(Enum):
    PRIMARY = auto()
    FALLBACK = auto()

class MarketStatus(Enum):
    OPEN = auto()
    CLOSED = auto()

# =====================
# DATA CLASSES
# =====================
@dataclass
class GoldPrice:
    buy: Decimal
    sell: Decimal
    
    def __post_init__(self):
        self.buy = Decimal(str(self.buy))
        self.sell = Decimal(str(self.sell))
    
    def to_dict(self) -> Dict[str, str]:
        return {"buy": str(self.buy), "sell": str(self.sell)}

@dataclass  
class DailyStats:
    high_sell: Decimal = field(default_factory=lambda: Decimal("0"))
    high_buy: Decimal = field(default_factory=lambda: Decimal("0"))
    high_time: str = ""
    low_sell: Decimal = field(default_factory=lambda: Decimal("999999"))
    low_buy: Decimal = field(default_factory=lambda: Decimal("999999"))
    low_time: str = ""
    sell_sum: Decimal = field(default_factory=lambda: Decimal("0"))
    buy_sum: Decimal = field(default_factory=lambda: Decimal("0"))
    count: int = 0
    
    def update(self, price: GoldPrice, time_str: str):
        if price.sell > self.high_sell:
            self.high_sell, self.high_buy, self.high_time = price.sell, price.buy, time_str
        if price.sell < self.low_sell:
            self.low_sell, self.low_buy, self.low_time = price.sell, price.buy, time_str
        self.sell_sum += price.sell
        self.buy_sum += price.buy
        self.count += 1
    
    @property
    def avg_sell(self) -> Optional[Decimal]:
        return self.sell_sum / self.count if self.count > 0 else None
    
    @property
    def avg_buy(self) -> Optional[Decimal]:
        return self.buy_sum / self.count if self.count > 0 else None

# =====================
# FLASK APP
# =====================
app = Flask(__name__)

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
EGYPT_TZ = pytz.timezone("Africa/Cairo")

# =====================
# STATE (Thread-safe)
# =====================
class AppState:
    def __init__(self):
        self._lock = Lock()
        self.last_hash: Optional[str] = None
        self.last_data: Optional[Dict] = None
        self.sent_close_msg = False
        self.sent_open_msg = False
        self.fail_count = 0
        self.source_mode = SourceMode.PRIMARY
        self.daily_stats: Dict[str, DailyStats] = {}
        self.yesterday_close: Dict[str, GoldPrice] = {}
        self.market_status = MarketStatus.CLOSED
    
    @property
    def using_fallback(self) -> bool:
        return self.source_mode == SourceMode.FALLBACK
    
    def reset_stats(self):
        with self._lock:
            self.daily_stats.clear()
            log.info("Daily stats reset")
    
    def update_stats(self, data: Dict):
        with self._lock:
            now_str = datetime.now(EGYPT_TZ).strftime("%I:%M %p")
            for key, value in data.items():
                if not isinstance(value, dict):
                    continue
                try:
                    price = GoldPrice(value["buy"], value["sell"])
                    if key not in self.daily_stats:
                        self.daily_stats[key] = DailyStats()
                    self.daily_stats[key].update(price, now_str)
                except (KeyError, ValueError) as e:
                    log.warning(f"Failed to update stats for {key}: {e}")
    
    def get_stats(self, key: str) -> Optional[DailyStats]:
        with self._lock:
            return self.daily_stats.get(key)
    
    def set_yesterday_close(self, data: Dict):
        with self._lock:
            self.yesterday_close = {
                k: GoldPrice(v["buy"], v["sell"])
                for k, v in data.items() if isinstance(v, dict)
            }
    
    def switch_mode(self, mode: SourceMode):
        with self._lock:
            if self.source_mode != mode:
                self.source_mode = mode
                log.info(f"Source switched to: {mode.name}")
    
    def get_health(self) -> Dict:
        with self._lock:
            return {
                "status": "ok",
                "source_mode": self.source_mode.name,
                "last_data": bool(self.last_data),
                "fail_count": self.fail_count,
                "sent_open": self.sent_open_msg,
                "sent_close": self.sent_close_msg,
                "daily_high": {
                    k: {"sell": str(v.high_sell), "time": v.high_time}
                    for k, v in self.daily_stats.items()
                },
                "daily_low": {
                    k: {"sell": str(v.low_sell), "time": v.low_time}
                    for k, v in self.daily_stats.items()
                }
            }

state = AppState()

# =====================
# UTILITIES
# =====================
def parse_decimal(text: str) -> Decimal:
    """Safely parse decimal from text with comma removal."""
    cleaned = text.replace(",", "").replace("٬", "").strip()
    return Decimal(cleaned)

def pct_change(current: Decimal, previous: Optional[Decimal]) -> Optional[Decimal]:
    """Calculate percentage change safely."""
    if previous is None or previous == 0:
        return None
    return ((current - previous) / previous) * 100

def retry(max_retries: int, delay: float = 2.0):
    """Decorator for retry logic with exponential backoff."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    wait = delay * (2 ** attempt) + random.uniform(0, 1)
                    log.warning(f"{func.__name__} attempt {attempt + 1}/{max_retries} failed: {e}. Retrying in {wait:.1f}s...")
                    time.sleep(wait)
            log.error(f"{func.__name__} failed after {max_retries} attempts")
            return None
        return wrapper
    return decorator

def calculate_gold_dollar(gram_24: Decimal, ounce: Decimal) -> Optional[Decimal]:
    """Calculate gold dollar rate."""
    if gram_24 and ounce and ounce != 0:
        return (gram_24 * Decimal("31.1034768")) / ounce
    return None

# =====================
# EXTRACTORS
# =====================
class PriceExtractor:
    """Base class for price extraction from different sources."""
    
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    }
    
    def __init__(self, url: str, timeout: int):
        self.url = url
        self.timeout = timeout
    
    def fetch(self) -> Optional[str]:
        """Fetch HTML content."""
        try:
            time.sleep(random.uniform(1, 3))
            resp = requests.get(self.url, headers=self.HEADERS, timeout=self.timeout)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            log.error(f"Failed to fetch {self.url}: {e}")
            return None
    
    def parse(self, html: str) -> Tuple[Dict, str]:
        """Parse HTML and return (data, hash). Must be implemented by subclasses."""
        raise NotImplementedError
    
    def extract(self) -> Optional[Tuple[Dict, str]]:
        """Full extraction pipeline."""
        html = self.fetch()
        if not html:
            return None
        try:
            data, page_hash = self.parse(html)
            if data:
                log.info(f"[{self.__class__.__name__}] Extracted {len(data)} items")
                return data, page_hash
        except Exception as e:
            log.error(f"[{self.__class__.__name__}] Parse error: {e}")
        return None

class PrimaryExtractor(PriceExtractor):
    """Extractor for the primary dashboard URL."""
    
    def __init__(self):
        super().__init__(Config.PRIMARY_URL, Config.PRIMARY_TIMEOUT)
    
    def parse(self, html: str) -> Tuple[Dict, str]:
        soup = BeautifulSoup(html, "html.parser")
        data = {}
        gram_24 = None
        ounce = None
        
        # Try multiple selector strategies
        selectors = [
            ("div", "price-item"),
            ("tr", lambda x: x and "price" in x.lower() if x else False),
            ("div", lambda x: x and "gold" in x.lower() if x else False),
            ("div", "card"),
        ]
        
        items = []
        for tag, cls in selectors:
            items = soup.find_all(tag, class_=cls)
            if items:
                log.debug(f"Found {len(items)} items with {tag}.{cls}")
                break
        
        for item in items:
            result = self._parse_item(item)
            if result:
                name, sell, buy = result
                if "عيار" in name:
                    data[name] = {"buy": str(buy), "sell": str(sell)}
                    if "24" in name:
                        gram_24 = sell
                elif "أوقية" in name or "ounce" in name.lower():
                    ounce = sell
                    data["الأوقية العالمية"] = str(ounce)
                elif "USD" in name or "الدولار" in name:
                    data["الدولار الأمريكي"] = str(sell)
        
        # Calculate derived values
        gold_dollar = calculate_gold_dollar(gram_24, ounce)
        if gold_dollar:
            data["دولار الصاغة"] = f"{gold_dollar:.2f}"
        
        sorted_data = dict(sorted(data.items()))
        page_hash = hashlib.md5(str(sorted_data).encode()).hexdigest()
        return sorted_data, page_hash
    
    def _parse_item(self, item) -> Optional[Tuple[str, Decimal, Decimal]]:
        """Parse a single price item."""
        # Find title
        title_elem = (
            item.find(["span", "h3", "h4", "div", "td"], class_=lambda x: x and any(t in str(x).lower() for t in ["title", "name", "font-medium"]) if x else False)
            or item.find(["span", "h3", "h4", "div", "td"])
        )
        if not title_elem:
            return None
        
        name = title_elem.text.strip()
        
        # Find numbers
        nums = item.find_all(["span", "td", "div"], class_=lambda x: x and any(t in str(x).lower() for t in ["number", "price", "value"]) if x else False)
        if len(nums) < 2:
            nums = item.find_all(["span", "td", "div"])
        
        text_nums = [n.text.strip() for n in nums if any(c.isdigit() for c in n.text)]
        if len(text_nums) < 2:
            return None
        
        try:
            sell = parse_decimal(text_nums[0])
            buy = parse_decimal(text_nums[1])
            return name, sell, buy
        except (ValueError, IndexError):
            return None

class FallbackExtractor(PriceExtractor):
    """Extractor for the fallback URL (original implementation)."""
    
    def __init__(self):
        super().__init__(Config.FALLBACK_URL, Config.FALLBACK_TIMEOUT)
    
    def parse(self, html: str) -> Tuple[Dict, str]:
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
                sell = parse_decimal(nums[0].text)
                buy = parse_decimal(nums[1].text)
                data[name] = {"buy": str(buy), "sell": str(sell)}
                if "24" in name:
                    gram_24 = sell
            
            elif "أوقية" in name or "ounce" in name.lower():
                ounce = parse_decimal(nums[0].text)
                data["الأوقية العالمية"] = str(ounce)
            
            elif "USD" in name or "الدولار" in name:
                data["الدولار الأمريكي"] = str(parse_decimal(nums[0].text))
        
        # Calculate derived values
        gold_dollar = calculate_gold_dollar(gram_24, ounce)
        if gold_dollar:
            data["دولار الصاغة"] = f"{gold_dollar:.2f}"
        
        sorted_data = dict(sorted(data.items()))
        page_hash = hashlib.md5(str(sorted_data).encode()).hexdigest()
        return sorted_data, page_hash

# =====================
# TELEGRAM SERVICE
# =====================
class TelegramService:
    """Handles all Telegram bot interactions."""
    
    BASE_URL = "https://api.telegram.org/bot"
    
    def __init__(self, token: str, channel: str):
        self.token = token
        self.channel = channel
        self.url = f"{self.BASE_URL}{token}/sendMessage"
        self.keyboard = {
            "inline_keyboard": [
                [
                    {"text": "🌐 الموقع", "url": "https://andriagold.netlify.app/"},
                    {"text": "📢 القناة", "url": "https://t.me/AndreaGold"}
                ]
            ]
        }
    
    def send(self, message: str, retries: int = 3) -> bool:
        """Send message with retry logic."""
        if not self.token:
            log.error("TELEGRAM_BOT_TOKEN not set")
            return False
        
        for attempt in range(retries):
            try:
                resp = requests.post(
                    self.url,
                    data={
                        "chat_id": self.channel,
                        "text": message,
                        "parse_mode": "HTML",
                        "reply_markup": json.dumps(self.keyboard)
                    },
                    timeout=10
                )
                if resp.status_code == 200:
                    log.info("Message sent successfully")
                    return True
                log.warning(f"Telegram API returned {resp.status_code}: {resp.text}")
            except Exception as e:
                log.error(f"Send attempt {attempt + 1}/{retries} failed: {e}")
            time.sleep(2 ** attempt)
        
        log.error("Failed to send message after all retries")
        return False

# =====================
# MESSAGE FORMATTER
# =====================
class MessageFormatter:
    """Formats messages for Telegram."""
    
    @staticmethod
    def format_update(data: Dict, is_fallback: bool = False) -> str:
        """Format live update message."""
        fallback_notice = "⚠️ <b>[وضع احتياطي]</b>\n" if is_fallback else ""
        msg = f"💎 <b>تحديث لحظي للذهب</b>\n{fallback_notice}\n━━━━━━━━━━━━━━\n"
        
        # Gold karats first (monitored for changes)
        for key, value in data.items():
            if isinstance(value, dict) and "عيار" in key:
                msg += f"🔸 <b>{key}</b>\n🟢 بيع: {value['sell']} | 🔴 شراء: {value['buy']}\n──────────────\n"
        
        # Other data (display only, not monitored)
        msg += "━━━━━━━━━━━━━━\n"
        for key, value in data.items():
            if not isinstance(value, dict) or "عيار" not in key:
                msg += f"📌 {key}: <b>{value}</b>\n"
        
        return msg + "━━━━━━━━━━━━━━\n"
    
    @staticmethod
    def format_close(data: Dict, stats: Dict[str, DailyStats], yesterday: Dict[str, GoldPrice], is_fallback: bool = False) -> str:
        """Format market close message with statistics."""
        fallback_notice = "⚠️ <b>[وضع احتياطي]</b>\n" if is_fallback else ""
        msg = f"🌙 <b>إغلاق سوق الذهب اليوم</b>\n{fallback_notice}\n"
        
        if not data:
            msg += "\n❤️ شكراً لمتابعتكم\n💎 نلقاكم 10 صباحاً"
            return msg
        
        msg += "📊 <b>آخر سعر قبل الإغلاق:</b>\n━━━━━━━━━━━━━━\n"
        
        for key, value in data.items():
            if not isinstance(value, dict):
                msg += f"📌 {key}: <b>{value}</b>\n"
                continue
            
            msg += f"🔸 <b>{key}</b>\n🟢 بيع: {value['sell']} | 🔴 شراء: {value['buy']}\n"
            
            stat = stats.get(key)
            if stat:
                msg += f"📈 أعلى: {stat.high_sell} ({stat.high_time})\n"
                msg += f"📉 أقل: {stat.low_sell} ({stat.low_time})\n"
                
                avg_sell = stat.avg_sell
                if avg_sell is not None:
                    msg += f"📊 متوسط: {avg_sell:.2f}\n"
            
            # Compare with yesterday
            yest = yesterday.get(key)
            if yest:
                current_sell = parse_decimal(value["sell"])
                change = pct_change(current_sell, yest.sell)
                if change is not None:
                    arrow = "⬆️" if change > 0 else "⬇️" if change < 0 else "➖"
                    msg += f"{arrow} مقارنة بأمس: {change:+.2f}%\n"
            
            msg += "──────────────\n"
        
        msg += "━━━━━━━━━━━━━━\n"
        msg += "\n❤️ شكراً لمتابعتكم\n💎 نلقاكم 10 صباحاً"
        return msg

# =====================
# PRICE SERVICE
# =====================
class PriceService:
    """Orchestrates price fetching with fallback logic."""
    
    def __init__(self):
        self.primary = PrimaryExtractor()
        self.fallback = FallbackExtractor()
    
    def get_snapshot(self) -> Optional[Tuple[Dict, str]]:
        """Get snapshot, trying primary first then fallback."""
        # Try primary
        result = self.primary.extract()
        if result:
            if state.using_fallback:
                log.info("✅ PRIMARY is back online! Switching from fallback.")
                state.switch_mode(SourceMode.PRIMARY)
            state.fail_count = 0
            return result
        
        # Primary failed, try fallback
        log.warning("⚠️ PRIMARY failed. Switching to FALLBACK...")
        state.switch_mode(SourceMode.FALLBACK)
        
        result = self.fallback.extract()
        if result:
            state.fail_count = 0
            return result
        
        # Both failed
        state.fail_count += 1
        log.error("❌ Both PRIMARY and FALLBACK failed")
        return None

# =====================
# MAIN LOOP
# =====================
class GoldTracker:
    """Main tracking loop with market hours logic."""
    
    def __init__(self):
        self.telegram = TelegramService(Config.TELEGRAM_BOT_TOKEN, Config.TELEGRAM_CHANNEL)
        self.formatter = MessageFormatter()
        self.price_service = PriceService()
    
    def is_market_open(self) -> bool:
        """Check if market is open (10 AM to midnight Egypt time)."""
        now = datetime.now(EGYPT_TZ)
        return Config.MARKET_OPEN_HOUR <= now.hour < Config.MARKET_CLOSE_HOUR
    
    def get_gold_hash(self, data: Dict) -> str:
        """Generate hash for gold karats only (for change detection)."""
        gold_only = {k: v for k, v in data.items() if isinstance(v, dict) and "عيار" in k}
        return hashlib.md5(str(dict(sorted(gold_only.items()))).encode()).hexdigest()
    
    def run(self):
        """Main loop."""
        log.info("🚀 Gold Tracker started")
        
        while True:
            try:
                now = datetime.now(EGYPT_TZ)
                log.info(f"Hour: {now.hour} | Mode: {state.source_mode.name} | Fails: {state.fail_count}")
                
                if self.is_market_open():
                    self._handle_market_open()
                else:
                    self._handle_market_close()
                
            except Exception as e:
                log.error(f"Loop error: {e}", exc_info=True)
                time.sleep(5)
    
    def _handle_market_open(self):
        """Handle market open hours."""
        state.sent_close_msg = False
        
        if not state.sent_open_msg:
            self._send_opening_message()
            return
        
        # Regular monitoring
        result = self.price_service.get_snapshot()
        if not result:
            time.sleep(10)
            return
        
        data, page_hash = result
        state.update_stats(data)
        
        if state.last_hash is None:
            self._send_update(data, page_hash)
            return
        
        # Check for gold price changes only
        current_gold_hash = self.get_gold_hash(data)
        last_gold_hash = self.get_gold_hash(state.last_data) if state.last_data else None
        
        if current_gold_hash != last_gold_hash:
            self._send_update(data, page_hash)
        
        time.sleep(Config.LOOP_INTERVAL)
    
    def _handle_market_close(self):
        """Handle market close hours."""
        if not state.sent_close_msg:
            if state.last_data:
                state.set_yesterday_close(state.last_data)
            
            msg = self.formatter.format_close(
                state.last_data,
                state.daily_stats,
                state.yesterday_close,
                state.using_fallback
            )
            self.telegram.send(msg)
            state.sent_close_msg = True
        
        state.sent_open_msg = False
        time.sleep(60)
    
    def _send_opening_message(self):
        """Send opening message and initialize stats."""
        log.info("Market opened → sending opening message")
        result = self.price_service.get_snapshot()
        
        if result:
            data, page_hash = result
            state.reset_stats()
            state.update_stats(data)
            self._send_update(data, page_hash, force=True)
            state.sent_open_msg = True
        else:
            log.warning("No data at market open, retrying in 30s")
            time.sleep(30)
    
    def _send_update(self, data: Dict, page_hash: str, force: bool = False):
        """Send update message and update state."""
        msg = self.formatter.format_update(data, state.using_fallback)
        if self.telegram.send(msg) or force:
            state.last_hash = page_hash
            state.last_data = data

# =====================
# API ROUTES
# =====================
@app.route("/api")
def api():
    key = request.args.get("key")
    if key != Config.API_KEY:
        return jsonify({"error": "unauthorized"}), 403
    
    result = price_service.get_snapshot()
    if result:
        return jsonify(result[0])
    return jsonify({"error": "service unavailable"}), 503

@app.route("/health")
def health():
    return jsonify(state.get_health())

@app.route("/")
def home():
    return f"💎 Live Gold System Running Secure<br>Mode: <b>{state.source_mode.name}</b>"

# =====================
# GLOBALS FOR ROUTES
# =====================
price_service = PriceService()

# =====================
# STARTUP
# =====================
if __name__ == "__main__":
    tracker = GoldTracker()
    Thread(target=tracker.run, daemon=True).start()
    app.run(host="0.0.0.0", port=Config.PORT)
