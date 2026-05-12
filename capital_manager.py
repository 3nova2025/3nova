"""
capital_manager.py
==================
Gestión de capital y bloqueo de símbolos.

Capital fijo: $4 por operación siempre.
Bloqueo de símbolo: 15 minutos (protección contra duplicados)
"""
import json, os, time

CAPITAL_FILE      = 'capital.json'
BASE_CAPITAL      = 4.0
SYMBOL_BLOCK_MIN  = 15     # minutos de bloqueo por símbolo tras cierre (antes 1 hora)


def _load() -> dict:
    if not os.path.exists(CAPITAL_FILE): return {}
    try:
        with open(CAPITAL_FILE, 'r') as f: return json.load(f)
    except: return {}


def _save(data: dict):
    with open(CAPITAL_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def get_current_capital() -> float:
    return float(_load().get('capital', BASE_CAPITAL))


def update_capital(pnl: float, symbol: str, side: str):
    """
    Actualiza el estado tras cerrar una posición.
    Capital siempre fijo en $4 — bloquea el símbolo 15 minutos.
    """
    data    = _load()
    capital = float(data.get('capital', BASE_CAPITAL))
    blocked = data.get('blocked_symbols', {})
    ahora   = time.time()

    # Capital siempre fijo en $4
    nuevo_capital = BASE_CAPITAL

    # Bloquear símbolo por 15 minutos
    blocked[symbol] = ahora + SYMBOL_BLOCK_MIN * 60

    _save({
        'capital':           round(nuevo_capital, 4),
        'blocked_symbols':   blocked,
    })

    emoji = '📈' if pnl >= 0 else '📉'
    print(f"\n  {'─'*48}")
    print(f"  {emoji} CAPITAL ACTUALIZADO")
    print(f"     PnL        : {'+'if pnl>=0 else ''}{pnl:.4f} USDT")
    print(f"     Capital op : ${nuevo_capital:.4f} (fijo)")
    print(f"     Con 4x     = ${nuevo_capital*4:.2f} por posición")
    print(f"     {symbol} bloqueado por {SYMBOL_BLOCK_MIN}min")
    print(f"  {'─'*48}\n")

    return nuevo_capital


def is_direction_blocked(side: str) -> bool:
    """
    Direcciones YA NO se bloquean.
    Siempre retorna False.
    """
    return False


def is_symbol_blocked(symbol: str) -> bool:
    """Verifica si un símbolo está bloqueado"""
    blocked = _load().get('blocked_symbols', {})
    until   = blocked.get(symbol, 0)
    return time.time() < until


def clean_expired_blocks():
    """Limpia bloqueos vencidos del archivo."""
    data    = _load()
    blocked = data.get('blocked_symbols', {})
    ahora   = time.time()
    data['blocked_symbols'] = {k: v for k, v in blocked.items() if v > ahora}
    _save(data)


def print_capital_status():
    data    = _load()
    capital = float(data.get('capital', BASE_CAPITAL))
    blocked = data.get('blocked_symbols', {})
    ahora   = time.time()
    activos = {k: v for k, v in blocked.items() if v > ahora}

    print(f"\n  {'─'*48}")
    print(f"  💼 CAPITAL POR OPERACIÓN: ${capital:.4f}")
    print(f"     Con 4x = ${capital*4:.2f} por posición")
    print(f"     Mínimo : ${BASE_CAPITAL} (fijo, nunca cambia)")
    if activos:
        bloqs = ', '.join(
            f"{k.replace('USDT','')} ({int((v-ahora)/60)}min)"
            for k, v in activos.items()
        )
        print(f"     Símbolos bloqueados (15min): {bloqs}")
    else:
        print(f"     🔓 Sin bloqueos activos")
    print(f"  {'─'*48}\n")
    return capital