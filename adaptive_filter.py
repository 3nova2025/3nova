"""
adaptive_filter.py
==================
Módulo de aprendizaje adaptativo.

CORRECCIÓN BUG10:
  recover_score_range: new_penalized se construye desde cero; si wr>=WIN_RATE_OK
  el label nunca fue añadido, entonces el .remove() nunca ejecutaba.
  Ahora se compara contra el estado previo (current_state) en lugar de new_penalized.

CORRECCIÓN BUG11 (2026-05):
  Las señales aprobadas por el modo FLEXIBLE pasan directamente sin ser filtradas.
  El flexible ya tiene su propio sistema de puntuación (score ≥ 3) y no debe ser
  bloqueado por el adaptive filter.
"""
import json
import os
import time
from datetime import datetime

from console_util import safe_print as _safe_print

STATS_FILE    = 'estadisticas.json'
ADAPTIVE_FILE = 'adaptive_state.json'

MIN_OPS_TO_LEARN  = 50
MIN_OPS_PER_GROUP = 10
WIN_RATE_FLOOR    = 0.40
WIN_RATE_OK       = 0.55
UPDATE_INTERVAL   = 300

DEFAULT_SCORE_1H  = 13  # umbral mínimo filtro maestro
DEFAULT_SCORE_15M = 13
DEFAULT_SCORE_4H  = 13

MAX_SCORE_PENALTY      = 3
PENALTY_PER_BAD_GROUP  = 1

ADX_CATS = {
    'high':     (25, 100),
    'moderate': (18, 24),
    'low':      (0, 17),
}
VOL_CATS = {
    'high':   (0.7, 1.0),
    'medium': (0.3, 0.69),
    'low':    (0.0, 0.29),
}
TREND_STRENGTH_CATS = {
    'strong':   (0.8, 1.0),
    'moderate': (0.5, 0.79),
    'weak':     (0.0, 0.49),
}
TREND_DIR_CATS = {
    'bullish': 'BULLISH',
    'bearish': 'BEARISH',
    'neutral': 'NEUTRAL',
}

# Score técnico del filtro maestro (no quality_score BOTAI)
SCORE_RANGES = [
    (11, 12, 'score_minimo'),     # aprobación mínima
    (13, 15, 'score_solido'),     # aprobación sólida
    (16, 18, 'score_fuerte'),     # aprobación fuerte
    (19, 25, 'score_excepcional'),# aprobación excepcional
]

HOUR_BLOCKS = {
    'madrugada': list(range(0, 6)),
    'mañana':    list(range(6, 12)),
    'tarde':     list(range(12, 18)),
    'noche':     list(range(18, 24)),
}

PENALIZE_BAD_HOURS = True


def _clr(t, c): return f"\033[{c}m{t}\033[0m"
def cyan(t):   return _clr(t, '96')
def green(t):  return _clr(t, '92')
def red(t):    return _clr(t, '91')
def yellow(t): return _clr(t, '93')
def bold(t):   return _clr(t, '1')
def dim(t):    return _clr(t, '2')


# ── Carga / guardado ──────────────────────────────────────────
def _load_stats() -> list:
    if not os.path.exists(STATS_FILE):
        return []
    try:
        with open(STATS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f).get('operaciones', [])
    except:
        return []


def _load_state() -> dict:
    if not os.path.exists(ADAPTIVE_FILE):
        return _default_state()
    try:
        with open(ADAPTIVE_FILE, 'r') as f:
            return json.load(f)
    except:
        return _default_state()


def _save_state(state: dict):
    try:
        with open(ADAPTIVE_FILE, 'w') as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        _safe_print(f"  [!] [ADAPTIVE] No se pudo guardar estado: {e}")


def _default_state() -> dict:
    return {
        'last_update':     0,
        'total_ops_seen':  0,
        'score_min': {
            '1h':  DEFAULT_SCORE_1H,
            '15m': DEFAULT_SCORE_15M,
            '4h':  DEFAULT_SCORE_4H,
        },
        'blocked_hours':    [],
        'blocked_tfs':      [],
        'penalized_scores': [],
        'group_stats':      {},
        'ajustes_log':      [],
    }


# ── Extracción de características ────────────────────────────
def _extract_features(signal: dict) -> dict:
    metrics        = signal.get('metrics', {})
    side           = signal.get('side', '')
    trend_raw      = metrics.get('trend', '').upper()
    trend_strength = float(metrics.get('trend_strength', 0.5))
    adx            = float(metrics.get('adx14', 0))
    vol            = float(metrics.get('volume_strength', 0.5))
    # Score técnico del filtro maestro (no quality_score BOTAI)
    score          = int(signal.get('puntaje', signal.get('score_tecnico', 0)))

    adx_cat = 'high' if adx >= ADX_CATS['high'][0] else \
              'moderate' if adx >= ADX_CATS['moderate'][0] else 'low'

    vol_cat = 'high' if vol >= VOL_CATS['high'][0] else \
              'medium' if vol >= VOL_CATS['medium'][0] else 'low'

    strength_cat = 'strong' if trend_strength >= TREND_STRENGTH_CATS['strong'][0] else \
                   'moderate' if trend_strength >= TREND_STRENGTH_CATS['moderate'][0] else 'weak'

    trend_dir = 'neutral'
    if 'BULLISH' in trend_raw:
        trend_dir = 'bullish'
    elif 'BEARISH' in trend_raw:
        trend_dir = 'bearish'
    aligned = (side == 'LONG' and trend_dir == 'bullish') or \
              (side == 'SHORT' and trend_dir == 'bearish')
    trend_aligned = 'aligned' if aligned else 'opposed'

    score_label = None
    for lo, hi, label in SCORE_RANGES:
        if lo <= score <= hi:
            score_label = label
            break

    # Clasificación por símbolo (top pares)
    symbol = signal.get('symbol', '').replace('USDT', '')
    symbol_cat = symbol if symbol else 'UNKNOWN'

    return {
        'adx':            adx_cat,
        'volume':         vol_cat,
        'trend_strength': strength_cat,
        'trend_aligned':  trend_aligned,
        'score_range':    score_label,
        'timeframe':      signal.get('timeframe', ''),
        'symbol':         symbol_cat,
        'side':           signal.get('side', ''),
    }


# ── Actualizar estadísticas de grupo ─────────────────────────
def _update_group_stats(groups: dict, features: dict, won: bool, pnl: float):
    tf = features.get('timeframe')
    if tf:
        key = f'tf_{tf}'
        if key not in groups:
            groups[key] = {'total': 0, 'ganadas': 0, 'pnl_sum': 0.0}
        groups[key]['total']   += 1
        groups[key]['ganadas'] += 1 if won else 0
        groups[key]['pnl_sum'] += pnl

    for feat_key, cat in [
        ('adx',            features['adx']),
        ('vol',            features['volume']),
        ('trend_strength', features['trend_strength']),
        ('trend_aligned',  features['trend_aligned']),
    ]:
        key = f'{feat_key}_{cat}'
        if key not in groups:
            groups[key] = {'total': 0, 'ganadas': 0, 'pnl_sum': 0.0}
        groups[key]['total']   += 1
        groups[key]['ganadas'] += 1 if won else 0
        groups[key]['pnl_sum'] += pnl

    if features['score_range']:
        key = features['score_range']
        if key not in groups:
            groups[key] = {'total': 0, 'ganadas': 0, 'pnl_sum': 0.0}
        groups[key]['total']   += 1
        groups[key]['ganadas'] += 1 if won else 0
        groups[key]['pnl_sum'] += pnl

    # Tracking por símbolo
    if features.get('symbol'):
        key = f"symbol_{features['symbol']}"
        if key not in groups:
            groups[key] = {'total': 0, 'ganadas': 0, 'pnl_sum': 0.0}
        groups[key]['total']   += 1
        groups[key]['ganadas'] += 1 if won else 0
        groups[key]['pnl_sum'] += pnl

    # Tracking por side (LONG/SHORT)
    if features.get('side'):
        key = f"side_{features['side']}"
        if key not in groups:
            groups[key] = {'total': 0, 'ganadas': 0, 'pnl_sum': 0.0}
        groups[key]['total']   += 1
        groups[key]['ganadas'] += 1 if won else 0
        groups[key]['pnl_sum'] += pnl


# ── Recalcular ajustes ────────────────────────────────────────
def _compute_adjustments(groups: dict, current_state: dict) -> dict:
    """
    CORRECCIÓN BUG10:
    new_penalized se construye desde cero. Para que 'recover' funcione,
    comparamos contra current_state['penalized_scores'] (el estado anterior),
    no contra new_penalized (que aún está vacío cuando se decide no penalizar).
    """
    prev_penalized = set(current_state['penalized_scores'])  # ← estado anterior

    changes = {
        'score_min':        dict(current_state['score_min']),
        'penalized_scores': list(current_state['penalized_scores']),
        'ajustes_log':      [],
    }

    new_penalized = []
    for lo, hi, label in SCORE_RANGES:
        g = groups.get(label, {})
        if g.get('total', 0) < MIN_OPS_PER_GROUP:
            # sin datos suficientes → mantener estado previo
            if label in prev_penalized:
                new_penalized.append(label)
            continue

        wr = g['ganadas'] / g['total']
        g['win_rate'] = wr

        if wr < WIN_RATE_FLOOR:
            new_penalized.append(label)
            if label not in prev_penalized:
                # recién penalizado
                changes['ajustes_log'].append({
                    'tipo': 'penalize_score_range',
                    'rango': f'{lo}-{hi}',
                    'wr': wr,
                    'motivo': f'Score {lo}-{hi}: WR={wr:.0%}',
                })
        elif wr >= WIN_RATE_OK:
            # NO se añade a new_penalized
            if label in prev_penalized:
                # ← CORRECCIÓN: comparar contra prev_penalized, no new_penalized
                changes['ajustes_log'].append({
                    'tipo': 'recover_score_range',
                    'rango': f'{lo}-{hi}',
                    'wr': wr,
                    'motivo': f'Score {lo}-{hi} recuperado: WR={wr:.0%}',
                })
        else:
            # wr entre FLOOR y OK — mantener estado previo
            if label in prev_penalized:
                new_penalized.append(label)

    changes['penalized_scores'] = new_penalized

    # Ajustar score mínimo según peor grupo relevante
    relevant_groups = [
        'adx_low', 'adx_moderate', 'adx_high',
        'vol_low', 'vol_medium', 'vol_high',
        'trend_strength_weak', 'trend_strength_moderate', 'trend_strength_strong',
        'trend_aligned_opposed', 'trend_aligned_aligned',
    ]
    worst_wr    = 1.0
    worst_group = None
    for gname in relevant_groups:
        g = groups.get(gname, {})
        if g.get('total', 0) >= MIN_OPS_PER_GROUP:
            wr = g['ganadas'] / g['total']
            if wr < worst_wr:
                worst_wr    = wr
                worst_group = gname

    if worst_group and worst_wr < WIN_RATE_FLOOR:
        penalty = min(MAX_SCORE_PENALTY, int((WIN_RATE_FLOOR - worst_wr) * 20))
        if penalty > 0:
            for tf in ['1h', '15m']:
                current = changes['score_min'].get(tf, DEFAULT_SCORE_15M)
                new_min = min(current + penalty, 99)
                if new_min != current:
                    changes['score_min'][tf] = new_min
                    changes['ajustes_log'].append({
                        'tipo': 'score_tf', 'tf': tf,
                        'antes': current, 'despues': new_min,
                        'motivo': f'Peor grupo "{worst_group}" WR={worst_wr:.0%} → +{penalty} pts',
                    })
    else:
        for tf in ['1h', '15m']:
            default = DEFAULT_SCORE_1H if tf == '1h' else DEFAULT_SCORE_15M
            current = changes['score_min'].get(tf, default)
            if current > default:
                changes['score_min'][tf] = default
                changes['ajustes_log'].append({
                    'tipo': 'score_tf_recover', 'tf': tf,
                    'antes': current, 'despues': default,
                    'motivo': 'Grupos de indicadores recuperados',
                })

    return changes


# ── Imprimir cambios ─────────────────────────────────────────
def _print_changes(log_entries: list, groups: dict):
    if not log_entries:
        return
    W = 56
    _safe_print(bold(cyan(f"\n╔{'═'*W}╗")))
    _safe_print(bold(cyan("║")) + bold(f"  ADAPTIVE FILTER - AJUSTE AUTOMATICO{'':<{W-40}}") + bold(cyan("║")))
    _safe_print(bold(cyan(f"╠{'═'*W}╣")))
    for e in log_entries:
        tipo = e.get('tipo', '')
        if 'score_tf' in tipo and 'recover' not in tipo:
            _safe_print(cyan("║") + yellow(f"  + Score min {e['tf']}: {e['antes']} -> {e['despues']}  ({e['motivo']})"[:W].ljust(W)) + cyan("║"))
        elif 'score_tf_recover' in tipo:
            _safe_print(cyan("║") + green(f"  - Score min {e['tf']}: {e['antes']} -> {e['despues']}  ({e['motivo']})"[:W].ljust(W)) + cyan("║"))
        elif 'penalize_score_range' in tipo:
            _safe_print(cyan("║") + yellow(f"  ! Score {e['rango']} penalizado - WR={e['wr']:.0%}"[:W].ljust(W)) + cyan("║"))
        elif 'recover_score_range' in tipo:
            _safe_print(cyan("║") + green(f"  OK Score {e['rango']} recuperado - WR={e['wr']:.0%}"[:W].ljust(W)) + cyan("║"))
    _safe_print(bold(cyan(f"╠{'═'*W}╣")))
    for g in ['adx_high', 'vol_high', 'trend_aligned_aligned', 'trend_strength_strong']:
        grp = groups.get(g, {})
        if grp.get('total', 0) >= MIN_OPS_PER_GROUP:
            wr  = grp.get('win_rate', 0)
            col = green if wr >= WIN_RATE_OK else (red if wr < WIN_RATE_FLOOR else yellow)
            _safe_print(cyan("║") + col(f"  {g:<20}: {grp['total']:>3} ops | WR={wr:.0%} | avg={grp.get('pnl_avg',0):+.4f}"[:W].ljust(W)) + cyan("║"))
    _safe_print(bold(cyan(f"╚{'═'*W}╝\n")))


# ── Clase principal ───────────────────────────────────────────
class AdaptiveFilter:

    def __init__(self):
        self._state = _load_state()
        if not os.path.exists(ADAPTIVE_FILE):
            _save_state(self._state)
        self._print_status()

    def _print_status(self):
        state = self._state
        W  = 56
        sm = state['score_min']
        _safe_print(bold(cyan(f"\n╔{'═'*W}╗")))
        _safe_print(bold(cyan("║")) + bold(f"  ADAPTIVE FILTER - Estado actual{'':<{W-36}}") + bold(cyan("║")))
        _safe_print(bold(cyan(f"╠{'═'*W}╣")))
        _safe_print(cyan("║") + f"  Score min 1h : {sm.get('1h', DEFAULT_SCORE_1H):<{W-16}}" + cyan("║"))
        _safe_print(cyan("║") + f"  Score min 15m: {sm.get('15m', DEFAULT_SCORE_15M):<{W-16}}" + cyan("║"))
        pen = state['penalized_scores']
        if pen:
            ranges_str = ', '.join(f"{lo}-{hi}" for lo, hi, lbl in SCORE_RANGES if lbl in pen)
            _safe_print(cyan("║") + yellow(f"  Scores penalizados: {ranges_str:<{W-21}}") + cyan("║"))
        else:
            _safe_print(cyan("║") + dim(f"  Sin scores penalizados{'':<{W-23}}") + cyan("║"))
        ops    = _load_stats()
        needed = max(0, MIN_OPS_TO_LEARN - len(ops))
        if needed > 0:
            _safe_print(cyan("║") + dim(f"  Aprendizaje activo en: {needed} ops mas{'':<{W-34}}") + cyan("║"))
        else:
            _safe_print(cyan("║") + green(f"  Aprendizaje activo - {len(ops)} ops en historial{'':<{W-47}}") + cyan("║"))
        _safe_print(bold(cyan(f"╚{'═'*W}╝\n")))

    def check_signal(self, raw: dict) -> tuple[bool, str]:
        """
        Verifica si una señal debe ser aceptada o rechazada según el historial.
        
        🔥 CORRECCIÓN BUG11 (2026-05):
        Las señales aprobadas por el modo FLEXIBLE pasan directamente sin ser filtradas.
        El flexible ya tiene su propio sistema de puntuación (score ≥ 3) y no debe ser
        bloqueado por el adaptive filter.
        """
        # ── Si la señal viene del modo FLEXIBLE, pasar directo ──
        if raw.get('modo_filtro') == 'FLEXIBLE':
            return True, ""
        
        # ── A partir de aquí, solo para modo ESTRICTO ──
        tf    = raw.get('timeframe', '')
        score = int(raw.get('puntaje', raw.get('score_tecnico', 0)))
        state = self._state

        if tf in state['blocked_tfs']:
            return False, f"TF {tf} bloqueado por bajo rendimiento histórico"

        min_score = state['score_min'].get(tf, DEFAULT_SCORE_15M)
        if score < min_score:
            return False, f"Score técnico {score} < mínimo adaptativo {min_score} para {tf}"

        for lo, hi, label in SCORE_RANGES:
            if lo <= score <= hi and label in state['penalized_scores']:
                return False, f"Score técnico {score} en rango penalizado ({lo}-{hi}), WR histórico bajo"

        features    = _extract_features(raw)
        group_stats = state.get('group_stats', {})
        penalties   = 0
        reasons     = []

        for key, cat in features.items():
            if key == 'score_range':
                continue
            gname = f"{key}_{cat}"
            g     = group_stats.get(gname, {})
            if g.get('total', 0) >= MIN_OPS_PER_GROUP:
                wr = g.get('ganadas', 0) / g['total']
                if wr < WIN_RATE_FLOOR:
                    penalties += PENALTY_PER_BAD_GROUP
                    reasons.append(f"{gname} (WR {wr:.0%})")

        if penalties > 0:
            effective_min = min_score + penalties
            if score < effective_min:
                return False, f"Score {score} insuficiente para {tf} debido a características débiles: {', '.join(reasons)}"

        if PENALIZE_BAD_HOURS:
            hora_actual = datetime.now().hour
            for bloque, horas in HOUR_BLOCKS.items():
                if hora_actual in horas:
                    key = f'hora_{bloque}'
                    g   = group_stats.get(key, {})
                    if g.get('total', 0) >= MIN_OPS_PER_GROUP:
                        wr = g.get('ganadas', 0) / g['total']
                        if wr < WIN_RATE_FLOOR:
                            penalties += 1
                            if score < min_score + penalties:
                                return False, f"Hora {bloque} ({hora_actual:02d}h) con bajo WR ({wr:.0%})"

        return True, ""

    def record_trade(self, signal: dict, result: dict):
        features = _extract_features(signal)
        won      = result.get('win', False)
        pnl      = result.get('pnl', 0.0)

        groups = self._state.get('group_stats', {})
        _update_group_stats(groups, features, won, pnl)

        for g in groups.values():
            if g['total'] > 0:
                g['win_rate'] = g['ganadas'] / g['total']
                g['pnl_avg']  = g['pnl_sum'] / g['total']

        self._state['group_stats'] = groups
        _save_state(self._state)
        self.force_update()

    def maybe_update(self):
        ahora = time.time()
        if ahora - self._state['last_update'] < UPDATE_INTERVAL:
            return
        ops = _load_stats()
        if len(ops) < MIN_OPS_TO_LEARN:
            self._state['last_update'] = ahora
            return
        if len(ops) == self._state.get('total_ops_seen', 0):
            self._state['last_update'] = ahora
            return
        self._recalculate(ops)
        self._state['last_update']    = ahora
        self._state['total_ops_seen'] = len(ops)
        _save_state(self._state)

    def force_update(self):
        ops = _load_stats()
        if len(ops) < MIN_OPS_TO_LEARN:
            _safe_print(f"  [ADAPTIVE] Solo {len(ops)} ops - necesita {MIN_OPS_TO_LEARN} para aprender")
            return
        self._recalculate(ops)
        self._state['last_update']    = time.time()
        self._state['total_ops_seen'] = len(ops)
        _save_state(self._state)

    def _recalculate(self, ops: list):
        groups = self._state.get('group_stats', {})
        for g in groups.values():
            if g['total'] > 0:
                g['win_rate'] = g['ganadas'] / g['total']
                g['pnl_avg']  = g['pnl_sum'] / g['total']

        cambios = _compute_adjustments(groups, self._state)
        if not cambios['ajustes_log']:
            return

        self._state['score_min']        = cambios['score_min']
        self._state['penalized_scores'] = cambios['penalized_scores']
        ts_now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        for e in cambios['ajustes_log']:
            e['timestamp'] = ts_now
        self._state['ajustes_log'] = (self._state.get('ajustes_log', []) + cambios['ajustes_log'])[-20:]
        _print_changes(cambios['ajustes_log'], groups)

    def get_score_min(self, tf: str) -> int:
        return self._state['score_min'].get(tf, DEFAULT_SCORE_15M)

    def print_report(self):
        groups = self._state.get('group_stats', {})
        W = 56
        _safe_print(bold(cyan(f"\n╔{'═'*W}╗")))
        _safe_print(bold(cyan("║")) + bold(f"  Rendimiento por grupos de indicadores{'':<{W-38}}") + bold(cyan("║")))
        _safe_print(bold(cyan(f"╠{'═'*W}╣")))
        for key in sorted(groups.keys()):
            g = groups[key]
            if g['total'] < MIN_OPS_PER_GROUP:
                continue
            wr  = g['win_rate']
            col = green if wr >= WIN_RATE_OK else (red if wr < WIN_RATE_FLOOR else yellow)
            _safe_print(cyan("║") + col(f"  {key:<25}: {g['total']:>3} ops | WR={wr:.0%} | avg={g.get('pnl_avg',0):+.4f}"[:W].ljust(W)) + cyan("║"))
        _safe_print(bold(cyan(f"╚{'═'*W}╝\n")))


# Instancia global
adaptive_filter = AdaptiveFilter()

if __name__ == '__main__':
    _safe_print("\n" + "="*60)
    _safe_print("  ADAPTIVE FILTER - Diagnostico")
    _safe_print("="*60)
    af = AdaptiveFilter()
    af.force_update()
    af.print_report()