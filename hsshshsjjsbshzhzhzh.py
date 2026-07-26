import logging
import time
import os
import sqlite3
import threading
import requests
import json
import queue
import pandas as pd
import numpy as np
import math
from datetime import datetime, time as dt_time, timedelta
from logging.handlers import TimedRotatingFileHandler

# Angel One API
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2
import pyotp

# ==========================================
# 1. LOGGING & TRANSPARENT DEBUG SETUP
# ==========================================
logger = logging.getLogger()
logger.setLevel(logging.INFO)
logging.getLogger("smartConnect").setLevel(logging.WARNING)

if not logger.handlers:
    log_handler = TimedRotatingFileHandler("apex_hunter_v59.log", when="midnight", interval=1, backupCount=5)
    log_handler.setFormatter(logging.Formatter("%(asctime)s [%(threadName)s] [%(levelname)s] %(message)s"))
    logger.addHandler(log_handler)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] [%(message)s]"))
    logger.addHandler(console_handler)

API_KEY = os.getenv("ANGEL_API_KEY", "sN62SVfT")
CLIENT_ID = os.getenv("ANGEL_CLIENT_ID", "AACK055412")
PASSWORD = os.getenv("ANGEL_PASSWORD", "1234")
TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET", "HKRW7EEVAMZ64PJZAUO2WBSXSQ")

# ==========================================
# 2. DYNAMIC ENVIRONMENT VARIABLES
# ==========================================
SYMBOL_NAME = "NIFTY"
SPOT_TOKEN = "99926000"
INDIA_VIX_TOKEN = "99926017"
EXCHANGE = "NSE"
LOT_SIZE = 75 
RISK_FREE_RATE = 0.07

PAPER_BALANCE = 100000.0
ESTIMATED_BROKERAGE_PER_ORDER = 20.0 
DB_FILE = "apex_hunter_v59.db"

state_lock = threading.Lock()
cache_lock = threading.Lock()
subs_lock = threading.Lock()

OPTION_DICT, TOKEN_TO_TYPE_MAP = {}, {}
LAST_LOGIN_DATE, PARSED_EXPIRY_DATE = None, None
DAILY_PNL = 0.0

# State Management
IN_POSITION = False
ACTIVE_OPT_TOKEN, ACTIVE_OPT_SYMBOL, ACTIVE_OPT_TYPE = None, None, None
TOTAL_LOTS = 0; REMAINING_QTY = 0
ENTRY_PREMIUM, CURRENT_SL, PEAK_PREMIUM = 0.0, 0.0, 0.0
ACTIVE_TRADE_PNL = 0.0  
LAST_TRADE_TIME = 0.0  

# Caches & Queues
SPOT_TICK_Q, OPT_TICK_Q = [], {}
LTP_CACHE, BID_VOL_CACHE, ASK_VOL_CACHE, VOLUME_CACHE = {}, {}, {}, {}
TICK_HISTORY = {} 
CURRENT_SUBSCRIBED_TOKENS = set()
LAST_WS_MESSAGE_TIME = time.time()
SYSTEM_HEALTH_SCORE = 100.0 
TRADE_HISTORY_LOG = []
smartApi, sws = None, None

# HFT Alpha States
ALPHA_MATRIX = {
    "SMART_MONEY_ACTION": "NONE",
    "BULL_POWER_INDEX": 0.0, 
    "BEAR_POWER_INDEX": 0.0
}
GREEKS_CACHE = {}

# ==========================================
# 3. UNIVERSAL MATHEMATICS & PURE PYTHON GREEKS
# ==========================================
class UniversalMath:
    @staticmethod
    def calculate_z_score(current_value, history_array):
        if len(history_array) < 5: return 0.0
        arr = np.array(history_array)
        std = np.std(arr) + 1e-9
        return (current_value - np.mean(arr)) / std

    @staticmethod
    def calculate_hurst(prices):
        if len(prices) < 15: return 0.5
        lags = range(2, 6)
        tau = [np.sqrt(np.std(np.subtract(prices[lag:], prices[:-lag]))) for lag in lags]
        poly = np.polyfit(np.log(lags), np.log(tau), 1)
        return poly[0] * 2.0 

    @staticmethod
    def dynamic_fractional_kelly(balance, entry_price, health_score):
        wins = [t for t in TRADE_HISTORY_LOG[-20:] if t > 0]
        losses = [abs(t) for t in TRADE_HISTORY_LOG[-20:] if t < 0]
        win_prob = len(wins) / max(1, len(TRADE_HISTORY_LOG[-20:])) if TRADE_HISTORY_LOG else 0.5
        avg_win = sum(wins) / len(wins) if wins else 1.0
        avg_loss = sum(losses) / len(losses) if losses else 1.0
        w_l = avg_win / (avg_loss + 1e-9)
        kelly = (win_prob * w_l - (1.0 - win_prob)) / (w_l + 1e-9)
        
        health_factor = (max(10.0, health_score) / 100.0) ** 2 
        adjusted_kelly = max(0.01, min(kelly * 0.4 * health_factor, 0.15)) # Slightly higher allocation for flexible mode
        lots = int((balance * adjusted_kelly) / (entry_price * LOT_SIZE))
        max_lots = max(1, int((balance * 0.08) / (entry_price * LOT_SIZE)))
        return max(1, min(lots, max_lots))

class OptionsAnalytics:
    @staticmethod
    def norm_cdf(x):
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    @staticmethod
    def norm_pdf(x):
        return math.exp(-0.5 * x**2) / math.sqrt(2.0 * math.pi)

    @classmethod
    def estimate_iv(cls, S, K, T, r, market_price, opt_type):
        sigma = 0.20 
        for _ in range(30):
            d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
            d2 = d1 - sigma * math.sqrt(T)
            price = S * cls.norm_cdf(d1) - K * math.exp(-r * T) * cls.norm_cdf(d2) if opt_type == "CE" else K * math.exp(-r * T) * cls.norm_cdf(-d2) - S * cls.norm_cdf(-d1)
            diff = market_price - price
            if abs(diff) < 1e-4: return sigma
            vega = S * cls.norm_pdf(d1) * math.sqrt(T)
            if vega == 0.0: return sigma
            sigma += diff / vega 
        return sigma

    @classmethod
    def calculate_greeks(cls, S, K, T, r, sigma, opt_type):
        if T <= 0 or sigma <= 0: return {"delta": 0, "theta": 0}
        d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        delta = cls.norm_cdf(d1) if opt_type == "CE" else cls.norm_cdf(d1) - 1.0
        theta = (- (S * cls.norm_pdf(d1) * sigma) / (2 * math.sqrt(T)) - r * K * math.exp(-r * T) * cls.norm_cdf(d2)) / 365.0 if opt_type == "CE" else (- (S * cls.norm_pdf(d1) * sigma) / (2 * math.sqrt(T)) + r * K * math.exp(-r * T) * cls.norm_cdf(-d2)) / 365.0
        return {"delta": round(delta, 3), "theta": round(theta, 2)}

# ==========================================
# 4. DATABASE WORKER THREAD (Safe & Unified)
# ==========================================
db_queue = queue.Queue()

def db_worker_thread():
    try:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=15.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        cursor = conn.cursor()
        
        cursor.execute('''CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, symbol TEXT, type TEXT, lots INTEGER, entry_price REAL, exit_price REAL, net_pnl REAL, balance REAL)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS bot_state (id INTEGER PRIMARY KEY CHECK (id = 1), in_position INTEGER, token TEXT, symbol TEXT, type TEXT, lots INTEGER, remaining_qty INTEGER, entry_premium REAL, current_sl REAL, balance REAL, daily_pnl REAL, health REAL)''')
        conn.commit()

        while True:
            task = db_queue.get()
            if task is None: break
            try: 
                cursor.execute(task[0], task[1])
                conn.commit()
            except Exception as e: 
                logger.exception(f"DB Write Error: {e}")
            db_queue.task_done()
        conn.close()
    except Exception as e:
        logger.exception(f"DB Thread Init Error: {e}")

threading.Thread(target=db_worker_thread, daemon=True).start()

def save_state_to_db():
    sql = '''INSERT OR REPLACE INTO bot_state (id, in_position, token, symbol, type, lots, remaining_qty, entry_premium, current_sl, balance, daily_pnl, health) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'''
    db_queue.put((sql, (int(IN_POSITION), str(ACTIVE_OPT_TOKEN), str(ACTIVE_OPT_SYMBOL), str(ACTIVE_OPT_TYPE), TOTAL_LOTS, REMAINING_QTY, ENTRY_PREMIUM, CURRENT_SL, PAPER_BALANCE, DAILY_PNL, SYSTEM_HEALTH_SCORE)))

def load_state_from_db():
    global IN_POSITION, ACTIVE_OPT_TOKEN, ACTIVE_OPT_SYMBOL, ACTIVE_OPT_TYPE, TOTAL_LOTS, REMAINING_QTY, ENTRY_PREMIUM, CURRENT_SL, PAPER_BALANCE, DAILY_PNL, SYSTEM_HEALTH_SCORE
    try:
        time.sleep(0.5)
        conn = sqlite3.connect(DB_FILE, timeout=5.0)
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM bot_state WHERE id=1")
        row = cursor.fetchone()
        if row:
            if row[1] == 1: 
                with state_lock:
                    IN_POSITION = True
                    ACTIVE_OPT_TOKEN, ACTIVE_OPT_SYMBOL, ACTIVE_OPT_TYPE = row[2], row[3], row[4]
                    TOTAL_LOTS, REMAINING_QTY, ENTRY_PREMIUM, CURRENT_SL = row[5], row[6], row[7], row[8]
            PAPER_BALANCE, DAILY_PNL, SYSTEM_HEALTH_SCORE = row[10], row[11], row[12] if len(row) > 12 else 100.0
        conn.close()
        logger.info("✅ Apex State Loaded Successfully from DB.")
    except Exception as e: logger.exception(f"State Load Error: {e}")

# ==========================================
# 5. DYNAMIC UNIVERSE BUILDER
# ==========================================
def build_option_universe():
    global OPTION_DICT, TOKEN_TO_TYPE_MAP, NEAREST_EXPIRY, PARSED_EXPIRY_DATE
    scrip_file = "OpenAPIScripMaster.json"
    for attempt in range(3):
        try:
            today = datetime.now().date()
            if not os.path.exists(scrip_file) or (datetime.now().timestamp() - os.path.getmtime(scrip_file)) > 43200:
                res = requests.get("https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json", timeout=15)
                if res.status_code == 200:
                    with open(scrip_file, "wb") as f: f.write(res.content)
            with open(scrip_file, "r") as f: raw = json.load(f)
            
            expiries, contracts = set(), []
            for s in raw:
                if s.get('name') == SYMBOL_NAME and s.get('exch_seg') == 'NFO' and s.get('instrumenttype') == 'OPTIDX':
                    contracts.append(s); expiries.add(s.get('expiry'))
                    
            valid = sorted([(datetime.strptime(e, "%d%b%Y").date(), e) for e in expiries if datetime.strptime(e, "%d%b%Y").date() >= today])
            if valid:
                NEAREST_EXPIRY, PARSED_EXPIRY_DATE = valid[0][1], valid[0][0]
                OPTION_DICT.clear(); TOKEN_TO_TYPE_MAP.clear()
                for s in contracts:
                    side = "CE" if "CE" in s['symbol'] else ("PE" if "PE" in s['symbol'] else None)
                    if side and s.get('expiry') == NEAREST_EXPIRY:
                        strike = int(float(s['strike']) / 100) if len(s['strike']) > 5 else int(float(s['strike']))
                        OPTION_DICT[(strike, side)] = (s['token'], s['symbol'])
                        TOKEN_TO_TYPE_MAP[s['token']] = side 
                logger.info(f"✅ Option Universe Built. Total Contracts: {len(OPTION_DICT)}")
                break
        except Exception as e: time.sleep(2 ** attempt)

# ==========================================
# 6. TICK WEBSOCKET & BACKGROUND ALPHA DAEMON
# ==========================================
def init_smart_session() -> bool:
    global smartApi, sws
    try:
        if sws: 
            try: sws.close_connection()
            except: pass
            time.sleep(1)
        smartApi = SmartConnect(api_key=API_KEY)
        totp = pyotp.TOTP(TOTP_SECRET).now()
        data = smartApi.generateSession(CLIENT_ID, PASSWORD, totp)
        if not data: return False
        
        sws = SmartWebSocketV2(data['data']['jwtToken'], API_KEY, CLIENT_ID, data['data']['feedToken'])
        
        def on_data(wsapp, message):
            global LAST_WS_MESSAGE_TIME, SPOT_TICK_Q, OPT_TICK_Q, CURRENT_SL, PEAK_PREMIUM, IN_POSITION
            try:
                LAST_WS_MESSAGE_TIME = time.time()
                if isinstance(message, dict) and 'token' in message:
                    token = message['token']
                    ltp = float(message.get('last_traded_price', 0)) / 100.0 if float(message.get('last_traded_price', 0)) > 10000 else float(message.get('last_traded_price', 0))
                    bid, ask = float(message.get('best_bid_price', 0)) / 100.0, float(message.get('best_ask_price', 0)) / 100.0
                    bid_sz, ask_sz = float(message.get('best_bid_size', 1)), float(message.get('best_ask_size', 1))
                    vol = float(message.get('volume_traded_today', 0))
                    
                    if cache_lock.acquire(timeout=1.0):
                        try:
                            if ltp > 0: 
                                LTP_CACHE[token] = ltp
                                BID_VOL_CACHE[token] = bid_sz; ASK_VOL_CACHE[token] = ask_sz; VOLUME_CACHE[token] = vol
                                
                                curr_time = time.time()
                                if token not in TICK_HISTORY: TICK_HISTORY[token] = []
                                TICK_HISTORY[token].append((curr_time, ltp, bid_sz, ask_sz, vol))
                                TICK_HISTORY[token] = [x for x in TICK_HISTORY[token] if curr_time - x[0] <= 10.0]

                            if token == SPOT_TOKEN and ltp > 0:
                                SPOT_TICK_Q.append(ltp)
                                if len(SPOT_TICK_Q) > 100: SPOT_TICK_Q.pop(0) 
                            elif token in TOKEN_TO_TYPE_MAP and ltp > 0:
                                if token not in OPT_TICK_Q: OPT_TICK_Q[token] = []
                                OPT_TICK_Q[token].append(ltp)
                                if len(OPT_TICK_Q[token]) > 100: OPT_TICK_Q[token].pop(0) 

                            if IN_POSITION and token == ACTIVE_OPT_TOKEN and ltp > 0:
                                if state_lock.acquire(timeout=1.0):
                                    try:
                                        if ltp > PEAK_PREMIUM: PEAK_PREMIUM = ltp
                                        if (ltp - ENTRY_PREMIUM) > (ENTRY_PREMIUM * 0.04):
                                            tick_vol = np.std(OPT_TICK_Q[token][-10:]) if len(OPT_TICK_Q.get(token, [])) >= 10 else 1.0
                                            CURRENT_SL = max(CURRENT_SL, ltp - max(1.5, tick_vol * 2.5))
                                    finally: state_lock.release()
                        finally: cache_lock.release()
            except Exception as e: logger.exception(f"WS Parsing Error: {e}")

        sws.on_data = on_data
        threading.Thread(target=sws.connect, daemon=True).start()
        time.sleep(1.5)
        logger.info("✅ WebSocket Connected Successfully.")
        return True
    except Exception as e: return False

def manage_ws_subs(tokens, action="ADD"):
    global CURRENT_SUBSCRIBED_TOKENS
    if not tokens or not sws: return
    if subs_lock.acquire(timeout=1.0):
        try:
            t_str = {str(t) for t in tokens}
            sub_list = list(t_str - CURRENT_SUBSCRIBED_TOKENS) if action == "ADD" else list(t_str.intersection(CURRENT_SUBSCRIBED_TOKENS))
            if not sub_list: return
            payload = [{"exchangeType": 1, "tokens": [SPOT_TOKEN, INDIA_VIX_TOKEN]}, {"exchangeType": 2, "tokens": [t for t in sub_list if t not in {SPOT_TOKEN, INDIA_VIX_TOKEN}]}]
            if action == "ADD": sws.subscribe("stream_dynamic", 1, payload); CURRENT_SUBSCRIBED_TOKENS.update(t_str)
        finally: subs_lock.release()

def background_alpha_daemon():
    while True:
        try:
            with cache_lock:
                spot_p = LTP_CACHE.get(SPOT_TOKEN, 0)
                if spot_p == 0 or not PARSED_EXPIRY_DATE: 
                    time.sleep(1); continue
                
                atm = int(round(spot_p / 50.0) * 50)
                T_years = max((PARSED_EXPIRY_DATE - datetime.now().date()).days, 0.01) / 365.0
                
                for token, side in [(OPTION_DICT.get((atm, "CE"), (None, None))[0], "CE"), 
                                    (OPTION_DICT.get((atm, "PE"), (None, None))[0], "PE")]:
                    if not token: continue
                    opt_p = LTP_CACHE.get(token, 0)
                    
                    if opt_p > 0:
                        iv = OptionsAnalytics.estimate_iv(spot_p, atm, T_years, RISK_FREE_RATE, opt_p, side)
                        greeks = OptionsAnalytics.calculate_greeks(spot_p, atm, T_years, RISK_FREE_RATE, iv, side)
                        greeks["iv_pct"] = round(iv * 100, 2)
                        GREEKS_CACHE[token] = greeks

                    hist = TICK_HISTORY.get(token, [])
                    spot_hist = TICK_HISTORY.get(SPOT_TOKEN, [])
                    
                    if len(hist) >= 3 and len(spot_hist) >= 3:
                        spot_roc = ((spot_hist[-1][1] - spot_hist[-3][1]) / spot_hist[-3][1]) * 100
                        prem_roc = ((hist[-1][1] - hist[-3][1]) / hist[-3][1]) * 100
                        
                        trap = np.clip((spot_roc - prem_roc) * 200, 0, 100) if (side == "CE" and spot_roc > 0) else \
                               np.clip((abs(spot_roc) - prem_roc) * 200, 0, 100) if (side == "PE" and spot_roc < 0) else 0.0
                        
                        avg_vol = np.mean([hist[i][4] - hist[i-1][4] for i in range(1, len(hist))])
                        curr_vol = hist[-1][4] - hist[-2][4]
                        
                        bid_accel = (hist[-1][2] - hist[-2][2]) - (hist[-2][2] - hist[-3][2])
                        ask_accel = (hist[-1][3] - hist[-2][3]) - (hist[-2][3] - hist[-3][3])
                        
                        action = "NONE"
                        if (bid_accel > 3000 or ask_accel > 3000) and curr_vol < (avg_vol * 0.3): action = "SPOOFING"
                        elif ask_accel > 0 and bid_accel > (hist[-1][2] * 0.05) and curr_vol > avg_vol * 0.8: action = "ABSORPTION_BUY"
                        elif curr_vol > (avg_vol * 1.5) and bid_accel > ask_accel: action = "EXPLOSION"
                        
                        ALPHA_MATRIX["SMART_MONEY_ACTION"] = action
                        
                        power = 0.0
                        if action != "SPOOFING":
                            base_p = (bid_accel - ask_accel) / 1000
                            power = trap + base_p if side == "CE" else trap - base_p
                            if curr_vol > (avg_vol * 1.5): power *= 1.3
                            
                        if side == "PE": ALPHA_MATRIX["BULL_POWER_INDEX"] = round(np.clip(power, -100, 100), 2)
                        else: ALPHA_MATRIX["BEAR_POWER_INDEX"] = round(np.clip(power, -100, 100), 2)
        except Exception as e: pass
        time.sleep(1)

threading.Thread(target=background_alpha_daemon, daemon=True).start()

# ==========================================
# 7. THE OMEGA ENGINE (Main Absolute Loop - Flexible Mode)
# ==========================================
def run_bot():
    global IN_POSITION, ACTIVE_OPT_TOKEN, ACTIVE_OPT_SYMBOL, ACTIVE_OPT_TYPE, TOTAL_LOTS, REMAINING_QTY, ENTRY_PREMIUM, CURRENT_SL, PEAK_PREMIUM
    global DAILY_PNL, PAPER_BALANCE, LAST_TRADE_TIME, LAST_LOGIN_DATE, SYSTEM_HEALTH_SCORE
    global LAST_WS_MESSAGE_TIME, ACTIVE_TRADE_PNL

    now_dt = datetime.now(); curr_time = now_dt.time(); curr_date = now_dt.date()

    if curr_time >= dt_time(15, 30) or curr_time < dt_time(8, 30): return

    if curr_time >= dt_time(8, 45) and LAST_LOGIN_DATE != curr_date:
        DAILY_PNL = 0.0
        SYSTEM_HEALTH_SCORE = min(100.0, SYSTEM_HEALTH_SCORE + 10.0) 
        if cache_lock.acquire(timeout=2.0):
            try: SPOT_TICK_Q.clear(); OPT_TICK_Q.clear(); LTP_CACHE.clear(); TICK_HISTORY.clear()
            finally: cache_lock.release()
        build_option_universe()
        if init_smart_session(): 
            manage_ws_subs([SPOT_TOKEN, INDIA_VIX_TOKEN], "ADD")
            LAST_LOGIN_DATE = curr_date
        return

    if (time.time() - LAST_WS_MESSAGE_TIME) > 30.0 and curr_time > dt_time(9, 15):
        init_smart_session()
        time.sleep(5); return

    if SYSTEM_HEALTH_SCORE < 30.0:
        time.sleep(1800); return

    spot_price = LTP_CACHE.get(SPOT_TOKEN, 0.0)
    vix_price = LTP_CACHE.get(INDIA_VIX_TOKEN, 15.0)
    
    if spot_price <= 0: return

    # --- THE SQUARE-OFF MATRIX ---
    if IN_POSITION:
        curr_p = LTP_CACHE.get(ACTIVE_OPT_TOKEN, ENTRY_PREMIUM)
        if state_lock.acquire(timeout=1.0):
            try:
                time_alive = time.time() - LAST_TRADE_TIME
                momentum_dead = (time_alive > 240 and (curr_p - ENTRY_PREMIUM) <= 0.5)
                
                matrix_reversal = False
                if ACTIVE_OPT_TYPE == "CE" and ALPHA_MATRIX["BEAR_POWER_INDEX"] > 70: matrix_reversal = True
                if ACTIVE_OPT_TYPE == "PE" and ALPHA_MATRIX["BULL_POWER_INDEX"] > 70: matrix_reversal = True

                if (curr_p <= CURRENT_SL or curr_time >= dt_time(15, 15) or momentum_dead or matrix_reversal):
                    net_pnl = ((curr_p - ENTRY_PREMIUM) * REMAINING_QTY) - ESTIMATED_BROKERAGE_PER_ORDER
                    PAPER_BALANCE += net_pnl; DAILY_PNL += net_pnl; ACTIVE_TRADE_PNL = net_pnl 
                    TRADE_HISTORY_LOG.append(ACTIVE_TRADE_PNL)
                    
                    if ACTIVE_TRADE_PNL < 0: SYSTEM_HEALTH_SCORE -= 3.0
                    else: SYSTEM_HEALTH_SCORE = min(100.0, SYSTEM_HEALTH_SCORE + 2.0)

                    db_queue.put(('''INSERT INTO trades (timestamp, symbol, type, lots, entry_price, exit_price, net_pnl, balance) VALUES (?, ?, ?, ?, ?, ?, ?, ?)''', 
                                  (now_dt.strftime("%H:%M:%S"), ACTIVE_OPT_SYMBOL, ACTIVE_OPT_TYPE, TOTAL_LOTS, ENTRY_PREMIUM, curr_p, net_pnl, PAPER_BALANCE)))
                    
                    exit_reason = "Matrix Reversal" if matrix_reversal else ("Momentum Dead" if momentum_dead else "SL/Time Hit")
                    logger.info(f"📉 Trade Closed ({exit_reason}) | PnL: {net_pnl:.2f} | Balance: {PAPER_BALANCE:.2f}")

                    IN_POSITION = False
                    manage_ws_subs([ACTIVE_OPT_TOKEN], "REMOVE")
                    ACTIVE_OPT_TOKEN = None; ACTIVE_TRADE_PNL = 0.0
                    save_state_to_db()
            finally: state_lock.release()
        return

    # --- THE FLEXIBLE ENTRY MATRIX ---
    if (dt_time(9, 15) <= curr_time <= dt_time(9, 20)) or (dt_time(15, 15) <= curr_time): return
    if len(SPOT_TICK_Q) < 15 or (time.time() - LAST_TRADE_TIME) < 30: return # Reduced cooldown to 30s
    if ALPHA_MATRIX["SMART_MONEY_ACTION"] == "SPOOFING": return

    hurst = UniversalMath.calculate_hurst(SPOT_TICK_Q[-15:])
    spot_trend = SPOT_TICK_Q[-1] - SPOT_TICK_Q[-8]
    target_direction = "CE" if spot_trend > 0.15 else ("PE" if spot_trend < -0.15 else None) # More sensitive trend
    
    if not target_direction: return

    # FLEXIBLE THRESHOLD: Lowered power requirement to 45.0 instead of 60/75
    is_valid_entry = False
    req_power = 45.0 if hurst >= 0.45 else 55.0
    
    if target_direction == "CE" and ALPHA_MATRIX["BULL_POWER_INDEX"] >= req_power: is_valid_entry = True
    elif target_direction == "PE" and ALPHA_MATRIX["BEAR_POWER_INDEX"] >= req_power: is_valid_entry = True

    if is_valid_entry:
        atm = int(round(spot_price / 50.0) * 50)
        token, symbol = OPTION_DICT.get((atm, target_direction), (None, None))
        if not token: return
        
        manage_ws_subs([token], "ADD")
        mid = LTP_CACHE.get(token, 0.0)
        greeks = GREEKS_CACHE.get(token, {})
        
        if mid > 0:
            # Relaxed Greeks check (Theta limit increased to 35.0)
            if abs(greeks.get("theta", 0)) > 35.0 and ALPHA_MATRIX["SMART_MONEY_ACTION"] != "EXPLOSION": 
                logger.info("⚠️ Trade Blocked by Greeks Engine: Theta Decay extremely high.")
                return

            lots_to_trade = UniversalMath.dynamic_fractional_kelly(PAPER_BALANCE, mid, SYSTEM_HEALTH_SCORE)

            if state_lock.acquire(timeout=1.0):
                try:
                    IN_POSITION = True
                    ACTIVE_OPT_TOKEN, ACTIVE_OPT_SYMBOL, ACTIVE_OPT_TYPE = token, symbol, target_direction
                    TOTAL_LOTS, REMAINING_QTY, ENTRY_PREMIUM = lots_to_trade, LOT_SIZE * lots_to_trade, mid
                    
                    CURRENT_SL = mid - (mid * (vix_price / 100.0) * 1.2) # Tighter SL buffer for flexible mode
                    PEAK_PREMIUM = mid; LAST_TRADE_TIME = time.time()
                    save_state_to_db()
                    logger.info(f"🚀 EXECUTED {target_direction} | Lots: {lots_to_trade} | Hurst: {hurst:.2f} | Power: {ALPHA_MATRIX['BULL_POWER_INDEX' if target_direction=='CE' else 'BEAR_POWER_INDEX']}")
                finally: state_lock.release()

if __name__ == "__main__":
    load_state_from_db()
    build_option_universe()
    
    if init_smart_session():
        manage_ws_subs([SPOT_TOKEN, INDIA_VIX_TOKEN], "ADD")
        LAST_LOGIN_DATE = datetime.now().date()
        
    logger.info("🌌 APEX-HUNTER V59.4 [Flexible & Active Mode] Fully Initialized & Online.")
    while True:
        try: 
            run_bot()
        except Exception as e: logger.exception(f"Main Loop Error: {e}")
        time.sleep(1.0)
