import time, hmac, hashlib, base64, json, uuid, math, requests
from dataclasses import dataclass

# ================== CONFIG ==================
API_KEY = "bg_aef3f1fd1131d53a300900a583720bfb"
API_SECRET = "c4d69f7e3122eb858b45c9f2a7a30540e7e84597cae3103d3b189d9556b70da7"
API_PASSPHRASE = "12345678"

BASE_URL = "https://api.bitget.com"

SYMBOL = "ZECUSDT"      # spot symbol Bitget
INVEST_USDT = 16.0

GRIDS = 6              # vốn 16 thì 4-6 aggressive là hợp lý
RANGE_PCT = 0.03       # auto range ±3% quanh giá hiện tại

SLEEP_SEC = 5
HEARTBEAT_SEC = 10     # mỗi 10s in 1 dòng alive
LOCALE = "en-US"
# ============================================


@dataclass
class GridLevel:
    price: float
    buy_oid: str = ""
    sell_oid: str = ""


def ts_ms():
    return str(int(time.time() * 1000))


def sign_request(timestamp, method, request_path, body=""):
    prehash = f"{timestamp}{method.upper()}{request_path}{body}"
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


def get_symbol_config(symbol):
    path = "/api/v2/spot/public/symbols"
    url = BASE_URL + path
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    res = r.json()
    if res.get("code") != "00000":
        raise RuntimeError(res)

    items = res["data"]
    cfg = next((x for x in items if x.get("symbol") == symbol), None)
    if not cfg:
        raise RuntimeError(f"Symbol {symbol} not found in config")

    price_scale = int(cfg.get("priceScale", cfg.get("pricePrecision", 6)))
    qty_scale   = int(cfg.get("quantityScale", cfg.get("quantityPrecision", 6)))

    price_step = float(cfg.get("priceStep", 10 ** (-price_scale)))
    qty_step   = float(cfg.get("quantityStep", 10 ** (-qty_scale)))

    min_quote = float(
        cfg.get("minTradeUSDT")
        or cfg.get("minTradeAmount")
        or cfg.get("minTradeQuote")
        or 1.0
    )
    min_base = float(cfg.get("minTradeSize", cfg.get("minTradeBase", 0.0)) or 0.0)

    return {
        "price_scale": price_scale,
        "qty_scale": qty_scale,
        "price_step": price_step,
        "qty_step": qty_step,
        "min_quote": min_quote,
        "min_base": min_base
    }


def round_step(x, step, scale):
    if step <= 0:
        return round(x, scale)
    return round(math.floor(x / step) * step, scale)


def get_last_price(symbol):
    path = f"/api/v2/spot/market/tickers?symbol={symbol}"
    url = BASE_URL + path
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    res = r.json()
    if res.get("code") != "00000":
        raise RuntimeError(res)
    return float(res["data"][0]["lastPr"])


def place_limit(symbol, side, price, size):
    path = "/api/v2/spot/trade/place-order"
    url = BASE_URL + path
    body_dict = {
        "symbol": symbol,
        "side": side,
        "orderType": "limit",
        "force": "gtc",
        "price": str(price),
        "size": str(size),
        "clientOid": str(uuid.uuid4()),
    }
    body = json.dumps(body_dict)
    r = requests.post(url, headers=make_headers("POST", path, body), data=body, timeout=10)
    r.raise_for_status()
    res = r.json()
    if res.get("code") != "00000":
        raise RuntimeError(res)
    return res["data"]["orderId"]


def get_order(order_id):
    path = f"/api/v2/spot/trade/orderInfo?orderId={order_id}"
    url = BASE_URL + path
    r = requests.get(url, headers=make_headers("GET", path), timeout=10)
    r.raise_for_status()
    res = r.json()
    if res.get("code") != "00000":
        raise RuntimeError(res)

    data = res["data"]
    if isinstance(data, list):
        if not data:
            raise RuntimeError("orderInfo returned empty list")
        data = data[0]
    return data


def build_grid(lower, upper, n, cfg):
    step = (upper - lower) / n
    levels = []
    for i in range(n + 1):
        p = lower + step * i
        p = round_step(p, cfg["price_step"], cfg["price_scale"])
        levels.append(GridLevel(price=p))
    return levels, step


def main():
    cfg = get_symbol_config(SYMBOL)
    print("Symbol config:", cfg)

    last = get_last_price(SYMBOL)
    print("Current price:", last)

    # ===== AUTO RANGE aggressive quanh giá hiện tại =====
    lower = last * (1 - RANGE_PCT)
    upper = last * (1 + RANGE_PCT)
    lower = round_step(lower, cfg["price_step"], cfg["price_scale"])
    upper = round_step(upper, cfg["price_step"], cfg["price_scale"])
    print(f"Auto range: {lower} -> {upper} | grids={GRIDS}")
    # ===================================================

    levels, raw_step = build_grid(lower, upper, GRIDS, cfg)
    print("Grid levels:", [lv.price for lv in levels])

    buy_levels = [lv for lv in levels if lv.price < last]
    if not buy_levels:
        print("Price <= lower, no initial buys. Stop.")
        return

    usdt_per_buy = INVEST_USDT / len(buy_levels)
    print("USDT per buy:", usdt_per_buy)

    for lv in buy_levels:
        size = usdt_per_buy / lv.price
        size = round_step(size, cfg["qty_step"], cfg["qty_scale"])

        if usdt_per_buy < cfg["min_quote"] or (cfg["min_base"] and size < cfg["min_base"]):
            print(f"Skip BUY @ {lv.price}: below min")
            continue

        oid = place_limit(SYMBOL, "buy", lv.price, size)
        lv.buy_oid = oid
        print(f"Placed BUY {size} @ {lv.price}, oid={oid}")

    last_heartbeat = time.time()

    while True:
        try:
            for i, lv in enumerate(levels):

                if lv.buy_oid:
                    info = get_order(lv.buy_oid)
                    if info.get("status") == "filled":
                        filled_base = float(info["baseVolume"])
                        lv.buy_oid = ""

                        if i + 1 < len(levels):
                            sell_lv = levels[i + 1]
                            sell_size = round_step(filled_base, cfg["qty_step"], cfg["qty_scale"])
                            oid = place_limit(SYMBOL, "sell", sell_lv.price, sell_size)
                            sell_lv.sell_oid = oid
                            print(f"BUY filled @ {lv.price} -> SELL {sell_size} @ {sell_lv.price}, oid={oid}")

                if lv.sell_oid:
                    info = get_order(lv.sell_oid)
                    if info.get("status") == "filled":
                        filled_quote = float(info["quoteVolume"])
                        lv.sell_oid = ""

                        if i - 1 >= 0:
                            buy_lv = levels[i - 1]
                            buy_size = filled_quote / buy_lv.price
                            buy_size = round_step(buy_size, cfg["qty_step"], cfg["qty_scale"])

                            if filled_quote < cfg["min_quote"] or (cfg["min_base"] and buy_size < cfg["min_base"]):
                                print(f"Skip reBUY @ {buy_lv.price}: below min")
                                continue

                            oid = place_limit(SYMBOL, "buy", buy_lv.price, buy_size)
                            buy_lv.buy_oid = oid
                            print(f"SELL filled @ {lv.price} -> BUY {buy_size} @ {buy_lv.price}, oid={oid}")

            # heartbeat cho biết bot còn sống
            if time.time() - last_heartbeat >= HEARTBEAT_SEC:
                cur = get_last_price(SYMBOL)
                print(f"[alive] bot running | price now = {cur}")
                last_heartbeat = time.time()

            time.sleep(SLEEP_SEC)

        except Exception as e:
            print("Loop error:", e)
            time.sleep(SLEEP_SEC * 2)


if __name__ == "__main__":
    main()
