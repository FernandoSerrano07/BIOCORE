import zoneinfo
from datetime import datetime, timedelta, time, date
from typing import List, Dict, Any
from models import Usuario, Marcaje, TipoMarcaje, Sucursal


def normalizar_tipo_marcaje(tipo) -> str:
    """Normaliza el tipo de marcaje ya sea String o Enum de SQLAlchemy."""
    if hasattr(tipo, "value"):
        return tipo.value.upper()
    return str(tipo).upper()


def calcular_resumen_diario(
    usuario: Usuario,
    marcajes_dia: List[Marcaje],
    zona_horaria_str: str = "America/El_Salvador"
) -> Dict[str, Any]:
    """
    Recibe un objeto Usuario y la lista de Marcajes de un solo día (ordenados cronológicamente).
    Calcula horas normales, horas extra, minutos de tardanza y exceso de almuerzo.
    """
    try:
        tz = zoneinfo.ZoneInfo(zona_horaria_str)
    except Exception:
        tz = zoneinfo.ZoneInfo("America/El_Salvador")

    # Identificar marcajes clave del día
    m_entrada = None
    m_salida = None
    m_salida_alm = None
    m_regreso_alm = None

    for m in marcajes_dia:
        t_tipo = normalizar_tipo_marcaje(m.tipo)
        if t_tipo == "ENTRADA" and not m_entrada:
            m_entrada = m
        elif t_tipo in ["SALIDA", "SALIDA_IMPREVISTA", "SALIDA_ENFERMEDAD"]:
            m_salida = m  # Asigna la última salida registrada del día
        elif t_tipo == "SALIDA_ALMUERZO" and not m_salida_alm:
            m_salida_alm = m
        elif t_tipo == "REGRESO_ALMUERZO":
            m_regreso_alm = m

    # Si falta entrada o salida principal, la jornada está incompleta o ausente
    if not m_entrada or not m_salida:
        return {
            "estado": "INCOMPLETO" if (m_entrada or m_salida) else "AUSENTE",
            "horas_trabajadas": 0.0,
            "horas_normales": 0.0,
            "horas_extra": 0.0,
            "minutos_tardanza": 0,
            "exceso_almuerzo_min": 0,
            "entrada_real": m_entrada.fecha_hora.astimezone(tz).strftime("%H:%M:%S") if m_entrada else None,
            "salida_real": m_salida.fecha_hora.astimezone(tz).strftime("%H:%M:%S") if m_salida else None,
        }

    # Datetimes reales en zona horaria local
    dt_entrada_real = m_entrada.fecha_hora.astimezone(tz)
    dt_salida_real = m_salida.fecha_hora.astimezone(tz)
    fecha_base = dt_entrada_real.date()

    # Horario oficial programado del usuario
    turno_in = usuario.turno_entrada or time(8, 0, 0)
    turno_out = usuario.turno_salida or time(17, 0, 0)

    dt_entrada_oficial = datetime.combine(fecha_base, turno_in, tzinfo=tz)
    dt_salida_oficial = datetime.combine(fecha_base, turno_out, tzinfo=tz)

    # 1. Cálculo de Tardanza
    tolerancia = timedelta(minutes=5)
    limite_entrada = dt_entrada_oficial + tolerancia
    
    minutos_tardanza = 0
    if dt_entrada_real > limite_entrada:
        minutos_tardanza = int((dt_entrada_real - dt_entrada_oficial).total_seconds() // 60)

    # 2. Tiempo de Almuerzo tomado real vs. configurado
    minutos_almuerzo_config = usuario.minutos_almuerzo or 60
    minutos_almuerzo_descontar = minutos_almuerzo_config
    exceso_almuerzo_min = 0

    if m_salida_alm and m_regreso_alm:
        dt_sal_alm = m_salida_alm.fecha_hora.astimezone(tz)
        dt_reg_alm = m_regreso_alm.fecha_hora.astimezone(tz)
        duracion_alm_min = int((dt_reg_alm - dt_sal_alm).total_seconds() // 60)

        if duracion_alm_min > minutos_almuerzo_config:
            exceso_almuerzo_min = duracion_alm_min - minutos_almuerzo_config
            minutos_almuerzo_descontar = duracion_alm_min

    # 3. Tiempo total transcurrido
    tiempo_total_seg = (dt_salida_real - dt_entrada_real).total_seconds()
    horas_totales_reales = max(0.0, (tiempo_total_seg / 3600.0) - (minutos_almuerzo_descontar / 60.0))

    # 4. Horas esperadas del turno
    duracion_turno_seg = (dt_salida_oficial - dt_entrada_oficial).total_seconds()
    horas_programadas = max(0.0, (duracion_turno_seg / 3600.0) - (minutos_almuerzo_config / 60.0))

    # 5. Desglose en Horas Normales y Extra
    horas_normales = 0.0
    horas_extra = 0.0

    if horas_totales_reales <= horas_programadas:
        horas_normales = round(horas_totales_reales, 2)
        horas_extra = 0.0
    else:
        horas_normales = round(horas_programadas, 2)
        if dt_salida_real > dt_salida_oficial:
            segundos_extra = (dt_salida_real - dt_salida_oficial).total_seconds()
            horas_extra = round(segundos_extra / 3600.0, 2)

    return {
        "estado": "PRESENTES",
        "horas_trabajadas": round(horas_totales_reales, 2),
        "horas_normales": horas_normales,
        "horas_extra": horas_extra,
        "minutos_tardanza": minutos_tardanza,
        "exceso_almuerzo_min": exceso_almuerzo_min,
        "entrada_real": dt_entrada_real.strftime("%H:%M:%S"),
        "salida_real": dt_salida_real.strftime("%H:%M:%S")
    }
