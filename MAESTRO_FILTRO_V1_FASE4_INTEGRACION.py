"""
================================================================================
NOMBRE DEL ARCHIVO: MAESTRO_FILTRO_V1_FASE4_INTEGRACION.py
================================================================================
SISTEMA: Filtro Maestro Multiframe Institucional
VERSIÓN: 2.7
FASE: 4 - Integración Total (Orquestador para dinero real)

CAMBIOS v2.7 (2026-05):
  - CORRECCIÓN: Rechazos por modo FLEXIBLE ahora incluyen 'modo_filtro': 'FLEXIBLE'
    para que signal_logger los guarde en signals_rechazadas_flexible.txt
  - _rechazo() ahora acepta parámetro modo_filtro (default 'ESTRICTO')
  - Llamadas a _rechazo() cuando flexible rechaza ahora pasan modo_filtro='FLEXIBLE'

CAMBIOS v2.6 (2026-05):
  - CORRECCIÓN: En modo FLEXIBLE post-Fase 3, ahora usa modo_filtro = 'FLEXIBLE'
    (antes usaba 'FLEXIBLE_POST_F3', lo que causaba que adaptive_filter lo bloqueara)
    Ahora adaptive_filter reconoce 'FLEXIBLE' y deja pasar la señal.

CAMBIOS v2.5 (2026-05):
  - MODO FLEXIBLE ahora se ejecuta también cuando Fase 3 rechaza.
    Antes solo se ejecutaba si Fase 2 rechazaba.
    Ahora: cualquier rechazo del estricto (Fase 2 o Fase 3) activa el flexible.
    Esto permite capturar señales con SMF 4H opuesto, score bajo, etc.

CAMBIOS v2.4 (2026-05):
  - MODO FLEXIBLE: cuando el flexible aprueba, ACEPTA DIRECTAMENTE sin pasar por Fase 3.
    El flexible ya tiene su propio sistema de puntuación (REBOTE +5, INICIO +3, etc.)
    Forzar que pase por Fase 3 (que exige 13 puntos) mataba señales flexibles legítimas.
    Ahora: Flexible aprueba → retorna ACEPTADA inmediatamente.

CAMBIOS v2.3 (2026-05):
  - MODO FLEXIBLE: cuando el modo estricto rechaza, se activa FlexibleAnalyzer.
    Cubre 3 escenarios: rebote en tendencia, ADX bajo operable, inicio de movimiento.
    Si el flexible aprueba → señal etiquetada como FLEXIBLE en el log.
    Si el flexible también rechaza → rechazo total.
    El scoring de Fase 3 corre igual en ambos modos.
    El flexible NO genera bonus_contratendencia (siempre 0).
    Los campos modo_filtro, flexible_score, flexible_setup, flexible_razones
    se propagan al resultado para trazabilidad en signal_logger.

CAMBIOS v2.2 (2026-04):
  - tf_minutos derivado de temporalidad y propagado a evaluar_senal → puntuar_confluencia
    → calc_sr_score. El bloque S/R usa panorama y ventana de rebote calibrados por TF:
      · 15M → panorama 200 velas (~50h),  rebote últimas 12 velas (~3h),  ruptura últimas 24 velas
      · 1H  → panorama 100 velas (~100h), rebote últimas 8  velas (~8h),  ruptura últimas 16 velas
      · 4H  → panorama 60  velas (~240h), rebote últimas 6  velas (~24h), ruptura últimas 12 velas
    Cada TF ve el contexto natural que necesita — no una equivalencia matemática
    arbitraria, sino el panorama donde ese TF identifica S/R y rebotes reales.

CAMBIOS v2.1 (2026-04):
  - ELIMINADO: bonus_botai y quality_score del scoring.
    El BOTAI es el generador de señales (symbol/side/entry/stop/tps).
    El filtro evalúa la señal de forma puramente técnica.
    quality_score ya NO se traduce a puntos de ningún tipo.

  - ELIMINADO: level_price como bonus de puntos.
    level_price sigue llegando a Fase 3 pero solo como referencia
    visual en el resumen (el scoring no le suma pts adicionales).

  - bonus_total ahora = bonus_contratendencia (Fase 2) ÚNICAMENTE.
    Rango: -2 a +4 (puramente técnico: SMA36 + doble confirmación BTC EMA50 4H + ADX).

  - Se mantiene el log completo de quality_score para trazabilidad,
    pero queda explícito que no afecta el veredicto.
================================================================================
"""

import json
import os
from datetime import datetime

from MAESTRO_FILTRO_V1_FASE1_BITUNIX import DataInfrastructure
from MAESTRO_FILTRO_V1_FASE2_ANALYZER import InstitutionalAnalyzer
from MAESTRO_FILTRO_V1_FASE2_FLEXIBLE import FlexibleAnalyzer
from MAESTRO_FILTRO_V1_FASE3_SCORING import InstitutionalScoring


class FiltroMaestroIntegrado:
    def __init__(self, symbol: str = "BTCUSDT", log_dir: str = "logs"):
        self.symbol = symbol
        self.nombre_archivo = "MAESTRO_FILTRO_V1_FASE4_INTEGRACION.py"
        self.version = "2.7"
        self.fase = "Fase 4 - Integración"

        os.makedirs(log_dir, exist_ok=True)
        self.log_dir = log_dir

        self.datos = DataInfrastructure(symbol=self.symbol)
        self.analizador = InstitutionalAnalyzer()
        self.flexible  = FlexibleAnalyzer()
        self.scoring = InstitutionalScoring()

        self.ultimo_contexto = None
        self.ultimas_candles = {}

        print(f"\n[INICIANDO] {self.nombre_archivo} v{self.version}")
        print(f"[MODO] DINERO REAL - SIN SIMULACIONES")
        print(f"[SCORING] 100% TÉCNICO — Sin bonus BOTAI")
        print(f"[SYMBOL] {self.symbol}")
        print(f"[LOG DIR] {self.log_dir}\n")

    def _guardar_log(self, prefix: str, payload: dict):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        path = os.path.join(self.log_dir, f"{prefix}_{ts}.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, default=str)
        except Exception:
            pass

    async def obtener_datos(self, session):
        """Obtiene velas de todas las temporalidades necesarias."""
        print(f"\n[ACTUALIZACIÓN] Obteniendo datos para {self.symbol}...")

        frames = await self.datos.obtener_multiframe(
            session,
            self.symbol,
            frames={"4h": 120, "1h": 120, "15m": 200, "1d": 60}
        )

        self.ultimas_candles = frames
        self.ultima_actualizacion = datetime.now()

        print(f"  ✅ Datos obtenidos: {list(frames.keys())}")
        return frames

    async def analizar_contexto(self, session, direccion_senal: str, adx_umbral: float = 20.0) -> dict:
        """Analiza el contexto del mercado usando Fase 2."""
        print("\n[ANÁLISIS] Procesando contexto institucional...")

        if "4h" not in self.ultimas_candles or "1h" not in self.ultimas_candles:
            return {
                "ok": False,
                "razon": "No hay datos disponibles. Ejecute obtener_datos() primero."
            }

        candles_4h = self.ultimas_candles["4h"].candles
        candles_1h = self.ultimas_candles["1h"].candles

        contexto = await self.analizador.analizar_contexto(
            session=session,
            symbol=self.symbol,
            direccion_senal=direccion_senal,
            candles_4h=candles_4h,
            candles_1h=candles_1h,
            adx_umbral=adx_umbral
        )

        self.ultimo_contexto = contexto
        self._guardar_log("contexto", contexto)

        return contexto

    async def recibir_senal_externa(
        self,
        session,
        symbol: str,
        direccion: str,
        precio_entrada: float,
        stop: float,
        tps_list: list,
        temporalidad: str = "1h",
        quality_score: int = 0,
        metrics: dict = None,
    ) -> dict:
        """
        ⚠️ MÉTODO PRINCIPAL PARA DINERO REAL ⚠️
        """
        print("\n" + "🔴" * 35)
        print(f"[SEÑAL REAL] {symbol} {direccion} a {precio_entrada}")
        if quality_score:
            print(f"[QS={quality_score}] (informativo — no afecta scoring)")
        print("🔴" * 35)

        # Validaciones básicas
        if direccion not in ['LONG', 'SHORT']:
            return self._rechazo(f"Dirección inválida: {direccion}")

        if precio_entrada <= 0 or stop <= 0:
            return self._rechazo("Entry/Stop inválidos")

        if abs(precio_entrada - stop) < 1e-10:
            return self._rechazo(
                f"Entry ({precio_entrada}) igual a Stop ({stop}) — señal corrupta del BOTAI"
            )

        if symbol != self.symbol:
            self.symbol = symbol
            self.datos.symbol = symbol
            self.ultimas_candles = {}

        await self.obtener_datos(session)

        contexto = await self.analizar_contexto(session, direccion, adx_umbral=20.0)

        # ─────────────────────────────────────────────────────────────────────
        # Helper para ejecutar el flexible (evita duplicar código)
        # ─────────────────────────────────────────────────────────────────────
        async def evaluar_flexible():
            candles_1h_flex = self.ultimas_candles.get("1h")
            candles_1h_flex = candles_1h_flex.candles[:-1] if candles_1h_flex and candles_1h_flex.candles else []

            resultado = self.flexible.evaluar(
                macro_4h=contexto.get("macro", {}),
                adx_data=contexto.get("adx", {}),
                btc_data=contexto.get("btc", {}),
                candles_1h=candles_1h_flex,
                direccion_senal=direccion,
            )
            return resultado

        # ─────────────────────────────────────────────────────────────────────
        # PRIMERA OPORTUNIDAD: Fase 2 rechazó → flexible
        # ─────────────────────────────────────────────────────────────────────
        if not contexto.get("ok", False):
            razon_estricta = contexto.get("razon", "Contexto de mercado no válido")
            print(f"  ⚠️  [{symbol}] Estricto (Fase 2) rechazó: {razon_estricta}")
            print(f"  🔄 [{symbol}] Evaluando modo FLEXIBLE...")

            resultado_flexible = await evaluar_flexible()

            if not resultado_flexible.get("ok", False):
                print(f"  ❌ [{symbol}] Flexible también rechazó — rechazo total")
                # ✅ CORRECCIÓN v2.7: pasar modo_filtro='FLEXIBLE'
                return self._rechazo(razon_estricta, modo_filtro='FLEXIBLE')

            # Flexible aprobó → aceptar directamente
            tipo_setup = resultado_flexible.get("tipo_setup", [])
            score_flex = resultado_flexible.get("score", 0)
            razones_flex = resultado_flexible.get("razones", [])
            setup_str = " + ".join(tipo_setup) if tipo_setup else "SIN_SETUP"

            print(f"  ✅ [{symbol}] Flexible APROBADO — setup={tipo_setup} score={score_flex}")

            self._guardar_log("senal_flexible", {
                'symbol': symbol,
                'direccion': direccion,
                'precio_entrada': precio_entrada,
                'stop': stop,
                'tps_list': tps_list,
                'temporalidad': temporalidad,
                'quality_score_botai': quality_score,
                'flexible_score': score_flex,
                'flexible_setup': tipo_setup,
                'flexible_razones': razones_flex,
            })

            return {
                'aceptada': True,
                'veredicto': 'APROBADA_FLEXIBLE',
                'puntaje': score_flex,
                'detalle_puntajes': {
                    'modo': 'FLEXIBLE',
                    'flexible_score': score_flex,
                    'flexible_setup': tipo_setup,
                },
                'mensaje': f"✅ APROBADA por modo FLEXIBLE | setup: {setup_str} | score flexible: {score_flex}",
                'razon_rechazo': None,
                'timestamp': datetime.now().isoformat(),
                'multiplicador_tamaño': 1.0,
                'modo_filtro': 'FLEXIBLE',
                'flexible_score': score_flex,
                'flexible_setup': tipo_setup,
                'flexible_razones': razones_flex,
            }

        # ─────────────────────────────────────────────────────────────────────
        # MODO ESTRICTO: Fase 2 aprobó → evaluamos con scoring completo
        # ─────────────────────────────────────────────────────────────────────
        contexto["modo_flexible"] = False

        tf_key = "15m" if temporalidad == "15m" else "1h"

        candles_entrada_pack = self.ultimas_candles.get(tf_key)
        if not candles_entrada_pack or not candles_entrada_pack.candles or len(candles_entrada_pack.candles) < 30:
            return self._rechazo(f"Datos insuficientes en {temporalidad}")

        candles_1h_pack = self.ultimas_candles.get("1h")
        if not candles_1h_pack or not candles_1h_pack.candles or len(candles_1h_pack.candles) < 30:
            return self._rechazo("Datos insuficientes en 1H para scoring")

        candles_1h = candles_1h_pack.candles[:-1]

        candles_4h_pack = self.ultimas_candles.get("4h")
        candles_4h = candles_4h_pack.candles[:-1] if candles_4h_pack and candles_4h_pack.candles else None

        candles_1d_pack = self.ultimas_candles.get("1d")
        candles_1d = candles_1d_pack.candles[:-1] if candles_1d_pack and candles_1d_pack.candles else None

        level_price = 0.0
        if metrics:
            level_info = metrics.get('level_info', {}) or {}
            level_price = float(level_info.get('price', 0) or 0)

        bonus_total = contexto.get("bonus_contratendencia", 0)
        bonus_total = max(min(bonus_total, 4), -3)

        _tf_map = {"15m": 15, "1h": 60, "4h": 240}
        tf_minutos = _tf_map.get(temporalidad.lower(), 60)

        resultado = self.scoring.evaluar_senal(
            side=direccion,
            entry=precio_entrada,
            stop=stop,
            tps_list=tps_list,
            candles_1h=candles_1h,
            candles_4h=candles_4h,
            candles_1d=candles_1d,
            contexto=contexto,
            rr_min=0.8,
            bonus_externo=bonus_total,
            level_price=level_price,
            tf_minutos=tf_minutos,
        )

        # ─────────────────────────────────────────────────────────────────────
        # SEGUNDA OPORTUNIDAD: Si Fase 3 rechazó → evaluamos flexible también
        # ─────────────────────────────────────────────────────────────────────
        if not resultado.get("aceptada", False):
            print(f"  ⚠️  [{symbol}] Fase 3 rechazó: {resultado.get('razon_rechazo', 'Score insuficiente')}")
            print(f"  🔄 [{symbol}] Evaluando modo FLEXIBLE (post-Fase 3)...")

            resultado_flexible = await evaluar_flexible()

            if resultado_flexible.get("ok", False):
                tipo_setup = resultado_flexible.get("tipo_setup", [])
                score_flex = resultado_flexible.get("score", 0)
                razones_flex = resultado_flexible.get("razones", [])
                setup_str = " + ".join(tipo_setup) if tipo_setup else "SIN_SETUP"

                print(f"  ✅ [{symbol}] Flexible APROBADO (post-Fase 3) — setup={tipo_setup} score={score_flex}")

                self._guardar_log("senal_flexible_post_f3", {
                    'symbol': symbol,
                    'direccion': direccion,
                    'precio_entrada': precio_entrada,
                    'stop': stop,
                    'tps_list': tps_list,
                    'temporalidad': temporalidad,
                    'quality_score_botai': quality_score,
                    'flexible_score': score_flex,
                    'flexible_setup': tipo_setup,
                    'flexible_razones': razones_flex,
                    'fase3_rechazo': resultado.get('razon_rechazo', ''),
                })

                # 🔥 CORRECCIÓN v2.6: usar 'FLEXIBLE' consistentemente
                return {
                    'aceptada': True,
                    'veredicto': 'APROBADA_FLEXIBLE',
                    'puntaje': score_flex,
                    'detalle_puntajes': {
                        'modo': 'FLEXIBLE',
                        'flexible_score': score_flex,
                        'flexible_setup': tipo_setup,
                        'fase3_resultado': resultado.get('detalle_puntajes', {}),
                    },
                    'mensaje': f"✅ APROBADA por modo FLEXIBLE (post-Fase 3) | setup: {setup_str} | score flexible: {score_flex}",
                    'razon_rechazo': None,
                    'timestamp': datetime.now().isoformat(),
                    'multiplicador_tamaño': 1.0,
                    'modo_filtro': 'FLEXIBLE',  # ← AHORA SIEMPRE 'FLEXIBLE'
                    'flexible_score': score_flex,
                    'flexible_setup': tipo_setup,
                    'flexible_razones': razones_flex,
                }
            else:
                print(f"  ❌ [{symbol}] Flexible también rechazó (post-Fase 3) — rechazo total")

        # Log completo
        self._guardar_log("senal", {
            'symbol': symbol,
            'direccion': direccion,
            'precio_entrada': precio_entrada,
            'stop': stop,
            'tps_list': tps_list,
            'temporalidad': temporalidad,
            'quality_score_botai': quality_score,
            'bonus_botai': 0,
            'bonus_contratendencia': bonus_total,
            'bonus_total': bonus_total,
            'resultado': resultado,
        })

        return {
            'aceptada': resultado.get('aceptada', False),
            'veredicto': resultado.get('veredicto', 'RECHAZO'),
            'puntaje': resultado.get('puntaje', 0),
            'detalle_puntajes': resultado.get('detalle_puntajes', {}),
            'mensaje': resultado.get('mensaje', ''),
            'razon_rechazo': resultado.get('razon_rechazo'),
            'timestamp': datetime.now().isoformat(),
            'multiplicador_tamaño': 1.0 if resultado.get('aceptada') else 0.0,
            'modo_filtro': 'ESTRICTO',
            'flexible_score': None,
            'flexible_setup': [],
            'flexible_razones': [],
        }

    # ✅ CORRECCIÓN v2.7: _rechazo ahora acepta modo_filtro como parámetro
    def _rechazo(self, razon: str, modo_filtro: str = 'ESTRICTO') -> dict:
        return {
            'aceptada': False,
            'veredicto': 'RECHAZO',
            'puntaje': 0,
            'detalle_puntajes': {},
            'mensaje': f"❌ {razon}",
            'razon_rechazo': razon,
            'timestamp': datetime.now().isoformat(),
            'multiplicador_tamaño': 0.0,
            'modo_filtro': modo_filtro,  # ← AGREGADO
            'flexible_score': None,
            'flexible_setup': [],
            'flexible_razones': [],
        }