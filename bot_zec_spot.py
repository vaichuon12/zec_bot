# Ultimate Trading Bot — Smart DIP + Multi-DCA + EMA Trend Filter + Dynamic Trailing + Liquidity Shield
# REQUIREMENTS: pip install ccxt
# IMPORTANT: Replace API_KEY/API_SECRET/PASSPHRASE with NEW keys (DO NOT LEAK).
# Start with dry_run = True to test.

import ccxt
import time
import math
import statistics
import csv
from collections import deque
from datetime import datetime

# ======================
# 🔐 API KEY (ĐIỀN API MỚI)
# ======================
API_KEY     = "bg_aef3f1fd1131d53a300900a583720bfb"
API_SECRET  = "c4d69f7e3122eb858b45c9f2a7a30540e7e84597cae3103d3b189d9556b70da7"
PASSPHRASE  = "12345678"

# ======================
# ⚙️ CONFIG
# ======================
symbol = "ZEC/USDT"
base_asset = symbol.split("/")[0]   # "ZEC"
quote_asset = symbol.split("/")[1]  # "USDT"

total_capital_per_cycle = 16.0      # tổng vốn cho 1 chu kỳ DCA (USD)
dca_levels = [0.005, 0.015, 0.03]   # các mức dip 0.5%, 1.5%, 3% (từ đỉnh)
dca_splits = [0.5, 0.3, 0.2]        # tỷ lệ vốn cho từng level (tổng = 1)
dip_confirmation = 0.001            # rebound 0.1% từ đáy để xác nhận BUY
max_one_position = 1                # chỉ 1 vị thế (1 vòng DCA) cùng lúc

# Trailing sell params (dynamic via ATR)
tsl_profit_min = 0.003              # tối thiểu kích hoạt trailing (0.3%)
tsl_back_default = 0.0015           # default retrace 0.15% (used if ATR not available)

# Volatility / ATR settings (x ticks)
ohlcv_limit = 50                    # dùng để tính EMA/ATR/volatility
ema_period = 50
atr_period = 14

# Liquidity/whale detector
orderbook_depth = 20                # depth to check
liquidity_spike_ratio = 3.0         # nếu imbalance > ratio => skip buy

# Flash crash protection
flash_window = 5                    # ticks
flash_crash_threshold = 0.03        # 3% move inside window => stop buy

# Exchange / timing
check_interval = 1.5
cooldown_after_trade = 4
dry_run = False                      # True để TEST (không gửi order thật)
log_file = "bot_ultimate_log.csv"

# Dynamic cooldown scaling
min_cooldown = 0.5
max_cooldown = 6.0

# ======================
# SETUP EXCHANGE
# ======================
exchange = ccxt.bitget({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "password": PASSPHRASE,
    "enableRateLimit": True,
    "options": {"defaultType": "spot"}
})
exchange.load_markets()

market = exchange.markets.get(symbol)
if not market:
    raise Exception(f"Market {symbol} không tồn tại trên exchange")

# market params
min_amount = None
min_notional = None
base_precision = None
try:
    limits = market.get('limits', {})
    if 'amount' in limits and limits['amount']:
        min_amount = limits['amount'].get('min', None)
    if 'cost' in limits and limits['cost']:
        min_notional = limits['cost'].get('min', None)
    base_precision = market.get('precision', {}).get('amount', None)
except Exception:
    pass

# Helpers
def log(*args):
    t = datetime.utcnow().isoformat()
    line = f"[{t}] " + " ".join(map(str, args))
    print(line)
    # append CSV for record
    try:
        with open(log_file, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([t] + list(map(str, args)))
    except Exception:
        pass

def safe_call(func, retries=5, delay=1):
    for i in range(retries):
        try:
            return func()
        except Exception as e:
            log(f"⚠ Retry {i+1}/{retries} error:", e)
            time.sleep(delay)
    log("❌ API fail after retries")
    return None

def amount_to_precision_safe(sym, amount):
    try:
        return exchange.amount_to_precision(sym, amount)
    except Exception:
        # fallback: round to base_precision
        if base_precision is not None:
            factor = 10 ** base_precision
            return str(math.floor(amount * factor) / factor)
        return str(amount)

def extract_fill_price(order):
    if not order:
        return None
    # ccxt standardized fields
    avg = order.get("average")
    if avg:
        try:
            return float(avg)
        except:
            pass
    # try info/fills
    info = order.get("info") or {}
    fills = order.get("fills") or info.get("fills")
    if fills and isinstance(fills, list) and len(fills) > 0:
        p = fills[0].get("price") or fills[0].get("priceStr")
        try:
            return float(p)
        except:
            pass
    # fallback to price field
    price = order.get("price")
    try:
        return float(price)
    except:
        return None

# Indicators using OHLCV
def fetch_ohlcv(symbol, timeframe='1m', limit=ohlcv_limit):
    data = safe_call(lambda: exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit))
    if not data:
        return None
    # returns list of [ts, open, high, low, close, volume]
    return data

def ema(values, period):
    if not values or period <= 0 or len(values) < period:
        return None
    k = 2 / (period + 1)
    ema_val = sum(values[:period]) / period
    for price in values[period:]:
        ema_val = price * k + ema_val * (1 - k)
    return ema_val

def atr_from_ohlcv(ohlcv, period=atr_period):
    if not ohlcv or len(ohlcv) < period + 1:
        return None
    trs = []
    for i in range(1, len(ohlcv)):
        prev_close = ohlcv[i-1][4]
        high = ohlcv[i][2]
        low = ohlcv[i][3]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    if len(trs) < period:
        return None
    # simple moving average of TRs for ATR
    return sum(trs[-period:]) / period

# Liquidity / orderbook imbalance
def check_liquidity_spike(symbol, depth=orderbook_depth):
    ob = safe_call(lambda: exchange.fetch_order_book(symbol, depth))
    if not ob:
        return False, 0.0
    bids = ob.get("bids") or []
    asks = ob.get("asks") or []
    # sum top N volumes in quote currency approx
    bid_vol = sum([price * size for price, size in bids[:depth]])
    ask_vol = sum([price * size for price, size in asks[:depth]])
    if bid_vol == 0 and ask_vol == 0:
        return False, 0.0
    imbalance = (max(bid_vol, ask_vol) / (min(bid_vol, ask_vol) + 1e-9))
    # if ask liquidity empties (ask_vol small) => possible pump, but we care about big imbalance
    return imbalance >= liquidity_spike_ratio, imbalance

# Notional check
def enough_notional(amount, price):
    if min_notional is None:
        return True
    try:
        return (amount * price) >= float(min_notional)
    except:
        return True

# Dynamic cooldown based on volatility (stddev of close returns)
def dynamic_cooldown(close_prices):
    if not close_prices or len(close_prices) < 6:
        return max_cooldown
    returns = []
    for i in range(1, len(close_prices)):
        returns.append(abs((close_prices[i] - close_prices[i-1]) / close_prices[i-1]))
    vol = statistics.mean(returns[-10:]) if returns else 0.0
    # scale cooldown inversely to volatility
    scaled = max(min_cooldown, min(max_cooldown, (1.0 / (vol*50 + 1e-9)) ))
    return round(scaled, 3)

# STATE
highest_price = None
in_position = False
dca_stage = 0           # 0..len(dca_levels) indicating how many buys made
avg_entry_price = None
position_qty = 0.0
tsl_peak = 0.0
flash_buffer = deque(maxlen=flash_window)
recent_closes = deque(maxlen=ohlcv_limit)

log("BOT ULTIMATE STARTED", "symbol=", symbol, "dry_run=", dry_run)

# main loop
while True:
    try:
        ticker = safe_call(lambda: exchange.fetch_ticker(symbol))
        if not ticker:
            time.sleep(check_interval)
            continue
        price = float(ticker.get("last") or ticker.get("close") or ticker.get("info", {}).get("last"))
        log(f"Price: {price:.8f}")

        # update flash buffer
        flash_buffer.append(price)
        if len(flash_buffer) == flash_window:
            mv = max(flash_buffer)
            mn = min(flash_buffer)
            if mv > 0 and ((mv - mn) / mv) >= flash_crash_threshold:
                log("⛔ FLASH CRASH detected in last window. Skipping buy cycle.")
                time.sleep(check_interval)
                continue

        # fetch ohlcv for indicators
        ohlcv = fetch_ohlcv(symbol, timeframe='1m', limit=ohlcv_limit)
        if ohlcv:
            closes = [c[4] for c in ohlcv]
            recent_closes.extend([c for c in closes[-(ohlcv_limit - len(recent_closes)):]])
            ema_val = ema(closes, ema_period)
            atr_val = atr_from_ohlcv(ohlcv, atr_period)
        else:
            ema_val = None
            atr_val = None

        # update highest_price (peak) when not in position
        if not in_position:
            if highest_price is None:
                highest_price = price
            elif price > highest_price:
                highest_price = price

        drop_from_peak = 0.0
        if highest_price and highest_price > 0:
            drop_from_peak = (highest_price - price) / highest_price

        log(f"Peak: {highest_price:.6f} Drop from peak: {drop_from_peak*100:.4f}% EMA:{ema_val} ATR:{atr_val}")

        # dynamic cooldown
        cooldown_dynamic = dynamic_cooldown(list(recent_closes)) if len(recent_closes) >= 6 else check_interval

        # trend filter: only buy if price >= EMA (i.e., in uptrend) OR EMA unavailable
        trend_ok = True
        if ema_val:
            # allow buy if price is not far below EMA (small buffer)
            trend_ok = price >= ema_val * 0.985  # allow slight dip under EMA (1.5%)
        log("Trend OK:", trend_ok)

        # liquidity check
        liq_spike, imbalance = check_liquidity_spike(symbol)
        if liq_spike:
            log(f"⚠ Liquidity spike detected (imbalance={imbalance:.2f}) — skipping buys this tick")
        # compute ATR-based trailing back threshold
        if atr_val and price > 0:
            tsl_back = max(tsl_back_default, (atr_val / price) * 0.5)  # relative percent
        else:
            tsl_back = tsl_back_default

        # fetch balance safely
        balance = safe_call(lambda: exchange.fetch_balance())
        if not balance:
            time.sleep(check_interval)
            continue
        free = balance.get("free", {})
        usdt_free = float(free.get(quote_asset, 0) or 0)
        base_free = float(free.get(base_asset, 0) or 0)
        log(f"Bal: {quote_asset}={usdt_free} {base_asset}={base_free}")

        # BUY LOGIC: Multi-level DCA with confirmation rebound & filters
        if not in_position and usdt_free >= 5 and not liq_spike and trend_ok:
            # check for each DCA level (we will only start at stage 0)
            for idx, level in enumerate(dca_levels):
                # only try the current stage
                if idx != dca_stage:
                    continue
                # compute required drop threshold from recorded peak
                if drop_from_peak >= level:
                    # we require a small rebound from the local low: price >= (highest_price * (1 - level) + price * dip_confirmation)
                    # simpler: require price to have recovered dip_confirmation fraction from the local minimum
                    # Implementation: require price >= highest_price * (1 - level + dip_confirmation)
                    rebound_threshold = highest_price * (1 - level + dip_confirmation)
                    rebound = price >= rebound_threshold
                    log(f"DCA Stage {idx} triggered: level={level*100:.3f}% drop, rebound check price>={rebound_threshold:.6f} => {rebound}")
                    if rebound:
                        # determine amount for this DCA stage
                        cap_for_stage = total_capital_per_cycle * dca_splits[idx]
                        raw_amount = cap_for_stage / price
                        # precision / min amount checks
                        if base_precision is not None:
                            factor = 10 ** base_precision
                            raw_amount = math.floor(raw_amount * factor) / factor
                        amount_str = amount_to_precision_safe(symbol, raw_amount)
                        try:
                            amount_f = float(amount_str)
                        except:
                            amount_f = raw_amount
                        if min_amount and amount_f < float(min_amount):
                            log(f"⚠ Computed amount {amount_f} < min_amount {min_amount}. Skipping level {idx}")
                            break
                        if not enough_notional(amount_f, price):
                            log(f"⚠ Computed notional {amount_f*price:.6f} < min_notional {min_notional}. Skipping level {idx}")
                            break
                        # place market buy
                        log(f"🔥 EXECUTE BUY stage {idx}: amount={amount_str} price~{price:.6f} (dry_run={dry_run})")
                        if dry_run:
                            order = {"average": price, "fills": [{"price": price}], "info": {"simulated": True}}
                        else:
                            order = safe_call(lambda: exchange.create_market_buy_order(symbol, amount_str))
                        log("order:", order)
                        fill_price = extract_fill_price(order)
                        if fill_price is None:
                            fill_price = price
                        # update position state
                        in_position = True
                        dca_stage = idx + 1
                        # compute weighted average entry
                        position_qty = amount_f + 0.0  # start qty
                        avg_entry_price = fill_price
                        tsl_peak = fill_price
                        log(f"✅ Bought qty={position_qty:.8f} avg_price={avg_entry_price:.6f} stage={dca_stage}")
                        # after first buy, we might allow further DCA buys if deeper dip occurs (in_position True)
                        time.sleep(cooldown_after_trade)
                        break  # break for-loop after executing stage
                # end if stage match
        # If already in position we allow further DCA (add-on buys) if deeper dip occurs
        elif in_position and dca_stage < len(dca_levels) and usdt_free >= 2 and not liq_spike:
            # detect next level relative to peak at time of initial buy (we kept highest_price reset after buy? we should recompute local peak)
            # For simplicity, use highest_price as last known peak before buys (we reset it at initial buy in some implementations)
            next_level = dca_levels[dca_stage]
            if drop_from_peak >= next_level:
                # allow confirmation rebound similarly
                rebound_threshold = highest_price * (1 - next_level + dip_confirmation)
                if price >= rebound_threshold:
                    cap_for_stage = total_capital_per_cycle * dca_splits[dca_stage]
                    raw_amount = cap_for_stage / price
                    if base_precision is not None:
                        factor = 10 ** base_precision
                        raw_amount = math.floor(raw_amount * factor) / factor
                    amount_str = amount_to_precision_safe(symbol, raw_amount)
                    try:
                        amount_f = float(amount_str)
                    except:
                        amount_f = raw_amount
                    if min_amount and amount_f < float(min_amount):
                        log(f"⚠ Add-on amount {amount_f} < min_amount {min_amount} skip")
                    elif not enough_notional(amount_f, price):
                        log(f"⚠ Add-on notional {amount_f*price} < min_notional {min_notional} skip")
                    else:
                        log(f"🔥 ADD-ON BUY stage {dca_stage}: amount={amount_str} price~{price:.6f}")
                        if dry_run:
                            order = {"average": price, "fills": [{"price": price}], "info": {"simulated": True}}
                        else:
                            order = safe_call(lambda: exchange.create_market_buy_order(symbol, amount_str))
                        log("order:", order)
                        fill_price = extract_fill_price(order)
                        if fill_price is None:
                            fill_price = price
                        # update position avg
                        prev_qty = position_qty
                        position_qty += amount_f
                        avg_entry_price = (avg_entry_price * prev_qty + fill_price * amount_f) / position_qty
                        dca_stage += 1
                        tsl_peak = max(tsl_peak, fill_price)
                        log(f"✅ Added qty={amount_f:.8f} new_total_qty={position_qty:.8f} avg_price={avg_entry_price:.6f} stage={dca_stage}")
                        time.sleep(cooldown_after_trade)

        # Trailing Sell logic: dynamic trailing based on ATR or default
        if in_position and position_qty > 0:
            gain = (price - avg_entry_price) / avg_entry_price
            log(f"In position: qty={position_qty:.6f} avg_entry={avg_entry_price:.6f} gain={gain*100:.4f}%")
            # activate trailing when profit > tsl_profit_min
            if gain >= tsl_profit_min:
                # update tsl_peak
                if price > tsl_peak:
                    tsl_peak = price
                retrace = (tsl_peak - price) / tsl_peak if tsl_peak else 0
                log(f"Trailing: peak={tsl_peak:.6f} retrace={retrace*100:.4f}% back_threshold={tsl_back*100:.4f}%")
                if retrace >= tsl_back:
                    # SELL ALL
                    sell_amount_str = amount_to_precision_safe(symbol, position_qty)
                    try:
                        sell_amount_f = float(sell_amount_str)
                    except:
                        sell_amount_f = position_qty
                    if min_amount and sell_amount_f < float(min_amount):
                        log(f"⚠ Sell amount {sell_amount_f} < min_amount {min_amount} — forcing skip")
                    else:
                        log(f"💰 EXECUTE SELL trailing qty={sell_amount_str} (dry_run={dry_run})")
                        if dry_run:
                            order = {"average": price, "fills": [{"price": price}], "info": {"simulated": True}}
                        else:
                            order = safe_call(lambda: exchange.create_market_sell_order(symbol, sell_amount_str))
                        log("order:", order)
                        # Reset state on sold
                        in_position = False
                        position_qty = 0.0
                        avg_entry_price = None
                        dca_stage = 0
                        highest_price = price  # set new peak to current price
                        tsl_peak = 0.0
                        log("✅ SOLD position — reset state")
                        time.sleep(cooldown_after_trade)

        # Sleep dynamic
        time.sleep(cooldown_dynamic if not dry_run else min(cooldown_dynamic, 2.0))

    except Exception as e:
        log("❌ ERROR main loop:", e)
        time.sleep(2)
