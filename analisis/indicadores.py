"""
indicadores.py
==============
Funciones de cálculo de indicadores técnicos y utilidades de mercado.

CAMBIOS v2.1 (2026-04):
  - SCORE_APROBACION: 11  (umbral actual — EMA 9/21 + S/R + RSI recalibrado
                                 directo — el rango efectivo real bajó ~1 pt en señales
                                 legítimas. 12 evita filtrar altcoins con ADX/RSI neutro
                                 pero tendencia macro perfectamente alineada.)
  - calc_adx: guard robusto ante listas vacías y divisiones por cero;
              logging claro cuando devuelve 0.0 por datos insuficientes.
              AHORA RETORNA dict con 'adx', 'di_plus', 'di_minus'.
  - calc_zona_valor: umbral_exacto 0.3% → 0.5%, umbral_cercano 0.5% → 1.0%
                     (0.5% era demasiado estricto para altcoins de bajo precio)
"""
import logging
import math
from bitunix_api import _get

logger = logging.getLogger(__name__)

# CONSTANTES DE CONFIGURACIÓN
REJECTION_RATIO = 0.55

# CALIBRACIÓN 2026-04 v2.4:
# SCORE_APROBACION = 13 (valor activo).
# Se añadió el BLOQUE 7 (BB position, rango −2 a +2).
# El 1D solo penaliza (cap), nunca suma → no domina el score.
# Señal típica buena: 14–19 pts. Señal con BB desfavorable: −1/−2 pts adicionales
# exactamente donde hay más riesgo (precio estirado contra la señal).
SCORE_APROBACION = 13

SCORE_FUERTE = 18       # Señal fuerte — 1H y 4H completamente sincronizados
RR_NORMAL = 1.7
RR_REDUCIDO = 1.6
RSI_NEUTRAL = 50.0  # Evita "magic numbers"


def is_near_ema(price: float, ema: float, threshold_pct: float = 0.002) -> bool:
    """Verifica si el precio está en la 'zona de reacción' de la EMA (±0.2% por defecto)"""
    if ema == 0:
        return False
    return abs(price - ema) / ema <= threshold_pct


async def get_klines(session, symbol: str, interval: str, limit: int = 100) -> list:
    """
    Obtiene y normaliza las velas de la API de Bitunix.
    """
    try:
        params = {'symbol': symbol, 'interval': interval, 'limit': str(limit)}
        data = await _get(session, '/api/v1/futures/market/kline', params)
        if not data:
            return []
    except Exception as error:
        logger.error("Error klines %s %s: %s", symbol, interval, error)
        return []

    # Normalización de respuesta (dict o list)
    candles_raw = data if isinstance(data, list) else data.get('list', data.get('data', []))
    if not candles_raw:
        return []

    return _procesar_lista_velas(candles_raw)


def _procesar_lista_velas(candles_raw: list) -> list:
    """Helper para normalizar el formato de velas"""
    result = []
    for candle in candles_raw:
        try:
            result.append(_parsear_vela_individual(candle))
        except (ValueError, TypeError, KeyError, IndexError):
            continue

    return sorted(result, key=lambda x: x['time'])


def _parsear_vela_individual(candle) -> dict:
    """Extrae los datos de una vela sin importar si viene como lista o diccionario."""
    if isinstance(candle, dict):
        vol = float(candle.get('quoteVol', 0) or candle.get('baseVol', 0) or 0)
        return {
            'time': int(candle.get('time', 0)),
            'open': float(candle.get('open', 0)),
            'high': float(candle.get('high', 0)),
            'low': float(candle.get('low', 0)),
            'close': float(candle.get('close', 0)),
            'volume': vol,
        }

    # Si viene como lista [time, open, high, low, close, vol, ...]
    vol_idx = 0.0
    for i in [5, 6, 7]:
        if len(candle) > i and float(candle[i]) > 0:
            vol_idx = float(candle[i])
            break

    return {
        'time': int(candle[0]), 'open': float(candle[1]),
        'high': float(candle[2]), 'low': float(candle[3]),
        'close': float(candle[4]), 'volume': vol_idx,
    }


def calc_ema(closes: list, period: int) -> list:
    """Calcula la Media Móvil Exponencial (EMA)"""
    if len(closes) < period:
        return []

    smoothing = 2 / (period + 1)
    emas = [sum(closes[:period]) / period]

    for price in closes[period:]:
        emas.append(price * smoothing + emas[-1] * (1 - smoothing))
    return emas


def calc_sma(closes: list, period: int) -> list:
    """
    Calcula la Media Móvil Simple (SMA).

    Retorna lista de la misma longitud que closes.
    Los primeros (period-1) valores son None (datos insuficientes).
    """
    if len(closes) < period:
        return []

    smas = [None] * (period - 1)

    for i in range(period - 1, len(closes)):
        sma = sum(closes[i - period + 1:i + 1]) / period
        smas.append(round(sma, 8))

    return smas


def calc_rsi(closes: list, period: int = 14) -> float:
    """Calcula el Índice de Fuerza Relativa (RSI)"""
    if len(closes) < period + 1:
        return RSI_NEUTRAL

    gains = []
    losses = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i-1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs_value = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs_value)), 2)


def calc_adx(candles: list, period: int = 14) -> dict:
    """
    Calcula el Índice Promedio de Movimiento Direccional (ADX) y sus componentes +DI y -DI.

    FIX v2.1:
    - Requiere al menos (period * 2 + 2) velas para producir un ADX válido.
      Con period=14 necesita mínimo 30 velas. Antes requería solo (period+1)=15,
      lo que generaba dx_values vacío y devolvía 0.0 silenciosamente
      (causaba rechazo erróneo por "ADX=0.0 < 12").
    - Logging explícito cuando se devuelve 0.0 para distinguir
      "mercado sin momentum" de "datos insuficientes".
    - Guard en la división de dx_values para evitar ZeroDivisionError.
    
    🔥 NUEVO v2.2: Ahora retorna un dict con 'adx', 'di_plus', 'di_minus'
    """
    min_required = period * 2 + 2
    resultado_vacio = {'adx': 0.0, 'di_plus': 0.0, 'di_minus': 0.0}
    
    if not candles or len(candles) < min_required:
        logger.debug(
            "calc_adx: datos insuficientes (%d velas, necesita %d) → devuelve 0.0",
            len(candles) if candles else 0,
            min_required,
        )
        return resultado_vacio

    pdm, mdm, trl = [], [], []
    for i in range(1, len(candles)):
        h_diff = candles[i]['high'] - candles[i-1]['high']
        l_diff = candles[i-1]['low'] - candles[i]['low']

        pdm.append(h_diff if h_diff > l_diff and h_diff > 0 else 0)
        mdm.append(l_diff if l_diff > h_diff and l_diff > 0 else 0)
        trl.append(max(
            candles[i]['high'] - candles[i]['low'],
            abs(candles[i]['high'] - candles[i-1]['close']),
            abs(candles[i]['low'] - candles[i-1]['close'])
        ))

    def smooth(lst, p):
        if not lst or len(lst) < p:
            return []
        current_sum = sum(lst[:p])
        results = [current_sum]
        for val in lst[p:]:
            results.append(results[-1] - results[-1] / p + val)
        return results

    atr_s = smooth(trl, period)
    pdi_s = smooth(pdm, period)
    mdi_s = smooth(mdm, period)

    if not atr_s or not pdi_s or not mdi_s:
        logger.debug("calc_adx: smooth devolvió lista vacía → 0.0")
        return resultado_vacio

    dx_values = []
    for i in range(len(atr_s)):
        if atr_s[i] == 0:
            continue
        pdi = 100 * pdi_s[i] / atr_s[i]
        mdi = 100 * mdi_s[i] / atr_s[i]
        diff_sum = pdi + mdi
        dx_values.append(100 * abs(pdi - mdi) / diff_sum if diff_sum > 0 else 0.0)

    if len(dx_values) < period:
        logger.debug(
            "calc_adx: dx_values insuficientes (%d < %d) → 0.0", len(dx_values), period
        )
        return resultado_vacio

    adx_val = sum(dx_values[-period:]) / period
    
    # 🔥 Obtener los últimos valores de +DI y -DI
    di_plus = pdi_s[-1] if pdi_s else 0.0
    di_minus = mdi_s[-1] if mdi_s else 0.0

    return {
        'adx': round(adx_val, 2),
        'di_plus': round(di_plus, 2),
        'di_minus': round(di_minus, 2),
    }


def calc_bollinger(closes: list, period: int = 20, std_dev: float = 2.0) -> dict:
    """Calcula las Bandas de Bollinger y métricas de expansión/compresión."""
    if len(closes) < period:
        return {
            'ancho_pct': 5.0, 'squeeze': False, 'squeeze_fuerte': False,
            'banda_sup': 0, 'banda_inf': 0, 'media': 0, 'expansion': False
        }

    subset = closes[-period:]
    sma = sum(subset) / period
    variance = sum((x - sma) ** 2 for x in subset) / period
    stdev = variance ** 0.5

    upper = sma + std_dev * stdev
    lower = sma - std_dev * stdev
    width = (upper - lower) / sma * 100 if sma > 0 else 5.0

    return {
        'ancho_pct': round(width, 2),
        'squeeze': width < 4.0,
        'squeeze_fuerte': width < 2.0,
        'expansion': width > 8.0,
        'banda_sup': upper,
        'banda_inf': lower,
        'media': sma,
    }


def calc_bb_position_score(
    candles_entrada: list,
    side: str,
    resumen: list,
    candles_1d: list = None,
    period: int = 20,
    std_dev: float = 2.0,
) -> tuple[int, dict]:
    """
    BLOQUE 7 — Posición del precio dentro de las Bandas de Bollinger (−2 a +2).

    DISEÑO v2.4 (2026-04):
    ──────────────────────────────────────────────────────────────────
    Mide el %B del precio en el TF de entrada (1H o 15M):
        %B = (precio − BB_lower) / (BB_upper − BB_lower)
        0.0 = precio en BB lower, 1.0 = precio en BB upper

    LONG:
        %B ≤ 0.20 → cerca de BB inferior (zona de valor) → +2 pts
        %B 0.20–0.45 → zona media-baja                  → +1 pt
        %B 0.45–0.75 → zona media (sin ventaja clara)   →  0 pts
        %B 0.75–0.90 → cerca de BB superior (estirado)  → −1 pt
        %B > 0.90    → en/sobre BB superior (riesgo)    → −2 pts

    SHORT: lógica exactamente inversa (%B alto = bueno, bajo = malo).

    ROL DEL 1D (solo penaliza — nunca suma puntos positivos):
    ──────────────────────────────────────────────────────────────────
    Si las velas 1D están disponibles, se calcula el %B diario.
    Si el %B diario también está en zona extrema CONTRA la señal
    (p.ej. LONG con %B_1d > 0.85), confirma que la penalización
    es correcta y hace cap al −2 (no penaliza doble).
    El 1D NUNCA suma puntos: su rol es confirmar el riesgo, no inflar el score.
    Así el 1D informa sin dominar el veredicto.

    PARÁMETROS:
        candles_entrada: velas del TF de entrada (1H o 15M), ya cerradas
        side:            'LONG' o 'SHORT'
        resumen:         lista de log
        candles_1d:      velas diarias (opcional, solo para confirmación de cap)
        period:          período de la SMA de BB (default 20)
        std_dev:         desviaciones estándar (default 2.0)

    RETORNA: (puntos, detalle_dict)
    """
    detalle = {
        'bb_upper': 0.0, 'bb_lower': 0.0, 'bb_media': 0.0,
        'pct_b': 0.5, 'pts_bb': 0,
        'bb_1d_pct_b': None, 'bb_1d_confirma_cap': False,
    }

    if not candles_entrada or len(candles_entrada) < period + 5:
        resumen.append("  ➖ BB posición: datos insuficientes → 0 pts")
        return 0, detalle

    closes = [c['close'] for c in candles_entrada]
    bb = calc_bollinger(closes, period=period, std_dev=std_dev)

    upper = bb['banda_sup']
    lower = bb['banda_inf']
    media = bb['media']

    if upper <= lower or lower <= 0:
        resumen.append("  ➖ BB posición: bandas inválidas → 0 pts")
        return 0, detalle

    precio = closes[-1]
    banda_ancho = upper - lower
    pct_b = (precio - lower) / banda_ancho  # 0.0 = lower, 1.0 = upper

    detalle['bb_upper'] = round(upper, 6)
    detalle['bb_lower'] = round(lower, 6)
    detalle['bb_media'] = round(media, 6)
    detalle['pct_b'] = round(pct_b, 4)

    # ── Puntuación por %B según dirección ──────────────────────────────────
    if side == 'LONG':
        if pct_b <= 0.20:
            pts = 2
            zona = f"cerca BB lower (%B={pct_b:.2f})"
            icono = "🔥"
        elif pct_b <= 0.45:
            pts = 1
            zona = f"zona media-baja (%B={pct_b:.2f})"
            icono = "✅"
        elif pct_b <= 0.75:
            pts = 0
            zona = f"zona media (%B={pct_b:.2f})"
            icono = "➖"
        elif pct_b <= 0.90:
            pts = -1
            zona = f"cerca BB upper (%B={pct_b:.2f}) — estirado"
            icono = "⚠️"
        else:
            pts = -2
            zona = f"en/sobre BB upper (%B={pct_b:.2f}) — riesgo alto"
            icono = "🚫"
    else:  # SHORT — lógica inversa
        if pct_b >= 0.80:
            pts = 2
            zona = f"cerca BB upper (%B={pct_b:.2f})"
            icono = "🔥"
        elif pct_b >= 0.55:
            pts = 1
            zona = f"zona media-alta (%B={pct_b:.2f})"
            icono = "✅"
        elif pct_b >= 0.25:
            pts = 0
            zona = f"zona media (%B={pct_b:.2f})"
            icono = "➖"
        elif pct_b >= 0.10:
            pts = -1
            zona = f"cerca BB lower (%B={pct_b:.2f}) — estirado"
            icono = "⚠️"
        else:
            pts = -2
            zona = f"en/sobre BB lower (%B={pct_b:.2f}) — riesgo alto"
            icono = "🚫"

    # ── Confirmación 1D (solo cap, nunca suma) ─────────────────────────────
    bb_1d_confirma_cap = False
    if candles_1d and len(candles_1d) >= period + 5:
        closes_1d = [c['close'] for c in candles_1d]
        bb_1d = calc_bollinger(closes_1d, period=period, std_dev=std_dev)
        upper_1d = bb_1d['banda_sup']
        lower_1d = bb_1d['banda_inf']
        if upper_1d > lower_1d > 0:
            ancho_1d = upper_1d - lower_1d
            pct_b_1d = (closes_1d[-1] - lower_1d) / ancho_1d
            detalle['bb_1d_pct_b'] = round(pct_b_1d, 4)

            # 1D confirma el riesgo si también está estirado en la dirección contraria
            if side == 'LONG' and pct_b_1d > 0.85 and pts >= 0:
                # No añade penalización extra, solo evita que puntos positivos
                # sobrevivan cuando el contexto diario es de sobrecompra
                pts = min(pts, 0)
                bb_1d_confirma_cap = True
            elif side == 'SHORT' and pct_b_1d < 0.15 and pts >= 0:
                pts = min(pts, 0)
                bb_1d_confirma_cap = True

            detalle['bb_1d_confirma_cap'] = bb_1d_confirma_cap
            if bb_1d_confirma_cap:
                resumen.append(
                    f"  ⚠️  BB 1D confirma zona extrema (%B_1d={pct_b_1d:.2f})"
                    f" — bonus BB anulado por contexto diario adverso"
                )

    pts = max(-2, min(pts, 2))
    detalle['pts_bb'] = pts

    resumen.append(
        f"  {icono} BB posición ({side}): {zona} → {'+' if pts >= 0 else ''}{pts} pts"
    )

    return pts, detalle


def detect_rejection_candle(candles: list, side: str) -> tuple[bool, str]:
    """Detecta patrones de velas japonesas de rechazo (Martillo, Engulfing, etc.)"""
    if len(candles) < 3:
        return False, ""

    last, prev = candles[-1], candles[-2]
    open_p, high_p, low_p, close_p = last['open'], last['high'], last['low'], last['close']

    body = abs(close_p - open_p)
    full_range = high_p - low_p if high_p != low_p else 0.0001

    if (body / full_range) < 0.10:
        return False, "Doji — indecisión"

    if side == 'LONG':
        return _check_long_rejection(last, prev, body, full_range)

    return _check_short_rejection(last, prev, body, full_range)


def _check_long_rejection(last, prev, body, full_range) -> tuple[bool, str]:
    """Helper para reducir complejidad en detect_rejection_candle"""
    c, o, l = last['close'], last['open'], last['low']
    lower_wick = min(o, c) - l

    if lower_wick >= body * REJECTION_RATIO and c > o:
        return True, "Martillo alcista"
    if c > prev['high'] and o < prev['low'] and c > o:
        return True, "Bullish engulfing"
    if c > o and (body / full_range) > 0.6:
        return True, "Vela alcista fuerte"
    return False, ""


def _check_short_rejection(last, prev, body, full_range) -> tuple[bool, str]:
    """Helper para reducir complejidad en detect_rejection_candle"""
    c, o, h = last['close'], last['open'], last['high']
    upper_wick = h - max(o, c)

    if upper_wick >= body * REJECTION_RATIO and c < o:
        return True, "Estrella fugaz"
    if c < prev['low'] and o > prev['high'] and c < o:
        return True, "Bearish engulfing"
    if c < o and (body / full_range) > 0.6:
        return True, "Vela bajista fuerte"
    return False, ""


def calc_atr(candles: list, period: int = 14) -> float:
    """Calcula el Average True Range (ATR)"""
    if len(candles) < period + 1:
        return 0.0

    true_ranges = []
    for i in range(1, len(candles)):
        high = candles[i]['high']
        low = candles[i]['low']
        prev_close = candles[i-1]['close']

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )
        true_ranges.append(tr)

    if len(true_ranges) < period:
        return 0.0

    atr = sum(true_ranges[:period]) / period

    for tr in true_ranges[period:]:
        atr = (atr * (period - 1) + tr) / period

    return atr


def detectar_resistencias_soportes(candles: list, period: int = 20) -> dict:
    """
    Identifica niveles clave de S/R usando pivot points reales (fractales de 2 lados).

    MEJORADO v2.2:
    - Antes: tomaba el high/low máximo del rango — eso es el extremo absoluto, no un nivel
      donde el precio realmente *rechazó*. Un pico puntual sin repetición no es S/R.
    - Ahora: usa fractales (pivot high / pivot low) con ventana configurable.
      Un pivot high = vela cuyo high es mayor que las N velas de cada lado.
      Un pivot low  = vela cuyo low  es menor que las N velas de cada lado.
      Estos son los niveles donde el precio históricamente giró.
    - Retorna también la lista completa de pivotes para que calc_sr_score pueda
      calcular la distancia del entry a cada nivel y puntuar con precisión.
    - Mantiene retro-compatibilidad: 'resistencia' y 'soporte' siguen presentes.
    """
    resultado_vacio = {
        'resistencia': 0, 'soporte': 0,
        'zona_resistencia': 0, 'zona_soporte': 0,
        'pivot_highs': [], 'pivot_lows': [],
        'nivel_sr_mas_cercano': 0, 'tipo_nivel_cercano': 'NINGUNO',
    }

    if not candles or len(candles) < period:
        return resultado_vacio

    ventana = max(2, period // 8)  # ventana adaptativa: para period=20 → ventana=2
    subset = candles[-period:]
    n = len(subset)

    pivot_highs = []
    pivot_lows  = []

    for i in range(ventana, n - ventana):
        c = subset[i]
        vecinos_h = [subset[j]['high'] for j in range(i - ventana, i + ventana + 1) if j != i]
        vecinos_l = [subset[j]['low']  for j in range(i - ventana, i + ventana + 1) if j != i]

        if c['high'] >= max(vecinos_h):
            pivot_highs.append(c['high'])
        if c['low'] <= min(vecinos_l):
            pivot_lows.append(c['low'])

    # Fallback: si no hay pivotes suficientes usamos ordenación clásica
    if len(pivot_highs) < 2:
        raw_highs = sorted([c['high'] for c in subset], reverse=True)
        pivot_highs = raw_highs[:5]
    if len(pivot_lows) < 2:
        raw_lows = sorted([c['low'] for c in subset])
        pivot_lows = raw_lows[:5]

    resistencia      = max(pivot_highs)
    soporte          = min(pivot_lows)
    zona_resistencia = sum(sorted(pivot_highs, reverse=True)[:5]) / min(5, len(pivot_highs))
    zona_soporte     = sum(sorted(pivot_lows)[:5]) / min(5, len(pivot_lows))

    return {
        'resistencia':      resistencia,
        'soporte':          soporte,
        'zona_resistencia': zona_resistencia,
        'zona_soporte':     zona_soporte,
        'pivot_highs':      sorted(pivot_highs, reverse=True),
        'pivot_lows':       sorted(pivot_lows),
        'nivel_sr_mas_cercano': 0,      # se rellena en calc_sr_score
        'tipo_nivel_cercano':   'NINGUNO',
    }


def calc_estructura_mercado(candles: list, ventana: int = 10) -> dict:
    """
    Verifica si el mercado está haciendo HH/HL (alcista) o LH/LL (bajista).

    Retorna:
        {
          'estructura':  'ALCISTA' | 'BAJISTA' | 'NEUTRAL',
          'hh_hl':       bool,
          'lh_ll':       bool,
          'ultimo_hh':   float,
          'penultimo_hh': float,
          'ultimo_hl':   float,
          'penultimo_hl': float,
          'mensaje':     str,
        }
    """
    min_candles = ventana * 3
    resultado_neutral = {
        'estructura': 'NEUTRAL', 'hh_hl': False, 'lh_ll': False,
        'ultimo_hh': 0.0, 'penultimo_hh': 0.0,
        'ultimo_hl': 0.0, 'penultimo_hl': 0.0,
        'mensaje': 'Datos insuficientes para estructura',
    }

    if not candles or len(candles) < min_candles:
        return resultado_neutral

    maximos_swing = []
    minimos_swing = []

    for i in range(ventana, len(candles) - ventana):
        bloque = candles[i - ventana: i + ventana + 1]
        precio_high = candles[i]['high']
        precio_low  = candles[i]['low']

        es_maximo = all(c['high'] <= precio_high for c in bloque)
        es_minimo  = all(c['low'] >= precio_low  for c in bloque)

        if es_maximo:
            maximos_swing.append(precio_high)
        if es_minimo:
            minimos_swing.append(precio_low)

    if len(maximos_swing) < 2 or len(minimos_swing) < 2:
        return {**resultado_neutral, 'mensaje': 'Pocos pivotes — estructura indeterminada'}

    ult_hh, pen_hh = maximos_swing[-1], maximos_swing[-2]
    ult_hl, pen_hl = minimos_swing[-1], minimos_swing[-2]

    hh_hl = (ult_hh > pen_hh) and (ult_hl > pen_hl)
    lh_ll = (ult_hh < pen_hh) and (ult_hl < pen_hl)

    if hh_hl:
        estructura = 'ALCISTA'
        msg = f"HH ({pen_hh:.4g}→{ult_hh:.4g}) + HL ({pen_hl:.4g}→{ult_hl:.4g}) ✅"
    elif lh_ll:
        estructura = 'BAJISTA'
        msg = f"LH ({pen_hh:.4g}→{ult_hh:.4g}) + LL ({pen_hl:.4g}→{ult_hl:.4g}) ✅"
    else:
        estructura = 'NEUTRAL'
        msg = "Estructura mixta — sin tendencia clara"

    return {
        'estructura': estructura,
        'hh_hl': hh_hl,
        'lh_ll': lh_ll,
        'ultimo_hh': ult_hh,
        'penultimo_hh': pen_hh,
        'ultimo_hl': ult_hl,
        'penultimo_hl': pen_hl,
        'mensaje': msg,
    }


def calc_zona_valor(candles: list, ema20: float, ema50: float, side: str) -> dict:
    """
    Detecta si el precio está retrocediendo hacia la zona de valor (EMA 20 / EMA 50).

    FIX v2.1:
    - umbral_exacto:  0.3% → 0.5%  (0.3% era irrealmente preciso para altcoins)
    - umbral_cercano: 0.5% → 1.0%  (en monedas de bajo precio, 0.5% = fracciones de centavo)
    Efecto: más señales de altcoins llegan a EXACTO/CERCANO correctamente,
            sin que esto afecte BTC (que tiene sus propios niveles de precio absoluto).

    Retorna:
        {
          'en_zona_ema20':    bool,
          'en_zona_ema50':    bool,
          'cerca_zona':       bool,
          'rebote_detectado': bool,
          'calidad':          'EXACTO' | 'CERCANO' | 'FUERA',
          'mensaje':          str,
        }
    """
    resultado_base = {
        'en_zona_ema20': False, 'en_zona_ema50': False,
        'cerca_zona': False, 'rebote_detectado': False,
        'calidad': 'FUERA', 'mensaje': 'Sin datos para zona de valor',
    }

    if not candles or len(candles) < 3:
        return resultado_base
    if ema20 <= 0 and ema50 <= 0:
        return {**resultado_base, 'mensaje': 'EMAs no disponibles'}

    precio = float(candles[-1]['close'])

    # FIX: umbrales más generosos para cubrir altcoins de bajo precio unitario
    umbral_exacto  = 0.005   # ≤ 0.5%  (antes 0.3%)
    umbral_cercano = 0.010   # ≤ 1.0%  (antes 0.5%)

    def dist(p, ref):
        return abs(p - ref) / ref if ref > 0 else 1.0

    en_ema20    = (ema20 > 0) and (dist(precio, ema20) <= umbral_exacto)
    en_ema50    = (ema50 > 0) and (dist(precio, ema50) <= umbral_exacto)
    cerca_ema20 = (ema20 > 0) and (dist(precio, ema20) <= umbral_cercano)
    cerca_ema50 = (ema50 > 0) and (dist(precio, ema50) <= umbral_cercano)

    if side == 'LONG':
        posicion_correcta_20 = (ema20 > 0) and (precio >= ema20 * 0.995)
        posicion_correcta_50 = (ema50 > 0) and (precio >= ema50 * 0.995)
    else:
        posicion_correcta_20 = (ema20 > 0) and (precio <= ema20 * 1.005)
        posicion_correcta_50 = (ema50 > 0) and (precio <= ema50 * 1.005)

    zona_exacta  = (en_ema20 and posicion_correcta_20) or (en_ema50 and posicion_correcta_50)
    zona_cercana = (cerca_ema20 and posicion_correcta_20) or (cerca_ema50 and posicion_correcta_50)

    # Detectar rebote en las últimas 2 velas cerradas
    rebote = False
    if len(candles) >= 2:
        ult = candles[-1]
        pen = candles[-2]
        if side == 'LONG':
            rebote = ult['close'] > ult['open'] and ult['close'] > pen['close']
        else:
            rebote = ult['close'] < ult['open'] and ult['close'] < pen['close']

    if zona_exacta:
        calidad = 'EXACTO'
        msg = f"Precio en zona de valor exacta {'EMA20' if en_ema20 else 'EMA50'}"
    elif zona_cercana:
        calidad = 'CERCANO'
        msg = f"Precio cerca de zona de valor {'EMA20' if cerca_ema20 else 'EMA50'}"
    else:
        calidad = 'FUERA'
        msg = "Precio fuera de zona de valor"

    if rebote and zona_cercana:
        msg += " + rebote detectado ✅"

    return {
        'en_zona_ema20': en_ema20 and posicion_correcta_20,
        'en_zona_ema50': en_ema50 and posicion_correcta_50,
        'cerca_zona': zona_cercana,
        'rebote_detectado': rebote,
        'calidad': calidad,
        'mensaje': msg,
    }


# ═══════════════════════════════════════════════════════════════════
# 🔥 SMART MONEY FLOW CLOUD — traducido de Pine Script (BOSWaves)
# ═══════════════════════════════════════════════════════════════════

def calc_smf(
    candles: list,
    mf_len: int = 24,
    mf_smooth: int = 5,
    mf_power: float = 1.2,
    atr_len: int = 14,
    basis_len: int = 34,
    basis_smooth: int = 3,
    min_mult: float = 0.9,
    max_mult: float = 2.2,
) -> dict:
    """
    Calcula el Smart Money Flow Cloud completo.
    """
    min_candles = max(mf_len, basis_len + basis_smooth, atr_len) + 10
    if not candles or len(candles) < min_candles:
        return {
            'money_flow': 0.0, 'mf_strength': 0.0, 'mf_mult': min_mult,
            'basis': 0.0, 'upper': 0.0, 'lower': 0.0, 'atr': 0.0,
            'strength_pct': 0.0, 'direction': 'NEUTRAL', 'ok': False,
        }

    clv_vol = []
    for c in candles:
        h, l, cl, vol = c['high'], c['low'], c['close'], c['volume']
        if h == l:
            clv_vol.append(0.0)
        else:
            clv = ((cl - l) - (h - cl)) / (h - l)
            clv_vol.append(clv * vol)

    mf_raw_series = []
    for i in range(len(clv_vol)):
        if i < mf_len - 1:
            mf_raw_series.append(0.0)
            continue
        window = clv_vol[i - mf_len + 1: i + 1]
        num = sum(window)
        den = sum(abs(x) for x in window)
        mf_raw_series.append(num / den if den != 0 else 0.0)

    def ema_series(series, period):
        k = 2 / (period + 1)
        result = [series[0]]
        for v in series[1:]:
            result.append(v * k + result[-1] * (1 - k))
        return result

    mf_sm_series = ema_series(mf_raw_series, mf_smooth) if mf_smooth > 1 else mf_raw_series

    mf_sm = mf_sm_series[-1]
    mf_strength = min(1.0, max(0.0, abs(mf_sm) ** mf_power))
    mf_mult = min_mult + (max_mult - min_mult) * mf_strength

    closes = [c['close'] for c in candles]

    ema_raw = calc_ema(closes, basis_len)
    if not ema_raw:
        return {
            'money_flow': 0.0, 'mf_strength': 0.0, 'mf_mult': min_mult,
            'basis': 0.0, 'upper': 0.0, 'lower': 0.0, 'atr': 0.0,
            'strength_pct': 0.0, 'direction': 'NEUTRAL', 'ok': False,
        }

    if basis_smooth > 1:
        basis_series = calc_ema(ema_raw, basis_smooth)
    else:
        basis_series = ema_raw
    basis = basis_series[-1] if basis_series else 0.0

    atr = calc_atr(candles, atr_len)
    upper = basis + atr * mf_mult
    lower = basis - atr * mf_mult

    price = closes[-1]
    up_span = max(upper - basis, 1e-10)
    dn_span = max(basis - lower, 1e-10)

    strength_series = []
    for i in range(max(0, len(closes) - 10), len(closes)):
        p = closes[i]
        raw = (p - basis) / up_span if p >= basis else -(basis - p) / dn_span
        v = math.tanh(raw * 1.5)
        strength_series.append(v)

    if len(strength_series) >= 3:
        strength_ema = calc_ema(strength_series, 3)
        signed = strength_ema[-1] if strength_ema else strength_series[-1]
    else:
        signed = strength_series[-1] if strength_series else 0.0

    strength_pct = round(abs(signed) * 100, 1)
    direction = 'BULL' if price > basis else ('BEAR' if price < basis else 'NEUTRAL')

    return {
        'money_flow':   round(mf_sm, 4),
        'mf_strength':  round(mf_strength, 4),
        'mf_mult':      round(mf_mult, 3),
        'basis':        round(basis, 8),
        'upper':        round(upper, 8),
        'lower':        round(lower, 8),
        'atr':          round(atr, 8),
        'strength_pct': strength_pct,
        'direction':    direction,
        'ok':           True,
    }


def evaluar_smf_contexto(smf: dict, side: str) -> tuple[bool, int, str]:
    """
    Evalúa el SMF Cloud para la Fase 2 (contexto).
    Retorna: (flujo_confirmado, penalizacion, mensaje)
    """
    if not smf.get('ok'):
        return True, 0, "SMF sin datos — omitido"

    mf = smf['money_flow']
    strength = smf['mf_strength']

    if strength < 0.15:
        return True, 0, f"SMF flujo débil ({mf:+.2f}) — mercado sin dirección clara"

    if side == 'LONG':
        if mf > 0.15:
            return True, 0, f"SMF confirma LONG ({mf:+.2f} ↑ fuerza={strength:.0%})"
        elif mf < -0.30:
            return True, -2, f"SMF contra LONG ({mf:+.2f} ↓ fuerza={strength:.0%}) → penalización -2"
        else:
            return True, 0, f"SMF neutro para LONG ({mf:+.2f})"
    else:
        if mf < -0.15:
            return True, 0, f"SMF confirma SHORT ({mf:+.2f} ↓ fuerza={strength:.0%})"
        elif mf > 0.30:
            return True, -2, f"SMF contra SHORT ({mf:+.2f} ↑ fuerza={strength:.0%}) → penalización -2"
        else:
            return True, 0, f"SMF neutro para SHORT ({mf:+.2f})"


def evaluar_smf_scoring(smf: dict, side: str, entry: float) -> tuple[int, str]:
    """
    Evalúa el SMF Cloud para la Fase 3 (scoring).
    Retorna: (puntos, mensaje) → 0 a +3 puntos (cap 3)

    CALIBRACIÓN v2.3:
    - Banda adaptativa eliminada — generaba puntos con flujo débil (25% = +2 pts).
    - Ahora solo evalúa fuerza y dirección real del flujo:
      · flujo ≥ 0.20 y fuerza ≥ 60% → +3 pts (flujo institucional real)
      · flujo ≥ 0.10 y fuerza ≥ 40% → +1 pt  (flujo moderado)
      · fuerza < 40%                 →  0 pts  (ruido, no suma)
    """
    if not smf.get('ok'):
        return 0, "SMF sin datos — sin puntos"

    mf           = smf['money_flow']
    strength_pct = smf['strength_pct']
    upper        = smf.get('upper', 0)
    lower        = smf.get('lower', 0)

    # upper/lower conservados para compatibilidad con llamadas externas
    _ = upper, lower

    # Dirección alineada con la señal
    mf_alineado = (side == 'SHORT' and mf < 0) or (side == 'LONG' and mf > 0)

    if not mf_alineado:
        return 0, f"  SMF contra la señal (mf={mf:+.3f} str={strength_pct:.0f}%) → 0 pts"

    abs_mf = abs(mf)

    # Banda adaptativa: eliminada del scoring (v2.3)
    # ─────────────────────────────────────────────────────────────────
    # ANTES (v2.2): sumaba puntos por proximidad a banda:
    #   dist_upper = abs(entry - upper) / entry
    #   if dist_upper <= 0.005: puntos += 2  → +2 pts banda superior
    #   elif dist_upper <= 0.015: puntos += 1 → +1 pt cerca de banda
    #   dist_lower = abs(entry - lower) / entry
    #   if dist_lower <= 0.005: puntos += 2  → +2 pts banda inferior
    #   elif dist_lower <= 0.015: puntos += 1 → +1 pt cerca de banda
    # PROBLEMA: con flujo 25.8% fuerza igual sumaba +2 pts (ruido).
    # SOLUCIÓN v2.3: zona de entrada ya cubierta por:
    #   BLOQUE 2 → Zona valor EMA20/50
    #   BLOQUE 5 → S/R + rebote calibrado por TF
    #   BLOQUE 6 → EMA 9/21 + distancia entry
    # SMF ahora solo evalúa fuerza real del flujo institucional:
    # ─────────────────────────────────────────────────────────────────
    if abs_mf >= 0.20 and strength_pct >= 60:
        return 3, f"  🔥 SMF fuerte alineado (mf={mf:+.3f} str={strength_pct:.0f}%) → +3 pts"
    elif abs_mf >= 0.10 and strength_pct >= 40:
        return 1, f"  ✅ SMF moderado alineado (mf={mf:+.3f} str={strength_pct:.0f}%) → +1 pt"
    else:
        return 0, f"  ➖ SMF débil/ruido (mf={mf:+.3f} str={strength_pct:.0f}%) → 0 pts"


# ═══════════════════════════════════════════════════════════════════
# 🎯 SCORING S/R + REBOTE — BLOQUE 5 del scoring (Fase 3)
# ═══════════════════════════════════════════════════════════════════

def calc_sr_score(
    candles: list,
    entry: float,
    side: str,
    resumen: list,
    period_sr: int = 40,
    umbral_zona: float = 0.008,
    umbral_cercano: float = 0.020,
    level_price_externo: float = 0.0,
    tf_minutos: int = 60,
) -> tuple[int, dict]:
    """
    BLOQUE 5 — Scoring de Soporte/Resistencia + Rebote confirmado.

    DISEÑO v2.2 (2026-04):
    ──────────────────────────────────────────────────────────────────
    El precio cercano a un nivel S/R conocido NO es suficiente — lo que
    importa es si el precio *reaccionó* ahí con una vela de rechazo.
    Eso es lo que separa una zona de valor real de un precio cualquiera.

    VENTANA DE REBOTE Y PANORAMA S/R — por timeframe:
    ──────────────────────────────────────────────────────────────────
    Cada TF tiene su propio panorama fijo calibrado para identificar
    S/R reales y rebotes con contexto suficiente:

      tf_minutos=15  (15M) → panorama 100 velas (~25h), rebote 8 velas
      tf_minutos=60  (1H)  → panorama 50  velas (~50h), rebote 6 velas
      tf_minutos=240 (4H)  → panorama 30  velas (~120h), rebote 4 velas

    La ventana de rebote (~15% del panorama) cubre el rango natural donde
    el precio reacciona desde un nivel antes de alejarse definitivamente.
    La ventana de ruptura (×2 la de rebote) detecta retesteos post-ruptura.

    PUNTUACIÓN (rango: -1 a +5, cap aplicado):
      Nivel S/R exacto (≤0.8%):
        + Rebote confirmado (vela de rechazo)   → +5
        + Toque sin vela de rechazo             → +2
      Nivel S/R cercano (0.8%-2.0%):
        + Rebote confirmado                      → +3
        + Toque sin vela de rechazo             → +1
      Ruptura reciente de S/R a favor           → +2 adicional (cap total +5)
      Precio en zona de S/R pero en contra      → -1
      Sin nivel relevante                        → 0

    level_price_externo:
      Nivel S/R adicional proveniente del BOTAI o fuente externa.
      Si está dentro del ±5% del entry, se inyecta como pivote extra.

    Parámetros:
        candles:              velas cerradas (mínimo 20)
        entry:                precio de entrada de la señal
        side:                 'LONG' o 'SHORT'
        resumen:              lista de strings para el log
        period_sr:            cuántas velas hacia atrás para buscar pivotes (default 40)
        umbral_zona:          % de distancia para nivel "exacto" (default 0.8%)
        umbral_cercano:       % de distancia para nivel "cercano" (default 2.0%)
        level_price_externo:  nivel S/R externo (ej: del BOTAI). 0 = no usar.
        tf_minutos:           duración en minutos de cada vela (60=1H, 15=15M, 240=4H)

    Retorna: (puntos, detalle_dict)
    """
    detalle = {
        'nivel_cercano': 0.0,
        'tipo_nivel': 'NINGUNO',
        'dist_pct': 0.0,
        'rebote_confirmado': False,
        'patron_rebote': '',
        'ruptura_a_favor': False,
        'pts_sr': 0,
        'ventana_rebote_velas': 0,
    }

    if not candles or len(candles) < 10 or entry <= 0:
        resumen.append("  ➖ S/R: datos insuficientes → +0")
        return 0, detalle

    # ── Panorama de velas por timeframe ─────────────────────────────────────
    # Cada TF tiene su propia "memoria" natural para identificar S/R reales:
    #   15M → 100 velas = ~25 horas de contexto  (rebote: últimas 8 velas)
    #   1H  → 50  velas = ~50 horas de contexto  (rebote: últimas 6 velas)
    #   4H  → 30  velas = ~120 horas de contexto (rebote: últimas 4 velas)
    # La ventana de rebote es proporcional: ~15-20% del panorama total.
    # La ventana de ruptura es el doble de la de rebote.
    _panorama_map = {
        15:  {'period_sr': 200, 'ventana_rebote': 12, 'ventana_ruptura': 24},  # ~50h S/R, rebote 3h
        60:  {'period_sr': 100, 'ventana_rebote': 8,  'ventana_ruptura': 16},  # ~100h S/R, rebote 8h
        240: {'period_sr': 60,  'ventana_rebote': 6,  'ventana_ruptura': 12},  # ~240h S/R, rebote 24h
    }
    cfg = _panorama_map.get(tf_minutos, _panorama_map[60])
    # Usar siempre el panorama calibrado por TF, ignorar el default heredado
    period_sr       = cfg['period_sr']
    ventana_rebote  = cfg['ventana_rebote']
    ventana_ruptura = cfg['ventana_ruptura']
    detalle['ventana_rebote_velas'] = ventana_rebote

    # ── 1. Calcular pivotes S/R ──────────────────────────────────────────────
    sr = detectar_resistencias_soportes(candles, period=period_sr)
    pivot_highs = list(sr.get('pivot_highs', []))
    pivot_lows  = list(sr.get('pivot_lows', []))

    # Inyectar nivel externo (BOTAI) si está dentro del ±5% del entry
    if level_price_externo > 0 and entry > 0:
        dist_ext = abs(entry - level_price_externo) / entry
        if dist_ext <= 0.05:
            if level_price_externo >= entry:
                pivot_highs.append(level_price_externo)
            else:
                pivot_lows.append(level_price_externo)
            resumen.append(
                f"  📌 Nivel externo BOTAI {level_price_externo:.6g}"
                f" (dist {dist_ext*100:.2f}%) incluido en análisis S/R"
            )

    if not pivot_highs and not pivot_lows:
        resumen.append("  ➖ S/R: sin pivotes detectados → +0")
        return 0, detalle

    # ── 2. Encontrar el nivel más cercano al entry ───────────────────────────
    todos_niveles = []
    for ph in pivot_highs:
        todos_niveles.append(('RESISTENCIA', ph))
    for pl in pivot_lows:
        todos_niveles.append(('SOPORTE', pl))

    todos_niveles.sort(key=lambda x: abs(entry - x[1]) / entry if entry > 0 else 1.0)

    tipo_nivel, nivel_cercano = todos_niveles[0]
    dist_pct = abs(entry - nivel_cercano) / entry if entry > 0 else 1.0

    detalle['nivel_cercano'] = nivel_cercano
    detalle['tipo_nivel'] = tipo_nivel
    detalle['dist_pct'] = round(dist_pct * 100, 3)

    # ── 3. ¿El nivel tiene sentido para la dirección? ───────────────────────
    nivel_correcto = (
        (side == 'LONG'  and tipo_nivel == 'SOPORTE')
        or (side == 'SHORT' and tipo_nivel == 'RESISTENCIA')
    )
    nivel_en_contra = (
        (side == 'LONG'  and tipo_nivel == 'RESISTENCIA' and dist_pct <= umbral_zona)
        or (side == 'SHORT' and tipo_nivel == 'SOPORTE'     and dist_pct <= umbral_zona)
    )

    if nivel_en_contra:
        resumen.append(
            f"  ⚠️  S/R en contra: entry cerca de {tipo_nivel} ({nivel_cercano:.6g},"
            f" dist {dist_pct*100:.2f}%) — posible techo/piso inmediato → -1"
        )
        detalle['pts_sr'] = -1
        return -1, detalle

    if not nivel_correcto:
        for t, n in todos_niveles[1:]:
            d = abs(entry - n) / entry if entry > 0 else 1.0
            if (side == 'LONG' and t == 'SOPORTE') or (side == 'SHORT' and t == 'RESISTENCIA'):
                if d <= umbral_cercano:
                    tipo_nivel    = t
                    nivel_cercano = n
                    dist_pct      = d
                    detalle['nivel_cercano'] = nivel_cercano
                    detalle['tipo_nivel']    = tipo_nivel
                    detalle['dist_pct']      = round(dist_pct * 100, 3)
                    nivel_correcto = True
                break

    if not nivel_correcto or dist_pct > umbral_cercano:
        resumen.append(
            f"  ➖ S/R: sin nivel relevante cerca del entry ({entry:.6g}) → +0"
        )
        return 0, detalle

    # ── 4. Detectar rebote en ventana temporal equivalente a 2 velas 4H ─────
    candles_ventana = candles[-ventana_rebote:]
    rebote, patron = _detectar_rebote_sr(candles_ventana, side, nivel_cercano)
    detalle['rebote_confirmado'] = rebote
    detalle['patron_rebote']     = patron

    # ── 5. Detectar ruptura a favor (retesteo) ───────────────────────────────
    ruptura_a_favor = _detectar_ruptura_a_favor(
        candles, side, nivel_cercano, entry, ventana=ventana_ruptura
    )
    detalle['ruptura_a_favor'] = ruptura_a_favor

    # ── 6. Calcular puntos ───────────────────────────────────────────────────
    puntos = 0

    if dist_pct <= umbral_zona:
        if rebote:
            puntos = 5
            icono = "🔥"
            desc  = f"S/R EXACTO + rebote ({patron})"
        else:
            puntos = 2
            icono = "✅"
            desc  = f"S/R EXACTO sin rebote confirmado"
    else:
        if rebote:
            puntos = 3
            icono = "✅"
            desc  = f"S/R CERCANO + rebote ({patron})"
        else:
            puntos = 1
            icono = "➖"
            desc  = f"S/R CERCANO sin rebote"

    if ruptura_a_favor and puntos < 5:
        puntos = min(puntos + 2, 5)
        desc  += " + ruptura/retesteo a favor"

    puntos = max(-1, min(puntos, 5))
    detalle['pts_sr'] = puntos

    resumen.append(
        f"  {icono} {desc} | nivel={nivel_cercano:.6g}"
        f" dist={dist_pct*100:.2f}%"
        f" [ventana {ventana_rebote}v×{tf_minutos}min]"
        f" → +{puntos}"
    )

    return puntos, detalle


def _detectar_rebote_sr(
    candles_recientes: list,
    side: str,
    nivel: float,
    umbral_toque: float = 0.012,
) -> tuple[bool, str]:
    """
    Detecta si el precio tocó un nivel S/R y rebotó dentro de la ventana dada.

    LÓGICA v2.2 — dos condiciones obligatorias:
    ──────────────────────────────────────────────────────────────────
    1. TOQUE: al menos una vela de la ventana debe haber alcanzado el nivel
       (low ≤ nivel×(1+umbral_toque) para LONG,
        high ≥ nivel×(1-umbral_toque) para SHORT).
       Sin toque real, no hay rebote — solo precio cerca del nivel.

    2. PATRÓN DE RECHAZO en la misma vela del toque o en la siguiente:
       - Mecha ≥ 1.5× cuerpo en la dirección del rechazo  (martillo/estrella)
       - Engulfing (vela cubre rango completo de la anterior)
       - Cuerpo sólido ≥ 60% del rango en la dirección correcta
       - Secuencia: 2 cierres consecutivos alejándose del nivel

    La ventana se recibe ya cortada (candles[-ventana_rebote:]) desde calc_sr_score,
    por lo tanto no tiene que preocuparse del tamaño — solo analizar lo que recibe.

    Parámetros:
        candles_recientes: slice de velas a analizar (ya recortado por el caller)
        side:              'LONG' o 'SHORT'
        nivel:             precio del nivel S/R
        umbral_toque:      % de tolerancia para considerar que el precio tocó el nivel
                           (default 1.2% — cubre wicks que no llegan exacto)

    Retorna: (rebote_detectado, patron_nombre)
    """
    if not candles_recientes or len(candles_recientes) < 2:
        return False, ""

    # ── PASO 1: Identificar qué velas tocaron el nivel ───────────────────────
    indices_toque = []
    for i, c in enumerate(candles_recientes):
        if side == 'LONG':
            # El low de la vela se acercó al soporte (por debajo o dentro del umbral)
            toco = c['low'] <= nivel * (1 + umbral_toque)
        else:
            # El high de la vela se acercó a la resistencia
            toco = c['high'] >= nivel * (1 - umbral_toque)

        if toco:
            indices_toque.append(i)

    if not indices_toque:
        # Ninguna vela de la ventana tocó el nivel — no hay rebote posible
        return False, ""

    # ── PASO 2: Buscar patrón de rechazo desde el toque hacia adelante ───────
    # Revisamos la vela del toque y la siguiente (si existe)
    n = len(candles_recientes)

    for idx_t in indices_toque:
        for idx in range(idx_t, min(idx_t + 2, n)):
            c = candles_recientes[idx]
            o, h, l, cl = c['open'], c['high'], c['low'], c['close']
            body       = abs(cl - o)
            full_range = h - l if h != l else 1e-10
            body_ratio = body / full_range

            if body_ratio < 0.05:
                continue  # doji puro — ignorar

            if side == 'LONG':
                mecha_inferior = min(o, cl) - l
                alejo = cl > nivel * 0.995   # cerró por encima del nivel

                if not alejo:
                    continue

                if mecha_inferior >= body * 1.5 and cl > o:
                    return True, "Martillo / rechazo inferior"
                if body_ratio >= 0.60 and cl > o:
                    return True, "Vela alcista sólida desde soporte"
                if idx > 0:
                    prev = candles_recientes[idx - 1]
                    if cl > prev['high'] and o < prev['low'] and cl > o:
                        return True, "Bullish engulfing"

            else:  # SHORT
                mecha_superior = h - max(o, cl)
                alejo = cl < nivel * 1.005   # cerró por debajo del nivel

                if not alejo:
                    continue

                if mecha_superior >= body * 1.5 and cl < o:
                    return True, "Estrella fugaz / rechazo superior"
                if body_ratio >= 0.60 and cl < o:
                    return True, "Vela bajista sólida desde resistencia"
                if idx > 0:
                    prev = candles_recientes[idx - 1]
                    if cl < prev['low'] and o > prev['high'] and cl < o:
                        return True, "Bearish engulfing"

    # ── PASO 3: Patrón secuencial — 2 cierres consecutivos desde el nivel ────
    # Buscar en toda la ventana posterior al primer toque
    inicio = indices_toque[0]
    if inicio + 1 < n:
        for i in range(inicio, n - 1):
            c1, c2 = candles_recientes[i], candles_recientes[i + 1]
            if side == 'LONG' and c1['close'] > nivel and c2['close'] > c1['close']:
                return True, "Secuencia alcista 2-velas desde soporte"
            if side == 'SHORT' and c1['close'] < nivel and c2['close'] < c1['close']:
                return True, "Secuencia bajista 2-velas desde resistencia"

    return False, ""


def _detectar_ruptura_a_favor(
    candles: list,
    side: str,
    nivel: float,
    entry: float,
    ventana: int = 8,
) -> bool:
    """
    Detecta si hubo una ruptura reciente del nivel S/R en la dirección de la señal
    y el precio está ahora retestando el nivel (entry cerca del nivel pero del otro lado).

    LONG: el precio estaba bajo el nivel (resistencia), lo rompió hacia arriba,
          y ahora vuelve a testearlo como soporte.
    SHORT: el precio estaba sobre el nivel (soporte), lo rompió hacia abajo,
           y ahora vuelve a testearlo como resistencia.
    """
    if not candles or len(candles) < ventana + 2 or nivel <= 0:
        return False

    historico = candles[-(ventana + 2):-2]
    if not historico:
        return False

    if side == 'LONG':
        # ¿Hubo velas por debajo del nivel en el histórico reciente?
        estuvo_abajo = any(c['close'] < nivel for c in historico)
        # ¿El entry está por encima del nivel (ruptura confirmada)?
        entry_sobre_nivel = entry > nivel * 0.997
        return estuvo_abajo and entry_sobre_nivel

    else:  # SHORT
        # ¿Hubo velas por encima del nivel?
        estuvo_arriba = any(c['close'] > nivel for c in historico)
        # ¿El entry está por debajo del nivel?
        entry_bajo_nivel = entry < nivel * 1.003
        return estuvo_arriba and entry_bajo_nivel