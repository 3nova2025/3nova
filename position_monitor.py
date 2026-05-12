"""
position_monitor.py — Dashboard live con barras SL y TP independientes
LÓGICA SL:
Cuando TP1 se ejecuta (orden limit tp1_id desaparece de pendientes) → modify_sl modifica SL al precio de entrada (breakeven)
Cuando precio avanza 70% de TP1 a TP3 → modify_sl modifica SL al precio de TP1
modify_sl llama POST /tpsl/position/modify_order — modifica el valor del SL existente, no crea uno nuevo
Los TPs son órdenes LIMIT independientes — nunca se tocan
"""
import asyncio
import aiohttp
import time
import sys
import re
import os
from bitunix_api import (
    get_open_position, get_balance, print_balance, get_ticker,
    cancel_order_by_id,
    place_partial_tpsl,
    get_active_tpsl_orders, get_all_open_positions,
    flash_close, _get,
    TP1_RATIO,
    modify_sl,
    calc_tps,
)
from recovery import save_state, delete_state_entry
from capital_manager import update_capital, clean_expired_blocks
from estadisticas import registrar_operacion, mostrar_estadisticas
from time_filters import time_filter
import unicodedata
from console_util import safe_print
CLEAR_SCREEN = os.getenv("CLEAR_SCREEN", "0").strip() == "1"
def _visual_len(s: str) -> int:
    ansi_escape = re.compile(r'\x1b[[0-9;]*m')
    s_clean = ansi_escape.sub('', s)
    width = 0
    for ch in s_clean:
        eaw = unicodedata.east_asian_width(ch)
        if eaw in ('W', 'F'):
            width += 2
        else:
            cp = ord(ch)
            if (0x1F300 <= cp <= 0x1FAFF) or (0x2600 <= cp <= 0x27BF) or (0x1F000 <= cp <= 0x1F02F):
                width += 2
            else:
                width += 1
    return width
try:
    from adaptive_filter import adaptive_filter
    _ADAPTIVE_AVAILABLE = True
except ImportError:
    _ADAPTIVE_AVAILABLE = False
POLL_INTERVAL = 5
def _clr(t, c):  return f"\033[{c}m{t}\033[0m"
def green(t):    return _clr(t, '92')
def red(t):      return _clr(t, '91')
def yellow(t):   return _clr(t, '93')
def cyan(t):     return _clr(t, '96')
def bold(t):     return _clr(t, '1')
def dim(t):      return _clr(t, '2')
def _pnl_str(pnl):
    if pnl > 0:  return green(f"+${pnl:.4f}")
    if pnl < 0:  return red(f"-${abs(pnl):.4f}")
    return dim("$0.0000")
def _bar_tp(pct_avance, width=22):
    filled = min(int(pct_avance / 100 * width), width)
    return green('█' * filled) + dim('░' * (width - filled))
def _bar_sl(pct_peligro, width=22):
    filled = min(int(pct_peligro / 100 * width), width)
    if pct_peligro > 70:   color = red
    elif pct_peligro > 40: color = yellow
    else:                  color = dim
    return color('█' * filled) + dim('░' * (width - filled))
def _sl_estado(pct):
    if pct > 80: return red("⚠️  PELIGRO CRÍTICO")
    if pct > 50: return yellow("⚠️  ALERTA")
    if pct > 25: return yellow("ALERTA MEDIA")
    return green("ZONA SEGURA")
def _clear_lines(n):
    if not CLEAR_SCREEN:
        return
    if os.name == 'nt':
        os.system('cls')
    else:
        for _ in range(n):
            sys.stdout.write('\x1b[1A\x1b[2K')
            sys.stdout.flush()

# ══════════════════════════════════════════════════════════════
# SLManager — único responsable de modificar el SL
# ══════════════════════════════════════════════════════════════
class SLManager:
    def __init__(self):
        self._last_modification: dict = {}
        self._last_sl_value: dict     = {}
        self._cooldown_seconds        = 3

    async def modify_sl_to_breakeven(self, session, key: str, pos: dict) -> tuple[bool, str]:
        """
        TP1 ejecutado → modifica SL al precio de entrada (breakeven)
        """
        if pos.get('sl_modified_to_breakeven', False):
            return False, "ya modificado a breakeven"

        entry      = pos['entry']
        current_sl = pos['stop']
        side       = pos['side']
        price_prec = pos['price_prec']
        nuevo_sl   = round(entry, price_prec)

        if side  == 'LONG' and nuevo_sl  <= current_sl:
            return False, f"no mejora (long): {nuevo_sl} <= {current_sl}"
        if side == 'SHORT' and nuevo_sl  >= current_sl:
            return False, f"no mejora (short): {nuevo_sl} >= {current_sl}"

        ok = await self._ejecutar(session, key, pos, nuevo_sl)
        if ok:
            pos['sl_modified_to_breakeven'] = True
            safe_print(f"  ✅ [{pos['symbol']}] SL → BREAKEVEN: {nuevo_sl:.{price_prec}f} "
                       f"(entrada: {entry:.{price_prec}f})")
            return True, "breakeven"
        return False, "modificacion falló"

    async def modify_sl_to_tp1(self, session, key: str, pos: dict, tp1_price: float) -> tuple[bool, str]:
        """
        70% recorrido TP1→TP3 → modifica SL al precio que tenía TP1
        """
        if pos.get('sl_modified_to_tp1', False):
            return False, "ya modificado a TP1"
        if not pos.get('sl_modified_to_breakeven', False):
            return False, "SL no está en breakeven aún"

        current_sl = pos['stop']
        side       = pos['side']
        price_prec = pos['price_prec']
        nuevo_sl   = round(tp1_price, price_prec)

        if side == 'LONG' and nuevo_sl  <= current_sl:
            return False, f"no mejora (long): {nuevo_sl} <= {current_sl}"
        if side == 'SHORT' and nuevo_sl  >= current_sl:
            return False, f"no mejora (short): {nuevo_sl} >= {current_sl}"

        ok = await self._ejecutar(session, key, pos, nuevo_sl)
        if ok:
            pos['sl_modified_to_tp1'] = True
            safe_print(f"  ✅ [{pos['symbol']}] SL → TP1: {nuevo_sl:.{price_prec}f}")
            return True, "tp1"
        return False, "modificacion falló"

    async def _ejecutar(self, session, key: str, pos: dict, nuevo_sl: float) -> bool:
        """
        Llama modify_sl de bitunix_api.
        POST /tpsl/position/modify_order: symbol + positionId + slPrice + slStopType
        Bitunix actualiza el SL existente — no crea uno nuevo.
        Los TPs (órdenes LIMIT) no se tocan.
        """
        ahora      = time.time()
        price_prec = pos.get('price_prec', 4)
        symbol     = pos['symbol']

        if ahora - self._last_modification.get(key, 0) < self._cooldown_seconds:
            safe_print(f"  ⏳ [{symbol}] Cooldown SL")
            return False

        last_val = self._last_sl_value.get(key)
        if last_val and abs(last_val - nuevo_sl) < 10 ** (-price_prec):
            safe_print(f"  ⏳ [{symbol}] SL ya está en {nuevo_sl:.{price_prec}f}")
            return False

        position_id = pos.get('position_id', '')
        if not position_id:
            safe_print(f"  ❌ [{symbol}] Sin position_id")
            return False

        safe_print(f"  🔄 [SLManager] {symbol}: SL {pos['stop']:.{price_prec}f} → {nuevo_sl:.{price_prec}f}")

        try:
            result = await modify_sl(
                session     = session,
                symbol      = symbol,
                position_id = position_id,
                new_sl      = nuevo_sl,
                price_prec  = price_prec,
            )
            if result.get('success'):
                self._last_modification[key] = ahora
                self._last_sl_value[key]      = nuevo_sl
                pos['stop']                  = nuevo_sl
                pos['expected_sl']           = nuevo_sl
                pos['sl_last_update_time']   = ahora
                return True
            self._last_modification.pop(key, None)
            safe_print(f"  ❌ [{symbol}] modify_sl falló: {result.get('error','?')}")
            return False
        except Exception as e:
            self._last_modification.pop(key, None)
            safe_print(f"  ❌ [{symbol}] Excepción modify_sl: {e}")
            return False

# ══════════════════════════════════════════════════════════════
# PositionMonitor
# ══════════════════════════════════════════════════════════════
class PositionMonitor:
    def __init__(self):
        self._positions: dict      = {}
        self._running: bool        = False
        self._cooldown: dict       = {}
        self._dashboard_lines: int = 0
        self._close_confirms: dict = {}
        self._sync_counter: dict   = {}
        self._orphan_recovered     = False
        self._ultimo_log_sl: dict  = {}
        self._nueva_posicion       = False
        self._forzar_refresh       = False
        self._sl_manager           = SLManager()

    def add(self, key: str, data: dict):
        data['tp1_original']             = data.get('tp1', 0)
        data['tp3_original']             = data.get('tp3', 0)
        data['tp1_qty_ratio']             = TP1_RATIO
        data['sl_modified_to_breakeven'] = False
        data['sl_modified_to_tp1']       = False
        if 'expected_sl' not in data or data.get('expected_sl') in (None, 0):
            data['expected_sl'] = float(data.get('stop', 0) or 0)
        data.setdefault('sl_last_update_time', 0)
        ahora = time.time()
        data['timestamp_apertura'] = ahora
        data['proxima_revision']   = ahora + (2 * 60 * 60)
        data['modo_apertura']      = data.get('modo_apertura', 'ESTRICTO')
        self._positions[key] = data
        self._persist()
        self._nueva_posicion = True
        if self._dashboard_lines > 0:
            _clear_lines(self._dashboard_lines)
            self._dashboard_lines = 0
        pos = data
        safe_print(f"\n  📌 NUEVA POSICIÓN ABIERTA")
        safe_print(f"  {'═'*55}")
        safe_print(f"  {bold(pos['symbol'])} {pos['side']} | TF:{pos.get('timeframe','?')} | Modo:{pos.get('modo_apertura','ESTRICTO')}")
        safe_print(f"  Entry  : {pos['entry']:.8g}")
        safe_print(f"  SL     : {pos['stop']:.8g}")
        safe_print(f"  TP1 60%: {pos.get('tp1',0):.8g}")
        safe_print(f"  TP3 40%: {pos.get('tp3',0):.8g}")
        safe_print(f"  Capital: ${pos.get('capital_usado',4.0):.2f}")
        safe_print(f"  {'═'*55}")
        safe_print(f"  ⏳ Dashboard se actualizará en el próximo ciclo (5s)...\n", flush=True)

    def remove(self, key: str):
        self._positions.pop(key, None)
        delete_state_entry(key)

    def count(self) -> int:
        ahora = time.time()
        return len(self._positions) + sum(1 for t in self._cooldown.values() if t > ahora)

    def count_open(self) -> int:
        return len(self._positions)

    def _persist(self):
        save_state(self._positions)

    async def _recover_orphan_positions_once(self, session):
        if self._orphan_recovered: 
            return
        self._orphan_recovered = True
        try:
            vivas = await get_all_open_positions(session)
            if not vivas:
                return
            from bitunix_api import get_symbol_precision
            for pos_live in vivas:
                sym       = pos_live.get('symbol', '')
                ps        = str(pos_live.get('side', pos_live.get('holdSide', ''))).upper()
                side_live = 'LONG' if ps in ('BUY', 'LONG') else 'SHORT'
                pid       = str(pos_live.get('positionId', ''))
                ya_tiene  = any(
                    p.get('position_id') == pid or
                    (p.get('symbol') == sym and p.get('side') == side_live)
                    for p in self._positions.values()
                )
                if not ya_tiene and sym and pid:
                    entry      = float(pos_live.get('avgOpenPrice', pos_live.get('entryPrice', 0)))
                    qty        = float(pos_live.get('qty', 0))
                    prec       = await get_symbol_precision(session, sym)
                    price_prec = prec.get('price_prec', 4)
                    qty_prec   = prec.get('qty_prec', 3)
                    tpsl_info  = await get_active_tpsl_orders(session, sym, pid) or {}
                    has_sl     = tpsl_info.get('sl', 0) > 0
                    if has_sl:
                        stop = tpsl_info.get('sl', 0)
                        params      = {'symbol': sym}
                        if pid:
                            params['positionId'] = pid
                        orders_data = await _get(session, '/api/v1/futures/trade/get_pending_orders', params)
                        orders      = orders_data if isinstance(orders_data, list) \
                                      else (orders_data or {}).get('orderList', [])
                        tp_prices = []
                        tp_ids     = []
                        for o in orders:
                            if o.get('reduceOnly') and str(o.get('orderType', '')).upper() == 'LIMIT':
                                price = float(o.get('price', 0) or 0)
                                oid   = str(o.get('orderId', o.get('id', '')))
                                if price > 0:
                                    tp_prices.append(price)
                                    tp_ids.append(oid)
                        if side_live == 'LONG':
                            combined = sorted(zip(tp_prices, tp_ids))
                        else:
                            combined = sorted(zip(tp_prices, tp_ids), reverse=True)
                        tp_prices = [x[0] for x in combined]
                        tp_ids    = [x[1] for x in combined]
                        tp1_real  = tp_prices[0] if tp_prices else 0
                        tp3_real  = tp_prices[-1] if len(tp_prices) > 1 else 0
                        tp1_id    = tp_ids[0]     if tp_ids    else ''
                        tp3_id    = tp_ids[-1]    if len(tp_ids) > 1 else ''
                        if not tp1_real:
                            tp1_real, tp3_real = calc_tps(entry, stop, side_live)
                            tp1_real = round(tp1_real, price_prec)
                            tp3_real = round(tp3_real, price_prec)
                        # Si solo hay 1 orden LIMIT → TP1 ya fue ejecutado
                        tp1_ya_ejecutado = len(tp_prices) == 1
                        key_h = f"{sym}_{side_live}_{pid}"
                        self.add(key_h, {
                            'symbol':   sym, 'side': side_live, 'entry': entry,
                            'stop':     stop, 'tp1': tp1_real, 'tp3': tp3_real,
                            'position_id': pid,
                            'tp1_id':   tp1_id if not tp1_ya_ejecutado else '',
                            'tp3_id':   tp3_id,
                            'sl_id':    tpsl_info.get('tpsl_id', ''),
                            'qty':      qty, 'qty_prec': qty_prec, 'price_prec': price_prec,
                            'tp1_hit':  tp1_ya_ejecutado,
                            'sl_modified_to_breakeven': tp1_ya_ejecutado,
                            'modo_apertura': 'RECOVERY',
                        })
                        if tp1_ya_ejecutado:
                            safe_print(f"  📌 [RECOVERY] {sym} {side_live} — TP1 ya ejecutado, SL en breakeven pendiente")
                        else:
                            safe_print(f"  📌 [MONITOR] {sym} {side_live} recuperada CON SL")
                        continue
                    stop = round(entry * (1 - 0.01), price_prec) if side_live == 'LONG' \
                           else round(entry * (1 + 0.01), price_prec)
                    tp1, tp3 = calc_tps(entry, stop, side_live)
                    tp1 = round(tp1, price_prec)
                    tp3 = round(tp3, price_prec)
                    safe_print(f"  🔄 [RECOVERY] {sym} sin SL — colocando SL:{stop} TP1:{tp1} TP3:{tp3}")
                    result = await place_partial_tpsl(session, sym, pid, side_live,
                                                      stop, tp1, tp3, qty, price_prec, qty_prec)
                    key_h = f"{sym}_{side_live}_{pid}"
                    self.add(key_h, {
                        'symbol': sym, 'side': side_live, 'entry': entry,
                        'stop':   stop, 'tp1': tp1, 'tp3': tp3,
                        'position_id': pid,
                        'tp1_id': result.get('tp1_id', ''),
                        'tp3_id': result.get('tp3_id', ''),
                        'sl_id':  result.get('sl_id', ''),
                        'qty': qty, 'qty_prec': qty_prec, 'price_prec': price_prec,
                        'modo_apertura': 'RECOVERY',
                    })
                    safe_print(f"  📌 [MONITOR] {sym} {side_live} huérfana añadida")
        except Exception as e:
            safe_print(f"  ⚠️  [MONITOR] Error recuperando huérfanas: {e}")

    async def _check_tiempo_maximo(self, session, key) -> bool:
        pos = self._positions.get(key)
        if not pos or pos.get('sl_modified_to_breakeven') or pos.get('tp1_hit'):
            return False
        symbol      = pos['symbol']
        side        = pos['side']
        tf          = pos.get('timeframe', '1h').lower()
        position_id = pos.get('position_id', '')
        tiempo      = time.time() - pos.get('timestamp_apertura', time.time())
        limites     = {'15m': 4*3600, '1h': 8*3600, '4h': 12*3600}
        if tiempo < limites.get(tf, 8*3600):
            return False
        safe_print(f"\n  ⏰ [{symbol}] Tiempo máximo ({tiempo/3600:.1f}h) — cerrando...")
        ok = await flash_close(session, symbol, side, position_id)
        if ok:
            safe_print(f"  ✅ [{symbol}] Cerrada por tiempo máximo")
            return True
        return False

    async def _check_sl_distance(self, session, key):
        pos = self._positions.get(key)
        if not pos:
            return False
        symbol      = pos['symbol']
        side        = pos['side']
        entry       = pos['entry']
        stop        = pos['stop']
        price       = pos.get('current_price', entry)
        position_id = pos.get('position_id', '')
        if side == 'LONG':
            dist_total    = entry - stop
            dist_recorida = entry - price if price <= entry else 0
        else:
            dist_total    = stop - entry
            dist_recorida = price - entry if price >= entry else 0
        if dist_total <= 0:
            return False
        pct   = (dist_recorida / dist_total) * 100
        ahora = time.time()
        if ahora - self._ultimo_log_sl.get(key, 0) > 10:
            barra = "█" * int(pct/5) + "░" * (20 - int(pct/5))
            safe_print(f"  🛡️ [SL-DIST] {symbol} | {barra} {pct:.0f}% | Precio:{price:.6f} | SL:{stop:.6f}")
            self._ultimo_log_sl[key] = ahora
        if pct >= 80:
            safe_print(f"\n  🚨 [EMERGENCIA] {symbol} 80% hacia SL — CERRANDO!")
            ok = await flash_close(session, symbol, side, position_id)
            if ok:
                safe_print(f"  ✅ [EMERGENCIA] {symbol} cerrada")
                return True
        if pct >= 50 and not self._positions[key].get('_analisis_50_hecho'):
            safe_print(f"\n  ⚠️  [{symbol}] 50% hacia SL — monitoreando...")
            self._positions[key]['_analisis_50_hecho'] = True
            self._persist()
        elif pct < 40 and self._positions[key].get('_analisis_50_hecho'):
            self._positions[key]['_analisis_50_hecho'] = False
            self._persist()
        return False

    async def _sync_tpsl_from_exchange(self, session, key):
        self._sync_counter[key] = self._sync_counter.get(key, 0) + 1
        if self._sync_counter[key] % 3 != 0:
            return
        pos = self._positions.get(key)
        if not pos:
            return
        symbol      = pos.get('symbol', '')
        position_id = pos.get('position_id', '')
        price_prec  = pos.get('price_prec', 4)
        changed      = False
        params = {'symbol': symbol}
        if position_id:
            params['positionId'] = str(position_id)
        tpsl_data   = await _get(session, '/api/v1/futures/tpsl/get_pending_orders', params)
        tpsl_orders = tpsl_data if isinstance(tpsl_data, list) \
                      else (tpsl_data or {}).get('orderList', [])
        real_sl = 0.0
        sl_id   = ''
        for o in tpsl_orders:
            pid = str(o.get('positionId', ''))
            if position_id and pid and pid != str(position_id):
                continue
            sl  = float(o.get('slPrice', 0) or 0)
            oid = str(o.get('id', o.get('orderId', '')))
            if sl:
                real_sl = sl
                sl_id   = oid

        if real_sl:
            local_sl = pos.get('stop', 0)
            entry    = pos.get('entry', 0)
            tp1      = pos.get('tp1_original') or pos.get('tp1', 0)

            # ── Siempre detectar estado real del SL ──────────────
            # Caso 1: SL está en el precio de entrada → breakeven
            if entry and abs(real_sl - round(entry, price_prec)) < 10 ** (-price_prec + 1):
                if not pos.get('sl_modified_to_breakeven', False):
                    self._positions[key]['stop']                    = real_sl
                    self._positions[key]['expected_sl']             = real_sl
                    self._positions[key]['sl_modified_to_breakeven'] = True
                    self._positions[key]['tp1_hit']                   = True
                    changed = True
                    safe_print(f"  🔄 [{symbol}] SL detectado en BREAKEVEN: {real_sl:.{price_prec}f}")
                else:
                    # ya marcado, solo sincronizar valor
                    self._positions[key]['stop'] = real_sl

            # Caso 2: SL está en el precio de TP1 → ya pasó el 70%
            elif tp1 and abs(real_sl - round(tp1, price_prec)) < 10 ** (-price_prec + 1):
                if not pos.get('sl_modified_to_tp1', False):
                    self._positions[key]['stop']                    = real_sl
                    self._positions[key]['expected_sl']             = real_sl
                    self._positions[key]['sl_modified_to_breakeven'] = True
                    self._positions[key]['sl_modified_to_tp1']      = True
                    self._positions[key]['tp1_hit']                  = True
                    changed = True
                    safe_print(f"  🔄 [{symbol}] SL detectado en TP1: {real_sl:.{price_prec}f}")
                else:
                    self._positions[key]['stop'] = real_sl

            # Caso 3: SL cambió a un valor distinto (modificación manual o post-modify)
            elif abs(real_sl - local_sl) > 10 ** (-price_prec):
                last_update = pos.get('sl_last_update_time', 0)
                if time.time() - last_update < 8:
                    pass  # ignorar delay post-modificación
                else:
                    self._positions[key]['stop']        = real_sl
                    self._positions[key]['expected_sl'] = real_sl
                    changed = True
                    safe_print(f"  🔄 [{symbol}] SL actualizado: {local_sl:.{price_prec}f} → {real_sl:.{price_prec}f}")

        if sl_id and sl_id != pos.get('sl_id', ''):
            self._positions[key]['sl_id'] = sl_id
            changed = True
        if changed:
            self._persist()

    async def _check_tp_hit_and_modify_sl(self, session, key):
        """
        Detecta TP1 REAL por desaparición del tp1_id en órdenes pendientes.

        NO usa precio actual — evita falsos positivos por timing/retroceso.

        Casos:
        1. tp1_id existe:
           → si desaparece = TP1 ejecutado REAL.

        2. Recovery:
           tp1_id vacío porque recovery encontró
           que solo quedaba TP3.
           → fallback revisa si solo queda 1 LIMIT.
        """

        pos = self._positions.get(key)

        if not pos or pos.get('sl_modified_to_breakeven', False):
            return

        symbol      = pos['symbol']
        position_id = pos.get('position_id', '')
        tp1_id      = str(pos.get('tp1_id', ''))

        # Sin position_id no podemos verificar
        if not position_id:
            return

        # =========================================================
        # CONSULTAR ÓRDENES PENDIENTES
        # =========================================================
        params = {'symbol': symbol}
        params['positionId'] = position_id

        try:

            orders_data = await _get(
                session,
                '/api/v1/futures/trade/get_pending_orders',
                params
            )

        except Exception as e:

            safe_print(
                f"  ⚠️  [{symbol}] Error consultando órdenes pendientes: {e}"
            )
            return

        orders = orders_data if isinstance(orders_data, list) \
                 else (orders_data or {}).get('orderList', [])

        # =========================================================
        # FALLBACK RECOVERY
        # tp1_id vacío → recovery detectó que TP1 ya no existía
        # =========================================================
        if not tp1_id:

            limit_orders = [
                o for o in orders
                if o.get('reduceOnly')
                and str(o.get('orderType', '')).upper() == 'LIMIT'
            ]

            # Solo queda TP3
            if len(limit_orders) == 1:

                safe_print(
                    f"  🎯 [{symbol}] Recovery detectó TP1 ejecutado"
                )

                pos['tp1_hit'] = True

                success, motivo = await self._sl_manager.modify_sl_to_breakeven(
                    session,
                    key,
                    pos
                )

                if success:

                    self._persist()
                    self._forzar_refresh = True

                else:

                    safe_print(
                        f"  ❌ [{symbol}] Recovery BE falló: {motivo}"
                    )

            return

        # =========================================================
        # LÓGICA NORMAL
        # =========================================================

        # Verificar si tp1_id sigue viva
        tp1_sigue_viva = any(
            str(o.get('orderId', o.get('id', ''))) == tp1_id
            for o in orders
        )

        # Si ya NO existe → TP1 ejecutado REAL
        if not tp1_sigue_viva:

            safe_print(
                f"  🎯 [{symbol}] TP1 REAL ejecutado "
                f"(tp1_id {tp1_id} ya no existe en pendientes)"
            )

            pos['tp1_hit'] = True

            success, motivo = await self._sl_manager.modify_sl_to_breakeven(
                session,
                key,
                pos
            )

            if success:

                self._persist()
                self._forzar_refresh = True

            else:

                safe_print(
                    f"  ❌ [{symbol}] SL a breakeven falló: {motivo}"
                )

    async def _check_tp1_to_tp3_progress(self, session, key):
        """
        Cuando el precio avanza 70% del recorrido TP1→TP3
        → modify_sl envía symbol + positionId + slPrice=tp1
        Solo activo tras TP1 ejecutado (sl_modified_to_breakeven=True).
        """
        pos = self._positions.get(key)
        if not pos:
            return
        if not pos.get('sl_modified_to_breakeven', False):
            return
        if pos.get('sl_modified_to_tp1', False):
            return

        symbol        = pos['symbol']
        side          = pos['side']
        current_price = pos.get('current_price', pos['entry'])
        tp1           = pos.get('tp1_original') or pos.get('tp1', 0)
        tp3           = pos.get('tp3_original') or pos.get('tp3', 0)

        if not tp1 or not tp3 or not current_price:
            return

        if side == 'LONG':
            distancia_total  = tp3 - tp1
            distancia_actual = current_price - tp1
        else:
            distancia_total  = tp1 - tp3
            distancia_actual = tp1 - current_price
 
        if distancia_total <= 0:
            return

        pct = max(0.0, min(100.0, (distancia_actual / distancia_total) * 100))

        if pct < 70:
            return

        safe_print(f"  🎯 [{symbol}] {pct:.1f}% TP1→TP3 — modificando SL al precio de TP1: {tp1:.8g}")

        success, motivo = await self._sl_manager.modify_sl_to_tp1(session, key, pos, tp1)
        if success:
            self._persist()
            self._forzar_refresh = True
        else:
            safe_print(f"  ❌ [{symbol}] SL a TP1 falló: {motivo}")

    async def _cancel_known_tp_limits(self, session, symbol: str, pos: dict):
        for oid in (pos.get('tp1_id'), pos.get('tp3_id')):
            if oid:
                await cancel_order_by_id(session, symbol, str(oid))
        await asyncio.sleep(0.25)

    async def _check_invalidacion(self, session, key):
        from analisis.indicadores import get_klines
        from MAESTRO_FILTRO_V1_FASE2_ANALYZER import InstitutionalAnalyzer
        from MAESTRO_FILTRO_V1_FASE2_FLEXIBLE import FlexibleAnalyzer
        from MAESTRO_FILTRO_V1_FASE3_SCORING import InstitutionalScoring
        pos = self._positions.get(key)
        if not pos or pos.get('sl_modified_to_breakeven') or pos.get('tp1_hit'):
            return
        symbol        = pos['symbol']
        side           = pos['side']
        entry         = pos.get('entry', 0)
        stop          = pos.get('stop', 0)
        precio        = pos.get('current_price', entry)
        position_id   = pos.get('position_id', '')
        modo_apertura = pos.get('modo_apertura', 'ESTRICTO')
        safe_print(f"\n  🔍 [REANALISIS] {symbol} {side} | Modo: {modo_apertura}")
        try:
            candles_4h = await get_klines(session, symbol, "4h", 120)
            candles_1h = await get_klines(session, symbol, "1h", 120)
            if not candles_4h or not candles_1h:
                safe_print(f"     ⚠️ Datos insuficientes")
                return
            analyzer = InstitutionalAnalyzer()
            contexto = await analyzer.analizar_contexto(
                session=session, symbol=symbol, direccion_senal=side,
                candles_4h=candles_4h, candles_1h=candles_1h, adx_umbral=20.0
            )
            adx_data = contexto.get('adx', {})
            btc_data = contexto.get('btc', {})
            macro_4h = contexto.get('macro', {})
            aceptada = False
            razones  = []
            if modo_apertura == 'FLEXIBLE':
                flexible  = FlexibleAnalyzer()
                resultado = flexible.evaluar(
                    macro_4h=macro_4h, adx_data=adx_data, btc_data=btc_data,
                    candles_1h=candles_1h[:-1] if len(candles_1h) > 1 else candles_1h,
                    direccion_senal=side,
                )
                aceptada = resultado.get('ok', False)
                razones  = resultado.get('razones', [])
                safe_print(f"     📊 FLEXIBLE: {'✅ OK' if aceptada else '❌ INVALIDA'}")
            else:
                scoring     = InstitutionalScoring()
                _tf_map     = {"15m": 15, "1h": 60, "4h": 240}
                tf_minutos  = _tf_map.get(pos.get('timeframe', '1h').lower(), 60)
                tps_list    = [t for t in [pos.get('tp1', 0), pos.get('tp3', 0)] if t > 0]
                bonus_total = max(min(contexto.get('bonus_contratendencia', 0), 4), -3)
                resultado   = scoring.evaluar_senal(
                    side=side, entry=entry, stop=stop, tps_list=tps_list,
                    candles_1h=candles_1h[:-1] if len(candles_1h) > 1 else candles_1h,
                    candles_4h=candles_4h[:-1] if len(candles_4h) > 1 else candles_4h,
                    candles_1d=None, contexto=contexto, rr_min=0.8,
                    bonus_externo=bonus_total, level_price=0, tf_minutos=tf_minutos,
                )
                aceptada = resultado.get('aceptada', False)
                razones  = [resultado.get('razon_rechazo', '')] if not aceptada else []
                safe_print(f"     📊 ESTRICTO: {'✅ OK' if aceptada else '❌ INVALIDA'}")
            if aceptada:
                safe_print(f"     ✅ [{symbol}] Condiciones OK — continuar")
                return
            for r in razones[:2]:
                safe_print(f"     ⚠️ {r}")
            dist_total  = (entry - stop) if side == 'LONG' else (stop - entry)
            dist_actual = max((entry - precio) if side == 'LONG' else (precio - entry), 0)
            pct = (dist_actual / dist_total * 100) if dist_total > 0 else 0
            safe_print(f"     📊 Recorrido hacia SL: {pct:.1f}%")
            if pct < 30:
                pos['_pendiente_cierre'] = True
                self._persist()
            elif pct < 70:
                if pos.get('_pendiente_cierre'):
                    ok = await flash_close(session, symbol, side, position_id)
                    if ok: safe_print(f"     ✅ [{symbol}] Cerrada por invalidación")
                else:
                    pos['_pendiente_cierre'] = True
                    self._persist()
            else:
                ok = await flash_close(session, symbol, side, position_id)
                if ok: safe_print(f"     ✅ [{symbol}] Cerrada por invalidación (emergencia)")
        except Exception as e:
            safe_print(f"     ⚠️ Error reanalisis: {e}")
            import traceback; traceback.print_exc()

    async def _check_close(self, session, key) -> bool:
        pos = self._positions.get(key)
        if not pos:
            return False
        symbol      = pos['symbol']
        side        = pos['side']
        entry       = pos['entry']
        stop        = pos['stop']
        tp1         = pos.get('tp1', 0)
        open_pos    = await get_open_position(session, symbol, side)
        if open_pos:
            self._close_confirms.pop(key, None)
            return False
        position_id = pos.get('position_id', '')
        if position_id:
            all_pos = await get_all_open_positions(session)
            for p in all_pos:
                if str(p.get('positionId', '')) == str(position_id):
                    if float(p.get('qty', 0)) > 0:
                        self._close_confirms.pop(key, None)
                        return False
        self._close_confirms[key] = self._close_confirms.get(key, 0) + 1
        confirms = self._close_confirms[key]
        if confirms == 1:
            self._positions[key]['_cerrando'] = True
            self._persist()
        if confirms < 3:
            safe_print(f"  ⚠️  [{symbol}] Posición no encontrada ({confirms}/3) — verificando...")
            return False
        self._close_confirms.pop(key, None)
        if self._dashboard_lines > 0:
            _clear_lines(self._dashboard_lines)
            self._dashboard_lines = 0
        capital_usado = pos.get('capital_usado', 4.0)
        pnl           = None
        close_price   = 0.0
        try:
            params   = {'symbol': symbol}
            if position_id:
                params['positionId'] = str(position_id)
            hist     = await _get(session, '/api/v1/futures/position/get_history_positions', params)
            pos_list = hist if isinstance(hist, list) else (hist or {}).get('positionList', [])
            for h in pos_list:
                if str(h.get('positionId', '')) == str(position_id):
                    realized    = float(h.get('realizedPNL', 0) or 0)
                    fee         = float(h.get('fee', 0) or 0)
                    funding     = float(h.get('funding', 0) or 0)
                    close_price = float(h.get('closePrice', h.get('entryPrice', 0)) or 0)
                    pnl         = round(realized + fee + funding, 4)
                    safe_print(f"  📊 PnL: {realized} + fee:{fee} + funding:{funding} = {pnl:+.4f} USDT")
                    break
        except Exception as e:
            safe_print(f"  ⚠️  Error historial: {e}")
        balance_ahora = await get_balance(session)
        if pnl is None:
            balance_antes = pos.get('balance_before', 0)
            diff          = round(balance_ahora - balance_antes, 4) if balance_antes else 0
            max_pnl       = capital_usado * 4 * 2
            if balance_antes and abs(diff) <= max_pnl:
                pnl = diff
            else:
                price = pos.get('current_price', entry)
                qty   = pos.get('qty', 0)
                pnl   = round((price-entry)*qty if side=='LONG' else (entry-price)*qty, 4) \
                        if price and entry and qty else 0.0
        if _ADAPTIVE_AVAILABLE:
            signal = pos.get('signal')
            if signal:
                adaptive_filter.record_trade(signal, {'win': pnl > 0, 'pnl': pnl})
        resultado_usd = capital_usado + pnl
        try:
            W      = 56
            header = green(f"║  ✅ POSICIÓN CERRADA — GANANCIA{'':^{W-30}}║") if pnl >= 0 \
                     else red(f"║  ❌ POSICIÓN CERRADA — PÉRDIDA{'':^{W-30}}║")
            safe_print(bold(cyan(f"\n╔{'═'*W}╗")))
            safe_print(header)
            safe_print(cyan(f"╠{'─'*W}╣"))
            safe_print(cyan("║   ") + f"  {'Símbolo': <10}: {bold(f'{symbol} {side}')}" + cyan("  ║"))
            safe_print(cyan("║   ") + f"  {'Entrada': <10}: {entry: <{W-13}.8g}" + cyan("  ║"))
            if close_price:
                safe_print(cyan("║   ") + f"  {'Cierre': <10}: {close_price: <{W-13}.8g}" + cyan("  ║"))
            safe_print(cyan("║   ") + f"  {'SL': <10}: {stop: <{W-13}.8g}" + cyan("  ║"))
            safe_print(cyan("║   ") + f"  {'TP1': <10}: {tp1: <{W-13}.8g}" + cyan("  ║"))
            if pos.get('tp3', 0):
                safe_print(cyan("║   ") + f"  {'TP3': <10}: {pos.get('tp3',0): <{W-13}.8g}" + cyan("  ║"))
            safe_print(cyan(f"╠{'─'*W}╣"))
            safe_print(cyan("║   ") + f"  💰 Capital : ${capital_usado: <{W-18}.4f}" + cyan("  ║"))
            pnl_l = f"  {'📈' if pnl >=0 else '📉'} PnL     : {'+' if pnl >=0 else ''}{pnl:.4f} USDT"
            res_l = f"  🏦 Resultado: ${resultado_usd:.4f} USDT"
            safe_print(cyan("║   ") + f"{pnl_l: <{W}}" + cyan("  ║"))
            res_c = green(f"{res_l: <{W}}") if pnl >= 0 else red(f"{res_l: <{W}}")
            safe_print(cyan("║   ") + res_c + cyan("  ║"))
            safe_print(bold(cyan(f"╚{'═'*W}╝\n")))
        except Exception as fmt_err:
            safe_print(f"\n  {'✅' if pnl >=0 else '❌'} CERRADA: {symbol} {side} | PnL: {pnl:+.4f} USDT")
        ts_apertura  = pos.get('timestamp_apertura', 0)
        duracion_min = round((time.time() - ts_apertura) / 60, 1) if ts_apertura > 1000000 else 0
        close_reason = 'TP' if pnl > 0 else ('SL' if pnl < 0 else 'breakeven')
        detalle = {
            'score_botai':     pos.get('score_botai', 0),
            'ia_probability':  pos.get('ia_probability', 0),
            'timeframe':       pos.get('timeframe', ''),
            'analisis_score':  pos.get('analisis_score', 0),
            'analisis_calidad':pos.get('analisis_calidad', ''),
            'pts_4h':          pos.get('pts_4h', 0),
            'pts_1h':          pos.get('pts_1h', 0),
            'pts_gatillo':     pos.get('pts_gatillo', 0),
            'fvg_detectado':   pos.get('fvg_detectado', False),
            'ema50_hacia':     pos.get('ema50_hacia', ''),
            'ema12_26_ok':     pos.get('ema12_26_ok', False),
            'vela_rechazo':    pos.get('vela_rechazo', False),
            'tipo_rechazo':    pos.get('tipo_rechazo', ''),
            'tp_modificado':   pos.get('tp_modificado', False),
            'tp_rr':           pos.get('tp_rr', 1.7),
            'duracion_min':    duracion_min,
            'close_reason':    close_reason,
            'close_price':     close_price,
            'balance_antes':   pos.get('balance_before', 0),
            'balance_despues': balance_ahora,
            'modo_apertura':   pos.get('modo_apertura', 'ESTRICTO'),
        }
        self.remove(key)
        registrar_operacion(symbol, side, entry, stop, tp1, capital_usado, pnl, detalle=detalle)
        mostrar_estadisticas()
        update_capital(pnl, symbol, side)
        clean_expired_blocks()
        await print_balance(session, f"Tras cierre {symbol}")
        safe_print(f"  ✅ Posición cerrada — lista para nueva señal\n")
        self._cooldown = {k: v for k, v in self._cooldown.items() if v > time.time()}
        return True

    def _render(self, positions_data: list, balance: float) -> list:
        W     = 95
        lines = []
        title = f"MONITOREO DE POSICIONES EN VIVO FUTURES PULSE SIGNALS ({len(positions_data)})"
        lines.append(bold(cyan(f"╔{'═'*W}╗")))
        lines.append(bold(cyan(f"║   ")) + bold(f"{title:^{W}}") + bold(cyan(f"║")))
        lines.append(bold(cyan(f"╠{'═'*W}╣")))
        if not positions_data:
            msg = "Sin posiciones abiertas — esperando señales..."
            lines.append(cyan("║   ") + f"{msg:^{W}}" + cyan("║"))
        else:
            for i, pos in enumerate(positions_data):
                symbol  = pos['symbol']
                side    = pos['side']
                tf      = pos.get('timeframe', '?') 
                modo    = pos.get('modo_apertura', 'ESTRICTO')[:4]
                entry   = pos['entry']
                stop    = pos['stop']
                price   = pos.get('current_price', entry)
                qty     = pos.get('qty', 0)
                pnl_usd = (price-entry)*qty if side=='LONG' else (entry-price)*qty
                pnl_pct = (price-entry)/entry*100 if side=='LONG' else (entry-price)/entry*100
                pnl_str   = _pnl_str(pnl_usd)
                pnl_color = '🟩' if pnl_usd >= 0 else '🟥'
                header_text = (f" {symbol: <10} │ {side: <6} │ {tf: <4} │ {modo: <4} │  "
                               f"Entry:{entry:.8f} │ Actual:{price:.8f} │  "
                               f"{pnl_color} {pnl_str} ({pnl_pct:+.2f}%)  ")
                padding = max(0, W - len(header_text) - 2)
                lines.append(cyan("║   ") + header_text + "  "*padding + cyan("║"))
                lines.append(cyan("║   ") + "  "*W + cyan("║"))
                dist_total  = (entry-stop) if side=='LONG' else (stop-entry)
                dist_actual = (price-stop) if side=='LONG' else (stop-price)
                pct_peligro = 0
                if dist_total > 0:
                    pct_peligro = max(0, min(100, ((dist_total-dist_actual)/dist_total)*100))
                bar_sl    = _bar_sl(pct_peligro, width=25)
                estado_sl = _sl_estado(pct_peligro)
                color_sl  = red if pct_peligro >70 else yellow if pct_peligro >40 else dim
                sl_be     = pos.get('sl_modified_to_breakeven', False)
                sl_tp1    = pos.get('sl_modified_to_tp1', False)
                if sl_tp1:   sl_label = green("🔒 SL → TP1 (protegido)")
                elif sl_be:  sl_label = green("🔒 SL → BREAKEVEN")
                else:        sl_label = "✅ STOP LOSS"
                sl_text = (f"     {sl_label}: {stop:.8f}   [{bar_sl}]    "
                           f"{color_sl(f'{pct_peligro:.0f}%')}  -  {estado_sl}")
                lines.append(cyan("║   ") + sl_text + "  "*max(0,W-len(sl_text)-2) + cyan("║"))
                lines.append(cyan("║   ") + "  "*W + cyan("║"))
                tp1_price = pos.get('tp1_original') or pos.get('tp1', 0)
                tp3_price = pos.get('tp3_original') or pos.get('tp3', 0)
                if tp1_price > 0:
                    d_tp1   = (tp1_price-entry) if side=='LONG' else (entry-tp1_price)
                    d_act   = (price-entry)      if side=='LONG' else (entry-price)
                    pct_tp1 = max(0, min(100, (d_act/d_tp1)*100)) if d_tp1 > 0 else 0
                    icono   = "✅" if pct_tp1 >=100 else "⏳"
                    estado  = "COMPLETADO" if pct_tp1 >=100 else ("EN CAMINO" if pct_tp1 >0 else "PENDIENTE")
                    c_tp1   = green if pct_tp1 >=100 else (cyan if pct_tp1 >0 else dim)
                    gain    = abs(tp1_price-entry)/entry*100
                    tp_text = (f"     {icono} TP1: {tp1_price:.8f}  (+{gain:.2f}%)    "
                               f"[{_bar_tp(pct_tp1,25)}]   {c_tp1(f'{pct_tp1:.0f}%')}  -  {estado}")
                    lines.append(cyan("║   ") + tp_text + "  "*max(0,W-len(tp_text)-2) + cyan("║"))
                if tp3_price > 0:
                    tp1_ya  = sl_be or pos.get('tp1_hit', False)
                    if not tp1_ya:
                        pct_tp3 = 0; icono = "⏳"; estado = "ESPERANDO TP1"; c_tp3 = dim
                    else:
                        d_tp3  = (tp3_price-tp1_price) if side=='LONG' else (tp1_price-tp3_price)
                        d_act3 = (price-tp1_price)      if side=='LONG' else (tp1_price-price)
                        pct_tp3 = max(0, min(100, (d_act3/d_tp3)*100)) if d_tp3 > 0 else 0
                        if pct_tp3 >= 100:  icono="✅"; estado="COMPLETADO";    c_tp3=green
                        elif pct_tp3 >= 70: icono="🔥"; estado="70% → SL→TP1"; c_tp3=yellow
                        elif pct_tp3 > 0:   icono="⏳"; estado="EN CAMINO";     c_tp3=cyan
                        else:               icono="⏳"; estado="INICIANDO";      c_tp3=dim
                    gain    = abs(tp3_price-entry)/entry*100
                    tp_text = (f"     {icono} TP3: {tp3_price:.8f}  (+{gain:.2f}%)    "
                               f"[{_bar_tp(pct_tp3,25)}]   {c_tp3(f'{pct_tp3:.0f}%')}  -  {estado}")
                    lines.append(cyan("║   ") + tp_text + "  "*max(0,W-len(tp_text)-2) + cyan("║"))
                lines.append(cyan("║   ") + "  "*W + cyan("║"))
                if i < len(positions_data) - 1:
                    lines.append(bold(cyan(f"╠{'═'*W}╣")))
        hora   = time.strftime('%H:%M:%S')
        footer = f" 💰 Saldo Total: ${balance:.4f} USDT  │  Hora: {hora}  "
        lines.append(bold(cyan(f"╟{'─'*W}╢")))
        ventana = time_filter.obtener_estado_ventana()
        estado  = ventana['estado']
        vtext   = f" {ventana['texto']}  "
        c_v     = green if estado=='activo' else (yellow if estado=='cerrando' else red)
        pad_v   = max(0, W - _visual_len(vtext))
        lines.append(c_v("║   ") + c_v(vtext) + ' '*pad_v + c_v("║"))
        lines.append(bold(cyan(f"╟{'─'*W}╢")))
        pad_f = max(0, W - _visual_len(footer))
        lines.append(cyan("║   ") + footer + ' '*pad_f + cyan("║"))
        lines.append(bold(cyan(f"╚{'═'*W}╝")))
        return lines

    async def start(self, session: aiohttp.ClientSession):
        self._running = True
        first         = True
        await self._recover_orphan_positions_once(session)
        while self._running:
            try:
                if self._nueva_posicion or self._forzar_refresh:
                    self._nueva_posicion = False
                    self._forzar_refresh = False
                    positions_data = []
                    for key, pos in list(self._positions.items()):
                        try:
                            ticker = await get_ticker(session, pos['symbol'])
                            if ticker:
                                pos['current_price'] = float(ticker.get('markPrice', ticker.get('lastPrice', pos['entry'])))
                        except: pass
                        positions_data.append(dict(pos))
                    balance = await get_balance(session)
                    lines   = self._render(positions_data, balance)
                    if os.name == 'nt': os.system('cls')
                    elif self._dashboard_lines > 0: _clear_lines(self._dashboard_lines)
                    for line in lines: safe_print(line, flush=True)
                    self._dashboard_lines = len(lines)
                    first = False
                    await asyncio.sleep(POLL_INTERVAL)
                    continue
                if self._positions:
                    for key in list(self._positions.keys()):
                        try:
                            if await self._check_tiempo_maximo(session, key): first = True
                        except Exception as e: safe_print(f"  ❌ [TIEMPO] {key}: {e}")
                if self._positions:
                    for key in list(self._positions.keys()):
                        try:
                            if await self._check_sl_distance(session, key): first = True
                        except Exception as e: safe_print(f"  ❌ [SL-DIST] {key}: {e}")
                if self._positions:
                    for key in list(self._positions.keys()):
                        try:
                            if await self._check_close(session, key): first = True
                        except Exception as e: safe_print(f"  ❌ [CLOSE] {key}: {e}")
                positions_data = []
                for key, pos in list(self._positions.items()):
                    try:
                        ticker = await get_ticker(session, pos['symbol'])
                        if ticker:
                            pos['current_price'] = float(ticker.get('markPrice', ticker.get('lastPrice', pos['entry'])))
                    except: pass
                    positions_data.append(dict(pos))
                for key in list(self._positions.keys()):
                    await self._sync_tpsl_from_exchange(session, key)
                for key in list(self._positions.keys()):
                    try:    await self._check_tp_hit_and_modify_sl(session, key)
                    except Exception as e: safe_print(f"  ⚠️  [TP_HIT] {key}: {e}")
                for key in list(self._positions.keys()):
                    try:    await self._check_tp1_to_tp3_progress(session, key)
                    except Exception as e: safe_print(f"  ⚠️  [TP3_PROG] {key}: {e}")
                ahora = time.time()
                for key, pos in list(self._positions.items()):
                    if ahora >= pos.get('proxima_revision', 0):
                        try:
                            await self._check_invalidacion(session, key)
                            self._positions[key]['proxima_revision'] = ahora + 2*3600
                            self._persist()
                        except Exception as e: safe_print(f"  ⚠️  [INVALIDA] {key}: {e}")
                if not self._positions:
                    if not hasattr(self, '_ultimo_msg_sin_pos') or ahora - self._ultimo_msg_sin_pos > 180:
                        safe_print(f"  ℹ️  [MONITOR] Sin posiciones abiertas — esperando señales...")
                        self._ultimo_msg_sin_pos = ahora
                balance = await get_balance(session)
                lines   = self._render(positions_data, balance)
                if not first:
                    if os.name == 'nt': os.system('cls')
                    else:
                        if self._dashboard_lines > 0:
                            sys.stdout.write(f'\033[{self._dashboard_lines}A')
                            sys.stdout.write('\033[J')
                    sys.stdout.flush()
                safe_print("\n".join(lines) + "\n", end='', flush=True)
                self._dashboard_lines = len(lines)
                first = False
                await asyncio.sleep(POLL_INTERVAL)
            except KeyboardInterrupt:
                break
            except Exception as e:
                safe_print(f"\n❌ ERROR CRÍTICO MONITOR: {e}")
                import traceback; traceback.print_exc()
                await asyncio.sleep(5)

    def stop(self):
        self._running = False