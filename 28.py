import logging
import time
import os
import sys
import sqlite3
import random
import threading
import queue
import json
from collections import deque, defaultdict
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from logging.handlers import TimedRotatingFileHandler
import requests

# Angel One API Imports
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2
import pyotp

# ==========================================================
# 0. MICRO-OPTIMIZATIONS & CLOUD TIME UTILITIES 
# ==========================================================
# 🚀 [PRO LEVEL FIX 1] Cloud-Native IST Time Sync
def get_ist_now():
    return datetime.utcnow() + timedelta(hours=5, minutes=30)

def round_to_tick(price, tick_size=0.05):
    if pd.isna(price) or price <= 0: return 0.05
    return round(price / tick_size) * tick_size

def is_market_open():
    now = get_ist_now()
    if now.weekday() >= 5: 
        return False
    start = datetime.strptime("09:14", "%H:%M").time()
    end = datetime.strptime("15:31", "%H:%M").time()
    return start <= now.time() <= end

# ==========================================================
# 1. LOGGING & CREDENTIALS SETUP 
# ==========================================================
logger = logging.getLogger()
logger.setLevel(logging.INFO)

angel_loggers = ['smartConnect', 'smartWebSocketV2', 'SmartWebSocketV2', 'urllib3', 'root']
for log_name in angel_loggers:
    temp_logger = logging.getLogger(log_name)
    temp_logger.setLevel(logging.CRITICAL)
    temp_logger.propagate = False

if not logger.handlers:
    log_handler = TimedRotatingFileHandler("apex_v61.log", when="midnight", interval=1, backupCount=7)
    log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(log_handler)

API_KEY = os.getenv("ANGEL_API_KEY", "sN62SVfT")
CLIENT_ID = os.getenv("ANGEL_CLIENT_ID", "AACK055412")
PASSWORD = os.getenv("ANGEL_PASSWORD", "1234")
TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET", "HKRW7EEVAMZ64PJZAUO2WBSXSQ")

SYMBOL_NAME = "NIFTY"
SPOT_TOKEN = "99926000"
VIX_TOKEN = "26017"

LOT_SIZE = 25      
TOTAL_LOTS = 1     
MAX_TRADES_PER_DAY = 5    
MAX_CONSECUTIVE_LOSS = 3  
MAX_DAILY_DRAWDOWN = -2500.0 * TOTAL_LOTS 

PROFIT_LOCK_THRESHOLD = 4000.0 * TOTAL_LOTS 

OPTION_DICT = {}
smartApi, sws, master = None, None, None 
ce_token, ce_symbol, pe_token, pe_symbol = None, None, None, None
base_atm_strike, current_ce_strike, current_pe_strike = 0, 0, 0
current_strike_type = "ATM"
dte = 0 
NEAREST_EXPIRY_DATE = None

state_lock = threading.Lock()
entry_lock = threading.Lock() 
tick_queue = queue.Queue(maxsize=3000)
order_queue = queue.Queue() 
last_message_timestamp = time.time()
reconnect_attempts = 0 
last_action_msg = "Apex V61.0 [Omni-Adaptive Framework] Initialized..."

# ==========================================================
# 2. LAYER 1: MACRO & FOUNDATION
# ==========================================================
class MacroEnvironmentBrain:
    def __init__(self): 
        self.india_vix = 15.0
        self.morning_end = datetime.strptime("10:30", "%H:%M").time()
        self.midday_end = datetime.strptime("13:00", "%H:%M").time()

    def update_vix(self, current_vix): 
        if 5.0 < current_vix < 50.0: self.india_vix = current_vix
    
    def get_market_context(self):
        now = get_ist_now().time()
        if now < self.morning_end: regime = "MORNING_RUSH"
        elif now < self.midday_end: regime = "MIDDAY_CHOP"
        else: regime = "AFTERNOON_TREND"
        event_status = "HIGH_FEAR" if self.india_vix > 18 else "NORMAL"
        return regime, event_status

class FoundationBrain:
    def __init__(self):
        self.major_support, self.major_resistance = 0, 999999
        self.daily_atr = 100.0 
        self.sup_strength, self.res_strength = "UNKNOWN", "UNKNOWN"
        self.is_ready = False
        self.last_sync_date = None 

    def build_foundation(self, api_session):
        global last_action_msg
        now = get_ist_now()
        if self.last_sync_date == now.date(): return
        
        last_action_msg = "Syncing Daily MTF Foundation..."
        to_date = now.strftime("%Y-%m-%d %H:%M")
        daily_from = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M")
        micro_from = (now - timedelta(days=4)).strftime("%Y-%m-%d %H:%M")
        
        for attempt in range(3):
            try:
                time.sleep(1.0)
                res_daily = api_session.getCandleData({"exchange": "NSE", "symboltoken": SPOT_TOKEN, "interval": "ONE_DAY", "fromdate": daily_from, "todate": to_date})
                if res_daily and res_daily.get('status') and res_daily.get('data'):
                    df_d = pd.DataFrame(res_daily['data'], columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    df_d = df_d[(df_d['high'] > 0) & (df_d['low'] > 0)].copy() 
                    df_d.dropna(inplace=True)
                    if not df_d.empty:
                        df_d['tr'] = df_d['high'] - df_d['low']
                        self.daily_atr = df_d['tr'].rolling(14).mean().iloc[-1]
                        if pd.isna(self.daily_atr) or self.daily_atr <= 0: self.daily_atr = 100.0
                
                time.sleep(1.0)
                res_micro = api_session.getCandleData({"exchange": "NSE", "symboltoken": SPOT_TOKEN, "interval": "FIVE_MINUTE", "fromdate": micro_from, "todate": to_date})
                if res_micro and res_micro.get('status') and res_micro.get('data'):
                    df_m = pd.DataFrame(res_micro['data'], columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    df_m = df_m[(df_m['high'] > 0) & (df_m['low'] > 0)].copy()
                    df_m.dropna(inplace=True)
                    if not df_m.empty:
                        self.major_support = df_m['low'].min()
                        self.major_resistance = df_m['high'].max()
                        
                        avg_vol = df_m['volume'].mean()
                        sup_vol = df_m[df_m['low'] <= self.major_support + 20]['volume'].sum()
                        res_vol = df_m[df_m['high'] >= self.major_resistance - 20]['volume'].sum()
                        self.sup_strength = "STRONG 💪" if sup_vol > avg_vol * 4 else "MODERATE"
                        self.res_strength = "STRONG 🧱" if res_vol > avg_vol * 4 else "MODERATE"
                        
                        self.is_ready = True
                        self.last_sync_date = now.date()
                        last_action_msg = f"Omni-Foundation Synced | ATR: {self.daily_atr:.1f}"
                        return
            except Exception: 
                time.sleep(2)
        last_action_msg = "MTF Failed. Running on Live AI Matrix Only."

    def get_historical_boost(self, current_price):
        if not self.is_ready: return 0
        if "STRONG" in self.sup_strength and abs(current_price - self.major_support) < 15: return 2.0 
        if "STRONG" in self.res_strength and abs(current_price - self.major_resistance) < 15: return -2.0 
        return 0

class TrendBrain:
    def __init__(self): 
        # 🚀 [PRO LEVEL FIX 2] Self-Adaptive Multi-Dimensional EMAs
        self.prices = deque(maxlen=40)
        self.ema5, self.ema9, self.ema13, self.ema21, self.ema34 = None, None, None, None, None
        self.m5, self.m9, self.m13, self.m21, self.m34 = 2/6, 2/10, 2/14, 2/22, 2/35
        
    def reset(self):
        self.prices.clear()
        self.ema5, self.ema9, self.ema13, self.ema21, self.ema34 = None, None, None, None, None

    def get_trend(self, price, cvd, vix):
        if self.ema34 is None:
            self.prices.append(price)
            if len(self.prices) >= 34:
                pl = list(self.prices)
                self.ema5 = sum(pl[-5:]) / 5
                self.ema9 = sum(pl[-9:]) / 9
                self.ema13 = sum(pl[-13:]) / 13
                self.ema21 = sum(pl[-21:]) / 21
                self.ema34 = sum(pl[-34:]) / 34
            return "NEUTRAL"
        
        self.ema5 = (price - self.ema5) * self.m5 + self.ema5
        self.ema9 = (price - self.ema9) * self.m9 + self.ema9
        self.ema13 = (price - self.ema13) * self.m13 + self.ema13
        self.ema21 = (price - self.ema21) * self.m21 + self.ema21
        self.ema34 = (price - self.ema34) * self.m34 + self.ema34
        
        # VIX-based shifting gears
        if vix > 20.0:
            short_ema, long_ema = self.ema5, self.ema13  # Fast Gear for extreme speed
        elif vix < 12.0:
            short_ema, long_ema = self.ema13, self.ema34 # Slow Gear for choppy markets
        else:
            short_ema, long_ema = self.ema9, self.ema21  # Normal Gear
            
        if short_ema > long_ema and cvd >= 0: return "BULLISH"
        elif short_ema < long_ema and cvd <= 0: return "BEARISH"
        return "NEUTRAL"

# ==========================================================
# 3. LAYER 2: DEEP-KARMA AI & PNL TRACKER
# ==========================================================
class DeepKarmaAI:
    def __init__(self): 
        self.weights = {"trend": 25, "obi": 35, "accel": 25, "foundation": 15}
        self.trade_history = deque(maxlen=10)
        self.daily_pnl = 0.0          
        self.highest_pnl_today = 0.0 
        self.trades_today = 0
        self.consecutive_losses = 0   
        self.circuit_breaker = False
        self.circuit_reason = ""
        self.profit_locked = False 
        self.load_brain() 

    def save_brain(self):
        try:
            with open("apex_brain.json", "w") as f: json.dump(self.weights, f)
        except: pass

    def load_brain(self):
        try:
            if os.path.exists("apex_brain.json"):
                with open("apex_brain.json", "r") as f: self.weights = json.load(f)
        except: pass

    def reset_daily_stats(self):
        self.daily_pnl = 0.0
        self.highest_pnl_today = 0.0
        self.trades_today = 0
        self.consecutive_losses = 0
        self.circuit_breaker = False
        self.circuit_reason = ""
        self.profit_locked = False
        self.trade_history.clear()

    def add_trade_result(self, points_gained, entry_context):
        real_points = points_gained - 0.5 
        
        is_win = real_points > 0
        pnl_val = real_points * TOTAL_LOTS * LOT_SIZE
        
        self.daily_pnl += pnl_val
        self.trades_today += 1
        
        if self.daily_pnl > self.highest_pnl_today:
            self.highest_pnl_today = self.daily_pnl
        
        if is_win:
            self.trade_history.append("WIN")
            self.consecutive_losses = 0
            adjustment = 2.0
        else:
            self.trade_history.append("LOSS")
            self.consecutive_losses += 1
            adjustment = -3.0 
            
        locked_profit = 0.0
        if self.highest_pnl_today >= PROFIT_LOCK_THRESHOLD:
            locked_profit = self.highest_pnl_today * 0.5
            
        if locked_profit > 0 and self.daily_pnl <= locked_profit:
            self.circuit_breaker = True
            self.circuit_reason = f"PROFIT LOCK HIT! Secured ₹{locked_profit}"
        elif self.consecutive_losses >= MAX_CONSECUTIVE_LOSS:
            self.circuit_breaker = True
            self.circuit_reason = "MAX CONSECUTIVE LOSS HIT"
        elif self.trades_today >= MAX_TRADES_PER_DAY:
            self.circuit_breaker = True
            self.circuit_reason = "MAX TRADES LIMIT HIT"
        elif self.daily_pnl <= MAX_DAILY_DRAWDOWN:
            self.circuit_breaker = True
            self.circuit_reason = "MAX DRAWDOWN LIMIT HIT! CAPITAL SECURED."
        
        if entry_context and not self.circuit_breaker:
            dominant_factor = max(entry_context, key=entry_context.get)
            self.weights[dominant_factor] += adjustment
            total = sum(self.weights.values())
            if total > 0:
                for k in self.weights:
                    self.weights[k] = max(5, round((self.weights[k] / total) * 100))
            self.save_brain() 
                
    def is_system_confident(self):
        if self.circuit_breaker: return False, self.circuit_reason
        return True, "Confident"

class DynamicRiskUstad:
    def __init__(self, karma_engine): 
        self.tick_variance = deque(maxlen=30)
        self.karma = karma_engine 
    
    def calculate_live_risk(self, price, last_price, gns):
        if last_price: 
            diff = abs(price - last_price)
            if diff > 0.05: self.tick_variance.append(diff)
                
        live_volatility = np.mean(self.tick_variance) if len(self.tick_variance) > 10 else 0.5
        historical_min_sl = max(1.0, gns.get('atr', 100.0) * 0.05) 
        
        base_sl = max(2.5, historical_min_sl, live_volatility * 3.5)
        
        if self.karma.consecutive_losses >= 2:
            base_sl *= 0.8
            
        base_tgt = max(5.0, base_sl * 2.5) 
        
        regime = gns.get('regime', '')
        if "MORNING" in regime: base_sl *= 1.2; base_tgt *= 1.5 
        elif "MIDDAY" in regime: base_sl *= 0.8; base_tgt *= 0.8 
        
        if gns.get('event_status', '') == "HIGH_FEAR": base_sl *= 1.5; base_tgt *= 2.0
        if abs(gns.get('gravity', 0)) > 60: base_tgt *= 1.5 
            
        return round_to_tick(base_sl), round_to_tick(base_tgt)

# ==========================================================
# 4. LAYER 3: INTERLINKED OPERATOR 
# ==========================================================
class VolumeProfileBrain:
    def __init__(self):
        self.volume_at_price = defaultdict(int)
        self.poc_price = 0
        
    def reset(self): 
        self.volume_at_price.clear()
        self.poc_price = 0

    def update_profile(self, price, volume):
        self.volume_at_price[price] += volume
        if self.volume_at_price:
            self.poc_price = max(self.volume_at_price, key=self.volume_at_price.get)

    def check_poc_bounce(self, current_price): 
        if self.poc_price == 0: return False
        return abs(current_price - self.poc_price) <= 2.0

class OperatorBrain:
    def __init__(self): 
        self.cvd = 0
        self.raw_obi = 0.0
        self.obi_ema = 0.0 
        self.momentum_state = "FLAT"
        self.reversal_prob = "LOW"
        self.last_price = None
        
    def reset(self):
        self.cvd = 0
        self.obi_ema = 0.0
        self.last_price = None

    def analyze_order_flow(self, price, delta, best_5_buy, best_5_sell, gns):
        global last_action_msg
        self.cvd += delta
        t_buy = sum([o.get('quantity', 0) for o in best_5_buy]) if best_5_buy else 0
        t_sell = sum([o.get('quantity', 0) for o in best_5_sell]) if best_5_sell else 0
        total_depth = t_buy + t_sell
        
        gns['liquidity_ok'] = True
        if total_depth < 300:
            gns['liquidity_ok'] = False

        spread = 0
        if best_5_buy and best_5_sell:
            bb = best_5_buy[0].get('price', 0)
            bs = best_5_sell[0].get('price', 0)
            if bb > 0 and bs > 0: spread = abs(bs - bb)
        gns['spread'] = spread

        self.raw_obi = ((t_buy - t_sell) / total_depth) * 100 if total_depth > 0 else 0.0
        self.obi_ema = (self.raw_obi * 0.5) + (self.obi_ema * 0.5)
        obi_val = self.obi_ema
        
        thresh = 40 if "MIDDAY" in gns.get('regime', '') else 30
        
        if obi_val > thresh and delta > 0: self.momentum_state = "EXTREME BULLISH 🚀"
        elif obi_val < -thresh and delta < 0: self.momentum_state = "EXTREME BEARISH ☄️"
        elif obi_val > 10: self.momentum_state = "BULLISH BUILDING"
        elif obi_val < -10: self.momentum_state = "BEARISH BUILDING"
        else: self.momentum_state = "NEUTRAL"

        if self.last_price:
            if price > self.last_price and obi_val < -20: self.reversal_prob = "HIGH (Bearish Div) ⚠️"
            elif price < self.last_price and obi_val > 20: self.reversal_prob = "HIGH (Bullish Div) ⚠️"
            else: self.reversal_prob = "LOW"

        self.last_price = price
        
        max_allowed_spread = 3.5 if (abs(obi_val) > 40 or abs(gns.get('gravity', 0)) > 50) else 2.0
        gns['max_spread'] = max_allowed_spread

        is_toxic = False
        if t_sell > (t_buy * 2.5) and total_depth > 1000 and self.cvd < 0: is_toxic = True
        if spread > max_allowed_spread: 
            is_toxic = True
            last_action_msg = f"🛡️ SPREAD TRAP AVOIDED: Spread is {spread:.2f} pts (Max {max_allowed_spread:.1f})"
            
        gns['is_toxic'] = is_toxic
        return is_toxic, obi_val

class ShadowQuantBrain:
    def __init__(self):
        self.last_price = None
        self.last_velocity = 0
        
    def reset(self):
        self.last_price = None
        self.last_velocity = 0
        
    def get_acceleration(self, price):
        if self.last_price is None: self.last_price = price; return 0
        velocity = price - self.last_price
        accel = velocity - self.last_velocity
        self.last_velocity, self.last_price = velocity, price
        return accel

# ==========================================================
# 5. THE GLASS-BOX HUB 
# ==========================================================
class CognitiveNeuralHub:
    def __init__(self, foundation, karma):
        self.foundation = foundation
        self.karma = karma
        self.macro = MacroEnvironmentBrain()
        self.trend = TrendBrain()
        self.risk_ustad = DynamicRiskUstad(karma)
        self.profile = VolumeProfileBrain()
        self.operator = OperatorBrain()
        self.quant = ShadowQuantBrain()
        
        self.last_price = None
        self.conviction_score = 0
        self.market_gravity = 0.0 
        self.resonance_status = "Calibrating AI..."
        self.gns = {} 
        self.tick_count = 0 
        self.last_trade_exit_time = 0 
        
        self.safe_start_time = datetime.strptime("09:25", "%H:%M").time()
        self.hard_stop_time = datetime.strptime("15:10", "%H:%M").time()
        self.delayed_start_time = datetime.strptime("09:30", "%H:%M").time()

    def reset_short_term_memory(self):
        self.operator.reset()
        self.quant.reset()
        self.trend.reset()
        self.profile.reset() 
        self.tick_count = 0
        self.resonance_status = "Memory Flushed. Recalibrating..."

    def update_global_state(self, price, volume, best_5_buy, best_5_sell):
        self.tick_count += 1
        regime, event_status = self.macro.get_market_context()
        self.gns.update({
            'regime': regime,
            'event_status': event_status,
            'vix': self.macro.india_vix,
            'atr': self.foundation.daily_atr,
            'sup_strength': self.foundation.sup_strength,
            'res_strength': self.foundation.res_strength,
            'trend': self.trend.get_trend(price, self.operator.cvd, self.macro.india_vix), # Linked VIX to Trend
            'accel': self.quant.get_acceleration(price),
            'at_poc': self.profile.check_poc_bounce(price),
            'price': price
        })
        
        self.profile.update_profile(price, volume)
        delta = volume if (self.last_price and price > self.last_price) else (-volume if self.last_price and price < self.last_price else 0)
        
        is_toxic, smoothed_obi = self.operator.analyze_order_flow(price, delta, best_5_buy, best_5_sell, self.gns)
        
        self.gns['obi'] = smoothed_obi
        self.gns['reversal'] = self.operator.reversal_prob
        
        vix_factor = max(10.0, self.gns.get('vix', 15.0))
        self.market_gravity = (self.gns['obi'] * self.gns['accel']) / (vix_factor * 0.1)
        self.gns['gravity'] = self.market_gravity
        
        return is_toxic
        
    def process_entry_logic(self, price, is_toxic):
        global last_action_msg, dte
        
        is_confident, msg = self.karma.is_system_confident()
        if not is_confident:
            self.resonance_status = f"🛑 {msg}"
            return "HOLD", "NONE", "NONE", 0, 0, {}

        if time.time() - self.last_trade_exit_time < 180:
            self.resonance_status = "POST-TRADE COOL-DOWN (3 Min)"
            return "HOLD", "NONE", "NONE", 0, 0, {}

        if self.macro.india_vix > 25.0:
            self.resonance_status = "VIX EXTREME! CAPITAL PROTECTION HALT"
            return "HOLD", "NONE", "NONE", 0, 0, {}

        now_time = get_ist_now().time()
        effective_stop_time = datetime.strptime("14:15", "%H:%M").time() if dte == 0 else self.hard_stop_time
        
        if now_time >= effective_stop_time:
            self.resonance_status = "0-DTE GAMMA CUTOFF" if dte == 0 else "MARKET CLOSED (No New Entries)"
            return "HOLD", "NONE", "NONE", 0, 0, {}

        target_start_time = self.safe_start_time if abs(self.market_gravity) > 50 else self.delayed_start_time
        if now_time < target_start_time:
            self.resonance_status = f"OPENING COOL-DOWN (Target: {target_start_time.strftime('%H:%M')})"
            return "HOLD", "NONE", "NONE", 0, 0, {}

        if self.tick_count < 50:
            self.resonance_status = f"Calibrating AI Matrix... ({self.tick_count}/50)"
            return "HOLD", "NONE", "NONE", 0, 0, {}

        q_size = tick_queue.qsize()
        if q_size > 50:
            if q_size > 150:
                with tick_queue.mutex: tick_queue.queue.clear()
                self.reset_short_term_memory() 
                self.resonance_status = "DEEP LAG FLUSHED. MEMORY RESET."
            else:
                self.resonance_status = "API LAG DETECTED (Protecting Entry)"
            return "HOLD", "NONE", "NONE", 0, 0, {}
            
        if not self.gns.get('liquidity_ok', True):
            self.resonance_status = "LOW LIQUIDITY AVOIDED"
            return "HOLD", "NONE", "NONE", 0, 0, {}
        
        if self.last_price and self.last_price > 0 and price > 0:
            if abs(price - self.last_price) / self.last_price > 0.15:
                last_action_msg = f"🛡️ GLITCH REJECTED: ₹{self.last_price:.2f} -> ₹{price:.2f}"
                return "HOLD", "NONE", "NONE", 0, 0, {}

        live_sl, live_tgt = self.risk_ustad.calculate_live_risk(price, self.last_price, self.gns)
        self.last_price = price

        if is_toxic or "HIGH" in self.gns['reversal']: 
            self.conviction_score = 0
            self.resonance_status = "Disconnected/Toxic"
            return "HOLD", "NONE", "NONE", 0, 0, {}

        ce_score, pe_score = 0, 0
        w = self.karma.weights 
        current_context = {"trend": 0, "obi": 0, "accel": 0, "foundation": 0}

        if self.gns['trend'] == "BULLISH": ce_score += w["trend"]; current_context["trend"] = w["trend"]
        elif self.gns['trend'] == "BEARISH": pe_score += w["trend"]; current_context["trend"] = w["trend"]

        dynamic_obi_req = 20.0 * max(1.0, self.macro.india_vix / 15.0)
        self.gns['dynamic_obi_req'] = dynamic_obi_req

        if self.gns['obi'] > dynamic_obi_req: ce_score += w["obi"]; current_context["obi"] = w["obi"]
        elif self.gns['obi'] < -dynamic_obi_req: pe_score += w["obi"]; current_context["obi"] = w["obi"]

        accel_threshold = max(0.5, price * 0.01)
        self.gns['accel_threshold'] = accel_threshold

        if self.gns['accel'] > accel_threshold: ce_score += w["accel"]; current_context["accel"] = w["accel"]
        elif self.gns['accel'] < -accel_threshold: pe_score += w["accel"]; current_context["accel"] = w["accel"]

        f_boost = self.foundation.get_historical_boost(price)
        if self.gns['at_poc'] or f_boost > 0: ce_score += w["foundation"]; current_context["foundation"] = w["foundation"]
        if self.gns['at_poc'] or f_boost < 0: pe_score += w["foundation"]; current_context["foundation"] = w["foundation"]

        if self.market_gravity > 50: ce_score = 100; current_context["obi"] = 100
        elif self.market_gravity < -50: pe_score = 100; current_context["obi"] = 100

        if self.gns['trend'] == "BULLISH" and self.gns['obi'] > (dynamic_obi_req * 1.5) and self.gns['accel'] > accel_threshold and f_boost >= 0:
            self.resonance_status = "CE RESONANCE (GOD-MODE) ✨"
            ce_score = 100
        elif self.gns['trend'] == "BEARISH" and self.gns['obi'] < -(dynamic_obi_req * 1.5) and self.gns['accel'] < -accel_threshold and f_boost <= 0:
            self.resonance_status = "PE RESONANCE (GOD-MODE) ✨"
            pe_score = 100
        else:
            self.resonance_status = "Analyzing Nodes..."

        buffer_pts = 1.5 if abs(self.market_gravity) > 50 else 0.5

        if ce_score > pe_score:
            self.conviction_score = min(100, ce_score)
            if self.conviction_score >= 75:
                if self.foundation.major_resistance - price < 15 and self.market_gravity < 50: 
                    return "HOLD", "NONE", "NONE", 0, 0, {}
                order_type = "SMART_LIMIT" if self.conviction_score >= 85 else "LIMIT_ORDER"
                return "BUY_SIGNAL", "CE", order_type, live_sl, live_tgt, current_context
        else:
            self.conviction_score = min(100, pe_score)
            if self.conviction_score >= 75:
                if price - self.foundation.major_support < 15 and self.market_gravity > -50: 
                    return "HOLD", "NONE", "NONE", 0, 0, {}
                order_type = "SMART_LIMIT" if self.conviction_score >= 85 else "LIMIT_ORDER"
                return "BUY_SIGNAL", "PE", order_type, live_sl, live_tgt, current_context
            
        self.conviction_score = max(ce_score, pe_score) 
        return "HOLD", "NONE", "NONE", 0, 0, {}

# ==========================================================
# 6. OMNI-LINKED LIVING SHIELD
# ==========================================================
class AdaptiveLivingShield:
    def __init__(self, hub, karma_engine):
        self.active = False
        self.entry_price = 0
        self.entry_timestamp = 0 
        self.hub = hub
        self.karma = karma_engine
        self.active_side = "CE"
        self.entry_context = {}
        self.square_off_time = datetime.strptime("15:15", "%H:%M").time()
        self.ticks_in_trade = 0 
        self.sl_warnings = 0 

    def enter_trade(self, price, symbol, side, order_type, sl, tgt, context):
        global last_action_msg
        self.active = True
        self.active_side = side
        self.ticks_in_trade = 0
        self.sl_warnings = 0
        self.entry_timestamp = time.time()
        
        buffer_val = 1.5 if abs(self.hub.market_gravity) > 50 else 0.5
        if order_type == "SMART_LIMIT": self.entry_price = round_to_tick(price + buffer_val) 
        else: self.entry_price = round_to_tick(price - 0.5) 
            
        self.dynamic_tgt = round_to_tick(tgt)
        self.trailing_sl = round_to_tick(self.entry_price - sl)
        self.entry_context = context 
        last_action_msg = f"💥 [ENTERED {side}] @ ₹{self.entry_price:.2f} [{order_type}]"

    def monitor_and_adapt(self, current_price):
        global last_action_msg
        if not self.active: return
        self.ticks_in_trade += 1
        
        if current_price < (self.entry_price * 0.5): return 
        
        if get_ist_now().time() >= self.square_off_time:
            points = round_to_tick(current_price - self.entry_price)
            last_action_msg = f"⏰ [AUTO SQUARE-OFF] Forced Exit at 3:15 PM!"
            self.exit_trade(current_price, "TIME_SQUARE_OFF", points)
            return

        points_gained = current_price - self.entry_price
        gns = self.hub.gns 
        time_in_trade = time.time() - self.entry_timestamp 
        
        if (self.ticks_in_trade > 150 or time_in_trade > 180) and points_gained <= 1.0:
            last_action_msg = f"⏳ [THETA DECAY GUARD] Forced Exit due to no momentum."
            self.exit_trade(current_price, "TIME_STOP", points_gained)
            return
        
        vix_multiplier = max(1.0, self.hub.macro.india_vix / 15.0)

        if "HIGH" in gns.get('reversal', '') and points_gained > 1.0:
            if self.active_side == "CE" and "STRONG" in gns.get('sup_strength', '') and (current_price - self.hub.foundation.major_support < 20):
                pass 
            else:
                new_sl = round_to_tick(current_price - (0.5 * vix_multiplier))
                if new_sl > self.trailing_sl:
                    self.trailing_sl = new_sl
                    last_action_msg = f"⚠️ REVERSAL PREDICTED! SL Choked to: ₹{self.trailing_sl:.2f}"

        elif points_gained >= self.dynamic_tgt or abs(gns.get('gravity', 0)) > 40:
            obi_strength = abs(gns.get('obi', 0))
            
            if obi_strength > 50:
                buffer = 2.0 * vix_multiplier 
            elif points_gained > 10.0 * vix_multiplier:
                buffer = 0.5 * vix_multiplier
            elif points_gained > 5.0 * vix_multiplier:
                buffer = 1.0 * vix_multiplier
            else:
                buffer = (2.0 if obi_strength > 40 else 1.5) * vix_multiplier
                
            new_sl = round_to_tick(current_price - buffer) 
            if new_sl > self.trailing_sl:
                self.trailing_sl = new_sl
                last_action_msg = f"🔥 Parabolic Shield Active! Trailing SL: ₹{self.trailing_sl:.2f}"

        elif points_gained >= 3.0 * vix_multiplier and self.trailing_sl < self.entry_price:
            self.trailing_sl = self.entry_price
            last_action_msg = "✅ Breakeven reached. Risk is ZERO."

        if current_price <= self.trailing_sl:
            if current_price < self.trailing_sl - 2.0:
                points = round_to_tick(current_price - self.entry_price)
                last_action_msg = f"🛑 [FLASH CRASH DETECTED] Panic Exit at: ₹{current_price:.2f}"
                self.exit_trade(current_price, "STOP_LOSS_FLASH", points)
            else:
                self.sl_warnings += 1
                if self.sl_warnings >= 2:
                    points = round_to_tick(current_price - self.entry_price)
                    last_action_msg = f"🛑 [STOP_LOSS HIT] Exit at: ₹{current_price:.2f}"
                    self.exit_trade(current_price, "STOP_LOSS", points)
        else:
            self.sl_warnings = 0 

    def exit_trade(self, price, reason, points):
        order_queue.put(("SELL_EXIT", price, reason)) 
        self.active = False
        self.hub.last_trade_exit_time = time.time() 
        self.karma.add_trade_result(points, self.entry_context)
        logger.info(last_action_msg)

# ==========================================================
# 7. ASYNCHRONOUS EXECUTION ENGINE
# ==========================================================
class RobustVault:
    def __init__(self, api_session):
        self.api = api_session
        self.db_queue = queue.Queue() 
        self.db_thread = threading.Thread(target=self._db_worker, daemon=True)
        self.db_thread.start()
        
    def _db_worker(self):
        conn = sqlite3.connect('apex_live_vault.db', check_same_thread=False, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;") 
        conn.execute("CREATE TABLE IF NOT EXISTS trades (time TEXT, type TEXT, price REAL, note TEXT)")
        while True:
            t_type, price, note = self.db_queue.get()
            try:
                conn.execute("INSERT INTO trades VALUES (?, ?, ?, ?)", (get_ist_now().strftime("%H:%M:%S"), t_type, price, note))
                conn.commit()
            except Exception: pass
            self.db_queue.task_done()
            
    def execute_trade(self, t_type, price, note=""):
        self.db_queue.put((t_type, price, note))

class ExecutionEngine:
    def __init__(self, api_session):
        self.api = api_session
        self.foundation = FoundationBrain()
        self.karma = DeepKarmaAI() 
        self.hub = CognitiveNeuralHub(self.foundation, self.karma)
        self.shield = AdaptiveLivingShield(self.hub, self.karma)
        self.vault = RobustVault(api_session)
        
        self.last_reboot_date = None 
        self.last_order_time = 0 
        self.last_render_time = 0 
        self.last_tick_fingerprint = "" 
        
        os.system('cls' if os.name == 'nt' else 'clear')
        
        self.worker_thread = threading.Thread(target=self._process_queue, daemon=True)
        self.worker_thread.start()
        
        self.order_thread = threading.Thread(target=self._order_worker, daemon=True) 
        self.order_thread.start()
        
        self.strike_thread = threading.Thread(target=self._dynamic_strike_watcher, daemon=True)
        self.strike_thread.start()

    def process_vix(self, vix_price):
        with state_lock: self.hub.macro.update_vix(vix_price)

    def queue_tick(self, token, price, volume, best_5_buy, best_5_sell):
        if price <= 0 or volume < 0:
            return 

        current_fingerprint = f"{token}_{price}_{volume}"
        if current_fingerprint == self.last_tick_fingerprint:
            return
        self.last_tick_fingerprint = current_fingerprint
        
        try:
            if not tick_queue.full(): tick_queue.put_nowait((token, price, volume, best_5_buy, best_5_sell))
        except: pass

    def _order_worker(self):
        while True:
            try: 
                t_type, price, note = order_queue.get()
                
                time_since_last_order = time.time() - self.last_order_time
                if time_since_last_order < 3.0:
                    time.sleep(3.0 - time_since_last_order)
                
                self.vault.execute_trade(t_type, price, note)
                self.last_order_time = time.time()
                
                order_queue.task_done()
            except Exception as e:
                logger.error(f"Order Worker Error: {e}")

    def _dynamic_strike_watcher(self):
        global base_atm_strike, current_ce_strike, current_pe_strike, current_strike_type, ce_token, ce_symbol, pe_token, pe_symbol, last_action_msg, dte, NEAREST_EXPIRY_DATE
        while True:
            time.sleep(15)
            
            if not is_market_open():
                continue

            try: 
                now = get_ist_now()
                now_time = now.time()
                trigger_time = datetime.strptime("09:00", "%H:%M").time()
                
                if self.last_reboot_date != now.date() and now_time >= trigger_time:
                    self.last_reboot_date = now.date()
                    last_action_msg = "🔄 Daily Reboot: Re-Logging API & Purging Old Memory..."
                    
                    if sws and getattr(sws, 'ws', None) is not None:
                        try: sws.close_connection()
                        except: pass
                    
                    time.sleep(5) 
                    
                    if init_smart_session():
                        build_option_universe() 
                        self.foundation.build_foundation(smartApi)
                        self.hub.reset_short_term_memory() 
                        self.karma.reset_daily_stats()

                if self.shield.active: continue 

                res = smartApi.ltpData("NSE", "Nifty 50", SPOT_TOKEN)
                if res and res.get('status') and res.get('data'):
                    lpt = res['data']['ltp']
                    
                    if base_atm_strike == 0:
                        new_atm = int(round(lpt / 50.0) * 50)
                    else:
                        if lpt > base_atm_strike + 35: new_atm = base_atm_strike + 50
                        elif lpt < base_atm_strike - 35: new_atm = base_atm_strike - 50
                        else: new_atm = base_atm_strike
                    
                    dte = max(0, (NEAREST_EXPIRY_DATE - get_ist_now().date()).days) if NEAREST_EXPIRY_DATE else 3
                    vix = self.hub.macro.india_vix
                    
                    offset = 0
                    if dte <= 1:
                        offset = 2 if vix > 20.0 else 1 
                    else:
                        offset = 1 if vix > 22.0 else 0
                        
                    strike_type = "DEEP ITM" if offset == 2 else ("ITM" if offset == 1 else "ATM")
                    
                    new_ce = new_atm - (offset * 50)
                    new_pe = new_atm + (offset * 50)

                    with state_lock:
                        if new_ce != current_ce_strike or new_pe != current_pe_strike:
                            c_t, c_s = OPTION_DICT.get((new_ce, "CE"), (None, None))
                            p_t, p_s = OPTION_DICT.get((new_pe, "PE"), (None, None))
                            
                            if not c_t or not p_t:
                                c_t, c_s = OPTION_DICT.get((new_atm, "CE"), (None, None))
                                p_t, p_s = OPTION_DICT.get((new_atm, "PE"), (None, None))
                                new_ce, new_pe = new_atm, new_atm
                                strike_type = "ATM (Fallback)"

                            if c_t and p_t:
                                if sws and getattr(sws, 'ws', None) is not None:
                                    try: sws.unsubscribe("v61_feed", 3, [{"exchangeType": 2, "tokens": [ce_token, pe_token]}])
                                    except: pass
                                
                                base_atm_strike = new_atm
                                current_ce_strike, current_pe_strike = new_ce, new_pe
                                current_strike_type = strike_type
                                ce_token, ce_symbol = c_t, c_s
                                pe_token, pe_symbol = p_t, p_s
                                last_action_msg = f"🔄 KINEMATIC STRIKE: CE {new_ce} | PE {new_pe} [{strike_type}]"
                                
                                if sws and getattr(sws, 'ws', None) is not None:
                                    try: sws.subscribe("v61_feed", 3, [{"exchangeType": 1, "tokens": [VIX_TOKEN]}, {"exchangeType": 2, "tokens": [ce_token, pe_token]}])
                                    except: pass
            except Exception: pass

    def render_dashboard(self, price):
        current_time = time.time()
        if current_time - self.last_render_time < 5.0:
            return
        self.last_render_time = current_time

        os.system('cls' if os.name == 'nt' else 'clear')
        
        gns = self.hub.gns
        obi = gns.get('obi', 0.0)
        score = self.hub.conviction_score
        w = self.karma.weights
        
        c_warm = "✅"
        now_time = get_ist_now().time()
        
        target_start_time = self.hub.safe_start_time if abs(self.hub.market_gravity) > 50 else self.hub.delayed_start_time
        
        if now_time < target_start_time: c_warm = "⏳"
        elif self.hub.tick_count < 50: c_warm = "⏳"
        elif not gns.get('liquidity_ok', True): c_warm = "🛑"
        
        t_stat = "✅" if gns.get('trend') in ["BULLISH", "BEARISH"] else "❌"
        
        req_obi = gns.get('dynamic_obi_req', 20.0)
        o_stat = "✅" if abs(obi) >= req_obi else "⏳"
        
        accel_th = gns.get('accel_threshold', 0.8)
        a_stat = "✅" if abs(gns.get('accel', 0)) >= accel_th else "⏳"
        f_stat = "✅" if (gns.get('at_poc') or self.foundation.get_historical_boost(price) != 0) else "⏳"
        s_stat = "❌" if gns.get('is_toxic') else "✅"
        
        if self.hub.macro.india_vix > 25.0:
            time_status = "VIX EXTREME HALT"
            c_warm = "🛑"
        elif time.time() - self.hub.last_trade_exit_time < 180:
            time_status = "POST-TRADE COOL-DOWN (3 Min)"
            c_warm = "⏳"
        elif dte == 0 and now_time >= datetime.strptime("14:15", "%H:%M").time():
            time_status = "0-DTE Gamma Cutoff (No New Entries)"
            c_warm = "🛑"
        elif not gns.get('liquidity_ok', True):
            time_status = "LOW LIQUIDITY DETECTED"
        else:
            time_status = f"09:15 to {target_start_time.strftime('%H:%M')} (Opening Cool-down)" if now_time < target_start_time else "Active Trading Phase"
        
        score_icon = "🔥" if score >= 75 else ("⚠️" if score >= 50 else "🧊")
        shield_str = f"🟢 ACTIVE ({self.shield.active_side}) | Trail SL: {self.shield.trailing_sl:.2f}" if self.shield.active else "🔴 OFF (Hunting...)"
        
        pnl_color = "🟩" if self.karma.daily_pnl >= 0 else "🟥"
        breaker_status = f"🔴 {self.karma.circuit_reason}" if self.karma.circuit_breaker else "🟢 SAFE"
        max_sp = gns.get('max_spread', 2.0)
        
        dashboard = f"""
==========================================================
 🌌 APEX GOD-MODE V61.0 [THE OMNI-ADAPTIVE FRAMEWORK] 🌌 
==========================================================
 📡 NIFTY LTP      : ₹ {price:.2f}
 🎯 SMART STRIKE   : CE {current_ce_strike} | PE {current_pe_strike} [{current_strike_type} | DTE: {dte}]
 💰 TODAY'S NET PnL: {pnl_color} ₹ {self.karma.daily_pnl:.2f}  (Trades: {self.karma.trades_today}/{MAX_TRADES_PER_DAY})
 🛑 CIRCUIT BREAKER: {breaker_status}
----------------------------------------------------------
 🚥 LIVE CRITERIA CHECKLIST (AI TARGET: 75% Score) 🚥
 [{c_warm}] TIME & CALIBRATION: {time_status} | Ticks: {min(self.hub.tick_count, 50)}/50
 [{t_stat}] TREND ALIGN       : {gns.get('trend')} (Weight: {w['trend']}%)
 [{o_stat}] OBI MOMENTUM      : {obi:+.1f}% (Needs > ±{req_obi:.1f}% | W: {w['obi']}%)
 [{a_stat}] ACCELERATION      : {gns.get('accel', 0):+.2f} (Needs > ±{accel_th:.2f} | W: {w['accel']}%)
 [{f_stat}] MTF SUPPORT       : Dynamic Nodes / Zones
 [{s_stat}] SPREAD & TOXIC    : Spread: {gns.get('spread', 0.0):.1f} Pts (Max {max_sp:.1f})
----------------------------------------------------------
 🔗 GNS RESONANCE  : {self.hub.resonance_status}
 🪐 MARKET GRAVITY : {gns.get('gravity', 0.0):+.2f} (Omni-Data Linked)
 🧮 AI TOTAL SCORE : {score}% {score_icon}
 🛡️ SHIELD STATUS  : {shield_str}
 ⚡ LIVE LAG (Queue): {tick_queue.qsize()} Ticks Pending
----------------------------------------------------------
 🔔 SYSTEM LOG     : {last_action_msg}
==========================================================
"""
        print(dashboard.strip())

    def _process_queue(self):
        global trading_token, trading_symbol
        while True:
            if not is_market_open():
                try:
                    while not tick_queue.empty():
                        tick_queue.get_nowait()
                        tick_queue.task_done()
                except: pass
                time.sleep(5)
                continue

            try: 
                token, price, volume, b5_buy, b5_sell = tick_queue.get(timeout=1.0)
                with state_lock:
                    is_toxic = self.hub.update_global_state(price, volume, b5_buy, b5_sell)

                    if not self.shield.active:
                        with entry_lock: 
                            if not self.shield.active: 
                                decision, side, order_type, sl, tgt, context = self.hub.process_entry_logic(price, is_toxic)
                                if decision == "BUY_SIGNAL":
                                    if side == "CE" and ce_token: trading_token, trading_symbol = ce_token, ce_symbol
                                    elif side == "PE" and pe_token: trading_token, trading_symbol = pe_token, pe_symbol
                                    self.shield.enter_trade(price, trading_symbol, side, order_type, sl, tgt, context)
                                    order_queue.put(("BUY_ENTRY", price, f"V61.0_{side}")) 
                        self.render_dashboard(price) 
                    else:
                        if token == trading_token:
                            self.shield.monitor_and_adapt(price)
                            self.render_dashboard(price)
                tick_queue.task_done()
            except queue.Empty: 
                if self.shield.active:
                    now_time = get_ist_now().time()
                    if now_time >= self.shield.square_off_time:
                        self.shield.monitor_and_adapt(self.hub.last_price or self.shield.entry_price)
                continue
            except Exception: pass

# ==========================================================
# 8. WEBSOCKET SETUP & IMMORTAL WATCHDOG
# ==========================================================
def build_option_universe():
    global OPTION_DICT, NEAREST_EXPIRY_DATE
    scrip_file = "OpenAPIScripMaster.json"
    
    for attempt in range(3):
        try:
            if not os.path.exists(scrip_file) or (time.time() - os.path.getmtime(scrip_file)) > 43200:
                res = requests.get("https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json", timeout=20)
                if res.status_code == 200:
                    with open(scrip_file, "wb") as f: f.write(res.content)
            
            with open(scrip_file, "r") as f: raw = json.load(f)
            today = get_ist_now().date()
            expiries, contracts = set(), []
            for s in raw:
                if s.get('name') == SYMBOL_NAME and s.get('exch_seg') == 'NFO' and s.get('instrumenttype') == 'OPTIDX':
                    contracts.append(s); expiries.add(s.get('expiry'))
            valid = sorted([(datetime.strptime(e, "%d%b%Y").date(), e) for e in expiries if datetime.strptime(e, "%d%b%Y").date() >= today])
            if valid:
                NEAREST_EXPIRY_DATE = valid[0][0]
                nearest_expiry = valid[0][1]
                OPTION_DICT.clear()
                for s in contracts:
                    side = "CE" if "CE" in s['symbol'] else ("PE" if "PE" in s['symbol'] else None)
                    if side and s.get('expiry') == nearest_expiry:
                        strike = int(float(s['strike']) / 100) if len(s['strike']) > 5 else int(float(s['strike']))
                        OPTION_DICT[(strike, side)] = (s['token'], s['symbol'])
            break 
        except Exception:
            time.sleep(5) 

def init_smart_session() -> bool:
    global smartApi, sws, last_action_msg, reconnect_attempts
    try:
        smartApi = SmartConnect(api_key=API_KEY)
        totp = pyotp.TOTP(TOTP_SECRET).now()
        data = smartApi.generateSession(CLIENT_ID, PASSWORD, totp)
        if not data or not data.get('data'): return False
        sws = SmartWebSocketV2(data['data']['jwtToken'], API_KEY, CLIENT_ID, data['data']['feedToken'])
        reconnect_attempts = 0 
        return True
    except Exception as e:
        last_action_msg = f"⚠️ Session Error: {e}"
        return False

def select_smart_options():
    global ce_token, ce_symbol, pe_token, pe_symbol, trading_token, base_atm_strike, current_ce_strike, current_pe_strike, current_strike_type, last_action_msg, dte
    try:
        res = smartApi.ltpData("NSE", "Nifty 50", SPOT_TOKEN)
        if res and res.get('status') and res.get('data'):
            lpt = res['data']['ltp']
            base_atm_strike = int(round(lpt / 50.0) * 50)
            
            dte = max(0, (NEAREST_EXPIRY_DATE - get_ist_now().date()).days) if NEAREST_EXPIRY_DATE else 3
            vix = 15.0 
            
            offset = 0
            if dte <= 1:
                offset = 2 if vix > 20.0 else 1
            else:
                offset = 1 if vix > 22.0 else 0
                
            current_strike_type = "DEEP ITM" if offset == 2 else ("ITM" if offset == 1 else "ATM")
            
            current_ce_strike = base_atm_strike - (offset * 50)
            current_pe_strike = base_atm_strike + (offset * 50)
            
            ce_token, ce_symbol = OPTION_DICT.get((current_ce_strike, "CE"), (None, None))
            pe_token, pe_symbol = OPTION_DICT.get((current_pe_strike, "PE"), (None, None))
            
            if not ce_token or not pe_token:
                ce_token, ce_symbol = OPTION_DICT.get((base_atm_strike, "CE"), (None, None))
                pe_token, pe_symbol = OPTION_DICT.get((base_atm_strike, "PE"), (None, None))
                current_ce_strike, current_pe_strike = base_atm_strike, base_atm_strike
                current_strike_type = "ATM (Fallback)"

            trading_token = ce_token 
            if ce_token and pe_token:
                last_action_msg = f"✅ Smart Kinematic Strike Locked | CE: {current_ce_strike} | PE: {current_pe_strike}"
                return True
    except Exception: pass
    return False

def on_data(wsapp, message):
    global master, last_message_timestamp
    try:
        last_message_timestamp = time.time()
        token = message.get('token')
        if token == VIX_TOKEN and 'last_traded_price' in message:
            vix_price = float(message['last_traded_price']) / 100.0
            if master: master.process_vix(vix_price)
            
        elif token in [ce_token, pe_token] and 'last_traded_price' in message:
            price = float(message['last_traded_price']) / 100.0
            vol = message.get('last_traded_quantity', 0)
            best_5_buy = message.get('best_5_buy_data', [])
            best_5_sell = message.get('best_5_sell_data', [])
            if master: master.queue_tick(token, price, vol, best_5_buy, best_5_sell)
    except Exception: pass

def on_open(wsapp):
    global last_action_msg
    last_action_msg = "🟢 V61.0 The Omni-Adaptive Framework Online. Fully Verified."
    tokens_to_subscribe = [
        {"exchangeType": 1, "tokens": [VIX_TOKEN]}, 
        {"exchangeType": 2, "tokens": [ce_token, pe_token]}
    ]
    sws.subscribe("v61_feed", 3, tokens_to_subscribe)

def on_error(wsapp, error): pass

def on_close(wsapp): print("\n🔌 [WEBSOCKET] Connection Closed.")

def watchdog_monitor():
    global last_message_timestamp, last_action_msg, master, sws, reconnect_attempts
    while True:
        time.sleep(5) 
        if not is_market_open():
            last_message_timestamp = time.time()
            continue

        now_hour = get_ist_now().hour
        timeout_limit = 15 if (9 <= now_hour < 15) else 60

        if time.time() - last_message_timestamp > timeout_limit:
            reconnect_attempts += 1
            last_action_msg = f"⚠️ FEED DEAD! Reconnecting... (Attempt {reconnect_attempts})"
            
            # 🚀 [PRO LEVEL FIX 3] Exponential Backoff for API Safety
            sleep_time = min(5 * (2 ** reconnect_attempts), 300)
            
            if reconnect_attempts > 3:
                last_action_msg = f"🚨 DEEP CRASH DETECTED: Healing API Session... (Waiting {sleep_time}s)"
                if sws and getattr(sws, 'ws', None) is not None:
                    try: sws.close_connection()
                    except: pass
                time.sleep(sleep_time)
                init_smart_session()
                
            try:
                with tick_queue.mutex: tick_queue.queue.clear() 
                if master and master.hub: master.hub.reset_short_term_memory()

                if sws and getattr(sws, 'ws', None) is not None:
                    sws.close_connection()
                time.sleep(2)
                if sws: sws.connect()
                last_message_timestamp = time.time()
            except Exception: pass

if __name__ == "__main__":
    if init_smart_session():
        master = ExecutionEngine(smartApi)
        build_option_universe()
        if select_smart_options():
            sws.on_open = on_open
            sws.on_data = on_data
            sws.on_error = on_error
            sws.on_close = on_close
            
            wd_thread = threading.Thread(target=watchdog_monitor, daemon=True)
            wd_thread.start()
            
            while True:
                try:
                    if is_market_open():
                        sws.connect() 
                    else:
                        os.system('cls' if os.name == 'nt' else 'clear')
                        print("🌙 Market is Closed/Weekend. Apex V61.0 is in Sleep Mode. Will awake next working day...")
                        time.sleep(60) 
                except Exception:
                    time.sleep(5)
                    try: init_smart_session() 
                    except: pass
                except KeyboardInterrupt: 
                    print("\n\n🔌 EMERGENCY SHUTDOWN SEQUENCE ACTIVATED!")
                    try:
                        if master and master.shield.active:
                            print("⚠️ FORCE CLOSING OPEN POSITION BEFORE SHUTDOWN...")
                            master.shield.exit_trade(master.hub.last_price, "EMERGENCY_FORCE_CLOSE", 0)
                            time.sleep(2) 
                    except: pass
                    
                    try:
                        if sws and getattr(sws, 'ws', None) is not None:
                            sws.close_connection()
                    except: pass
                    break
