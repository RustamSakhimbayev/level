# Vol - H1 Фьючерс 24 свечи 24 часа
# Изм - H1 Фьючерс 12 свечей 12 часов
# NATR - М5 Фьючерс 14 свечей 70 минут
# Кор - М5 Фьючерс 48 свечей 4 часа
# Всп - H1 Фьючерс 20 свечей 20 часов (сравнивается последняя свеча со средним занчением 20 свечей)

from __future__ import annotations
import asyncio, math
from dataclasses import dataclass
from typing import List, Dict, Tuple
import time
import websockets
import numpy as np

from PySide6 import QtWidgets, QtCore, QtGui
from PySide6.QtCore import QSettings
import pyqtgraph as pg
import json
from PySide6.QtMultimedia import QSoundEffect
from PySide6.QtCore import QUrl
from bisect import bisect_left
from PySide6.QtGui import QPainterPathStroker


with open("tick_sizes.json", "r") as f:
    tick_sizes = json.load(f)


def round_to_step(price: float, step: float) -> float:
    """Округляет цену в соответствии со шагом фьючерса."""
    return round(round(price / step) * step, 10)


# --- Индикатор-цветная полоска для списка символов ---
def make_color_bar(color: QtGui.QColor, height: int = 12, width: int = 3) -> QtGui.QPixmap:
    pixmap = QtGui.QPixmap(width, height)
    pixmap.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pixmap)
    painter.fillRect(0, 0, width, height, color)
    painter.end()
    return pixmap


# ---------------- Storage ----------------
CANDLES_H1: Dict[str, List[dict]] = {}       # Spot H1
CANDLES_H1_FUT: Dict[str, List[dict]] = {}   # Futures H1
CANDLES_M5: Dict[str, List[dict]] = {}       # Spot M5 для второго графика
CANDLES_M5_FUT: Dict[str, List[dict]] = {}   # ✅ Futures M5
LEVELS_BY_SYMBOL: Dict[str, List[dict]] = {}
CORR_M5: Dict[str, float] = {}               # { "ETHUSDT": +45.0, ... }


# ---------------- Params -----------------
H1_LIMIT = 720   # сколько H1 свечей загружается (30 дней)
M5_LIMIT = 576   # сколько M5 свечей загружается (2 дня)

# -------------- Network ------------------
async def fetch_hourly_candles(session, symbol: str) -> List[dict]:
    # print(f"Часовые спот свечи")
    """Spot H1 candles, last 200."""
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": "1h", "limit": H1_LIMIT}
    try:
        async with session.get(url, params=params, timeout=10) as r:
            if r.status != 200:
                return []
            raw = await r.json()
    except Exception:
        return []

    out = []
    for c in raw:
        out.append({
            "time": int(c[0]),
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": float(c[5]),
        })
    return out


async def fetch_hourly_candles_fut(session, symbol: str) -> List[dict]:
    # print(f"Часовые свечи фьючерс")
    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {"symbol": symbol, "interval": "1h", "limit": H1_LIMIT}
    try:
        async with session.get(url, params=params, timeout=10) as r:
            if r.status != 200:
                return []
            raw = await r.json()
    except Exception:
        return []

    # --- добавляем OI ---
    oi_data = await fetch_open_interest(session, symbol, "1h", H1_LIMIT)
    oi_map = {oi["time"]: oi["open_interest"] for oi in oi_data}

    out = []
    for c in raw:
        t = int(c[0])
        out.append({
            "time": t,
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": float(c[5]),
            "open_interest": oi_map.get(t, None),
        })
    return out


async def fetch_m5_candles(session, symbol: str) -> List[dict]:
    # print(f"Свечи м5 спот")
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": "5m", "limit": M5_LIMIT}
    try:
        async with session.get(url, params=params, timeout=10) as r:
            if r.status != 200:
                return []
            raw = await r.json()
    except Exception:
        return []

    out = []
    for c in raw:
        out.append({
            "time": int(c[0]),
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": float(c[5]),
        })
    return out


async def fetch_m5_candles_fut(session, symbol: str) -> List[dict]:
    # print(f"Свечи м5 фьючерс")
    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {"symbol": symbol, "interval": "5m", "limit": M5_LIMIT}
    try:
        async with session.get(url, params=params, timeout=10) as r:
            if r.status != 200:
                return []
            raw = await r.json()
    except Exception:
        return []

    # --- добавляем OI ---
    oi_data = await fetch_open_interest(session, symbol, "5m", M5_LIMIT)
    oi_map = {oi["time"]: oi["open_interest"] for oi in oi_data}

    out = []
    for c in raw:
        t = int(c[0])
        out.append({
            "time": t,
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": float(c[5]),
            "open_interest": oi_map.get(t, None),
        })
    return out


async def fetch_open_interest(session, symbol: str, interval: str, limit: int):
    """
    Загружает исторические данные открытого интереса (OI) с Binance Futures.
    interval: '5m' или '1h'
    limit — сколько записей, совпадает с числом свечей.
    """
    # print(f"Открытый интерес")
    url = "https://fapi.binance.com/futures/data/openInterestHist"
    params = {"symbol": symbol, "period": interval, "limit": limit}
    try:
        async with session.get(url, params=params, timeout=10) as r:
            if r.status != 200:
                return []
            raw = await r.json()
    except Exception:
        return []

    out = []
    for c in raw:
        out.append({
            "time": int(c["timestamp"]),
            "open_interest": float(c["sumOpenInterest"]),
        })
    return out


import numpy as np


async def fetch_last_m5_candle(session, symbol: str) -> dict | None:
    tf_ms = 5 * 60 * 1000
    now_ms = int(time.time() * 1000)
    end_time = (now_ms // tf_ms) * tf_ms - 1

    url = "https://api.binance.com/api/v3/klines"
    params = {
        "symbol": symbol,
        "interval": "5m",
        "endTime": end_time,
        "limit": 1,
    }

    try:
        async with session.get(url, params=params, timeout=10) as r:
            if r.status != 200:
                return None
            raw = await r.json()
    except Exception:
        return None

    if not raw:
        return None

    c = raw[0]
    return {
        "time": int(c[0]),
        "open": float(c[1]),
        "high": float(c[2]),
        "low": float(c[3]),
        "close": float(c[4]),
        "volume": float(c[5]),
    }


async def fetch_last_h1_candle(session, symbol: str) -> dict | None:
    tf_ms = 60 * 60 * 1000
    now_ms = int(time.time() * 1000)
    end_time = (now_ms // tf_ms) * tf_ms - 1

    url = "https://api.binance.com/api/v3/klines"
    params = {
        "symbol": symbol,
        "interval": "1h",
        "endTime": end_time,
        "limit": 1,
    }

    try:
        async with session.get(url, params=params, timeout=10) as r:
            if r.status != 200:
                return None
            raw = await r.json()
    except Exception:
        return None

    if not raw:
        return None

    c = raw[0]
    return {
        "time": int(c[0]),
        "open": float(c[1]),
        "high": float(c[2]),
        "low": float(c[3]),
        "close": float(c[4]),
        "volume": float(c[5]),
    }


async def fetch_last_m5_candle_fut(session, symbol: str) -> dict | None:
    tf_ms = 5 * 60 * 1000
    now_ms = int(time.time() * 1000)
    end_time = (now_ms // tf_ms) * tf_ms - 1

    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {
        "symbol": symbol,
        "interval": "5m",
        "endTime": end_time,
        "limit": 1,
    }

    try:
        async with session.get(url, params=params, timeout=10) as r:
            if r.status != 200:
                return None
            raw = await r.json()
    except Exception:
        return None

    if not raw:
        return None

    c = raw[0]

    oi = await fetch_open_interest(session, symbol, "5m", 1)
    oi_val = oi[0]["open_interest"] if oi else None

    return {
        "time": int(c[0]),
        "open": float(c[1]),
        "high": float(c[2]),
        "low": float(c[3]),
        "close": float(c[4]),
        "volume": float(c[5]),
        "open_interest": oi_val,
    }


async def fetch_last_h1_candle_fut(session, symbol: str) -> dict | None:
    tf_ms = 60 * 60 * 1000
    now_ms = int(time.time() * 1000)
    end_time = (now_ms // tf_ms) * tf_ms - 1

    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {
        "symbol": symbol,
        "interval": "1h",
        "endTime": end_time,
        "limit": 1,
    }

    try:
        async with session.get(url, params=params, timeout=10) as r:
            if r.status != 200:
                return None
            raw = await r.json()
    except Exception:
        return None

    if not raw:
        return None

    c = raw[0]

    oi = await fetch_open_interest(session, symbol, "1h", 1)
    oi_val = oi[0]["open_interest"] if oi else None

    return {
        "time": int(c[0]),
        "open": float(c[1]),
        "high": float(c[2]),
        "low": float(c[3]),
        "close": float(c[4]),
        "volume": float(c[5]),
        "open_interest": oi_val,
    }


def merge_last_candle(store: dict, symbol: str, candle: dict):
    arr = store.get(symbol)

    if arr is None:
        store[symbol] = [candle]
        return

    last = arr[-1]

    # 1) Если это ТА ЖЕ свеча (5 минут ещё не прошли)
    if candle["time"] == last["time"]:
        arr[-1] = candle
        return

    # 2) Если это НОВАЯ свеча (начались следующие 5 минут)
    if candle["time"] > last["time"]:
        arr.append(candle)
        return

    # 3) Старые свечи от WebSocket игнорируем
    return

async def fetch_last_m5_candle_fut_live(session, symbol: str):
    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {
        "symbol": symbol,
        "interval": "5m",
        "limit": 1
    }
    try:
        async with session.get(url, params=params, timeout=10) as r:
            if r.status != 200:
                return None
            raw = await r.json()
    except:
        return None
    if not raw:
        return None

    c = raw[0]
    oi = await fetch_open_interest(session, symbol, "5m", 1)
    oi_val = oi[0]["open_interest"] if oi else None

    return {
        "time": int(c[0]),
        "open": float(c[1]),
        "high": float(c[2]),
        "low": float(c[3]),
        "close": float(c[4]),
        "volume": float(c[5]),
        "open_interest": oi_val,
    }


async def fetch_last_h1_candle_fut_live(session, symbol: str):
    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {
        "symbol": symbol,
        "interval": "1h",
        "limit": 1
    }
    try:
        async with session.get(url, params=params, timeout=10) as r:
            if r.status != 200:
                return None
            raw = await r.json()
    except:
        return None

    if not raw:
        return None

    c = raw[0]
    oi = await fetch_open_interest(session, symbol, "1h", 1)
    oi_val = oi[0]["open_interest"] if oi else None

    return {
        "time": int(c[0]),
        "open": float(c[1]),
        "high": float(c[2]),
        "low": float(c[3]),
        "close": float(c[4]),
        "volume": float(c[5]),
        "open_interest": oi_val,
    }



def calc_corr(symbol: str, tf: str = "M5", bars: int = 48) -> float | None:
    """
    Возвращает корреляцию монеты с BTCUSDT в % (от -100 до +100).
    Можно выбрать таймфрейм: M5 или H1.
    """
    # выбираем источник свечей в зависимости от tf
    if tf.upper() == "M5":
        source = CANDLES_M5
    else:
        source = CANDLES_H1

    # проверяем наличие данных
    if symbol not in source or "BTCUSDT" not in source:
        return None

    candles_coin = source.get(symbol, [])[-(bars + 1):]
    candles_btc = source.get("BTCUSDT", [])[-(bars + 1):]
    if len(candles_coin) < bars + 1 or len(candles_btc) < bars + 1:
        return None

    # считаем доходности (returns)
    r_coin = []
    r_btc = []
    for i in range(1, bars + 1):
        c1 = candles_coin[i]["close"]
        c0 = candles_coin[i - 1]["close"]
        b1 = candles_btc[i]["close"]
        b0 = candles_btc[i - 1]["close"]
        if c0 <= 0 or b0 <= 0:
            return None
        r_coin.append((c1 - c0) / c0)
        r_btc.append((b1 - b0) / b0)

    # считаем коэффициент корреляции Пирсона
    corr = np.corrcoef(r_coin, r_btc)[0, 1]
    if np.isnan(corr):
        return None

    return round(corr * 100)  # в процентах

def calc_boi(sym: str, tf: str = "H1", period: int = 20):
    src = CANDLES_M5_FUT if tf.upper() == "M5" else CANDLES_H1_FUT
    candles = src.get(sym)
    if not candles or len(candles) < period + 1:
        return None

    vals = [c.get("open_interest") for c in candles[-(period+1):]]
    if any(v is None for v in vals):
        return None

    prev = vals[:-1]
    last = vals[-1]

    avg_prev = sum(prev) / len(prev)
    if avg_prev <= 0:
        return None

    # проценты
    return round((last - avg_prev) / avg_prev * 100, 1)


# --- Всплеск торгового объема (фьючерсы H1) ---
def calc_volume_spike_fut(symbol: str, tf: str = "H1", period: int = 20) -> float | None:
    """
    Возвращает, во сколько раз объём последней фьючерсной свечи превышает
    средний объём за указанный период.
    Работает с таймфреймами H1 и M5.
    Пример: 2.5 => объём вырос в 2.5 раза.
    """

    # выбираем только фьючерсные источники
    if tf.upper() == "M5":
        candles = CANDLES_M5_FUT.get(symbol)
    else:
        candles = CANDLES_H1_FUT.get(symbol)

    if not candles or len(candles) < period + 1:
        return None

    recent = candles[-(period + 1):]
    volumes = [c["volume"] for c in recent[:-1]]
    avg_volume = sum(volumes) / len(volumes)
    current_volume = recent[-1]["volume"]

    if avg_volume <= 0:
        return None

    return round(current_volume / avg_volume, 2)


# -------------- Detection ----------------
def detect_levels_for_symbol(candles, tf="H1", symbol=None):
    if not candles:
        return []

    # --- STEP (шаг цены инструмента) ---
    step = None
    if symbol:
        key = f"Spot:{symbol}"
        step_str = tick_sizes.get(key)
        if step_str:
            step = float(step_str)

    if step is None:
        print(f"[WARN] Tick size NOT FOUND for {symbol}, fallback=0.0001")
        step = 0.0001

    # --- ПАРАМЕТРЫ ---
    if tf.upper() == "H1":
        EXT      = 10            # окно поиска экстремума
        MIN_GAP  = 5            # минимальная дистанция между первым и вторым касанием
        P        = step * 2      # пробой уровней (закрытием)
        TOL_DUP  = step * 1      # допуск для объединения близких уровней
    else:
        EXT      = 10
        MIN_GAP  = 5
        P        = step * 2
        TOL_DUP  = step * 1

    # --- ДАННЫЕ ---
    lows   = [c["low"] for c in candles]
    highs  = [c["high"] for c in candles]
    closes = [c["close"] for c in candles]
    n = len(candles)

    minima = []
    maxima = []

    # --- ПОИСК ЭКСТРЕМУМОВ ---
    for i in range(n):
        lo = lows[i]
        hi = highs[i]

        left  = max(0, i - EXT)
        right = min(n - 1, i + EXT)

        # локальный минимум
        if all(lows[j] >= lo or j == i for j in range(left, right + 1)):
            minima.append((i, lo))

        # локальный максимум
        if all(highs[j] <= hi or j == i for j in range(left, right + 1)):
            maxima.append((i, hi))

    # --- СОЗДАЁМ УРОВНИ ---
    levels = []
    last_index = n - 1

    for idx, price in minima:
        price = round(price / step) * step     # нормализация цены
        levels.append({"price": price, "i1": idx, "i2": last_index, "side": "sup"})

    for idx, price in maxima:
        price = round(price / step) * step     # нормализация цены
        levels.append({"price": price, "i1": idx, "i2": last_index, "side": "res"})

    # --- 1. ФИЛЬТР ПО ПРОБОЮ + СБОР ВСЕХ КАСАНИЙ + АНТИДУБЛИКАТ ПО ЦЕНЕ ---
    candidates = []   # уровни с полным списком касаний

    for lvl in levels:
        price = lvl["price"]
        side  = lvl["side"]
        i1    = lvl["i1"]

        # --- пробой закрытием ---
        future_closes = closes[i1+1:]
        if side == "res":
            broken = any(c >= price + P for c in future_closes)
        else:
            broken = any(c <= price - P for c in future_closes)
        if broken:
            continue

        # --- все касания после MIN_GAP ---
        i_start = i1 + MIN_GAP
        if i_start >= n:
            continue

        touches = []

        if side == "res":
            # максимум считается касанием если:
            # 1) high >= уровень  (пробили хвостом)
            # 2) недошли ≤ 2 шага
            for i in range(i_start, n):
                if highs[i] >= price or (highs[i] < price and (price - highs[i]) <= 2 * step):
                    touches.append(i)
        else:
            # минимум считается касанием если:
            # 1) low <= уровень   (пробили хвостом вниз)
            # 2) недошли ≤ 2 шага
            for i in range(i_start, n):
                if lows[i] <= price or (lows[i] > price and (lows[i] - price) <= 2 * step):
                    touches.append(i)

        # нужен хотя бы один повторный тест уровня
        if not touches:
            continue

        # --- АНТИДУБЛИКАТ ПО ЦЕНЕ СРАЗУ НА ЭТАПЕ КАНДИДАТОВ ---
        # (как раньше: если уже есть уровень почти по той же цене и того же типа — новый не добавляем)
        is_dup = any(
            (lvl2["side"] == side) and (abs(price - lvl2["price"]) <= TOL_DUP)
            for lvl2 in candidates
        )
        if is_dup:
            continue

        lvl = dict(lvl)  # копия
        lvl["touches"] = touches
        candidates.append(lvl)

    # --- 2. УДАЛЕНИЕ "младших" уровней, если КАСАНИЯ НА ОДНИХ И ТЕХ ЖЕ СВЕЧАХ ---

    m = len(candidates)
    keep = [True] * m

    for i in range(m):
        if not keep[i]:
            continue
        lvl_i = candidates[i]
        side_i = lvl_i["side"]
        price_i = lvl_i["price"]
        touches_i = set(lvl_i["touches"])

        for j in range(i + 1, m):
            if not keep[j]:
                continue
            lvl_j = candidates[j]
            if lvl_j["side"] != side_i:
                continue  # сравниваем только sup с sup и res с res

            touches_j = set(lvl_j["touches"])

            # есть ли общая свеча касания?
            if not (touches_i & touches_j):
                continue

            price_j = lvl_j["price"]

            if side_i == "res":
                # для сопротивления оставляем более ВЫСОКИЙ уровень
                if price_i >= price_j:
                    keep[j] = False
                else:
                    keep[i] = False
                    break  # lvl_i проиграл, дальше его не сравниваем
            else:
                # для поддержки оставляем более НИЗКИЙ уровень
                if price_i <= price_j:
                    keep[j] = False
                else:
                    keep[i] = False
                    break

    filtered = [lvl for idx, lvl in enumerate(candidates) if keep[idx]]

    return filtered


# -------------- UI: Chart ----------------
class LevelsChart(QtWidgets.QWidget):
    def __init__(self, pane, parent=None, tf="H1"):
        super().__init__(parent)
        self.pane = pane

        self._auto_range_done = False

        self.tf = tf

        # --- Однотонный фон ---
        self.setAutoFillBackground(True)
        palette = self.palette()
        palette.setColor(QtGui.QPalette.Window, QtGui.QColor("#1b1f22"))
        self.setPalette(palette)

        # --- Layout ---
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # --- Оси и двойной ViewBox (цены + объёмы) ---
        from pyqtgraph.graphicsItems.DateAxisItem import DateAxisItem
        axis_time = DateAxisItem(orientation='bottom')
        self.plot = pg.PlotWidget(axisItems={'bottom': axis_time})
        # --- Принудительное форматирование шкалы Y по шагу цены ---
        right_axis = self.plot.getPlotItem().getAxis('right')

        def _format_price_ticks(values, scale, spacing, chart=self):
            symbol = chart._current_symbol
            if not symbol:
                return [f"{v:.4f}" for v in values]

            tick_key = f"Spot:{symbol}"
            tick_str = tick_sizes.get(tick_key)

            if not tick_str:
                return [f"{v:.4f}" for v in values]

            decimals = len(tick_str.split(".")[1])
            return [f"{v:.{decimals}f}" for v in values]

        # подменяем метод форматирования
        right_axis.tickStrings = _format_price_ticks

        self.plot.setBackground(QtGui.QColor("#1b1f22"))

        self.plot.showAxis('left', False)
        self.plot.getPlotItem().hideButtons()

        self.plot.setMenuEnabled(False)
        self.plot.getPlotItem().setMenuEnabled(False)
        self.plot.getViewBox().setMenuEnabled(False)


        # основной viewbox для цены
        self.vb_price = self.plot.getViewBox()

        # === Универсальный ограничитель зума по количеству баров ===
        def _limited_wheel(ev, orig=self.vb_price.wheelEvent, vb=self.vb_price, chart=self):

            if not hasattr(chart, "_bars_count") or chart._bars_count < 3:
                return orig(ev)

            (x1, x2), (y1, y2) = vb.viewRange()

            # --- правильный шаг свечи, БЕЗ вычислений span ---
            if chart.tf == "H1":
                candle_step = 3600
            else:
                candle_step = 300

            bars_in_view = (x2 - x1) / candle_step

            # === ТВОИ ОГРАНИЧЕНИЯ ===
            MIN_BARS = 40

            if chart.tf == "H1":
                MAX_BARS = 720  # ← ты сказал
            else:
                MAX_BARS = 576  # ← ты сказал

            # --- zoom-in ---
            if ev.delta() > 0 and bars_in_view <= MIN_BARS:
                ev.accept()
                return

            # --- zoom-out ---
            if ev.delta() < 0 and bars_in_view >= MAX_BARS:
                ev.accept()
                return

            return orig(ev)

        self.vb_price.wheelEvent = _limited_wheel

        # делаем фон прозрачным, чтобы сквозь него было видно слой объёмов
        try:
            self.vb_price.setBackgroundColor(QtCore.Qt.transparent)
        except Exception:
            pass

        # включаем управление мышью для ценового графика
        self.vb_price.setMouseEnabled(x=True, y=True)
        self.vb_price.setMouseMode(pg.ViewBox.PanMode)  # ЛКМ — панорамирование, ПКМ — масштаб

        # === ViewBox для объёмов (нижний слой, независим от графика цен) ===
        self.vb_volume = pg.ViewBox(enableMouse=False)
        self.vb_volume.setMenuEnabled(False)
        self.vb_volume.ctrlMenu = None
        self.vb_volume.setBackgroundColor(None)
        self.vb_volume.setXLink(self.vb_price)
        self.plot.scene().addItem(self.vb_volume)
        self.vb_volume.setZValue(self.vb_price.zValue() - 1)
        self.vb_volume.setMouseEnabled(False, False)

        # === ViewBox для открытого интереса (чуть выше объёмов) ===
        self.vb_oi = pg.ViewBox(enableMouse=False)
        self.vb_oi.setMenuEnabled(False)
        self.vb_oi.ctrlMenu = None
        self.vb_oi.setBackgroundColor(None)
        self.vb_oi.setXLink(self.vb_price)
        self.plot.scene().addItem(self.vb_oi)
        self.vb_oi.setZValue(self.vb_price.zValue() - 2)
        self.vb_oi.setMouseEnabled(False, False)

        # --- слой объёмов не перехватывает мышь ---
        self.vb_volume.setAcceptHoverEvents(False)
        self.vb_volume.setAcceptedMouseButtons(QtCore.Qt.NoButton)

        # --- слой под свечами ---
        self.vb_volume.setZValue(self.vb_price.zValue() - 1)

        # включаем управление мышью для графика (панорамирование)
        self.vb_price.setMouseEnabled(True, True)
        self.vb_price.setMouseMode(pg.ViewBox.PanMode)

        # включаем управление мышью для графика (ЛКМ — перемещение)
        self.vb_price.setMouseEnabled(True, True); self.vb_price.setMouseMode(pg.ViewBox.PanMode)

        # скрываем нижнюю ось объёмов при старте
        self.plot.getPlotItem().showAxis('bottom', False)
        self._volume_axis_visible = False

        # --- Перекрестие ---
        self.v_line = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen((100, 100, 100), width=1))
        self.h_line = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen((100, 100, 100), width=1))
        self.plot.addItem(self.v_line, ignoreBounds=True)
        self.plot.addItem(self.h_line, ignoreBounds=True)
        self.v_line.hide()
        self.h_line.hide()
        # === Подписи перекрестия на осях ===
        self._axis_label_x = pg.TextItem("", anchor=(0.5, 1), color=(200, 200, 200))
        self._axis_label_y = pg.TextItem("", anchor=(0, 0.5), color=(200, 200, 200))
        # подпись времени живёт в scene, а не в ViewBox
        self.plot.scene().addItem(self._axis_label_x)
        self.plot.scene().addItem(self._axis_label_y)

        self._axis_label_x.hide()
        self._axis_label_y.hide()
        # метка текущей цены на правой оси
        self._axis_label_y_current = pg.TextItem("", anchor=(0, 0.5))
        self.plot.scene().addItem(self._axis_label_y_current)
        self._axis_label_y_current.setZValue(30000)
        self._axis_label_y_current.hide()

        # --- Линия текущей цены (сегмент от свечи до правой оси) ---
        self.current_price_line = pg.PlotCurveItem(
            pen=pg.mkPen(color=(250, 200, 40), width=1, style=QtCore.Qt.DashLine)
        )
        self.current_price_line.setZValue(1500)
        self.plot.addItem(self.current_price_line)
        self.current_price_line.hide()

        # --- Цена на правой оси (используем штатный axis_label_y, без TextItem) ---
        # Ничего НЕ создаём! TextItem больше не нужен.

        # === Информация о свече под курсором ===
        self.candle_info = pg.TextItem("", anchor=(0, 1), color=(200, 255, 200))
        self.candle_info.setZValue(5001)
        self.plot.addItem(self.candle_info)
        self.candle_info.hide()

        # отслеживаем движение курсора
        self.plot.scene().sigMouseMoved.connect(self._on_mouse_move)
        # перехватываем нажатия мыши для линейки
        self.plot.scene().mousePressEvent = self._ruler_mouse_press
        self.plot.scene().mouseReleaseEvent = self._ruler_mouse_release

        lay.addWidget(self.plot)
        # --- КНОПКА СИГНАЛА поверх графика ---
        self.btn_signal = QtWidgets.QPushButton("🔔")
        self.btn_signal.setFixedSize(24, 24)

        # кнопка НЕ переключатель — одноразовое действие
        self.btn_signal.setCheckable(False)

        self.btn_signal.setStyleSheet("""
            QPushButton {
                background-color: #444;
                color: #ddd;
                border: 1px solid #666;
                border-radius: 4px;
                font-size: 11px;
            }
        """)

        # помещаем кнопку поверх графика
        self.btn_signal.setParent(self)
        self.btn_signal.raise_()

        # один клик — включаем режим создания СИГНАЛЬНОЙ ЛИНИИ
        self.btn_signal.clicked.connect(self._activate_single_signal_mode)

        # --- Кнопка горизонтального луча ---
        self.btn_ray = QtWidgets.QPushButton("─")
        self.btn_ray.setFixedSize(24, 24)
        self.btn_ray.setParent(self)
        self.btn_ray.raise_()

        self.btn_ray.setStyleSheet("""
                    QPushButton {
                        background-color: #444;
                        color: #ddd;
                        border: 1px solid #666;
                        border-radius: 4px;
                        font-size: 11px;
                    }
                """)

        self.btn_ray.clicked.connect(self._activate_ray_mode)
        self._ray_mode = False
        self._ray_lines = {}  # { "BTCUSDT": [line1, line2...] }
        # --- Кнопка магнита ---
        self.btn_magnet = QtWidgets.QPushButton("🧲")
        self.btn_magnet.setFixedSize(24, 24)
        self.btn_magnet.setParent(self)
        self.btn_magnet.raise_()
        self.btn_magnet.setCheckable(True)

        self.btn_magnet.setStyleSheet("""
            QPushButton {
                background-color: #444;
                color: #ddd;
                border: 1px solid #666;
                border-radius: 4px;
                font-size: 11px;
            }
            QPushButton:checked {
                background-color: #0088ff;
                color: white;
            }
        """)

        self._magnet_enabled = False
        self.btn_magnet.toggled.connect(lambda st: setattr(self, "_magnet_enabled", st))
        #   Кнопка добавления в избранное
        self.btn_fav = QtWidgets.QPushButton("★")
        self.btn_fav.setFixedSize(24, 24)
        self.btn_fav.setParent(self)
        self.btn_fav.raise_()

        self.btn_fav.setStyleSheet("""
            QPushButton {
                background-color: #444;
                color: #ddd;
                border: 1px solid #666;
                border-radius: 4px;
                font-size: 11px;
            }
            QPushButton:checked {
                background-color: #ffaa00;
                color: black;
            }
        """)

        self.btn_fav.setCheckable(True)
        self.btn_fav.clicked.connect(self._toggle_favorite)
        # --- Кнопка авто-масштаба ---
        self.btn_autorange = QtWidgets.QPushButton("⛶")
        self.btn_autorange.setFixedSize(24, 24)
        self.btn_autorange.setParent(self)
        self.btn_autorange.raise_()

        self.btn_autorange.setStyleSheet("""
                    QPushButton {
                        background-color: #444;
                        color: #ddd;
                        border: 1px solid #666;
                        border-radius: 4px;
                        font-size: 14px;
                    }
                    QPushButton:pressed {
                        background-color: #666;
                    }
                """)

        self.btn_autorange.clicked.connect(self._force_autorange)

        # позиционируем кнопки по центру графика
        def _reposition_btn():
            buttons = [
                self.btn_signal,
                self.btn_ray,
                self.btn_magnet,
                self.btn_autorange,
                self.btn_fav,
            ]

            spacing = 30  # расстояние между кнопками
            total_width = (len(buttons) - 1) * spacing + buttons[0].width()

            w = self.width()
            x0 = int((w - total_width) / 2)

            for i, btn in enumerate(buttons):
                btn.move(x0 + i * spacing, 10)

        self.resizeEvent = lambda e: (
            super(LevelsChart, self).resizeEvent(e),
            _reposition_btn()
        )
        _reposition_btn()

        # --- единый QSettings (один на весь график) ---
        self._settings = QtCore.QSettings("MyCompany", "BinanceScanner")

        self._candles = []
        self._levels = []
        self._level_labels = []
        self._order_lines = {}  # будет: {"BTCUSDT": [(line, price, side)], ...}
        self._signal_mode = False
        # ключ словаря: (symbol, tf) -> список линий (pg.InfiniteLine)
        self._signal_lines: Dict[Tuple[str, str], List] = {}

        self._current_symbol = None  # какой символ сейчас отображается
        # фиксированный заголовок на экране
        self.title_item = QtWidgets.QGraphicsSimpleTextItem("")
        self.title_item.setBrush(QtGui.QColor(0, 255, 0))
        font = QtGui.QFont()
        font.setPixelSize(12)
        self.title_item.setFont(font)

        # добавляем в СЦЕНУ, но поверх всего
        self.plot.scene().addItem(self.title_item)
        self.title_item.setZValue(999999)

        # ставим фиксированную позицию (экранные координаты)
        self.title_item.setPos(10, 10)

        # --- Ссылки на графические элементы, чтобы избежать утечек ---
        self._wick_item = None
        self._up_bars = None
        self._dn_bars = None
        self._fut_vol_item = None
        self._fut_oi_item = None
        self._level_lines = []  # линии уровней
        # --- ЛИНЕЙКА (инструмент измерения) ---
        self._ruler_active = False
        self._ruler_start = None

        self._ruler_line = pg.PlotDataItem([], [], pen=pg.mkPen((230, 180, 20), width=1))
        self._ruler_text = pg.TextItem("", anchor=(0, 1), color=(230, 180, 20))
        self._ruler_line.setZValue(20000)
        self._ruler_text.setZValue(20001)

        self.plot.addItem(self._ruler_line)
        self.plot.addItem(self._ruler_text)
        self._ruler_line.hide()
        self._ruler_text.hide()

        # === ОБОВЛЕНИЕ КАЖДЫЕ 5 МИНУТ ПО ВРЕМЕНИ ПК ===
        if self.tf == "M5":
            self._timer_5m = QtCore.QTimer(self)
            self._timer_5m.timeout.connect(self._on_5min)
        # === ОБНОВЛЕНИЕ КАЖДЫЙ ЧАС ПО ВРЕМЕНИ ПК ===
        if self.tf == "H1":
            self._timer_h1 = QtCore.QTimer(self)
            self._timer_h1.timeout.connect(self._on_h1)
        # ========== ТАЙМЕР ДЛЯ ОБНОВЛЕНИЯ ТЕКУЩЕЙ НЕЗАКРЫТОЙ СВЕЧИ ==========
        self._timer_live_candle = QtCore.QTimer(self)
        self._timer_live_candle.timeout.connect(self._update_live_candle)
        self._timer_live_candle.start(10_000)  # каждые 10 секунд

        self._http_session = None

    async def ws_m5_listener(self):
        """
        WebSocket: обновляет ТОЛЬКО текущую M5-свечу.
        Закрытые свечи игнорируются — их чинит _on_5min.
        """

        # подписка на ВСЕ спот-символы
        streams = "/".join(
            f"{s.lower()}@kline_5m" for s in self.pane.mw.spot_syms
        )
        url = f"wss://stream.binance.com:9443/stream?streams={streams}"

        while True:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    async for msg in ws:
                        data = json.loads(msg)
                        k = data["data"]["k"]

                        # ❗ закрытые свечи НЕ трогаем
                        if k["x"]:
                            continue

                        sym = k["s"]

                        candle = {
                            "time": int(k["t"]),
                            "open": float(k["o"]),
                            "high": float(k["h"]),
                            "low": float(k["l"]),
                            "close": float(k["c"]),
                            "volume": float(k["v"]),
                        }

                        # обновляем ТОЛЬКО последнюю свечу
                        merge_last_candle(CANDLES_M5, sym, candle)

                        # если символ выбран — обновляем график
                        if (
                                self._current_symbol == sym
                                and self.tf == "M5"
                        ):
                            QtCore.QTimer.singleShot(
                                0,
                                lambda c=candle: self.update_last_candle_only(c)
                            )

            except Exception as e:
                print("[WS ERROR]", e)
                await asyncio.sleep(5)

    async def ws_h1_listener(self):
        """
        WebSocket: обновляет ТОЛЬКО текущую H1 свечу.
        Закрытые свечи игнорируются — их корректирует _on_h1.
        """

        streams = "/".join(
            f"{s.lower()}@kline_1h" for s in self.pane.mw.spot_syms
        )
        url = f"wss://stream.binance.com:9443/stream?streams={streams}"

        while True:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    async for msg in ws:
                        data = json.loads(msg)
                        k = data["data"]["k"]

                        # закрытую свечу игнорируем
                        if k["x"]:
                            continue

                        sym = k["s"]

                        candle = {
                            "time": int(k["t"]),
                            "open": float(k["o"]),
                            "high": float(k["h"]),
                            "low": float(k["l"]),
                            "close": float(k["c"]),
                            "volume": float(k["v"]),
                        }

                        merge_last_candle(CANDLES_H1, sym, candle)

                        if (
                                self._current_symbol == sym
                                and self.tf == "H1"
                        ):
                            QtCore.QTimer.singleShot(
                                0,
                                lambda c=candle: self.update_last_candle_only_h1(c)
                            )

            except Exception as e:
                print("[WS H1 ERROR]", e)
                await asyncio.sleep(5)

    def _force_autorange(self):
        """Принудительный авто-масштаб графика"""
        self._auto_range_done = False
        self._redraw()

    def _toggle_favorite(self):
        if not self._current_symbol:
            return

        sym = self._current_symbol

        # обновляем данные избранного
        if self.btn_fav.isChecked():
            self.pane.add_to_favorites(sym)
        else:
            self.pane.remove_from_favorites(sym)

        # --- обновить кнопку на другом графике ---
        try:
            other = (
                self.pane.chartPanel_m5
                if self is self.pane.chartPanel
                else self.pane.chartPanel
            )
            if other._current_symbol == sym:
                other.btn_fav.setChecked(self.btn_fav.isChecked())
        except:
            pass

        # --- обновить левый список ---
        try:
            self.pane.refresh_fav_icons()
        except:
            pass

    def request_sync(self):
        """Отложенная синхронизация — максимум 1 раз в 120 мс."""
        if getattr(self, "_sync_pending", False):
            return
        self._sync_pending = True
        QtCore.QTimer.singleShot(120, self._do_sync)

    def _do_sync(self):
        self._sync_pending = False
        self._sync_volumes_geometry()

    def set_data(self, candles: List[dict], levels: List[dict], symbol: str = None):
        # import inspect
        # import time
        #
        # caller = inspect.stack()[1]
        # print(
        #     f"[SET_DATA_CALL] "
        #     f"{time.strftime('%Y-%m-%d %H:%M:%S')} | "
        #     f"tf={self.tf} "
        #     f"sym={self._current_symbol} | "
        #     f"from={caller.function} "
        #     f"({caller.filename.split('/')[-1]}:{caller.lineno})"
        # )

        self._candles = candles or []
        self._levels = levels or []
        if symbol:
            self._current_symbol = symbol
            if symbol in self.pane.favorites:
                self.btn_fav.setChecked(True)
            else:
                self.btn_fav.setChecked(False)

        # --- УДАЛЯЕМ ВСЕ ЛИНИИ И ДОБАВЛЯЕМ ТОЛЬКО ТЕКУЩЕГО СИМВОЛА ---
        try:
            # удалить все
            for sym in self._order_lines:
                for line, _, _ in self._order_lines[sym]:
                    self.plot.removeItem(line)
            # --- Удаляем объекты сигнальных линий для ЧУЖИХ символов (только наш TF) ---
            for (sym, tf), lines in list(self._signal_lines.items()):
                if tf == self.tf and sym != self._current_symbol:
                    for line in lines:
                        for obj in (getattr(line, "_hitbox", None), line):
                            if obj is None:
                                continue
                            try:
                                self.vb_price.removeItem(obj)
                            except:
                                try:
                                    self.plot.removeItem(obj)
                                except:
                                    pass

            # --- Добавляем обратно ТОЛЬКО линии текущего символа/TF (линия, хитбокс, маркер) ---
            for line in self._signal_lines.get((self._current_symbol, self.tf), []):
                try:
                    # линия — добавляем ТОЛЬКО если не в сцене
                    if line.scene() is None:
                        self.vb_price.addItem(line)

                    # хитбокс
                    hb = getattr(line, "_hitbox", None)
                    if hb is not None and hb.scene() is None:
                        self.vb_price.addItem(hb)

                    # маркер (если есть)
                    # маркер — НИКОГДА не добавляем вручную
                    # он дочерний элемент линии
                    pass

                except:
                    pass

            # --- удалить ВСЕ старые лучи ---
            for sym, items in self._ray_lines.items():
                for visible, hitbox in items:
                    try:
                        self.plot.removeItem(visible)
                    except:
                        pass
                    try:
                        self.plot.removeItem(hitbox)
                    except:
                        pass

            # добавить обратно только текущего символа
            if self._current_symbol in self._order_lines:
                for line, _, _ in self._order_lines[self._current_symbol]:
                    self.plot.addItem(line)

            # вернуть лучи текущего символа
            for visible, hitbox in self._ray_lines.get(self._current_symbol, []):
                try:
                    if visible not in self.plot.items():
                        self.plot.addItem(visible)
                    if hitbox not in self.plot.items():
                        self.plot.addItem(hitbox)
                except:
                    pass



        except:
            pass

        # ---- избегаем полной перерисовки, если данные не поменялись ----
        levels_sig = tuple((round(lv.get("price", 0), 10), lv.get("side")) for lv in self._levels)
        candles_len = len(self._candles)

        if self._candles:
            last = self._candles[-1]
            last_candle_sig = (
                last["time"],
                last["open"],
                last["high"],
                last["low"],
                last["close"],
                last.get("volume"),
            )
        else:
            last_candle_sig = None

        self._last_levels_sig = levels_sig
        self._last_candles_len = candles_len
        self._last_candle_sig = last_candle_sig
        # print(
        #     f"[SET_DATA] "
        #     f"{time.strftime('%Y-%m-%d %H:%M:%S')} | "
        #     f"tf={self.tf} "
        #     f"sym={self._current_symbol} "
        #     f"candles={len(self._candles)} "
        #     f"last={time.strftime('%H:%M:%S', time.localtime(self._candles[-1]['time'] / 1000)) if self._candles else 'None'}"
        # )

        self._redraw()
        # загрузить лучи только после очистки и перерисовки
        self.load_rays()
        # восстановление _visible_y, если пропал
        for line in self._signal_lines.get((self._current_symbol, self.tf), []):
            if not hasattr(line, "_visible_y"):
                try:
                    ys = line.getData()[1]
                    if ys:
                        line._visible_y = float(ys[0])
                except:
                    pass

        # --- теперь действительно можно восстановить маркеры ---
        for line in self._signal_lines.get((self._current_symbol, self.tf), []):
            if getattr(line, "_marker", None) is None:
                self._restore_marker(line)

        # отключаем авто-синхронизацию по X со вторым графиком
        try:
            self.plot.getViewBox().setXLink(None)
        except:
            pass

    def _restore_marker(self, visible):
        pass

    def _redraw(self):
        # Throttle full redraw to at most 4 Hz to avoid UI freezes.
        now = time.time()
        if not hasattr(self, "_last_full_redraw"):
            self._last_full_redraw = 0.0
        if now - self._last_full_redraw < 0.25:
            return
        self._last_full_redraw = now

        """Полная отрисовка графика с восстановлением уровней, без утечек и лагов."""

        # --- удаляем только то, что мы сами создавали ---
        for attr in ("_wick_item", "_up_bars", "_dn_bars"):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    self.plot.removeItem(obj)
                except Exception:
                    pass
                setattr(self, attr, None)

        for ln in getattr(self, "_level_lines", []):
            try:
                self.plot.removeItem(ln)
            except Exception:
                pass
        self._level_lines = []

        for lbl in getattr(self, "_level_labels", []):
            try:
                self.plot.removeItem(lbl)
            except Exception:
                pass
        self._level_labels = []

        # --- перекрестие ---
        if self.v_line not in self.plot.items():
            self.plot.addItem(self.v_line, ignoreBounds=True)
        if self.h_line not in self.plot.items():
            self.plot.addItem(self.h_line, ignoreBounds=True)

        if not self._candles:
            self.plot.showAxis('bottom', False)
            self.plot.showAxis('right', False)
            return

        # === Ограничение числа баров ===
        settings = QSettings("MyCompany", "BinanceScanner")
        max_bars = settings.value(
            "chart_h1_bars" if self.tf == "H1" else "chart_m5_bars",
            240, type=int
        )
        candles = self._candles[-max_bars:]
        if not candles:
            return

        # --- координаты времени ---
        if self.tf == "H1":
            xs = [int(c["time"] / 3600000) * 3600 for c in candles]
            self._xs = xs
            step = 3600
        else:
            xs = [int(c["time"] / 300000) * 300 for c in candles]
            self._xs = xs
            step = 300
        body_w = step * 0.7
        # === ВСТАВИТЬ СЮДА ===
        self._bars_count = len(xs)
        self._current_span = xs[-1] - xs[0] if len(xs) > 1 else 0
        # ======================

        # --- свечи ---
        wick_x, wick_y = [], []
        up_x, up_h, up_y0 = [], [], []
        dn_x, dn_h, dn_y0 = [], [], []
        for t, c in zip(xs, candles):
            o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
            wick_x += [t, t, np.nan]
            wick_y += [l, h, np.nan]
            top, bot = max(o, cl), min(o, cl)
            height = max(top - bot, 1e-12)
            if cl >= o:
                up_x.append(t)
                up_h.append(height)
                up_y0.append(bot)
            else:
                dn_x.append(t)
                dn_h.append(height)
                dn_y0.append(bot)

        wick_pen = pg.mkPen(180, 180, 180, 200)
        up_brush = pg.mkBrush(210, 210, 210, 255)
        dn_brush = pg.mkBrush(QtGui.QColor("#1b1f22"))
        border_pen = pg.mkPen(180, 180, 180, 255)

        self._wick_item = pg.PlotDataItem(wick_x, wick_y, pen=wick_pen)
        self.plot.addItem(self._wick_item)

        if up_x:
            self._up_bars = pg.BarGraphItem(
                x=up_x, height=up_h, width=body_w, y0=up_y0,
                brush=up_brush, pen=border_pen)
            self.plot.addItem(self._up_bars)

        if dn_x:
            self._dn_bars = pg.BarGraphItem(
                x=dn_x, height=dn_h, width=body_w, y0=dn_y0,
                brush=dn_brush, pen=border_pen)
            self.plot.addItem(self._dn_bars)

        # --- Обновление объёмов и OI ---
        self._update_futures_layers(candles, body_w)

        # --- уровни ---
        for lbl in getattr(self, "_level_labels", []):
            try:
                self.plot.removeItem(lbl)
            except Exception:
                pass
        self._level_labels = []

        last_t = xs[-1]
        for lv in self._levels:
            y = lv["price"]
            side = lv["side"]
            i1 = lv["i1"]

            if i1 < 0:
                x1 = xs[0]
            else:
                x1 = xs[min(i1, len(xs) - 1)]

            x2 = last_t

            # линия уровня
            pen = pg.mkPen((224, 58, 58), width=1)
            line = pg.PlotCurveItem([x1, x2], [y, y], pen=pen)
            line.setZValue(1000)
            self.plot.addItem(line)
            self._level_lines.append(line)

            # --- корректировка цены по шагу фьючерса ---
            symbol = self._current_symbol
            tick_key = f"Spot:{symbol}"
            tick_str = tick_sizes.get(tick_key, None)

            if tick_str:
                step = float(tick_str)
                y_adj = round_to_step(y, step)
                decimals = len(tick_str.split(".")[1])
                txt = f"{y_adj:.{decimals}f}"
            else:
                # если шага нет – используем старый вывод
                txt = (
                    f"{y:.2f}" if y >= 1
                    else f"{y:.4f}" if y >= 0.001
                    else f"{y:.8f}"
                )

            label = pg.TextItem(txt, color=(160, 160, 160))  # тёмно-серый
            label.setFont(QtGui.QFont("Segoe UI", 8))

            # support -> подпись НИЖЕ линии (sup)
            # resistance -> подпись ВЫШЕ линии (res)
            if side == "sup":
                # anchor чуть выше точки, поэтому текст окажется ниже линии
                label.setAnchor((0, -0.05))
            else:
                # anchor чуть ниже точки, поэтому текст окажется выше линии
                label.setAnchor((0, 1.05))

            label.setPos(x1, y)
            label.setZValue(1001)

            self.plot.addItem(label)
            self._level_labels.append(label)

        # --- ордер-линии ---
        cur = getattr(self, "_current_symbol", None)
        if cur in self._order_lines:
            for line, price, side in self._order_lines[cur]:
                if line not in self.plot.items():
                    self.plot.addItem(line, ignoreBounds=True)

        # --- масштаб ---
        vb = self.plot.getViewBox()

        if not self._auto_range_done:
            span = xs[-1] - xs[0]
            right_pad = span * 0.05
            self.plot.setXRange(xs[0], xs[-1] + right_pad, padding=0)

            price_min = min(c["low"] for c in candles)
            price_max = max(c["high"] for c in candles)
            margin_bottom = (price_max - price_min) * 0.20
            vb.setYRange(price_min - margin_bottom, price_max * 1.02, padding=0)

            self._auto_range_done = True

        self.plot.showAxis('bottom', True)
        self.plot.showAxis('right', True)

        # --- ПЕРЕОТРИСОВКА ЛУЧЕЙ ---
        cur = self._current_symbol
        if cur in self._ray_lines and hasattr(self, "_xs") and self._xs:
            x_end = self._xs[-1] + (self._xs[-1] - self._xs[0]) * 2

            for visible, hitbox in self._ray_lines[cur]:
                ts = getattr(visible, "_ray_ts", None)
                price = getattr(visible, "_ray_price", None)
                if ts is None or price is None:
                    continue

                hitbox.prepareGeometryChange()

                visible.setData([ts, x_end], [price, price])
                hitbox.setData([ts, x_end], [price, price])

        QtCore.QTimer.singleShot(0, self._update_current_price_line)

        self.request_sync()

    def _sync_volumes_geometry(self):
        """Корректно синхронизирует слои объёмов и OI, без перекрытий и вспуханий."""
        try:
            rect = self.vb_price.sceneBoundingRect()
            if not rect.isValid():
                return

            # --- слой объёмов: нижние 10% ---
            h_vol = rect.height() * 0.15
            y_vol = rect.y() + rect.height() - h_vol
            self.vb_volume.setGeometry(QtCore.QRectF(rect.x(), y_vol, rect.width(), h_vol))
            self.vb_volume.linkedViewChanged(self.vb_price, self.vb_volume.XAxis)

            # --- слой OI: чуть выше объёмов, занимает 10–15% высоты ---
            h_oi = rect.height() * 0.06  # было 0.01 — в 12 раз выше
            y_oi = y_vol - h_oi * 1.0  # чуть выше объёмов
            self.vb_oi.setGeometry(QtCore.QRectF(rect.x(), y_oi, rect.width(), h_oi))

        except Exception as e:
            print("sync_volumes_geometry error:", e)

    def _update_current_price_line(self):
        if not self._candles or not hasattr(self, "_xs") or not self._xs:
            self.current_price_line.hide()
            self._axis_label_y_current.hide()
            return

        # текущая цена
        last = self._candles[-1]
        price = last["close"]

        # шаг цены
        symbol = self._current_symbol
        tick_key = f"Spot:{symbol}"
        tick_str = tick_sizes.get(tick_key, "0.0001")
        step = float(tick_str)
        decimals = len(tick_str.split(".")[1])

        price = round_to_step(price, step)

        # X текущей свечи
        x_candle = self._xs[-1]

        vb = self.plot.getViewBox()
        (x_min, x_max), (y_min, y_max) = vb.viewRange()

        # --- 1) ЛИНИЯ ---
        xs = [x_candle, x_max]
        ys = [price, price]

        self.current_price_line.setData(xs, ys)
        self.current_price_line.setZValue(10_000)
        self.current_price_line.show()

        # --- 2) ПОДПИСЬ ЦЕНЫ (ОТДЕЛЬНАЯ МЕТКА) ---
        label = self._axis_label_y_current

        txt = f"{price:.{decimals}f}"

        label.setHtml(
            f"<span style='background-color:#1b1f22;"
            "color: rgb(250,200,40); padding:2px 6px; border-radius:4px;'>"
            f"{txt}</span>"
        )

        axis = self.plot.getPlotItem().getAxis('right')
        axis_rect = axis.mapRectToScene(axis.boundingRect())
        axis_x = axis_rect.left() + 6

        scene_y = vb.mapViewToScene(QtCore.QPointF(x_candle, price)).y()

        label.setPos(axis_x, scene_y)
        label.setZValue(20000)
        label.show()

    def _update_live_candle(self):
        symbol = self._current_symbol
        if not symbol:
            return

        # стартуем асинхронную работу
        asyncio.create_task(self._job_update_live(symbol))

    def _update_futures_layers(self, candles, body_w):
        """Отрисовка объёмов и OI с переиспользованием объектов, без лагов и вспухания."""
        if not self._current_symbol:
            return

        fut_candles = (
            CANDLES_H1_FUT.get(self._current_symbol)
            if self.tf.upper() == "H1"
            else CANDLES_M5_FUT.get(self._current_symbol)
        )
        if not fut_candles:
            return

        fut_candles = fut_candles[-len(candles):]

        # --- собираем данные ---
        fut_vols = [c.get("volume", 0.0) for c in fut_candles]
        fut_oi = [c.get("open_interest", 0.0) for c in fut_candles]
        xs = [c["time"] / 1000.0 for c in fut_candles]

        # --- отложенная перерисовка, чтобы не блокировать GUI ---
        QtCore.QTimer.singleShot(0, lambda: self._render_futures_layers(xs, fut_vols, fut_oi, body_w))
        self.request_sync()

    def _render_futures_layers(self, xs, fut_vols, fut_oi, body_w):
        """Рисуем объёмы как бары и OI как сглаженную линию, с реальной динамикой."""
        if not xs or not fut_vols:
            return

        xs_f = np.array(xs)
        bar_width = (xs_f[1] - xs_f[0]) * 0.8 if len(xs_f) > 1 else 60

        # === ОБЪЁМЫ: нормализация по ВИДИМОМУ диапазону ===
        # получаем видимые границы по X
        (xmin, xmax), _ = self.vb_price.viewRange()

        # находим индексы баров, которые попадают в окно
        xs_f = np.array(xs)  # уже есть, но можно оставить
        visible_idx = np.where((xs_f >= xmin) & (xs_f <= xmax))[0]

        # вычисляем максимум только по видимому диапазону
        if len(visible_idx) > 0:
            local_max = max(fut_vols[i] for i in visible_idx) or 1
        else:
            local_max = max(fut_vols) or 1

        # нормализуем объёмы ПО ВИДИМЫМ ДАННЫМ
        vol_scaled = [v / local_max for v in fut_vols]

        if getattr(self, "_fut_vol_item", None) is None:
            bars = pg.BarGraphItem(
                x=xs_f,
                height=vol_scaled,
                width=bar_width,
                y0=0,
                brush=pg.mkBrush(80, 140, 255, 180),
                pen=pg.mkPen((50, 90, 180), width=0.5),
            )
            self.vb_volume.addItem(bars)
            self._fut_vol_item = bars
        else:
            self._fut_vol_item.setOpts(x=xs_f, height=vol_scaled, width=bar_width, y0=0)

        self.vb_volume.setYRange(0, 1.0, padding=0)

        # --- OPEN INTEREST ---
        if fut_oi and any(fut_oi):
            # Заполняем None последним известным значением
            oi_filled = []
            last_val = None
            for v in fut_oi:
                if v is not None and v > 0:
                    last_val = v
                oi_filled.append(last_val if last_val is not None else 0)

            oi_arr = np.array(oi_filled, dtype=float)

            if len(oi_arr) < 3 or np.max(oi_arr) == 0:
                return

            # === OI: нормализация по ВИДИМОЙ области ===
            (xmin, xmax), _ = self.vb_price.viewRange()

            xs_oi_arr = np.array(xs[:len(oi_arr)])
            visible_idx_oi = np.where((xs_oi_arr >= xmin) & (xs_oi_arr <= xmax))[0]

            if len(visible_idx_oi) > 0:
                vis_values = oi_arr[visible_idx_oi]
                local_min = vis_values.min()
                local_max = vis_values.max()
            else:
                local_min = oi_arr.min()
                local_max = oi_arr.max()

            # если OI почти не менялся → слегка растягиваем диапазон
            if abs(local_max - local_min) < 1e-9:
                local_max = local_min + 1.0

            # нормализуем по видимому диапазону
            oi_scaled = (oi_arr - local_min) / (local_max - local_min)

            xs_oi = xs[:len(oi_scaled)]

            # --- масштабируем OI в те же координаты, что и объёмы ---
            # (так, чтобы 0 совпадал с нулём объёмов)
            oi_aligned = oi_scaled * 1.0  # масштаб динамики
            # никаких сдвигов, оба слоя начинаются с 0

            # создаём или обновляем линию
            if getattr(self, "_fut_oi_item", None) is None:
                pen = pg.mkPen((255, 200, 70, 220), width=1.3)
                curve = pg.PlotCurveItem(xs_oi, oi_aligned, pen=pen, antialias=True)
                self.vb_volume.addItem(curve)  # 👈 добавляем линию в vb_volume, не в vb_oi!
                self._fut_oi_item = curve
            else:
                self._fut_oi_item.setData(xs_oi, oi_aligned)

            # уравниваем диапазон с объёмами
            self.vb_volume.setYRange(0, 1.0, padding=0)

    def _on_mouse_move(self, evt):
        now = time.time()
        if now - getattr(self, "last_move_time", 0) < 0.016:  # ограничение 60 FPS
            return
        self.last_move_time = now

        if not self._candles:
            self.v_line.hide()
            self.h_line.hide()
            return

        pos = evt
        vb = self.plot.getViewBox()
        # используем именно область ценового viewbox
        if not vb.sceneBoundingRect().contains(pos):
            self.v_line.hide()
            self.h_line.hide()
            return

        try:
            mouse_point = vb.mapSceneToView(pos)
            x, y = mouse_point.x(), mouse_point.y()
            # ==== ПОКАЗЫВАЕМ ДАННЫЕ СВЕЧИ ====
            xs = []
            if self.tf == "H1":
                xs = [int(c["time"] / 3600000) * 3600 for c in self._candles]
            else:
                xs = [int(c["time"] / 300000) * 300 for c in self._candles]

            if xs:
                # ищем ближайшую свечу по X
                idx = min(range(len(xs)), key=lambda i: abs(xs[i] - x))
                c = self._candles[idx]
                cx = xs[idx]

                # шаг таймфрейма
                step = 3600 if self.tf == "H1" else 300

                # границы свечи в координатах данных
                x_left = cx - step * 0.35
                x_right = cx + step * 0.35

                # границы свечи по Y — ВКЛЮЧАЯ ФИТИЛИ
                candle_top = c["high"]
                candle_bottom = c["low"]

                # проверка: курсор над полной свечой
                inside = (x_left <= x <= x_right) and (candle_bottom <= y <= candle_top)

                if not inside:
                    # скрываем ВСЁ
                    self.candle_info.hide()
                    self.title_item.setText("")  # <── скрываем название монеты
                else:
                    # показываем popup
                    # объём в USDT
                    # берём фьючерсные свечи
                    if self.tf == "H1":
                        fut_list = CANDLES_H1_FUT.get(self._current_symbol, [])
                    else:
                        fut_list = CANDLES_M5_FUT.get(self._current_symbol, [])

                    # --- ИЩЕМ ФЬЮЧЕРСНУЮ СВЕЧУ ПО TIMESTAMP ---
                    cur_time = c["time"]

                    # точное совпадение по времени
                    fut_match = next((fc for fc in fut_list if fc.get("time") == cur_time), None)

                    # если точного совпадения нет — ищем ближайшую по времени
                    if fut_match is None and fut_list:
                        fut_match = min(fut_list, key=lambda fc: abs(fc.get("time", 0) - cur_time))

                    if fut_match:
                        fut_vol = float(fut_match.get("volume", 0))
                    else:
                        fut_vol = 0.0

                    # перевод объёма в USDT (фьючерсный объём × close спота)
                    close_price = float(c["close"])
                    vol_usdt = fut_vol * close_price

                    # компактный формат:
                    # до 1 млн → тысячи
                    # >1 млн → K
                    # >1 млрд → M
                    if vol_usdt >= 1_000_000_000:
                        vol_fmt = f"{vol_usdt / 1_000_000_000:.1f}M"
                    elif vol_usdt >= 1_000_000:
                        vol_fmt = f"{vol_usdt / 1_000_000:.1f}K"
                    elif vol_usdt >= 1_000:
                        vol_fmt = f"{vol_usdt / 1000:.0f}"
                    else:
                        vol_fmt = f"{int(vol_usdt)}"

                    # форматируем OI так же, как объем
                    # ищем соответствующую фьючерсную свечу
                    if self.tf == "H1":
                        fut_list = CANDLES_H1_FUT.get(self._current_symbol, [])
                    else:
                        fut_list = CANDLES_M5_FUT.get(self._current_symbol, [])

                    if fut_match:
                        oi = float(fut_match.get("open_interest", 0))
                    else:
                        oi = 0.0

                    if oi >= 1_000_000_000:
                        oi_fmt = f"{oi / 1_000_000_000:.1f}M"
                    elif oi >= 1_000_000:
                        oi_fmt = f"{oi / 1_000_000:.1f}K"
                    elif oi >= 1_000:
                        oi_fmt = f"{oi / 1000:.0f}"
                    else:
                        oi_fmt = f"{int(oi)}"

                    txt = (
                        f"O: {c['open']}<br>"
                        f"H: {c['high']}<br>"
                        f"L: {c['low']}<br>"
                        f"C: {c['close']}<br>"
                        f"V: {vol_fmt}<br>"
                        f"OI: {oi_fmt}"
                    )

                    dt = time.strftime("%Y-%m-%d %H:%M", time.localtime(cx))

                    # показываем заголовок (символ + данные свечи)
                    self.title_item.setText(
                        f"{self._current_symbol}   |   {dt}   |   "
                        f"O {c['open']}  H {c['high']}  L {c['low']}  C {c['close']}  "
                        f"V {vol_fmt}  OI {oi_fmt}"
                    )

                    self.candle_info.setHtml(
                        f"<div style='background-color:#1b1f22; padding:4px 6px; "
                        f"border-radius:4px; font-size:10pt;'>{txt}</div>"
                    )

                    sp = vb.mapViewToScene(QtCore.QPointF(x, y))
                    self.candle_info.setPos(sp.x() + 10, sp.y() - 10)
                    self.candle_info.show()

            (x_min, x_max), (y_min, y_max) = vb.viewRange()
            if not (x_min <= x <= x_max and y_min <= y <= y_max):
                self.v_line.hide()
                self.h_line.hide()
                return

            # === ОБНОВЛЕНИЕ ПЕРЕКРЕСТИЯ ===
            self.v_line.setPos(x)

            # магнитная логика — смещаем горизонтальную линию
            if self._magnet_enabled:
                snapped = self._snap_price(y, x)
                self.h_line.setPos(snapped)
            else:
                self.h_line.setPos(y)

            if not self.v_line.isVisible():
                self.v_line.show()
                self.h_line.show()

            # --- обновление линейки ---
            if self._ruler_active and self._ruler_start:
                x0, y0 = self._ruler_start

                if not self._ruler_line.isVisible():
                    self._ruler_line.show()
                    self._ruler_text.show()

                self._ruler_line.setData([x0, x], [y0, y])

                dx = x - x0
                dy = y - y0
                step = 3600 if self.tf == "H1" else 300
                candles = int(round(abs(dx) / step))
                pct = (dy / y0 * 100) if y0 else 0

                self._ruler_text.setHtml(
                    "<div style='background-color:#1b1f22;"
                    "padding:4px 6px; border-radius:4px; "
                    "font-size:10pt;'>"
                    f"<span style='color: rgb(230,180,20);'>"
                    f"Свечи: {candles}<br>Процент: {pct:+.2f}%"
                    "</span></div>"
                )

                self._ruler_text.setPos(x, y)
            # === ПОДПИСИ НА ОСЯХ ДЛЯ ПЕРЕКРЕСТИЯ ===

            # --- подпись времени ПРЯМО ПОД СВЕЧАМИ ---
            try:
                dt = time.strftime("%Y-%m-%d %H:%M", time.localtime(x))
                self._axis_label_x.setHtml(
                    f"<span style='background-color:#1b1f22;"
                    "color:#78B4FF; padding:2px 6px; border-radius:4px;'>"
                    f"{dt}</span>"
                )

                # X-координата курсора → в сцену
                scene_x = vb.mapViewToScene(QtCore.QPointF(x, y)).x()

                # нижняя граница свечного ViewBox (а НЕ настоящая ось!)
                (_, _), (y_min, y_max) = vb.viewRange()

                # переносим эту координату в сцену
                y_bottom_scene = vb.mapViewToScene(QtCore.QPointF(0, y_min)).y()

                # немного сдвигаем вниз (чтобы текст не касался свечи)
                self._axis_label_x.setPos(scene_x, y_bottom_scene + 22)

                self._axis_label_x.show()
            except Exception:
                self._axis_label_x.hide()

            # --- подпись цены на самой оси Y ---
            try:
                symbol = self._current_symbol
                tick_key = f"Spot:{symbol}"
                tick = tick_sizes.get(tick_key)

                if tick:
                    dec = len(tick.split(".")[1])
                else:
                    dec = 4

                price_txt = f"{y:.{dec}f}"

                # фон и текст как у осей
                self._axis_label_y.setHtml(
                    f"<span style='background-color:#1b1f22;"
                    "color:#78B4FF; padding:2px 6px; border-radius:4px;'>"
                    f"{price_txt}</span>"
                )

                # координата X позиции правой оси
                axis = self.plot.getPlotItem().getAxis('right')
                axis_rect = axis.mapRectToScene(axis.boundingRect())
                axis_x = axis_rect.left() + 6  # чуть левее оси

                # переводим координату Y на сцену
                scene_y = vb.mapViewToScene(QtCore.QPointF(x, y)).y()

                # ставим подпись на уровне Y прямо на оси
                self._axis_label_y.setPos(axis_x, scene_y)
                self._axis_label_y.show()
            except:
                self._axis_label_y.hide()


        except Exception:
            pass

    def _ruler_mouse_press(self, ev):
        # --- СОХРАНЯЕМ ПОЗИЦИЮ КУРСОРА ДЛЯ СИГНАЛЬНОЙ ЛИНИИ ---
        vb = self.plot.getViewBox()
        self._last_click_pos = vb.mapSceneToView(ev.scenePos())
        # -----------------------------------------------------
        # === РЕЖИМ РИСОВАНИЯ ЛУЧА ===
        if self._ray_mode and ev.button() == QtCore.Qt.LeftButton:
            pos = ev.scenePos()
            vb = self.plot.getViewBox()
            p = vb.mapSceneToView(pos)
            price = p.y()

            ts = p.x()

            # магнит
            if self._magnet_enabled:
                price = self._snap_price(price, ts)

            self._creating_ray_now = True
            self.add_ray(self._current_symbol, price, ts)
            self._creating_ray_now = False

            self._ray_mode = False
            self.btn_ray.setStyleSheet("""
                QPushButton { background-color:#444; color:#ddd;
                border:1px solid #666; border-radius:4px; }
            """)

            ev.accept()
            return

        # === режим установки сигнала ===
        if self._signal_mode and ev.button() == QtCore.Qt.LeftButton:
            pos = ev.scenePos()
            vb = self.plot.getViewBox()
            p = vb.mapSceneToView(pos)

            # ← ВСТАВЛЯЕШЬ ЭТО
            price = self._snap_price(p.y(), p.x()) if self._magnet_enabled else p.y()

            try:
                self.pane._clicked_tf = self.tf
                self.pane.add_signal(self._current_symbol, price)

            except:
                pass

            # Отключаем РЕЖИМ после одной линии
            self._signal_mode = False

            # возвращаем нормальный стиль кнопки
            self.btn_signal.setStyleSheet("""
                QPushButton {
                    background-color: #444;
                    color: #ddd;
                    border: 1px solid #666;
                    border-radius: 4px;
                    font-size: 11px;
                }
            """)

            ev.accept()
            return

        # === конец вставки ===

        if ev.button() == QtCore.Qt.MiddleButton:
            pos = ev.scenePos()
            vb = self.plot.getViewBox()
            p = vb.mapSceneToView(pos)

            self._ruler_active = True
            self._ruler_start = (p.x(), p.y())

            # НЕ показываем линию сразу
            self._ruler_line.hide()
            self._ruler_text.hide()

            ev.accept()
        else:
            pg.GraphicsScene.mousePressEvent(self.plot.scene(), ev)

    def _snap_price(self, price, x):
        if not self._candles:
            return price

        if self.tf == "H1":
            xs = [int(c["time"] / 3600000) * 3600 for c in self._candles]
        else:
            xs = [int(c["time"] / 300000) * 300 for c in self._candles]

        # ближайшая свеча
        idx = min(range(len(xs)), key=lambda i: abs(xs[i] - x))
        c = self._candles[idx]

        # уровни для притяжения
        levels = [c["high"], c["low"], c["open"], c["close"]]

        # ищем ближайший уровень
        snapped = min(levels, key=lambda v: abs(v - price))
        dist = abs(snapped - price)

        # СИЛА МАГНИТА ДЛЯ РАЗНЫХ ТАЙМФРЕЙМОВ
        if self.tf == "H1":
            MAGNET_THRESHOLD = price * 0.05  # сильный магнит на H1 (5%)
        else:
            MAGNET_THRESHOLD = price * 0.025  # слабый магнит на M5 (1%)

        # если слишком далеко — НЕ тянем!
        if dist > MAGNET_THRESHOLD:
            return price

        return snapped

    def add_ray(self, symbol, price, ts=None):
        # --- координаты времени (timestamp) ---
        if self.tf == "H1":
            xs = [int(c["time"] / 3600000) * 3600 for c in self._candles]
        else:
            xs = [int(c["time"] / 300000) * 300 for c in self._candles]

        if not xs:
            return

        # если ts не передан — ставим на последнюю свечу
        if ts is None:
            ts = xs[-1]

        # X всегда вычисляем из timestamp
        x0 = ts
        x1 = xs[-1] + (xs[-1] - xs[0]) * 2

        visible = pg.PlotCurveItem(
            [x0, x1],
            [price, price],
            pen=pg.mkPen((0, 180, 255), width=1)
        )
        visible.setZValue(5001)

        # толстый прозрачный хитбокс
        hitbox = pg.PlotCurveItem(
            [x0, x1],
            [price, price],
            pen=pg.mkPen((0, 0, 0, 0), width=12)
        )
        hitbox.setZValue(5000)

        # --- ВАЖНО: делаем РЕАЛЬНО толстый hit-area ---
        def wide_shape():
            path = pg.PlotCurveItem.shape(hitbox)

            vb = hitbox.getViewBox()
            if vb is None:
                return path

            # 12 пикселей в координатах данных по Y
            (_, _), (y_min, y_max) = vb.viewRange()
            h_pixels = vb.height()
            if h_pixels <= 0:
                return path

            units_per_pixel = (y_max - y_min) / h_pixels
            width_in_data = units_per_pixel * 12  # ← 12px

            stroker = QPainterPathStroker()
            stroker.setWidth(width_in_data)
            return stroker.createStroke(path)

        hitbox.shape = wide_shape

        # --- разрешаем события мыши ---
        hitbox.setAcceptHoverEvents(True)
        hitbox.setAcceptedMouseButtons(
            QtCore.Qt.LeftButton | QtCore.Qt.RightButton
        )

        # 🔑 ВАЖНО: сообщаем pyqtgraph, что item интерактивный
        hitbox.setClickable(True)

        # --- метаданные (ВАЖНО) ---
        visible._symbol = symbol
        hitbox._symbol = symbol

        visible._ray_ts = ts
        visible._ray_price = price

        hitbox._visible = visible
        visible._hitbox = hitbox

        # --- drag на hitbox ---
        def _drag(ev):
            vb = self.plot.getViewBox()

            if ev.isStart():
                hitbox._dragging = True
                hitbox._start_pos = vb.mapSceneToView(ev.scenePos())
                hitbox._start_ts = visible._ray_ts
                hitbox._start_price = visible._ray_price
                ev.accept()
                return

            if ev.isFinish():
                hitbox._dragging = False
                self.save_rays()
                ev.accept()
                return

            if not getattr(hitbox, "_dragging", False):
                ev.ignore()
                return

            pos = vb.mapSceneToView(ev.scenePos())

            dx = pos.x() - hitbox._start_pos.x()
            dy = pos.y() - hitbox._start_pos.y()

            # --- новая цена ---
            new_price = hitbox._start_price + dy
            if self._magnet_enabled:
                new_price = self._snap_price(new_price, pos.x())

            new_ts = hitbox._start_ts + dx

            visible._ray_ts = new_ts
            visible._ray_price = new_price

            # --- перерисовка ---
            x0 = new_ts
            x1 = xs[-1] + (xs[-1] - xs[0]) * 2

            hitbox.prepareGeometryChange()

            visible.setData([x0, x1], [new_price, new_price])
            hitbox.setData([x0, x1], [new_price, new_price])

            ev.accept()

        # --- ПКМ для удаления ---
        def _press(ev):
            if ev.button() == QtCore.Qt.RightButton:
                try:
                    self.plot.removeItem(visible)
                    self.plot.removeItem(hitbox)
                except Exception:
                    pass

                lst = self._ray_lines.get(symbol, [])
                self._ray_lines[symbol] = [t for t in lst if t[0] is not visible]

                self.save_rays()
                ev.accept()
            else:
                ev.ignore()

        hitbox.mouseDragEvent = _drag
        hitbox.mousePressEvent = _press

        # --- сохраняем пару ---
        self._ray_lines.setdefault(symbol, []).append((visible, hitbox))

        # --- добавляем на график только если символ текущий ---
        if symbol == self._current_symbol:
            self.plot.addItem(visible)
            self.plot.addItem(hitbox)

        self.save_rays()

    def _ruler_mouse_release(self, ev):
        if ev.button() == QtCore.Qt.MiddleButton:
            self._ruler_active = False
            self._ruler_start = None
            self._ruler_line.hide()
            self._ruler_text.hide()
            ev.accept()
        else:
            pg.GraphicsScene.mouseReleaseEvent(self.plot.scene(), ev)

    def _start_5min_timer(self):
        now = time.time()
        delay = 300 - (int(now) % 300)
        QtCore.QTimer.singleShot(delay * 1000, self._start_5min_loop)

    def _start_h1_timer(self):
        now = time.time()
        delay = 3600 - (int(now) % 3600)  # до следующего 00:00
        QtCore.QTimer.singleShot(delay * 1000, self._start_h1_loop)

    def _start_5min_loop(self):
        self._timer_5m.start(300_000)
        self._on_5min()

    def _start_h1_loop(self):
        self._timer_h1.start(3_600_000)  # 1 час
        self._on_h1()  # выполнить сразу

    def _on_5min(self):
        if getattr(self, "_refresh_running", False):
            return
        self._refresh_running = True

        async def _job():
            try:
                job_started_at = time.time()
                # print(
                #     "[JOB START]",
                #     time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                # )

                import asyncio

                pane = self.pane
                mw = pane.mw

                now = asyncio.get_event_loop().time()
                last_m5 = getattr(self, "_last_m5_sync", 0.0)
                last_h1 = getattr(self, "_last_h1_sync", 0.0)

                updated_m5 = False
                updated_h1 = False

                sema_spot = asyncio.Semaphore(70)
                sema_fut = asyncio.Semaphore(25)
                if self._http_session is None:
                    import aiohttp
                    self._http_session = aiohttp.ClientSession(
                        connector=aiohttp.TCPConnector(
                            limit=200,
                            ttl_dns_cache=300
                        )
                    )

                session = self._http_session

                # ===== PROBE (НЕ gate!) =====
                probe_sym = mw.spot_syms[0] if mw.spot_syms else None
                if probe_sym:
                    arr = CANDLES_M5.get(probe_sym)
                    last_time = arr[-1]["time"] if arr else 0
                    probe = await fetch_last_m5_candle(session, probe_sym)
                    if probe and probe["time"] > last_time:
                        pass  # просто информация, НЕ return

                # ===== M5 SPOT =====
                async def _m5_spot(sym):
                    async with sema_spot:
                        c = await fetch_last_m5_candle(session, sym)
                        if c:
                            merge_last_candle(CANDLES_M5, sym, c)

                await asyncio.gather(*[_m5_spot(s) for s in mw.spot_syms])
                updated_m5 = True
                pane.check_signal_alerts()

                # ===== M5 FUT =====
                if updated_m5:
                    async def _m5_fut(sym):
                        async with sema_fut:
                            c = await fetch_last_m5_candle_fut(session, sym)
                            if c:
                                merge_last_candle(CANDLES_M5_FUT, sym, c)

                    await asyncio.gather(*[_m5_fut(s) for s in mw.fut_syms])

                # ===== UI =====
                if updated_m5:
                    QtCore.QTimer.singleShot(
                        0,
                        lambda: (
                            pane.refresh_symbol_list(
                                update_m5=updated_m5
                            ),
                            pane.refresh_current_symbol_charts()
                        )
                    )


            finally:
                self._refresh_running = False

        asyncio.create_task(_job())

    def _on_h1(self):
        if getattr(self, "_h1_refresh_running", False):
            return
        self._h1_refresh_running = True

        async def _job():
            try:
                import asyncio

                pane = self.pane
                mw = pane.mw
                session = self._http_session

                # те же лимиты, что в _on_5min
                sema_spot = asyncio.Semaphore(70)
                sema_fut = asyncio.Semaphore(25)

                # === H1 SPOT REST ===
                async def _h1_spot(sym):
                    async with sema_spot:
                        c = await fetch_last_h1_candle(session, sym)
                        if c:
                            merge_last_candle(CANDLES_H1, sym, c)

                # === H1 FUT REST ===
                async def _h1_fut(sym):
                    async with sema_fut:
                        c = await fetch_last_h1_candle_fut(session, sym)
                        if c:
                            merge_last_candle(CANDLES_H1_FUT, sym, c)

                # параллельные запросы с ограничителем
                await asyncio.gather(*[_h1_spot(s) for s in mw.spot_syms])
                await asyncio.gather(*[_h1_fut(s) for s in mw.fut_syms])

                # обновляем график
                QtCore.QTimer.singleShot(0, pane.refresh_current_symbol_charts)

            finally:
                self._h1_refresh_running = False

        asyncio.create_task(_job())

    async def _job_update_live(self, symbol: str):
        try:
            if self._http_session is None:
                import aiohttp
                self._http_session = aiohttp.ClientSession(
                    connector=aiohttp.TCPConnector(limit=50)
                )

            session = self._http_session

            # ---- запрашиваем текущую незакрытую свечу 5m futures ----
            m5 = await fetch_last_m5_candle_fut_live(session, symbol)
            # ---- текущую незакрытую свечу 1h futures ----
            h1 = await fetch_last_h1_candle_fut_live(session, symbol)

            # обновляем локальное хранилище свечей
            if m5:
                CANDLES_M5_FUT.setdefault(symbol, [])
                if not CANDLES_M5_FUT[symbol]:
                    CANDLES_M5_FUT[symbol] = [m5]
                else:
                    if m5["time"] == CANDLES_M5_FUT[symbol][-1]["time"]:
                        CANDLES_M5_FUT[symbol][-1] = m5
                    elif m5["time"] > CANDLES_M5_FUT[symbol][-1]["time"]:
                        CANDLES_M5_FUT[symbol].append(m5)

            if h1:
                CANDLES_H1_FUT.setdefault(symbol, [])
                if not CANDLES_H1_FUT[symbol]:
                    CANDLES_H1_FUT[symbol] = [h1]
                else:
                    if h1["time"] == CANDLES_H1_FUT[symbol][-1]["time"]:
                        CANDLES_H1_FUT[symbol][-1] = h1
                    elif h1["time"] > CANDLES_H1_FUT[symbol][-1]["time"]:
                        CANDLES_H1_FUT[symbol].append(h1)
            # перерисовать график текущего символа
            QtCore.QTimer.singleShot(0, self.pane.refresh_current_symbol_charts)

        except Exception as e:
            print("live_candle_error:", e)

    def update_last_candle_only(self, candle: dict):
        if not self._candles:
            return

        ws_time = candle["time"]
        ui_time = self._candles[-1]["time"]

        ws_m5 = ws_time // 300000
        ui_m5 = ui_time // 300000

        # --- 1. Новая свеча ---
        if ws_m5 > ui_m5:
            self._candles.append(candle.copy())

            # ❗ сбрасываем сигнатуры, чтобы _on_5min смог заменить свечу
            self._last_candle_sig = None
            self._last_candles_len = None

            self._redraw()
            return

        # --- 2. Обновление текущей незакрытой свечи ---
        if ws_m5 == ui_m5:
            self._candles[-1] = candle

            # ❗ сбрасываем сигнатуры, так как candle изменился
            self._last_candle_sig = None

            self._redraw()
            return

        # --- 3. Старые свечи игнорируем ---
        return

    def update_last_candle_only_h1(self, candle: dict):
        if not self._candles:
            return

        ws_time = candle["time"]
        ui_time = self._candles[-1]["time"]

        ws_h1 = ws_time // 3600000
        ui_h1 = ui_time // 3600000

        # 1 — новая H1 свеча
        if ws_h1 > ui_h1:
            self._candles.append(candle.copy())

            self._last_candle_sig = None
            self._last_candles_len = None

            self._redraw()
            return

        # 2 — обновление незакрытой свечи
        if ws_h1 == ui_h1:
            self._candles[-1] = candle
            self._last_candle_sig = None
            self._redraw()
            return

        # 3 — старая свеча. Игнор.
        return

    def leaveEvent(self, event):
        self.v_line.hide()
        self.h_line.hide()
        self._axis_label_x.hide()
        self._axis_label_y.hide()
        self.candle_info.hide()

        # НЕ показывать символ — полностью очищаем
        self.title_item.setText("")

        super().leaveEvent(event)

    def show_order_line(self, price: float, side: str, symbol: str = None):
        side = side.lower()
        if self.tf != "M5":
            return
        if symbol not in self._order_lines:
            self._order_lines[symbol] = []

        # уже есть такая (тот же sym/side/price)? → выходим
        for line, pr, sd in self._order_lines.get(symbol, []):
            if abs(pr - price) < 1e-12 and sd == side:
                return

        if not self._candles:
            return

        xs = [c["time"] / 1000.0 for c in self._candles]
        last_x = xs[-1]
        right_x = last_x + (xs[-1] - xs[0]) * 0.05

        if side.lower() in ("buy", "bid", "long"):
            color = (255, 214, 92)  # жёлтая линия BUY
        else:
            color = (60, 182, 255)  # голубая линия SELL

        pen = pg.mkPen(color, width=1)  # ширина линии = 1 px
        line = pg.PlotCurveItem([last_x, right_x], [price, price], pen=pen)
        line.setZValue(10_000)

        # добавляем в plot ТОЛЬКО если это текущий символ и линия ещё не добавлена
        if symbol == getattr(self, "_current_symbol", None):
            if line not in self.plot.items():
                self.plot.addItem(line, ignoreBounds=True)

        self._order_lines[symbol].append((line, price, side))

    def set_signal_mode(self, state: bool):
        self._signal_mode = state

    def _activate_single_signal_mode(self):
        # если режим уже включён — выключаем его
        if self._signal_mode:
            self._signal_mode = False

            # вернуть обычный вид кнопки
            self.btn_signal.setStyleSheet("""
                QPushButton {
                    background-color: #444;
                    color: #ddd;
                    border: 1px solid #666;
                    border-radius: 4px;
                    font-size: 11px;
                }
            """)
            return

        # включаем режим установки сигнала
        self._signal_mode = True

        # подсветить кнопку
        self.btn_signal.setStyleSheet("""
            QPushButton {
                background-color: #aa5500;
                color: white;
                border: 1px solid #666;
                border-radius: 4px;
                font-size: 11px;
            }
        """)

    def _activate_ray_mode(self):
        self._ray_mode = not self._ray_mode

        if self._ray_mode:
            self.btn_ray.setStyleSheet("""
                QPushButton { background-color:#aa5500; color:white;
                border:1px solid #666; border-radius:4px; }
            """)
        else:
            self.btn_ray.setStyleSheet("""
                QPushButton { background-color:#444; color:#ddd;
                border:1px solid #666; border-radius:4px; }
            """)

    def remove_order_line(self, price: float, side: str, symbol: str = None):
        side = side.lower()
        if self.tf != "M5":
            return
        if symbol not in self._order_lines:
            return

        new_list = []
        for line, pr, sd in self._order_lines[symbol]:
            # совпадение линии по цене и стороне
            if sd == side and abs(pr - price) <= max(1e-9, pr * 1e-6):
                try:
                    self.plot.removeItem(line)
                except:
                    pass
            else:
                new_list.append((line, pr, sd))

        self._order_lines[symbol] = new_list

    def show_signal_line(self, symbol: str, price: float, role=None):
        for ln in self._signal_lines.get((symbol, self.tf), []):
            try:
                if abs(ln._visible_y - price) < 1e-12:
                    # ⬇⬇⬇ ВАЖНО: если линии есть, но маркера нет — СОЗДАЁМ МАРКЕР
                    if getattr(ln, "_marker", None) is None and self._candles:
                        marker_x = signal_data["timestamp"] / 1000.0
                        marker = pg.ScatterPlotItem(
                            [marker_x], [ln._visible_y],
                            size=8,
                            brush=pg.mkBrush(255, 140, 0, 200),
                            pen=pg.mkPen(255, 255, 255, 180, width=1)
                        )
                        marker.setZValue(15001)

                        # ⬇⬇⬇ КЛЮЧЕВО
                        marker.setParentItem(ln)

                        ln._marker = marker

                    return ln
            except:
                pass

        if self._current_symbol != symbol:
            return

        # === рисуем как ЛУЧИ: вычисляем координаты времени как в add_ray ===
        if self.tf == "H1":
            xs = [int(c["time"] / 3600000) * 3600 for c in self._candles]  # секунды
        else:
            xs = [int(c["time"] / 300000) * 300 for c in self._candles]  # секунды

        if not xs:
            return

        # находим timestamp сигнала (ms → sec)
        ts = None
        for sig in self.pane.signal_levels.get(symbol, []):
            if abs(sig["price"] - price) < 1e-12:
                ts = sig.get("timestamp")
                break

        # --- хотим ставить начало там, где был клик ---
        x_click = None
        pos = getattr(self, "_last_click_pos", None)
        if pos is not None:
            x_click = pos.x()

        # находим информацию о сигнале с этой ценой
        sig_list = self.pane.signal_levels.setdefault(symbol, [])

        signal_index = None
        signal_data = None

        # 1) сначала ищем сигнал, у которого ещё НЕТ линии этого TF
        for i, s in enumerate(sig_list):
            if abs(s["price"] - price) < 1e-12:
                if (self.tf == "H1" and not s.get("has_main")) or \
                        (self.tf == "M5" and not s.get("has_m5")):
                    signal_index = i
                    signal_data = s
                    break

        # 2) если не нашли — берём сигнал просто по цене
        if signal_data is None:
            for i, s in enumerate(sig_list):
                if abs(s["price"] - price) < 1e-12:
                    signal_index = i
                    signal_data = s
                    break

        # 3) если сигнала вообще нет — выходим
        if signal_data is None:
            return

        # определяем ключ TF (ИСПОЛЬЗУЕТСЯ НИЖЕ)
        key_tf = "main" if self.tf == "H1" else "m5"

        # --- начало линии ---
        # 1) приоритет: сохранённая позиция
        saved_x0 = signal_data.get(f"x0_{key_tf}")
        if saved_x0 is not None:
            x0 = saved_x0
        else:
            # 2) если есть клик — используем его
            pos = getattr(self, "_last_click_pos", None)
            if pos is not None:
                x0 = pos.x()
            else:
                # 3) fallback — ТЕКУЩАЯ свеча, а не первая
                x0 = xs[-1]
        # === ВАЖНО: сохранить x0 при первом создании сигнала ===
        if signal_data.get(f"x0_{key_tf}") is None:
            signal_data[f"x0_{key_tf}"] = x0

        # конец луча — как у add_ray
        # сохраняем прежнюю длину, если есть
        if signal_data.get(f"x1_{key_tf}") is not None:
            x1 = signal_data[f"x1_{key_tf}"]
        else:
            x1 = x0 + (xs[-1] - xs[0]) * 2

        # видимая линия
        pen_color = (255, 140, 0)
        # если неактивная — серым
        for sig in self.pane.signal_levels.get(symbol, []):
            if abs(sig["price"] - price) < 1e-12 and not sig.get("active", True):
                pen_color = (130, 130, 130)
                break

        visible = pg.PlotCurveItem(
            [x0, x1], [price, price],
            pen=pg.mkPen(pen_color, width=1, style=QtCore.Qt.DashLine)
        )
        visible.setZValue(15000)

        # хитбокс — толстый прозрачный
        hitbox = pg.PlotCurveItem(
            [x0, x1], [price, price],
            pen=pg.mkPen((0, 0, 0, 0), width=12)
        )
        hitbox.setZValue(14999)

        # метаданные
        visible._symbol = symbol
        hitbox._symbol = symbol
        visible._hitbox = hitbox
        hitbox._visible = visible
        visible._visible_y = price
        hitbox._visible_y = price
        visible.role = role
        hitbox.role = role
        # координаты луча как у add_ray
        visible._ray_x0 = x0
        visible._ray_x1 = x1
        visible._ray_price = price

        hitbox._ray_x0 = x0
        hitbox._ray_x1 = x1
        hitbox._ray_price = price

        # добавляем
        self.vb_price.addItem(visible)
        self.vb_price.addItem(hitbox)
        if signal_index is not None:
            key = "line_main" if self.tf == "H1" else "line_m5"
            self.pane.signal_levels[symbol][signal_index][key] = visible
            if self.tf == "H1":
                self.pane.signal_levels[symbol][signal_index]["has_main"] = True
            else:
                self.pane.signal_levels[symbol][signal_index]["has_m5"] = True

        # === МАРКЕР: рисуем ТОЛЬКО на том графике, где поставлен сигнал ===
        create_marker = False
        marker_x = signal_data["timestamp"] / 1000.0

        marker_y = price

        if signal_data:
            # Сигнал был поставлен на H1 → маркер только на H1-графике того же символа
            if signal_data.get("has_main") and self.tf == "H1" and self._current_symbol == symbol:
                create_marker = True

            # Сигнал был поставлен на M5 → маркер только на M5-графике того же символа
            if signal_data.get("has_m5") and self.tf == "M5" and self._current_symbol == symbol:
                create_marker = True

        if create_marker:
            marker = pg.ScatterPlotItem(
                [marker_x], [marker_y],
                size=8,
                brush=pg.mkBrush(255, 140, 0, 200),
                pen=pg.mkPen(255, 255, 255, 180, width=1)
            )
            marker.setZValue(15001)

            # ⬇⬇⬇ КЛЮЧЕВО
            marker.setParentItem(visible)

            visible._marker = marker


        else:
            visible._marker = None
            hitbox._marker = None

        # сохраняем видимую как основную линию в списке
        self._signal_lines.setdefault((symbol, self.tf), []).append(visible)

        # ====== УДАЛЕНИЕ ПКМ ======
        def _delete(ev):
            if ev.button() != QtCore.Qt.RightButton:
                ev.ignore()
                return

            # убрать с графика
            try:
                self.vb_price.removeItem(visible)
            except:
                pass
            try:
                self.vb_price.removeItem(hitbox)
            except:
                pass
            # удалить маркер
            try:
                self.vb_price.removeItem(visible._marker)
            except:
                pass

            # убрать из локального списка
            try:
                lst = self._signal_lines.get((symbol, self.tf), [])
                lst.remove(visible)
            except:
                pass

            # удалить из общей структуры сигналов
            sigs = self.pane.signal_levels.get(symbol, [])
            key = "line_main" if self.tf == "H1" else "line_m5"

            for sig in list(sigs):
                if sig.get(key) is visible:
                    # удалить линию другого TF
                    other_role = "m5" if key == "line_main" else "main"
                    other_key = "line_m5" if key == "line_main" else "line_main"
                    other = sig.get(other_key)

                    if other:
                        try:
                            panel = self.pane.chartPanel if other_key == "line_main" \
                                else self.pane.chartPanel_m5
                            panel.vb_price.removeItem(other)
                        except:
                            pass

                    # удалить сам сигнал
                    try:
                        self.pane.signal_levels[symbol].remove(sig)
                    except:
                        pass

                    self.pane.save_signal_levels()
                    break

            ev.accept()

        hitbox.mousePressEvent = _delete


        # ====== ПЕРЕТЯГИВАНИЕ ======
        def _drag(ev):
            vb = self.plot.getViewBox()

            if ev.isStart():
                hitbox._dragging = True
                hitbox._start_pos = vb.mapSceneToView(ev.scenePos())
                hitbox._start_price = visible._visible_y
                hitbox._start_x0 = visible._ray_x0
                hitbox._start_x1 = visible._ray_x1

                ev.accept()
                return

            if ev.isFinish():
                hitbox._dragging = False

                # Сохраняем новое X-начало линии
                key = "line_main" if self.tf == "H1" else "line_m5"

                for sig in self.pane.signal_levels.get(symbol, []):
                    if sig.get(key) is visible:
                        key = "main" if self.tf == "H1" else "m5"

                        sig[f"timestamp_x_{key}"] = visible._ray_x0
                        sig[f"x0_{key}"] = visible._ray_x0
                        sig[f"x1_{key}"] = visible._ray_x1

                        break

                self.pane.save_signal_levels()
                ev.accept()
                return

            if not getattr(hitbox, "_dragging", False):
                ev.ignore()
                return

            pos = vb.mapSceneToView(ev.scenePos())
            dy = pos.y() - hitbox._start_pos.y()
            new_price = hitbox._start_price + dy

            # магнит
            if self._magnet_enabled:
                new_price = self._snap_price(new_price, pos.x())

            dx = pos.x() - hitbox._start_pos.x()
            length = hitbox._start_x1 - hitbox._start_x0

            new_x0 = hitbox._start_x0 + dx
            new_x1 = new_x0 + length

            visible.setData([new_x0, new_x1], [new_price, new_price])
            hitbox.setData([new_x0, new_x1], [new_price, new_price])

            visible._ray_x0 = new_x0
            visible._ray_x1 = new_x1

            hitbox._ray_x0 = new_x0
            hitbox._ray_x1 = new_x1

            # обновляем метаданные
            visible._ray_price = new_price
            hitbox._ray_price = new_price

            visible._visible_y = new_price
            hitbox._visible_y = new_price
            # --- двигаем маркер только по высоте ---
            try:
                marker = visible._marker
                marker.setData([marker.data['x'][0]], [new_price])
                # альтернативно:
                # marker.setData([marker_x0], [new_price])
            except:
                pass

            # обновляем запись сигнала
            key = "line_main" if self.tf == "H1" else "line_m5"

            for sig in self.pane.signal_levels.get(symbol, []):
                if sig.get(key) is visible:
                    sig["price"] = new_price

                    # обновить вторую линию на другом TF
                    other_key = "line_m5" if key == "line_main" else "line_main"
                    other = sig.get(other_key)
                    if other and hasattr(other, "_visible_y"):
                        ox0 = other._ray_x0
                        ox1 = other._ray_x1
                        other.setData([ox0, ox1], [new_price, new_price])
                        other._visible_y = new_price

                    break

            ev.accept()
        hitbox.mouseDragEvent = _drag
        return visible

    def load_signal_levels(self):
        """Загружаем уровни из settings."""
        settings = QSettings("Scriner", "LevelsPane")
        raw = settings.value("signal_levels", "")

        if not raw:
            return

        try:
            data = json.loads(raw)
        except:
            return

        self.pane.signal_levels = data

    def save_rays(self):
        settings = QSettings("MyCompany", "BinanceScanner")
        key = f"ray_levels_{self.tf}"

        # --- читаем то, что уже есть в реестре ---
        raw = settings.value(key, "")
        try:
            stored = json.loads(raw) if raw else {}
        except Exception:
            stored = {}

        # --- обновляем / перезаписываем ТОЛЬКО известные символы ---
        for sym, pairs in self._ray_lines.items():
            out = []
            for visible, hitbox in pairs:
                ts = getattr(visible, "_ray_ts", None)
                price = getattr(visible, "_ray_price", None)

                if ts is None or price is None:
                    continue

                out.append({
                    "ts": int(ts),
                    "price": float(price)
                })

            if out:
                stored[sym] = out
            else:
                # если по символу лучей нет — удаляем его
                stored.pop(sym, None)

        # --- сохраняем ОБЪЕДИНЁННЫЕ данные ---
        settings.setValue(key, json.dumps(stored))

    def load_rays(self):
        settings = QSettings("MyCompany", "BinanceScanner")
        key = f"ray_levels_{self.tf}"
        raw = settings.value(key, "")

        if not raw:
            return

        try:
            data = json.loads(raw)
        except Exception:
            return

        cur = self._current_symbol
        if cur not in data:
            return

        # 🔑 ВАЖНО: ПОЛНОСТЬЮ УДАЛЯЕМ СТАРЫЕ ЛУЧИ ЭТОГО СИМВОЛА
        old = self._ray_lines.get(cur, [])
        for visible, hitbox in old:
            try:
                self.plot.removeItem(visible)
            except:
                pass
            try:
                self.plot.removeItem(hitbox)
            except:
                pass

        self._ray_lines[cur] = []

        # --- создаём заново ИЗ НАСТРОЕК ---
        for d in data[cur]:
            ts = d.get("ts")
            price = d.get("price")
            if ts is None or price is None:
                continue

            self._creating_ray_now = True
            try:
                self.add_ray(cur, price, ts)
            except Exception:
                pass
            self._creating_ray_now = False


class Toast(QtWidgets.QFrame):
    clicked = QtCore.Signal()
    closed = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            QtCore.Qt.Tool |
            QtCore.Qt.FramelessWindowHint |
            QtCore.Qt.WindowStaysOnTopHint
        )
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, False)

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def closeEvent(self, event):
        self.closed.emit()

        # аккуратно закрываем aiohttp.ClientSession
        try:
            session = getattr(self, "_http_session", None)
            if session and not session.closed:
                import asyncio
                asyncio.create_task(session.close())
        except Exception as e:
            print("[CLOSE] error closing http session:", e)

        super().closeEvent(event)


# -------------- UI: Symbols + Chart shell --------------
class LevelsPane(QtCore.QObject):
    def __init__(self, main_window):
        super().__init__(main_window)
        self.favorites = set()
        settings = QtCore.QSettings("Scriner", "LevelsPane")
        saved = settings.value("favorites", [])
        if saved:
            self.favorites = set(saved)

        self.mw = main_window
        self._first_autoselect_done = False

        # Безопасный запуск периодического обновления индикаторов (через 1 секунду после старта GUI)
        QtCore.QTimer.singleShot(1000, lambda: asyncio.create_task(self._periodic_indicators_refresh()))

        # --- Левый список символов ---
        self.symbolsPanel = QtWidgets.QTreeWidget()
        self.symbolsPanel.setIconSize(QtCore.QSize(3, 12))

        self.symbolsPanel.setFocusPolicy(QtCore.Qt.NoFocus)

        self.symbolsPanel.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.symbolsPanel.setAllColumnsShowFocus(True)
        self.symbolsPanel.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.symbolsPanel.setHeaderLabels(["★", "Тикер", "Vol", "Изм", "NATR", "Кор", "Всп", "BOI"])
        self.symbolsPanel.headerItem().setTextAlignment(0, QtCore.Qt.AlignCenter)
        self.symbolsPanel.headerItem().setTextAlignment(1, QtCore.Qt.AlignCenter)
        self.symbolsPanel.headerItem().setTextAlignment(2, QtCore.Qt.AlignCenter)
        self.symbolsPanel.headerItem().setTextAlignment(3, QtCore.Qt.AlignCenter)
        self.symbolsPanel.headerItem().setTextAlignment(4, QtCore.Qt.AlignCenter)
        self.symbolsPanel.headerItem().setTextAlignment(5, QtCore.Qt.AlignCenter)
        self.symbolsPanel.headerItem().setTextAlignment(6, QtCore.Qt.AlignCenter)
        self.symbolsPanel.headerItem().setTextAlignment(7, QtCore.Qt.AlignCenter)

        self.symbolsPanel.setColumnWidth(0, 15)
        self.symbolsPanel.setColumnWidth(1, 130)
        self.symbolsPanel.setColumnWidth(2, 100)
        self.symbolsPanel.setColumnWidth(3, 100)
        self.symbolsPanel.setColumnWidth(4, 100)
        self.symbolsPanel.setColumnWidth(5, 100)
        self.symbolsPanel.setColumnWidth(6, 100)
        self.symbolsPanel.setColumnWidth(7, 100)

        header = self.symbolsPanel.header()

        # 👇 разрешаем секциям быть уже, чем стандартный минимум
        header.setMinimumSectionSize(5)

        # 0-й столбец (★) — вручную регулируемый
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.Interactive)

        # 6-й столбец — тоже обычный, без Stretch
        header.setSectionResizeMode(6, QtWidgets.QHeaderView.Interactive)

        header.setStretchLastSection(False)

        self.symbolsPanel.setRootIsDecorated(False)
        self.symbolsPanel.setAlternatingRowColors(False)
        self.symbolsPanel.setSortingEnabled(False)

        self.symbolsPanel.setStyleSheet("""
            QTreeWidget {
                background-color: #161a1d;
                border: none;
                color: #c8c8c8;
                font-size: 10pt;
            }

            QTreeWidget:focus {
                outline: none;
            }

            QTreeWidget::item:selected {
                background-color: #242a2f;
                color: #c8c8c8;
                outline: none;
                border: none;
            }

            QTreeWidget::item:focus {
                outline: none;
                border: none;
            }

            /* === Заголовки таблицы === */
            QHeaderView {
                background-color: #1b1f22;   /* <-- этот блок убирает серую область справа от заголовков */
            }

            QHeaderView::section {
                background-color: #1b1f22;
                color: #aaaaaa;
                padding: 4px;
                border: none;
            }
        """)

        # важно: разрешить стилизовать фон и не заливать viewport базовым цветом
        self.symbolsPanel.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        self.symbolsPanel.viewport().setAutoFillBackground(False)
        self.symbolsPanel.setFrameShape(QtWidgets.QFrame.NoFrame)

        self.symbolsPanel.setMinimumWidth(60)

        self.symbolsPanel.setMinimumWidth(100)
        self.symbolsPanel.itemSelectionChanged.connect(self._on_symbol_selected)
        self.symbolsPanel.itemClicked.connect(self._on_symbol_clicked_copy)
        self.symbolsPanel.itemClicked.connect(self._on_item_clicked)

        # --- Центральный график ---
        self.chartPanel = LevelsChart(self, tf="H1")
        self.chartPanel_m5 = LevelsChart(self, tf="M5")

        # --- Контейнер для двух графиков (H1 сверху, M5 снизу) ---
        self.chartContainer = QtWidgets.QWidget()
        vlay = QtWidgets.QVBoxLayout(self.chartContainer)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.setSpacing(1)

        vlay.addWidget(self.chartPanel)  # ← H1
        vlay.addWidget(self.chartPanel_m5)  # ← M5

        self._orders_by_symbol = {}
        self.signal_levels = {}  # { "BTCUSDT": [ { "price": float, "active": True } ] }
        self._alerts = []
        self.toasts = []  # <-- ОБЯЗАТЕЛЬНО! список активных уведомлений
        # сигнальные уровни (живут в LevelsPane, не в MainWindow)
        self.signal_levels: Dict[str, List[dict]] = {}

        # загружаем сохранённые сигналы
        self.load_signal_levels()


        # --- Служебные данные ---
        self._last_m5_sync = 0.0
        self._last_ui_refresh = 0.0

        # --- восстановление ширины столбцов ---
        settings = QtCore.QSettings("Scriner", "LevelsPane")
        widths = settings.value("column_widths")
        if widths:
            for i, w in enumerate(widths):
                try:
                    self.symbolsPanel.setColumnWidth(i, int(w))
                except Exception:
                    pass

    def add_to_favorites(self, symbol):
        self.favorites.add(symbol)
        QSettings("Scriner", "LevelsPane").setValue("favorites", list(self.favorites))
        self.refresh_fav_icons()

    def remove_from_favorites(self, symbol):
        if symbol in self.favorites:
            self.favorites.remove(symbol)
            QSettings("Scriner", "LevelsPane").setValue("favorites", list(self.favorites))
        self.refresh_fav_icons()

    def refresh_fav_icons(self):
        it = self.symbolsPanel.invisibleRootItem()
        for i in range(it.childCount()):
            node = it.child(i)
            # берём полный символ, который ты везде используешь (BTCUSDT)
            sym = node.data(1, QtCore.Qt.UserRole)
            node.setText(0, "★" if sym in self.favorites else "")

    def _on_item_clicked(self, item, column):
        # если кликнули по звезде
        if column == 0:
            sym = item.data(1, QtCore.Qt.UserRole)

            if sym in self.favorites:
                self.favorites.remove(sym)
            else:
                self.favorites.add(sym)

            self.save_favorites()
            self.refresh_symbol_list(force=True)

    def add_signal(self, symbol: str, price: float):
        candles = CANDLES_M5.get(symbol)
        last_price = candles[-1]["close"] if candles else None

        # определяем таймфрейм
        is_main = (self._clicked_tf == "H1")
        is_m5 = (self._clicked_tf == "M5")

        # --- определяем X-координату клика ---
        pos = None
        if getattr(self.chartPanel, "_last_click_pos", None):
            pos = self.chartPanel._last_click_pos
        elif getattr(self.chartPanel_m5, "_last_click_pos", None):
            pos = self.chartPanel_m5._last_click_pos

        click_x = pos.x() if pos else None
        click_tf = self._clicked_tf if click_x is not None else None
        marker_price = last_price
        direction = "up" if marker_price < price else "down"

        sig = {
            "price": float(price),
            "active": True,
            "last_price": last_price,

            "timestamp": int(time.time() * 1000),
            "direction": direction,  # ← ВАЖНО

            # добавляем к сигналу X координату клика и TF клика
            "timestamp_x_main": click_x if is_main else None,
            "timestamp_x_m5": click_x if is_m5 else None,

            "line_main": None if is_main else False,
            "line_m5": None if is_m5 else False,

            "has_main": is_main,
            "has_m5": is_m5,
        }

        # --- Сначала добавляем сигнал в реестр, чтобы show_signal_line мог его найти ---
        lst = self.signal_levels.setdefault(symbol, [])
        lst.append(sig)
        self.save_signal_levels()

        # --- Рисуем линию(и) и сохраняем объекты линий в сам сигнал ---
        try:
            if is_main:
                ln = self.chartPanel.show_signal_line(symbol, price, role="main")
                # обновляем ссылку в реестре (последний добавленный сигнал)
                if ln is not None:
                    lst[-1]["line_main"] = ln
            if is_m5:
                ln2 = self.chartPanel_m5.show_signal_line(symbol, price, role="m5")
                if ln2 is not None:
                    lst[-1]["line_m5"] = ln2
        except Exception as e:
            # не ломаем программу, но логгируем для отладки
            print("add_signal: show_signal_line error:", e)

        # и снова сохраняем (чтобы при повторном запуске восстановить линии)
        self.save_signal_levels()

    def save_favorites(self):
        settings = QtCore.QSettings("Scriner", "LevelsPane")
        settings.setValue("favorites", list(self.favorites))

    def save_signal_levels(self):
        data = {}

        for sym, lst in self.signal_levels.items():
            new_list = []

            for sig in lst:
                entry = {
                    "price": float(sig["price"]),
                    "active": bool(sig.get("active", True)),
                    "has_main": bool(sig.get("has_main", False)),
                    "has_m5": bool(sig.get("has_m5", False)),
                    "timestamp": sig.get("timestamp"),
                    "direction": sig.get("direction"),

                    # H1
                    "timestamp_x_main": sig.get("timestamp_x_main"),
                    "x0_main": sig.get("x0_main"),
                    "x1_main": sig.get("x1_main"),

                    # M5
                    "timestamp_x_m5": sig.get("timestamp_x_m5"),
                    "x0_m5": sig.get("x0_m5"),
                    "x1_m5": sig.get("x1_m5"),
                }

                new_list.append(entry)

            data[sym] = new_list

        settings = QtCore.QSettings("Scriner", "LevelsPane")
        settings.setValue("signal_levels", json.dumps(data))

    def load_signal_levels(self):
        """
        Загружаем сигналы из QSettings. Линии дорисуем позже, когда откроем график.
        Сохраняем флаги has_main/has_m5 чтобы знать, на каких TF восстанавливать линии.
        """
        settings = QtCore.QSettings("Scriner", "LevelsPane")
        raw = settings.value("signal_levels")

        if not raw:
            return

        try:
            data = json.loads(raw)
        except Exception:
            return

        self.signal_levels = {}
        for sym, lst in data.items():
            self.signal_levels[sym] = []
            for sig in lst:
                price = float(sig.get("price"))
                active = bool(sig.get("active", True))
                has_main = bool(sig.get("has_main", False))
                has_m5 = bool(sig.get("has_m5", False))
                ts = int(sig.get("timestamp", 0))  # timestamp теперь всегда int, в миллисекундах

                self.signal_levels[sym].append({
                    "price": price,
                    "active": active,
                    "last_price": None,
                    "timestamp": ts,
                    "direction": sig.get("direction"),

                    # ⬇⬇⬇ ВОССТАНАВЛИВАЕМ ГЕОМЕТРИЮ
                    "timestamp_x_main": sig.get("timestamp_x_main"),
                    "x0_main": sig.get("x0_main"),
                    "x1_main": sig.get("x1_main"),

                    "timestamp_x_m5": sig.get("timestamp_x_m5"),
                    "x0_m5": sig.get("x0_m5"),
                    "x1_m5": sig.get("x1_m5"),

                    "line_main": None,
                    "line_m5": None,

                    "has_main": has_main,
                    "has_m5": has_m5,
                })

    def mark_signal_triggered(self, symbol, sig):
        """Помечает сигнал как выполненный и красит линию в серый."""
        sig["active"] = False

        grey_pen = pg.mkPen((130, 130, 130), width=1, style=QtCore.Qt.DashLine)

        for key in ("line_main", "line_m5"):
            line = sig.get(key)
            if line:
                line.setPen(grey_pen)

        # сохранить изменения
        self.save_signal_levels()

    def format_price(self, symbol, price):
        # пробуем определить кол-во знаков после запятой из последней свечи
        candles = CANDLES_M5.get(symbol)
        if candles:
            last = candles[-1]["close"]
            text = f"{last}"
            if "." in text:
                decimals = len(text.split(".")[1])
                return f"{price:.{decimals}f}"
        # fallback
        return f"{price:.8f}".rstrip("0").rstrip(".")

    def show_toast(self, symbol, price):
        toast = Toast(self.mw)
        toast.setObjectName("toastBox")

        toast.setStyleSheet("""
            QWidget#toastBox {
                background-color: #fafafa;
                border: 1px solid #dddddd;
                border-radius: 10px;
            }
            QLabel {
                color: black;
                background: transparent;
                border: none;
                padding: 0px;
                font-size: 14pt;
            }
            QPushButton#closeBtn {
                background: transparent;
                color: #666;
                border: none;
                font-size: 16pt;
                padding: 0px;
            }
            QPushButton#closeBtn:hover {
                color: #000;
            }
        """)

        toast.setFixedSize(220, 50)

        layout = QtWidgets.QHBoxLayout(toast)
        layout.setContentsMargins(10, 8, 10, 8)

        msg = QtWidgets.QLabel(f"{symbol}: {self.format_price(symbol, price)}")
        msg.setWordWrap(True)

        close_btn = QtWidgets.QPushButton("×")
        close_btn.setObjectName("closeBtn")
        close_btn.setFixedSize(20, 20)

        layout.addWidget(msg)
        layout.addWidget(close_btn)

        close_btn.clicked.connect(toast.close)

        toast.clicked.connect(lambda: (
            QtWidgets.QApplication.clipboard().setText(symbol),  # КОПИРУЕМ ТИКЕР
            self.open_symbol(symbol),  # ОТКРЫВАЕМ ГРАФИК
            toast.close()  # ЗАКРЫВАЕМ ТОСТ
        ))

        # когда тост закрыт — удалить из списка и переразместить остальные
        toast.closed.connect(lambda: (
            self.toasts.remove(toast),
            self.reposition_toasts()
        ))

        # ДОБАВЛЯЕМ в список
        self.toasts.append(toast)

        # ПОКАЗЫВАЕМ
        toast.show()

        # ПЕРЕРАЗМЕСТИТЬ ВСЕ
        self.reposition_toasts()

    def reposition_toasts(self):
        """Смещает все активные уведомления вверх, чтобы новые вставали ниже."""
        geo = self.mw.geometry()
        base_x = geo.x() + 315
        base_y = geo.y() + geo.height() - 75

        # последние тосты должны быть внизу
        for i, toast in enumerate(reversed(self.toasts)):
            offset_y = base_y - i * 60  # 60px расстояние между окнами
            toast.move(base_x, offset_y)

    def trigger_alert(self, symbol, price):
        self.sound = QSoundEffect()
        self.sound.setSource(QUrl.fromLocalFile("alert.wav"))
        self.sound.setVolume(1.0)
        self.sound.play()

        # новое всплывающее окно
        self.show_toast(symbol, price)

    def open_symbol(self, symbol):
        """
        Открывает нужный тикер в списке.
        Если символ отсутствует — добавляет его вручную, игнорируя фильтры.
        """

        item_found = None
        root = self.symbolsPanel.invisibleRootItem()

        # === 1) Поиск по полному тикеру (в UserRole) ===
        for i in range(root.childCount()):
            it = root.child(i)
            if it.data(1, QtCore.Qt.UserRole) == symbol:
                item_found = it
                break

        # === 2) Поиск по короткому названию (BTC) ===
        if item_found is None:
            short = symbol.replace("USDT", "")
            items = self.symbolsPanel.findItems(short, QtCore.Qt.MatchExactly, 1)
            if items:
                item_found = items[0]

        # === 3) ЕСЛИ НЕ НАШЛИ — ДОБАВЛЯЕМ СИМВОЛ В СПИСОК ===
        if item_found is None:
            # создаём элемент вручную
            short = symbol.replace("USDT", "")
            new_item = QtWidgets.QTreeWidgetItem([
                "",  # звезда
                short,  # тикер
                "-", "-", "-", "-", "-"  # остальные колонки
            ])
            new_item.setData(1, QtCore.Qt.UserRole, symbol)

            # выравнивание
            for col in range(7):
                new_item.setTextAlignment(col, QtCore.Qt.AlignLeft)

            # добавляем в начало списка (можно вниз — как хочешь)
            root.insertChild(0, new_item)

            item_found = new_item

        # === 4) Выбор элемента и переключение графика ===
        self.symbolsPanel.setCurrentItem(item_found)

        try:
            self._on_symbol_selected()
        except Exception as e:
            print("open_symbol error:", e)
        # гарантируем обновление графиков после добавления тикера
        QtCore.QTimer.singleShot(10, lambda: self._on_symbol_selected())

    def save_column_widths(self):
        """Сохраняем ширину столбцов"""
        widths = [self.symbolsPanel.columnWidth(i) for i in range(self.symbolsPanel.columnCount())]
        settings = QtCore.QSettings("Scriner", "LevelsPane")
        settings.setValue("column_widths", widths)

    def highlight_symbol(self, symbol: str, active: bool = True, side: str = None):
        """
        Подсвечивает символ — ищет его во ВСЁМ дереве, включая 'Избранное'.
        """

        def find_items_recursive(parent):
            """Возвращает список всех item внутри дерева, соответствующих символу."""
            result = []

            for i in range(parent.childCount()):
                child = parent.child(i)
                sym = child.data(1, QtCore.Qt.UserRole)

                if sym == symbol:
                    result.append(child)

                # рекурсивно идём в детей
                result += find_items_recursive(child)

            return result

        # корень дерева
        root = self.symbolsPanel.invisibleRootItem()

        # ищем все item данного symbol
        matched_items = find_items_recursive(root)

        if not matched_items:
            return  # нет такого символа вообще

        # подсвечиваем ВСЕ найденные экземпляры символа (Spot/Futures/Избранное)
        for item in matched_items:
            if active and side:
                # цвет полоски
                if side.lower() in ("buy", "bid", "long"):
                    bar = make_color_bar(QtGui.QColor("#ffd65c"))  # жёлтая
                else:
                    bar = make_color_bar(QtGui.QColor("#3cb6ff"))  # голубая
                item.setData(0, QtCore.Qt.DecorationRole, bar)
            else:
                item.setData(0, QtCore.Qt.DecorationRole, None)

    def check_signal_alerts(self):
        for sym, lst in self.signal_levels.items():
            candles = CANDLES_M5.get(sym)
            if not candles:
                continue

            for sig in lst:
                if not sig.get("active", True):
                    continue

                level = sig["price"]
                direction = sig.get("direction")
                ts = sig.get("timestamp", 0)

                if direction not in ("up", "down"):
                    continue

                # свечи строго ПОСЛЕ маркера
                after = [c for c in candles if c["time"] > ts]
                if not after:
                    continue

                for c in after:
                    high = c["high"]
                    low = c["low"]

                    if direction == "up":
                        # пробой ВВЕРХ: ХВОСТОМ ИЛИ ТЕЛОМ
                        if high >= level:
                            self.mark_signal_triggered(sym, sig)
                            self.trigger_alert(sym, level)
                            break

                    elif direction == "down":
                        # пробой ВНИЗ
                        if low <= level:
                            self.mark_signal_triggered(sym, sig)
                            self.trigger_alert(sym, level)
                            break

    async def load_all_hourly(self, session, spot_symbols: List[str]):
        sema = asyncio.Semaphore(50)

        async def _one(sym):
            async with sema:
                candles = await fetch_hourly_candles(session, sym)
                CANDLES_H1[sym] = candles

        await asyncio.gather(*[_one(s) for s in spot_symbols])

    async def load_all_hourly_futures(self, session, spot_symbols: List[str]):
        sema = asyncio.Semaphore(4)

        async def _one(sym):
            async with sema:
                candles = await fetch_hourly_candles_fut(session, sym)
                CANDLES_H1_FUT[sym] = candles

        await asyncio.gather(*[_one(s) for s in spot_symbols])

    async def load_all_m5(self, session, spot_symbols: List[str]):
        sema = asyncio.Semaphore(50)

        async def _one(sym):
            async with sema:
                candles = await fetch_m5_candles(session, sym)
                CANDLES_M5[sym] = candles

                # 🔑 если это текущий символ — обновить оба графика
                if (
                        hasattr(self, "levelsPane")
                        and self.levelsPane.chartPanel._current_symbol == sym
                ):
                    QtCore.QTimer.singleShot(
                        0,
                        self.levelsPane._on_symbol_selected
                    )

        await asyncio.gather(*[_one(s) for s in spot_symbols])

        # корреляции — можно, они не трогают график
        self.update_corr_values()

    async def load_all_m5_futures(self, session, fut_symbols: List[str]):
        """Загружает M5 свечи для всех фьючерсных инструментов."""
        sema = asyncio.Semaphore(4)

        async def _one(sym):
            async with sema:
                candles = await fetch_m5_candles_fut(session, sym)
                CANDLES_M5_FUT[sym] = candles

        await asyncio.gather(*[_one(s) for s in fut_symbols])

        # После загрузки можно обновить таблицу
        self.update_corr_values()
        # self.refresh_symbol_list(force=True)

    def update_corr_values(self, bars: int | None = None):
        """Пересчитывает корреляцию для монет, у которых есть M5 и BTCUSDT,
        используя текущее значение ind_corr_bars из MainWindow."""
        if "BTCUSDT" not in CANDLES_M5:
            return
        if bars is None:
            bars = getattr(self.mw, "ind_corr_bars", 48)

        for sym in list(CANDLES_M5.keys()):
            # if sym == "BTCUSDT":
            #     continue
            corr = calc_corr(sym, tf=getattr(self.mw, "ind_corr_tf", "M5"), bars=bars)

            if corr is not None:
                CORR_M5[sym] = corr

    def refresh_symbol_list(self, force: bool = False, update_h1: bool = True, update_m5: bool = True):
        """Обновляет таблицу символов только после полной загрузки всех данных."""

        # === если обновление списка уже идёт — не запускать второе ===
        if getattr(self, "_symbol_refresh_in_progress", False):
            return

        self._symbol_refresh_in_progress = True
        try:  # FIX: гарантируем снятие флага

            # === Проверяем, что все данные загружены ===
            def _data_ready():
                if not (CANDLES_H1 and CANDLES_H1_FUT and CANDLES_M5 and CANDLES_M5_FUT):
                    return False
                for sym in CANDLES_H1.keys():
                    if not (
                            len(CANDLES_H1.get(sym, [])) > 0 and
                            len(CANDLES_H1_FUT.get(sym, [])) > 0 and
                            len(CANDLES_M5.get(sym, [])) > 0 and
                            len(CANDLES_M5_FUT.get(sym, [])) > 0
                    ):
                        return False
                return True

            # === Если данных ещё нет — подождать и попытаться снова ===
            if not _data_ready():
                QtCore.QTimer.singleShot(
                    300,
                    lambda: self.refresh_symbol_list(force, update_h1, update_m5)
                )
                return

            # === throttling ДОЛЖЕН БЫТЬ ЗДЕСЬ, а не раньше ===
            now = time.time()
            if not force and now - getattr(self, "_last_symbol_refresh", 0) < 2.0:
                return
            self._last_symbol_refresh = now

            if not _data_ready():
                QtCore.QTimer.singleShot(
                    300,
                    lambda: self.refresh_symbol_list(force, update_h1, update_m5)
                )
                return

            # === Когда все данные готовы — продолжаем как обычно ===
            now = time.monotonic()

            # === Берём настройки напрямую из MainWindow (актуальные после OK) ===
            ind_vol_tf = getattr(self.mw, "ind_vol_tf", "H1")
            ind_vol_bars = getattr(self.mw, "ind_vol_bars", 24)
            ind_izm_tf = getattr(self.mw, "ind_izm_tf", "H1")
            ind_izm_bars = getattr(self.mw, "ind_izm_bars", 12)
            ind_natr_tf = getattr(self.mw, "ind_natr_tf", "M5")
            ind_natr_bars = getattr(self.mw, "ind_natr_bars", 48)
            ind_corr_tf = getattr(self.mw, "ind_corr_tf", "M5")
            ind_corr_bars = getattr(self.mw, "ind_corr_bars", 48)
            ind_spike_tf = getattr(self.mw, "ind_spike_tf", "H1")
            ind_spike_bars = getattr(self.mw, "ind_spike_bars", 20)
            ind_boi_tf = getattr(self.mw, "ind_boi_tf", "H1")
            ind_boi_bars = getattr(self.mw, "ind_boi_bars", 20)
            min_boi = getattr(self.mw, "filter_min_boi", 0.0)

            # ✅ корреляцию пересчитываем с текущим числом баров
            self.update_corr_values(bars=ind_corr_bars)

            self.symbolsPanel.blockSignals(True)

            # --- готовим локальные копии настроек и списков ---
            syms = sorted(CANDLES_H1.keys())

            min_vol = getattr(self.mw, "filter_min_vol_m", 0.0) * 1e6
            max_vol = getattr(self.mw, "filter_max_vol_m", 9999999.0) * 1e6
            min_chg = getattr(self.mw, "filter_min_chg_pct", 0.0)
            min_natr = getattr(self.mw, "filter_min_natr_pct", 0.0)
            min_spike_ratio = getattr(self.mw, "filter_min_spike_ratio", 0.0)

            ind_vol_tf_local = ind_vol_tf
            ind_vol_bars_local = ind_vol_bars
            ind_izm_tf_local = ind_izm_tf
            ind_izm_bars_local = ind_izm_bars
            ind_natr_tf_local = ind_natr_tf
            ind_natr_bars_local = ind_natr_bars
            ind_corr_tf_local = ind_corr_tf
            ind_corr_bars_local = ind_corr_bars
            ind_spike_tf_local = ind_spike_tf
            ind_spike_bars_local = ind_spike_bars
            min_corr_pct_local = getattr(self.mw, "filter_min_corr_pct", 30.0)
            max_price_limit = getattr(self.mw, "filter_max_price_usd", 0.0)

            # --- ФУНКЦИЯ СБОРКИ ДАННЫХ ---
            def _build_items():
                out = []
                for sym in syms:
                    natr = 0.0  # FIX: обязательно инициализируем

                    candles_h1_spot = CANDLES_H1.get(sym)
                    if not candles_h1_spot or len(candles_h1_spot) < 25:
                        continue

                    src_vol = CANDLES_M5 if ind_vol_tf_local.upper() == "M5" else CANDLES_H1_FUT
                    src_izm = CANDLES_M5 if ind_izm_tf_local.upper() == "M5" else CANDLES_H1_FUT

                    # --- vol ---
                    vol_24h = 0.0
                    candles_for_vol = src_vol.get(sym)
                    if candles_for_vol and len(candles_for_vol) >= ind_vol_bars_local:
                        vol_24h = sum(
                            c["volume"] * c["close"]
                            for c in candles_for_vol[-ind_vol_bars_local:]
                        )

                    # --- izm ---
                    pct_change = 0.0
                    candles_for_izm = src_izm.get(sym)
                    if candles_for_izm and len(candles_for_izm) >= ind_izm_bars_local + 1:
                        open_prev = candles_for_izm[-(ind_izm_bars_local + 1)]["open"]
                        close_now = candles_for_izm[-1]["close"]
                        pct_change = (close_now - open_prev) / open_prev * 100 if open_prev > 0 else 0.0

                    # --- natr ---
                    candles_src = (
                        CANDLES_M5_FUT.get(sym)
                        if ind_natr_tf_local.upper() == "M5"
                        else CANDLES_H1_FUT.get(sym)
                    )
                    if candles_src and len(candles_src) > ind_natr_bars_local:
                        trs = []
                        for i in range(1, len(candles_src)):
                            h = candles_src[i]["high"]
                            l = candles_src[i]["low"]
                            cp = candles_src[i - 1]["close"]
                            trs.append(max(h - l, abs(h - cp), abs(l - cp)))
                        atr = sum(trs[-ind_natr_bars_local:]) / ind_natr_bars_local
                        close_now = candles_src[-1]["close"]
                        if close_now > 0:
                            natr = (atr / close_now) * 100

                    # --- фильтры ---
                    if vol_24h < min_vol or vol_24h > max_vol:
                        if sym not in self.favorites:
                            continue

                    if -min_chg < pct_change < min_chg:
                        if sym not in self.favorites:
                            continue

                    corr = calc_corr(sym, tf=ind_corr_tf_local, bars=ind_corr_bars_local)
                    if corr is None or abs(corr) > min_corr_pct_local:
                        if sym not in self.favorites:
                            continue

                    spike = calc_volume_spike_fut(
                        sym,
                        tf=ind_spike_tf_local,
                        period=ind_spike_bars_local
                    )
                    if spike is None or spike < min_spike_ratio:
                        if sym not in self.favorites:
                            continue
                    boi = calc_boi(sym, tf=ind_boi_tf, period=ind_boi_bars)

                    # если min_boi == 0 — фильтр отключён
                    if min_boi > 0:
                        if boi is None or boi < min_boi:
                            if sym not in self.favorites:
                                continue

                    if max_price_limit > 0:
                        m5 = CANDLES_M5.get(sym)
                        last_close = m5[-1]["close"] if m5 else None
                        if last_close is not None and last_close > max_price_limit:
                            if sym not in self.favorites:
                                continue

                    out.append((sym, vol_24h, pct_change, natr, corr, spike, boi))

                return out

            # --- СТАРТ СБОРА ---
            async def _run_build_and_apply():
                raw = await asyncio.to_thread(_build_items)
                QtCore.QTimer.singleShot(
                    0,
                    lambda: self._apply_new_items_from_raw(raw, None, False, None)
                )

            try:
                asyncio.create_task(_run_build_and_apply())
            except RuntimeError:
                raw = _build_items()
                QtCore.QTimer.singleShot(
                    0,
                    lambda: self._apply_new_items_from_raw(raw, None, False, None)
                )

            self.check_signal_alerts()
            return
        finally:  # FIX: ключевая строка
            self._symbol_refresh_in_progress = False

    def _on_symbol_selected(self):
        # import inspect
        # import time
        #
        # caller = inspect.stack()[1]
        # print(
        #     f"[ON_SYMBOL_SELECTED_CALL] "
        #     f"{time.strftime('%Y-%m-%d %H:%M:%S')} | "
        #     f"from={caller.function} "
        #     f"({caller.filename.split('/')[-1]}:{caller.lineno})"
        # )

        """При выборе символа показываем график (если есть выбор; иначе оставляем старый)"""
        items = self.symbolsPanel.selectedItems()
        if not items:
            # Если символ исчез из списка, но графики уже были — не очищаем
            if getattr(self.chartPanel, "_current_symbol", None):
                return
            # если график был пуст — очищаем как раньше
            self.chartPanel.set_data([], [])
            self.chartPanel_m5.set_data([], [])
            return

        sym_full = items[0].data(1, QtCore.Qt.UserRole)

        # === Проверяем, загружены ли все данные ===
        def _data_ready():
            return (
                    sym_full in CANDLES_H1
                    and sym_full in CANDLES_H1_FUT
                    and sym_full in CANDLES_M5
                    and sym_full in CANDLES_M5_FUT
                    and len(CANDLES_H1[sym_full]) > 0
                    and len(CANDLES_H1_FUT[sym_full]) > 0
                    and len(CANDLES_M5[sym_full]) > 0
                    and len(CANDLES_M5_FUT[sym_full]) > 0
            )

        if not _data_ready():
            # Если данных ещё нет — подождём 300 мс и проверим снова
            QtCore.QTimer.singleShot(300, self._on_symbol_selected)
            return
        # 🔑 СБРОС АВТОМАСШТАБА ПРИ СМЕНЕ СИМВОЛА
        self.chartPanel._auto_range_done = False
        self.chartPanel_m5._auto_range_done = False
        # --- График H1 ---
        candles = CANDLES_H1.get(sym_full, [])
        settings = QtCore.QSettings("MyCompany", "BinanceScanner")
        max_bars_h1 = settings.value("chart_h1_bars", 240, type=int)
        candles = candles[-max_bars_h1:]  # берём последние N свечей

        levels = detect_levels_for_symbol(candles, "H1", sym_full)
        self.chartPanel.set_data(candles, levels, sym_full)

        # --- График M5 ---
        candles_m5 = CANDLES_M5.get(sym_full, [])
        max_bars_m5 = settings.value("chart_m5_bars", 288, type=int)
        candles_m5 = candles_m5[-max_bars_m5:]  # берём последние N свечей

        levels_m5 = detect_levels_for_symbol(candles_m5, "M5", sym_full)
        self.chartPanel_m5.set_data(candles_m5, levels_m5, sym_full)

        # --- ВОССТАНОВЛЕНИЕ СИГНАЛОВ (ПОСЛЕ ПЕРЕРИСОВКИ) ---
        def _restore():
            if sym_full in self.signal_levels:
                for sig in self.signal_levels[sym_full]:
                    price = sig["price"]
                    if sig.get("has_main"):
                        sig["line_main"] = self.chartPanel.show_signal_line(sym_full, price, role="main")
                    if sig.get("has_m5"):
                        sig["line_m5"] = self.chartPanel_m5.show_signal_line(sym_full, price, role="m5")

        QtCore.QTimer.singleShot(0, _restore)

        # --- Подсветка ордеров ---
        symbol = sym_full
        if symbol in self._orders_by_symbol:
            orders = self._orders_by_symbol[symbol]
            last_side = None

            if isinstance(orders, list):
                for price, side in orders:
                    self.chartPanel_m5.show_order_line(price, side, symbol)
                    last_side = side
            else:
                price, side = orders
                self.chartPanel_m5.show_order_line(price, side, symbol)
                last_side = side

            if last_side:
                self.highlight_symbol(symbol, True, last_side)

        # Если нет ордера — не трогаем подсветку

    def refresh_current_symbol_charts(self):
        sym = getattr(self.chartPanel, "_current_symbol", None)
        if not sym:
            return

        # === H1 ===
        candles = CANDLES_H1.get(sym, [])
        settings = QtCore.QSettings("MyCompany", "BinanceScanner")
        max_bars_h1 = settings.value("chart_h1_bars", 240, type=int)
        candles_h1 = candles[-max_bars_h1:]

        levels_h1 = detect_levels_for_symbol(candles_h1, "H1", sym)
        self.chartPanel.set_data(candles_h1, levels_h1, sym)

        # === M5 ===
        candles = CANDLES_M5.get(sym, [])
        max_bars_m5 = settings.value("chart_m5_bars", 288, type=int)
        candles_m5 = candles[-max_bars_m5:]

        levels_m5 = detect_levels_for_symbol(candles_m5, "M5", sym)
        self.chartPanel_m5.set_data(candles_m5, levels_m5, sym)

    def _on_symbol_clicked_copy(self, item):
        full_symbol = item.data(1, QtCore.Qt.UserRole)
        QtWidgets.QApplication.clipboard().setText(full_symbol)

    def show_order_on_chart(self, symbol: str, price: float, side: str):
        items = self.symbolsPanel.selectedItems()
        if not items:
            return
        # тикер хранится в 1-й колонке (там "Тикер")
        current_symbol = items[0].data(1, QtCore.Qt.UserRole)
        if current_symbol == symbol:
            self.chartPanel_m5.show_order_line(price, side, symbol)  # ← только M5

    def register_order(self, symbol: str, price: float, side: str):
        """Поддержка нескольких ордеров по одному инструменту."""
        side = side.lower()

        # получить список ордеров
        orders = self._orders_by_symbol.get(symbol)
        if not isinstance(orders, list):
            orders = []

        # не добавлять дубликат
        if not any(abs(p - price) < 1e-9 and s == side for p, s in orders):
            orders.append((price, side))

        self._orders_by_symbol[symbol] = orders

        # подсветить символ (по последнему ордеру)
        self.highlight_symbol(symbol, True, side)

        # если график сейчас открыт - показать линию
        if self.chartPanel_m5._current_symbol == symbol:
            self.chartPanel_m5.show_order_line(price, side, symbol)

    def unregister_order(self, symbol: str, price: float, side: str):
        """Удаляет КОНКРЕТНЫЙ ордер. Если по инструменту остаются ордера — метку не убираем."""
        side = side.lower()

        remaining_orders = []

        # --- удалить конкретный (price, side)
        if symbol in self._orders_by_symbol:
            orders = self._orders_by_symbol[symbol]
            if isinstance(orders, list):
                for p, s in orders:
                    if not (abs(p - price) < 1e-9 and s == side):
                        remaining_orders.append((p, s))

                if remaining_orders:
                    self._orders_by_symbol[symbol] = remaining_orders
                else:
                    self._orders_by_symbol.pop(symbol, None)
            else:
                # старый формат (price, side)
                self._orders_by_symbol.pop(symbol, None)

        # --- подсветка ---
        if symbol in self._orders_by_symbol:
            # оставить подсветку по последнему оставшемуся ордеру
            _, last_side = self._orders_by_symbol[symbol][-1]
            self.highlight_symbol(symbol, True, last_side)
        else:
            # ордеров больше нет
            self.highlight_symbol(symbol, False, None)

        # --- удалить линию на графике конкретного ордера ---
        try:
            self.chartPanel_m5.remove_order_line(price, side, symbol)
        except Exception as e:
            print("remove_order_line error:", e)

        # --- подчищаем _order_lines ---
        if symbol in self.chartPanel_m5._order_lines:
            self.chartPanel_m5._order_lines[symbol] = [
                (line, pr, sd)
                for (line, pr, sd) in self.chartPanel_m5._order_lines[symbol]
                if not (sd == side and abs(pr - price) <= max(1e-9, pr * 1e-6))
            ]

    def apply_filters_and_refresh(self):
        """
        Применяет текущие фильтры из настроек и сразу пересчитывает список символов.
        """
        try:
            self.refresh_symbol_list()
        except Exception as e:
            print("Ошибка при обновлении фильтров:", e)

    async def _periodic_indicators_refresh(self):
        """Периодически пересчитывает индикаторы и обновляет таблицу.
        Защита: H1 обновляется не чаще 1 часа (если выбран)."""
        tf_map = {"M5": 300, "H1": 3600}

        # время последнего обновления H1 в рамках этого цикла (0 = ещё не было)
        self._last_h1_update = getattr(self, "_last_h1_update", 0.0)

        while True:
            try:
                # берём все активные TF индикаторов
                ind_tfs = [
                    getattr(self.mw, "ind_vol_tf", "M5"),
                    getattr(self.mw, "ind_izm_tf", "M5"),
                    getattr(self.mw, "ind_natr_tf", "M5"),
                    getattr(self.mw, "ind_corr_tf", "M5"),
                    getattr(self.mw, "ind_spike_tf", "M5"),
                ]

                # минимальный (самый частый) интервал между циклами
                min_tf = min(ind_tfs, key=lambda tf: tf_map.get(tf, 300))
                sleep_time = tf_map.get(min_tf, 300)

                await asyncio.sleep(sleep_time)

                now = time.time()
                want_h1 = any(tf.upper() == "H1" for tf in ind_tfs)

                # решаем, можно ли обновлять H1 (не чаще часа)
                if want_h1:
                    if self._last_h1_update == 0.0 or (now - self._last_h1_update) >= 3600:
                        can_update_h1 = True
                    else:
                        can_update_h1 = False
                else:
                    can_update_h1 = False  # если H1 не нужен — не обновляем

                # M5 обновляем всегда (если нужно можно добавить логику аналогично)
                can_update_m5 = True

                # вызываем refresh_symbol_list с флагами
                try:
                    self.refresh_symbol_list(force=True, update_h1=can_update_h1, update_m5=can_update_m5)
                except TypeError:
                    # fallback на старый интерфейс (на случай, если где-то не обновлён)
                    self.refresh_symbol_list(force=True)

                self.check_signal_alerts()

                # если обновили H1 — фиксируем время
                if want_h1 and can_update_h1:
                    self._last_h1_update = now

            except Exception as e:
                print("Ошибка при обновлении индикаторов:", e)
                await asyncio.sleep(5)

    def _apply_new_items_from_raw(self, raw_items, prev_symbol, had_selection, old_count):
        """Преобразуем данные из фона в QTreeWidgetItem и атомарно обновляем виджет."""
        # ===== СОХРАНЯЕМ ТЕКУЩИЙ ВЫБРАННЫЙ SYMBOL =====
        selected_symbol = None
        cur = self.symbolsPanel.currentItem()
        if cur:
            selected_symbol = cur.data(1, QtCore.Qt.UserRole)
        # ============================================
        new_items = []
        for sym, vol_24h, pct_change, natr, corr, spike, boi in raw_items:
            star = "★" if sym in self.favorites else ""
            item = QtWidgets.QTreeWidgetItem([
                star,
                sym.replace("USDT", ""),
                f"{vol_24h / 1e6:.0f}",
                f"{pct_change:.0f}" if pct_change != 0 else "0",
                f"{natr:.1f}" if natr != 0 else "0.0",
                f"{corr:.0f}" if corr is not None else "",
                f"{spike:.1f}" if spike is not None else "",
                f"{boi:.1f}" if boi is not None else "",
            ])

            # сохраняем тикер в 1-ю колонку (там "Тикер")
            item.setData(1, QtCore.Qt.UserRole, sym)

            # Выравнивание всех колонок, включая 6-ю ("Всп")
            item.setTextAlignment(0, QtCore.Qt.AlignCenter)
            item.setTextAlignment(1, QtCore.Qt.AlignLeft)
            item.setTextAlignment(2, QtCore.Qt.AlignCenter)
            item.setTextAlignment(3, QtCore.Qt.AlignCenter)
            item.setTextAlignment(4, QtCore.Qt.AlignCenter)
            item.setTextAlignment(5, QtCore.Qt.AlignCenter)
            item.setTextAlignment(6, QtCore.Qt.AlignCenter)  # ← добавлено
            item.setTextAlignment(7, QtCore.Qt.AlignCenter)  # ← добавлено

            new_items.append(item)

        self.symbolsPanel.setUpdatesEnabled(False)
        self.symbolsPanel.blockSignals(True)
        self.symbolsPanel.clear()

        # === ШАГ 6: избранные наверх ===
        fav_items = [it for it in new_items if it.data(1, QtCore.Qt.UserRole) in self.favorites]
        other_items = [it for it in new_items if it.data(1, QtCore.Qt.UserRole) not in self.favorites]
        new_items = fav_items + other_items
        # =================================

        if new_items:
            self.symbolsPanel.addTopLevelItems(new_items)
            # ===== ВОССТАНАВЛИВАЕМ ВЫДЕЛЕНИЕ =====
            if selected_symbol:
                for i in range(self.symbolsPanel.topLevelItemCount()):
                    it = self.symbolsPanel.topLevelItem(i)
                    if it.data(1, QtCore.Qt.UserRole) == selected_symbol:
                        self.symbolsPanel.setCurrentItem(it)
                        self.symbolsPanel.scrollToItem(
                            it,
                            QtWidgets.QAbstractItemView.PositionAtCenter
                        )
                        break
            # ===================================
        self.symbolsPanel.blockSignals(False)
        self.symbolsPanel.setUpdatesEnabled(True)
        QtCore.QTimer.singleShot(0, self.symbolsPanel.viewport().update)

        # восстановим подсветку ордеров
        for sym, orders in self._orders_by_symbol.items():

            # список ордеров [(price, side), (price2, side2), ...]
            if isinstance(orders, list) and orders:
                # подсветка берётся по последнему ордеру
                last_price, last_side = orders[-1]
                self.highlight_symbol(sym, True, last_side)

            # старый формат (price, side) — на всякий случай
            elif isinstance(orders, tuple) and len(orders) == 2:
                price, side = orders
                self.highlight_symbol(sym, True, side)

        # ===== первый авто-выбор (ТОЛЬКО ОДИН РАЗ) =====
        if not getattr(self, "_first_autoselect_done", False):
            if self.symbolsPanel.topLevelItemCount() > 0:
                it = self.symbolsPanel.topLevelItem(0)

                self.symbolsPanel.blockSignals(True)
                self.symbolsPanel.setCurrentItem(it)
                self.symbolsPanel.blockSignals(False)

                # один-единственный вызов графика
                QtCore.QTimer.singleShot(0, self._on_symbol_selected)

                # старт 5-минутного таймера
                if self.chartPanel_m5.tf == "M5":
                    QtCore.QTimer.singleShot(
                        0,
                        self.chartPanel_m5._start_5min_timer
                    )
                # старт часового таймера
                if self.chartPanel.tf == "H1":
                    QtCore.QTimer.singleShot(
                        0,
                        self.chartPanel._start_h1_timer
                    )

                # старт WebSocket (один раз)
                if not hasattr(self, "_ws_started"):
                    asyncio.create_task(self.chartPanel_m5.ws_m5_listener())  # M5
                    asyncio.create_task(self.chartPanel.ws_h1_listener())  # H1
                    self._ws_started = True

                self._first_autoselect_done = True

        # 🔒 ТОЛЬКО ТЕПЕРЬ разрешаем новые refresh
        self._symbol_refresh_in_progress = False
