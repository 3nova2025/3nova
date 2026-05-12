"""
time_filters.py
Filtro de horarios y días para evitar operar en momentos de baja liquidez o alto riesgo

REGLA ÚNICA DE CIERRE:
- Sábado desde las 11:00 AM hasta el Domingo a las 6:00 PM (18:00) → SIN OPERACIONES
- Todo el resto de la semana opera sin restricciones
"""

import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ_COLOMBIA = ZoneInfo("America/Bogota")  # UTC-5, sin horario de verano


class TimeFilter:
    def __init__(self):
        pass

    def es_horario_valido(self, timestamp: float = None) -> tuple:
        """
        Verifica si el horario actual es válido para operar.
        BLOQUEADO: Sábado 11:00 AM → Domingo 6:00 PM (hora Colombia)
        Retorna: (bool, str) → (válido, mensaje)
        """
        if timestamp is None:
            ahora = datetime.now(TZ_COLOMBIA)
        else:
            ahora = datetime.fromtimestamp(timestamp, tz=TZ_COLOMBIA)

        dia_semana = ahora.weekday()  # 0=Lunes … 5=Sábado, 6=Domingo
        hora = ahora.hour
        minuto = ahora.minute
        hora_decimal = hora + minuto / 60.0

        # ============================================================
        # SÁBADO desde las 11:00 AM → CERRADO
        # ============================================================
        if dia_semana == 5 and hora_decimal >= 11:
            return False, f"❌ Sábado {hora:02d}:{minuto:02d} — cierre de fin de semana (desde las 11:00)"

        # ============================================================
        # DOMINGO hasta las 6:00 PM (18:00) → CERRADO
        # ============================================================
        if dia_semana == 6 and hora_decimal < 18:
            return False, f"❌ Domingo {hora:02d}:{minuto:02d} — cierre de fin de semana (hasta las 18:00)"

        # ============================================================
        # TODO LO DEMÁS → OPERABLE
        # ============================================================
        dias = {0: "Lunes", 1: "Martes", 2: "Miércoles", 3: "Jueves",
                4: "Viernes", 5: "Sábado", 6: "Domingo"}
        nombre_dia = dias[dia_semana]
        return True, f"✅ {nombre_dia} {hora:02d}:{minuto:02d} — horario permitido"

    def filtrar_senal(self, timestamp: float = None) -> dict:
        """
        Filtro principal para usar en el bot.
        Retorna: {'aprobada': bool, 'motivo': str, 'horario_valido': bool}
        """
        valido, mensaje = self.es_horario_valido(timestamp)
        return {
            'aprobada': valido,
            'motivo': mensaje,
            'horario_valido': valido,
        }

    def obtener_estado_ventana(self) -> dict:
        """
        Retorna el estado completo de la ventana operativa para mostrar en dashboard.
        """
        ahora = datetime.now(TZ_COLOMBIA)
        dia_semana = ahora.weekday()
        hora = ahora.hour
        hora_decimal = hora + ahora.minute / 60.0

        # Sábado desde las 11:00
        if dia_semana == 5 and hora_decimal >= 11:
            return {
                'estado': 'inactivo',
                'texto': '🚫 Cierre de fin de semana (Sáb 11:00 – Dom 18:00). Próximo: Domingo 18:00',
                'minutos_restantes': -1,
            }

        # Domingo antes de las 18:00
        if dia_semana == 6 and hora_decimal < 18:
            return {
                'estado': 'inactivo',
                'texto': '🚫 Cierre de fin de semana (Sáb 11:00 – Dom 18:00) — esperando apertura',
                'minutos_restantes': -1,
            }

        return {
            'estado': 'activo',
            'texto': '✅ Horario operativo activo',
            'minutos_restantes': -1,
        }

    def obtener_proximo_datetime_valido(self) -> datetime:
        """Retorna el datetime exacto del próximo horario válido para operar."""
        ahora = datetime.now(TZ_COLOMBIA)
        dia_semana = ahora.weekday()
        hora_decimal = ahora.hour + ahora.minute / 60.0

        # Sábado desde las 11:00 → próximo es Domingo 18:00
        if dia_semana == 5 and hora_decimal >= 11:
            dias_al_domingo = 1
            return ahora.replace(hour=18, minute=0, second=0, microsecond=0) + timedelta(days=dias_al_domingo)

        # Domingo antes de las 18:00 → próximo es hoy a las 18:00
        if dia_semana == 6 and hora_decimal < 18:
            return ahora.replace(hour=18, minute=0, second=0, microsecond=0)

        # Ya está en horario válido
        return ahora

    def obtener_proximo_horario_valido(self) -> str:
        """Retorna string descriptivo del próximo horario válido."""
        proximo = self.obtener_proximo_datetime_valido()
        dias = {0: 'Lunes', 1: 'Martes', 2: 'Miércoles', 3: 'Jueves',
                4: 'Viernes', 5: 'Sábado', 6: 'Domingo'}
        nombre_dia = dias.get(proximo.weekday(), '')
        return f"{nombre_dia} {proximo.strftime('%d/%m')} a las {proximo.strftime('%H:%M')}"

    def obtener_cuenta_regresiva(self) -> str:
        """Retorna string con horas y minutos exactos que faltan para operar."""
        valido, _ = self.es_horario_valido()
        if valido:
            return ""

        ahora = datetime.now(TZ_COLOMBIA)
        proximo = self.obtener_proximo_datetime_valido()
        diff = proximo - ahora
        total_minutos = int(diff.total_seconds() // 60)
        horas = total_minutos // 60
        minutos = total_minutos % 60

        if horas == 0:
            return f"⏳ Faltan {minutos} minutos para activarse"
        elif minutos == 0:
            return f"⏳ Faltan {horas}h exactas para activarse"
        else:
            return f"⏳ Faltan {horas}h {minutos}min para activarse"

    def mostrar_estado(self):
        """Muestra el estado actual del horario con cuenta regresiva."""
        valido, mensaje = self.es_horario_valido()
        ahora = datetime.now(TZ_COLOMBIA)
        dias = {0: 'Lunes', 1: 'Martes', 2: 'Miércoles', 3: 'Jueves',
                4: 'Viernes', 5: 'Sábado', 6: 'Domingo'}
        nombre_dia = dias.get(ahora.weekday(), '')

        print("\n" + "=" * 55)
        print("  ⏰ FILTRO DE HORARIO")
        print("=" * 55)
        print(f"  📅 {nombre_dia} {ahora.strftime('%d/%m/%Y %H:%M')} (Colombia)")
        print(f"  {mensaje}")

        if not valido:
            proximo = self.obtener_proximo_horario_valido()
            cuenta = self.obtener_cuenta_regresiva()
            print(f"  🔜 Próximo horario: {proximo}")
            print(f"  {cuenta}")
            print(f"  🚫 Bot en espera — señales rechazadas hasta entonces")

        print("=" * 55 + "\n")


# Instancia global
time_filter = TimeFilter()


# ══════════════════════════════════════════════════════════════
# TEST
# ══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("🧪 TEST: TimeFilter")
    print("=" * 55)

    horarios_prueba = [
        # Lunes a Viernes → todos deben pasar
        (2026, 4, 13,  0,  0),   # Lunes 00:00      → pasar
        (2026, 4, 13,  5, 30),   # Lunes 05:30      → pasar
        (2026, 4, 13, 12,  0),   # Lunes 12:00      → pasar
        (2026, 4, 13, 23, 30),   # Lunes 23:30      → pasar
        (2026, 4, 17, 17,  0),   # Viernes 17:00    → pasar
        (2026, 4, 17, 23, 59),   # Viernes 23:59    → pasar
        # Sábado antes de 11:00 → pasar
        (2026, 4, 18,  0,  0),   # Sábado 00:00     → pasar
        (2026, 4, 18, 10, 59),   # Sábado 10:59     → pasar
        # Sábado desde las 11:00 → bloquear
        (2026, 4, 18, 11,  0),   # Sábado 11:00     → bloquear
        (2026, 4, 18, 20,  0),   # Sábado 20:00     → bloquear
        # Domingo antes de 18:00 → bloquear
        (2026, 4, 19,  0,  0),   # Domingo 00:00    → bloquear
        (2026, 4, 19, 17, 59),   # Domingo 17:59    → bloquear
        # Domingo desde las 18:00 → pasar
        (2026, 4, 19, 18,  0),   # Domingo 18:00    → pasar
        (2026, 4, 19, 22,  0),   # Domingo 22:00    → pasar
        (2026, 4, 19, 23, 30),   # Domingo 23:30    → pasar
    ]

    for year, month, day, hour, minute in horarios_prueba:
        ts = datetime(year, month, day, hour, minute).timestamp()
        valido, mensaje = time_filter.es_horario_valido(ts)
        estado = "✅ ACTIVO  " if valido else "❌ BLOQUEADO"
        print(f"  {year}-{month:02d}-{day:02d} {hour:02d}:{minute:02d} → {estado} | {mensaje}")

    print("\n" + "=" * 55)
    time_filter.mostrar_estado()