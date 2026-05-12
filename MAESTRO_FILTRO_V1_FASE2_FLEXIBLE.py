"""
NOMBRE DEL ARCHIVO: MAESTRO_FILTRO_V1_FASE2_FLEXIBLE.py
SISTEMA: Filtro Maestro Multiframe Institucional
VERSIÓN: 2.2
FASE: 2B - Análisis Flexible (módulo paralelo al estricto)
PROPÓSITO:
Capturar oportunidades que el modo ESTRICTO rechaza porque el mercado
no está perfectamente alineado, pero sí tiene condiciones operables.
Este módulo NUNCA reemplaza al estricto.
Solo se activa cuando el estricto ya rechazó.

CAMBIOS v2.2 (microestructura + filtros institucionales + endurecimiento):
[NUEVO BLOQUE 12] → MICROESTRUCTURA: HH/HL (LONG) y LH/LL (SHORT), ruptura de rango
[NUEVO BLOQUE 13] → VELA PARABÓLICA: penaliza climax/agotamiento (rango >2.5× promedio)
[NUEVO BLOQUE 14] → DISTANCIA EMA20: rechazo directo si >3%, penaliza si >2%
[ENDURECIDO]      → EXPLOSIÓN TARDÍA: rechazo directo si avance >3% + RSI extremo
[AJUSTADO]        → CRUCE+REBOTE: VELAS_MAX=10, VELAS_FULL=6, máximo +4 pts
[AJUSTADO]        → BB POSITION: solo puntúa si vela cierra en dirección del trade
[SUBIDO]          → Score mínimo aprobación: 5 → 7 pts

CAMBIOS v2.1 (mejoras de timing y detección de nacimiento de impulso):
[NUEVO BLOQUE] → EXPANSIÓN DE TENDENCIA (EMA20/34 + pendiente + separación progresiva)
[NUEVO FILTRO] → Detección de salida de compresión BB (ancho creciente)
[NUEVO FILTRO] → Penalización por "explosión tardía" (avance >2.5% + RSI extremo)
[OPTIMIZACIÓN] → Reducción de dependencia de EMA50 como trigger principal.
                 EMA50 queda como referencia macro/SR dinámica.
                 El timing real pasa a EMA20/34 + aceleración + expansión.

CASOS QUE CUBRE:
1. REBOTE EN TENDENCIA (EMA50 1H)
2. ADX BAJO PERO OPERABLE
3. INICIO DE MOVIMIENTO
4. BANDAS DE BOLLINGER (posición)
5. CRUCE + REBOTE (BB media cruza EMA50 + rebote medido)
6. REBOTE EN MEDIA BB (precio toca SMA20 y rebota)
7. RSI (solo zonas extremas, neutro 45-55)
8. DI (Direccional Index) - +DI/-DI confirma dirección
9. [NUEVO] EXPANSIÓN DE TENDENCIA (nacimiento del impulso, separación progresiva EMA20/34)

REGLAS DE DISEÑO:
- Score mínimo de aprobación: 5 pts
- DETECCIÓN DE LATERAL: Si BB squeeze (ancho < 4%) → RECHAZO
- SIN penalización por 4H en contra (solo informativo)
- TIMING PRINCIPAL: EMA20/34 + expansión + momentum progresivo
- EMA50: Referencia institucional, soporte/resistencia dinámica, zona macro

INTEGRACIÓN:
Fase 4 lo llama DESPUÉS de que el modo estricto rechazó.
Si el flexible aprueba → señal etiquetada como "FLEXIBLE"
Si el flexible rechaza → rechazo total
"""
from __future__ import annotations
from typing import Dict, List
from analisis.indicadores import calc_ema, calc_adx, calc_bb_position_score, calc_bollinger, calc_rsi


class FlexibleAnalyzer:
    def __init__(self):
        self.nombre_archivo = "MAESTRO_FILTRO_V1_FASE2_FLEXIBLE.py"
        self.version = "2.2"
        self.fase = "Fase 2B - Análisis Flexible"

    # ══════════════════════════════════════════════════════════════
    # CÁLCULO DE EMA50 1H REAL
    # ══════════════════════════════════════════════════════════════
    def _calc_ema50_1h(self, candles_1h: List[dict]) -> float:
        """
        Calcula la EMA50 real del 1H.
        NO usa la EMA50 del 4H — son valores distintos.
        Retorna 0.0 si hay datos insuficientes.
        """
        if not candles_1h or len(candles_1h) < 52:
            return 0.0
        velas_cerradas = candles_1h[:-1]
        closes = [c["close"] for c in velas_cerradas]
        ema_list = calc_ema(closes, 50)
        return float(ema_list[-1]) if ema_list else 0.0

    # ══════════════════════════════════════════════════════════════
    # BLOQUE 0 — DETECCIÓN DE MERCADO LATERAL (BB SQUEEZE)
    # ══════════════════════════════════════════════════════════════
    def _detectar_lateral_bb(self, candles_1h: List[dict], razones: list) -> bool:
        """
        Detecta mercado lateral usando Bandas de Bollinger.
        Si el ancho de banda es < 4% → squeeze → lateral → NO OPERAR.
        Retorna True si es lateral, False si hay tendencia.
        """
        if not candles_1h or len(candles_1h) < 25:
            return False  # Sin datos suficientes, asumir no lateral

        velas_cerradas = candles_1h[:-1] if len(candles_1h) > 1 else candles_1h
        closes = [c["close"] for c in velas_cerradas]

        bb = calc_bollinger(closes, period=20, std_dev=2.0)
        ancho_pct = bb.get('ancho_pct', 5.0)

        # Si las bandas están comprimidas (squeeze), es lateral
        if ancho_pct < 4.0:
            razones.append(f"  🚫 BB SQUEEZE: ancho={ancho_pct:.1f}% → mercado lateral, NO OPERAR")
            return True

        razones.append(f"  ✅ BB ancho normal: {ancho_pct:.1f}% → mercado con dirección")
        return False

    # ══════════════════════════════════════════════════════════════
    # BLOQUE 1 — REBOTE EN TENDENCIA (EMA50)
    # ══════════════════════════════════════════════════════════════
    def _evaluar_rebote(
        self,
        macro_4h: dict,
        candles_1h: List[dict],
        direccion_senal: str,
        score: int,
        razones: list,
        tipo_setup: list,
    ) -> int:
        """
        Condición: precio 1H cerca de EMA50 1H.
        Si 4H está alineado → +5 pts (base + boost)
        Si 4H está opuesto → +3 pts (solo base)

        Distancia máxima: 1.5% del precio actual.
        [v2.1] La EMA50 actúa como referencia macro/SR, no como trigger único.
        """
        ema50_1h = self._calc_ema50_1h(candles_1h)
        if ema50_1h <= 0:
            razones.append("  ➖ Rebote: EMA50 1H no calculable → 0 pts")
            return score

        if not candles_1h or len(candles_1h) < 2:
            return score

        precio = float(candles_1h[-2]["close"])
        distancia_pct = abs(precio - ema50_1h) / precio

        direccion_4h = macro_4h.get("direccion_permitida")
        alineado_4h = (direccion_4h == direccion_senal)

        if distancia_pct <= 0.015:
            score += 3  # base
            if alineado_4h:
                score += 2  # BOOST solo si 4H alineado
                razones.append(
                    f"  🔄 Rebote en tendencia: precio {precio:.6g} cerca "
                    f"EMA50_1H {ema50_1h:.6g} (dist={distancia_pct*100:.2f}%) → +5 pts (base+boost, 4H alineado)"
                )
            else:
                razones.append(
                    f"  🔄 Rebote técnico (4H opuesto): precio {precio:.6g} cerca "
                    f"EMA50_1H {ema50_1h:.6g} (dist={distancia_pct*100:.2f}%) → +3 pts (solo base)"
                )
            tipo_setup.append("REBOTE")

        # ── [GAP-2 v2.0] IMPULSO RECIENTE ────────────────────────────
        # Si el precio está entre 1.5% y 5% de la EMA50 1H PERO el
        # alejamiento ocurrió en las últimas 5 velas → es un impulso
        # activo, no una consolidación lejana. +2 pts.
        elif distancia_pct <= 0.05:
            impulso_detectado = False
            velas_cerradas = candles_1h[:-1] if len(candles_1h) > 1 else candles_1h
            ema_list = calc_ema([c["close"] for c in velas_cerradas], 50)
            if ema_list and len(velas_cerradas) >= 6:
                # Buscar si hace <= 5 velas el precio aún estaba cerca (<= 1.5%)
                ventana = velas_cerradas[-6:-1]  # las 5 velas anteriores a la actual
                for c in ventana:
                    dist_ant = abs(float(c["close"]) - ema50_1h) / float(c["close"])
                    if dist_ant <= 0.015:
                        impulso_detectado = True
                        break

            if impulso_detectado and alineado_4h:
                score += 2
                razones.append(
                    f"  🚀 [GAP-2] Impulso reciente desde EMA50_1H: precio {precio:.6g} "
                    f"(dist={distancia_pct*100:.2f}%, alejamiento en <= 5 velas, 4H alineado) → +2 pts"
                )
                tipo_setup.append("IMPULSO_RECIENTE")
            elif impulso_detectado:
                score += 1
                razones.append(
                    f"  🚀 [GAP-2] Impulso reciente desde EMA50_1H: precio {precio:.6g} "
                    f"(dist={distancia_pct*100:.2f}%, alejamiento en <= 5 velas, 4H opuesto) → +1 pt"
                )
                tipo_setup.append("IMPULSO_RECIENTE")
            else:
                razones.append(
                    f"  ➖ Rebote: precio alejado de EMA50_1H "
                    f"(dist={distancia_pct*100:.2f}%, alejamiento antiguo) → 0 pts"
                )
        else:
            razones.append(
                f"  ➖ Rebote: precio muy alejado de EMA50_1H "
                f"(dist={distancia_pct*100:.2f}% > 5%) → 0 pts"
            )

        return score

    # ══════════════════════════════════════════════════════════════
    # BLOQUE 2 — ADX FLEXIBLE (USA DI_PLUS Y DI_MINUS)
    # ══════════════════════════════════════════════════════════════
    def _evaluar_adx_flexible(
        self,
        candles_1h: List[dict],
        adx_data: dict,
        direccion_senal: str,
        score: int,
        razones: list,
        tipo_setup: list,
    ) -> int:
        """
        Usa el ADX real del 1H (recalculado aquí si adx_data viene vacío).
        ADX >= 20      → +2 pts (tendencia real)
        ADX 15–19      → +1 pt
        ADX 10–14      → 0 pts
        ADX < 10       → -2 pts
        Pendiente SUBIENDO → +1 pt adicional
        Penalización si ADX fuerte va contra dirección
        Setup ADX_FUERTE si ADX ≥ 20 y subiendo
        """
        adx_val = adx_data.get("adx", 0.0) if adx_data else 0.0
        pendiente = adx_data.get("pendiente", "NEUTRAL") if adx_data else "NEUTRAL"
        direccion_adx = adx_data.get("direccion", "NEUTRAL") if adx_data else "NEUTRAL"

        if adx_val == 0.0 and candles_1h and len(candles_1h) >= 30:
            velas_cerradas = candles_1h[:-1]
            adx_result = calc_adx(velas_cerradas)
            adx_val = adx_result.get('adx', 0.0)
            if len(velas_cerradas) >= 18:
                adx_ant_result = calc_adx(velas_cerradas[:-4])
                adx_ant = adx_ant_result.get('adx', adx_val)
                if adx_val > adx_ant + 0.5:
                    pendiente = "SUBIENDO"
                elif adx_val < adx_ant - 0.5:
                    pendiente = "BAJANDO"
                else:
                    pendiente = "NEUTRAL"

        # Penalización ADX contra dirección
        if adx_val >= 20 and direccion_adx != "NEUTRAL" and direccion_adx != direccion_senal:
            score -= 1
            razones.append(f"  ⚠️ ADX fuerte ({adx_val:.1f}) pero contra dirección ({direccion_adx} vs {direccion_senal}) → -1 pt")

        # Setup ADX_FUERTE
        if adx_val >= 20 and pendiente == "SUBIENDO":
            tipo_setup.append("ADX_FUERTE")
            razones.append(f"  🔥 ADX fuerte y subiendo ({adx_val:.1f}) → setup ADX_FUERTE detectado")

        if adx_val >= 20:
            score += 2
            razones.append(f"  ✅ ADX fuerte ({adx_val:.1f} ≥ 20) → +2 pts")
        elif adx_val >= 15:
            score += 1
            razones.append(f"  ⚡ ADX operable ({adx_val:.1f}, 15–19) → +1 pt")
            tipo_setup.append("ADX_DEBIL")
        elif adx_val >= 10:
            razones.append(f"  ➖ ADX muy débil ({adx_val:.1f}, 10–14) → 0 pts")
            tipo_setup.append("ADX_DEBIL")
        else:
            score -= 2
            razones.append(f"  🚫 ADX mercado muerto ({adx_val:.1f} < 10) → -2 pts")

        if pendiente == "SUBIENDO":
            score += 1
            razones.append(f"  ✅ ADX subiendo → +1 pt adicional")
        elif pendiente == "BAJANDO":
            razones.append(f"  ⚠️ ADX cayendo → 0 pts (sin penalización en modo flexible)")

        return score

    # ══════════════════════════════════════════════════════════════
    # BLOQUE 3 — INICIO DE MOVIMIENTO
    # ══════════════════════════════════════════════════════════════
    def _evaluar_inicio_movimiento(
        self,
        macro_4h: dict,
        candles_1h: List[dict],
        direccion_senal: str,
        score: int,
        razones: list,
        tipo_setup: list,
    ) -> int:
        """
        Condición: 4H apoya la señal + 1H aún no confirma + cruce reciente.
        Puntos: +3
        [v2.1] Se mantiene como contexto institucional, no como trigger principal.
        """
        if macro_4h.get("direccion_permitida") != direccion_senal:
            return score

        ema50_1h = self._calc_ema50_1h(candles_1h)
        if ema50_1h <= 0 or not candles_1h or len(candles_1h) < 10:
            return score

        velas_cerradas = candles_1h[:-1]
        precio_actual = float(velas_cerradas[-1]["close"])

        if direccion_senal == "LONG":
            en_lado_incorrecto_1h = precio_actual <= ema50_1h
        else:
            en_lado_incorrecto_1h = precio_actual >= ema50_1h

        if not en_lado_incorrecto_1h:
            return score

        ema_reciente = calc_ema([c["close"] for c in velas_cerradas], 50)
        if not ema_reciente:
            return score

        ema_val = ema_reciente[-1]
        hubo_cruce = False
        for c in velas_cerradas[-8:]:
            precio_c = float(c["close"])
            if direccion_senal == "LONG" and precio_c > ema_val:
                hubo_cruce = True
                break
            elif direccion_senal == "SHORT" and precio_c < ema_val:
                hubo_cruce = True
                break

        if hubo_cruce and en_lado_incorrecto_1h:
            score += 3
            razones.append(
                f"  🚀 Inicio de movimiento: 4H confirmó {direccion_senal}, "
                f"1H cruzó EMA50 recientemente (precio={precio_actual:.6g} vs EMA50_1H={ema50_1h:.6g}) → +3 pts"
            )
            tipo_setup.append("INICIO")
        else:
            razones.append(
                f"  ➖ Inicio de movimiento: no detectado (sin cruce reciente de EMA50 1H) → 0 pts"
            )

        return score

    # ══════════════════════════════════════════════════════════════
    # BLOQUE 4 — BTC SUAVE
    # ══════════════════════════════════════════════════════════════
    def _evaluar_btc_suave(
        self,
        btc_data: dict,
        direccion_senal: str,
        score: int,
        razones: list,
    ) -> int:
        """
        Versión suave del bonus BTC.
        BTC alineado  → +1 pt
        BTC neutral   →  0 pts
        BTC en contra → -1 pt
        """
        if not btc_data:
            razones.append("  ➖ BTC: sin datos → 0 pts")
            return score

        dir_btc = btc_data.get("direccion_btc", "NEUTRAL")

        if dir_btc == direccion_senal:
            score += 1
            razones.append(f"  ✅ BTC alineado ({dir_btc}) con señal → +1 pt")
        elif dir_btc == "NEUTRAL":
            razones.append(f"  ➖ BTC neutral → 0 pts")
        else:
            score -= 1
            razones.append(f"  ⚠️ BTC en contra ({dir_btc} vs {direccion_senal}) → -1 pt")

        return score

    # ══════════════════════════════════════════════════════════════
    # BLOQUE 5 — BANDAS DE BOLLINGER (posición del precio)
    # ══════════════════════════════════════════════════════════════
    def _evaluar_bb_position(
        self,
        candles_1h: List[dict],
        direccion_senal: str,
        score: int,
        razones: list,
        tipo_setup: list,
    ) -> int:
        """
        Evalúa posición del precio en Bandas de Bollinger usando calc_bb_position_score.
        La función ya devuelve puntos (-2 a +2) según %B.
        LONG:  %B bajo (cerca lower) → bueno (+2)
        SHORT: %B alto (cerca upper) → bueno (+2)
        """
        if not candles_1h or len(candles_1h) < 25:
            razones.append("  ➖ BB posición: datos insuficientes → 0 pts")
            return score

        # Usar velas cerradas
        velas_cerradas = candles_1h[:-1] if len(candles_1h) > 1 else candles_1h

        # Confirmación de vela real en dirección antes de puntuar
        vela_actual = velas_cerradas[-1]
        vela_confirma = (
            (direccion_senal == "LONG" and float(vela_actual["close"]) > float(vela_actual["open"])) or
            (direccion_senal == "SHORT" and float(vela_actual["close"]) < float(vela_actual["open"]))
        )

        # Llamar a la función existente (sin resumen para no duplicar)
        pts_bb, _ = calc_bb_position_score(
            candles_entrada=velas_cerradas,
            side=direccion_senal,
            resumen=[],  # No queremos que escriba en el resumen del flexible
            candles_1d=None,  # Sin datos 1D
        )

        # Solo puntuar positivo si la vela real confirma dirección
        if pts_bb > 0 and not vela_confirma:
            razones.append(f"  ➖ BB posición favorable ({pts_bb:+d}) pero vela cierra en contra → 0 pts")
            return score

        if pts_bb > 0:
            razones.append(f"  ✅ BB posición favorable ({pts_bb:+d} pts) + vela confirma dirección")
            if pts_bb >= 2:
                tipo_setup.append("BB_VALOR")
        elif pts_bb < 0:
            razones.append(f"  ⚠️ BB posición en contra ({pts_bb:+d} pts)")
        else:
            razones.append(f"  ➖ BB posición neutral ({pts_bb:+d} pts)")

        score += pts_bb
        return score

    # ══════════════════════════════════════════════════════════════
    # BLOQUE 6 — CRUCE + REBOTE (BB media cruza EMA50)
    # ══════════════════════════════════════════════════════════════
    def _evaluar_cruce_y_rebote(
        self,
        candles_1h: List[dict],
        direccion_senal: str,
        score: int,
        razones: list,
        tipo_setup: list,
    ) -> int:
        """
        Detecta cruce de media BB (SMA20) con EMA50 y verifica que el rebote
        no haya avanzado demasiado ni sea demasiado viejo.

        Parámetros:
            VELAS_MAX = 10   → máximo de velas desde el cruce [v2.2]
            AVANCE_MAX = 1.5 → avance máximo permitido (%)
            AVANCE_MIN = 0.3 → avance mínimo para confirmar rebote
        """
        if not candles_1h or len(candles_1h) < 60:
            return score

        velas_cerradas = candles_1h[:-1] if len(candles_1h) > 1 else candles_1h
        closes = [c["close"] for c in velas_cerradas]
        n = len(closes)

        # Calcular media BB (SMA20) y EMA50 para todas las velas
        medias_bb = []
        emas_50 = []

        for i in range(20, n + 1):
            subset = closes[:i]
            bb = calc_bollinger(subset, period=20, std_dev=2.0)
            medias_bb.append(bb.get('media', 0))
            ema = calc_ema(subset, 50)
            emas_50.append(ema[-1] if ema else 0)

        if len(medias_bb) < 5 or len(emas_50) < 5:
            return score

        # Buscar el CRUCE más reciente (últimas 40 velas)
        idx_cruce = -1
        tipo_cruce = None
        precio_cruce = 0

        for i in range(max(0, len(medias_bb) - 40), len(medias_bb) - 1):
            if i < 1:
                continue

            bb_ant = medias_bb[i-1]
            bb_act = medias_bb[i]
            ema_ant = emas_50[i-1]
            ema_act = emas_50[i]

            # Cruce alcista (BB cruza por encima de EMA50)
            if bb_ant <= ema_ant and bb_act > ema_act:
                idx_cruce = i
                tipo_cruce = "LONG"
                precio_cruce = closes[i]
            # Cruce bajista (BB cruza por debajo de EMA50)
            elif bb_ant >= ema_ant and bb_act < ema_act:
                idx_cruce = i
                tipo_cruce = "SHORT"
                precio_cruce = closes[i]

        # No hay cruce o la dirección no coincide
        if idx_cruce == -1 or tipo_cruce != direccion_senal:
            return score

        # ── FILTROS DE TIEMPO Y AVANCE ──────────────────────────────────────
        # [v2.2] Ajustado: VELAS_MAX=10, VELAS_FULL=6, máximo +4 pts
        # (antes: 15/10/+5 → demasiado generoso en ventana y puntuación)
        VELAS_MAX = 10
        VELAS_FULL = 6    # hasta aquí puntaje completo
        AVANCE_MAX = 1.5
        AVANCE_MIN = 0.3

        velas_desde_cruce = n - 1 - idx_cruce
        precio_actual = closes[-1]

        # Filtro 1: Máximo VELAS_MAX velas desde el cruce
        if velas_desde_cruce > VELAS_MAX:
            razones.append(
                f"  ➖ CRUCE + REBOTE: cruce hace {velas_desde_cruce} velas "
                f"(>{VELAS_MAX}) → demasiado viejo, se pierde oportunidad"
            )
            return score

        if direccion_senal == "LONG":
            avance_pct = (precio_actual - precio_cruce) / precio_cruce * 100

            # Filtro 2: Avance máximo
            if avance_pct > AVANCE_MAX:
                razones.append(
                    f"  ➖ CRUCE + REBOTE LONG: avance {avance_pct:.2f}% (>{AVANCE_MAX}%) "
                    f"→ ya subió mucho, entrada tardía"
                )
                return score

            # Filtro 3: Avance mínimo
            if avance_pct < AVANCE_MIN:
                razones.append(
                    f"  ➖ CRUCE + REBOTE LONG: avance {avance_pct:.2f}% (<{AVANCE_MIN}%) "
                    f"→ rebote no confirmado"
                )
                return score

            # Buscar mínimo después del cruce (para medir profundidad del rebote)
            minimo_post_cruce = min(closes[idx_cruce:])
            profundidad_rebote = (precio_cruce - minimo_post_cruce) / precio_cruce * 100

            pts_cruce = 4 if velas_desde_cruce <= VELAS_FULL else 2
            score += pts_cruce
            razon_deg = " " if velas_desde_cruce <= VELAS_FULL else " (degradado, vela 7-10) "
            razones.append(
                f"  🔥 CRUCE + REBOTE LONG: cruce hace {velas_desde_cruce} velas, "
                f"avance {avance_pct:.2f}%, profundidad rebote {profundidad_rebote:.2f}% → +{pts_cruce} pts{razon_deg}"
            )
            tipo_setup.append("CRUCE_REBOTE")

        else:  # SHORT
            avance_pct = (precio_cruce - precio_actual) / precio_cruce * 100

            if avance_pct > AVANCE_MAX:
                razones.append(
                    f"  ➖ CRUCE + REBOTE SHORT: caída {avance_pct:.2f}% (>{AVANCE_MAX}%) "
                    f"→ ya bajó mucho, entrada tardía"
                )
                return score

            if avance_pct < AVANCE_MIN:
                razones.append(
                    f"  ➖ CRUCE + REBOTE SHORT: caída {avance_pct:.2f}% (<{AVANCE_MIN}%) "
                    f"→ rebote no confirmado"
                )
                return score

            maximo_post_cruce = max(closes[idx_cruce:])
            profundidad_rebote = (maximo_post_cruce - precio_cruce) / precio_cruce * 100

            pts_cruce = 4 if velas_desde_cruce <= VELAS_FULL else 2
            score += pts_cruce
            razon_deg = " " if velas_desde_cruce <= VELAS_FULL else " (degradado, vela 7-10) "
            razones.append(
                f"  🔥 CRUCE + REBOTE SHORT: cruce hace {velas_desde_cruce} velas, "
                f"caída {avance_pct:.2f}%, profundidad rebote {profundidad_rebote:.2f}% → +{pts_cruce} pts{razon_deg}"
            )
            tipo_setup.append("CRUCE_REBOTE")

        return score

    # ══════════════════════════════════════════════════════════════
    # BLOQUE 7 — REBOTE EN MEDIA BB (precio toca SMA20)
    # ══════════════════════════════════════════════════════════════
    def _evaluar_rebote_bb_media(
        self,
        candles_1h: List[dict],
        direccion_senal: str,
        score: int,
        razones: list,
        tipo_setup: list,
    ) -> int:
        """
        Detecta cuando el precio toca la media de BB (SMA20) y rebota.
        """
        if not candles_1h or len(candles_1h) < 22:
            return score

        velas_cerradas = candles_1h[:-1] if len(candles_1h) > 1 else candles_1h
        closes = [c["close"] for c in velas_cerradas]

        bb = calc_bollinger(closes, period=20, std_dev=2.0)
        media_bb = bb.get('media', 0)

        if media_bb <= 0:
            return score

        precio_actual = closes[-1]
        distancia_pct = abs(precio_actual - media_bb) / precio_actual * 100

        # Precio muy cerca de la media BB (±0.8%)
        if distancia_pct <= 0.8:
            vela_actual = velas_cerradas[-1]
            vela_prev = velas_cerradas[-2] if len(velas_cerradas) >= 2 else vela_actual

            rebote_long = (
                float(vela_prev["close"]) < media_bb and
                float(vela_actual["close"]) > float(vela_prev["close"])
            )
            rebote_short = (
                float(vela_prev["close"]) > media_bb and
                float(vela_actual["close"]) < float(vela_prev["close"])
            )

            if direccion_senal == "LONG" and rebote_long:
                score += 3
                razones.append(f"  🔄 Rebote en media BB: precio {precio_actual:.6g} bajo media {media_bb:.6g} con intención alcista (dist={distancia_pct:.2f}%) → +3 pts")
                tipo_setup.append("REBOTE_BB_MEDIA")
            elif direccion_senal == "SHORT" and rebote_short:
                score += 3
                razones.append(f"  🔄 Rebote en media BB: precio {precio_actual:.6g} sobre media {media_bb:.6g} con intención bajista (dist={distancia_pct:.2f}%) → +3 pts")
                tipo_setup.append("REBOTE_BB_MEDIA")
            else:
                razones.append(f"  ➖ Precio cerca de media BB pero sin confirmación de rebote → 0 pts")
        else:
            razones.append(f"  ➖ Precio lejos de media BB (dist={distancia_pct:.2f}%) → 0 pts")

        return score

    # ══════════════════════════════════════════════════════════════
    # BLOQUE 8 — RSI FLEXIBLE (zonas extremas, neutro 45-55)
    # ══════════════════════════════════════════════════════════════
    def _evaluar_rsi_flexible(
        self,
        candles_1h: List[dict],
        direccion_senal: str,
        score: int,
        razones: list,
        tipo_setup: list,
        rsi_4h: float = 0.0,
    ) -> int:
        """
        Evalúa RSI con zona neutra en 45-55.
        Zona neutra → 0 pts (sin ruido).

        [GAP-1 v2.0] RSI doble sobrecompra/sobreventa:
        Si el RSI del 1H y el del 4H están ambos en zona extrema
        en la misma dirección → -1 pt de alerta (el movimiento ya corrió
        en ambos timeframes, riesgo de corrección mayor).
        No es un rechazo, es un aviso para gestionar el riesgo.
        """
        if not candles_1h or len(candles_1h) < 30:
            return score

        closes = [c["close"] for c in candles_1h[:-1]]
        rsi = calc_rsi(closes, 14)

        if direccion_senal == "LONG":
            if rsi <= 30:
                score += 3
                razones.append(f"  🔥 RSI sobreventa extrema ({rsi:.1f} ≤ 30) → +3 pts")
                tipo_setup.append("RSI_SOBREVENTA")
            elif rsi <= 40:
                score += 2
                razones.append(f"  ✅ RSI zona de venta ({rsi:.1f} ≤ 40) → +2 pts")
            elif rsi <= 45:
                score += 1
                razones.append(f"  ➕ RSI bajo ({rsi:.1f}) → +1 pt")
            elif rsi <= 55:
                razones.append(f"  ➖ RSI neutro (45-55: {rsi:.1f}) → 0 pts (sin confluencia)")
            else:
                razones.append(f"  ➖ RSI {rsi:.1f} (zona alta) → 0 pts")
        else:  # SHORT
            if rsi >= 70:
                score += 3
                razones.append(f"  🔥 RSI sobrecompra extrema ({rsi:.1f} ≥ 70) → +3 pts")
                tipo_setup.append("RSI_SOBRECOMPRA")
            elif rsi >= 60:
                score += 2
                razones.append(f"  ✅ RSI zona de compra ({rsi:.1f} ≥ 60) → +2 pts")
            elif rsi >= 55:
                score += 1
                razones.append(f"  ➕ RSI alto ({rsi:.1f}) → +1 pt")
            elif rsi >= 45:
                razones.append(f"  ➖ RSI neutro (45-55: {rsi:.1f}) → 0 pts (sin confluencia)")
            else:
                razones.append(f"  ➖ RSI {rsi:.1f} (zona baja) → 0 pts")

        # ── [GAP-1 v2.0] ALERTA RSI DOBLE EXTREMO ───────────────────────
        # Si ambos TFs están sobrecomprados (LONG) o sobrevendidos (SHORT)
        # simultáneamente → -1 pt. No rechaza; solo avisa del riesgo.
        if rsi_4h > 0:
            if direccion_senal == "LONG" and rsi > 65 and rsi_4h > 65:
                score -= 1
                razones.append(
                    f"  ⚠️ [GAP-1] RSI doble sobrecompra: 1H={rsi:.1f} y 4H={rsi_4h:.1f} "
                    f"ambos > 65 → -1 pt (movimiento ya corrió en ambos TFs)"
                )
            elif direccion_senal == "SHORT" and rsi < 35 and rsi_4h < 35:
                score -= 1
                razones.append(
                    f"  ⚠️ [GAP-1] RSI doble sobreventa: 1H={rsi:.1f} y 4H={rsi_4h:.1f} "
                    f"ambos < 35 → -1 pt (movimiento ya corrió en ambos TFs)"
                )

        return score

    # ══════════════════════════════════════════════════════════════
    # BLOQUE 9 — DI (DIRECCIONAL INDEX) NUEVO v1.9
    # ══════════════════════════════════════════════════════════════
    def _evaluar_di_flexible(
        self,
        adx_data: dict,
        direccion_senal: str,
        score: int,
        razones: list,
        tipo_setup: list,
    ) -> int:
        """
        Evalúa +DI y -DI para confirmar dirección.
        +DI > -DI confirma LONG
        -DI > +DI confirma SHORT
        """
        di_plus = adx_data.get("di_plus", 0.0) if adx_data else 0.0
        di_minus = adx_data.get("di_minus", 0.0) if adx_data else 0.0

        if di_plus == 0.0 and di_minus == 0.0:
            return score

        if direccion_senal == "LONG":
            if di_plus > di_minus:
                score += 2
                razones.append(f"  ✅ DI confirma LONG (+DI={di_plus:.1f} > -DI={di_minus:.1f}) → +2 pts")
                tipo_setup.append("DI_CONFIRMA")
            else:
                razones.append(f"  ⚠️ DI no confirma LONG (+DI={di_plus:.1f} ≤ -DI={di_minus:.1f}) → 0 pts")
        else:  # SHORT
            if di_minus > di_plus:
                score += 2
                razones.append(f"  ✅ DI confirma SHORT (-DI={di_minus:.1f} > +DI={di_plus:.1f}) → +2 pts")
                tipo_setup.append("DI_CONFIRMA")
            else:
                razones.append(f"  ⚠️ DI no confirma SHORT (-DI={di_minus:.1f} ≤ +DI={di_plus:.1f}) → 0 pts")

        return score

    # ══════════════════════════════════════════════════════════════
    # BLOQUE 10 — EXPANSIÓN DE TENDENCIA (NUEVO v2.1)
    # ══════════════════════════════════════════════════════════════
    def _evaluar_expansion_tendencia(
        self,
        candles_1h: List[dict],
        adx_data: dict,
        direccion_senal: str,
        score: int,
        razones: list,
        tipo_setup: list,
    ) -> int:
        """
        BLOQUE NUEVO → EXPANSIÓN DE TENDENCIA
        Detecta el nacimiento real del impulso:
        - EMA20/34 alineadas y separación progresiva creciente
        - Pendiente EMA20 en dirección
        - BB saliendo de compresión (ancho creciendo)
        - ADX subiendo
        Penaliza "explosión tardía" (precio ya corrió + RSI extremo)
        """
        if not candles_1h or len(candles_1h) < 55:
            razones.append("  ➖ Expansión: datos insuficientes para EMA20/34 → 0 pts")
            return score

        velas_cerradas = candles_1h[:-1] if len(candles_1h) > 1 else candles_1h
        closes = [c["close"] for c in velas_cerradas]

        # Calcular EMAs
        ema20_list = calc_ema(closes, 20)
        ema34_list = calc_ema(closes, 34)
        if not ema20_list or not ema34_list or len(ema20_list) < 10:
            razones.append("  ➖ Expansión: cálculo de EMA20/34 fallido → 0 pts")
            return score

        ema20_curr = ema20_list[-1]
        ema34_curr = ema34_list[-1]

        # Alineación
        alineada = False
        if direccion_senal == "LONG":
            alineada = ema20_curr > ema34_curr
        else:
            alineada = ema20_curr < ema34_curr

        if not alineada:
            razones.append(f"  ➖ Expansión: EMA20/34 no alineadas para {direccion_senal} → 0 pts")
            return score

        score += 1
        razones.append(f"  ✅ EMA20/34 alineadas para {direccion_senal} → +1 pt")
        tipo_setup.append("EMA_ALINEADAS")

        # Separación progresiva (últimas 4 velas)
        separaciones = []
        for i in range(-4, 0):
            sep = abs(ema20_list[i] - ema34_list[i]) / ema34_list[i] * 100
            separaciones.append(sep)

        separacion_actual = separaciones[-1]
        # Verificar si es progresivamente creciente
        progresiva = all(separaciones[i] < separaciones[i+1] for i in range(len(separaciones)-1))
        if progresiva and separacion_actual > 0.08: # Umbral ajustado (0.08) para capturar más activos lentos
            score += 2
            razones.append(f"  📈 Separación EMA20-34 progresiva y creciente ({separacion_actual:.2f}%) → +2 pts")
            tipo_setup.append("SEP_PROGRESIVA")
        elif separacion_actual > 0.1:
            score += 1
            razones.append(f"  ➕ Separación EMA20-34 presente pero no progresiva ({separacion_actual:.2f}%) → +1 pt")
        else:
            razones.append(f"  ➖ Separación EMA20-34 mínima ({separacion_actual:.2f}%) → 0 pts adicionales")

        # Pendiente EMA20 (aceleración)
        pendiente_ema20 = (ema20_curr - ema20_list[-4]) / ema20_list[-4] * 100
        if (direccion_senal == "LONG" and pendiente_ema20 > 0.1) or \
           (direccion_senal == "SHORT" and pendiente_ema20 < -0.1):
            score += 1
            razones.append(f"  📐 Pendiente EMA20 favorable ({pendiente_ema20:+.2f}%) → +1 pt")
        else:
            razones.append(f"  ➖ Pendiente EMA20 plana o en contra → 0 pts")

        # BB Expansión (salida de squeeze)
        bb_actual = calc_bollinger(closes[-20:], period=20, std_dev=2.0)
        bb_prev = calc_bollinger(closes[-40:-20], period=20, std_dev=2.0) if len(closes) >= 40 else None
        if bb_actual and bb_prev:
            ancho_actual = bb_actual.get('ancho_pct', 0)
            ancho_prev = bb_prev.get('ancho_pct', 0)
            if ancho_actual > ancho_prev and ancho_prev < 4.0: # Salía de squeeze
                score += 1
                razones.append(f"  🌊 BB expandiendo desde squeeze ({ancho_prev:.1f}% → {ancho_actual:.1f}%) → +1 pt")
                tipo_setup.append("BB_EXPANSION")
            elif ancho_actual > ancho_prev:
                razones.append(f"  ➕ BB expandiendo ({ancho_prev:.1f}% → {ancho_actual:.1f}%) → 0 pts adicionales")
            else:
                razones.append(f"  ➖ BB contrayéndose o estable → 0 pts")
        else:
            razones.append("  ➖ BB: datos insuficientes para comparar expansión → 0 pts")

        # ADX subiendo (usar pendiente de adx_data)
        adx_pendiente = adx_data.get("pendiente", "NEUTRAL") if adx_data else "NEUTRAL"
        if adx_pendiente == "SUBIENDO":
            score += 1
            razones.append("  🔼 ADX subiendo → +1 pt adicional")
        else:
            razones.append(f"  ➖ ADX pendiente: {adx_pendiente} → 0 pts adicionales")

        # ── PENALIZACIÓN / RECHAZO: EXPLOSIÓN TARDÍA ────────────────────────────────
        precio_actual = float(candles_1h[-2]["close"])
        # Calcular avance reciente (últimas 8 velas)
        if len(closes) >= 9:
            min_reciente = min(closes[-9:])
            max_reciente = max(closes[-9:])

            if direccion_senal == "LONG":
                avance_reciente = (precio_actual - min_reciente) / min_reciente * 100
            else:
                avance_reciente = (max_reciente - precio_actual) / max_reciente * 100

            rsi_1h = calc_rsi(closes, 14)

            # RECHAZO DIRECTO: avance > 3% + RSI extremo → FOMO institucional
            if avance_reciente > 3.0:
                if (direccion_senal == "LONG" and rsi_1h > 68) or \
                   (direccion_senal == "SHORT" and rsi_1h < 32):
                    razones.append(
                        f"  🚫 EXPLOSIÓN TARDÍA CRÍTICA: avance {avance_reciente:.2f}% > 3% "
                        f"+ RSI {'alto' if direccion_senal == 'LONG' else 'bajo'} ({rsi_1h:.1f}) "
                        f"→ RECHAZO DIRECTO (FOMO institucional)"
                    )
                    tipo_setup.append("EXPLOSION_TARDIA_CRITICA")
                    return -999  # Señal de rechazo forzado al método principal

            # PENALIZACIÓN LEVE: avance 2.5-3% + RSI extremo → -2 pts
            elif avance_reciente > 2.5:
                if (direccion_senal == "LONG" and rsi_1h > 68) or \
                   (direccion_senal == "SHORT" and rsi_1h < 32):
                    score -= 2
                    razones.append(
                        f"  ⚠️ EXPLOSIÓN TARDÍA: avance {avance_reciente:.2f}% + RSI "
                        f"{'alto' if direccion_senal == 'LONG' else 'bajo'} → -2 pts (evita chase)"
                    )
                    tipo_setup.append("EXPLOSION_TARDIA")

        return score

    # ══════════════════════════════════════════════════════════════
    # BLOQUE 11 — RECHAZO INSTITUCIONAL RECIENTE
    # ══════════════════════════════════════════════════════════════
    def _evaluar_rechazo_reciente(
        self,
        candles_1h: List[dict],
        direccion_senal: str,
        score: int,
        razones: list,
        tipo_setup: list,
    ) -> int:
        """
        Detecta mechas institucionales fuertes recientes que indican
        rechazo de precio en zonas de resistencia/soporte.
        - Penaliza LONGs si hay wick superior fuerte en velas recientes
        - Penaliza SHORTs si hay wick inferior fuerte en velas recientes
        SIN bloquear totalmente → solo -2 pts para mantener flexibilidad.
        """
        if not candles_1h or len(candles_1h) < 12:
            return score

        velas = candles_1h[-8:-1]

        rechazo_detectado = False

        for vela in velas:

            high = float(vela["high"])
            low = float(vela["low"])
            open_ = float(vela["open"])
            close = float(vela["close"])

            cuerpo = abs(close - open_)
            rango = high - low

            if rango <= 0:
                continue

            mecha_superior = high - max(open_, close)
            mecha_inferior = min(open_, close) - low

            # LONG → rechazo arriba
            if direccion_senal == "LONG":

                if mecha_superior > cuerpo * 1.8 and mecha_superior > rango * 0.45:
                    rechazo_detectado = True
                    break

            # SHORT → rechazo abajo
            else:

                if mecha_inferior > cuerpo * 1.8 and mecha_inferior > rango * 0.45:
                    rechazo_detectado = True
                    break

        if rechazo_detectado:
            score -= 2

            razones.append(
                f"  ⚠️ Rechazo institucional reciente detectado contra {direccion_senal} → -2 pts"
            )

            tipo_setup.append("RECHAZO_RECIENTE")

        else:
            razones.append(
                f"  ✅ Sin rechazo institucional reciente → 0 pts"
            )

        return score

    # ══════════════════════════════════════════════════════════════
    # BLOQUE 12 — MICROESTRUCTURA (HH/HL para LONG, LH/LL para SHORT)
    # ══════════════════════════════════════════════════════════════
    def _evaluar_microestructura(
        self,
        candles_1h: List[dict],
        direccion_senal: str,
        score: int,
        razones: list,
        tipo_setup: list,
    ) -> int:
        """
        Valida que el precio esté ROMPIENDO estructura real, no solo
        mostrando confluencia de indicadores.
        LONG: último high > high anterior Y último low > low anterior (HH + HL)
              O close actual > máx de las últimas 5 velas (ruptura de rango)
        SHORT: último low < low anterior Y último high < high anterior (LH + LL)
              O close actual < mín de las últimas 5 velas (ruptura de rango)
        +2 pts si estructura confirmada
        -1 pt si estructura en contra (trampa potencial)
        """
        if not candles_1h or len(candles_1h) < 10:
            razones.append("  ➖ Microestructura: datos insuficientes → 0 pts")
            return score

        velas = candles_1h[:-1] if len(candles_1h) > 1 else candles_1h

        if len(velas) < 4:
            return score

        v_curr  = velas[-1]
        v_prev  = velas[-2]
        v_prev2 = velas[-3]

        high_curr  = float(v_curr["high"])
        low_curr   = float(v_curr["low"])
        close_curr = float(v_curr["close"])
        high_prev  = float(v_prev["high"])
        low_prev   = float(v_prev["low"])
        high_prev2 = float(v_prev2["high"])
        low_prev2  = float(v_prev2["low"])

        # Ruptura de rango: close actual > max(highs últimas 5 velas) para LONG
        highs_5 = [float(c["high"]) for c in velas[-6:-1]]
        lows_5  = [float(c["low"])  for c in velas[-6:-1]]

        if direccion_senal == "LONG":
            hh_hl = (high_curr > high_prev) and (low_curr > low_prev)
            ruptura_rango = close_curr > max(highs_5) if highs_5 else False
            estructura_en_contra = (high_curr < high_prev2) and (low_curr < low_prev2)

            if hh_hl or ruptura_rango:
                score += 2
                motivo = "HH+HL" if hh_hl else "ruptura de rango"
                razones.append(f"  🏗️ Microestructura LONG confirmada ({motivo}) → +2 pts")
                tipo_setup.append("MICROESTRUCTURA_OK")
            elif estructura_en_contra:
                score -= 1
                razones.append(f"  ⚠️ Microestructura en contra de LONG (LH+LL) → -1 pt")
                tipo_setup.append("MICROESTRUCTURA_CONTRA")
            else:
                razones.append(f"  ➖ Microestructura LONG neutral → 0 pts")

        else:  # SHORT
            lh_ll = (low_curr < low_prev) and (high_curr < high_prev)
            ruptura_rango = close_curr < min(lows_5) if lows_5 else False
            estructura_en_contra = (low_curr > low_prev2) and (high_curr > high_prev2)

            if lh_ll or ruptura_rango:
                score += 2
                motivo = "LH+LL" if lh_ll else "ruptura de rango"
                razones.append(f"  🏗️ Microestructura SHORT confirmada ({motivo}) → +2 pts")
                tipo_setup.append("MICROESTRUCTURA_OK")
            elif estructura_en_contra:
                score -= 1
                razones.append(f"  ⚠️ Microestructura en contra de SHORT (HH+HL) → -1 pt")
                tipo_setup.append("MICROESTRUCTURA_CONTRA")
            else:
                razones.append(f"  ➖ Microestructura SHORT neutral → 0 pts")

        return score

    # ══════════════════════════════════════════════════════════════
    # BLOQUE 13 — VELA PARABÓLICA / CLIMAX (filtro de velocidad)
    # ══════════════════════════════════════════════════════════════
    def _evaluar_vela_parabolica(
        self,
        candles_1h: List[dict],
        direccion_senal: str,
        score: int,
        razones: list,
        tipo_setup: list,
    ) -> int:
        """
        Detecta velas de climax/agotamiento: rango actual > 2.5× promedio.
        Indica barrida, exhaustion o entrada tardía en movimiento parabólico.
        -2 pts si se detecta. No rechaza solo (puede combinarse con otros filtros).
        """
        if not candles_1h or len(candles_1h) < 20:
            return score

        velas = candles_1h[:-1] if len(candles_1h) > 1 else candles_1h

        # Rango promedio de las últimas 14 velas (excluye la actual)
        rangos = [float(c["high"]) - float(c["low"]) for c in velas[-15:-1]]
        if not rangos or len(rangos) < 5:
            return score

        promedio_rango = sum(rangos) / len(rangos)
        if promedio_rango <= 0:
            return score

        vela_actual = velas[-1]
        rango_actual = float(vela_actual["high"]) - float(vela_actual["low"])
        ratio = rango_actual / promedio_rango

        if ratio > 2.5:
            score -= 2
            razones.append(
                f"  ⚠️ Vela parabólica detectada: rango {rango_actual:.6g} = "
                f"{ratio:.1f}× promedio ({promedio_rango:.6g}) → -2 pts (posible climax/agotamiento)"
            )
            tipo_setup.append("VELA_PARABOLICA")
        elif ratio > 1.8:
            razones.append(
                f"  ➖ Vela extendida ({ratio:.1f}× promedio) → informativo, sin penalización"
            )
        else:
            razones.append(
                f"  ✅ Rango vela normal ({ratio:.1f}× promedio) → 0 pts"
            )

        return score

    # ══════════════════════════════════════════════════════════════
    # BLOQUE 14 — DISTANCIA DEL PRECIO A EMA20 (filtro de timing)
    # ══════════════════════════════════════════════════════════════
    def _evaluar_distancia_ema20(
        self,
        candles_1h: List[dict],
        direccion_senal: str,
        score: int,
        razones: list,
        tipo_setup: list,
    ) -> int:
        """
        Si el precio está > 2% alejado de EMA20 en dirección del trade,
        probablemente ya se entró tarde al impulso.
        > 2%: -2 pts
        > 3%: rechazo directo (precio sobreextendido)
        Distancia < 0.5%: precio pegado a EMA20 → +1 pt (timing ideal)
        """
        if not candles_1h or len(candles_1h) < 25:
            return score

        velas = candles_1h[:-1] if len(candles_1h) > 1 else candles_1h
        closes = [c["close"] for c in velas]

        ema20_list = calc_ema(closes, 20)
        if not ema20_list:
            return score

        ema20 = ema20_list[-1]
        precio = float(velas[-1]["close"])

        if ema20 <= 0:
            return score

        distancia_pct = abs(precio - ema20) / ema20 * 100

        # Verificar si el precio está del lado correcto de EMA20
        lado_correcto = (
            (direccion_senal == "LONG"  and precio > ema20) or
            (direccion_senal == "SHORT" and precio < ema20)
        )

        if lado_correcto:
            if distancia_pct > 3.0:
                razones.append(
                    f"  🚫 Precio sobreextendido vs EMA20: {distancia_pct:.2f}% > 3% "
                    f"→ RECHAZO DIRECTO (entrada tardía)"
                )
                tipo_setup.append("SOBREEXTENDIDO_EMA20")
                return -999  # Señal de rechazo forzado al método principal
            elif distancia_pct > 2.0:
                score -= 2
                razones.append(
                    f"  ⚠️ Precio alejado de EMA20: {distancia_pct:.2f}% → -2 pts (timing tardío)"
                )
                tipo_setup.append("ALEJADO_EMA20")
            elif distancia_pct < 0.5:
                score += 1
                razones.append(
                    f"  ✅ Precio en EMA20 (dist={distancia_pct:.2f}%) → timing ideal +1 pt"
                )
                tipo_setup.append("TIMING_EMA20")
            else:
                razones.append(
                    f"  ✅ Distancia EMA20 normal ({distancia_pct:.2f}%) → 0 pts"
                )
        else:
            razones.append(
                f"  ➖ Precio al otro lado de EMA20 ({distancia_pct:.2f}%) → 0 pts (ya contemplado en otros bloques)"
            )

        return score

    # ══════════════════════════════════════════════════════════════
    # MÉTODO PRINCIPAL
    # ══════════════════════════════════════════════════════════════
    def evaluar(
        self,
        macro_4h: dict,
        adx_data: dict,
        btc_data: dict,
        candles_1h: List[dict],
        direccion_senal: str,
    ) -> dict:
        """
        Evalúa una señal en modo flexible.
        v2.1: Se prioriza el timing vía EMA20/34 + expansión + aceleración.
        EMA50 se mantiene como referencia macro/SR dinámica.
        """
        score = 0
        razones = []
        tipo_setup = []

        if not macro_4h or not macro_4h.get("direccion_permitida"):
            return {
                "ok": False,
                "score": 0,
                "decision": "RECHAZADA",
                "razones": ["Sin datos de macro 4H — no se puede evaluar en modo flexible"],
                "tipo_setup": [],
            }

        razones.append(f"  📋 Modo FLEXIBLE — {direccion_senal} | Macro 4H: {macro_4h.get('direccion_permitida', 'N/A')}")
        razones.append(f"  {'─'*50}")

        # Detectar mercado lateral con BB
        if self._detectar_lateral_bb(candles_1h, razones):
            return {
                "ok": False,
                "score": 0,
                "decision": "RECHAZADA",
                "razones": razones,
                "tipo_setup": [],
                "bonus_contratendencia": 0,
            }

        # Solo informativo, sin penalización
        if macro_4h.get("direccion_permitida") != direccion_senal:
            razones.append(f"  ⚠️ 4H en contra ({macro_4h.get('direccion_permitida')} vs {direccion_senal}) → solo informativo (flexible)")

        # ── Bloques ─────────────────────────────────────────────
        # [v2.2] Orden: timing → estructura → filtros de calidad → penalizaciones
        score = self._evaluar_rebote(macro_4h, candles_1h, direccion_senal, score, razones, tipo_setup)
        score = self._evaluar_adx_flexible(candles_1h, adx_data, direccion_senal, score, razones, tipo_setup)
        score = self._evaluar_inicio_movimiento(macro_4h, candles_1h, direccion_senal, score, razones, tipo_setup)
        score = self._evaluar_btc_suave(btc_data, direccion_senal, score, razones)
        score = self._evaluar_bb_position(candles_1h, direccion_senal, score, razones, tipo_setup)
        score = self._evaluar_cruce_y_rebote(candles_1h, direccion_senal, score, razones, tipo_setup)
        score = self._evaluar_rebote_bb_media(candles_1h, direccion_senal, score, razones, tipo_setup)
        rsi_4h = float(macro_4h.get("rsi", 0.0)) if macro_4h else 0.0
        score = self._evaluar_rsi_flexible(candles_1h, direccion_senal, score, razones, tipo_setup, rsi_4h=rsi_4h)
        score = self._evaluar_di_flexible(adx_data, direccion_senal, score, razones, tipo_setup)
        score = self._evaluar_rechazo_reciente(
            candles_1h, direccion_senal, score, razones, tipo_setup
        )

        # NUEVO BLOQUE v2.1: Expansión de tendencia (timing principal)
        score = self._evaluar_expansion_tendencia(candles_1h, adx_data, direccion_senal, score, razones, tipo_setup)
        if score == -999:
            razones.append(f"  {'─'*50}")
            razones.append(f"  🚫 RECHAZADA por explosión tardía crítica (FOMO institucional)")
            return {
                "ok": False,
                "score": 0,
                "decision": "RECHAZADA",
                "razones": razones,
                "tipo_setup": list(set(tipo_setup)),
                "bonus_contratendencia": 0,
            }

        # ── Bloques de calidad estructural [v2.2] ───────────────
        score = self._evaluar_microestructura(candles_1h, direccion_senal, score, razones, tipo_setup)
        score = self._evaluar_vela_parabolica(candles_1h, direccion_senal, score, razones, tipo_setup)

        # Bloques con posible rechazo directo (-999)
        score = self._evaluar_distancia_ema20(candles_1h, direccion_senal, score, razones, tipo_setup)
        if score == -999:
            razones.append(f"  {'─'*50}")
            razones.append(f"  🚫 RECHAZADA por rechazo directo (precio sobreextendido vs EMA20)")
            return {
                "ok": False,
                "score": 0,
                "decision": "RECHAZADA",
                "razones": razones,
                "tipo_setup": list(set(tipo_setup)),
                "bonus_contratendencia": 0,
            }

        # Rechazo directo de explosión tardía crítica también retorna -999 desde _evaluar_expansion_tendencia
        # (score ya se chequeó antes pero el return dentro del bloque 10 sí propaga -999)
        # — verificación defensiva —
        if score <= -900:
            razones.append(f"  {'─'*50}")
            razones.append(f"  🚫 RECHAZADA por rechazo directo (explosión tardía crítica)")
            return {
                "ok": False,
                "score": 0,
                "decision": "RECHAZADA",
                "razones": razones,
                "tipo_setup": list(set(tipo_setup)),
                "bonus_contratendencia": 0,
            }

        # ── Decisión final: score >= 7 = APROBADA [v2.2 subido de 5→7] ──
        if score >= 7:
            decision = "APROBADA"
        else:
            decision = "RECHAZADA"

        razones.append(f"  {'─'*50}")
        razones.append(f"  📊 Score flexible: {score} pts → {decision}")

        if tipo_setup:
            razones.append(f"  🏷️  Tipo setup: {' + '.join(sorted(set(tipo_setup)))}")

        return {
            "ok": decision != "RECHAZADA",
            "score": score,
            "decision": decision,
            "razones": razones,
            "tipo_setup": list(set(tipo_setup)),
            "bonus_contratendencia": 0,
        }