#!/usr/bin/env python3
"""
BG-BOT v5 — Full-Stack Trading Bot
Flask + SocketIO + Bitget API + Dual Timeframe Charts
"""

import json, os, time, hmac, hashlib, base64, threading, math, traceback
from datetime import datetime, timezone
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit

try:
    import requests as http_requests
    import pandas as pd
    import numpy as np
except ImportError:
    print("pip install flask flask-socketio pandas numpy requests")
    exit(1)

# ══════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════
CONFIG_FILE = "config.json"
DEFAULT_CFG = {
    "api_key": "", "api_secret": "", "api_passphrase": "",
    "demo": True, "market_mode": "spot", "symbol": "BTCUSDT",
    "order_size": 50, "max_positions": 3,
    "tp_percent": 2.5, "sl_percent": 1.5,
    "leverage": 3, "margin_mode": "crossed",
    "order_type": "market", "limit_offset": 0.2,
    "strategy": "multi_confirm",
    "indicators": {
        "rsi":   {"enabled": True,  "period": 14, "overbought": 70, "oversold": 30},
        "macd":  {"enabled": True,  "fast": 12, "slow": 26, "signal": 9},
        "bb":    {"enabled": True,  "period": 20, "std_dev": 2},
        "ema":   {"enabled": True,  "fast": 9, "slow": 21},
        "stoch": {"enabled": False, "k_period": 14, "d_period": 3, "smooth": 3},
        "atr":   {"enabled": False, "period": 14, "multiplier": 1.5}
    }
}


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        for k, v in DEFAULT_CFG.items():
            if k not in cfg:
                cfg[k] = v
            elif isinstance(v, dict) and isinstance(cfg.get(k), dict):
                for k2, v2 in v.items():
                    if k2 not in cfg[k]:
                        cfg[k][k2] = v2
        return cfg
    return DEFAULT_CFG.copy()


def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


# ══════════════════════════════════════════════════════════
#  BITGET API CLIENT
# ══════════════════════════════════════════════════════════
class BitgetClient:
    BASE = "https://api.bitget.com"

    def __init__(self, key, secret, passphrase, demo=True):
        self.key, self.secret, self.passphrase = key, secret, passphrase
        self.demo = demo
        self.sess = http_requests.Session()

    def _sign(self, ts, method, path, body=""):
        msg = ts + method.upper() + path + body
        mac = hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256)
        return base64.b64encode(mac.digest()).decode()

    def _headers(self, method, path, body=""):
        ts = str(int(time.time()))
        h = {"ACCESS-KEY": self.key, "ACCESS-SIGN": self._sign(ts, method, path, body),
             "ACCESS-TIMESTAMP": ts, "ACCESS-PASSPHRASE": self.passphrase,
             "Content-Type": "application/json", "locale": "en-US"}
        if self.demo:
            h["paptrading"] = "1"
        return h

    def _req(self, method, path, params=None, data=None):
        url = self.BASE + path
        body = json.dumps(data) if data else ""
        try:
            r = self.sess.request(method, url, headers=self._headers(method, path, body),
                                  params=params, data=body or None, timeout=10)
            return r.json()
        except Exception as e:
            return {"code": "99999", "msg": str(e)}

    def get_klines(self, symbol, gran, market="spot", limit=200):
        if market == "spot":
            path = "/api/v2/spot/market/candles"
        else:
            path = "/api/v2/mix/market/candles"
        params = {"symbol": symbol, "granularity": gran, "limit": str(limit)}
        if market != "spot":
            params["productType"] = "USDT-FUTURES"
        result = self._req("GET", path, params)
        if not result or result.get("code") != "00000":
            return None
        try:
            df = pd.DataFrame(result["data"], columns=[
                "timestamp", "open", "high", "low", "close", "volume", "quote_volume"
            ])
            for c in ["open", "high", "low", "close", "volume"]:
                df[c] = pd.to_numeric(df[c])
            df["timestamp"] = pd.to_datetime(df["timestamp"].astype(int), unit="ms")
            return df.sort_values("timestamp").reset_index(drop=True)
        except Exception:
            return None

    def get_balance(self, market="spot"):
        try:
            if market == "spot":
                r = self._req("GET", "/api/v2/spot/account/assets", {"coin": "USDT"})
                if r and r.get("data") and r["data"]:
                    return float(r["data"][0].get("available", 0))
            else:
                r = self._req("GET", "/api/v2/account/get-account-balance",
                              {"productType": "USDT-FUTURES"})
                if r and r.get("data"):
                    return float(r["data"][0].get("available", 0))
        except Exception:
            pass
        return 0

    def test_connection(self):
        try:
            bal = self.get_balance("spot")
            return {"ok": True, "balance": bal}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def spot_market(self, symbol, side, size):
        return self._req("POST", "/api/v2/spot/trade/place-order",
                         data={"symbol": symbol, "side": side,
                               "orderType": "market", "force": "gtc", "size": str(size)})

    def spot_limit(self, symbol, side, price, size):
        return self._req("POST", "/api/v2/spot/trade/place-order",
                         data={"symbol": symbol, "side": side,
                               "orderType": "limit", "force": "gtc",
                               "price": str(price), "size": str(size)})

    def set_leverage(self, symbol, lev, hold_side):
        return self._req("POST", "/api/v2/mix/account/set-leverage",
                         data={"productType": "USDT-FUTURES", "symbol": symbol,
                               "leverage": str(lev), "holdSide": hold_side})

    def perp_market(self, symbol, side, size, tp=None, sl=None):
        data = {"productType": "USDT-FUTURES", "symbol": symbol,
                "marginMode": "crossed", "marginCoin": "USDT",
                "size": str(size), "side": side, "orderType": "market"}
        if tp: data["presetStopSurplusPrice"] = str(tp)
        if sl: data["presetStopLossPrice"] = str(sl)
        return self._req("POST", "/api/v2/mix/order/place-order", data=data)

    def perp_limit(self, symbol, side, price, size, tp=None, sl=None):
        data = {"productType": "USDT-FUTURES", "symbol": symbol,
                "marginMode": "crossed", "marginCoin": "USDT",
                "size": str(size), "side": side, "orderType": "limit",
                "price": str(price)}
        if tp: data["presetStopSurplusPrice"] = str(tp)
        if sl: data["presetStopLossPrice"] = str(sl)
        return self._req("POST", "/api/v2/mix/order/place-order", data=data)

    def get_positions(self, symbol=None):
        params = {"productType": "USDT-FUTURES"}
        if symbol: params["symbol"] = symbol
        r = self._req("GET", "/api/v2/mix/position/get-all-position", params)
        if r and r.get("data"):
            return [p for p in r["data"] if float(p.get("total", 0)) > 0]
        return []

    def close_position(self, symbol, hold_side):
        return self._req("POST", "/api/v2/mix/order/close-positions",
                         data={"productType": "USDT-FUTURES",
                               "symbol": symbol, "holdSide": hold_side})


# ══════════════════════════════════════════════════════════
#  INDICATOR ENGINE — Returns chart-ready overlay data
# ══════════════════════════════════════════════════════════
class IndicatorEngine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.enabled = [k for k, v in cfg.items() if v.get("enabled")]

    def compute_all(self, df):
        """Compute indicators and return both signals and chart data."""
        signals = {}
        overlays = {
            "ema_fast": [], "ema_slow": [],
            "bb_upper": [], "bb_middle": [], "bb_lower": [],
            "rsi": [], "rsi_ob": 70, "rsi_os": 30,
            "macd_line": [], "macd_signal": [], "macd_hist": [],
            "stoch_k": [], "stoch_d": [],
        }

        times = df["timestamp"].tolist()

        # EMA
        if "ema" in self.enabled or True:  # Always compute for chart
            c = self.cfg.get("ema", {"fast": 9, "slow": 21})
            ef = df["close"].ewm(span=c.get("fast", 9)).mean()
            es = df["close"].ewm(span=c.get("slow", 21)).mean()
            for i, t in enumerate(times):
                ts = int(t.timestamp())
                overlays["ema_fast"].append({"time": ts, "value": round(float(ef.iloc[i]), 2)})
                overlays["ema_slow"].append({"time": ts, "value": round(float(es.iloc[i]), 2)})
            if "ema" in self.enabled:
                if ef.iloc[-1] > es.iloc[-1] and ef.iloc[-2] <= es.iloc[-2]:
                    signals["ema"] = "LONG"
                elif ef.iloc[-1] < es.iloc[-1] and ef.iloc[-2] >= es.iloc[-2]:
                    signals["ema"] = "SHORT"
                elif ef.iloc[-1] > es.iloc[-1]:
                    signals["ema"] = "LONG"
                else:
                    signals["ema"] = "SHORT"

        # Bollinger Bands
        if "bb" in self.enabled or True:
            c = self.cfg.get("bb", {"period": 20, "std_dev": 2})
            p = c.get("period", 20)
            sd = c.get("std_dev", 2)
            m = df["close"].rolling(p).mean()
            std = df["close"].rolling(p).std()
            for i, t in enumerate(times):
                ts = int(t.timestamp())
                if not math.isnan(m.iloc[i]):
                    overlays["bb_upper"].append({"time": ts, "value": round(float(m.iloc[i] + std.iloc[i] * sd), 2)})
                    overlays["bb_middle"].append({"time": ts, "value": round(float(m.iloc[i]), 2)})
                    overlays["bb_lower"].append({"time": ts, "value": round(float(m.iloc[i] - std.iloc[i] * sd), 2)})
            if "bb" in self.enabled:
                pr = df["close"].iloc[-1]
                if pr <= (m - std * sd).iloc[-1]:
                    signals["bb"] = "LONG"
                elif pr >= (m + std * sd).iloc[-1]:
                    signals["bb"] = "SHORT"
                else:
                    signals["bb"] = "NEUTRAL"

        # RSI
        if "rsi" in self.enabled or True:
            c = self.cfg.get("rsi", {"period": 14, "overbought": 70, "oversold": 30})
            p = c.get("period", 14)
            overlays["rsi_ob"] = c.get("overbought", 70)
            overlays["rsi_os"] = c.get("oversold", 30)
            delta = df["close"].diff()
            g = delta.where(delta > 0, 0).rolling(p).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(p).mean()
            rs = g / loss
            rsi = 100 - (100 / (1 + rs))
            for i, t in enumerate(times):
                ts = int(t.timestamp())
                if not math.isnan(rsi.iloc[i]):
                    overlays["rsi"].append({"time": ts, "value": round(float(rsi.iloc[i]), 2)})
            if "rsi" in self.enabled:
                v = rsi.iloc[-1]
                prev = rsi.iloc[-2]
                if v < c.get("oversold", 30):
                    signals["rsi"] = "LONG"
                elif v > c.get("overbought", 70):
                    signals["rsi"] = "SHORT"
                elif prev < c.get("oversold", 30) and v >= c.get("oversold", 30):
                    signals["rsi"] = "LONG"
                elif prev > c.get("overbought", 70) and v <= c.get("overbought", 70):
                    signals["rsi"] = "SHORT"
                else:
                    signals["rsi"] = "NEUTRAL"

        # MACD
        if "macd" in self.enabled or True:
            c = self.cfg.get("macd", {"fast": 12, "slow": 26, "signal": 9})
            ef = df["close"].ewm(span=c.get("fast", 12)).mean()
            es = df["close"].ewm(span=c.get("slow", 26)).mean()
            ml = ef - es
            sl = ml.ewm(span=c.get("signal", 9)).mean()
            hist = ml - sl
            for i, t in enumerate(times):
                ts = int(t.timestamp())
                if not math.isnan(ml.iloc[i]):
                    h_val = float(hist.iloc[i])
                    overlays["macd_line"].append({"time": ts, "value": round(float(ml.iloc[i]), 4)})
                    overlays["macd_signal"].append({"time": ts, "value": round(float(sl.iloc[i]), 4)})
                    overlays["macd_hist"].append({
                        "time": ts, "value": round(h_val, 4),
                        "color": "rgba(0,212,170,0.6)" if h_val >= 0 else "rgba(255,71,87,0.6)"
                    })
            if "macd" in self.enabled:
                h = hist
                if h.iloc[-1] > 0 and h.iloc[-2] <= 0:
                    signals["macd"] = "LONG"
                elif h.iloc[-1] < 0 and h.iloc[-2] >= 0:
                    signals["macd"] = "SHORT"
                elif h.iloc[-1] > 0:
                    signals["macd"] = "LONG"
                elif h.iloc[-1] < 0:
                    signals["macd"] = "SHORT"
                else:
                    signals["macd"] = "NEUTRAL"

        # Stochastic
        if "stoch" in self.enabled:
            c = self.cfg.get("stoch", {"k_period": 14, "d_period": 3, "smooth": 3})
            kp = c.get("k_period", 14)
            lo = df["low"].rolling(kp).min()
            hi = df["high"].rolling(kp).max()
            k = (100 * (df["close"] - lo) / (hi - lo)).rolling(c.get("smooth", 3)).mean()
            d = k.rolling(c.get("d_period", 3)).mean()
            for i, t in enumerate(times):
                ts = int(t.timestamp())
                if not math.isnan(k.iloc[i]):
                    overlays["stoch_k"].append({"time": ts, "value": round(float(k.iloc[i]), 2)})
                    overlays["stoch_d"].append({"time": ts, "value": round(float(d.iloc[i]), 2)})
            if k.iloc[-1] < 20 and d.iloc[-1] < 20:
                signals["stoch"] = "LONG"
            elif k.iloc[-1] > 80 and d.iloc[-1] > 80:
                signals["stoch"] = "SHORT"
            elif k.iloc[-1] > d.iloc[-1] and k.iloc[-2] <= d.iloc[-2]:
                signals["stoch"] = "LONG"
            elif k.iloc[-1] < d.iloc[-1] and k.iloc[-2] >= d.iloc[-2]:
                signals["stoch"] = "SHORT"
            else:
                signals["stoch"] = "NEUTRAL"

        # ATR
        if "atr" in self.enabled:
            c = self.cfg.get("atr", {"period": 14, "multiplier": 1.5})
            p = c.get("period", 14)
            tr = pd.concat([
                df["high"] - df["low"],
                (df["high"] - df["close"].shift()).abs(),
                (df["low"] - df["close"].shift()).abs()
            ], axis=1).max(axis=1)
            atr = tr.rolling(p).mean()
            avg = atr.rolling(50).mean().iloc[-1]
            if atr.iloc[-1] > avg * c.get("multiplier", 1.5):
                signals["atr"] = "LONG" if df["close"].iloc[-1] > df["close"].iloc[-5] else "SHORT"
            else:
                signals["atr"] = "NEUTRAL"

        return signals, overlays


# ══════════════════════════════════════════════════════════
#  SIMULATED DATA GENERATOR (for demo/no-API mode)
# ══════════════════════════════════════════════════════════
class SimDataGen:
    BASE_PRICES = {"BTCUSDT": 68420, "ETHUSDT": 3850, "SOLUSDT": 142,
                   "BNBUSDT": 580, "XRPUSDT": 0.58, "DOGEUSDT": 0.12,
                   "ADAUSDT": 0.45, "AVAXUSDT": 28}

    @staticmethod
    def generate_candles(symbol, minutes=200, interval_min=1):
        base = SimDataGen.BASE_PRICES.get(symbol, 100)
        now = datetime.now(timezone.utc)
        candles = []
        price = base * (0.97 + np.random.random() * 0.06)

        for i in range(minutes):
            t = now - pd.Timedelta(minutes=(minutes - i) * interval_min)
            change = np.random.normal(0, base * 0.0008)
            o = price
            c = price + change
            h = max(o, c) + abs(np.random.normal(0, base * 0.0003))
            l = min(o, c) - abs(np.random.normal(0, base * 0.0003))
            v = abs(np.random.normal(100, 30))
            candles.append({
                "timestamp": t,
                "open": round(o, 2), "high": round(h, 2),
                "low": round(l, 2), "close": round(c, 2),
                "volume": round(v, 2)
            })
            price = c

        df = pd.DataFrame(candles)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df


# ══════════════════════════════════════════════════════════
#  TRADING BOT ENGINE
# ══════════════════════════════════════════════════════════
class TradingBot:
    def __init__(self, socketio):
        self.sio = socketio
        self.running = False
        self.config = load_config()
        self.client = None
        self.indicators = IndicatorEngine(self.config["indicators"])
        self.sim = SimDataGen()
        self.thread = None
        self.state = {
            "running": False, "mode": "spot", "symbol": "BTCUSDT",
            "balance": 0, "connected": False,
            "m1": {"signal": "WAITING", "price": 0, "indicators": {}},
            "m5": {"signal": "WAITING", "price": 0, "indicators": {}},
            "aligned": False,
            "trades": [], "logs": [], "positions": [],
            "stats": {"total": 0, "wins": 0, "losses": 0,
                      "spot": 0, "perp": 0, "alignments": 0, "checks": 0},
        }

    def _log(self, level, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        entry = {"time": ts, "level": level, "msg": msg}
        self.state["logs"].append(entry)
        if len(self.state["logs"]) > 300:
            self.state["logs"] = self.state["logs"][-300:]
        try:
            self.sio.emit("log", entry)
        except Exception:
            pass
        colors = {"info": "\033[36m", "success": "\033[32m",
                  "warn": "\033[33m", "error": "\033[31m", "tf": "\033[35m"}
        print(f"{colors.get(level, '')}[{ts}][{level}] {msg}\033[0m")

    def _init_client(self):
        cfg = self.config
        if not cfg.get("api_key"):
            self._log("warn", "No API key — using SIMULATED data")
            self.state["connected"] = False
            return False
        try:
            self.client = BitgetClient(
                cfg["api_key"], cfg["api_secret"],
                cfg["api_passphrase"], cfg.get("demo", True))
            result = self.client.test_connection()
            if result["ok"]:
                self.state["connected"] = True
                self.state["balance"] = result["balance"]
                self._log("success", f"API connected! Balance: ${result['balance']:.2f}")
                return True
            else:
                self._log("error", f"API test failed: {result.get('msg', 'unknown')}")
                self.state["connected"] = False
                return False
        except Exception as e:
            self._log("error", f"Connection error: {e}")
            self.state["connected"] = False
            return False

    def _get_klines(self, symbol, gran, market):
        """Get klines from API or simulation."""
        if self.state["connected"] and self.client:
            df = self.client.get_klines(symbol, gran, market)
            if df is not None and not df.empty:
                return df
        # Fallback to simulation
        interval = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240}.get(gran, 1)
        return self.sim.generate_candles(symbol, minutes=200, interval_min=interval)

    def _resolve(self, signals):
        strat = self.config.get("strategy", "multi_confirm")
        if strat == "multi_confirm":
            bull = sum(1 for s in signals.values() if s == "LONG")
            bear = sum(1 for s in signals.values() if s == "SHORT")
            thr = max(2, len(signals) // 2)
            if bull >= thr: return "LONG"
            if bear >= thr: return "SHORT"
        elif strat == "primary_secondary":
            primary = signals.get("rsi", "NEUTRAL")
            others = [v for k, v in signals.items() if k != "rsi"]
            if primary == "LONG" and "SHORT" not in others: return "LONG"
            if primary == "SHORT" and "LONG" not in others: return "SHORT"
        elif strat == "weighted_score":
            w = {"rsi": 2, "macd": 2, "bb": 1.5, "ema": 1.5, "stoch": 1, "atr": 1}
            score = sum(w.get(k, 1) if v == "LONG" else -w.get(k, 1) if v == "SHORT" else 0
                        for k, v in signals.items())
            if score >= 3: return "LONG"
            if score <= -3: return "SHORT"
        return "NEUTRAL"

    def _build_chart_payload(self, df, signals, overlays):
        """Build chart data payload for frontend."""
        candles = []
        for _, row in df.iterrows():
            candles.append({
                "time": int(row["timestamp"].timestamp()),
                "open": float(row["open"]), "high": float(row["high"]),
                "low": float(row["low"]), "close": float(row["close"]),
            })
        return {
            "candles": candles,
            "overlays": overlays,
            "signals": signals,
            "price": float(df["close"].iloc[-1]),
        }

    def _loop(self):
        self._log("info", "═══ Bot Engine Started ═══")
        symbol = self.config.get("symbol", "BTCUSDT")
        mode = self.config.get("market_mode", "spot")
        self.state["mode"] = mode
        self.state["symbol"] = symbol

        api_ok = self._init_client()
        if not api_ok:
            self._log("info", "Running in SIMULATION mode (no API key or connection failed)")

        self._log("info", f"Pair: {symbol} | Mode: {mode.upper()} | "
                          f"Strategy: {self.config.get('strategy')}")

        while self.running:
            try:
                # 1. Fetch M1 & M5 data
                df_m1 = self._get_klines(symbol, "1m", mode)
                df_m5 = self._get_klines(symbol, "5m", mode)

                if df_m1 is None or df_m5 is None:
                    self._log("warn", "Data fetch failed, retrying...")
                    time.sleep(3)
                    continue

                # 2. Compute indicators + overlays
                sig_m1, ov_m1 = self.indicators.compute_all(df_m1)
                sig_m5, ov_m5 = self.indicators.compute_all(df_m5)

                m1_signal = self._resolve(sig_m1)
                m5_signal = self._resolve(sig_m5)

                price_m1 = float(df_m1["close"].iloc[-1])
                price_m5 = float(df_m5["close"].iloc[-1])

                # 3. Build chart payloads
                chart_m1 = self._build_chart_payload(df_m1, sig_m1, ov_m1)
                chart_m5 = self._build_chart_payload(df_m5, sig_m5, ov_m5)

                # 4. Alignment
                aligned = m1_signal == m5_signal and m1_signal != "NEUTRAL"
                self.state["m1"] = {"signal": m1_signal, "price": price_m1, "indicators": sig_m1}
                self.state["m5"] = {"signal": m5_signal, "price": price_m5, "indicators": sig_m5}
                self.state["aligned"] = aligned
                self.state["stats"]["checks"] += 1

                self._log("tf", f"[M1] {m1_signal} ${price_m1:,.2f} | "
                                f"[M5] {m5_signal} ${price_m5:,.2f} "
                                f"{'✓ ALIGNED' if aligned else '✗'}")

                if aligned:
                    self.state["stats"]["alignments"] += 1
                    self._log("success", f"✓ ALIGNED: {m1_signal} → EXECUTE")
                    self._execute(m1_signal, price_m5)

                # 5. Update balance (if connected)
                if self.state["connected"] and self.client:
                    try:
                        self.state["balance"] = self.client.get_balance(mode)
                    except Exception:
                        pass

                # 6. Emit everything via WebSocket
                try:
                    self.sio.emit("state_update", self.state)
                    self.sio.emit("chart_m1", chart_m1)
                    self.sio.emit("chart_m5", chart_m5)
                except Exception as e:
                    self._log("error", f"WebSocket emit error: {e}")

                # 7. Wait for next M5 candle
                for _ in range(60):
                    if not self.running:
                        break
                    time.sleep(5)

            except Exception as e:
                self._log("error", f"Loop error: {e}\n{traceback.format_exc()}")
                time.sleep(10)

        self._log("warn", "Bot engine stopped.")
        self.state["running"] = False

    def _execute(self, signal, price):
        cfg = self.config
        mode = cfg.get("market_mode", "spot")
        otype = cfg.get("order_type", "market")
        symbol = cfg.get("symbol", "BTCUSDT")
        size = cfg.get("order_size", 50)

        if not self.state["connected"] or not self.client:
            # Simulated execution
            trade = {
                "time": datetime.now().strftime("%H:%M:%S"),
                "mode": mode, "side": "buy" if signal == "LONG" else "sell",
                "pair": symbol, "price": price, "type": otype,
                "pnl": round(np.random.uniform(-5, 12), 2)
            }
            self.state["trades"].insert(0, trade)
            self.state["stats"]["total"] += 1
            self.state["stats"][mode] += 1
            if trade["pnl"] >= 0:
                self.state["stats"]["wins"] += 1
            else:
                self.state["stats"]["losses"] += 1
            self._log("success", f"[SIM] {otype.upper()} {signal} {symbol} @ ${price:,.2f}")
            self.sio.emit("trade", trade)
            return

        # Real execution logic (simplified for space)
        try:
            if mode == "spot":
                side = "buy" if signal == "LONG" else "sell"
                r = self.client.spot_market(symbol, side, size)
                if r and r.get("code") == "00000":
                    self._log("success", f"SPOT {side.upper()} filled!")
                    self.state["stats"]["total"] += 1
                    self.state["stats"]["spot"] += 1
            else:
                side = "buy" if signal == "LONG" else "sell"
                lev = cfg.get("leverage", 3)
                contracts = round(size * lev / price, 3)
                r = self.client.perp_market(symbol, side, contracts)
                if r and r.get("code") == "00000":
                    self._log("success", f"PERP {signal} filled!")
                    self.state["stats"]["total"] += 1
                    self.state["stats"]["perp"] += 1
        except Exception as e:
            self._log("error", f"Execution error: {e}")

    def start(self):
        if self.running:
            return {"ok": False, "msg": "Already running"}
        self.config = load_config()
        self.indicators = IndicatorEngine(self.config["indicators"])
        self.running = True
        self.state["running"] = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        return {"ok": True}

    def stop(self):
        self.running = False
        self.state["running"] = False
        return {"ok": True}


# ══════════════════════════════════════════════════════════
#  FLASK + SOCKETIO APP
# ══════════════════════════════════════════════════════════
app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24).hex()
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading",
                    ping_timeout=30, ping_interval=10)
bot = TradingBot(socketio)


@app.route("/")
def index():
    return render_template("index.html")


@socketio.on("connect")
def handle_connect():
    print(f"\033[32m[WS] Client connected\033[0m")
    emit("state_update", bot.state)
    emit("config", bot.config)


@socketio.on("disconnect")
def handle_disconnect():
    print(f"\033[33m[WS] Client disconnected\033[0m")


@socketio.on("get_state")
def handle_get_state():
    emit("state_update", bot.state)


@socketio.on("get_config")
def handle_get_config():
    emit("config", bot.config)


@socketio.on("save_config")
def handle_save_config(data):
    try:
        bot.config = data
        save_config(data)
        bot.indicators = IndicatorEngine(data.get("indicators", {}))
        emit("config_saved", {"ok": True})
        bot._log("info", "Configuration saved.")
    except Exception as e:
        emit("error", {"msg": f"Config save failed: {e}"})


@socketio.on("test_connection")
def handle_test(data):
    try:
        client = BitgetClient(
            data.get("api_key", ""), data.get("api_secret", ""),
            data.get("api_passphrase", ""), data.get("demo", True))
        result = client.test_connection()
        emit("connection_result", result)
    except Exception as e:
        emit("connection_result", {"ok": False, "msg": str(e)})


@socketio.on("start_bot")
def handle_start():
    result = bot.start()
    emit("bot_status", result)


@socketio.on("stop_bot")
def handle_stop():
    result = bot.stop()
    emit("bot_status", result)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"""
\033[36m╔═══════════════════════════════════════════════╗
║  BG-BOT v5 — Full-Stack + Charts + WebSocket  ║
║                                               ║
║  Server:  http://0.0.0.0:{port:<20s}║
║                                               ║
║  Android: http://YOUR_IP:{port:<19s}║
║           ⋮ → Desktop site ✓                  ║
║                                               ║
║  Press Ctrl+C to stop                         ║
╚═══════════════════════════════════════════════╝\033[0m
    """)
    socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)
