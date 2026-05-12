"""
bitunix_bot.py — Bot principal Bitunix Futures
CORRECCIONES:
[BUG1/2] Keys de posición ya no tienen espacio al final
[BUG3]   execute_signal NO usa partial — signal_reader recibe execute_signal directo
y lo llama como execute_fn(sig) — sesión viene del propio reader
[WARN3]  Reemplazados import('json').load(open(...)) por with open(...) correctamente
[MEJORA] eliminado flujo de analisis_tecnico — solo Filtro Maestro
para evitar doble impresión
[TPS_PARCIALES V2] Implementación de 2 TPs parciales (60%/40%) con batch_order
[TPS_BOTAI] Usa los TPs enviados por el BOTAI (TP1 y TP3), TP2 solo como referencia interna
[VISUAL_PRO] (deshabilitado) ya no se usa mostrar_analisis
[DEDUP] Agregada verificación de duplicados (mismo símbolo + dirección en últimos 5 min)
[CONSOLA] Muestra los TPs REALES que se enviaron a Bitunix
[FIX] NO recalcula TP3, usa SOLO los TPs del BOTAI
[FIX2] legacy: mostrar_analisis ya no aplica
[COOLDOWN] Eliminado: BOTAI ya controla duplicados del mismo símbolo
[V2_SYNC] Corregidas llamadas a place_partial_tpsl: ahora 3 precios (sl, tp1, tp3)
[BUG11_FIX] Añadido modo_filtro a la señal para que adaptive_filter no bloquee señales FLEXIBLE
[BUG_FLEXIBLE_FIX] CORRECCIÓN CRÍTICA: señales FLEXIBLE aprobadas ahora se ejecutan correctamente.
  - Antes: analisis se reasignaba con 'or' que podía fallar si _analisis_precomputed
    era None/vacío, causando "Señal sin resultado de filtro maestro → se omite".
  - Ahora: se usa una sola asignación defensiva con fallback explícito.
  - Antes: la verificación 'aceptada' bloqueaba señales FLEXIBLE cuando
    _analisis_precomputed no estaba seteado correctamente.
  - Ahora: si modo_filtro es FLEXIBLE, se bypassea la verificación de aceptada
    igual que adaptive_filter, ya que el signal_reader ya validó la aprobación.
"""
import asyncio, aiohttp, time, os, json, re, sys
from dotenv import load_dotenv


from bitunix_api import (
    test_connection, get_balance, print_balance,
    get_symbol_precision, get_open_position,
    set_leverage_isolated, calc_qty, effective_margin,
    place_market_order, place_partial_tpsl,
    calc_tps,  # fallback cuando la señal no trae TPs
    can_open_trade, MAX_TRADES, MARGIN_PER_TRADE, LEVERAGE, TP_RR, _floor,
)
from signal_reader import SignalReader
from position_monitor import PositionMonitor
from recovery import recover_positions, load_state, save_state
from capital_manager import get_current_capital, is_symbol_blocked, is_direction_blocked, print_capital_status, BASE_CAPITAL
from adaptive_filter import adaptive_filter
from time_filters import time_filter
from console_util import safe_print as _console_safe_print
from signal_logger import log_resultado

load_dotenv()

monitor        = None  # se inicializa en main()
signal_reader  = None  # se inicializa en main()
http_session: aiohttp.ClientSession | None = None
_balance_actual: float = 0.0

# ═══════════════════════════════════════════════════════════════
# CONTROL DE DEDUPLICACIÓN (evita re-ejecutar misma señal)
# ═══════════════════════════════════════════════════════════════
_last_executed_signals = {}  # {f"{symbol}_{side}": timestamp}
_DEDUP_WINDOW_SECONDS = 300  # 5 minutos

def _check_and_record_signal(symbol: str, side: str) -> bool:
    """Retorna True si es duplicado (no ejecutar), False si es nueva señal."""
    key = f"{symbol}_{side}"
    now = time.time()
    last_time = _last_executed_signals.get(key, 0)
    if now - last_time < _DEDUP_WINDOW_SECONDS:
        return True  # Duplicado
    _last_executed_signals[key] = now
    # Limpiar entradas viejas
    expired = [k for k, t in _last_executed_signals.items() if now - t > _DEDUP_WINDOW_SECONDS * 2]
    for k in expired:
        del _last_executed_signals[k]
    return False  # Nueva señal


_adaptive_log_last     = 0.0
_adaptive_log_interval = 60.0

def _should_log_adaptive():
    global _adaptive_log_last
    now = time.time()
    if now - _adaptive_log_last >= _adaptive_log_interval:
        _adaptive_log_last = now
        return True
    return False

def _log_adaptive(message: str, force: bool = False):
    if force or _should_log_adaptive():
        print(message)

_ws_clients: set = set()
_console_log: list = []
_MAX_LOG      = 200

try:
    import websockets as _websockets
    _WS_AVAILABLE = True
except ImportError:
    _WS_AVAILABLE = False

def _build_snapshot() -> dict:
    positions = []
    for key, pos in monitor._positions.items():
        positions.append({
            'key':           key,
            'symbol':        pos.get('symbol', ''),
            'side':          pos.get('side', ''),
            'entry':         pos.get('entry', 0),
            'stop':          pos.get('stop', 0),
            'tp1':           pos.get('tp1', 0),
            'tp3':           pos.get('tp3', 0),  # V2: solo TP1 y TP3
            'qty':           pos.get('qty', 0),
            'qty_prec':      pos.get('qty_prec', 3),
            'price_prec':    pos.get('price_prec', 4),
            'timeframe':     pos.get('timeframe', '?'),
            'capital':       pos.get('capital_usado', 4.0),
            'current_price': pos.get('current_price', pos.get('entry', 0)),
            'sl_inicial':    pos.get('sl_inicial', pos.get('stop', 0)),
        })

    BOT_DIR   = os.path.dirname(os.path.abspath(__file__))
    stats     = _read_json(os.path.join(BOT_DIR, 'estadisticas.json'))
    capital   = _read_json(os.path.join(BOT_DIR, 'capital.json'))
    adaptive  = _read_json(os.path.join(BOT_DIR, 'adaptive_state.json'))

    ops_stats = stats.get('operaciones', [])

    total    = stats.get('total', 0)
    ganadas  = stats.get('ganadas', 0)
    perdidas = stats.get('perdidas', 0)
    ganado   = round(stats.get('total_ganado', 0.0), 4)
    perdido  = round(stats.get('total_perdido', 0.0), 4)

    wr         = round(ganadas / total * 100, 1) if total > 0 else 0
    cap_actual = capital.get('capital', BASE_CAPITAL)

    balance_real = _balance_actual if _balance_actual > 0 else cap_actual

    blocked    = capital.get('blocked_symbols', {})
    ahora      = time.time()
    blocked_active = {k: round((v - ahora) / 60) for k, v in blocked.items() if v > ahora}

    score_min   = adaptive.get('score_min', {})
    blocked_tfs = adaptive.get('blocked_tfs', [])
    pen_scores  = adaptive.get('penalized_scores', [])
    blocked_hrs = adaptive.get('blocked_hours', [])

    ultima = ops_stats[-1] if ops_stats else None

    return {
        'type':      'snapshot',
        'ts':        time.strftime('%H:%M:%S'),
        'positions': positions,
        'stats': {
            'total':    total,
            'ganadas':  ganadas,
            'perdidas': perdidas,
            'ganado':   ganado,
            'perdido':  perdido,
            'neto':     round(ganado + perdido, 4),
            'wr':       wr,
        },
        'capital': {
            'actual':       round(cap_actual, 4),
            'balance_real': round(balance_real, 4),
            'blocked':      blocked_active,
        },
        'adaptive': {
            'score_1h':    score_min.get('1h', 95),
            'score_15m':   score_min.get('15m', 92),
            'blocked_tfs': blocked_tfs,
            'pen_scores':  pen_scores,
            'blocked_hrs': len(blocked_hrs),
        },
        'ultima_op': {
            'symbol': ultima['symbol'],
            'side':   ultima['side'],
            'pnl':    ultima['pnl'],
            'ganada': ultima.get('ganada', False),
        } if ultima else None,
        'log': list(_console_log),
    }

def _read_json(path: str) -> dict:
    try:
        if not os.path.exists(path):
            return {}
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {}

def _log_line(text: str, cls: str = 'c-white'):
    entry = {'text': text, 'cls': cls, 'ts': time.strftime('%H:%M:%S')}
    _console_log.append(entry)
    if len(_console_log) > _MAX_LOG:
        _console_log.pop(0)
    if _ws_clients:
        asyncio.create_task(_ws_broadcast({'type': 'log', 'lines': [entry]}))

async def _ws_broadcast(msg: dict):
    if not _ws_clients:
        return
    data = json.dumps(msg, ensure_ascii=False)
    dead = set()
    for ws in list(_ws_clients):
        try:
            await ws.send(data)
        except:
            dead.add(ws)
    _ws_clients.difference_update(dead)

async def _ws_handler(ws):
    _ws_clients.add(ws)
    try:
        await ws.send(json.dumps(_build_snapshot()))
        async for _ in ws:
            pass
    except:
        pass
    finally:
        _ws_clients.discard(ws)

async def _ws_poll_loop():
    global _balance_actual
    _tick = 0
    while True:
        await asyncio.sleep(3)
        _tick += 1
        if _tick % 5 == 0 and http_session and not http_session.closed:
            try:
                _balance_actual = await get_balance(http_session)
            except Exception:
                pass
        if _ws_clients:
            await _ws_broadcast(_build_snapshot())

async def _start_dashboard():
    if not _WS_AVAILABLE:
        print("  ⚠️  [DASHBOARD] websockets no instalado — pip install websockets")
        print("      El bot funciona igual, solo sin dashboard web.\n")
        return
    try:
        server = await _websockets.serve(_ws_handler, 'localhost', 8765)
        print("  📊 [DASHBOARD] Servidor activo en ws://localhost:8765")
        print("      Abre dashboard.html en el navegador\n")
        asyncio.create_task(_ws_poll_loop())
    except OSError:
        print("  ⚠️  [DASHBOARD] Puerto 8765 ocupado — dashboard desactivado\n")

import builtins

_original_print = builtins.print
_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trading_bot.log')
_SYMBOL_RE = re.compile(r'\b([A-Z]{2,12}USDT)\b')

def _extract_symbol(text: str) -> str:
    match = _SYMBOL_RE.search(text.upper())
    return match.group(1) if match else 'GLOBAL'

def _write_trading_log(text: str):
    try:
        lines = [ln.strip() for ln in str(text).splitlines() if ln.strip()]
        if not lines:
            return
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
        with open(_LOG_FILE, 'a', encoding='utf-8') as f:
            for line in lines:
                if any(x in line for x in ['╔', '╠', '╚', '║', '░', '█']):
                    continue
                symbol = _extract_symbol(line)
                f.write(f"{timestamp} | {symbol} | {line}\n")
    except:
        pass

def _patched_print(*args, **kwargs):
    kw = dict(kwargs)
    file = kw.pop('file', sys.stdout)
    sep = kw.pop('sep', ' ')
    end = kw.pop('end', '\n')
    flush = kw.pop('flush', False)
    if file is not sys.stdout or kw:
        _original_print(*args, sep=sep, end=end, flush=flush, file=file, **kw)
    else:
        line = sep.join(str(a) for a in args)
        _console_safe_print(line, end=end, flush=flush)
    try:
        text = ' '.join(str(a) for a in args)
        if not text.strip():
            return
        _write_trading_log(text)
        if any(x in text for x in ['✅','APROBADA','GANANCIA','PROTEGIDA','ABIERTA','OK ']):
            cls = 'c-green'
        elif any(x in text for x in ['❌','RECHAZADA','PÉRDIDA','Error','bloqueado']):
            cls = 'c-red'
        elif any(x in text for x in ['⚠️','ALERTA','Cooldown','movido','ADAPTIVE','🔒']):
            cls = 'c-yellow'
        elif any(x in text for x in ['🔬','ANÁLISIS','📊','Score']):
            cls = 'c-purple'
        elif any(x in text for x in ['⭐','EJECUTANDO','MARKET','Entry','SL','TP']):
            cls = 'c-blue'
        elif any(x in text for x in ['🔍','[READER]','[MONITOR]','[RECOVERY]']):
            cls = 'c-cyan'
        elif '─'*4 in text or '═'*4 in text:
            cls = 'c-sep'
        else:
            cls = 'c-white'

        skip_patterns = [
            '╔', '╠', '╚', '║',
            'MONITOREO DE POSICIONES',
            'Sin posiciones abiertas',
            'Saldo: $',
            'ZONA SEGURA', 'EN CAMINO', 'PELIGRO',
            'Capital: $', 'Ganancia: +$', 'Ganancia: -$',
            'SL:', 'TP:', 'Entry:', 'Precio:',
            '░', '█',
        ]
        if any(p in text for p in skip_patterns):
            return

        _log_line(text, cls)
    except:
        pass

builtins.print = _patched_print

_last_known_positions    = set()
_closed_trades_registered = set()
_signal_by_key           = {}
_open_timestamp_by_key   = {}

async def _record_closed_trades_loop():
    global _last_known_positions, _closed_trades_registered
    while True:
        await asyncio.sleep(10)
        if not monitor._positions:
            _last_known_positions.clear()
            continue

        current_keys = set(monitor._positions.keys())
        closed_keys  = _last_known_positions - current_keys
        for key in closed_keys:
            if key in _closed_trades_registered:
                continue
            signal = _signal_by_key.get(key)
            if signal:
                stats    = _read_json('estadisticas.json')
                ops      = stats.get('operaciones', [])
                ts_open  = _open_timestamp_by_key.get(key, 0)
                for op in reversed(ops):
                    if (op.get('symbol') == signal.get('symbol') and
                            op.get('side') == signal.get('side')):
                        op_ts = op.get('timestamp')
                        if op_ts is None:
                            fecha_str = op.get('fecha', '')
                            try:
                                op_ts = time.mktime(time.strptime(fecha_str, '%Y-%m-%d %H:%M:%S'))
                            except:
                                op_ts = 0
                        if abs(op_ts - ts_open) < 120:
                            win = op.get('ganada', False)
                            pnl = op.get('pnl', 0.0)
                            duracion = op.get('duracion_min', 0.0)
                            log_resultado(
                                symbol      = signal.get('symbol', '?'),
                                side        = signal.get('side', '?'),
                                entry       = signal.get('entry', 0),
                                resultado   = 'WIN' if win else 'LOSS',
                                pnl         = pnl,
                                duracion_min= duracion
                            )
                            adaptive_filter.record_trade(signal, {'win': win, 'pnl': pnl})
                            _closed_trades_registered.add(key)
                            _log_adaptive(
                                f"  🧠 [ADAPTIVE] Operación registrada: "
                                f"{signal['symbol']} {signal['side']} → {win} (pnl={pnl:.4f})"
                            )
                            break
        _last_known_positions = current_keys


# ═══════════════════════════════════════════════════════════════
# REINTENTO TPS PARCIALES EN BACKGROUND (V2: 2 TPs)
# ═══════════════════════════════════════════════════════════════
async def _retry_place_partial_tpsl(session, symbol, side, position_id,
                                     sl_price, tp1_price, tp3_price,  # ✅ V2: solo 3 precios
                                     total_qty, price_prec, qty_prec, key):
    """
    Si place_partial_tpsl falló al abrir, reintenta en background cada 30s
    durante máximo 5 minutos. Actualiza el monitor cuando lo logra.
    """
    MAX_ESPERA = 5 * 60
    INTERVALO  = 30
    inicio     = time.time()

    print(f"  🔄 [TPS_PARCIALES] Reintento background para {symbol} {side}...")

    while time.time() - inicio < MAX_ESPERA:
        await asyncio.sleep(INTERVALO)

        pos = monitor._positions.get(key)
        if not pos:
            print(f"  ℹ️  [TPS_PARCIALES] {symbol} ya no está en monitor — cancelando reintento")
            return

        # Verificar si ya tiene IDs guardados
        if pos.get('tp1_id') and pos.get('sl_id'):
            print(f"  ✅ [TPS_PARCIALES] {symbol} ya tiene TPs/SL con IDs — reintento cancelado")
            return

        # Si tiene SL pero no tp1_id → leer IDs del exchange y guardarlos
        if pos.get('sl_id') and not pos.get('tp1_id'):
            print(f"  🔍 [TPS_PARCIALES] {symbol} tiene SL pero sin tp1_id — leyendo IDs del exchange...")
            try:
                from bitunix_api import _get, get_active_tpsl_orders
                params = {'symbol': symbol, 'positionId': str(position_id)}
                orders_data = await _get(session, '/api/v1/futures/trade/get_pending_orders', params)
                orders = orders_data if isinstance(orders_data, list) else (orders_data or {}).get('orderList', [])
                limit_orders = sorted(
                    [o for o in orders if o.get('reduceOnly') and str(o.get('orderType','')).upper() == 'LIMIT'],
                    key=lambda o: float(o.get('price', 0))
                )
                if len(limit_orders) >= 2:
                    if side == 'LONG':
                        tp1_order = limit_orders[0]
                        tp3_order = limit_orders[-1]
                    else:
                        tp1_order = limit_orders[-1]
                        tp3_order = limit_orders[0]
                    state = monitor._positions.get(key, {})
                    state['tp1_id'] = str(tp1_order.get('orderId', tp1_order.get('id', '')))
                    state['tp3_id'] = str(tp3_order.get('orderId', tp3_order.get('id', '')))
                    monitor._positions[key] = state
                    monitor._persist()
                    print(f"  ✅ [TPS_PARCIALES] IDs guardados — tp1_id:{state['tp1_id']} tp3_id:{state['tp3_id']}")
                    return
                elif len(limit_orders) == 1:
                    state = monitor._positions.get(key, {})
                    state['tp3_id'] = str(limit_orders[0].get('orderId', limit_orders[0].get('id', '')))
                    monitor._positions[key] = state
                    monitor._persist()
                    print(f"  ✅ [TPS_PARCIALES] Solo TP3 encontrado (TP1 ya ejecutado) — tp3_id:{state['tp3_id']}")
                    return
            except Exception as e:
                print(f"  ⚠️  [TPS_PARCIALES] Error leyendo IDs: {e}")
            return

        # ✅ V2: place_partial_tpsl recibe 3 precios (sl, tp1, tp3)
        result = await place_partial_tpsl(
            session, symbol, position_id, side,
            sl_price, tp1_price, tp3_price,
            total_qty, price_prec, qty_prec
        )

        if result.get('success'):
            state = monitor._positions.get(key, {})
            state['tp1_id'] = result.get('tp1_id', '')
            state['tp3_id'] = result.get('tp3_id', '')  # ✅ V2: sin tp2_id
            state['sl_id']  = result.get('sl_id', '')
            state['tp1']    = tp1_price
            state['tp3']    = tp3_price
            state['expected_sl'] = sl_price
            state['sl_last_update_time'] = time.time()
            monitor._positions[key] = state
            monitor._persist()
            print(f"  ✅ [TPS_PARCIALES] TPs/SL colocados en reintento: {symbol}")
            print(f"      TP1:{tp1_price:.8g} TP3:{tp3_price:.8g} SL:{sl_price:.8g}")
            return
        else:
            elapsed = int(time.time() - inicio)
            print(f"  ⚠️  [TPS_PARCIALES] {symbol} sin TPs/SL ({elapsed}s) — reintentando en {INTERVALO}s...")

    print(f"  🚨 [TPS_PARCIALES] {symbol} — timeout 5min sin TPs/SL. Revisar manualmente en Bitunix.")


# ═══════════════════════════════════════════════════════════════
# EJECUTAR SEÑAL (MODIFICADO CON VISUALIZACIÓN PRO - V2)
# ═══════════════════════════════════════════════════════════════
async def execute_signal(sig: dict) -> bool:
    """Ejecuta una señal. La sesión HTTP viene del closure de main()."""
    global _signal_by_key, _open_timestamp_by_key, _balance_actual
    session = http_session
    if session is None or session.closed:
        print("  ❌ [execute_signal] Sesión HTTP no disponible")
        return False

    symbol = sig['symbol']
    side   = sig['side']
    entry  = sig['entry']
    stop   = sig['stop']

    # ══════════════════════════════════════════════════════════════
    # 🔥 CORRECCIÓN BUG_FLEXIBLE_FIX:
    # Obtener el resultado del filtro maestro de forma defensiva.
    # Prioridad: _analisis_precomputed → _filtro_maestro_result → None
    # No usar 'or' que puede fallar si _analisis_precomputed es un dict vacío {}
    # (en Python {} es falsy, aunque raro, puede ocurrir en condiciones de borde).
    # ══════════════════════════════════════════════════════════════
    analisis = sig.get('_analisis_precomputed')
    if analisis is None:
        analisis = sig.get('_filtro_maestro_result')

    # Extraer modo_filtro del resultado del filtro maestro
    # Si no hay analisis disponible aún, asumir ESTRICTO
    modo_filtro = 'ESTRICTO'
    if analisis and isinstance(analisis, dict):
        modo_filtro = analisis.get('modo_filtro', 'ESTRICTO')

    # Propagar modo_filtro a la señal para que adaptive_filter pueda usarlo
    sig['modo_filtro'] = modo_filtro

    # Si es modo FLEXIBLE, mostrar mensaje informativo
    if modo_filtro == 'FLEXIBLE':
        print(f"  🔥 [FLEXIBLE] Señal {symbol} {side} aprobada por modo flexible → ejecutando")

    # ──────────────────────────────────────────────────────────
    # DEDUPLICACIÓN: evitar misma señal en los últimos 5 min
    # Las señales FLEXIBLE no se bloquean por dedup — ya fueron
    # aprobadas por su propio filtro y tienen prioridad de ejecución.
    # ──────────────────────────────────────────────────────────
    if modo_filtro != 'FLEXIBLE' and _check_and_record_signal(symbol, side):
        print(f"  ⏳ {symbol} {side} — señal duplicada en los últimos {_DEDUP_WINDOW_SECONDS//60} min, ignorando")
        return True
    elif modo_filtro == 'FLEXIBLE':
        # Registrar en dedup para evitar duplicados entre sí, pero no bloquear
        _check_and_record_signal(symbol, side)

    prec       = await get_symbol_precision(session, symbol)
    qty_prec   = prec['qty_prec']
    price_prec = prec['price_prec']
    riesgo_pct = abs(entry - stop) / entry * 100

    SL_MIN_PCT = 0.5
    SL_MAX_PCT = 2.5   # Permite SL hasta 2.5%
    sl_origen  = 'BOTAI'

    if riesgo_pct < SL_MIN_PCT:
        print(f"\n  ❌ RECHAZADA: SL muy ajustado ({riesgo_pct:.2f}% < {SL_MIN_PCT}%) — se tocaría con ruido")
        return True

    if riesgo_pct > SL_MAX_PCT:
        if side == 'LONG':
            stop = round(entry * (1 - SL_MAX_PCT / 100), price_prec)
        else:
            stop = round(entry * (1 + SL_MAX_PCT / 100), price_prec)
        riesgo_pct = abs(entry - stop) / entry * 100
        sl_origen  = f'RECALCULADO (BOTAI={sig["stop"]:.8g} era >{SL_MAX_PCT}%)'

    # ============================================================
    # 🔒 TPs DEL BOTAI — NUNCA SE RECALCULAN NI MODIFICAN
    # Se toman los valores exactos que envió el BOTAI.
    # Solo si el BOTAI no envió ningún TP se calculan localmente (fallback).
    # ============================================================

    # Reconstruir tps_list desde tps dict si por algún motivo llegó vacía
    tps_list = sig.get('tps_list', [])
    if not tps_list:
        tps_raw = sig.get('tps', {})
        if isinstance(tps_raw, dict):
            for tp_key in ['tp1', 'tp2', 'tp3']:
                val = tps_raw.get(tp_key)
                if val:
                    try:
                        tps_list.append(float(val))
                    except:
                        pass
        if tps_list:
            sig['tps_list'] = tps_list  # restaurar para consistencia
            print(f"  🔄 tps_list reconstruida desde sig['tps']: {tps_list}")

    if len(tps_list) >= 2:
        # ✅ Usar los TPs EXACTOS del BOTAI — sin ninguna modificación
        tp1 = round(tps_list[0], price_prec)   # TP1 BOTAI (60%)
        tp3 = round(tps_list[-1], price_prec)  # TP3 BOTAI (40%) — último de la lista
        tp2_ref = round(tps_list[1], price_prec) if len(tps_list) >= 3 else None
        print(f"  ✅ TPs del BOTAI (sin modificar):")
        print(f"     TP1 (60%) = {tp1}")
        if tp2_ref:
            print(f"     TP2 (ref) = {tp2_ref}")
        print(f"     TP3 (40%) = {tp3}")
    elif len(tps_list) == 1:
        tp1 = round(tps_list[0], price_prec)
        riesgo = abs(entry - stop)
        tp3 = round(tp1 + riesgo * 2.0, price_prec) if side == 'LONG' else round(tp1 - riesgo * 2.0, price_prec)
        print(f"  ⚠️ BOTAI envió 1 TP: TP1={tp1} → TP3 calculado={tp3}")
        tp2_ref = None
    else:
        # Fallback: BOTAI no envió TPs — calcular localmente
        tp1, tp3 = calc_tps(entry, stop, side)
        tp1 = round(tp1, price_prec)
        tp3 = round(tp3, price_prec)
        print(f"  ⚠️ Sin TPs del BOTAI — calculados localmente: TP1={tp1} TP3={tp3}")
        tp2_ref = None

    sep_full = "=" * 55
    print(f"\n{sep_full}")
    print(f"  EJECUTANDO: {symbol} {side}")
    print(f"  Score:{sig['quality_score']} | IA:{sig['ia_probability']:.2%} | TF:{sig.get('timeframe','')} | Modo:{modo_filtro}")
    print(f"  Entry : {entry:.8g}")
    print(f"  SL    : {stop:.8g}  ({riesgo_pct:.2f}% riesgo) [{sl_origen}]")
    print(f"  TP1   : {tp1:.8g} (60% real)")
    if tp2_ref:
        print(f"  TP2   : {tp2_ref:.8g} (solo referencia para mover SL)")
    print(f"  TP3   : {tp3:.8g} (40% real)")
    print(f"{sep_full}")

    sig['tp1'] = tp1
    sig['tp3'] = tp3
    if tp2_ref:
        sig['tp2'] = tp2_ref  # Solo para logging interno

    # ══════════════════════════════════════════════════════════════
    # 🔥 CORRECCIÓN BUG_FLEXIBLE_FIX — Verificación del filtro maestro
    #
    # ANTES (bug): analisis se reasignaba con 'or', lo que causaba que
    # si _analisis_precomputed era falsy (None o {}), caía a
    # _filtro_maestro_result que podría ser None → "Se omite por seguridad"
    #
    # AHORA: analisis ya se obtuvo correctamente arriba con lógica defensiva.
    # Si modo_filtro es FLEXIBLE, la señal ya fue validada por el filtro
    # maestro (signal_reader solo llama execute_fn cuando aceptada=True).
    # Por lo tanto, para FLEXIBLE salteamos la verificación de aceptada.
    # ══════════════════════════════════════════════════════════════
    if not analisis:
        # Sin resultado del filtro maestro — no se puede proceder con seguridad
        print("  ⚠️  Señal sin resultado de filtro maestro precomputado. Se omite por seguridad.")
        return True

    # Para modo FLEXIBLE: signal_reader ya verificó aceptada=True antes de llamar execute_fn.
    # No se hace doble-check — si llegó hasta aquí, está aprobada.
    if modo_filtro != 'FLEXIBLE':
        if not analisis.get('aceptada', False):
            motivo = (analisis.get('razon_rechazo') or analisis.get('mensaje') or 'rechazada')
            print(f"  RECHAZADA por filtro maestro: {motivo}")
            return True

    # ══════════════════════════════════════════════════════════════
    # Adaptive filter — FLEXIBLE bypasea automáticamente (sig['modo_filtro'] = 'FLEXIBLE')
    # ══════════════════════════════════════════════════════════════
    ok_adapt, motivo_adapt = adaptive_filter.check_signal(sig)
    if not ok_adapt:
        _log_adaptive(f"\n  🧠 [ADAPTIVE] Aprobada técnicamente pero rechazada por historial: {motivo_adapt}")
        return True

    balance = await get_balance(session)
    _balance_actual = balance
    puede, motivo = can_open_trade(balance, monitor.count_open())
    if not puede:
        print(f"  PAUSA: {motivo}")
        if 'balance' in motivo.lower() or 'insuficiente' in motivo.lower():
            return True
        return False

    margin = get_current_capital()
    if balance < margin:
        margin = max(balance, 4.0)
    if balance < 4.0:
        print(f"  PAUSA: Balance insuficiente (${balance:.4f} — mínimo $4)")
        return True
    print(f"  Balance: {balance:.4f} USDT | Capital op: ${margin:.4f} x {LEVERAGE}x")

    qty = calc_qty(margin, entry, qty_prec)
    if qty <= 0:
        print(f"  ERROR: Qty=0 para ${margin:.2f}x{LEVERAGE}/{entry}")
        return False
    print(f"  Qty: {qty} | Posición: ${qty*entry:.4f}")
    print(f"  Ops abiertas: {monitor.count_open()}/{MAX_TRADES}")

    await set_leverage_isolated(session, symbol)
    await asyncio.sleep(0.5)

    order_id = await place_market_order(session, symbol, side, qty, qty_prec)
    if not order_id:
        print("  ERROR: Falló la orden de mercado")
        return False

    await asyncio.sleep(3.0)
    position_id = ''
    pos_data    = {}
    for attempt in range(8):
        pos_data = await get_open_position(session, symbol, side)
        if pos_data:
            pid = str(pos_data.get('positionId', ''))
            if pid and pid != '0':
                position_id = pid
                print(f"  OK positionId: {position_id}")
                break
        print(f"  Esperando positionId... {attempt+1}/8")
        await asyncio.sleep(2.0)

    key = f"{symbol}_{side}_{int(time.time())}"

    if not position_id:
        print(f"  ⚠️  SIN positionId — posición abierta sin TPs/SL")
        monitor.add(key, {
            'symbol': symbol, 'side': side, 'entry': entry,
            'stop': stop, 'tp1': tp1, 'tp3': tp3,  # ✅ V2: sin tp2
            'tp': tp1,
            'position_id': '', 'tp1_id': '', 'tp3_id': '', 'sl_id': '',  # ✅ V2: sin tp2_id
            'tpsl_id': '',
            'qty': qty, 'qty_prec': qty_prec,
            'price_prec': price_prec, 'botai_key': sig['key'],
            'capital_usado': margin if 'margin' in dir() else MARGIN_PER_TRADE,
            'timestamp_apertura': time.time(),
            'trailing_niveles': [],
        })
        # 🔥 FIX: Forzar guardado y refresh aunque no haya position_id
        monitor._persist()
        monitor._nueva_posicion = True
        return True

    real_qty = _floor(float(pos_data.get('qty', pos_data.get('available', qty))), qty_prec)
    if real_qty <= 0:
        real_qty = qty
    print(f"  Qty real: {real_qty}")

    # ── Precio REAL de ejecución (fill price del exchange) ────────────
    real_entry = float(pos_data.get('avgOpenPrice', pos_data.get('openPrice', entry)))
    if real_entry <= 0:
        real_entry = entry
    if abs(real_entry - entry) / entry > 0.0001:
        print(f"  ⚠️  Slippage detectado: señal={entry:.8g} → fill real={real_entry:.8g}")
    entry = real_entry
    print(f"  Entry real: {entry:.8g}")

    _signal_by_key[key]         = sig
    _open_timestamp_by_key[key] = time.time()

    # ── Colocar TPs (órdenes LIMIT) + SL primero ─────────────────────────
    tpsl_result = await place_partial_tpsl(
        session, symbol, position_id, side,
        stop, tp1, tp3,
        real_qty, price_prec, qty_prec
    )

    # Precios reales confirmados por el exchange
    tp1_real = tpsl_result.get('tp1_price_enviado', tp1)
    tp3_real = tpsl_result.get('tp3_price_enviado', tp3)
    tp1_id   = tpsl_result.get('tp1_id', '')
    tp3_id   = tpsl_result.get('tp3_id', '')
    sl_id    = tpsl_result.get('sl_id', '')

    # ── Añadir al monitor con valores reales del exchange ────────────
    monitor.add(key, {
        'symbol':             symbol,
        'side':               side,
        'entry':              entry,       # precio real fill del exchange
        'stop':               stop,        # SL real que puso el exchange
        'expected_sl':        stop,
        'tp1':                tp1_real,    # precio real TP1 del exchange
        'tp3':                tp3_real,    # precio real TP3 del exchange
        'tp':                 tp1_real,
        'position_id':        position_id,
        'tp1_id':             tp1_id,      # ID orden limit TP1
        'tp3_id':             tp3_id,      # ID orden limit TP3
        'sl_id':              sl_id,
        'tpsl_id':            '',
        'qty':                real_qty,
        'qty_prec':           qty_prec,
        'price_prec':         price_prec,
        'botai_key':          sig['key'],
        'balance_before':     balance,
        'capital_usado':      margin,
        'timestamp_apertura': time.time(),
        'signal':             sig,
        'score_botai':        sig.get('quality_score', 0),
        'ia_probability':     sig.get('ia_probability', 0),
        'timeframe':          sig.get('timeframe', ''),
        'modo_filtro':        modo_filtro,
        'analisis_score':     analisis.get('puntaje', 0),
        'analisis_calidad':   analisis.get('veredicto', ''),
        'detalle_puntajes':   analisis.get('detalle_puntajes', {}),
        'trailing_niveles':   [],
        'sl_last_update_time': time.time(),
    })

    if tpsl_result.get('success'):
        print(f"\n  ✅ POSICIÓN ABIERTA EN BITUNIX [{modo_filtro}]")
        print(f"  {side} {symbol}")
        print(f"  Entry: {entry:.8g} | SL: {stop:.8g}")
        print(f"  🔥 TP1 (60%): {tp1_real:.8g} | ID:{tp1_id}")
        print(f"  🔥 TP3 (40%): {tp3_real:.8g} | ID:{tp3_id}")
        print(f"  Exchange gestiona los cierres automáticamente")
    else:
        print(f"\n  🚨 POSICIÓN ABIERTA SIN TPs/SL — {symbol} {side}")
        print(f"  Entry: {entry:.8g} | SL esperado: {stop:.8g}")
        print(f"  TP1 esperado: {tp1:.8g} | TP3: {tp3:.8g}")
        print(f"  ⚠️  Coloca TPs y SL MANUALMENTE en Bitunix ahora mismo")

        asyncio.create_task(
            _retry_place_partial_tpsl(session, symbol, side, position_id,
                                      stop, tp1, tp3,
                                      real_qty, price_prec, qty_prec, key)
        )

    # Nota: La visualización "análisis técnico" fue eliminada del flujo.

    print(f"  Ops abiertas: {monitor.count_open()}/{MAX_TRADES}")
    print(f"{sep_full}\n")
    return True


async def main():
    global http_session, monitor, signal_reader
    # ✅ Inicializar aquí — dentro del event loop para evitar problemas con asyncio.Lock
    monitor       = PositionMonitor()
    signal_reader = SignalReader()
    print("\n" + "="*60)
    print("  🚀 BITUNIX BOT — versión con TPs parciales V2 (60%/40%)")
    print("="*60)
    print("\n" + "="*55)
    print("  BITUNIX BOT FUTURES PULSE SIGNAL — iniciando")
    print("="*55)
    http_session = aiohttp.ClientSession()

    await _start_dashboard()

    ok = await test_connection()
    if not ok:
        await http_session.close()
        return

    global _balance_actual
    _balance_actual = await get_balance(http_session)
    await print_balance(http_session, "Inicio del bot")

    recovered = await recover_positions(http_session, monitor)
    if recovered:
        print(f"  {recovered} posicion(es) recuperada(s) y en monitoreo\n")

    state = load_state()
    executed_keys = [v.get('botai_key', '') for v in state.values() if v.get('botai_key')]
    signal_reader.load_executed_from_state(executed_keys)
    if executed_keys:
        print(f"  {len(executed_keys)} señal(es) bloqueadas (posiciones aún abiertas)")
    else:
        print("  Sin señales bloqueadas — disponibles para reejecutarse si BOTAI las reactiva")

    print_capital_status()
    print(f"  {LEVERAGE}x isolation | Capital dinámico (base $4) | Max {MAX_TRADES} ops")
    print(f"  SL = señal | TPs parciales V2 60/40 | Símbolo bloqueado 15min")

    # ── Estado del horario al arrancar ───────────────────────
    time_filter.mostrar_estado()

    print("  Bot activo — buscando señales...\n")

    tasks = [
        asyncio.create_task(
            signal_reader.start(http_session, execute_signal, monitor.count, MAX_TRADES)
        ),
        asyncio.create_task(monitor.start(http_session)),
        asyncio.create_task(_record_closed_trades_loop()),
    ]

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    except KeyboardInterrupt:
        pass
    finally:
        signal_reader.stop()
        monitor.stop()
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _shutdown()


async def _shutdown():
    global http_session
    sep_full = "=" * 55
    print(f"\n\n{sep_full}")
    print(f"  🛑 BOT DETENIDO — guardando estado...")
    print(sep_full)
    signal_reader.stop()
    monitor.stop()

    from recovery import save_state
    if monitor._positions:
        save_state(monitor._positions)
        print(f"  ✅ {len(monitor._positions)} posición(es) guardada(s) en estado.json")
        for key, pos in monitor._positions.items():
            print(f"     📌 {pos['symbol']} {pos['side']} | Entry:{pos['entry']:.8g} | SL:{pos['stop']:.8g} | TP1:{pos.get('tp1',0):.8g} TP3:{pos.get('tp3',0):.8g}")
    else:
        print("  ✅ Sin posiciones abiertas — estado limpio")

    if http_session and not http_session.closed:
        await http_session.close()

    print(sep_full)
    print("  👋 Hasta luego\n")


def _verificar_instancia_unica():
    """Evita que corran dos instancias del bot al mismo tiempo."""
    import tempfile, sys
    lock_file = os.path.join(tempfile.gettempdir(), 'bitunix_bot.lock')
    try:
        import msvcrt  # Windows
        _lock_fh = open(lock_file, 'w')
        msvcrt.locking(_lock_fh.fileno(), msvcrt.LK_NBLCK, 1)
        return _lock_fh  # mantener referencia viva
    except ImportError:
        import fcntl  # Linux/Mac
        _lock_fh = open(lock_file, 'w')
        try:
            fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return _lock_fh
        except OSError:
            print("  🚫 Ya hay una instancia del bot corriendo. Cerrando esta.")
            sys.exit(1)
    except OSError:
        print("  🚫 Ya hay una instancia del bot corriendo. Cerrando esta.")
        sys.exit(1)

if __name__ == '__main__':
    # ── Lanzar puente.py automáticamente en paralelo ──────────────────────
    import subprocess
    import pathlib

    _puente_path = pathlib.Path(r'C:\Users\DMG TECNOLOGIA\Videos\bitunix\bitunix2\puente\puente.py')
    _puente_proc = None

    if _puente_path.exists():
        try:
            # Intentar abrir en pestaña nueva de Windows Terminal
            _wt = subprocess.Popen(
                ['wt', '-w', '0', 'new-tab', '--title', 'PUENTE',
                 sys.executable, str(_puente_path)],
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            _puente_proc = _wt
            print(f"  🌉 puente.py abierto en nueva pestaña del terminal")
        except FileNotFoundError:
            # Windows Terminal no disponible → abrir en ventana nueva
            try:
                _puente_proc = subprocess.Popen(
                    [sys.executable, str(_puente_path)],
                    creationflags=subprocess.CREATE_NEW_CONSOLE
                )
                print(f"  🌉 puente.py iniciado en ventana nueva (PID: {_puente_proc.pid})")
            except Exception as e:
                print(f"  ⚠️  No se pudo iniciar puente.py: {e}")
        except Exception as e:
            print(f"  ⚠️  No se pudo iniciar puente.py: {e}")
    else:
        print(f"  ⚠️  puente.py no encontrado en {_puente_path}")

    # ── Iniciar bot principal ─────────────────────────────────────────────
    _lock = _verificar_instancia_unica()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        import traceback
        print(f"\n  ❌ ERROR FATAL:")
        print(f"  {type(e).__name__}: {e}")
        traceback.print_exc()
    finally:
        if _puente_proc and _puente_proc.poll() is None:
            _puente_proc.terminate()
            print("  🌉 puente.py detenido")