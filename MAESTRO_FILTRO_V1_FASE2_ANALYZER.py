"""
MAESTRO_FILTRO_V1_FASE2_ANALYZER.py
FASE 2 — Contexto (macro + estado de mercado + doble confirmación BTC EMA50 4H).

Debe responder: "¿vale la pena mirar esta moneda ahora mismo?"

Reglas obligatorias:
4) EMA34 4H (macro): define dirección permitida (cambiado de EMA50 a EMA34 para mayor velocidad)
5) ADX (estado mercado): debe ser >= 20–25
6) Doble confirmación BTC: EMA50 4H de BTCUSDT

CAMBIOS v2.2 (2026-05):
  - CAMBIO: EMA50 4H → EMA34 4H (detección de tendencia más rápida)
  - ELIMINADO: rechazo por 4H en contra (ahora solo informativo, flexible decides)
  - ELIMINADO: rechazo por 1H en contra (ahora solo informativo)
  - ACTUALIZADO: calc_adx ahora retorna dict con 'adx', 'di_plus', 'di_minus'

CAMBIOS v2.1 (2026-04):
  - Doble confirmación BTC: EMA50 4H de BTCUSDT.
    EMA50 4H de BTCUSDT define si el mercado crypto va LONG o SHORT.
    BTC alineado → +2 pts | BTC neutral → 0 pts | BTC contra → -1 pt.
  - ADX rechazo duro: < 20 (tendencia confirmada requerida).
"""

from __future__ import annotations

from typing import Dict, List

from analisis.indicadores import calc_adx, calc_ema, calc_sma, calc_smf, evaluar_smf_contexto
# Doble confirmación BTC: EMA50 4H de BTCUSDT


class InstitutionalAnalyzer:
    def __init__(self):
        self.nombre_archivo = "MAESTRO_FILTRO_V1_FASE2_ANALYZER.py"
        self.version = "2.2"
        self.fase = "Fase 2 - Contexto (Macro + ADX + BTC)"

    @staticmethod
    def _last_close(candles: List[dict]) -> float:
        return float(candles[-1]["close"]) if candles else 0.0

    def evaluar_tendencia_macro_4h(self, candles_4h: List[dict]) -> Dict:
        """
        CAMBIADO v2.2: usa EMA34 en lugar de EMA50 para detectar tendencia más rápido.
        """
        if not candles_4h or len(candles_4h) < 39:  # 34 + 5 de margen
            return {
                "ok": False,
                "direccion_permitida": "NEUTRAL",
                "precio": 0.0,
                "ema": 0.0,
                "mensaje": "Datos insuficientes 4H para EMA34",
            }

        velas_cerradas = candles_4h[:-1]
        closes = [c["close"] for c in velas_cerradas]

        # 🔥 CAMBIO: EMA34 en lugar de EMA50
        ema34_list = calc_ema(closes, 34)
        ema34 = float(ema34_list[-1]) if ema34_list else 0.0

        precio_cerrado = float(velas_cerradas[-1]["close"])

        if ema34 <= 0:
            return {
                "ok": False,
                "direccion_permitida": "NEUTRAL",
                "precio": precio_cerrado,
                "ema": ema34,
                "mensaje": "EMA34 inválida",
            }

        if precio_cerrado > ema34:
            direc = "LONG"
        elif precio_cerrado < ema34:
            direc = "SHORT"
        else:
            direc = "NEUTRAL"

        return {
            "ok": direc != "NEUTRAL",
            "direccion_permitida": direc,
            "precio": precio_cerrado,
            "ema": ema34,
            "mensaje": f"Macro 4H (Cerrado): precio {precio_cerrado:.4f} vs EMA34 {ema34:.4f} → {direc}",
        }

    def evaluar_estado_mercado_adx(self, candles_1h: List[dict], umbral: float = 20.0) -> Dict:
        """
        Evalúa el estado del mercado usando ADX.
        🔥 ACTUALIZADO: calc_adx ahora retorna dict con 'adx', 'di_plus', 'di_minus'
        """
        if not candles_1h or len(candles_1h) < 30:
            return {
                "ok": False,
                "adx": 0.0,
                "di_plus": 0.0,
                "di_minus": 0.0,
                "pendiente": "NEUTRAL",
                "mensaje": "Datos insuficientes 1H para ADX"
            }

        velas_cerradas_1h = candles_1h[:-1]
        adx_result = calc_adx(velas_cerradas_1h)
        adx_actual = adx_result.get('adx', 0.0)
        di_plus = adx_result.get('di_plus', 0.0)
        di_minus = adx_result.get('di_minus', 0.0)
        
        # Pendiente: comparar ADX actual vs ADX de hace 4 velas
        adx_anterior_result = calc_adx(velas_cerradas_1h[:-4]) if len(velas_cerradas_1h) >= 18 else adx_result
        adx_anterior = adx_anterior_result.get('adx', adx_actual) if isinstance(adx_anterior_result, dict) else adx_anterior_result
        
        if adx_actual > adx_anterior + 0.5:
            pendiente = "SUBIENDO"    # tendencia acelerando ✅
        elif adx_actual < adx_anterior - 0.5:
            pendiente = "BAJANDO"     # tendencia debilitándose ⚠️
        else:
            pendiente = "NEUTRAL"     # estable

        ok = adx_actual >= umbral
        return {
            "ok": ok,
            "adx": adx_actual,
            "di_plus": di_plus,
            "di_minus": di_minus,
            "pendiente": pendiente,
            "mensaje": f"ADX 1H={adx_actual:.2f} (umbral {umbral}) | pendiente: {pendiente} | +DI={di_plus:.2f} -DI={di_minus:.2f}",
        }

    async def evaluar_tendencia_btc(self, session) -> Dict:
        """
        Doble confirmación BTC: verifica EMA50 4H de BTCUSDT.
        Si BTC está sobre su EMA50 4H → mercado crypto alcista (LONG).
        Si BTC está bajo  su EMA50 4H → mercado crypto bajista (SHORT).
        Usa acción del precio real (EMA50 4H de BTCUSDT).
        """
        from analisis.indicadores import get_klines, calc_ema
        try:
            candles_btc = await get_klines(session, "BTCUSDT", "4h", 60)
            if not candles_btc or len(candles_btc) < 55:
                return {"direccion_btc": "NEUTRAL", "mensaje": "BTC: datos insuficientes"}
            
            velas_cerradas = candles_btc[:-1]
            closes_btc = [c["close"] for c in velas_cerradas]
            ema50_list = calc_ema(closes_btc, 50)
            if not ema50_list:
                return {"direccion_btc": "NEUTRAL", "mensaje": "BTC: EMA50 no calculable"}
            
            ema50_btc  = float(ema50_list[-1])
            precio_btc = float(velas_cerradas[-1]["close"])
            
            if precio_btc > ema50_btc:
                direccion = "LONG"
            elif precio_btc < ema50_btc:
                direccion = "SHORT"
            else:
                direccion = "NEUTRAL"
            
            return {
                "direccion_btc": direccion,
                "precio_btc":    precio_btc,
                "ema50_btc":     ema50_btc,
                "mensaje": f"BTC 4H: precio {precio_btc:.0f} vs EMA50 {ema50_btc:.0f} → {direccion}"
            }
        except Exception as e:
            return {"direccion_btc": "NEUTRAL", "mensaje": f"BTC: error ({e})"}

    def obtener_direccion_permitida(self, candles_4h: List[dict]) -> Dict:
        macro = self.evaluar_tendencia_macro_4h(candles_4h)

        if not macro["ok"]:
            return {
                'direccion_permitida': 'NEUTRAL',
                'precio_actual': macro.get('precio', 0),
                'ema': macro.get('ema', 0),
                'mensaje': macro.get('mensaje', 'Datos insuficientes')
            }

        return {
            'direccion_permitida': macro['direccion_permitida'],
            'precio_actual': macro['precio'],
            'ema': macro['ema'],
            'mensaje': macro['mensaje']
        }

    def verificar_mercado_operable(self, candles_1h: List[dict], umbral: float = 20.0) -> Dict:
        adx_data = self.evaluar_estado_mercado_adx(candles_1h, umbral=umbral)

        return {
            'operable': adx_data['ok'],
            'adx': adx_data['adx'],
            'mensaje': adx_data['mensaje']
        }

    def analizar_entorno_con_candles(
        self,
        candles_1h: List[dict],
        candles_4h: List[dict]
    ) -> Dict:
        adx_check = self.verificar_mercado_operable(candles_1h, umbral=20.0)
        direccion = self.obtener_direccion_permitida(candles_4h)

        return {
            'mercado_operable': adx_check['operable'],
            'adx_1h': adx_check['adx'],
            'mensaje_adx': adx_check['mensaje'],
            'direccion_permitida': direccion['direccion_permitida'],
            'precio_actual_4h': direccion['precio_actual'],
            'ema_4h': direccion['ema'],
            'mensaje_macro': direccion['mensaje']
        }

    # ============================================================
    # Validación de fuerza local con EMA 50 + SMA 36
    # ============================================================
    def _validar_fuerza_local(
        self,
        candles_1h: List[dict],
        direccion_senal: str,
    ) -> tuple[bool, int, str]:
        """
        Valida la fuerza local en 1H usando:
        - EMA 50: OBLIGATORIO (si no la respeta → rechazo)
        - SMA 36: BONUS (si también la respeta → +2 puntos)

        Retorna: (es_valida, bonus, mensaje)
        """
        if len(candles_1h) < 56:
            return False, 0, "Datos insuficientes para EMA 50 en 1H (necesita >= 56)"

        velas_cerradas = candles_1h[:-1]
        closes = [c["close"] for c in velas_cerradas]

        ema_50_list = calc_ema(closes, 50)
        if not ema_50_list:
            return False, 0, "Error calculando EMA 50"

        ema_50 = ema_50_list[-1]
        precio_actual = velas_cerradas[-1]["close"]

        if direccion_senal == "LONG":
            if precio_actual <= ema_50:
                return False, 0, f"LONG débil: precio {precio_actual:.6f} <= EMA50 {ema_50:.6f}"
        else:
            if precio_actual >= ema_50:
                return False, 0, f"SHORT débil: precio {precio_actual:.6f} >= EMA50 {ema_50:.6f}"

        bonus = 0
        mensaje_bonus = ""

        if len(closes) >= 36:
            sma_36_list = calc_sma(closes, 36)
            if sma_36_list:
                sma_36 = sma_36_list[-1] if sma_36_list[-1] is not None else 0

                if sma_36 > 0:
                    if direccion_senal == "LONG":
                        if precio_actual > sma_36:
                            bonus = 2
                            mensaje_bonus = f" + bonus SMA36 (precio > SMA36)"
                        else:
                            mensaje_bonus = f" (sin bonus: precio <= SMA36)"
                    else:
                        if precio_actual < sma_36:
                            bonus = 2
                            mensaje_bonus = f" + bonus SMA36 (precio < SMA36)"
                        else:
                            mensaje_bonus = f" (sin bonus: precio >= SMA36)"

        return True, bonus, f"✅ Fuerza local válida (respeta EMA50){mensaje_bonus}"

    # ============================================================
    # Doble confirmación BTC — EMA50 4H de BTCUSDT
    # ============================================================
    def _calcular_bonus_btc(self, direccion_btc: str, direccion_senal: str) -> tuple[int, str]:
        """
        Bonus/penalización según EMA50 4H de BTC vs dirección de la señal.
        Acción del precio real — EMA50 4H de BTCUSDT.

        BTC alineado con señal  → +2 pts (mercado crypto confirma dirección)
        BTC neutral (lateral)   →  0 pts (sin confirmación adicional)
        BTC contra la señal     → -1 pt  (mercado crypto va en contra)
        """
        if direccion_btc == "NEUTRAL":
            return -2, "BTC lateral/neutral (EMA50 4H) → -2 pts ⚠️ sin dirección de mercado"
        elif direccion_btc == direccion_senal:
            return 2, f"BTC alineado ({direccion_btc}) con señal {direccion_senal} → +2 pts ✅"
        else:
            return -3, f"BTC en {direccion_btc} vs señal {direccion_senal} → -3 pts ❌ contra la corriente"

    async def analizar_contexto(
        self,
        session,
        symbol: str,
        direccion_senal: str,
        candles_4h: List[dict],
        candles_1h: List[dict],
        adx_umbral: float = 20.0,
    ) -> Dict:
        """
        Orden de análisis:

        PASO 1: EMA34 4H → tendencia macro (LONG/SHORT).
        PASO 2: EMA50 1H → fuerza local.
        PASO 3: ADX 1H → momentum.
        PASO 4: SMF → flujo de dinero.
        PASO 5: Doble confirmación BTC → bonus/penalización.
        PASO 6: Veredicto.
        
        🔥 CAMBIOS v2.2:
        - Ya NO rechaza por 4H en contra (solo informativo, pasa al flexible)
        - Ya NO rechaza por 1H en contra (solo informativo)
        """
        # ── PASO 1: Tendencia macro 4H (EMA34) ──────────────────────────────
        macro = self.evaluar_tendencia_macro_4h(candles_4h)
        if not macro["ok"]:
            return {
                "ok": False,
                "razon": macro["mensaje"],
                "macro": macro,
                "adx": {"ok": False, "adx": 0.0, "mensaje": ""},
                "btc": {"direccion_btc": "NEUTRAL", "mensaje": ""},
            }

        # 🔥 YA NO RECHAZA POR 4H EN CONTRA (solo informativo)
        es_contratendencia_4h = (
            macro["direccion_permitida"] != "NEUTRAL"
            and direccion_senal != macro["direccion_permitida"]
        )

        # ── PASO 2: EMA50 1H — fuerza local ────────────────────────────────
        es_valida_1h, bonus_sma, mensaje_local = self._validar_fuerza_local(
            candles_1h, direccion_senal
        )
        # 🔥 YA NO RECHAZA POR 1H EN CONTRA (solo informativo)
        es_contratendencia_1h = not es_valida_1h

        # ── PASO 3: ADX 1H — momentum ───────────────────────────────────────
        adx = self.evaluar_estado_mercado_adx(candles_1h, umbral=adx_umbral)
        adx_val = adx.get("adx", 0)

        if adx_val < 19.5:
            return {
                "ok": False,
                "razon": f"Mercado lateral (ADX={adx_val:.1f} < 19.5) — sin tendencia confirmada",
                "macro": macro,
                "adx": adx,
                "btc": {"direccion_btc": "NEUTRAL", "mensaje": ""},
            }

        penalizacion_adx = -1 if (20 <= adx_val < adx_umbral) else 0
        if penalizacion_adx:
            adx["mensaje"] += f" (tendencia débil — penalización {penalizacion_adx})"

        # Penalización adicional si ADX está bajando (tendencia debilitándose)
        pendiente_adx = adx.get("pendiente", "NEUTRAL")
        penalizacion_pendiente = -1 if pendiente_adx == "BAJANDO" else 0
        if penalizacion_pendiente:
            adx["mensaje"] += f" ↘️ cayendo — penalización adicional {penalizacion_pendiente}"

        # ── PASO 4: SMF Cloud — flujo de dinero real ────────────────────────
        velas_smf = candles_1h[:-1] if len(candles_1h) > 1 else candles_1h
        smf_data = calc_smf(velas_smf)
        _, penalizacion_smf, msg_smf = evaluar_smf_contexto(smf_data, direccion_senal)

        # ── PASO 5: Doble confirmación BTC EMA50 4H ────────────────────────
        btc = await self.evaluar_tendencia_btc(session)
        dir_btc = btc.get("direccion_btc", "NEUTRAL")
        bonus_corr, mensaje_corr = self._calcular_bonus_btc(dir_btc, direccion_senal)

        # ── PASO 6: Veredicto ───────────────────────────────────────────────
        # 🔥 Ya NO rechaza por 4H/1H en contra. Solo suma penalizaciones.
        
        # Construir mensaje informativo sobre contratendencias
        advertencias = []
        if es_contratendencia_4h:
            advertencias.append(f"4H en contra ({macro['direccion_permitida']} vs {direccion_senal})")
        if es_contratendencia_1h:
            advertencias.append(f"1H en contra (no respeta EMA50)")
        
        razon_base = f"✅ Contexto evaluado"
        if advertencias:
            razon_base = f"⚠️ {', '.join(advertencias)} — señal pasa al flexible"
        
        # Calcular bonus total
        bonus_base = bonus_sma + bonus_corr + penalizacion_smf + penalizacion_adx + penalizacion_pendiente
        bonus_contratendencia = bonus_base
        
        razon_completa = f"{razon_base} | {mensaje_local} | {mensaje_corr}"

        return {
            "ok": True,  # 🔥 AHORA SIEMPRE True si pasó ADX (sin importar 4H/1H)
            "razon": razon_completa,
            "macro": macro,
            "adx": adx,
            "btc": btc,
            "smf": smf_data,
            "es_excepcion": False,
            "es_contratendencia_4h": es_contratendencia_4h,
            "es_contratendencia_1h": es_contratendencia_1h,
            "bonus_contratendencia": bonus_contratendencia,
        }