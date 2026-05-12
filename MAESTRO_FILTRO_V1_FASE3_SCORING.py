"""
MAESTRO_FILTRO_V1_FASE3_SCORING.py
FASE 3 — Scoring (sumar puntos) + RR.

CAMBIOS v2.4 (2026-04):
  - NUEVO BLOQUE 7: BB position score (−2 a +2).
    Mide %B del precio en el TF de entrada (1H o 15M).
    LONG: %B ≤ 0.20 → +2 | 0.20–0.45 → +1 | 0.45–0.75 → 0 | 0.75–0.90 → −1 | >0.90 → −2
    SHORT: lógica inversa exacta.
    1D: solo confirma cap (nunca suma puntos positivos), así no domina el resultado.

  - SCORE_APROBACION = 13 (valor activo).
    El bloque BB suma hasta +2 en condiciones ideales y resta hasta −2 en zonas de riesgo.
    Señal típica buena: 14–19 pts. El umbral 13 es el punto de corte.

  - puntuar_confluencia recibe candles_1d (opcional) y lo propaga a calc_bb_position_score.
  - evaluar_senal recibe candles_1d (opcional) y lo propaga.

CAMBIOS v2.3 (2026-04):
  - RSI NEUTRO → 0 pts (era +1).
    RSI entre 40-60 (LONG) o 40-60 (SHORT) no confirma presión direccional.
    Dar +1 por "indecisión" inflaba el score y hacía pasar señales sin
    confluencia real (caso NEAR/UNI: esos +1 empujaron de 12 → 13).
    Nueva escala LONG:  ≤30=+4, ≤40=+3, ≤50=+2, ≤60=0, >60=0
    Nueva escala SHORT: ≥70=+4, ≥60=+3, ≥50=+2, ≥40=0,  <40=0

  - SMF 4H OPUESTO → AHORA SOLO PENALIZA (ya NO rechaza inmediato).
    Se cambió de -999 a penalización de -2 puntos.

  - ELIMINADO bonus_externo por quality_score BOTAI (desde v2.1).
    El BOTAI solo provee señales (symbol/side/entry/stop/tps). No puntúa.
    La decisión final es 100% técnica — BOTAI es el generador, el filtro
    es el árbitro. Mezclar ambos distorsionaba el umbral real.

  - RR FIX: guard explícito para entry == stop (devolvía 0.0 sin advertencia).
    Ahora loguea el problema y rechaza con mensaje claro.

  - level_price (nivel S/R del BOTAI): se pasa a calc_sr_score como referencia
    adicional si el entry coincide con el nivel del BOTAI — ya integrado.

CAMBIOS v2.5 (2026-05):
  - SMF 4H opuesto: eliminado el rechazo inmediato (-999).
    Ahora solo penaliza con -2 puntos en pts_4h.
    Esto permite que señales con SMF 4H en contra puedan ser evaluadas
    y posiblemente aprobadas por el flexible.

BLOQUES DE SCORING:
  1H base:     RSI (0..4) + Zona valor EMA20/50 (0..4) + Volumen (-2..+4) + SMF (0..3 cap)
  NUEVO →      S/R + Rebote (−1..+5)
  4H confirma: RSI (0..2) + SMF (0..2 | penalización si opuesto) + Estructura HH/HL (0..1) = máx +5
  Sin datos 4H: penalización -2
  Contexto:    bonus_contratendencia de Fase 2 (-2..+4) — solo técnico, sin BOTAI

RANGO TEÓRICO POST-CALIBRACIÓN v2.5:
  Sin S/R y sin 4H:              ~2-7  pts → NO aprueba
  Con S/R + rebote + 4H parcial: ~11-15 pts → aprueba (>= 13)
  Señal con 4H completo + S/R:   ~16-22 pts → aprobación holgada
"""

from __future__ import annotations

from typing import Dict, List, Tuple
import logging

from analisis.indicadores import (
    RSI_NEUTRAL,
    SCORE_APROBACION,
    calc_ema,
    calc_rsi,
    calc_smf,
    calc_estructura_mercado,
    calc_zona_valor,
    calc_sr_score,
    calc_bb_position_score,
    detectar_resistencias_soportes,
    evaluar_smf_scoring,
)
from analisis.volumen_btc import _analizar_volumen

logger = logging.getLogger(__name__)


class InstitutionalScoring:
    def __init__(self):
        self.nombre_archivo = "MAESTRO_FILTRO_V1_FASE3_SCORING.py"
        self.version = "2.5"
        self.fase = "Fase 3 - Scoring (Puntos + RR)"

    @staticmethod
    def _calc_rr(entry: float, stop: float, tp: float) -> float:
        """
        Calcula el Risk/Reward ratio.

        FIX v2.1: guard explícito para riesgo = 0.
        Antes: silenciosamente devolvía 0.0 cuando entry == stop,
               causando rechazo por "RR insuficiente" sin explicar por qué.
        Ahora: devuelve -1.0 como señal de error para que evaluar_senal()
               lo detecte y rechace con mensaje informativo.
        """
        riesgo = abs(entry - stop)
        if riesgo <= 0:
            logger.warning(
                "_calc_rr: entry=%.6f igual a stop=%.6f — RR inválido", entry, stop
            )
            return -1.0  # señal de error, no de RR bajo
        beneficio = abs(tp - entry)
        return beneficio / riesgo

    def puntuar_confluencia(
        self,
        side: str,
        entry: float,
        candles_1h: List[dict],
        resumen: List[str],
        level_price: float = 0.0,
        candles_4h: List[dict] = None,
        tf_minutos: int = 60,
        candles_1d: List[dict] = None,
    ) -> Tuple[int, Dict]:
        """
        Scoring multiframe: 1H (base) + 4H (confirmación).

        BLOQUE 1 — RSI 1H (0..4)         [recalibrado: neutro=+1, confirmado=+3/+4]
        BLOQUE 2 — Zona de valor 1H (0..3)
        BLOQUE 3 — Volumen 1H (-2..+4)
        BLOQUE 4 — SMF Cloud 1H (0..3 cap)
        BLOQUE 5 — S/R + Rebote (-1..+5)    [panorama calibrado por tf_minutos]
        BLOQUE 6 — EMA 9/21 + Distancia Entry (-2..+1) [ventana por TF]
        CONFIRMACIÓN 4H — RSI+SMF+Estructura (0..5)
        Sin datos 4H → -2 pts

        tf_minutos: duración de cada vela de candles_1h en minutos.
                    60 = 1H (default), 15 = 15M, 240 = 4H.
                    Usado por calc_sr_score para calibrar la ventana de rebote.

        Umbral de aprobación: SCORE_APROBACION = 13
        Rango: -3 a 23 (con bloque S/R)
        """
        puntos = 0
        detalle: Dict = {}

        candles_cerradas = candles_1h[:-1] if candles_1h and len(candles_1h) > 1 else candles_1h or []
        closes = [c["close"] for c in candles_cerradas] if candles_cerradas else []

        # ── BLOQUE 1: RSI 1H (0..4) ──────────────────────────────────────────
        rsi = float(calc_rsi(closes, 14)) if closes else RSI_NEUTRAL
        detalle["rsi"] = rsi

        if side == "LONG":
            if rsi <= 30:
                pts_rsi = 4; tag = f"sobreventa fuerte ({rsi:.1f}≤30) 🔥"
            elif rsi <= 40:
                pts_rsi = 3; tag = f"sobreventa ({rsi:.1f}≤40)"
            elif rsi <= 50:
                pts_rsi = 2; tag = f"bajo-neutro ({rsi:.1f}≤50)"
            elif rsi <= 60:
                # FIX v2.3: zona neutra (40-60) ya no regala puntos.
                # RSI en 40-60 no confirma presión compradora → no aporta confluencia real.
                pts_rsi = 0; tag = f"neutro ({rsi:.1f}≤60) — sin confluencia RSI"
            else:
                pts_rsi = 0; tag = f"sobrecompra ({rsi:.1f}>60) — riesgo"
        else:  # SHORT
            if rsi >= 70:
                pts_rsi = 4; tag = f"sobrecompra fuerte ({rsi:.1f}≥70) 🔥"
            elif rsi >= 60:
                pts_rsi = 3; tag = f"sobrecompra ({rsi:.1f}≥60)"
            elif rsi >= 50:
                pts_rsi = 2; tag = f"alto-neutro ({rsi:.1f}≥50)"
            elif rsi >= 40:
                # FIX v2.3: zona neutra (40-60) ya no regala puntos.
                # RSI en 40-60 no confirma presión vendedora → no aporta confluencia real.
                pts_rsi = 0; tag = f"neutro ({rsi:.1f}≥40) — sin confluencia RSI"
            else:
                pts_rsi = 0; tag = f"sobreventa ({rsi:.1f}<40) — riesgo SHORT"

        puntos += pts_rsi
        detalle["pts_rsi_1h"] = pts_rsi
        icono = "✅" if pts_rsi >= 3 else ("➖" if pts_rsi >= 1 else "❌")
        resumen.append(f"  {icono} RSI 1H {tag} → +{pts_rsi}")

        # ── BLOQUE 2: Zona de valor 1H (0..4) ───────────────────────────────
        pts_zona = 0
        if len(closes) >= 20:
            ema20_list = calc_ema(closes, 20)
            ema20_1h = float(ema20_list[-1]) if ema20_list else 0.0
        else:
            ema20_1h = 0.0

        if len(closes) >= 50:
            ema50_list = calc_ema(closes, 50)
            ema50_1h = float(ema50_list[-1]) if ema50_list else 0.0
        else:
            ema50_1h = 0.0

        zona = calc_zona_valor(candles_cerradas, ema20_1h, ema50_1h, side)
        detalle["zona_valor"] = zona

        if zona["calidad"] == "EXACTO":
            pts_zona = 3 if zona["rebote_detectado"] else 2
            icono_z = "🔥" if zona["rebote_detectado"] else "✅"
            resumen.append(f"  {icono_z} Zona de valor EXACTA {'+ rebote ✅' if zona['rebote_detectado'] else ''} → +{pts_zona}")
        elif zona["calidad"] == "CERCANO":
            pts_zona = 2 if zona["rebote_detectado"] else 1
            icono_z = "✅" if zona["rebote_detectado"] else "➖"
            resumen.append(f"  {icono_z} Zona de valor CERCANA {'+ rebote ✅' if zona['rebote_detectado'] else ''} → +{pts_zona}")
        else:
            pts_zona = 0
            resumen.append(f"  ➖ Precio fuera de zona de valor (EMAs 20/50) → +0")

        # level_price del BOTAI: solo confirma zona visualmente, no suma puntos extra.
        # El nivel ya está capturado por el bloque de zona de valor si es relevante.
        if level_price > 0:
            dist_level = abs(entry - level_price) / entry if entry > 0 else 1.0
            resumen.append(f"  📌 Nivel S/R BOTAI: {level_price:.6g} (dist {dist_level*100:.2f}%)")

        puntos += pts_zona
        detalle["pts_zona_valor_1h"] = pts_zona

        # ── BLOQUE 3: Volumen 1H (-2..+4) ────────────────────────────────────
        pts_vol = int(_analizar_volumen(candles_cerradas, side, resumen, label="1H"))
        detalle["pts_volumen_1h"] = pts_vol
        puntos += pts_vol

        # ── BLOQUE 4: SMF Cloud 1H (0..3 cap) ────────────────────────────────
        smf_1h = calc_smf(candles_cerradas)
        pts_smf, msg_smf = evaluar_smf_scoring(smf_1h, side, entry)
        detalle["smf_1h_money_flow"]   = smf_1h.get("money_flow", 0)
        detalle["smf_1h_strength_pct"] = smf_1h.get("strength_pct", 0)
        detalle["smf_1h_direction"]    = smf_1h.get("direction", "NEUTRAL")
        detalle["pts_smf_1h"]          = pts_smf
        if pts_smf != 0:
            resumen.append(msg_smf)
        puntos += pts_smf

        # ── BLOQUE 5: S/R + Rebote confirmado (-1..+5) ───────────────────────
        # Panorama y ventana de rebote calibrados por TF via tf_minutos:
        #   15M → 100 velas de S/R, rebote en últimas 8 velas
        #   1H  → 50  velas de S/R, rebote en últimas 6 velas
        #   4H  → 30  velas de S/R, rebote en últimas 4 velas
        # level_price del BOTAI se inyecta como pivote adicional si está ±5% del entry.
        candles_sr = list(candles_cerradas)

        pts_sr, det_sr = calc_sr_score(
            candles=candles_sr,
            entry=entry,
            side=side,
            resumen=resumen,
            level_price_externo=level_price,
            tf_minutos=tf_minutos,
        )
        detalle["pts_sr_rebote"]        = pts_sr
        detalle["sr_nivel_cercano"]     = det_sr.get("nivel_cercano", 0)
        detalle["sr_dist_pct"]          = det_sr.get("dist_pct", 0)
        detalle["sr_rebote_confirmado"] = det_sr.get("rebote_confirmado", False)
        detalle["sr_patron_rebote"]     = det_sr.get("patron_rebote", "")
        detalle["sr_ruptura_a_favor"]   = det_sr.get("ruptura_a_favor", False)
        puntos += pts_sr

        # ── BLOQUE 6: EMA 9/21 Momentum + Distancia Entry (-2..+1) ─────────────
        # Valida que el cruce de EMA 9/21 sea reciente (ventana por TF)
        # y que el entry del BOTAI no esté lejos del precio actual
        pts_ema921 = 0
        ventana_cruce = 2 if tf_minutos == 15 else 1  # 15M→2 velas, 1H/4H→1 vela
        
        if len(closes) >= 22 and len(candles_cerradas) >= ventana_cruce + 2:
            ema9_list  = calc_ema(closes, 9)
            ema21_list = calc_ema(closes, 21)
            
            if ema9_list and ema21_list and len(ema9_list) >= ventana_cruce + 1:
                # Verificar cruce reciente en ventana permitida
                cruce_detectado = False
                for i in range(1, ventana_cruce + 2):
                    ema9_curr  = ema9_list[-i]
                    ema9_prev  = ema9_list[-(i+1)]
                    ema21_curr = ema21_list[-i]
                    ema21_prev = ema21_list[-(i+1)]
                    
                    if side == "LONG":
                        if ema9_prev <= ema21_prev and ema9_curr > ema21_curr:
                            cruce_detectado = True
                            break
                    else:  # SHORT
                        if ema9_prev >= ema21_prev and ema9_curr < ema21_curr:
                            cruce_detectado = True
                            break
                
                # Verificar alineación actual (EMA9 vs EMA21)
                ema9_actual  = ema9_list[-1]
                ema21_actual = ema21_list[-1]
                alineado = (side == "LONG" and ema9_actual > ema21_actual) or                            (side == "SHORT" and ema9_actual < ema21_actual)
                
                # Verificar distancia del entry al precio actual
                precio_actual = closes[-1]
                dist_entry_pct = abs(entry - precio_actual) / precio_actual * 100 if precio_actual > 0 else 99
                entry_cerca = dist_entry_pct <= 0.3
                
                detalle["ema9_actual"]    = ema9_actual
                detalle["ema21_actual"]   = ema21_actual
                detalle["ema921_alineado"] = alineado
                detalle["ema921_cruce_reciente"] = cruce_detectado
                detalle["dist_entry_pct"] = dist_entry_pct
                
                if cruce_detectado and entry_cerca:
                    pts_ema921 = 1
                    resumen.append(f"  ✅ EMA 9/21 cruce reciente + entry cerca ({dist_entry_pct:.2f}%) → +1")
                elif alineado and entry_cerca:
                    pts_ema921 = 0
                    resumen.append(f"  ➖ EMA 9/21 alineada, sin cruce reciente ({dist_entry_pct:.2f}%) → +0")
                elif not alineado and not entry_cerca:
                    pts_ema921 = -2
                    resumen.append(f"  ❌ EMA 9/21 contra señal + entry lejos ({dist_entry_pct:.2f}%) → -2")
                elif not alineado:
                    pts_ema921 = -2
                    resumen.append(f"  ❌ EMA 9/21 contra la señal → -2")
                elif not entry_cerca:
                    pts_ema921 = -1
                    resumen.append(f"  ⚠️ Entry lejos del precio actual ({dist_entry_pct:.2f}%) → -1")
        
        detalle["pts_ema921"] = pts_ema921
        puntos += pts_ema921

        # ── BLOQUE 7: BB Posición (−2..+2) ───────────────────────────────────
        # Mide %B del precio en el TF de entrada.
        # 1D actúa solo como confirmación de cap (nunca suma puntos positivos).
        pts_bb, det_bb = calc_bb_position_score(
            candles_entrada=candles_cerradas,
            side=side,
            resumen=resumen,
            candles_1d=candles_1d,
        )
        detalle["pts_bb_posicion"] = pts_bb
        detalle["bb_pct_b"]        = det_bb.get("pct_b", 0.5)
        detalle["bb_upper"]        = det_bb.get("bb_upper", 0)
        detalle["bb_lower"]        = det_bb.get("bb_lower", 0)
        detalle["bb_1d_pct_b"]     = det_bb.get("bb_1d_pct_b")
        detalle["bb_1d_cap"]       = det_bb.get("bb_1d_confirma_cap", False)
        puntos += pts_bb

        # ── CONFIRMACIÓN 4H (0..5) ────────────────────────────────────────────
        pts_4h = 0
        if candles_4h and len(candles_4h) > 1:
            candles_4h_cerradas = candles_4h[:-1]
            closes_4h = [c["close"] for c in candles_4h_cerradas]

            # RSI 4H (0..2)
            rsi_4h = float(calc_rsi(closes_4h, 14)) if closes_4h else 50.0
            detalle["rsi_4h"] = rsi_4h

            rsi_4h_fuerte   = (side == "SHORT" and rsi_4h >= 60) or (side == "LONG" and rsi_4h <= 40)
            rsi_4h_confirma = (side == "SHORT" and rsi_4h >= 55) or (side == "LONG" and rsi_4h <= 45)

            if rsi_4h_fuerte:
                pts_4h += 2
                resumen.append(f"  🔥 RSI 4H confirma fuerte ({rsi_4h:.1f}) → +2")
            elif rsi_4h_confirma:
                pts_4h += 1
                resumen.append(f"  ✅ RSI 4H confirma ({rsi_4h:.1f}) → +1")
            else:
                resumen.append(f"  ➖ RSI 4H diverge ({rsi_4h:.1f}) → +0")

            # SMF 4H (0..2)
            # 🔥 CAMBIO v2.5: SMF 4H opuesto ya NO rechaza, solo penaliza -2
            smf_4h = calc_smf(candles_4h_cerradas)
            mf_4h  = smf_4h.get("money_flow", 0)
            str_4h = smf_4h.get("mf_strength", 0)
            detalle["smf_4h_money_flow"]   = mf_4h
            detalle["smf_4h_strength_pct"] = smf_4h.get("strength_pct", 0)

            smf_4h_opuesto = (side == "SHORT" and mf_4h > 0.10) or \
                             (side == "LONG"  and mf_4h < -0.10)

            # 🔥 ANTES: return -999, detalle
            # 🔥 AHORA: solo penaliza -2 puntos
            if smf_4h_opuesto:
                resumen.append(
                    f"  ⚠️ SMF 4H OPUESTO a {side} (mf={mf_4h:+.2f}) → penalización -2 pts (ya NO rechaza)"
                )
                pts_4h -= 2  # penalización en lugar de rechazo
                detalle["smf_4h_opuesto_penalizado"] = True
            else:
                smf_4h_fuerte   = (side == "SHORT" and mf_4h < -0.20 and str_4h > 0.60) or \
                                   (side == "LONG"  and mf_4h >  0.20 and str_4h > 0.60)
                smf_4h_confirma = (side == "SHORT" and mf_4h < -0.10 and str_4h >= 0.40) or \
                                   (side == "LONG"  and mf_4h >  0.10 and str_4h >= 0.40)

                if smf_4h_fuerte:
                    pts_4h += 2
                    resumen.append(f"  🔥 SMF 4H fuerte (mf={mf_4h:+.2f} str={str_4h*100:.0f}%) → +2")
                elif smf_4h_confirma:
                    pts_4h += 1
                    resumen.append(f"  ✅ SMF 4H moderado (mf={mf_4h:+.2f} str={str_4h*100:.0f}%) → +1")
                else:
                    resumen.append(f"  ➖ SMF 4H débil/ruido (mf={mf_4h:+.2f} str={str_4h*100:.0f}%) → +0")

            # Estructura HH/HL 4H (0..1)
            est_4h = calc_estructura_mercado(candles_4h_cerradas, ventana=5)
            detalle["estructura_4h"] = est_4h["estructura"]
            est_alineada = (side == "LONG" and est_4h["estructura"] == "ALCISTA") or \
                           (side == "SHORT" and est_4h["estructura"] == "BAJISTA")

            if est_alineada:
                pts_4h += 1
                resumen.append(f"  ✅ Estructura 4H alineada ({est_4h['estructura']}) → +1")
            else:
                resumen.append(f"  ➖ Estructura 4H: {est_4h['estructura']} → +0")

            detalle["pts_confirmacion_4h"] = pts_4h
            puntos += pts_4h

            if pts_4h >= 4:
                resumen.append(f"  ⚡ Sincronización 4H completa (+{pts_4h})")
            elif pts_4h >= 2:
                resumen.append(f"  ✅ Sincronización 4H parcial (+{pts_4h})")
        else:
            detalle["pts_confirmacion_4h"] = 0
            puntos -= 2
            resumen.append("  ⚠️ Sin datos 4H — penalización -2 (scoring incompleto, solo 1H)")

        return puntos, detalle

    def evaluar_senal(
        self,
        *,
        side: str,
        entry: float,
        stop: float,
        tps_list: List[float],
        candles_1h: List[dict],
        contexto: Dict,
        rr_min: float = 0.8,
        bonus_externo: int = 0,
        level_price: float = 0.0,
        candles_4h: List[dict] = None,
        candles_1d: List[dict] = None,
        tf_minutos: int = 60,
    ) -> Dict:
        """
        Evalúa la señal y devuelve veredicto.

        tf_minutos: duración de cada vela en candles_1h (60=1H, 15=15M, 240=4H).
                    Se propaga a puntuar_confluencia → calc_sr_score para calibrar
                    la ventana de rebote en equivalencia temporal real con 4H.

        NOTA sobre bonus_externo:
        Ya NO incluye quality_score del BOTAI.
        Solo recibe bonus_contratendencia de Fase 2 (SMA36 + doble confirmación BTC EMA50 4H + ADX),
        que es puramente técnico. Rango: -2 a +4.
        """
        resumen: List[str] = []

        if not tps_list:
            return {
                "aceptada": False, "veredicto": "RECHAZO", "puntaje": 0,
                "detalle_puntajes": {}, "mensaje": "❌ Sin TPs disponibles",
                "razon_rechazo": "Sin TPs", "resumen": [],
            }

        tp1 = float(tps_list[0])
        tp3 = float(tps_list[-1])

        # FIX v2.1: detectar RR inválido (entry == stop)
        rr_tp1 = self._calc_rr(entry, stop, tp1)
        rr_tp3 = self._calc_rr(entry, stop, tp3)

        if rr_tp1 < 0:
            return {
                "aceptada": False,
                "veredicto": "RECHAZO",
                "puntaje": 0,
                "detalle_puntajes": {"rr_tp1": rr_tp1, "rr_tp3": rr_tp3},
                "mensaje": f"❌ RR inválido: entry={entry} igual a stop={stop} — datos de señal corruptos",
                "razon_rechazo": "Entry igual a stop",
                "resumen": resumen,
            }

        if rr_tp1 < rr_min:
            return {
                "aceptada": False,
                "veredicto": "RECHAZO",
                "puntaje": 0,
                "detalle_puntajes": {"rr_tp1": rr_tp1, "rr_tp3": rr_tp3},
                "mensaje": f"❌ RR TP1 insuficiente: {rr_tp1:.2f} (< {rr_min})",
                "razon_rechazo": "RR insuficiente",
                "resumen": resumen,
            }

        resumen.append(f"  ✅ RR OK: TP1={rr_tp1:.2f} | TP3={rr_tp3:.2f} (mín {rr_min})")

        # Confluencia técnica
        pts_conf, det_conf = self.puntuar_confluencia(
            side, entry, candles_1h, resumen,
            level_price=level_price,
            candles_4h=candles_4h,
            tf_minutos=tf_minutos,
            candles_1d=candles_1d,
        )

        # 🔥 ELIMINADO: ya no hay -999 por SMF 4H opuesto (ahora solo penaliza)
        # Ya no se verifica pts_conf == -999

        # Bonus externo = solo bonus_contratendencia de Fase 2 (técnico puro)
        # Rango permitido: -3 a +4
        bonus = max(min(bonus_externo, 4), -3)
        score_final = int(pts_conf) + bonus

        if bonus > 0:
            resumen.append(f"  ⭐ Bonus contexto (Fase 2): +{bonus}")
        elif bonus < 0:
            resumen.append(f"  ⚠️ Penalización contexto (Fase 2): {bonus}")

        # ── UMBRAL DINÁMICO según modo (ESTRICTO vs FLEXIBLE) ────────────────
        # El contexto trae "modo_flexible = True" cuando el flexible aprobó
        es_flexible = contexto.get("modo_flexible", False) if contexto else False

        if es_flexible:
            UMBRAL_FLEXIBLE = 5   # El flexible solo necesita 5 puntos
            aceptada = score_final >= UMBRAL_FLEXIBLE
            modo_texto = "FLEXIBLE"
        else:
            aceptada = score_final >= SCORE_APROBACION  # 13 para el estricto
            modo_texto = "ESTRICTO"

        # Actualizar el mensaje para reflejar el umbral usado
        if aceptada:
            mensaje = f"✅ APROBADA ({modo_texto}): score {score_final} (>= {UMBRAL_FLEXIBLE if es_flexible else SCORE_APROBACION})"
        else:
            mensaje = f"❌ RECHAZADA ({modo_texto}): score {score_final} (< {UMBRAL_FLEXIBLE if es_flexible else SCORE_APROBACION})"

        return {
            "aceptada": aceptada,
            "veredicto": "EJECUCION" if aceptada else "RECHAZO",
            "puntaje": score_final,
            "detalle_puntajes": {
                "confluencia": pts_conf,
                "bonus_contexto_fase2": bonus,
                "rr_tp1": rr_tp1,
                "rr_tp3": rr_tp3,
                "contexto": contexto,
                **det_conf,
            },
            "mensaje": mensaje,
            "razon_rechazo": None if aceptada else "Score insuficiente",
            "resumen": resumen,
        }