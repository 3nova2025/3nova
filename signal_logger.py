"""
signal_logger.py
================
Registra TODAS las señales analizadas en archivos separados:

  📁 signals_aprobadas.txt           — señales aprobadas por modo ESTRICTO
  📁 signals_rechazadas.txt          — rechazadas totales (ambos modos fallaron)
  📁 signals_aprobadas_flexible.txt  — señales aprobadas por modo FLEXIBLE
  📁 signals_rechazadas_flexible.txt — rechazadas por flexible (estricto ya rechazó)

Formato DETALLADO: cada filtro explicado con su puntaje individual
para calibrar y comparar el rendimiento de ambos modos.

CAMBIOS v2.5 (2026-05):
  - log_rechazada_flexible ahora también escribe en signals_rechazadas.txt
    para que todas las señales rechazadas (estrictas + flexibles) estén
    en un solo archivo general.
"""
import os
import time
from pathlib import Path

BOT_DIR = Path(r'C:\Users\DMG TECNOLOGIA\Videos\bitunix\bitunix2')
BOT_DIR.mkdir(parents=True, exist_ok=True)

ARCHIVO_APROBADAS           = BOT_DIR / 'signals_aprobadas.txt'
ARCHIVO_RECHAZADAS          = BOT_DIR / 'signals_rechazadas.txt'
ARCHIVO_APROBADAS_FLEXIBLE  = BOT_DIR / 'signals_aprobadas_flexible.txt'
ARCHIVO_RECHAZADAS_FLEXIBLE = BOT_DIR / 'signals_rechazadas_flexible.txt'

SEP      = '═' * 70
SEP_THIN = '─' * 70
SEP_MID  = '┄' * 70


def _ts() -> str:
    return time.strftime('%Y-%m-%d %H:%M:%S')


def _escribir(path: str, texto: str):
    try:
        with open(path, 'a', encoding='utf-8') as f:
            f.write(texto + '\n')
    except Exception as e:
        print(f"  ⚠️  [SIGNAL_LOGGER] Error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers de formato
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_rsi(rsi: float, side: str) -> str:
    """Explica en palabras lo que significa el RSI para este lado."""
    if side == "LONG":
        if rsi <= 30:
            return f"{rsi:.1f} ← SOBREVENTA fuerte → ideal para LONG (+5 pts)"
        elif rsi <= 40:
            return f"{rsi:.1f} ← zona baja → bueno para LONG (+4 pts)"
        elif rsi <= 50:
            return f"{rsi:.1f} ← neutro-bajo → aceptable para LONG (+3 pts)"
        elif rsi <= 60:
            return f"{rsi:.1f} ← neutro → riesgo medio para LONG (+2 pts)"
        elif rsi <= 70:
            return f"{rsi:.1f} ← elevado → comprar en zona cara (+1 pt)"
        else:
            return f"{rsi:.1f} ← SOBRECOMPRA → peligroso para LONG (0 pts)"
    else:  # SHORT
        if rsi >= 70:
            return f"{rsi:.1f} ← SOBRECOMPRA fuerte → ideal para SHORT (+5 pts)"
        elif rsi >= 60:
            return f"{rsi:.1f} ← zona alta → bueno para SHORT (+4 pts)"
        elif rsi >= 50:
            return f"{rsi:.1f} ← neutro-alto → aceptable para SHORT (+3 pts)"
        elif rsi >= 40:
            return f"{rsi:.1f} ← neutro → riesgo medio para SHORT (+2 pts)"
        elif rsi >= 30:
            return f"{rsi:.1f} ← bajo → vender en zona barata (+1 pt)"
        else:
            return f"{rsi:.1f} ← SOBREVENTA → peligroso para SHORT (0 pts)"


def _fmt_rsi_4h(rsi_4h: float, side: str) -> str:
    """Explica el RSI 4H como confirmación macro."""
    if side == "LONG":
        if rsi_4h <= 40:
            return f"{rsi_4h:.1f} ← 4H confirma fuerte alcista (+2 pts)"
        elif rsi_4h <= 50:
            return f"{rsi_4h:.1f} ← 4H confirma alcista (+1 pt)"
        else:
            return f"{rsi_4h:.1f} ← 4H diverge del LONG (0 pts) ⚠️"
    else:
        if rsi_4h >= 60:
            return f"{rsi_4h:.1f} ← 4H confirma fuerte bajista (+2 pts)"
        elif rsi_4h >= 50:
            return f"{rsi_4h:.1f} ← 4H confirma bajista (+1 pt)"
        else:
            return f"{rsi_4h:.1f} ← 4H diverge del SHORT (0 pts) ⚠️"


def _fmt_smf(direction: str, money_flow: float, strength_pct: float, pts: int, side: str) -> str:
    """Explica el Smart Money Flow 1H."""
    dir_emoji = "📈" if direction == "BULL" else ("📉" if direction == "BEAR" else "➡️")
    alineado = (
        (side == "LONG" and direction == "BULL") or
        (side == "SHORT" and direction == "BEAR")
    )
    estado = "✅ alineado con la señal" if alineado else "❌ contra la señal"
    return (
        f"{dir_emoji} Dirección: {direction} | Flujo: {money_flow:+.3f} | "
        f"Fuerza: {strength_pct:.1f}% | {estado} → {pts:+d} pts"
    )


def _fmt_smf_4h(money_flow: float, strength_pct: float, pts: int, side: str) -> str:
    """Explica el Smart Money Flow 4H."""
    direction = "BULL" if money_flow > 0 else ("BEAR" if money_flow < 0 else "NEUTRAL")
    dir_emoji = "📈" if direction == "BULL" else ("📉" if direction == "BEAR" else "➡️")
    alineado = (
        (side == "LONG" and money_flow > 0.10) or
        (side == "SHORT" and money_flow < -0.10)
    )
    estado = "✅ alineado con la señal" if alineado else "❌ contra la señal"
    return (
        f"{dir_emoji} Dirección: {direction} | Flujo: {money_flow:+.3f} | "
        f"Fuerza: {strength_pct:.1f}% | {estado} → {pts:+d} pts"
    )


def _fmt_volumen(pts_vol: int) -> str:
    """Explica el puntaje de volumen."""
    if pts_vol >= 3:
        return f"+{pts_vol} ← Volumen FUERTE confirma el movimiento 🔥"
    elif pts_vol >= 1:
        return f"+{pts_vol} ← Volumen moderado, hay interés"
    elif pts_vol == 0:
        return "0 ← Volumen neutral, sin confirmación"
    elif pts_vol == -1:
        return f"{pts_vol} ← Volumen débil, señal poco respaldada ⚠️"
    else:
        return f"{pts_vol} ← Volumen muy bajo, desconfianza alta ❌"


def _fmt_sr(soporte: float, resistencia: float, entry: float, side: str) -> str:
    """Explica la proximidad al soporte/resistencia."""
    if side == "LONG" and soporte > 0:
        dist_pct = abs(entry - soporte) / entry * 100
        if dist_pct <= 0.5:
            nivel = f"≤0.5% del soporte → precio exactamente en zona de rebote (+5 pts) 🎯"
        elif dist_pct <= 1.5:
            nivel = f"≤1.5% del soporte → muy cerca de zona de rebote (+3 pts) ✅"
        elif dist_pct <= 3.0:
            nivel = f"≤3.0% del soporte → cerca del soporte (+1 pt) ➖"
        else:
            nivel = f"{dist_pct:.1f}% del soporte → lejos, sin apoyo estructural (0 pts) ❌"
        return f"Soporte: {soporte:.6g} | Entry: {entry:.6g} | Distancia: {dist_pct:.2f}% | {nivel}"
    elif side == "SHORT" and resistencia > 0:
        dist_pct = abs(entry - resistencia) / entry * 100
        if dist_pct <= 0.5:
            nivel = f"≤0.5% de resistencia → precio exactamente en techo (+5 pts) 🎯"
        elif dist_pct <= 1.5:
            nivel = f"≤1.5% de resistencia → muy cerca del techo (+3 pts) ✅"
        elif dist_pct <= 3.0:
            nivel = f"≤3.0% de resistencia → cerca del techo (+1 pt) ➖"
        else:
            nivel = f"{dist_pct:.1f}% de resistencia → lejos, sin resistencia clara (0 pts) ❌"
        return f"Resistencia: {resistencia:.6g} | Entry: {entry:.6g} | Distancia: {dist_pct:.2f}% | {nivel}"
    return "Niveles S/R no disponibles"


def _fmt_rr(rr: float, rr_min: float = 0.8) -> str:
    if rr == 0.0:
        return "N/A (señal rechazada antes del cálculo de RR)"
    """Explica si el RR es suficiente."""
    if rr >= 3.0:
        return f"1:{rr:.2f} ← Excepcional, riesgo muy bien compensado 🔥"
    elif rr >= 2.0:
        return f"1:{rr:.2f} ← Bueno, ganancia al doble del riesgo ✅"
    elif rr >= rr_min:
        return f"1:{rr:.2f} ← Aceptable, supera el mínimo de 1:{rr_min} ✅"
    else:
        return f"1:{rr:.2f} ← INSUFICIENTE, mínimo requerido: 1:{rr_min} ❌ → RECHAZO AUTOMÁTICO"


def _fmt_bonus(bonus_ctx: int) -> str:
    """Explica el bonus/penalización de contexto técnico (Fase 2)."""
    lines = []
    if bonus_ctx > 0:
        lines.append(f"  Contexto SMA36/BTC EMA50 4H → +{bonus_ctx} (BTC alineado con señal)")
    elif bonus_ctx < 0:
        lines.append(f"  Contexto SMA36/BTC EMA50 4H → {bonus_ctx} (BTC contra la señal)")
    else:
        lines.append(f"  Contexto SMA36/BTC EMA50 4H → 0 (BTC lateral/neutral)")
    lines.append(f"  BONUS TOTAL         → {bonus_ctx:+d} pts (límite: -3 a +4)")
    return "\n".join(lines)


def _construir_desglose(analisis: dict, sig: dict) -> str:
    """
    Construye el bloque de desglose de filtros con explicaciones detalladas.
    Funciona con el nuevo formato (Filtro Maestro Fase 3).
    """
    det      = analisis.get('detalle_puntajes', {})
    resumen  = analisis.get('resumen', [])
    side     = sig.get('side', '?')
    entry    = float(sig.get('entry', 0))
    stop     = float(sig.get('stop', 0))
    tps      = sig.get('tps_list', [])
    qs       = int(sig.get('quality_score', 0))
    score    = analisis.get('puntaje', 0)

    # --- EXTRACCIÓN DE PUNTOS (MOVIDO AQUÍ PARA EVITAR ERRORES) ---
    rsi_pts  = int(det.get('pts_rsi_1h', 0))
    sr_pts   = int(det.get('pts_sr_rebote', det.get('pts_sr', 0)))
    zona_pts = int(det.get('pts_zona_valor_1h', 0))
    # --------------------------------------------------------------

    tp1      = float(tps[0]) if tps else 0
    tp3      = float(tps[-1]) if tps else 0
    rr_tp1   = float(det.get('rr_tp1', 0))
    rr_tp3   = float(det.get('rr_tp3', 0))

    rsi_1h   = float(det.get('rsi', 50))
    rsi_4h   = float(det.get('rsi_4h', 50))
    soporte  = float(det.get('soporte', 0))
    resist   = float(det.get('resistencia', 0))
    pts_vol  = int(det.get('pts_volumen_1h', 0))
    pts_smf  = int(det.get('pts_smf_1h', 0))
    pts_4h   = int(det.get('pts_confirmacion_4h', 0))
    pts_conf = int(det.get('confluencia', 0))
    bonus    = int(det.get('bonus_externo', 0))
    pts_bb   = int(det.get('pts_bb_posicion', 0))
    bb_pct_b = float(det.get('bb_pct_b', 0.5))
    bb_upper = float(det.get('bb_upper', 0))
    bb_lower = float(det.get('bb_lower', 0))
    bb_1d_pct_b = det.get('bb_1d_pct_b', None)
    bb_1d_cap   = bool(det.get('bb_1d_cap', False))

    smf_dir  = det.get('smf_1h_direction', 'NEUTRAL')
    smf_mf   = float(det.get('smf_1h_money_flow', 0))
    smf_str  = float(det.get('smf_1h_strength_pct', 0))
    smf4_mf  = float(det.get('smf_4h_money_flow', 0))
    smf4_str = float(det.get('smf_4h_strength_pct', 0))
    pts_smf4 = pts_4h  # pts_4h incluye RSI4H + SMF4H

    # bonus_externo = solo contexto técnico (Fase 2): SMA36 + doble confirmación BTC EMA50 4H + ADX
    bonus_ctx = bonus

    # Detectar si el rechazo fue en Fase 2 (antes del scoring)
    # En ese caso rr_tp1==0 y detalle_puntajes está vacío o sin confluencia
    razon_rechazo = analisis.get('razon_rechazo', '') or ''
    motivo_raw    = analisis.get('mensaje', '') or ''
    # rechazo_fase2: True cuando Fase 2 o Fase 4 rechazaron ANTES de ejecutar scoring.
    # Condición: detalle_puntajes vacío (sin confluencia) + mensaje coincide con
    # cualquiera de los motivos conocidos de rechazo pre-scoring.
    _motivo_lower  = motivo_raw.lower()
    _razon_lower   = razon_rechazo.lower()
    _keywords_f2 = (
        'contratendencia', 'lateral', 'adx', 'ema50',
        'datos insuficientes', '4h en short', '4h en long',
        'smf 4h opuesto', 'smf 4h', 'alineación',
        'contexto', 'corruptos', 'entry igual',
        'dirección inválida', 'entry/stop',
    )
    rechazo_fase2 = (
        rr_tp1 == 0.0 and
        det.get('confluencia', None) is None and
        any(kw in _motivo_lower or kw in _razon_lower for kw in _keywords_f2)
    )

    lines = []

    # ── 1. RATIO RIESGO/BENEFICIO ─────────────────────────────────────────
    lines.append(f"\n  {'─'*66}")
    lines.append(f"  🎯 FILTRO 1 — RATIO RIESGO/BENEFICIO (RR)")
    lines.append(f"  {'─'*66}")
    lines.append(f"  {'Qué es':20s}: Mide cuánto ganas vs cuánto arriesgas. Mínimo requerido: 1:0.8")
    lines.append(f"  {'Entry':20s}: {entry:.6g}")
    lines.append(f"  {'Stop Loss':20s}: {stop:.6g}  (riesgo unitario: {abs(entry-stop):.6g})")
    lines.append(f"  {'TP1':20s}: {tp1:.6g}  → RR con TP1: {_fmt_rr(rr_tp1)}")
    lines.append(f"  {'TP3':20s}: {tp3:.6g}  → RR con TP3: {_fmt_rr(rr_tp3, rr_min=0)}")
    if rechazo_fase2:
        lines.append(f"  {'Resultado':20s}: ⏭️  NO EVALUADO — señal rechazada en Fase 2 antes del RR")
    elif rr_tp1 >= 0.8:
        lines.append(f"  {'Resultado':20s}: ✅ PASA — se continúa el análisis")
    else:
        lines.append(f"  {'Resultado':20s}: ❌ FALLA — señal RECHAZADA aquí, no se puntúa nada más")

    # Si fue rechazada en Fase 2, mostrar filtros 2-8 como NO EVALUADOS
    if rechazo_fase2:
        NO_EVAL = "⏭️  NO EVALUADO — señal rechazada en Fase 2 (contexto de mercado)"
        for filtro in [
            ("📊 FILTRO 2 — RSI 1H", "RSI 1H"),
            ("🏛️  FILTRO 3 — PROXIMIDAD A SOPORTE / RESISTENCIA", "S/R"),
            ("🔄 FILTRO 4 — REBOTE CONFIRMADO EN EL NIVEL", "Rebote"),
            ("📦 FILTRO 5 — VOLUMEN 1H", "Volumen"),
            ("🏦 FILTRO 6 — SMART MONEY FLOW 1H", "SMF"),
            ("📅 FILTRO 7 — CONFIRMACIÓN MULTIFRAME 4H", "4H"),
            ("📉 FILTRO 7B — BANDAS DE BOLLINGER (Posición del precio)", "BB"),
            ("⭐ FILTRO 8 — BONUS / PENALIZACIÓN DE CONTEXTO", "Bonus"),
        ]:
            lines.append(f"\n  {'─'*66}")
            lines.append(f"  {filtro[0]}")
            lines.append(f"  {'─'*66}")
            lines.append(f"  {'Resultado':20s}: {NO_EVAL}")

        lines.append(f"\n  {'═'*66}")
        lines.append(f"  📋 RESUMEN DEL SCORING")
        lines.append(f"  {'═'*66}")
        lines.append(f"  {'Filtro':<35s} {'Puntos':>8s}")
        lines.append(f"  {'-'*43}")
        lines.append(f"  {'RR':35s} {'N/A':>8s}")
        lines.append(f"  {'RSI 1H':35s} {'N/A':>8s}")
        lines.append(f"  {'Zona Valor (EMA20/50)':35s} {'N/A':>8s}")
        lines.append(f"  {'Soporte/Resistencia 1H':35s} {'N/A':>8s}")
        lines.append(f"  {'Rebote confirmado':35s} {'N/A':>8s}")
        lines.append(f"  {'Volumen 1H':35s} {'N/A':>8s}")
        lines.append(f"  {'Smart Money Flow 1H':35s} {'N/A':>8s}")
        lines.append(f"  {'Confirmación 4H (RSI+SMF)':35s} {'N/A':>8s}")
        lines.append(f"  {'BB Posición':35s} {'N/A':>8s}")
        lines.append(f"  {'-'*43}")
        lines.append(f"  {'SCORE FINAL':35s} {'N/A':>8s}")
        lines.append(f"  {'═'*43}")
        lines.append(f"  ⚠️  Señal rechazada en Fase 2 — scoring no ejecutado")
        return "\n".join(lines)

    # ── 2. RSI 1H ────────────────────────────────────────────────────────
    lines.append(f"\n  {'─'*66}")
    lines.append(f"  📊 FILTRO 2 — RSI 1H (Índice de Fuerza Relativa — 14 períodos)")
    lines.append(f"  {'─'*66}")
    lines.append(f"  {'Qué es':20s}: Mide si el precio está sobrecomprado (>70) o sobrevendido (<30)")
    lines.append(f"  {'Escala':20s}: 0 a 5 pts   (max para LONG: RSI≤30, max para SHORT: RSI≥70)")
    lines.append(f"  {'RSI actual':20s}: {_fmt_rsi(rsi_1h, side)}")

    # ── 3. SOPORTE / RESISTENCIA ────────────────────────────────────────
    lines.append(f"\n  {'─'*66}")
    lines.append(f"  🏛️  FILTRO 3 — PROXIMIDAD A SOPORTE / RESISTENCIA")
    lines.append(f"  {'─'*66}")
    lines.append(f"  {'Qué es':20s}: Zonas de precio donde el mercado históricamente rebota")
    lines.append(f"  {'Escala':20s}: -1 a +5 pts  (exacto+rebote=+5, cercano+rebote=+3, ruptura=-1)")
    # Leer detalle del S/R directamente de detalle_puntajes
    nivel_sr = float(det.get('sr_nivel_cercano', 0))
    dist_sr  = float(det.get('sr_dist_pct', 0))
    rebote_sr = det.get('sr_rebote_confirmado', False)
    patron_sr = det.get('sr_patron_rebote', '')
    ruptura_sr = det.get('sr_ruptura_a_favor', False)
    if nivel_sr > 0:
        lines.append(f"  {'Nivel S/R':20s}: {nivel_sr:.6g} — dist {dist_sr:.2f}%")
        if rebote_sr:
            lines.append(f"  {'Rebote':20s}: ✅ Confirmado — {patron_sr}")
        elif ruptura_sr:
            lines.append(f"  {'Ruptura':20s}: ✅ A favor de la señal")
        else:
            lines.append(f"  {'Rebote':20s}: ➖ Sin confirmar")
        lines.append(f"  {'Puntaje':20s}: {sr_pts:+d} pts")
    else:
        lines.append(f"  {'Detalle':20s}: Niveles S/R no disponibles → {sr_pts:+d} pts")

    # ── 4. REBOTE CONFIRMADO ─────────────────────────────────────────────
    lines.append(f"\n  {'─'*66}")
    lines.append(f"  🔄 FILTRO 4 — REBOTE CONFIRMADO EN EL NIVEL")
    lines.append(f"  {'─'*66}")
    lines.append(f"  {'Qué es':20s}: Las últimas 2 velas cerradas confirman que el precio rebotó")
    lines.append(f"  {'Escala':20s}: 0 a 3 pts   (vela cerrando en dirección correcta = +3)")
    # Extraer del resumen las líneas de rebote
    rebote_lines = [l for l in resumen if 'Rebote' in l or 'rebote' in l]
    if rebote_lines:
        for rl in rebote_lines:
            lines.append(f"  {'Resultado':20s}: {rl.strip()}")
    else:
        lines.append(f"  {'Resultado':20s}: ➖ Sin rebote detectado en los últimos 2 cierres (0 pts)")

    # ── 5. VOLUMEN 1H ────────────────────────────────────────────────────
    lines.append(f"\n  {'─'*66}")
    lines.append(f"  📦 FILTRO 5 — VOLUMEN 1H")
    lines.append(f"  {'─'*66}")
    lines.append(f"  {'Qué es':20s}: Confirma si hay participación real del mercado en la vela")
    lines.append(f"  {'Escala':20s}: -2 a +4 pts  (volumen bajo penaliza, volumen alto suma)")
    lines.append(f"  {'Puntaje':20s}: {_fmt_volumen(pts_vol)}")
    # Extraer líneas de volumen del resumen
    vol_lines = [l for l in resumen if 'olumen' in l and '1H' in l]
    for vl in vol_lines:
        lines.append(f"  {'Detalle':20s}: {vl.strip()}")

    # ── 6. SMF 1H (Smart Money Flow) ─────────────────────────────────────
    lines.append(f"\n  {'─'*66}")
    lines.append(f"  🏦 FILTRO 6 — SMART MONEY FLOW 1H (Flujo de Dinero Institucional)")
    lines.append(f"  {'─'*66}")
    lines.append(f"  {'Qué es':20s}: Detecta si el dinero grande (institucional) fluye a favor")
    lines.append(f"  {'Cómo funciona':20s}: Banda adaptativa + momentum ponderado por volumen real")
    lines.append(f"  {'Escala':20s}: 0 a +4 pts   (flujo fuerte alineado = máximo)")
    lines.append(f"  {'Resultado':20s}: {_fmt_smf(smf_dir, smf_mf, smf_str, pts_smf, side)}")

    # ── 7. CONFIRMACIÓN MULTIFRAME 4H ─────────────────────────────────
    lines.append(f"\n  {'─'*66}")
    lines.append(f"  📅 FILTRO 7 — CONFIRMACIÓN MULTIFRAME 4H")
    lines.append(f"  {'─'*66}")
    lines.append(f"  {'Qué es':20s}: El timeframe mayor (4H) valida lo que dice el 1H")
    lines.append(f"  {'Escala':20s}: 0 a +4 pts   (RSI 4H + SMF 4H, cada uno 0-2 pts)")
    lines.append(f"  {'RSI 4H':20s}: {_fmt_rsi_4h(rsi_4h, side)}")
    lines.append(f"  {'SMF 4H':20s}: {_fmt_smf_4h(smf4_mf, smf4_str, 0, side)}")
    lines.append(f"  {'Subtotal 4H':20s}: {pts_4h} pts")
    if pts_4h >= 3:
        lines.append(f"  {'Resumen':20s}: ⚡ Sincronización FUERTE — ambos TF confirman la señal")
    elif pts_4h >= 1:
        lines.append(f"  {'Resumen':20s}: ✅ Sincronización parcial — al menos un TF confirma")
    else:
        lines.append(f"  {'Resumen':20s}: ⚠️  Sin confirmación en 4H — señal más débil")

    # ── BB POSITION (Bloque 7) ────────────────────────────────────────
    lines.append(f"\n  {'─'*66}")
    lines.append(f"  📉 FILTRO 7B — BANDAS DE BOLLINGER (Posición del precio)")
    lines.append(f"  {'─'*66}")
    lines.append(f"  {'Qué es':20s}: Mide dónde está el precio dentro de las BB del TF de entrada")
    lines.append(f"  {'Escala':20s}: -2 a +2 pts")
    lines.append(f"  {'%B actual':20s}: {bb_pct_b:.2f}  (0.0=BB lower, 0.5=media, 1.0=BB upper)")
    if bb_upper > 0 and bb_lower > 0:
        lines.append(f"  {'Bandas':20s}: lower={bb_lower:.6g}  upper={bb_upper:.6g}")
    if side == 'LONG':
        if bb_pct_b <= 0.20:
            zona_bb = "cerca BB lower — zona de valor para LONG 🔥"
        elif bb_pct_b <= 0.45:
            zona_bb = "zona media-baja — buena entrada LONG ✅"
        elif bb_pct_b <= 0.75:
            zona_bb = "zona media — sin ventaja clara ➖"
        elif bb_pct_b <= 0.90:
            zona_bb = "cerca BB upper — precio estirado, riesgo LONG ⚠️"
        else:
            zona_bb = "en/sobre BB upper — sobrecompra, alto riesgo LONG 🚫"
    else:
        if bb_pct_b >= 0.80:
            zona_bb = "cerca BB upper — zona de valor para SHORT 🔥"
        elif bb_pct_b >= 0.55:
            zona_bb = "zona media-alta — buena entrada SHORT ✅"
        elif bb_pct_b >= 0.25:
            zona_bb = "zona media — sin ventaja clara ➖"
        elif bb_pct_b >= 0.10:
            zona_bb = "cerca BB lower — precio estirado, riesgo SHORT ⚠️"
        else:
            zona_bb = "en/sobre BB lower — sobreventa, alto riesgo SHORT 🚫"
    lines.append(f"  {'Zona':20s}: {zona_bb}")
    if bb_1d_pct_b is not None:
        lines.append(f"  {'1D %B':20s}: {bb_1d_pct_b:.2f}  {'← anuló bonus (contexto diario adverso)' if bb_1d_cap else '← contexto diario neutral'}")
    else:
        lines.append(f"  {'1D %B':20s}: no disponible")
    lines.append(f"  {'Puntaje BB':20s}: {pts_bb:+d} pts")

    # ── 8. BONUS EXTERNO ──────────────────────────────────────────────
    lines.append(f"\n  {'─'*66}")
    lines.append(f"  ⭐ FILTRO 8 — BONUS / PENALIZACIÓN DE CONTEXTO")
    lines.append(f"  {'─'*66}")
    lines.append(f"  {'Qué es':20s}: Ajuste técnico por contexto de mercado (SMA36, doble confirmación BTC EMA50 4H, ADX)")
    lines.append(f"  {'Rango':20s}: -3 a +4 pts")
    lines.append(f"{_fmt_bonus(bonus_ctx)}")

    # ── RESUMEN DEL SCORING ──────────────────────────────────────────
    lines.append(f"\n  {'═'*66}")
    lines.append(f"  📋 RESUMEN DEL SCORING")
    lines.append(f"  {'═'*66}")

    # Tabla de puntos por filtro
    lines.append(f"  {'Filtro':<35s} {'Puntos':>8s}")
    lines.append(f"  {'-'*43}")

    # NOTA: rsi_pts, sr_pts y zona_pts ya están definidas al inicio de la función
    # rebote está dentro de pts_sr en calc_sr_score — extraer del resumen como fallback
    reb_pts = _extraer_pts_resumen(resumen, ['rebote', 'Rebote'])

    lines.append(f"  {'RR (filtro obligatorio)':35s} {'PASA' if rr_tp1>=0.8 else 'FALLA':>8s}")
    lines.append(f"  {'RSI 1H':35s} {rsi_pts:>+8d}")
    lines.append(f"  {'Zona Valor (EMA20/50)':35s} {zona_pts:>+8d}")
    lines.append(f"  {'Soporte/Resistencia 1H':35s} {sr_pts:>+8d}")
    lines.append(f"  {'Rebote confirmado':35s} {reb_pts:>+8d}")
    lines.append(f"  {'Volumen 1H':35s} {pts_vol:>+8d}")
    lines.append(f"  {'Smart Money Flow 1H':35s} {pts_smf:>+8d}")
    lines.append(f"  {'Confirmación 4H (RSI+SMF)':35s} {pts_4h:>+8d}")
    lines.append(f"  {'BB Posición (%B={:.2f})'.format(bb_pct_b):35s} {pts_bb:>+8d}")
    lines.append(f"  {'-'*43}")
    lines.append(f"  {'Subtotal confluencia':35s} {pts_conf:>+8d}")
    lines.append(f"  {'Bonus externo':35s} {bonus:>+8d}")
    lines.append(f"  {'═'*43}")
    lines.append(f"  {'SCORE FINAL':35s} {score:>8d}")

    return "\n".join(lines)


def _extraer_pts_resumen(resumen: list, keywords) -> int:
    """Extrae el valor de puntos de las líneas del resumen que contengan keywords."""
    if isinstance(keywords, str):
        keywords = [keywords]
    for line in resumen:
        if any(kw in line for kw in keywords):
            # Buscar patrón → +N o → N
            import re
            match = re.search(r'→\s*([+-]?\d+)', line)
            if match:
                return int(match.group(1))
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Funciones públicas
# ─────────────────────────────────────────────────────────────────────────────

def log_aprobada(sig: dict, analisis: dict):
    """Registra señal APROBADA con desglose completo de todos los filtros."""
    ts       = _ts()
    symbol   = sig.get('symbol', '?')
    side     = sig.get('side', '?')
    tf       = sig.get('timeframe', '?')
    entry    = float(sig.get('entry', 0))
    sl       = float(sig.get('stop', 0))
    tps      = sig.get('tps_list', [])
    tp1      = float(tps[0]) if tps else 0
    tp3      = float(tps[-1]) if tps else 0
    ia_prob  = float(sig.get('ia_probability', 0))
    qs       = int(sig.get('quality_score', 0))

    score    = analisis.get('puntaje', analisis.get('score_final', 0))
    calidad  = analisis.get('veredicto', analisis.get('calidad', '?'))
    det      = analisis.get('detalle_puntajes', {})
    bonus    = int(det.get('bonus_externo', 0))

    riesgo_pct   = abs(entry - sl)  / entry * 100 if entry > 0 else 0
    ganancia_tp1 = abs(tp1 - entry) / entry * 100 if entry > 0 else 0
    ganancia_tp3 = abs(tp3 - entry) / entry * 100 if entry > 0 else 0
    rr_tp1       = float(det.get('rr_tp1', 0))
    rr_tp3       = float(det.get('rr_tp3', 0))

    desglose = _construir_desglose(analisis, sig)

    texto = (
        f"\n{SEP}\n"
        f"✅  SEÑAL APROBADA — {ts}\n"
        f"{SEP}\n"
        f"\n"
        f"  📌 PAR          : {symbol}  {side}  ({tf})\n"
        f"  💰 ENTRY        : {entry:.8g}\n"
        f"  🛑 STOP LOSS    : {sl:.8g}   → riesgo {riesgo_pct:.2f}% por unidad\n"
        f"  🎯 TP1          : {tp1:.8g}   → potencial {ganancia_tp1:.2f}%\n"
        f"  🎯 TP3          : {tp3:.8g}   → potencial {ganancia_tp3:.2f}%\n"
        f"  📐 RR           : TP1=1:{rr_tp1:.2f}  /  TP3=1:{rr_tp3:.2f}\n"
        f"\n"
        f"  📊 Quality Score : {qs}  |  IA Prob: {ia_prob:.1%}\n"
        f"  🏆 SCORE FINAL  : {score} pts  →  {calidad}\n"
        f"\n"
        f"{SEP_THIN}"
        f"{desglose}\n"
        f"\n"
        f"{SEP_THIN}\n"
        f"  🔎 RESULTADO    : pendiente... (revisar si cerró en WIN o LOSS)\n"
        f"{SEP}"
    )
    _escribir(ARCHIVO_APROBADAS, texto)


def log_rechazada(sig: dict, analisis: dict):
    """Registra señal RECHAZADA con la razón exacta y desglose de filtros."""
    ts       = _ts()
    symbol   = sig.get('symbol', '?')
    side     = sig.get('side', '?')
    tf       = sig.get('timeframe', '?')
    entry    = float(sig.get('entry', 0))
    sl       = float(sig.get('stop', 0))
    tps      = sig.get('tps_list', [])
    tp1      = float(tps[0]) if tps else 0
    tp3      = float(tps[-1]) if tps else 0
    ia_prob  = float(sig.get('ia_probability', 0))
    qs       = int(sig.get('quality_score', 0))

    motivo   = analisis.get('razon_rechazo') or analisis.get('motivo_rechazo') or analisis.get('mensaje', 'desconocido')
    score    = analisis.get('puntaje', analisis.get('score_final', 0))
    det      = analisis.get('detalle_puntajes', {})

    riesgo_pct   = abs(entry - sl)  / entry * 100 if entry > 0 else 0
    ganancia_tp1 = abs(tp1 - entry) / entry * 100 if entry > 0 else 0
    ganancia_tp3 = abs(tp3 - entry) / entry * 100 if entry > 0 else 0
    rr_tp1       = float(det.get('rr_tp1', 0))
    rr_tp3       = float(det.get('rr_tp3', 0))

    # Razón en lenguaje natural
    razon_natural = _explicar_rechazo(motivo, score, analisis, sig)

    desglose = _construir_desglose(analisis, sig)

    texto = (
        f"\n{SEP}\n"
        f"❌  SEÑAL RECHAZADA — {ts}\n"
        f"{SEP}\n"
        f"\n"
        f"  📌 PAR          : {symbol}  {side}  ({tf})\n"
        f"  💰 ENTRY        : {entry:.8g}\n"
        f"  🛑 STOP LOSS    : {sl:.8g}   → riesgo {riesgo_pct:.2f}% por unidad\n"
        f"  🎯 TP1          : {tp1:.8g}   → potencial {ganancia_tp1:.2f}%\n"
        f"  🎯 TP3          : {tp3:.8g}   → potencial {ganancia_tp3:.2f}%\n"
        f"  📐 RR           : TP1=1:{rr_tp1:.2f}  /  TP3=1:{rr_tp3:.2f}\n"
        f"\n"
        f"  📊 Quality Score : {qs}  |  IA Prob: {ia_prob:.1%}\n"
        f"  🚫 SCORE FINAL  : {score} pts  →  RECHAZADA\n"
        f"\n"
        f"{SEP_THIN}\n"
        f"  💬 MOTIVO DE RECHAZO\n"
        f"{SEP_THIN}\n"
        f"{razon_natural}\n"
        f"\n"
        f"{SEP_THIN}"
        f"{desglose}\n"
        f"\n"
        f"{SEP_THIN}\n"
        f"  🔍 REVISIÓN     : ¿el precio llegó al TP o SL después? Revisar manualmente.\n"
        f"{SEP}"
    )
    _escribir(ARCHIVO_RECHAZADAS, texto)


def _explicar_rechazo(motivo: str, score: int, analisis: dict, sig: dict) -> str:
    """Genera una explicación detallada del motivo de rechazo."""
    from analisis.indicadores import SCORE_APROBACION

    det   = analisis.get('detalle_puntajes', {})
    side  = sig.get('side', '?')
    entry = float(sig.get('entry', 0))

    lines = []

    if 'RR' in motivo or 'rr' in motivo.lower():
        rr_tp1 = float(det.get('rr_tp1', 0))
        lines.append(f"  🚫 RAZÓN: RR insuficiente")
        lines.append(f"     El ratio Riesgo/Beneficio con TP1 es 1:{rr_tp1:.2f}")
        lines.append(f"     El mínimo requerido es 1:0.8")
        lines.append(f"     Esto significa que la ganancia potencial no justifica el riesgo.")
        lines.append(f"     → El trade no pasa ni al análisis técnico. Rechazo automático.")

    elif 'Score' in motivo or 'score' in motivo.lower() or 'insuficiente' in motivo.lower():
        try:
            min_score = SCORE_APROBACION
        except Exception:
            min_score = '?'
        lines.append(f"  🚫 RAZÓN: Score técnico insuficiente")
        lines.append(f"     Score obtenido: {score} pts")
        lines.append(f"     Score mínimo requerido: {min_score} pts")
        faltan = (min_score - score) if isinstance(min_score, int) else '?'
        lines.append(f"     Faltan: {faltan} pts para aprobar")
        lines.append(f"     → Los indicadores técnicos no tienen suficiente confluencia.")
        lines.append(f"        Revisar el desglose abajo para ver qué filtros fallaron.")

    elif 'Contexto' in motivo or 'contexto' in motivo.lower():
        lines.append(f"  🚫 RAZÓN: Contexto de mercado no válido")
        lines.append(f"     El análisis macro (SMA36, tendencia 4H, doble confirmación BTC EMA50 4H)")
        lines.append(f"     indica que la dirección del mercado va contra esta señal.")
        lines.append(f"     → Operar contra el contexto aumenta el riesgo de pérdida.")

    elif 'Sin TPs' in motivo or 'TP' in motivo:
        lines.append(f"  🚫 RAZÓN: Sin targets (TP) definidos")
        lines.append(f"     La señal no tiene niveles de take profit válidos.")
        lines.append(f"     → Sin TP no se puede calcular el RR ni el scoring.")

    elif 'datos' in motivo.lower() or 'insuficiente' in motivo.lower():
        lines.append(f"  🚫 RAZÓN: Datos de mercado insuficientes")
        lines.append(f"     No hay suficientes velas en el timeframe requerido.")
        lines.append(f"     → El análisis técnico necesita mínimo 30 velas cerradas.")

    elif '4h en short' in motivo.lower() or '4h en long' in motivo.lower() or 'smf 4h' in motivo.lower():
        lado_opuesto = "SHORT" if side == "LONG" else "LONG"
        lines.append(f"  🚫 RAZÓN: Conflicto de dirección institucional 4H")
        lines.append(f"     El flujo de dinero institucional en 4H apunta a {lado_opuesto}")
        lines.append(f"     pero la señal pide {side}.")
        lines.append(f"     El sistema exige alineación perfecta entre 4H y la dirección de la señal.")
        lines.append(f"     → Operar contra el flujo institucional 4H tiene alta probabilidad de pérdida.")
        lines.append(f"     → Scoring NO ejecutado. Rechazo en Fase 2/3 antes del análisis técnico.")

    elif 'alineación' in motivo.lower() or 'contratendencia' in motivo.lower():
        lines.append(f"  🚫 RAZÓN: Señal contratendencia — contexto macro adverso")
        lines.append(f"     El análisis macro (SMA36, EMA50 4H, ADX) indica tendencia contraria.")
        lines.append(f"     → Rechazo preventivo antes del scoring técnico.")

    else:
        lines.append(f"  🚫 RAZÓN: {motivo}")

    return "\n".join(lines)



# ─────────────────────────────────────────────────────────────────────────────
# MODO FLEXIBLE — logs separados para comparación
# ─────────────────────────────────────────────────────────────────────────────

def log_aprobada_flexible(sig: dict, analisis: dict):
    """
    Registra en signals_aprobadas_flexible.txt las señales aprobadas
    por el modo FLEXIBLE (el estricto las rechazó primero).

    Muestra el scoring técnico completo igual que log_aprobada,
    más un bloque adicional con el detalle del análisis flexible:
    tipo de setup detectado y puntuación del flexible.
    """
    ts     = _ts()
    symbol = sig.get('symbol', '?')
    side   = sig.get('side', '?')
    tf     = sig.get('timeframe', '?')
    entry  = float(sig.get('entry', 0))
    sl     = float(sig.get('stop',  0))
    tps    = sig.get('tps_list', [0, 0, 0])
    tp1    = float(tps[0]) if tps else 0
    tp3    = float(tps[-1]) if tps else 0
    qs     = sig.get('quality_score', 0)
    ia_prob = float(sig.get('ia_probability', 0))

    score  = analisis.get('puntaje', 0)
    det    = analisis.get('detalle_puntajes', {})
    rr_tp1 = float(det.get('rr_tp1', 0))
    rr_tp3 = float(det.get('rr_tp3', 0))

    riesgo_pct   = abs(entry - sl)  / entry * 100 if entry > 0 else 0
    ganancia_tp1 = abs(tp1 - entry) / entry * 100 if entry > 0 else 0
    ganancia_tp3 = abs(tp3 - entry) / entry * 100 if entry > 0 else 0

    # Datos del modo flexible
    flex_score  = analisis.get('flexible_score', 0) or 0
    flex_setup  = analisis.get('flexible_setup', []) or []
    flex_razones = analisis.get('flexible_razones', []) or []

    setup_str = ' + '.join(sorted(set(flex_setup))) if flex_setup else 'N/A'

    desglose = _construir_desglose(analisis, sig)

    texto = (
        f"\n{SEP}\n"
        f"🟡  SEÑAL APROBADA (FLEXIBLE) — {ts}\n"
        f"{SEP}\n"
        f"\n"
        f"  📌 PAR          : {symbol}  {side}  ({tf})\n"
        f"  💰 ENTRY        : {entry:.8g}\n"
        f"  🛑 STOP LOSS    : {sl:.8g}   → riesgo {riesgo_pct:.2f}% por unidad\n"
        f"  🎯 TP1          : {tp1:.8g}   → potencial {ganancia_tp1:.2f}%\n"
        f"  🎯 TP3          : {tp3:.8g}   → potencial {ganancia_tp3:.2f}%\n"
        f"  📐 RR           : TP1=1:{rr_tp1:.2f}  /  TP3=1:{rr_tp3:.2f}\n"
        f"\n"
        f"  📊 Quality Score : {qs}  |  IA Prob: {ia_prob:.1%}\n"
        f"  🟡 SCORE FINAL  : {score} pts  →  APROBADA (MODO FLEXIBLE)\n"
        f"\n"
        f"{SEP_THIN}\n"
        f"  🔄 ANÁLISIS FLEXIBLE\n"
        f"{SEP_THIN}\n"
        f"  Setup detectado : {setup_str}\n"
        f"  Score flexible  : {flex_score} pts\n"
    )

    if flex_razones:
        texto += f"  Detalle flexible:\n"
        for linea in flex_razones:
            texto += f"  {linea}\n"

    texto += (
        f"\n"
        f"  ⚠️  ATENCIÓN: Esta señal NO pasó el filtro estricto.\n"
        f"      Seguimiento manual recomendado para calibrar el modo flexible.\n"
        f"\n"
        f"{SEP_THIN}"
        f"{desglose}\n"
        f"\n"
        f"{SEP_THIN}\n"
        f"  🔍 REVISIÓN     : ¿el precio llegó al TP o SL después? Revisar manualmente.\n"
        f"{SEP}"
    )
    _escribir(ARCHIVO_APROBADAS_FLEXIBLE, texto)


def log_rechazada_flexible(sig: dict, analisis: dict):
    """
    Registra en signals_rechazadas_flexible.txt Y también en signals_rechazadas.txt
    las señales que el estricto rechazó Y el flexible también rechazó.
    """
    ts     = _ts()
    symbol = sig.get('symbol', '?')
    side   = sig.get('side', '?')
    tf     = sig.get('timeframe', '?')
    entry  = float(sig.get('entry', 0))
    sl     = float(sig.get('stop',  0))
    tps    = sig.get('tps_list', [0, 0, 0])
    tp1    = float(tps[0]) if tps else 0
    tp3    = float(tps[-1]) if tps else 0
    qs     = sig.get('quality_score', 0)
    ia_prob = float(sig.get('ia_probability', 0))

    motivo   = analisis.get('razon_rechazo') or analisis.get('mensaje', 'desconocido')
    score    = analisis.get('puntaje', 0)
    det      = analisis.get('detalle_puntajes', {})
    rr_tp1   = float(det.get('rr_tp1', 0))
    rr_tp3   = float(det.get('rr_tp3', 0))

    riesgo_pct   = abs(entry - sl)  / entry * 100 if entry > 0 else 0
    ganancia_tp1 = abs(tp1 - entry) / entry * 100 if entry > 0 else 0
    ganancia_tp3 = abs(tp3 - entry) / entry * 100 if entry > 0 else 0

    # Datos del flexible
    flex_score   = analisis.get('flexible_score', 0) or 0
    flex_setup   = analisis.get('flexible_setup', []) or []
    flex_razones = analisis.get('flexible_razones', []) or []
    setup_str    = ' + '.join(sorted(set(flex_setup))) if flex_setup else 'ninguno'

    texto = (
        f"\n{SEP}\n"
        f"🔴  RECHAZADA TOTAL (ESTRICTO + FLEXIBLE) — {ts}\n"
        f"{SEP}\n"
        f"\n"
        f"  📌 PAR          : {symbol}  {side}  ({tf})\n"
        f"  💰 ENTRY        : {entry:.8g}\n"
        f"  🛑 STOP LOSS    : {sl:.8g}   → riesgo {riesgo_pct:.2f}% por unidad\n"
        f"  🎯 TP1          : {tp1:.8g}   → potencial {ganancia_tp1:.2f}%\n"
        f"  🎯 TP3          : {tp3:.8g}   → potencial {ganancia_tp3:.2f}%\n"
        f"  📐 RR           : TP1=1:{rr_tp1:.2f}  /  TP3=1:{rr_tp3:.2f}\n"
        f"\n"
        f"  📊 Quality Score : {qs}  |  IA Prob: {ia_prob:.1%}\n"
        f"  🔴 SCORE FINAL  : {score} pts  →  RECHAZADA (ambos modos)\n"
        f"\n"
        f"{SEP_THIN}\n"
        f"  💬 MOTIVO RECHAZO ESTRICTO\n"
        f"{SEP_THIN}\n"
        f"  {motivo}\n"
        f"\n"
        f"{SEP_THIN}\n"
        f"  🔄 ANÁLISIS FLEXIBLE (también rechazó)\n"
        f"{SEP_THIN}\n"
        f"  Score flexible  : {flex_score} pts (mínimo requerido: 2)\n"
        f"  Setup detectado : {setup_str}\n"
    )

    if flex_razones:
        texto += f"  Detalle flexible:\n"
        for linea in flex_razones:
            texto += f"  {linea}\n"

    texto += (
        f"\n"
        f"{SEP_THIN}\n"
        f"  🔍 REVISIÓN     : ¿el precio llegó al TP o SL después? Revisar manualmente.\n"
        f"{SEP}"
    )
    
    # ✅ ESCRIBIR EN AMBOS ARCHIVOS
    _escribir(ARCHIVO_RECHAZADAS_FLEXIBLE, texto)
    _escribir(ARCHIVO_RECHAZADAS, texto)  # ← NUEVO: también en rechazadas generales


def log_resultado(symbol: str, side: str, entry: float,
                  resultado: str, pnl: float, duracion_min: float):
    """
    Agrega el resultado final a signals_aprobadas.txt
    cuando cierra una posicion (WIN o LOSS).
    """
    ts    = _ts()
    emoji = '🏆 WIN' if resultado == 'WIN' else '💥 LOSS'
    pnl_str = f"{pnl:+.4f} USDT"
    linea = (
        f"\n  ╔══ RESULTADO FINAL [{ts}] ══╗\n"
        f"  ║  {emoji}  |  {symbol} {side}\n"
        f"  ║  Entry: {entry:.6g}  |  PNL: {pnl_str}  |  Duración: {duracion_min:.0f} min\n"
        f"  ╚{'═'*50}╝\n"
    )
    _escribir(ARCHIVO_APROBADAS, linea)