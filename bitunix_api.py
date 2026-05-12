"""
bitunix_api.py  —  Bitunix Futures
LOGICA:
Entry y SL vienen de la señal del BOTAI (sin modificar)
TPs calculados por el bot: 2 niveles parciales (60% y 40%)
LONG:  TP1 = entry + (entry - SL) * 1.2
       TP3 = entry + (entry - SL) * 2.4
SHORT: TP1 = entry - (SL - entry) * 1.2
       TP3 = entry - (SL - entry) * 2.4

LÓGICA SL:
- SL se coloca via tpsl/place_order al abrir
- Cuando TP1 se ejecuta → modify_sl mueve el SL al precio de entrada (breakeven)
- Cuando precio avanza 70% de TP1 a TP3 → modify_sl mueve el SL al precio de TP1
- Los TPs son órdenes LIMIT independientes — nunca se tocan
"""
import time, uuid, json, hashlib, asyncio, aiohttp, os
from dotenv import load_dotenv
from console_util import safe_print

load_dotenv()
API_KEY    = os.getenv('BITUNIX_API_KEY', '')
SECRET_KEY = os.getenv('BITUNIX_SECRET_KEY', '')
BASE_URL   = 'https://fapi.bitunix.com'
LEVERAGE           = 4
MARGIN_COIN        = 'USDT'
MARGIN_PER_TRADE   = 4.0
MIN_BALANCE        = 1.0
MAX_TRADES         = 2
TP_RR              = 1.7
TP1_RATIO = 0.6
TP3_RATIO = 0.4
TP1_MULT  = 1.2
TP3_MULT  = 2.4


def _fmt_fixed(value: float, decimals: int) -> str:
    try:
        return f"{float(value):.{int(decimals)}f}"
    except Exception:
        return str(value)


def calc_tps(entry: float, stop: float, side: str) -> tuple:
    riesgo = abs(entry - stop)
    if side == 'LONG':
        tp1 = entry + riesgo * TP1_MULT
        tp3 = entry + riesgo * TP3_MULT
    else:
        tp1 = entry - riesgo * TP1_MULT
        tp3 = entry - riesgo * TP3_MULT
    return tp1, tp3


def calc_tp(entry: float, stop: float, side: str) -> float:
    riesgo = abs(entry - stop)
    return entry + riesgo * TP_RR if side == 'LONG' else entry - riesgo * TP_RR


def _sha256(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _query_string(params):
    return ''.join(f"{k}{v}" for k, v in sorted(params.items()))


def _build_sign(nonce, timestamp, query_str='', body=''):
    digest = _sha256(nonce + timestamp + API_KEY + query_str + body)
    return _sha256(digest + SECRET_KEY)


def _headers(query_str='', body=''):
    nonce     = uuid.uuid4().hex
    timestamp = str(int(time.time() * 1000))
    sign      = _build_sign(nonce, timestamp, query_str, body)
    return {
        'api-key': API_KEY, 'sign': sign, 'nonce': nonce,
        'timestamp': timestamp, 'language': 'en-US',
        'Content-Type': 'application/json',
    }


async def _post(session, path, payload):
    body = json.dumps(payload, separators=(',', ':'))
    try:
        async with session.post(BASE_URL + path, data=body,
                                headers=_headers(body=body),
                                ssl=False, timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
            code = str(data.get('code', '-1'))
            if code != '0':
                msg = data.get('msg', '?')
                safe_print(f"  ❌ POST {path} → code:{code} msg:{msg}")
                if 'modify_order' in path:
                    safe_print(f"     ⚠️  [DEBUG] payload: {body}")
                    safe_print(f"     ⚠️  [DEBUG] respuesta completa: {data}")
                return None
            result = data.get('data', {})
            if isinstance(result, list) and 'batch_order' not in path:
                result = result[0] if result else {}
            return result
    except Exception as e:
        safe_print(f"  ❌ POST {path}: {e}")
        return None


async def _get(session, path, params={}):
    query_sign = _query_string(params)
    qs_url     = '&'.join(f"{k}={v}" for k, v in sorted(params.items()))
    url        = BASE_URL + path + (f'?{qs_url}' if qs_url else '')
    try:
        async with session.get(url, headers=_headers(query_str=query_sign),
                               ssl=False, timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
            if str(data.get('code', '-1')) != '0':
                return {}
            return data.get('data', {})
    except Exception as e:
        return {}


async def get_balance(session) -> float:
    data = await _get(session, '/api/v1/futures/account', {'marginCoin': MARGIN_COIN})
    if isinstance(data, list) and data:
        return float(data[0].get('available', 0))
    if isinstance(data, dict):
        return float(data.get('available', 0))
    return 0.0


async def print_balance(session, motivo: str = ''):
    balance = await get_balance(session)
    label = f" — {motivo}" if motivo else ''
    safe_print(f"\n  {'─'*48}")
    safe_print(f"  💰 SALDO ACTUAL{label}")
    safe_print(f"     Disponible : {balance:.4f} USDT")
    if balance >= MARGIN_PER_TRADE:
        safe_print(f"     Estado     : ✅ Suficiente ($4)")
    elif balance >= MIN_BALANCE:
        safe_print(f"     Estado     : ⚠️  Reserva (${balance:.4f})")
    else:
        safe_print(f"     Estado     : 🔴 Insuficiente (min ${MIN_BALANCE})")
    safe_print(f"  {'─'*48}\n")
    return balance


def can_open_trade(balance: float, open_trades: int) -> tuple[bool, str]:
    if open_trades >= MAX_TRADES:
        return False, f"Límite {MAX_TRADES} ops simultáneas"
    if balance < MARGIN_PER_TRADE:
        return False, f"Balance insuficiente (${balance:.4f} — mínimo ${MARGIN_PER_TRADE})"
    return True, ''


def effective_margin(balance: float) -> float:
    return MARGIN_PER_TRADE


async def get_ticker(session, symbol) -> dict:
    url = f"{BASE_URL}/api/v1/futures/market/tickers?symbols={symbol}"
    try:
        async with session.get(url, ssl=False, timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
            if str(data.get('code')) == '0':
                items = data.get('data', [])
                if items:
                    return items[0]
    except Exception as e:
        if str(e):
            safe_print(f"  ⚠️ get_ticker {symbol}: {e}")
    return {}


async def get_symbol_precision(session, symbol) -> dict:
    url = f"{BASE_URL}/api/v1/futures/market/trading_pairs?symbols={symbol}"
    try:
        async with session.get(url, ssl=False, timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
            if str(data.get('code')) == '0':
                items = data.get('data', [])
                if items:
                    return {
                        'qty_prec':   int(items[0].get('volumePlace', 3)),
                        'price_prec': int(items[0].get('pricePlace', 4)),
                    }
    except Exception as e:
        safe_print(f"  ❌ get_symbol_precision: {e}")
    return {'qty_prec': 3, 'price_prec': 4}


async def get_pending_positions(session, symbol: str = None) -> list:
    params = {}
    if symbol:
        params['symbol'] = symbol
    data = await _get(session, '/api/v1/futures/position/get_pending_positions', params)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get('list', data.get('positionList', []))
    return []


async def get_all_open_positions(session, symbol: str = None) -> list:
    return await get_pending_positions(session, symbol)


async def set_leverage_isolated(session, symbol) -> bool:
    result = await _post(session, '/api/v1/futures/account/change_leverage', {
        'symbol': symbol, 'leverage': LEVERAGE, 'marginCoin': MARGIN_COIN,
    })
    await asyncio.sleep(0.5)
    info = await _get(session, '/api/v1/futures/account/get_leverage_margin_mode',
                      {'symbol': symbol, 'marginCoin': MARGIN_COIN})
    if info:
        if isinstance(info, list):
            info = info[0]
        lev  = int(float(info.get('longLeverage', info.get('leverage', 0))))
        mode = str(info.get('marginMode', '')).upper()
        safe_print(f"  ✅ {symbol} → {lev}x {mode}")
    elif result is not None:
        safe_print(f"  ✅ {symbol} → {LEVERAGE}x aplicado")
    return True


async def get_open_position(session, symbol, side) -> dict:
    api_side  = 'BUY' if side == 'LONG' else 'SELL'
    positions = await get_pending_positions(session, symbol)
    for pos in positions:
        pos_side = str(pos.get('side', pos.get('holdSide', ''))).upper()
        if pos_side == api_side:
            qty = float(pos.get('qty', pos.get('available', 0)))
            if qty > 0:
                return pos
    return {}


def _floor(value, decimals):
    factor = 10 ** decimals
    return int(value * factor) / factor


def calc_qty(margin: float, entry: float, qty_prec: int) -> float:
    return _floor((margin * LEVERAGE) / entry, qty_prec)


async def place_market_order(session, symbol, side, qty, qty_prec) -> str:
    order_side = 'BUY' if side == 'LONG' else 'SELL'
    result = await _post(session, '/api/v1/futures/trade/place_order', {
        'symbol': symbol, 'side': order_side, 'tradeSide': 'OPEN',
        'orderType': 'MARKET',
        'qty': _fmt_fixed(_floor(qty, qty_prec), qty_prec),
        'effect': 'GTC',
    })
    if isinstance(result, list):
        result = result[0] if result else {}
    order_id = (result or {}).get('orderId', '')
    if order_id:
        safe_print(f"  ✅ MARKET {side} {_floor(qty, qty_prec)} {symbol} → {order_id}")
        return str(order_id)
    return ""


async def place_partial_tpsl(session, symbol, position_id, side, sl_price,
                             tp1_price, tp3_price,
                             total_qty, price_prec, qty_prec) -> dict:
    close_side = 'SELL' if side == 'LONG' else 'BUY'
    tp1_qty = _floor(total_qty * TP1_RATIO, qty_prec)           # 60% FLOOR
    tp3_qty = total_qty - tp1_qty                               # RESTO EXACTO
    tp3_qty = _floor(tp3_qty, qty_prec)                         # Precision exchange
    safe_print(f"  📊 QTY: {total_qty} → TP1:{tp1_qty} + TP3:{tp3_qty} = {tp1_qty+tp3_qty}")

    order_list = []

    if tp1_qty > 0.000001 and tp1_price > 0:
        order_list.append({
            "side": close_side, "tradeSide": "CLOSE",
            "positionId": str(position_id),
            "price": _fmt_fixed(round(tp1_price, price_prec), price_prec),
            "qty": f"{tp1_qty:.{qty_prec}f}",
            "orderType": "LIMIT", "reduceOnly": True, "effect": "GTC"
        })

    if tp3_qty > 0.000001 and tp3_price > 0:
        order_list.append({
            "side": close_side, "tradeSide": "CLOSE",
            "positionId": str(position_id),
            "price": _fmt_fixed(round(tp3_price, price_prec), price_prec),
            "qty": f"{tp3_qty:.{qty_prec}f}",
            "orderType": "LIMIT", "reduceOnly": True, "effect": "GTC"
        })

    result_data = {
        'success': False, 'tp1_id': '', 'tp3_id': '', 'sl_id': '',
        'tp1_price_enviado': tp1_price, 'tp3_price_enviado': tp3_price,
    }

    if order_list:
        result = await _post(session, '/api/v1/futures/trade/batch_order',
                             {'symbol': symbol, 'orderList': order_list})
        safe_print(f"  🔍 [DEBUG batch_order] result={result}")
        if result and isinstance(result, dict):
            success_list = result.get('successList', [])
            failure_list = result.get('failureList', [])
            if failure_list:
                for fail in failure_list:
                    safe_print(f"  ❌ TP fallido: {fail.get('errorMsg','?')} "
                               f"(code:{fail.get('errorCode','?')})")
            if success_list:
                for i, order in enumerate(success_list):
                    oid = order.get('id', order.get('orderId', ''))
                    if i == 0:
                        result_data['tp1_id'] = oid
                        safe_print(f"  ✅ TP1 (60%) @ {round(tp1_price, price_prec)} → {oid}")
                    elif i == 1:
                        result_data['tp3_id'] = oid
                        safe_print(f"  ✅ TP3 (40%) @ {round(tp3_price, price_prec)} → {oid}")
            else:
                safe_print(f"  ❌ batch_order TPs: successList vacío")
        elif result and isinstance(result, list):
            for i, order in enumerate(result):
                oid = order.get('id', order.get('orderId', ''))
                if i == 0:
                    result_data['tp1_id'] = oid
                    safe_print(f"  ✅ TP1 (60%) @ {round(tp1_price, price_prec)} → {oid}")
                elif i == 1:
                    result_data['tp3_id'] = oid
                    safe_print(f"  ✅ TP3 (40%) @ {round(tp3_price, price_prec)} → {oid}")
        else:
            safe_print(f"  ❌ batch_order TPs falló — result={result}")

    if sl_price > 0:
        sl_existente = None
        try:
            tpsl_check = await _get(session, '/api/v1/futures/tpsl/get_pending_orders',
                                    {'symbol': symbol})
            tpsl_list  = tpsl_check if isinstance(tpsl_check, list) \
                         else (tpsl_check or {}).get('orderList', [])
            for o in tpsl_list:
                if o.get('symbol', '') == symbol:
                    existing_sl = float(o.get('slPrice', 0) or 0)
                    if existing_sl > 0:
                        sl_existente = str(o.get('id', o.get('orderId', '')))
                        safe_print(f"  ⚠️  [API] {symbol} ya tiene SL activo @ "
                                   f"{existing_sl} — NO se coloca otro")
                        result_data['sl_id'] = sl_existente
                        break
        except Exception:
            pass

        if not sl_existente:
            sl_payload = {
                'symbol':      symbol,
                'positionId':  str(position_id),
                'slPrice':     _fmt_fixed(round(sl_price, price_prec), price_prec),
                'slStopType':  'MARK_PRICE',
                'slOrderType': 'MARKET',
                'slQty':       _fmt_fixed(round(total_qty, qty_prec), qty_prec),
            }
            sl_result = await _post(session, '/api/v1/futures/tpsl/place_order', sl_payload)
            if sl_result is not None:
                sl_id = str(sl_result.get('orderId', sl_result.get('id', '')))
                result_data['sl_id'] = sl_id
                safe_print(f"  ✅ SL @ {round(sl_price, price_prec)} → {sl_id}")
            else:
                safe_print(f"  ❌ SL no se pudo colocar — colocar manualmente en Bitunix")

    if result_data['tp1_id']:
        result_data['success'] = True
    return result_data


async def place_sl_tp(session, symbol, position_id, sl_price, tp_price,
                      qty, price_prec, qty_prec) -> str:
    safe_print(f"  ⚠️ place_sl_tp es LEGACY. Usar place_partial_tpsl()")
    result = await _post(session, '/api/v1/futures/tpsl/place_order', {
        'symbol': symbol, 'positionId': str(position_id),
        'slPrice': str(round(sl_price, price_prec)), 'slStopType': 'MARK_PRICE',
        'slOrderType': 'MARKET', 'slQty': str(_floor(qty, qty_prec)),
        'tpPrice': str(round(tp_price, price_prec)), 'tpStopType': 'MARK_PRICE',
        'tpOrderType': 'MARKET', 'tpQty': str(_floor(qty, qty_prec)),
    })
    if isinstance(result, list):
        result = result[0] if result else {}
    tpsl_id = (result or {}).get('tpslId', (result or {}).get('orderId', ''))
    if tpsl_id:
        safe_print(f"  ✅ SL @ {round(sl_price, price_prec)} | TP @ {round(tp_price, price_prec)} → {tpsl_id}")
    else:
        safe_print(f"  ⚠️  SL/TP no confirmados")
    return str(tpsl_id)


async def cancel_order_by_id(session, symbol: str, order_id: str) -> bool:
    result = await _post(session, '/api/v1/futures/trade/cancel_orders', {
        'symbol': symbol, 'orderList': [{'orderId': str(order_id)}]
    })
    if result and isinstance(result, dict):
        if result.get('successList', []):
            safe_print(f"  🗑️ Orden cancelada: {order_id}")
            return True
    safe_print(f"  ⚠️ No se pudo cancelar orden {order_id}")
    return False


async def cancel_tpsl_order(session, tpsl_id: str, symbol: str) -> bool:
    result = await _post(session, '/api/v1/futures/tpsl/cancel_order',
                         {'symbol': symbol, 'orderId': str(tpsl_id)})
    if result is not None:
        safe_print(f"  🗑️ TPSL cancelado: {tpsl_id}")
        return True
    safe_print(f"  ⚠️ No se pudo cancelar TPSL {tpsl_id}")
    return False


async def get_active_tpsl_orders(session, symbol: str, position_id: str = '') -> dict:
    params = {'symbol': symbol}
    if position_id:
        params['positionId'] = str(position_id)
    data   = await _get(session, '/api/v1/futures/tpsl/get_pending_orders', params)
    orders = data if isinstance(data, list) \
             else (data or {}).get('orderList', (data or {}).get('list', []))
    sl_price = tp_price = tp3_price = 0.0
    tpsl_id  = ''
    for o in orders:
        pid = str(o.get('positionId', ''))
        if position_id and pid and pid != str(position_id):
            continue
        sl  = float(o.get('slPrice', 0) or 0)
        tp  = float(o.get('tpPrice', 0) or 0)
        tp3 = float(o.get('tp2Price', 0) or 0)
        oid = str(o.get('id', o.get('tpslId', o.get('orderId', ''))))
        if sl:  sl_price  = sl
        if tp:  tp_price  = tp
        if tp3: tp3_price = tp3
        if oid: tpsl_id   = oid
    if sl_price or tp_price or tp3_price:
        return {'sl': sl_price, 'tp': tp_price, 'tp3': tp3_price, 'tpsl_id': tpsl_id}
    return {}


async def get_tpsl_history(session, symbol: str, position_id: str = '', limit: int = 10) -> dict:
    params = {'symbol': symbol, 'limit': str(limit)}
    if position_id:
        params['positionId'] = str(position_id)
    data   = await _get(session, '/api/v1/futures/tpsl/get_history_orders', params)
    orders = data if isinstance(data, list) else (data or {}).get('orderList', [])
    sl_price = tp_price = 0.0
    tpsl_id  = ''
    latest   = 0
    for o in orders:
        pid = str(o.get('positionId', ''))
        if position_id and pid and pid != str(position_id):
            continue
        estado = str(o.get('status', '')).upper()
        if 'CANCEL' in estado and 'PART' not in estado:
            continue
        ctime = int(o.get('ctime', 0) or 0)
        if ctime < latest:
            continue
        sl  = float(o.get('slPrice', 0) or 0)
        tp  = float(o.get('tpPrice', 0) or 0)
        oid = str(o.get('id', o.get('tpslId', o.get('orderId', ''))))
        if sl or tp:
            sl_price = sl; tp_price = tp; tpsl_id = oid; latest = ctime
    if sl_price or tp_price:
        return {'sl': sl_price, 'tp': tp_price, 'tpsl_id': tpsl_id}
    return {}


async def modify_sl(session, symbol: str, position_id: str,
                    new_sl: float, price_prec: int,
                    sl_id: str = '') -> dict:
    """
    Modifica el valor del SL existente usando el endpoint oficial:
    POST /api/v1/futures/tpsl/position/modify_order
    Solo envía symbol + positionId + slPrice + slStopType.
    Bitunix actualiza el valor del SL sin crear uno nuevo.
    Los TPs (órdenes LIMIT) no se tocan.
    """
    try:
        payload = {
            'symbol':     symbol,
            'positionId': str(position_id),
            'slPrice':    _fmt_fixed(round(new_sl, price_prec), price_prec),
            'slStopType': 'MARK_PRICE',
        }
        result = await _post(session, '/api/v1/futures/tpsl/position/modify_order', payload)
        if result is not None:
            safe_print(f"  🔒 SL modificado → {round(new_sl, price_prec)}")
            return {'success': True, 'error': ''}
        return {'success': False, 'error': 'modify_order falló'}
    except Exception as e:
        safe_print(f"  ❌ modify_sl error: {e}")
        return {'success': False, 'error': str(e)}


async def flash_close(session, symbol, side, position_id='') -> bool:
    if position_id:
        result = await _post(session, '/api/v1/futures/trade/flash_close_position',
                             {'positionId': str(position_id)})
    else:
        result = await _post(session, '/api/v1/futures/trade/close_all_position',
                             {'symbol': symbol})
    return result is not None


async def test_connection():
    safe_print("\n" + "="*55)
    safe_print("  🔌 PROBANDO CONEXIÓN CON BITUNIX FUTURES")
    safe_print("="*55)
    if not API_KEY or not SECRET_KEY:
        safe_print("  ❌ Faltan credenciales en .env")
        return False
    safe_print(f"  🔑 API Key: {API_KEY[:8]}...{API_KEY[-4:]}")
    async with aiohttp.ClientSession() as session:
        ticker = await get_ticker(session, 'BTCUSDT')
        if not ticker:
            safe_print("  ❌ No se pudo conectar")
            return False
        price = float(ticker.get('lastPrice', 0))
        safe_print(f"  ✅ Servidor OK — BTC: ${price:,.2f}")
        balance = await get_balance(session)
        safe_print(f"  ✅ Autenticación OK — Balance: {balance:.4f} USDT")
    safe_print("="*55)
    safe_print(f"  🎉 LISTO | {LEVERAGE}x | TP1 60% / TP3 40%")
    safe_print("="*55 + "\n")
    return True


if __name__ == '__main__':
    asyncio.run(test_connection())