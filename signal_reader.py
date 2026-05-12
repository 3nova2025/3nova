"""
signal_reader.py - LEE DE TXT pero conserva TODOS los métodos originales
🆕 CORRECCIÓN: Al liberar un slot, siempre toma la SEÑAL MÁS RECIENTE del archivo
              (último consecutivo), marcando como procesadas las intermedias.
"""

import json
import asyncio
import time
import os
import sys
from pathlib import Path
from typing import Optional, Dict, Set

from console_util import safe_print

# Asegurar que el directorio raíz esté en el path
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.append(current_dir)

from bitunix_api import get_ticker
from capital_manager import is_symbol_blocked
from adaptive_filter import adaptive_filter
from signal_logger import log_aprobada, log_rechazada, log_aprobada_flexible, log_rechazada_flexible

from MAESTRO_FILTRO_V1_FASE4_INTEGRACION import FiltroMaestroIntegrado

# Inicializar el filtro maestro UNA sola vez
filtro_maestro = FiltroMaestroIntegrado()

# ============================================================
# Sistema de rechazos permanentes
# ============================================================
# ============================================================
# CARPETA BASE — todo se guarda aquí
# ============================================================
BOT_DIR = Path(r'C:\Users\DMG TECNOLOGIA\Videos\bitunix\bitunix2')
BOT_DIR.mkdir(parents=True, exist_ok=True)

REJECTED_PERMANENT_FILE = BOT_DIR / 'rejected_permanent.json'

class RejectedPermanentManager:
    def __init__(self):
        self._rejected_keys: Set[str] = set()
        self._load()
    
    def _load(self):
        try:
            if REJECTED_PERMANENT_FILE.exists():
                with open(REJECTED_PERMANENT_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self._rejected_keys = set(data.get('rejected_keys', []))
                    safe_print(f"  📂 [REJECTED_PERM] Cargados {len(self._rejected_keys)} rechazos permanentes")
        except Exception as e:
            safe_print(f"  ⚠️ [REJECTED_PERM] Error cargando: {e}")
    
    def _save(self):
        try:
            with open(REJECTED_PERMANENT_FILE, 'w', encoding='utf-8') as f:
                json.dump({'rejected_keys': list(self._rejected_keys)}, f, indent=2)
        except Exception as e:
            safe_print(f"  ⚠️ [REJECTED_PERM] Error guardando: {e}")
    
    def is_rejected(self, key: str) -> bool:
        return key in self._rejected_keys
    
    def mark_rejected(self, key: str, motivo: str = ""):
        if key not in self._rejected_keys:
            self._rejected_keys.add(key)
            self._save()
            safe_print(f"  🚫 [REJECTED_PERM] Señal {key} rechazada permanente: {motivo[:50]}")
    
    def get_stats(self) -> dict:
        return {'rechazados_permanentes': len(self._rejected_keys)}

rejected_permanent = RejectedPermanentManager()


# ============================================================
# Constantes — todos los archivos en bitunix2
# ============================================================
SIGNALS_TXT_FILE      = BOT_DIR / 'puente' / 'registro_secuencial.txt'
PROCESSED_LINE_FILE   = BOT_DIR / 'processed_lines.txt'
LAST_CONSECUTIVE_FILE = BOT_DIR / 'last_consecutive.txt'  # respaldo del último consecutivo procesado
EXECUTED_STATE_FILE   = BOT_DIR / 'executed_signals.json'
REJECTED_STATE_FILE   = BOT_DIR / 'rejected_signals.json'

ENTRY_TOLERANCE = 0.005
MAX_AGE_SECONDS = 300
READ_RETRIES = 5
RETRY_DELAY = 0.2
SCAN_INTERVAL = 2
SYMBOLS_BLOCKED = {'PIXELUSDT', 'SEIUSDT', 'ZKUSDT', 'TRXUSDT'}
ESPERA_MAX = {'15m': 2 * 3600, '1h': 4 * 3600, '4h': 6 * 3600}
ESPERA_DEFAULT = 2 * 3600
RECHECK_SECS = 60
REJECT_MAX_ATTEMPTS = 4
REJECT_RECHECK_INTERVAL = 5 * 60
REJECT_MAX_AGE_HOURS = 1


# ============================================================
# Gestor de líneas procesadas (NUEVO para TXT)
# ============================================================
class ProcessedLineManager:
    def __init__(self, processed_file: Path):
        self.processed_file = processed_file
        self.processed_numbers: Set[int] = set()
        self._load()
    
    def _load(self):
        try:
            if self.processed_file.exists():
                with open(self.processed_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line.isdigit():
                            self.processed_numbers.add(int(line))
                safe_print(f"  📂 [PROCESSED] Cargados {len(self.processed_numbers)} números de línea procesados")
        except Exception as e:
            safe_print(f"  ⚠️ [PROCESSED] Error cargando: {e}")
    
    def _save(self):
        try:
            with open(self.processed_file, 'w', encoding='utf-8') as f:
                for num in sorted(self.processed_numbers):
                    f.write(f"{num}\n")
        except Exception as e:
            safe_print(f"  ⚠️ [PROCESSED] Error guardando: {e}")
        # Guardar siempre el último consecutivo como respaldo independiente
        try:
            ultimo = max(self.processed_numbers) if self.processed_numbers else 0
            if ultimo > 0:
                with open(LAST_CONSECUTIVE_FILE, 'w', encoding='utf-8') as f:
                    f.write(str(ultimo))
        except:
            pass

    def _load(self):
        try:
            if self.processed_file.exists():
                with open(self.processed_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line.isdigit():
                            self.processed_numbers.add(int(line))
                safe_print(f"  📂 [PROCESSED] Cargados {len(self.processed_numbers)} números de línea procesados")
            
            # Si processed_lines.txt se perdió, restaurar desde el respaldo
            if not self.processed_numbers and LAST_CONSECUTIVE_FILE.exists():
                try:
                    with open(LAST_CONSECUTIVE_FILE, 'r', encoding='utf-8') as f:
                        ultimo = int(f.read().strip())
                    if ultimo > 0:
                        # Marcar todos los consecutivos hasta el último como procesados
                        for n in range(1, ultimo + 1):
                            self.processed_numbers.add(n)
                        self._save()
                        safe_print(f"  🔁 [PROCESSED] Restaurado desde respaldo: hasta consecutivo #{ultimo}")
                except:
                    pass
        except Exception as e:
            safe_print(f"  ⚠️ [PROCESSED] Error cargando: {e}")

    def is_processed(self, line_num: int) -> bool:
        return line_num in self.processed_numbers
    
    def mark_processed(self, line_num: int):
        if line_num not in self.processed_numbers:
            self.processed_numbers.add(line_num)
            self._save()
    
    def get_last_processed(self) -> int:
        return max(self.processed_numbers) if self.processed_numbers else 0

processed_manager = ProcessedLineManager(PROCESSED_LINE_FILE)


# ============================================================
# Funciones originales adaptadas para TXT
# ============================================================
def _read_signals_from_txt() -> dict:
    """
    Lee señales del TXT.
    ✅ CORREGIDO: Solo devuelve la SEÑAL MÁS RECIENTE (último consecutivo del archivo).
    Las señales antiguas pendientes se marcan automáticamente como procesadas para evitar
    operar con precios viejos.
    
    Formato de cada línea: "42 - 2026-04-27 08:42:50 - {...json...}"
    El número 42 es asignado por puente.py y nunca cambia aunque se reinicie el bot.
    """
    result = {}
    
    if not SIGNALS_TXT_FILE.exists():
        return result
    
    try:
        # 1. Leer todas las líneas válidas con su consecutivo
        valid_lines = []
        with open(SIGNALS_TXT_FILE, 'r', encoding='utf-8') as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    idx = line.index(' - ')
                    cons_str = line[:idx].strip()
                    if cons_str.isdigit():
                        valid_lines.append((int(cons_str), line))
                except:
                    continue
        
        if not valid_lines:
            return result
        
        # 2. Obtener el consecutivo más alto (última señal generada por el puente)
        valid_lines.sort(key=lambda x: x[0])
        max_cons, max_line = valid_lines[-1]
        
        # 3. ✅ Marcar TODOS los consecutivos ANTERIORES como procesados (saltados)
        for cons, _ in valid_lines:
            if cons < max_cons:
                processed_manager.mark_processed(cons)
        
        # 4. Si la última señal ya fue procesada, no devolver nada
        if processed_manager.is_processed(max_cons):
            return {}
        
        # 5. Parsear SOLO la última línea
        partes = max_line.split(' - ', 2)
        if len(partes) < 3:
            processed_manager.mark_processed(max_cons)
            return {}
        
        json_str = partes[2]
        signal_data = json.loads(json_str)
        
        # Clave única: símbolo + consecutivo del puente
        key = f"{signal_data.get('symbol')}_{max_cons}"
        result[key] = signal_data
        result[key]['_consecutivo'] = max_cons
        return result
        
    except Exception as e:
        safe_print(f"  ❌ Error leyendo {SIGNALS_TXT_FILE}: {e}")
        return {}


# Mantener la función original para compatibilidad (pero no se usa)
def _read_signals_file() -> dict:
    """Legacy: mantiene compatibilidad pero ahora lee del TXT"""
    return _read_signals_from_txt()


def _build_signal_hash(raw: dict) -> str:
    """Hash solo de los campos importantes (sin timestamp ni métricas)"""
    base = {
        'symbol': raw.get('symbol'),
        'side': raw.get('side'),
        'entry': raw.get('entry'),
        'stop': raw.get('stop'),
        'tps': raw.get('tps'),
    }
    return json.dumps(base, sort_keys=True)


def _load_executed_keys() -> Set[str]:
    try:
        with open(EXECUTED_STATE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return set(data.get('executed_keys', []))
    except:
        return set()


def _save_executed_keys(executed_keys: Set[str]):
    try:
        with open(EXECUTED_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump({'executed_keys': list(executed_keys)}, f)
    except:
        pass


def _is_signal_already_executed(raw: dict) -> bool:
    if raw.get('result') is not None:
        return True
    if raw.get('status') in ('closed', 'cancelled', 'expired'):
        return True
    try:
        rp = raw.get('remaining_position')
        if rp is not None and int(rp) != 100:
            return True
    except:
        pass
    return False


def _is_signal_fresh(raw: dict) -> tuple[bool, int]:
    ts_raw = raw.get('timestamp', 0)
    if ts_raw > 1e12:
        ts_raw = ts_raw / 1000.0
    age = time.time() - ts_raw
    return age <= MAX_AGE_SECONDS, int(age)


def _normalize_signal(key: str, raw: dict) -> Optional[dict]:
    symbol = raw.get('symbol', 'unknown')
    safe_print(f"  🔎 [NORM] Procesando {symbol} key={key}")
    
    if _is_signal_already_executed(raw):
        safe_print(f"  ⏭️  [NORM] {symbol} — ya ejecutada (result/status/remaining)")
        return None
    
    _, age = _is_signal_fresh(raw)
    
    if rejected_permanent.is_rejected(key):
        safe_print(f"  ⏭️  [NORM] {symbol} — rechazada permanente")
        return None
    
    score = int(raw.get('quality_score', 0))
    # score minimo lo decide el filtro maestro (Fase 2 duro/flexible), no aqui

    symbol_raw = raw.get('symbol', '')
    side = raw.get('side', '')
    entry = float(raw.get('entry', 0))
    stop = float(raw.get('stop', raw.get('original_stop', 0)))
    tps_raw = raw.get('tps')
    
    if not symbol_raw or not side or not entry or not stop:
        safe_print(f"  ⏭️  [NORM] {symbol} — faltan campos básicos: symbol={symbol_raw} side={side} entry={entry} stop={stop}")
        return None
    
    if not symbol_raw.endswith('USDT'):
        symbol_raw = symbol_raw + 'USDT'
    
    if symbol_raw in SYMBOLS_BLOCKED:
        safe_print(f"  ⏭️  [NORM] {symbol_raw} — símbolo bloqueado")
        return None
    
    tp1_val = None
    tp3_val = None
    if tps_raw and isinstance(tps_raw, dict):
        try:
            tp1_val = float(tps_raw['tp1']) if tps_raw.get('tp1') else None
        except:
            pass
        try:
            tp3_val = float(tps_raw['tp3']) if tps_raw.get('tp3') else None
        except:
            pass

    if not tp1_val or not tp3_val:
        tps_presentes = [k for k in ('tp1', 'tp2', 'tp3') if tps_raw and tps_raw.get(k)]
        safe_print(f"  ⏭️  [{symbol_raw}] Descartada: faltan tp1+tp3 — señal envió: {tps_presentes or 'ninguno'}")
        return None

    tps_list = [tp1_val, tp3_val]
    
    return {
        'key': key,
        'symbol': symbol_raw,
        'side': side.upper(),
        'entry': entry,
        'stop': stop,
        'tps': tps_raw,
        'tps_list': tps_list,
        'quality_score': score,
        'ia_probability': float(raw.get('ia_probability', 0)),
        'timeframe': raw.get('timeframe', ''),
        'timestamp': raw.get('timestamp', time.time()),
        'age_seconds': age,
        'age_minutes': round(age / 60, 1),
        'metrics': raw.get('metrics', {}),
    }


def _is_entry_valid(sig: dict, current_price: float) -> bool:
    entry = sig['entry']
    stop = sig['stop']
    side = sig['side']
    
    if side == 'LONG':
        if current_price > entry * (1 + ENTRY_TOLERANCE):
            return False
        if current_price <= stop:
            return False
    else:
        if current_price < entry * (1 - ENTRY_TOLERANCE):
            return False
        if current_price >= stop:
            return False
    return True


class SignalReader:
    def __init__(self):
        self._executed_keys: Set[str] = _load_executed_keys()
        self._running = False
        self._lock = asyncio.Lock()
        self._cola: Dict[str, dict] = {}
        self._last_recheck: float = 0.0
        self._execute_fn = None
        self._session = None
        self._get_monitor_count = None
        self._max_trades = 0
        
        # Para detección de cambios (hash)
        self._ultimos_hashes: Dict[str, str] = {}
        
        self._rejected = RejectedManager(
            REJECTED_STATE_FILE,
            max_attempts=REJECT_MAX_ATTEMPTS,
            recheck_interval=REJECT_RECHECK_INTERVAL,
            max_age_hours=REJECT_MAX_AGE_HOURS
        )
        
        safe_print(f"  [READER] {len(self._executed_keys)} ejecutadas, "
              f"{self._rejected.get_stats()['total']} rechazadas en seguimiento, "
              f"{rejected_permanent.get_stats()['rechazados_permanentes']} rechazos permanentes")
    
    def _save_state(self):
        _save_executed_keys(self._executed_keys)
    
    def load_executed_from_state(self, executed: list):
        """MÉTODO ORIGINAL - Conservado"""
        self._executed_keys.update(executed)
        self._save_state()
    
    def mark_executed(self, key: str):
        self._executed_keys.add(key)
        self._save_state()
    
    def encolar(self, sig: dict, motivo: str):
        symbol = sig['symbol']
        tf = sig.get('timeframe', '15m')
        espera = ESPERA_MAX.get(tf, ESPERA_DEFAULT)
        ahora = time.time()
        
        if symbol in self._cola:
            return
        
        if rejected_permanent.is_rejected(sig.get('key', '')):
            return
        
        self._cola[symbol] = {
            'sig': sig,
            'ts_recibida': ahora,
            'ts_expira': ahora + espera,
            'entry_orig': sig['entry'],
            'intentos': 0,
            'motivo': motivo,
        }
        safe_print(f"  ⏳ [QUEUE] {symbol} en cola | {espera//60:.0f}min — {motivo}")
    
    async def _procesar_senal_nueva(self, sig: dict):
        """Procesa una señal nueva (recién detectada)"""
        safe_print(f"\n  🔬 [NUEVA SEÑAL] {sig['symbol']} {sig['side']} | Score: {sig['quality_score']} | TF: {sig['timeframe']}")
        
        try:
            res = await filtro_maestro.recibir_senal_externa(
                session=self._session,
                symbol=sig["symbol"],
                direccion=sig["side"],
                precio_entrada=sig["entry"],
                stop=float(sig.get("stop", 0)),
                tps_list=list(sig.get("tps_list", [])),
                temporalidad=sig.get("timeframe", "1h") or "1h",
                quality_score=int(sig.get("quality_score", 0)),
                metrics=sig.get("metrics", {}),
            )
            
            if res.get('aceptada'):
                sig['_filtro_maestro_result'] = res
                if res.get('modo_filtro') == 'FLEXIBLE':
                    log_aprobada_flexible(sig, res)
                    safe_print(f"  🟡 {sig['symbol']} APROBADA (FLEXIBLE) - {res.get('mensaje', '')}")
                else:
                    log_aprobada(sig, res)
                    safe_print(f"  ✅ {sig['symbol']} APROBADA - {res.get('mensaje', '')}")
                
                if self._execute_fn:
                    sig['_analisis_precomputed'] = res
                    ok_exec = await self._execute_fn(sig)
                    if ok_exec:
                        self.mark_executed(sig['key'])
                    else:
                        safe_print(f"  ⚠️ Ejecución fallida, señal marcada como ejecutada")
                        self.mark_executed(sig['key'])
                else:
                    safe_print(f"  ⚠️ No hay execute_fn")
                    self.mark_executed(sig['key'])
            else:
                if res.get('modo_filtro') == 'FLEXIBLE':
                    log_rechazada_flexible(sig, res)
                else:
                    log_rechazada(sig, res)
                motivo = res.get('razon_rechazo') or res.get('mensaje') or 'rechazada'
                safe_print(f"  ❌ {sig['symbol']} RECHAZADA: {motivo[:60]}")
                rejected_permanent.mark_rejected(sig['key'], motivo[:60])
                self.mark_executed(sig['key'])
                
        except Exception as e:
            safe_print(f"  ❌ Error procesando {sig['symbol']}: {e}")
            import traceback
            traceback.print_exc()
    
    async def _procesar_cola(self):
        if not self._cola:
            return
        ahora = time.time()
        if ahora - self._last_recheck < RECHECK_SECS:
            return
        
        try:
            safe_print(f"\n  📋 [QUEUE] Analizando {len(self._cola)} señal(es)...")
            
            for symbol in list(self._cola.keys()):
                if self._get_monitor_count and self._get_monitor_count() >= self._max_trades:
                    break
                
                item = self._cola.get(symbol)
                if not item:
                    continue
                
                if rejected_permanent.is_rejected(item['sig'].get('key', '')):
                    safe_print(f"  🚫 [QUEUE] {symbol} rechazado permanente — eliminando")
                    del self._cola[symbol]
                    continue
                
                ahora = time.time()
                if ahora >= item['ts_expira']:
                    mins = int((ahora - item['ts_recibida']) / 60)
                    safe_print(f"  ⏰ [QUEUE] {symbol} expirada tras {mins}min")
                    del self._cola[symbol]
                    continue
                
                try:
                    ticker = await get_ticker(self._session, symbol)
                    precio = float(ticker.get('markPrice', ticker.get('lastPrice', 0))) if ticker else 0
                except:
                    continue
                
                if not precio:
                    continue
                
                item['intentos'] += 1
                sig_actual = dict(item['sig'])
                sig_actual['entry'] = precio
                if 'tps_list' not in sig_actual and item['sig'].get('tps_list'):
                    sig_actual['tps_list'] = item['sig']['tps_list']
                mins_rest = int((item['ts_expira'] - ahora) / 60)
                 
                safe_print(f"\n  🔬 [QUEUE] Analizando {symbol} | intento #{item['intentos']}")
                safe_print(f"     Precio: {precio:.8g} | Entry orig: {item['entry_orig']:.8g}")
                safe_print(f"     Restante: {mins_rest}min")
                
                try:
                    res = await filtro_maestro.recibir_senal_externa(
                        session=self._session,
                        symbol=sig_actual["symbol"],
                        direccion=sig_actual["side"],
                        precio_entrada=precio,
                        stop=float(sig_actual.get("stop", 0)),
                        tps_list=list(sig_actual.get("tps_list", [])),
                        temporalidad=sig_actual.get("timeframe", "1h") or "1h",
                        quality_score=int(sig_actual.get("quality_score", 0)),
                        metrics=sig_actual.get("metrics", {}),
                    )
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    safe_print(f"  ❌ [QUEUE] Error filtro maestro: {e}")
                    continue
                
                if res.get('aceptada'):
                    sig_actual['_filtro_maestro_result'] = res
                    if res.get('modo_filtro') == 'FLEXIBLE':
                        log_aprobada_flexible(sig_actual, res)
                        safe_print(f"\n  🟡 [QUEUE] {symbol} APROBADA (FLEXIBLE) | {res.get('mensaje','')}")
                    else:
                        log_aprobada(sig_actual, res)
                        safe_print(f"\n  ✅ [QUEUE] {symbol} APROBADA | {res.get('mensaje','')}")
                    if self._execute_fn:
                        try:
                            sig_actual['_analisis_precomputed'] = res
                            ok_exec = await self._execute_fn(sig_actual)
                            if ok_exec:
                                del self._cola[symbol]
                                self.mark_executed(item['sig'].get('key', ''))
                            else:
                                safe_print(f"  ⚠️ Ejecución fallida, se mantiene pendiente")
                                self._executed_keys.discard(item['sig'].get('key', ''))
                                self._save_state()
                        except Exception as e:
                            safe_print(f"  ❌ Error ejecutando: {e}")
                    if not self._execute_fn:
                        safe_print(f"  ⚠️ No hay execute_fn para {symbol}")
                else:
                    if res.get('modo_filtro') == 'FLEXIBLE':
                        log_rechazada_flexible(sig_actual, res)
                    else:
                        log_rechazada(sig_actual, res)
                    motivo = (res.get('razon_rechazo') or res.get('mensaje') or 'rechazada')[:60]
                    safe_print(f"  🚫 [QUEUE] {symbol} RECHAZADA: {motivo}")
                    rejected_permanent.mark_rejected(item['sig'].get('key', ''), motivo)
                    del self._cola[symbol]
                    
        finally:
            self._last_recheck = time.time()

    async def _process_rejected_queue(self):
        pending = self._rejected.get_pending()
        if not pending:
            return
        
        safe_print(f"\n  🔄 [REJECTED] Reanalizando {len(pending)} señales...")
        
        for key, sig in pending.items():
            if rejected_permanent.is_rejected(key):
                safe_print(f"  🚫 {sig.symbol} rechazado permanente — omitiendo")
                self.mark_executed(key)
                self._rejected.mark_attempt(key, success=True)
                continue
            
            if self._get_monitor_count and self._get_monitor_count() >= self._max_trades:
                break

            sig_dict = sig if isinstance(sig, dict) else vars(sig) if hasattr(sig, '__dict__') else {}
            if not sig_dict:
                continue

            symbol = sig_dict.get('symbol', key.split('_')[0])

            try:
                ticker = await get_ticker(self._session, symbol)
                precio = float(ticker.get('markPrice', ticker.get('lastPrice', 0))) if ticker else 0
            except:
                continue

            if not precio:
                continue

            sig_actual = dict(sig_dict)
            sig_actual['entry'] = precio

            try:
                res = await filtro_maestro.recibir_senal_externa(
                    session=self._session,
                    symbol=symbol,
                    direccion=sig_actual.get('side', ''),
                    precio_entrada=precio,
                    stop=float(sig_actual.get('stop', 0)),
                    tps_list=list(sig_actual.get("tps_list", [])),
                    temporalidad=sig_actual.get("timeframe", "1h") or "1h",
                    quality_score=int(sig_actual.get("quality_score", 0)),
                    metrics=sig_actual.get("metrics", {}),
                )
            except Exception as e:
                safe_print(f"  ❌ [REJECTED] Error filtro: {e}")
                continue

            if res.get('aceptada'):
                if res.get('modo_filtro') == 'FLEXIBLE':
                    log_aprobada_flexible(sig_actual, res)
                    safe_print(f"  🟡 [REJECTED] {symbol} APROBADA (FLEXIBLE) en reintento")
                else:
                    log_aprobada(sig_actual, res)
                    safe_print(f"  ✅ [REJECTED] {symbol} APROBADA en reintento")
                self._rejected.mark_attempt(key, success=True)
                self.mark_executed(key)
                if self._execute_fn:
                    sig_actual['_analisis_precomputed'] = res
                    await self._execute_fn(sig_actual)
            else:
                motivo = (res.get('razon_rechazo') or res.get('mensaje') or 'rechazada')[:60]
                safe_print(f"  ❌ [REJECTED] {symbol} sigue rechazada: {motivo}")
                self._rejected.mark_attempt(key, success=False)

    async def _try_open(self):
        """
        VERSIÓN CORREGIDA: Detecta señales por NÚMERO DE LÍNEA, no por hash.
        Así cada línea nueva se procesa UNA SOLA VEZ.
        """
        async with self._lock:
            if self._get_monitor_count and self._get_monitor_count() >= self._max_trades:
                return
            
            # Leer del TXT
            raw_signals = _read_signals_from_txt()
            safe_print(f"  🔍 [SCAN] TXT leído: {len(raw_signals)} señal(es) pendiente(s)")
            if not raw_signals:
                return
            
            # ✅ DETECCIÓN POR CONSECUTIVO DEL PUENTE (número asignado por puente.py)
            # Este número nunca cambia aunque se reinicie el bot, garantizando 0 duplicados.
            senales_a_procesar = []
            
            for key, raw in raw_signals.items():
                # Leer el consecutivo que adjuntamos en _read_signals_from_txt
                consecutivo = raw.get('_consecutivo')
                if consecutivo is None:
                    # Fallback: extraer del key "SYMBOL_42"
                    try:
                        consecutivo = int(key.split('_')[-1])
                    except:
                        continue
                
                # Solo procesar si el consecutivo NO ha sido procesado antes
                if not processed_manager.is_processed(consecutivo):
                    senales_a_procesar.append((key, raw, consecutivo))
            
            if not senales_a_procesar:
                return
            
            safe_print(f"\n  🆕 [SCAN] {len(senales_a_procesar)} señal(es) NUEVA(S) detectada(s)")
            for key, _, consecutivo in senales_a_procesar[:5]:
                safe_print(f"     - Consecutivo #{consecutivo}: {key}")
            
            for key, raw, consecutivo in senales_a_procesar:
                # 🔥 Marcar el consecutivo como procesado ANTES (evita duplicados si hay error)
                processed_manager.mark_processed(consecutivo)
                
                # Verificar si ya fue ejecutada
                if key in self._executed_keys:
                    continue
                
                if rejected_permanent.is_rejected(key):
                    self.mark_executed(key)
                    continue
                
                if raw.get('status') in ('closed', 'cancelled', 'expired'):
                    continue
                
                try:
                    rp = raw.get('remaining_position')
                    if rp is not None and int(rp) != 100:
                        continue
                except:
                    pass
                
                sig = _normalize_signal(key, raw)
                if sig is None:
                    continue
                
                if is_symbol_blocked(sig['symbol']):
                    self.mark_executed(key)
                    continue
                
                # Obtener precio actual
                # NUNCA se desmarca el consecutivo — reader sigue el orden secuencial
                # Si falla el ticker, la señal se encola para reintento sin perder la posición
                try:
                    ticker = await get_ticker(self._session, sig['symbol'])
                    if not ticker:
                        self.encolar(sig, "sin ticker — reintento")
                        continue
                    
                    price = float(ticker.get('markPrice', ticker.get('lastPrice', 0)))
                    if not price:
                        self.encolar(sig, "precio 0 — reintento")
                        continue
                    
                    sig['current_price'] = price
                except:
                    self.encolar(sig, "error ticker — reintento")
                    continue
                
                if not _is_entry_valid(sig, price):
                    self.encolar(sig, f"precio fuera: {price:.8g}")
                    continue
                
                await self._procesar_senal_nueva(sig)

    async def start(self, session, execute_fn, get_monitor_count, max_trades: int):
        self._running = True
        self._execute_fn = execute_fn
        self._session = session
        self._get_monitor_count = get_monitor_count
        self._max_trades = max_trades
        
        # Pre-cargar hashes al arranque
        raw_signals_iniciales = _read_signals_from_txt()
        for key, raw in raw_signals_iniciales.items():
            self._ultimos_hashes[key] = _build_signal_hash(raw)
        
        safe_print(f"\n{'='*60}")
        safe_print(f"  📂 SIGNAL READER - MODO TXT (puente.py)")
        safe_print(f"  📁 Archivo: {SIGNALS_TXT_FILE}")
        safe_print(f"  ⏱️  Escaneo cada {SCAN_INTERVAL} segundos")
        safe_print(f"  🔍 Detección: Por número consecutivo del puente (anti-duplicado)")
        safe_print(f"  🚫 Rechazadas: PERMANENTES (no se reintentan)")
        safe_print(f"  🧠 Integración: Filtro Maestro integrado")
        safe_print(f"  🔄 Fallos de ticker: REINTENTAN automáticamente")
        safe_print(f"  🆕 Arranque limpio: {len(self._ultimos_hashes)} señales existentes ignoradas")
        safe_print(f"  📝 Último consecutivo procesado: #{processed_manager.get_last_processed()}")
        safe_print(f"{'='*60}\n")
        
        last_rejected = 0
        
        while self._running:
            try:
                await self._try_open()
                await self._procesar_cola()
                
                now = time.time()
                if now - last_rejected >= REJECT_RECHECK_INTERVAL:
                    await self._process_rejected_queue()
                    last_rejected = now
                    
            except Exception as e:
                safe_print(f"  ❌ [READER] Error: {e}")
            
            await asyncio.sleep(SCAN_INTERVAL)

    def stop(self):
        self._running = False
        self._save_state()
        stats = self._rejected.get_stats()
        safe_print(f"  📂 [READER] Detenido. Ejecutadas: {len(self._executed_keys)}, "
              f"Rechazadas permanentes: {rejected_permanent.get_stats()['rechazados_permanentes']}")


# Fallback para RejectedManager
try:
    from rejected_manager import RejectedManager
except ImportError:
    class RejectedManager:
        def __init__(self, *a, **k): pass
        def contains(self, k): return False
        def add(self, s, m): pass
        def get_pending(self): return {}
        def mark_attempt(self, k, s=False): pass
        def get_stats(self): return {'pending': 0, 'total': 0}