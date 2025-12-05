import time, hmac, hashlib, base64, json, uuid, math, requests
from dataclasses import dataclass

# ================== TELEGRAM CONFIG ==================
TG_TOKEN = "8585897680:AAEimK1ZpJloMUPJgiDN9In-Ujw34obe0Lk"   # <-- THAY VÀO
TG_CHAT_ID = "5888854189"        # <-- THAY VÀO

def tg_send(msg):
    print("[TELEGRAM]", msg)
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        data = {"chat_id": TG_CHAT_ID, "text": msg}
        requests.post(url, data=data, timeout=5)
    except Exception as e:
        print("Telegram error:", e)


# ================== BITGET CONFIG =====================
API_KEY = "bg_9c5a0c1c2daa44672ce8fd5c6ace99ea"
API_SECRET = "a48dec900fd1f907932befe3a766153f5f4c0b55925c8c69ab3905acca365a9f"
API_PASSPHRASE = "12345678"

BASE_URL = "https://api.bitget.com"

SYMBOL = "SOLUSDT"
INVEST_USDT = 26.0

GRIDS = 3
# GRID SIÊU SÁT GIÁ
RANGE_PCT = 0.01              # ±1% → grid cực sát giá
BUY_DISTANCE_LIMIT = 0.015     # BUY cách giá tối đa 1%
AUTO_RERANGE_TRIGGER = 0.01   # lệch 1% reset ngay

SLEEP_SEC = 5
HEARTBEAT_SEC = 15
LOCALE = "en-US"
# =======================================================


# ================== PROFIT TRACKING ====================
daily_profit = 0.0
hourly_profit = 0.0
minute_profit = 0.0

total_profit = 0.0

daily_buy_count = 0
daily_sell_count = 0
hourly_buy_count = 0
hourly_sell_count = 0
minute_buy_count = 0
minute_sell_count = 0

daily_grid_rounds = 0
hourly_grid_rounds = 0
minute_grid_rounds = 0

last_report_day = None
last_report_hour = None
last_report_time = time.time()

REPORT_INTERVAL = 300   # 5 phút
# =======================================================


@dataclass
class GridLevel:
    price: float
    buy_oid: str = ""
    sell_oid: str = ""


def ts_ms():
    return str(int(time.time() * 1000))


def sign_request(timestamp, method, path, body=""):
    prehash = f"{timestamp}{method.upper()}{path}{body}"
    mac = hmac.new(API_SECRET.encode(), prehash.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()


def make_headers(method, path, body=""):
    t = ts_ms()
    sig = sign_request(t, method, path, body)
    return {
        "ACCESS-KEY": API_KEY,
        "ACCESS-SIGN": sig,
        "ACCESS-TIMESTAMP": t,
        "ACCESS-PASSPHRASE": API_PASSPHRASE,
        "locale": LOCALE,
        "Content-Type": "application/json"
    }


# ============== FIX JSON SAFE PARSING ==================
def get_symbol_config(symbol):
    print("Fetching symbol config…")
    url = BASE_URL + "/api/v2/spot/public/symbols"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    data = r.json()

    cfg = next((x for x in data["data"] if x["symbol"] == symbol), None)
    if not cfg:
        raise RuntimeError("Symbol not found")

    price_scale = int(cfg.get("priceScale") or cfg.get("pricePrecision") or 6)
    qty_scale = int(cfg.get("quantityScale") or cfg.get("quantityPrecision") or 6)

    price_step = float(cfg.get("priceStep") or (10 ** (-price_scale)))
    qty_step = float(cfg.get("quantityStep") or (10 ** (-qty_scale)))

    min_quote = float(cfg.get("minTradeUSDT") or 1.0)
    min_base = float(cfg.get("minTradeSize") or 0.0)

    print("Config loaded:", cfg)

    return {
        "price_scale": price_scale,
        "qty_scale": qty_scale,
        "price_step": price_step,
        "qty_step": qty_step,
        "min_quote": min_quote,
        "min_base": min_base
    }
# =======================================================


def round_step(x, step, scale):
    return round(math.floor(x / step) * step, scale)


def get_last_price(symbol):
    url = f"{BASE_URL}/api/v2/spot/market/tickers?symbol={symbol}"
    r = requests.get(url)
    r.raise_for_status()
    price = float(r.json()["data"][0]["lastPr"])
    print("Price now:", price)
    return price


def place_limit(symbol, side, price, size):
    print(f"Placing {side.upper()} {size} @ {price}")
    tg_send(f"📌 Placing {side.upper()} {size} SOL @ {price}")

    path = "/api/v2/spot/trade/place-order"
    body = json.dumps({
        "symbol": symbol,
        "side": side,
        "orderType": "limit",
        "force": "gtc",
        "price": str(price),
        "size": str(size),
        "clientOid": str(uuid.uuid4())
    })

    r = requests.post(BASE_URL + path, headers=make_headers("POST", path, body), data=body)
    print("Response:", r.text)
    r.raise_for_status()

    oid = r.json()["data"]["orderId"]
    print("Order ID:", oid)
    return oid


def get_order(order_id):
    path = f"/api/v2/spot/trade/orderInfo?orderId={order_id}"
    r = requests.get(BASE_URL + path, headers=make_headers("GET", path))
    r.raise_for_status()
    data = r.json()["data"]
    return data[0] if isinstance(data, list) else data


def cancel_order(order_id):
    print("Cancel order:", order_id)
    try:
        path = "/api/v2/spot/trade/cancel-order"
        body = json.dumps({"orderId": order_id, "symbol": SYMBOL})
        requests.post(BASE_URL + path, headers=make_headers("POST", path, body), data=body)
    except:
        pass


def cancel_all(levels):
    print("Canceling all pending orders…")
    for lv in levels:
        if lv.buy_oid: cancel_order(lv.buy_oid)
        if lv.sell_oid: cancel_order(lv.sell_oid)
    tg_send("⚠️ All pending orders canceled.")


def build_grid(lower, upper, n, cfg):
    step = (upper - lower) / n
    lv = [GridLevel(price=round_step(lower + step * i, cfg["price_step"], cfg["price_scale"])) for i in range(n + 1)]
    print("Grid levels:", [x.price for x in lv])
    return lv


def setup_grid(cfg):
    cur = get_last_price(SYMBOL)

    lower = round_step(cur * (1 - RANGE_PCT), cfg["price_step"], cfg["price_scale"])
    upper = round_step(cur * (1 + RANGE_PCT), cfg["price_step"], cfg["price_scale"])

    tg_send(f"🔄 New Grid: {lower} → {upper}")
    print("New grid:", lower, "→", upper)

    levels = build_grid(lower, upper, GRIDS, cfg)
    buy_levels = [lv for lv in levels if lv.price < cur]

    usdt_each = INVEST_USDT / len(buy_levels)

    # ================= ANTI BUY XA =================
    for lv in buy_levels:

        # Không BUY nếu mức giá cách thị trường quá 1%
        if abs(cur - lv.price) / cur > BUY_DISTANCE_LIMIT:
            print(f"Skip BUY {lv.price}: quá xa giá thị trường ({cur})")
            tg_send(f"⚠️ Skip BUY @ {lv.price}: quá xa giá hiện tại")
            continue

        size = round_step(usdt_each / lv.price, cfg["qty_step"], cfg["qty_scale"])

        if size < cfg["min_base"]:
            print("Skip BUY: size too small")
            tg_send(f"⚠️ Skip BUY @ {lv.price}: size quá nhỏ")
            continue

        lv.buy_oid = place_limit(SYMBOL, "buy", lv.price, size)

    # =================================================

    return levels, lower, upper


def main():
    global daily_profit, hourly_profit, minute_profit
    global daily_buy_count, hourly_buy_count, minute_buy_count
    global daily_sell_count, hourly_sell_count, minute_sell_count
    global daily_grid_rounds, hourly_grid_rounds, minute_grid_rounds
    global total_profit, last_report_day, last_report_hour, last_report_time

    tg_send("🚀 BOT GRID SOLUSDT STARTED")
    print("BOT STARTED")

    cfg = get_symbol_config(SYMBOL)
    levels, lower, upper = setup_grid(cfg)

    last_heartbeat = time.time()

    while True:
        try:
            cur = get_last_price(SYMBOL)

            # ===== AUTO RERANGE =====
            if cur > upper * 1.015 or cur < lower * 0.985:
                tg_send("⚠️ Price left range → Reset Grid")
                cancel_all(levels)
                levels, lower, upper = setup_grid(cfg)
                continue

            # ===== GRID LOGIC =====
            for i, lv in enumerate(levels):

                # BUY filled
                if lv.buy_oid:
                    info = get_order(lv.buy_oid)
                    if info["status"] == "filled":
                        lv.buy_oid = ""
                        amount = float(info["baseVolume"])

                        tg_send(f"🟢 BUY filled @ {lv.price}")
                        print(f"BUY filled @ {lv.price}")

                        # track
                        daily_buy_count += 1
                        hourly_buy_count += 1
                        minute_buy_count += 1

                        # place SELL
                        if i + 1 < len(levels):
                            sell_lv = levels[i + 1]
                            sell_size = round_step(amount, cfg["qty_step"], cfg["qty_scale"])
                            sell_lv.sell_oid = place_limit(SYMBOL, "sell", sell_lv.price, sell_size)

                # SELL filled
                if lv.sell_oid:
                    info = get_order(lv.sell_oid)
                    if info["status"] == "filled":
                        lv.sell_oid = ""
                        quote = float(info["quoteVolume"])

                        tg_send(f"🔴 SELL filled @ {lv.price}")
                        print(f"SELL filled @ {lv.price}")

                        # ====== TÍNH PROFIT ======
                        buy_price = levels[i - 1].price
                        sell_price = lv.price
                        profit = (sell_price - buy_price) * (quote / sell_price)

                        daily_profit += profit
                        hourly_profit += profit
                        minute_profit += profit
                        total_profit += profit

                        daily_sell_count += 1
                        hourly_sell_count += 1
                        minute_sell_count += 1

                        daily_grid_rounds += 1
                        hourly_grid_rounds += 1
                        minute_grid_rounds += 1

                        # Rebuy lower level
                        if i - 1 >= 0:
                            buy_lv = levels[i - 1]
                            size = round_step(quote / buy_lv.price, cfg["qty_step"], cfg["qty_scale"])
                            if size >= cfg["min_base"]:
                                buy_lv.buy_oid = place_limit(SYMBOL, "buy", buy_lv.price, size)

            # ===== HEARTBEAT =====
            if time.time() - last_heartbeat > HEARTBEAT_SEC:
                tg_send(f"❤️ Alive | Price = {cur}")
                last_heartbeat = time.time()

            # ===== DAILY REPORT =====
            current_day = time.strftime("%Y-%m-%d")
            if last_report_day != current_day:
                if last_report_day is not None:
                    tg_send(
                        f"📊 DAILY REPORT\n"
                        f"Ngày: {last_report_day}\n"
                        f"Buy filled: {daily_buy_count}\n"
                        f"Sell filled: {daily_sell_count}\n"
                        f"Grid rounds: {daily_grid_rounds}\n"
                        f"Lợi nhuận hôm nay: {daily_profit:.4f} USDT\n"
                        f"Lũy kế: {total_profit:.4f} USDT"
                    )

                daily_profit = 0.0
                daily_buy_count = 0
                daily_sell_count = 0
                daily_grid_rounds = 0
                last_report_day = current_day

            # ===== HOURLY REPORT =====
            current_hour = time.strftime("%H")
            if last_report_hour != current_hour:
                if last_report_hour is not None:
                    tg_send(
                        f"⏱ HOURLY REPORT\n"
                        f"Giờ: {last_report_hour}:00\n"
                        f"Buy: {hourly_buy_count} | Sell: {hourly_sell_count}\n"
                        f"Grid rounds: {hourly_grid_rounds}\n"
                        f"Lợi nhuận giờ: {hourly_profit:.4f} USDT\n"
                        f"Lũy kế: {total_profit:.4f} USDT"
                    )

                hourly_profit = 0.0
                hourly_buy_count = 0
                hourly_sell_count = 0
                hourly_grid_rounds = 0
                last_report_hour = current_hour

            # ===== 5-MIN REPORT =====
            now = time.time()
            if now - last_report_time >= REPORT_INTERVAL:
                tg_send(
                    f"⏱ 5-MIN REPORT\n"
                    f"Buy: {minute_buy_count} | Sell: {minute_sell_count}\n"
                    f"Grid rounds: {minute_grid_rounds}\n"
                    f"Lợi nhuận 5 phút: {minute_profit:.4f} USDT\n"
                    f"Lũy kế: {total_profit:.4f} USDT"
                )

                minute_profit = 0.0
                minute_buy_count = 0
                minute_sell_count = 0
                minute_grid_rounds = 0
                last_report_time = now

            time.sleep(SLEEP_SEC)

        except Exception as e:
            print("ERROR:", e)
            tg_send(f"⚠️ ERROR: {e}")
            time.sleep(3)


if __name__ == "__main__":
    main()
