"""
recovery.py
===========
Al arrancar el bot:
  1. Lee posiciones abiertas en Bitunix
  2. Cruza con estado.json (señales guardadas)
  3. Si una señal guardada ya NO está en Bitunix → se cerró mientras el bot estaba apagado → eliminar
  4. Si sigue abierta → registrar en monitor para seguimiento
  5. Completar hasta 2 posiciones con nuevas señales si hay menos de 2

🆕 SYNC V2: Soporte exclusivo para 2 TPs (60%/40%)
🆕 CORREGIDO: Lee datos desde registro_secuencial.txt (mismo que signal_reader)
🆕 CORREGIDO: El exchange es la verdad absoluta — se lee el SL real del exchange
🆕 CORREGIDO: Se detecta automáticamente si ya se movió a breakeven o TP1
🆕 CORREGIDO V2: tp1_original/tp3_original como fuente de verdad (no el exchange)

🟢 ARQUITECTURA CORREGIDA V3:
   🔵 MONITOR = inteligencia activa (detecta TP1 real, BE, modifica SL)
   🟡 RECOVERY = reconstrucción pasiva (SOLO restaura estado estructural)
   ❌ RECOVERY NO decide TP1 ni BE
"""
import json, os, aiohttp, time, traceback, asyncio
from bitunix_api import (
    get_all_open_positions, get_symbol_precision, calc_tps,
    place_partial_tpsl,
    get_active_tpsl_orders, cancel_order_by_id, _get, _post
)

STATE_FILE = 'estado.json'



def save_state(positions: dict):
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(positions, f, indent=2)
    except Exception as e:
        print(f"  ⚠️  No se pudo guardar estado: {e}")


def load_state() -> dict:
    if not os.path.exists(STATE_FILE): return {}
    try:
        with open(STATE_FILE, 'r') as f: return json.load(f)
    except Exception as e:
        print(f"  ⚠️  Error leyendo estado.json: {e}"); return {}


def delete_state_entry(key: str):
    state = load_state()
    if key in state:
        del state[key]; save_state(state)



async def _get_sl_from_exchange(session, symbol, position_id) -> tuple:
    """
    Lee el SL REAL desde el exchange.
    Retorna (sl_price, sl_id)
    """
    try:
        params = {'symbol': symbol}
        if position_id:
            params['positionId'] = str(position_id)
        
        tpsl_data = await _get(session, '/api/v1/futures/tpsl/get_pending_orders', params)
        tpsl_orders = tpsl_data if isinstance(tpsl_data, list) else (tpsl_data or {}).get('orderList', [])
        
        for o in tpsl_orders:
            pid = str(o.get('positionId', ''))
            if position_id and pid and pid != str(position_id):
                continue
            sl = float(o.get('slPrice', 0) or 0)
            oid = str(o.get('id', o.get('orderId', '')))
            if sl > 0:
                return sl, oid
    except Exception as e:
        print(f"  ⚠️  [RECOVERY] Error leyendo SL del exchange: {e}")
    
    return 0.0, ''


async def _get_tps_from_exchange(session, symbol, position_id, side) -> tuple:
    """
    Lee los TPs REALES desde el exchange (V2: Solo TP1 y TP3).
    Retorna (tp1, tp3, tp_count)

    tp_count = número de órdenes LIMIT activas.
    PRINCIPIO CLAVE:
      - Si tp_count == 2 → ambos TPs intactos, tp_prices[0]=TP1, tp_prices[1]=TP3
      - Si tp_count == 1 → TP1 ya se ejecutó, la única orden activa ES TP3.
        En ese caso tp1=0, tp3=tp_prices[0].
        El llamador DEBE usar tp1_original/tp3_original del estado.json como niveles matemáticos.
    """
    try:
        params = {'symbol': symbol}
        if position_id:
            params['positionId'] = str(position_id)

        pending_data = await _get(session, '/api/v1/futures/trade/get_pending_orders', params)
        pending_orders = pending_data if isinstance(pending_data, list) else (pending_data or {}).get('orderList', [])

        tp_prices = []
        for o in pending_orders:
            pid = str(o.get('positionId', ''))
            if position_id and pid and pid != str(position_id):
                continue
            reduce_only = o.get('reduceOnly', False)
            order_type = str(o.get('orderType', o.get('type', '')).upper())
            price = float(o.get('price', 0) or 0)

            if reduce_only and order_type == 'LIMIT' and price > 0:
                tp_prices.append(price)

        if side == 'LONG':
            tp_prices.sort()
        else:
            tp_prices.sort(reverse=True)

        tp_count = len(tp_prices)

        # Solo cuando hay 2 órdenes activas se puede leer TP1 desde el exchange.
        # Con 1 orden, esa orden ES TP3 — NO es TP1.
        if tp_count >= 2:
            tp1 = tp_prices[0]
            tp3 = tp_prices[1]
        else:
            tp1 = 0.0
            tp3 = tp_prices[0] if tp_count == 1 else 0.0

        return tp1, tp3, tp_count
    except Exception as e:
        print(f"  ⚠️  [RECOVERY] Error leyendo TPs del exchange: {e}")

    return 0.0, 0.0, 0


async def _recolocar_tpsl_si_falta(session, symbol, position_id, side, sl_price,
                                    tp1_price, tp3_price,
                                    qty, price_prec, qty_prec):
    """
    ✅ CORREGIDO V2: Coloca TPs + SL SOLO si no existe ninguno en el exchange.
    ✅ Solo 1 intento para evitar duplicados por latencia.
    Verifica en AMBOS endpoints (tpsl Y trade/pending) antes de colocar.
    """
    if not sl_price or sl_price <= 0:
        print(f"  ⚠️  [RECOVERY] {symbol} sin SL válido — no se colocan TPs/SL")
        return ''
    if not tp1_price or tp1_price <= 0:
        print(f"  ⚠️  [RECOVERY] {symbol} sin TPs válidos — no se colocan TPs/SL")
        return ''

    params = {'symbol': symbol}
    if position_id:
        params['positionId'] = str(position_id)

    # ── 1. Verificar SL en tpsl/get_pending_orders ────────────
    try:
        tpsl_data = await _get(session, '/api/v1/futures/tpsl/get_pending_orders', params)
        tpsl_list = tpsl_data if isinstance(tpsl_data, list) else \
                    (tpsl_data or {}).get('orderList', [])
        for o in tpsl_list:
            # Verificar por symbol sin filtrar positionId —
            # si hay cualquier SL activo para este symbol no colocar otro
            if o.get('symbol', '') != symbol:
                continue
            sl = float(o.get('slPrice', 0) or 0)
            if sl > 0:
                print(f"  ✅ [RECOVERY] {symbol} ya tiene SL activo @ {sl} — NO se coloca otro")
                return str(o.get('id', o.get('orderId', '')))
    except Exception as e:
        print(f"  ⚠️  [RECOVERY] Error verificando TPSL: {e}")

    # ── 2. Verificar TPs en trade/get_pending_orders ──────────
    try:
        pending_data = await _get(session, '/api/v1/futures/trade/get_pending_orders', params)
        pending_list = pending_data if isinstance(pending_data, list) else \
                       (pending_data or {}).get('orderList', [])
        for o in pending_list:
            pid = str(o.get('positionId', ''))
            if position_id and pid and pid != str(position_id):
                continue
            if o.get('reduceOnly', False):
                print(f"  ✅ [RECOVERY] {symbol} ya tiene TP activo — NO se coloca otro")
                return ''
    except Exception as e:
        print(f"  ⚠️  [RECOVERY] Error verificando TPs: {e}")

    # ── 3. No hay nada — colocar TPs + SL SOLO UNA VEZ ────────
    print(f"  🔄 [RECOVERY] {symbol} sin TPs/SL — colocando: SL:{sl_price:.8g} TP1:{tp1_price:.8g} TP3:{tp3_price:.8g}")
    
    # ✅ Solo 1 intento (no 3) para evitar duplicados
    result = await place_partial_tpsl(
        session, symbol, position_id, side,
        sl_price, tp1_price, tp3_price,
        qty, price_prec, qty_prec
    )
    if result.get('success'):
        print(f"  ✅ [RECOVERY] TPs/SL colocados exitosamente")
        return result.get('sl_id', '')
    else:
        print(f"  🚨 [RECOVERY] {symbol} — no se pudo colocar TPs/SL. Coloca manualmente.")
        return ''


async def _limpiar_sl_duplicados(session):
    """
    Detecta y cancela SLs duplicados para el mismo símbolo+positionId.
    Se ejecuta UNA VEZ al arrancar el bot.
    """
    print("  🔍 [RECOVERY] Buscando SLs duplicados...")
    try:
        tpsl_data = await _get(session, '/api/v1/futures/tpsl/get_pending_orders', {})
        tpsl_list = tpsl_data if isinstance(tpsl_data, list) else \
                    (tpsl_data or {}).get('orderList', [])

        grupos = {}
        for o in tpsl_list:
            sl = float(o.get('slPrice', 0) or 0)
            if sl <= 0:
                continue
            symbol = o.get('symbol', '')
            oid    = str(o.get('id', o.get('orderId', '')))
            ctime  = int(o.get('ctime', 0) or 0)
            # Agrupar solo por symbol — detecta duplicados aunque
            # tengan distinto positionId (caso típico tras reinicio)
            if symbol not in grupos:
                grupos[symbol] = []
            grupos[symbol].append({'id': oid, 'sl': sl, 'ctime': ctime, 'symbol': symbol})

        cancelados = 0
        for clave, sls in grupos.items():
            if len(sls) <= 1:
                continue
            sls.sort(key=lambda x: x['ctime'], reverse=True)
            sym = sls[0]['symbol']
            print(f"  ⚠️  [RECOVERY] {sym} tiene {len(sls)} SLs duplicados — cancelando los más viejos")
            for sl_viejo in sls[1:]:
                ok = await cancel_order_by_id(session, sym, sl_viejo['id'])
                if ok:
                    print(f"  🗑️  [RECOVERY] SL duplicado cancelado: {sl_viejo['id']} @ {sl_viejo['sl']}")
                    cancelados += 1

        if cancelados == 0:
            print("  ✅ [RECOVERY] Sin SLs duplicados")
        else:
            print(f"  ✅ [RECOVERY] {cancelados} SL(s) duplicado(s) cancelados")

    except Exception as e:
        print(f"  ⚠️  [RECOVERY] Error limpiando duplicados: {e}")


async def _was_position_closed(session, symbol, position_id) -> bool:
    """Verifica si una posición cerró consultando el historial de posiciones."""
    try:
        data = await _get(session, '/api/v1/futures/position/get_history_positions', {'symbol': symbol})
        positions = data if isinstance(data, list) else (data or {}).get('positionList', [])
        for p in positions:
            if str(p.get('positionId', '')) == str(position_id):
                return True
    except Exception as e:
        print(f"  ⚠️  No se pudo verificar historial: {e}")
    return False


async def recover_positions(session: aiohttp.ClientSession, monitor) -> int:
    print("\n  🔄 [RECOVERY] Verificando posiciones abiertas...")

    # Limpiar SLs duplicados antes de todo
    await _limpiar_sl_duplicados(session)

    saved_state = load_state()

    # Obtener posiciones abiertas
    try:
        open_positions = await get_all_open_positions(session)
        if not open_positions:
            print("  ⚠️  [RECOVERY] No se obtuvieron posiciones del exchange")
            open_positions = []
    except Exception as e:
        print(f"  ⚠️  [RECOVERY] Error obteniendo posiciones: {e}")
        open_positions = []

    # Si el exchange no devuelve posiciones abiertas, limpiar estado y salir
    if not open_positions:
        print("  ⚠️  [RECOVERY] Exchange no devolvió posiciones abiertas")

        # Verificar cada posición guardada contra historial REAL antes de borrar
        cerradas = []
        for key, v in saved_state.items():
            symbol = v.get('symbol', '')
            pid = str(v.get('position_id', ''))
            try:
                cerro = await _was_position_closed(session, symbol, pid) if pid else True
            except Exception as e:
                print(f"  ⚠️  [RECOVERY] Error de red al verificar {symbol}: {e} — conservando por seguridad.")
                cerro = False

            if cerro:
                print(f"  🗑️  [RECOVERY] {symbol} eliminada (confirmado en historial)")
                cerradas.append(key)
            else:
                print(f"  ⚠️  [RECOVERY] {symbol} — historial falló, conservando por seguridad")

        for key in cerradas:
            del saved_state[key]
        if cerradas:
            save_state(saved_state)

        print("  ✅ [RECOVERY] No hay posiciones abiertas confirmadas en Bitunix\n")
        return 0

    # Construir set de posiciones reales usando positionId (más preciso que symbol+side)
    open_pids = set()
    open_set  = set()
    for pos in open_positions:
        sym = pos.get('symbol', '')
        ps  = str(pos.get('side', pos.get('holdSide', ''))).upper()
        side = 'LONG' if ps in ('BUY', 'LONG') else 'SHORT'
        pid  = str(pos.get('positionId', ''))
        open_set.add((sym, side))
        if pid:
            open_pids.add(pid)

    # ✅ Limpiar estado.json: cualquier posición que NO esté en el exchange → borrar
    # El exchange es la verdad absoluta. No se consulta historial — si no está abierta, cerró.
    cerradas = []
    for key, data in list(saved_state.items()):
        sym  = data.get('symbol', '')
        side = data.get('side', '')
        pid  = str(data.get('position_id', ''))

        en_exchange = (pid and pid in open_pids) or ((sym, side) in open_set)
        if not en_exchange:
            cerradas.append(key)
            print(f"  🗑️  [RECOVERY] {sym} {side} no está en exchange → eliminado de estado.json")

    for key in cerradas:
        del saved_state[key]
    if cerradas:
        save_state(saved_state)

    if not open_positions:
        print("  ✅ [RECOVERY] No hay posiciones abiertas\n")
        return 0

    print(f"  📊 [RECOVERY] {len(open_positions)} posición(es) abierta(s) en Bitunix")

    # Obtener balance actual
    from bitunix_api import get_balance, MAX_TRADES
    current_balance = await get_balance(session)

    # Respetar límite MAX_TRADES
    if len(open_positions) > MAX_TRADES:
        print(f"  ⚠️  Hay {len(open_positions)} posiciones pero el límite es {MAX_TRADES} — recuperando solo las primeras {MAX_TRADES}")
        open_positions = open_positions[:MAX_TRADES]

    recovered = 0

    for pos in open_positions:
        try:
            symbol = pos.get('symbol', '')
            ps = str(pos.get('side', pos.get('holdSide', ''))).upper()
            side = 'LONG' if ps in ('BUY', 'LONG') else 'SHORT'

            entry_price = float(pos.get('avgOpenPrice', pos.get('entryPrice', pos.get('openPrice', 0))))
            qty = float(pos.get('qty', pos.get('available', 0)))
            position_id = str(pos.get('positionId', ''))

            if not symbol or not entry_price or qty <= 0:
                continue

            # ✅ Ignorar posiciones que monitor ya marcó como cerrando
            # (monitor detectó que no existen pero aún no completó las 3 confirmaciones)
            for k, v in saved_state.items():
                pid_saved = str(v.get('position_id', ''))
                sym_match = v.get('symbol') == symbol and v.get('side') == side
                pid_match = position_id and pid_saved == position_id
                if (pid_match or sym_match) and v.get('_cerrando', False):
                    print(f"  ⏭️  [RECOVERY] {symbol} {side} marcada como cerrando — ignorando")
                    symbol = ''  # forzar skip en el if de abajo
                    break

            if not symbol:
                continue

            print(f"  📌 {symbol} {side} | Entry:{entry_price:.8g} | Qty:{qty} | ID:{position_id}")

            prec = await get_symbol_precision(session, symbol)
            qty_prec = prec['qty_prec']
            price_prec = prec['price_prec']

            # 🔥 PASO 1: LEER SL REAL DEL EXCHANGE (VERDAD ABSOLUTA)
            real_sl, real_sl_id = await _get_sl_from_exchange(session, symbol, position_id)
            
            # 🔥 PASO 2: LEER TPs DEL EXCHANGE (solo para detectar estado operativo)
            # tp_count indica cuántas órdenes LIMIT activas hay:
            #   2 → ambos TPs pendientes  (TP1 no se ejecutó)
            #   1 → solo queda TP3        (TP1 ya se ejecutó)
            #   0 → sin TPs en exchange
            # NUNCA usar real_tp1/real_tp3 como niveles matemáticos — solo estado.json.
            real_tp1, real_tp3, tp_count = await _get_tps_from_exchange(session, symbol, position_id, side)
            if tp_count == 1:
                print(f"  🔍 [RECOVERY] {symbol} — solo 1 orden LIMIT activa → TP1 ya ejecutado (monitor lo detectará)")

            # 🔥 PASO 3: Si hay SL real, ese es el que se usa
            if real_sl > 0:
                stop = real_sl
                sl_id = real_sl_id
                print(f"  ✅ SL real desde exchange: {stop:.8g}")
            else:
                # No hay SL en exchange, buscar en estado guardado o señales TXT
                sl_id = ''
                # Buscar en estado guardado
                saved = None
                for k, v in saved_state.items():
                    if v.get('symbol') == symbol and v.get('side') == side:
                        saved = {**v, 'key': k}
                        break
                
                if saved:
                    stop = saved.get('stop', entry_price)
                    print(f"  📁 SL desde estado.json: {stop:.8g}")
                else:
                    # Buscar en TXT señales
                    txt_signals = _load_txt_signals()
                    txt_key = f"{symbol}_{side}"
                    txt_signal = txt_signals.get(txt_key)
                    if txt_signal:
                        stop = float(txt_signal.get('stop', entry_price))
                        print(f"  📁 SL desde TXT señales: {stop:.8g}")
                    else:
                        stop = entry_price * (0.99 if side == 'LONG' else 1.01)
                        print(f"  ⚠️  SL calculado por defecto: {stop:.8g}")

            # 🔥 PASO 4: TPs — estado.json es la ÚNICA fuente de verdad para niveles matemáticos.
            # PRINCIPIO CLAVE:
            #   Exchange = estado operativo (qty real, SL real, posición viva, TP ejecutado)
            #   Bot      = memoria histórica (tp1_original, tp3_original, entry_original)
            #
            # Si TP1 ya se ejecutó (tp_count == 1), la orden restante en el exchange ES TP3.
            # En ese caso real_tp1=0. NUNCA interpretar real_tp3 como tp1.
            # Siempre leer tp1_original y tp3_original de estado.json para cálculos.
            saved_for_tps = None
            for k, v in saved_state.items():
                if v.get('symbol') == symbol and v.get('side') == side:
                    saved_for_tps = v
                    break

            if saved_for_tps and saved_for_tps.get('tp1_original', 0) > 0:
                # ✅ Primera opción: tp1_original/tp3_original guardados explícitamente
                tp1 = saved_for_tps['tp1_original']
                tp3 = saved_for_tps.get('tp3_original', 0)
                print(f"  📁 TPs desde estado.json (tp1_original): TP1:{tp1:.8g} TP3:{tp3:.8g}")
            elif saved_for_tps and saved_for_tps.get('tp1', 0) > 0:
                # ✅ Segunda opción: tp1/tp3 guardados en estado.json
                tp1 = saved_for_tps['tp1']
                tp3 = saved_for_tps.get('tp3', 0)
                print(f"  📁 TPs desde estado.json: TP1:{tp1:.8g} TP3:{tp3:.8g}")
            elif real_tp1 > 0 and real_tp3 > 0:
                # ✅ Fallback SOLO si exchange confirma 2 órdenes activas (tp_count == 2)
                # → ambos TPs pendientes, seguro leer desde exchange
                tp1, tp3 = real_tp1, real_tp3
                print(f"  ✅ TPs desde exchange (2 órdenes activas): TP1:{tp1:.8g} TP3:{tp3:.8g}")
            else:
                # Último recurso: calcular desde entry/sl
                txt_signals = _load_txt_signals()
                txt_key = f"{symbol}_{side}"
                txt_signal = txt_signals.get(txt_key)
                if txt_signal:
                    tps = txt_signal.get('tps', {})
                    tp1 = float(tps.get('tp1', 0))
                    tp3 = float(tps.get('tp3', 0))
                    print(f"  📁 TPs desde TXT señales: TP1:{tp1:.8g} TP3:{tp3:.8g}")
                else:
                    tp1, tp3 = calc_tps(entry_price, stop, side)
                    tp1 = round(tp1, price_prec)
                    tp3 = round(tp3, price_prec)
                    print(f"  🔄 TPs calculados: TP1:{tp1:.8g} TP3:{tp3:.8g}")

            # 🔥 PASO 5: VERIFICAR Y CORREGIR SL
            # Los TPs son órdenes límite — el exchange los gestiona solo, recovery no los toca.
            # Solo se actúa sobre el SL:
            #   - Si hay SL en exchange pero no coincide con expected_sl → MODIFICAR
            #   - Si no hay SL en exchange → RECOLOCAR (único caso extremo)
            from bitunix_api import modify_sl as _modify_sl
            tpsl_id_final = sl_id

            # Leer expected_sl desde estado.json
            saved_expected = None
            for k, v in saved_state.items():
                if v.get('symbol') == symbol and v.get('side') == side:
                    saved_expected = v.get('expected_sl') or v.get('stop', 0)
                    break

            if real_sl > 0 and saved_expected and saved_expected > 0:
                diff = abs(real_sl - saved_expected)
                if diff > (10 ** -price_prec):
                    print(f"  🔄 [RECOVERY] {symbol} SL desactualizado: exchange={real_sl:.8g} esperado={saved_expected:.8g} — modificando")
                    result = await _modify_sl(session, symbol, position_id, saved_expected, price_prec)
                    ok = result.get("success", False) if isinstance(result, dict) else bool(result)
                    if ok:
                        stop = saved_expected
                        print(f"  ✅ [RECOVERY] SL modificado a {saved_expected:.8g}")
                    else:
                        print(f"  ⚠️  [RECOVERY] No se pudo modificar SL — se usa el del exchange: {real_sl:.8g}")
                else:
                    print(f"  ✅ [RECOVERY] SL en exchange coincide con expected_sl: {real_sl:.8g}")
            elif real_sl == 0 and stop > 0:
                print(f"  🚨 [RECOVERY] {symbol} sin SL en exchange — recolocando")
                tpsl_id_final = await _recolocar_tpsl_si_falta(
                    session, symbol, position_id, side,
                    stop, tp1, tp3, qty, price_prec, qty_prec
                )

            # 🔥 PASO 6: AÑADIR AL MONITOR — SOLO ESTADO ESTRUCTURAL
            # ❌ NO decidimos TP1 ni BE aquí
            # ✅ MONITOR es quien detecta tp1_hit y sl_modified_to_breakeven
            key = f"{symbol}_{side}_{position_id}"
            monitor.add(key, {
                # Datos estructurales básicos
                'symbol': symbol,
                'side': side,
                'entry': entry_price,
                'stop': stop,
                'tp1': tp1,
                'tp3': tp3,
                
                # Niveles originales inmutables (para cálculos del 70% TP1→TP3)
                'tp1_original': tp1,
                'tp3_original': tp3,
                
                # IDs del exchange
                'position_id': position_id,
                'sl_id': tpsl_id_final if tpsl_id_final else '',
                
                # Cantidades y precisiones
                'qty': qty,
                'price_prec': price_prec,
                'qty_prec': qty_prec,
                
                # Estado financiero
                'balance_before': current_balance,
                
                # Estado del SL (solo técnico, MONITOR decide los flags lógicos)
                'expected_sl': stop,
                'sl_last_update_time': time.time(),
                
                # ❌ ELIMINADO: 'sl_modified_to_breakeven'
                # ❌ ELIMINADO: 'sl_modified_to_tp1'
                # ❌ ELIMINADO: 'tp1_hit'
                # ❌ ELIMINADO: 'qty_original'
            })
            recovered += 1
            
        except Exception as e:
            print(f"  ⚠️  [RECOVERY] Error recuperando posición {pos.get('symbol', 'unknown')}: {e}")
            import traceback
            traceback.print_exc()
            continue

    if recovered > 0:
        print(f"  ✅ [RECOVERY] {recovered} posición(es) recuperada(s) y en monitoreo")
        print(f"  🔵 [RECOVERY] MONITOR se encargará de detectar TP1 y BE automáticamente")
    print()
    return recovered