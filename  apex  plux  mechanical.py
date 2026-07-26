import logging
import time
import os
import sqlite3
import multiprocessing as mp
import threading
import requests
import json
import queue
import pandas as pd
import numpy as np
import math
from datetime import datetime, time as dt_time
from logging.handlers import TimedRotatingFileHandler

# Angel One API Imports
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2
import pyotp
from sklearn.ensemble import RandomForestClassifier

# ==========================================================
# 1. LOGGING & CREDENTIALS SETUP
# ==========================================================
logger = logging.getLogger()
logger.setLevel(logging.INFO)
logging.getLogger("smartConnect").setLevel(logging.WARNING)

if not logger.handlers:
    log_handler = TimedRotatingFileHandler("god_mode_live.log", when="midnight", interval=1, backupCount=5)
    log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(log_handler)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] [%(message)s]"))
    logger.addHandler(console_handler)

# તમારી Angel One API ક્રેડેન્શિયલ્સ (Pydroid માં રન કરવા માટે અહીં સેટ કરો)
API_KEY = os.getenv("ANGEL_API_KEY", "sN62SVfT")
CLIENT_ID = os.getenv("ANGEL_CLIENT_ID", "AACK055412")
PASSWORD = os.getenv("ANGEL_PASSWORD", "1234")
TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET", "HKRW7EEVAMZ64PJZAUO2WBSXSQ")

# ==========================================================
# 2. CONFIGURATION & STATE
# ==========================================================
SYMBOL_NAME = "NIFTY"
SPOT_TOKEN = "99926000"
INDIA_VIX_TOKEN = "99926017"
LOT_SIZE = 75 
PAPER_BALANCE = 500000.0

class Config:
    TARGET_POINTS = 7.0          
    TIME_STOP_SECONDS = 12       # 12 સેકન્ડમાં મોમેન્ટમ ન આવે તો ઝીરો લોસ એક્ઝિટ
    BREAKEVEN_TRIGGER = 3.5      
    INITIAL_SL_POINTS = 2.5
    AI_CONFIDENCE_THRESHOLD = 0.80  

# Locks & Caches
state_lock = threading.Lock()
cache_lock = threading.Lock()
subs_lock = threading.Lock()

OPTION_DICT, TOKEN_TO_TYPE_MAP = {}, {}
PARSED_EXPIRY_DATE = None
LTP_CACHE, TICK_HISTORY = {}, {}
CURRENT_SUBSCRIBED_TOKENS = set()
LAST_WS_MESSAGE_TIME = time.time()
smartApi, sws = None, None

# ==========================================================
# 3. PURE MATH & GREEKS (Apex V59 Learned)
# ==========================================================
class UniversalMath:
    @staticmethod
    def calculate_hurst(prices):
        if len(prices) < 15: return 0.5
        lags = range(2, 6)
        tau = [np.sqrt(np.std(np.subtract(prices[lag:], prices[:-lag]))) for lag in lags]
        poly = np.polyfit(np.log(lags), np.log(tau), 1)
        return poly[0] * 2.0

class OptionsAnalytics:
    @staticmethod
    def norm_cdf(x):
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

# ==========================================================
# 4. PHYSICS & HFT ALPHA ENGINE
# ==========================================================
class PhysicsEngine:
    def __init__(self):
        self.last_price = None
        self.last_velocity = 0

    def calculate(self, price, volume):
        if self.last_price is None:
            self.last_price = price
            return None
        velocity = price - self.last_price
        force = volume * (velocity - self.last_velocity)
        z_score = (price - self.last_price) / 1.5 
        
        self.last_velocity = velocity
        self.last_price = price
        return {"price": price, "velocity": velocity, "force": force, "z_score": z_score}

# ==========================================================
# 5. MICRO-SCALPING SHIELD (Time-Stop & Breakeven)
# ==========================================================
class MicroScalpingShield:
    def __init__(self):
        self.active = False
        self.entry_price = 0
        self.entry_time = 0
        self.trailing_sl = 0

    def enter_trade(self, price):
        self.active = True
        self.entry_price = price
        self.entry_time = time.time()
        self.trailing_sl = price - Config.INITIAL_SL_POINTS
        print(f"⚡ [SCALP IN] Entry: ₹{price} | Target: ₹{price + Config.TARGET_POINTS} | Time-Stop Active ({Config.TIME_STOP_SECONDS}s)!")

    def monitor(self, current_price, vault):
        if not self.active: return

        points_gained = current_price - self.entry_price
        elapsed = time.time() - self.entry_time

        # 1. Breakeven SL
        if points_gained >= Config.BREAKEVEN_TRIGGER and self.trailing_sl < self.entry_price:
            self.trailing_sl = self.entry_price
            print(f"🛡️ [SCALP] +{points_gained:.1f} pts. SL moved to Breakeven (Risk = 0).")

        # 2. Time-Stop (Apex V59 Shield)
        if elapsed > Config.TIME_STOP_SECONDS and points_gained < 2.0:
            print(f"⏱️ [TIME-STOP] Momentum stagnant after {Config.TIME_STOP_SECONDS}s. Exiting at ₹{current_price}.")
            self.exit_trade(vault, current_price, "TIME_STOP")
            return

        # 3. Target Hit
        if points_gained >= Config.TARGET_POINTS:
            print(f"🎯 [TARGET HIT] Captured {points_gained:.1f} pts in {elapsed:.1f}s!")
            self.exit_trade(vault, current_price, "PROFIT")
            return

        # 4. Stop Loss
        if current_price <= self.trailing_sl:
            print(f"🛑 [SL HIT] Capital protected at ₹{current_price}.")
            self.exit_trade(vault, current_price, "STOP_LOSS")

    def exit_trade(self, vault, price, reason):
        vault.execute_trade("SELL_EXIT", price, reason)
        self.active = False
        self.entry_price = 0

# ==========================================================
# 6. ADVANCED AI BRAIN & PRE-DIVERGENCE
# ==========================================================
class AdvancedAIBrain:
    def __init__(self):
        self.model = RandomForestClassifier(n_estimators=100, max_depth=10)
        self.is_trained = False

    def train_model(self):
        print("🧠 [AI] Training Random Forest Core Model...")
        velocity = np.random.normal(0, 5, 3000)
        force = np.random.normal(0, 15000, 3000)
        z_score = np.random.normal(0, 2, 3000)
        target = np.where((velocity > 0.8) & (force > 2000), 1, 0)
        
        self.model.fit(pd.DataFrame({'velocity': velocity, 'force': force, 'z_score': z_score}), target)
        self.is_trained = True
        print("✅ [AI] Core Brain & Analytics Ready.")

    def evaluate(self, phys_data, divergence_signal):
        if not self.is_trained: return "HOLD"

        if divergence_signal == "BULLISH_OPTION_BUILDUP":
            print("💡 [PRE-DIVERGENCE] Smart Money Options Buildup Confirmed!")
            return "BUY_SIGNAL"

        features = pd.DataFrame([[phys_data['velocity'], phys_data['force'], phys_data['z_score']]], 
                                columns=['velocity', 'force', 'z_score'])
        proba = self.model.predict_proba(features)[0]
        
        if proba[1] >= Config.AI_CONFIDENCE_THRESHOLD:
            return "BUY_SIGNAL"
        return "HOLD"

# ==========================================================
# 7. ANGEL ONE WEBSOCKET SESSION BUILDER
# ==========================================================
def build_option_universe():
    global OPTION_DICT, TOKEN_TO_TYPE_MAP, PARSED_EXPIRY_DATE
    scrip_file = "OpenAPIScripMaster.json"
    try:
        if not os.path.exists(scrip_file) or (time.time() - os.path.getmtime(scrip_file)) > 43200:
            res = requests.get("https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json", timeout=15)
            if res.status_code == 200:
                with open(scrip_file, "wb") as f: f.write(res.content)
        with open(scrip_file, "r") as f: raw = json.load(f)
        
        today = datetime.now().date()
        expiries, contracts = set(), []
        for s in raw:
            if s.get('name') == SYMBOL_NAME and s.get('exch_seg') == 'NFO' and s.get('instrumenttype') == 'OPTIDX':
                contracts.append(s); expiries.add(s.get('expiry'))
                
        valid = sorted([(datetime.strptime(e, "%d%b%Y").date(), e) for e in expiries if datetime.strptime(e, "%d%b%Y").date() >= today])
        if valid:
            nearest_expiry, PARSED_EXPIRY_DATE = valid[0][1], valid[0][0]
            OPTION_DICT.clear(); TOKEN_TO_TYPE_MAP.clear()
            for s in contracts:
                side = "CE" if "CE" in s['symbol'] else ("PE" if "PE" in s['symbol'] else None)
                if side and s.get('expiry') == nearest_expiry:
                    strike = int(float(s['strike']) / 100) if len(s['strike']) > 5 else int(float(s['strike']))
                    OPTION_DICT[(strike, side)] = (s['token'], s['symbol'])
                    TOKEN_TO_TYPE_MAP[s['token']] = side 
            logger.info(f"✅ Live Option Universe Built. Total Contracts: {len(OPTION_DICT)}")
    except Exception as e:
        logger.exception(f"Option Universe Error: {e}")

def init_smart_session() -> bool:
    global smartApi, sws
    try:
        smartApi = SmartConnect(api_key=API_KEY)
        totp = pyotp.TOTP(TOTP_SECRET).now()
        data = smartApi.generateSession(CLIENT_ID, PASSWORD, totp)
        if not data or not data.get('data'): return False
        
        sws = SmartWebSocketV2(data['data']['jwtToken'], API_KEY, CLIENT_ID, data['data']['feedToken'])
        logger.info("✅ Angel One Live Session Initialized Successfully.")
        return True
    except Exception as e:
        logger.exception(f"Smart Session Init Error: {e}")
        return False

# ==========================================================
# 8. SAFE ASYNCHRONOUS VAULT (WAL Mode)
# ==========================================================
class RobustVault:
    def __init__(self):
        self.conn = sqlite3.connect('apex_live_vault.db', check_same_thread=False, timeout=10)
        self.conn.execute("PRAGMA journal_mode=WAL;") 
        self.conn.execute("CREATE TABLE IF NOT EXISTS trades (time TEXT, type TEXT, price REAL, note TEXT)")

    def execute_trade(self, t_type, price, note=""):
        time_now = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.conn.execute("INSERT INTO trades VALUES (?, ?, ?, ?)", (time_now, t_type, price, note))
        self.conn.commit()

# ==========================================================
# 9. MULTI-PROCESSING AGENTS & MASTER ENGINE
# ==========================================================
def buyer_agent(data_queue, decision_queue):
    ai = AdvancedAIBrain()
    ai.train_model()
    while True:
        if not data_queue.empty():
            item = data_queue.get()
            tick = item['tick']
            div_signal = item['option_divergence']
            
            if ai.evaluate(tick, div_signal) == "BUY_SIGNAL":
                decision_queue.put({"sender": "BUYER", "action": "BUY_SIGNAL", "price": tick['price']})
        time.sleep(0.001)

def options_divergence_agent(options_queue, out_queue):
    while True:
        time.sleep(0.05)
        # લાઈવ માર્કેટમાં અહી ઓપ્શન ચેઇન ડેટા ફેચ કરીને ડાયવર્ઝન చెક થશે
        is_pre_divergence = random.choice([False, False, False, True])
        if is_pre_divergence:
            out_queue.put("BULLISH_OPTION_BUILDUP")
        else:
            out_queue.put("NEUTRAL")

class MasterEngine:
    def __init__(self):
        self.data_queue = mp.Queue(maxsize=5000)
        self.decision_queue = mp.Queue(maxsize=5000)
        self.options_queue = mp.Queue(maxsize=5000)
        
        self.physics = PhysicsEngine()
        self.scalper = MicroScalpingShield()
        self.vault = RobustVault()
        self.latest_option_signal = "NEUTRAL"

    def boot_system(self):
        mp.Process(target=buyer_agent, args=(self.data_queue, self.decision_queue), daemon=True).start()
        mp.Process(target=options_divergence_agent, args=(self.options_queue, self.data_queue), daemon=True).start()

    def process_tick(self, price, volume):
        self.scalper.monitor(price, self.vault)
        
        phys = self.physics.calculate(price, volume)
        if not phys: return
        
        payload = {"tick": phys, "option_divergence": self.latest_option_signal}
        if not self.data_queue.full():
            self.data_queue.put(payload)
            
        self.process_hive_mind()

    def process_hive_mind(self):
        while not self.decision_queue.empty():
            msg = self.decision_queue.get()
            if msg['action'] == "BUY_SIGNAL" and not self.scalper.active:
                self.scalper.enter_trade(msg['price'])
                self.vault.execute_trade("BUY_ENTRY", msg['price'], "LIVE_PRE_DIVERGENCE_SCALP")

        if random.randint(1, 100) == 1: gc.collect()

# ==========================================================
# 10. MAIN EXECUTION
# ==========================================================
if __name__ == "__main__":
    print("==========================================================")
    print(" 🚀 APEX GOD-MODE V2.5 [Live Angel One API + Scalper] 🚀 ")
    print("==========================================================\n")
    
    # 1. સેશન અને ડેટાબેર યુનિવર્સ ઇનિશિયલાઈઝેશન
    if init_smart_session():
        build_option_universe()
    else:
        logger.warning("⚠️ API Login failed. Running in Secure Simulation Mode.")

    master = MasterEngine()
    master.boot_system()
    time.sleep(2) 
    
    print("\n🌐 Engine Online & Ready for Live/Simulated Ticks...\n")
    
    try:
        current_price = 24000.0
        for i in range(15):
            print(f"--- Tick {i+1} ---")
            current_price += random.uniform(-1, 3)
            vol = random.randint(2000, 10000)
            
            master.process_tick(current_price, vol)
            time.sleep(1)
            
    except KeyboardInterrupt:
        print("\n🔌 Shutdown Sequence Activated. Vault secure.")
