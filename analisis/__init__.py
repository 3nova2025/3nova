"""
analisis/__init__.py

Paquete mínimo: solo indicadores + volumen BTC.
NO exporta nada del sistema viejo (decision_engine / estrategias / filtros / validación).
"""

from .indicadores import (
    RSI_NEUTRAL,
    SCORE_APROBACION,
    calc_ema,
    calc_rsi,
    calc_adx,
    calc_atr,
    calc_bollinger,
    detectar_resistencias_soportes,
    get_klines,
)

from .volumen_btc import (
    _analizar_volumen,
)