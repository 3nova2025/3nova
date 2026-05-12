"""
estadisticas.py
===============
Registra y muestra estadísticas de todas las operaciones cerradas.
Se actualiza automáticamente cada vez que cierra una posición.
Guarda en estadisticas.json
"""
import json, os, time
from datetime import datetime

STATS_FILE = 'estadisticas.json'

def _load() -> dict:
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            pass
    return {
        'total':       0,
        'ganadas':     0,
        'perdidas':    0,
        'total_ganado':  0.0,
        'total_perdido': 0.0,
        'mejor_op':    None,
        'peor_op':     None,
        'operaciones': []
    }

def _save(data: dict):
    with open(STATS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def registrar_operacion(symbol: str, side: str, entry: float,
                         stop: float, tp: float,
                         capital: float, pnl: float,
                         detalle: dict = None):
    """
    Registra una operación cerrada y actualiza estadísticas.

    detalle (opcional) — contexto completo del trade para análisis futuro:
      score_botai, quality_score, ia_probability, timeframe
      analisis_score, analisis_calidad, analisis_4h, analisis_1h
      fvg_detectado, ema50_espacio, ema12_26_alineadas
      tp_ajustado, tp_rr_usado
      trailing_niveles, duracion_min
      close_reason (SL/TP/manual)
    """
    data = _load()

    op = {
        'fecha':     datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'symbol':    symbol,
        'side':      side,
        'entry':     entry,
        'sl':        stop,
        'tp':        tp,
        'capital':   capital,
        'pnl':       round(pnl, 4),
        'resultado': round(capital + pnl, 4),
        'ganada':    pnl >= 0,
    }

    # Añadir detalle completo si se proporcionó
    if detalle:
        op.update({
            # Bot 1
            'score_botai':      detalle.get('score_botai', 0),
            'ia_probability':   round(detalle.get('ia_probability', 0), 4),
            'timeframe':        detalle.get('timeframe', ''),
            # Análisis técnico (Bot 2)
            'analisis_score':   detalle.get('analisis_score', 0),
            'analisis_calidad': detalle.get('analisis_calidad', ''),
            'pts_4h':           detalle.get('pts_4h', 0),
            'pts_1h':           detalle.get('pts_1h', 0),
            'pts_gatillo':      detalle.get('pts_gatillo', 0),
            'fvg_detectado':    detalle.get('fvg_detectado', False),
            'ema50_hacia':      detalle.get('ema50_hacia', ''),
            'ema12_26_ok':      detalle.get('ema12_26_ok', False),
            'vela_rechazo':     detalle.get('vela_rechazo', False),
            'tipo_rechazo':     detalle.get('tipo_rechazo', ''),
            # TP
            'tp_modificado':    detalle.get('tp_modificado', False),
            'tp_rr':            detalle.get('tp_rr', 1.7),
            # Trailing SL
            'trailing_niveles': detalle.get('trailing_niveles', []),
            'trailing_max_pct': detalle.get('trailing_max_pct', 0),
            # Cierre
            'duracion_min':     detalle.get('duracion_min', 0),
            'close_reason':     detalle.get('close_reason', ''),
            'balance_antes':    round(detalle.get('balance_antes', 0), 4),
            'balance_despues':  round(detalle.get('balance_despues', 0), 4),
        })

    data['total']      += 1
    data['operaciones'].append(op)

    if pnl >= 0:
        data['ganadas']      += 1
        data['total_ganado'] = round(data['total_ganado'] + pnl, 4)
        # Mejor operación
        if data['mejor_op'] is None or pnl > data['mejor_op']['pnl']:
            data['mejor_op'] = {'symbol': symbol, 'side': side, 'pnl': round(pnl, 4)}
    else:
        data['perdidas']      += 1
        data['total_perdido'] = round(data['total_perdido'] + pnl, 4)
        # Peor operación
        if data['peor_op'] is None or pnl < data['peor_op']['pnl']:
            data['peor_op'] = {'symbol': symbol, 'side': side, 'pnl': round(pnl, 4)}

    _save(data)

    # Guardar también en historial detallado (archivo separado, nunca se limpia)
    _save_historial_detallado(op)

    # ✅ Actualizar signals_aprobadas.txt con el resultado real
    try:
        from signal_logger import log_resultado
        close_reason = detalle.get('close_reason', '') if detalle else ''
        duracion_min = detalle.get('duracion_min', 0) if detalle else 0

        if pnl > 0:
            resultado_str = 'WIN'
        elif pnl < 0:
            resultado_str = 'LOSS'
        else:
            resultado_str = 'BREAKEVEN'

        log_resultado(
            symbol=symbol,
            side=side,
            entry=entry,
            resultado=resultado_str,
            pnl=pnl,
            duracion_min=duracion_min,
        )
    except Exception as e:
        print(f'  ⚠️  [STATS] No se pudo actualizar signal_logger: {e}')

    return data


HISTORIAL_FILE = 'trade_history_detallado.json'

def _save_historial_detallado(op: dict):
    """Guarda el trade en el historial detallado — nunca se borra."""
    historial = []
    if os.path.exists(HISTORIAL_FILE):
        try:
            with open(HISTORIAL_FILE, 'r', encoding='utf-8') as f:
                historial = json.load(f)
        except:
            historial = []
    historial.append(op)
    try:
        with open(HISTORIAL_FILE, 'w', encoding='utf-8') as f:
            json.dump(historial, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"  ⚠️  No se pudo guardar historial detallado: {e}")

def mostrar_estadisticas():
    """Imprime el resumen de estadísticas en consola."""
    data  = _load()
    total = data['total']

    if total == 0:
        print("\n  📊 Sin operaciones registradas aún\n")
        return

    ganadas  = data['ganadas']
    perdidas = data['perdidas']
    pct_g    = (ganadas / total * 100) if total > 0 else 0
    pct_p    = (perdidas / total * 100) if total > 0 else 0
    ganado   = data['total_ganado']
    perdido  = data['total_perdido']
    neto     = round(ganado + perdido, 4)
    mejor    = data.get('mejor_op')
    peor     = data.get('peor_op')

    W = 50
    def _clr(t, c): return f"\033[{c}m{t}\033[0m"
    cyan  = lambda t: _clr(t, '96')
    green = lambda t: _clr(t, '92')
    red   = lambda t: _clr(t, '91')
    bold  = lambda t: _clr(t, '1')

    print(bold(cyan(f"\n╔{'═'*W}╗")))
    print(bold(cyan("║")) + bold(f"{'  📊 ESTADÍSTICAS DEL BOT':<{W}}") + bold(cyan("║")))
    print(bold(cyan(f"╠{'═'*W}╣")))
    print(cyan("║") + f"  {'Total operaciones':<22}: {total:<{W-25}}" + cyan("║"))
    print(cyan("║") + f"  ✅ {'Ganadas':<20}: {ganadas:<4} ({pct_g:.1f}%){'':<{W-33}}" + cyan("║"))
    print(cyan("║") + f"  ❌ {'Perdidas':<20}: {perdidas:<4} ({pct_p:.1f}%){'':<{W-33}}" + cyan("║"))
    print(bold(cyan(f"╠{'═'*W}╣")))

    gan_str = f"+${ganado:.4f} USDT"
    per_str = f"-${abs(perdido):.4f} USDT"
    net_str = f"{'+'if neto>=0 else ''}${neto:.4f} USDT"

    print(cyan("║") + green(f"  💰 {'Total ganado':<20}: {gan_str:<{W-24}}") + cyan("║"))
    print(cyan("║") + red(f"  📉 {'Total perdido':<20}: {per_str:<{W-24}}") + cyan("║"))

    neto_line = f"  🏦 {'Resultado neto':<20}: {net_str:<{W-24}}"
    if neto >= 0:
        print(cyan("║") + green(neto_line) + cyan("║"))
    else:
        print(cyan("║") + red(neto_line) + cyan("║"))

    print(bold(cyan(f"╠{'═'*W}╣")))
    if mejor:
        print(cyan("║") + green(f"  🏆 Mejor op : {mejor['symbol']} {mejor['side']} +${mejor['pnl']:.4f}{'':<{W-42}}") + cyan("║"))
    if peor:
        print(cyan("║") + red(f"  💀 Peor op  : {peor['symbol']} {peor['side']} -${abs(peor['pnl']):.4f}{'':<{W-43}}") + cyan("║"))

    print(bold(cyan(f"╚{'═'*W}╝\n")))

def mostrar_historial(ultimas: int = 10):
    """Muestra las últimas N operaciones."""
    data = _load()
    ops  = data['operaciones'][-ultimas:]

    if not ops:
        print("\n  📋 Sin historial aún\n")
        return

    def _clr(t, c): return f"\033[{c}m{t}\033[0m"
    cyan  = lambda t: _clr(t, '96')
    green = lambda t: _clr(t, '92')
    red   = lambda t: _clr(t, '91')
    bold  = lambda t: _clr(t, '1')
    dim   = lambda t: _clr(t, '2')

    W = 62
    print(bold(cyan(f"\n╔{'═'*W}╗")))
    print(bold(cyan("║")) + bold(f"  📋 ÚLTIMAS {ultimas} OPERACIONES{'':<{W-28}}") + bold(cyan("║")))
    print(bold(cyan(f"╠{'═'*W}╣")))
    print(cyan("║") + dim(f"  {'Fecha':<20} {'Símbolo':<12} {'Lado':<6} {'Capital':>8} {'PnL neto':>12}  ") + cyan("║"))
    print(cyan(f"╠{'─'*W}╣"))

    for op in reversed(ops):
        fecha   = op['fecha'][5:]  # quitar año
        symbol  = op['symbol'].replace('USDT','')
        side    = op['side']
        capital = f"${op['capital']:.2f}"
        pnl_v   = op['pnl']
        pnl_s   = f"+${pnl_v:.4f}" if pnl_v >= 0 else f"-${abs(pnl_v):.4f}"

        row = f"  {fecha:<20} {symbol:<12} {side:<6} {capital:>8} {pnl_s:>12}  "
        if pnl_v >= 0:
            print(cyan("║") + green(row) + cyan("║"))
        else:
            print(cyan("║") + red(row) + cyan("║"))

    print(bold(cyan(f"╚{'═'*W}╝\n")))

if __name__ == '__main__':
    mostrar_estadisticas()
    mostrar_historial()