"""
MAESTRO_FILTRO_V1_FASE1_BITUNIX.py
FASE 1 — Infraestructura de Datos (solo OHLCV, sin análisis).

Regla del proyecto (2026-04-23):
- NO usar `analisis_tecnico` ni módulos viejos.
- El único "núcleo" permitido para indicadores/volumen es:
  `analisis/indicadores.py` y `analisis/volumen_btc.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from analisis.indicadores import get_klines


@dataclass
class VelasPack:
    symbol: str
    tf: str
    candles: List[dict]


class DataInfrastructure:
    """
    Descarga y entrega velas normalizadas (lista de dicts).
    No hace scoring. No decide. Solo datos.
    """

    def __init__(self, symbol: str = "BTCUSDT"):
        """
        Args:
            symbol: Par de trading (ej: 'BTCUSDT', 'ETHUSDT')
        """
        self.symbol = symbol
        self.nombre_archivo = "MAESTRO_FILTRO_V1_FASE1_BITUNIX.py"
        self.version = "2.1"
        self.fase = "Fase 1 - Infraestructura de Datos (OHLCV)"

    async def obtener_velas(
        self,
        session,
        symbol: str,
        timeframe: str,
        limit: int = 100,
    ) -> VelasPack:
        candles = await get_klines(session, symbol, timeframe, limit)
        return VelasPack(symbol=symbol, tf=timeframe, candles=candles or [])

    async def obtener_multiframe(
        self,
        session,
        symbol: str,
        frames: Optional[Dict[str, int]] = None,
    ) -> Dict[str, VelasPack]:
        frames = frames or {"4h": 120, "1h": 120, "15m": 200, "1d": 60}
        out: Dict[str, VelasPack] = {}
        for tf, lim in frames.items():
            out[tf] = await self.obtener_velas(session, symbol, tf, lim)
        return out