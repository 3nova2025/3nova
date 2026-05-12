"""
volumen_btc.py
Análisis de volumen para confirmación de señales.

CAMBIOS v2.2 (2026-04):
  - ELIMINADO: _calcular_correlacion_btc, _detectar_ruptura_correlacion,
    _obtener_estado_btc, _obtener_peso_correlacion, _analisis_btc_correlacion,
    _analisis_btc_posicion_abierta.
    La correlación estadística fue reemplazada por doble confirmación EMA50 4H
    de BTCUSDT en FASE2 — acción del precio real, más confiable.

  - CONSERVADO: _analizar_volumen y detectar_volumen_climatico.
    Son los únicos módulos usados por FASE3 (scoring de volumen).

_analizar_volumen (valores actuales):
  * Volumen ≥1.8x promedio en dirección: +3 pts
  * Volumen ≥1.4x promedio en dirección: +2 pts
  * Volumen ≥1.0x promedio en dirección: +1 pt
  * Volumen fuerte en contra (≥1.3x):    -1 pt
  * Bonus tendencia creciente (3+/4 velas en dirección): +1 pt adicional
  * Penalización tendencia decreciente:  -1 pt adicional
  Rango total: -2 a +4
"""


# ============================================================================
# ANÁLISIS DE VOLUMEN
# ============================================================================

def _analizar_volumen(candles: list, side: str, resumen: list, label: str) -> int:
    """
    Analiza el volumen para confirmar la fuerza del movimiento.
    Retorna: puntos (-2 a +4)
      Base: -1 a +3 según ratio vol_actual/vol_promedio y dirección
      Bonus/penalización: ±1 por tendencia de volumen en las últimas 4 velas

    CALIBRACIÓN 2026-04:
      - Promedio calculado sobre las últimas 30 velas (antes 20).
        Con 20 velas en mercados laterales el promedio era bajo y cualquier
        vela decente daba 1.5x fácilmente. 30 velas da un promedio más robusto.
      - Umbrales subidos: ≥1.8x (+3 pts), ≥1.4x (+2 pts), ≥1.0x (+1 pt).
        Antes era 1.5x/1.2x/0.9x — demasiado fácil de alcanzar.
      - Rango sigue siendo -2 a +4.
    """
    if not candles or len(candles) < 25:
        resumen.append(f"  ➖ {label} Sin datos de volumen suficientes → 0 pts")
        return 0

    vols = [c['volume'] for c in candles[:-1] if c['volume'] > 0]
    if not vols:
        resumen.append(f"  ➖ {label} Sin volumen disponible → 0 pts")
        return 0

    # CALIBRACIÓN: promedio sobre 30 velas para baseline más robusto
    vol_prom   = sum(vols[-30:]) / min(30, len(vols))
    vol_actual = candles[-1]['volume']
    vol_rel    = vol_actual / vol_prom if vol_prom > 0 else 1.0
    vol_trend  = sum(1 for i in range(-5, -1) if candles[i]['volume'] > candles[i-1]['volume'])

    vela_actual = candles[-1]
    es_alcista  = vela_actual['close'] > vela_actual['open']
    vol_en_dir  = (side == 'LONG' and es_alcista) or (side == 'SHORT' and not es_alcista)

    resumen.append(f"     {label} Vol: {vol_rel:.2f}x promedio(30) | Tendencia: {vol_trend}/4 velas ↑ | Dirección: {'✅' if vol_en_dir else '❌'}")

    puntos = 0
    if vol_en_dir:
        # CALIBRACIÓN: umbrales subidos 1.8x/1.4x/1.0x (antes 1.5x/1.2x/0.9x)
        if vol_rel >= 1.8:
            puntos += 3
            resumen.append(f"  ✅ {label} Volumen MUY fuerte en dirección ({vol_rel:.2f}x) → +3 pts")
        elif vol_rel >= 1.4:
            puntos += 2
            resumen.append(f"  ✅ {label} Volumen fuerte en dirección ({vol_rel:.2f}x) → +2 pts")
        elif vol_rel >= 1.0:
            puntos += 1
            resumen.append(f"  ✅ {label} Volumen normal en dirección ({vol_rel:.2f}x) → +1 pt")
        else:
            resumen.append(f"  ➖ {label} Volumen bajo pero en dirección ({vol_rel:.2f}x) → 0 pts")
    else:
        if vol_rel >= 1.3:
            puntos -= 1
            resumen.append(f"  ⚠️ {label} Volumen fuerte en CONTRA ({vol_rel:.2f}x) → -1 pt")
        elif vol_rel >= 1.0:
            resumen.append(f"  ➖ {label} Volumen normal en CONTRA ({vol_rel:.2f}x) → 0 pts")
        else:
            resumen.append(f"  ➖ {label} Volumen bajo en contra ({vol_rel:.2f}x) → 0 pts")

    if vol_trend >= 3 and vol_en_dir:
        puntos += 1
        resumen.append(f"  ✅ {label} Volumen en aumento ({vol_trend}/4 velas) → +1 pt")
    elif vol_trend <= 1 and not vol_en_dir:
        puntos -= 1
        resumen.append(f"  ⚠️ {label} Volumen decreciente en contra ({vol_trend}/4 velas) → -1 pt")

    if vol_rel > 3.0:
        resumen.append(f"  ⚠️ {label} SPIKE de volumen detectado ({vol_rel:.2f}x) — posible manipulación")

    return puntos


# ============================================================================
# VOLUMEN CLIMÁTICO (CLÍMAX)
# ============================================================================

def detectar_volumen_climatico(candles: list, resumen: list) -> bool:
    """
    Detecta si el volumen actual es un Clímax (indica agotamiento del flujo).
    Volumen > 3.5x promedio → probable fin de movimiento institucional.
    """
    if not candles or len(candles) < 20:
        return False

    vols = [c['volume'] for c in candles[:-1] if c['volume'] > 0]
    if not vols:
        return False

    vol_prom   = sum(vols[-20:]) / min(20, len(vols))
    vol_actual = candles[-1]['volume']

    if vol_actual > vol_prom * 3.5:
        resumen.append(f"  ⚠️ CLÍMAX DE VOLUMEN: {vol_actual/vol_prom:.1f}x promedio — ¡Posible agotamiento del flujo!")
        return True
    return False