"""Señal de uso de los skills — la etapa 1 que faltaba. Determinista, sin modelo.

**Pregunta:** ¿este skill está aprendiendo, o se usa igual que hace un año?

La etapa 1 original leía sólo `role='tool'` y sólo veía errores. Eso mide una cosa: si el
agente se equivocó. No mide lo que a Mauro le importa — si un skill **mejora con el uso**.

Este módulo lee las señales que el arnés ya mantiene en `.usage.json` y que nadie estaba
mirando. Verificado en la máquina de Mauro (29-sep-2026):

```
172 skills   108 activos   64 archivados
 41 skills usados y parcheados   1.242 parches   0 verificaciones de que mejoraron
 20 skills con >=5 usos y 0 parches
```

El caso que lo resume:

```
andrea-docs               93 usos    0 parches   ← ¿perfecto, o fallando en silencio?
andrea-governance        189 parches  100,3 KB   ← patch_generation = 1
```

189 escrituras contadas como **una sola generación**. Sin una señal de uso, ese acumulador
es invisible.

Las cuatro señales
------------------
```
sin_usar        creado y nunca usado en N días. El agente lo creyó necesario y no lo fue.
sin_cambio      se usa mucho y nunca se parchea. Puede ser perfecto, o fallar sin ruido.
sin_reuso       se parcheó y no se volvió a usar. El parche pudo romperlo.
hinchado        creció fuera de la estructura: cuerpo enorme, profundidad sin separar.
```

`hinchado` viene de dos fuentes que coinciden. DSec (arXiv:2609.22978 §5.1) mide que una
imagen monolítica obliga a reconstruir todo para cambiar una parte: O(m·N) contra O(m) en
capas. Y el propio curador del arnés lo dice en `curator.py:289-293`: *"A SKILL.md body over
~24k chars is a consolidation target on its own [...] push topic depth into references/"*.

DSec mide contenedores, no skills. La transferencia es la estructura, no un resultado
probado para skills, y así se declara.

Por qué determinista
--------------------
Todo sale de contar y fechar. Un modelo acá haría que el mismo `.usage.json` diera
veredictos distintos en dos corridas, y la medición tiene que poder recalcularse.

Lo que este módulo NO decide
----------------------------
No decide qué cambio corresponde, ni si crear un skill, ni si darlo de baja. Clasifica y
declara hechos. La recomendación es de quien lee el reporte.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

try:  # cargado como paquete por el arnés
    from .effect.usage import _usage_file
except ImportError:  # cargado como módulo suelto (tests, ejecución directa)
    from effect.usage import _usage_file  # type: ignore

# -- Umbrales ------------------------------------------------------------------------

#: Días desde la creación para declarar que un skill creado nunca se usó.
SIN_USAR_DIAS = 14.0

#: Usos a partir de los cuales "0 parches" es señal, no casualidad.
SIN_CAMBIO_USOS = 5

#: Días desde el último parche para declarar que un cambio no se reusó.
SIN_REUSO_DIAS = 14.0

#: Bytes del SKILL.md a partir de los cuales el cuerpo dejó de ser "reglas siempre activas".
HINCHADO_BYTES = 24_000

#: Parches a partir de los cuales el desgaste es señal aunque el cuerpo sea chico.
HINCHADO_PARCHES = 50

#: Estados, del más urgente al menos.
ESTADOS = ("sin_usar", "sin_reuso", "hinchado", "sin_cambio", "sano", "desconocido")


@dataclass(frozen=True)
class UsoSkill:
    """Lo que se pudo medir de un skill, y lo que no.

    ``None`` y ``0`` no son lo mismo y no se mezclan: ``use_count=None`` es "no hay
    registro" (no se puede afirmar nada); ``use_count=0`` es una medición real de cero.
    Confundirlos convertiría "no lo medí" en "no se usa", que es la conclusión que este
    módulo existe para no inventar.
    """

    nombre: str
    estado: str
    use_count: Optional[int]
    patch_count: Optional[int]
    patch_generation: Optional[int]
    last_used_ts: Optional[float]
    last_patched_ts: Optional[float]
    created_ts: Optional[float]
    created_by: Optional[str]
    archived: bool
    tamano_bytes: Optional[int]
    ruta: Optional[str]
    motivo: str

    @property
    def activo(self) -> bool:
        return not self.archived

    @property
    def medido(self) -> bool:
        """¿Se pudo leer un registro para este skill?"""
        return self.use_count is not None


@dataclass(frozen=True)
class UsoReport:
    """El barrido completo, con los cuatro cubos y lo que no se pudo medir."""

    skills: tuple[UsoSkill, ...]
    sin_registro: int
    total_activos: int
    scanned_until: float

    def por_estado(self, estado: str) -> tuple[UsoSkill, ...]:
        return tuple(s for s in self.skills if s.estado == estado)

    @property
    def sin_usar(self) -> tuple[UsoSkill, ...]:
        return self.por_estado("sin_usar")

    @property
    def sin_cambio(self) -> tuple[UsoSkill, ...]:
        return self.por_estado("sin_cambio")

    @property
    def sin_reuso(self) -> tuple[UsoSkill, ...]:
        return self.por_estado("sin_reuso")

    @property
    def hinchado(self) -> tuple[UsoSkill, ...]:
        return self.por_estado("hinchado")

    @property
    def trustworthy(self) -> bool:
        """¿Se midió lo suficiente para creer en "no hay nada"?

        Si más de la mitad de los activos no tienen registro, un reporte vacío es en
        realidad "no se pudo mirar". Misma regla que ``RecurrenceReport.trustworthy``.
        """
        if self.total_activos == 0:
            return True
        return self.sin_registro <= self.total_activos


def _parse_iso(value: object) -> Optional[float]:
    """Timestamp ISO del registro a epoch, o ``None``.

    Acepta el sufijo ``Z`` y la ausencia de zona, que se interpreta como UTC — que es lo
    que escribe el arnés. Nunca se asume hora local: correría el conteo varias horas en
    una dirección arbitraria.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    texto = value.strip().replace("Z", "+00:00")
    try:
        from datetime import datetime, timezone

        marca = datetime.fromisoformat(texto)
    except ValueError:
        return None
    if marca.tzinfo is None:
        marca = marca.replace(tzinfo=timezone.utc)
    return marca.timestamp()


def _entero(value: object) -> Optional[int]:
    """Entero no negativo, o ``None``. Un valor raro no se rellena con cero."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0:
        return None
    return int(value)


def _dias(desde: Optional[float], ahora: float) -> Optional[float]:
    if desde is None:
        return None
    return max(0.0, (ahora - desde) / 86400.0)


def _buscar_skill_md(nombre: str, skills_dirs: list[Path]) -> Optional[Path]:
    """Ruta del SKILL.md de un skill, buscando en los directorios que el llamador indica.

    Se busca por nombre de directorio, que es como el arnés resuelve un skill. El orden
    de ``skills_dirs`` importa y lo decide quien llama: el arnés resuelve local primero.
    """
    for raiz in skills_dirs:
        try:
            if not raiz.is_dir():
                continue
            candidato = raiz / nombre / "SKILL.md"
            if candidato.is_file():
                return candidato
            for hallado in raiz.rglob(f"{nombre}/SKILL.md"):
                if hallado.is_file():
                    return hallado
        except OSError:
            continue
    return None


def clasificar(
    *,
    nombre: str,
    registro: Optional[dict],
    ruta: Optional[Path],
    ahora: float,
    sin_usar_dias: float = SIN_USAR_DIAS,
    sin_cambio_usos: int = SIN_CAMBIO_USOS,
    sin_reuso_dias: float = SIN_REUSO_DIAS,
    hinchado_bytes: int = HINCHADO_BYTES,
    hinchado_parches: int = HINCHADO_PARCHES,
) -> UsoSkill:
    """Clasifica un skill con las cuatro señales. Puro: sin I/O salvo el tamaño del archivo.

    El orden de evaluación es el de urgencia, no el de la lista de estados: un skill que
    nunca se usó es más accionable que uno que creció, y se reporta por lo primero.
    """
    r = registro if isinstance(registro, dict) else None
    archived = bool(r.get("archived_at")) if r else False
    use_count = _entero(r.get("use_count")) if r else None
    patch_count = _entero(r.get("patch_count")) if r else None
    patch_generation = _entero(r.get("patch_generation")) if r else None
    last_used = _parse_iso(r.get("last_used_at")) if r else None
    last_patched = _parse_iso(r.get("last_patched_at")) if r else None
    created = _parse_iso(r.get("created_at")) if r else None
    created_by = r.get("created_by") if r else None

    tamano: Optional[int] = None
    if ruta is not None:
        try:
            tamano = ruta.stat().st_size
        except OSError:
            tamano = None

    estado = "desconocido"
    motivo = "sin registro de uso: no se puede afirmar nada"

    if r is None:
        return UsoSkill(nombre, estado, use_count, patch_count, patch_generation,
                        last_used, last_patched, created, created_by, archived,
                        tamano, str(ruta) if ruta else None, motivo)

    # Hay entrada, pero si el conteo de usos NO se pudo leer, no se puede afirmar nada
    # sobre el uso. Rellenar con cero convertiría "no lo medí" en "no se usa" — que es
    # justo el error que este módulo existe para no cometer. El estado lo declara.
    if use_count is None:
        return UsoSkill(nombre, "desconocido", None, patch_count, patch_generation,
                        last_used, last_patched, created, created_by, archived,
                        tamano, str(ruta) if ruta else None,
                        "registro presente pero el conteo de usos es ilegible")

    parches = patch_count if patch_count is not None else 0
    usos = use_count

    if archived:
        estado, motivo = "sano", "archivado: fuera del catalogo activo"
    elif usos == 0:
        edad = _dias(created, ahora)
        if edad is not None and edad >= sin_usar_dias:
            estado = "sin_usar"
            motivo = f"creado hace {edad:.0f} d y nunca usado"
        else:
            estado, motivo = "sano", "sin uso todavia, pero es reciente"
    elif parches >= hinchado_parches or (
            tamano is not None and tamano >= hinchado_bytes):
        estado = "hinchado"
        detalle = []
        if tamano is not None and tamano >= hinchado_bytes:
            detalle.append(f"cuerpo de {tamano / 1024:.0f} KB")
        if parches >= hinchado_parches:
            detalle.append(f"{parches} parches")
        motivo = "desgaste acumulado: " + " y ".join(detalle)
    elif parches > 0:
        gen = patch_generation if patch_generation is not None else 0
        reusado = _entero(r.get("last_reused_patch_generation"))
        if reusado is not None and gen > 0 and reusado >= gen:
            estado, motivo = "sano", f"parcheado y vuelto a usar ({usos} usos)"
        else:
            espera = _dias(last_patched, ahora)
            if espera is not None and espera >= sin_reuso_dias:
                estado = "sin_reuso"
                motivo = f"parcheado hace {espera:.0f} d y no se volvio a usar"
            else:
                estado, motivo = "sano", "parcheado hace poco: todavia sin veredicto"
    elif usos >= sin_cambio_usos:
        estado = "sin_cambio"
        espera = _dias(last_patched or created, ahora)
        sufijo = f" en {espera:.0f} d" if espera is not None else ""
        motivo = f"{usos} usos{sufijo} y 0 parches: nunca aprendio nada"
    else:
        estado, motivo = "sano", f"{usos} usos, sin senal de desgaste"

    return UsoSkill(nombre, estado, use_count, patch_count, patch_generation,
                    last_used, last_patched, created, created_by, archived,
                    tamano, str(ruta) if ruta else None, motivo)


def medir(
    *,
    hermes_home: Path,
    skills_dirs: Optional[list[Path]] = None,
    ahora: Optional[float] = None,
    incluir_archivados: bool = False,
    **umbrales: Any,
) -> UsoReport:
    """Lee el ``.usage.json`` del hogar y clasifica todos los skills.

    Sólo lectura: no escribe nada, no llama al modelo, no toca ningún skill. ``hermes_home``
    se recibe por parámetro y nunca se deriva del entorno — un proceso sirve varios perfiles
    y el hogar correcto es el que el arnés entregó en la llamada.
    """
    import json

    marca = time.time() if ahora is None else ahora
    home = Path(hermes_home)
    dirs = [Path(d) for d in (skills_dirs or [home / "skills"])]

    path = _usage_file(home)
    if path is None:
        return UsoReport(skills=(), sin_registro=0, total_activos=0, scanned_until=marca)

    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return UsoReport(skills=(), sin_registro=0, total_activos=0, scanned_until=marca)

    if not isinstance(data, dict):
        return UsoReport(skills=(), sin_registro=0, total_activos=0, scanned_until=marca)

    out: list[UsoSkill] = []
    sin_registro = 0
    activos = 0
    for nombre, registro in sorted(data.items()):
        if not isinstance(nombre, str):
            continue
        ruta = _buscar_skill_md(nombre, dirs)
        item = clasificar(nombre=nombre, registro=registro, ruta=ruta,
                          ahora=marca, **umbrales)
        if not item.activo:
            if not incluir_archivados:
                continue
        else:
            activos += 1
            if not item.medido:
                sin_registro += 1
        out.append(item)

    return UsoReport(skills=tuple(out), sin_registro=sin_registro,
                     total_activos=activos, scanned_until=marca)


def resumen(report: UsoReport) -> str:
    """Una línea con el reparto, para el log del hook y el informe."""
    return (
        f"{len(report.sin_usar)} sin usar · {len(report.sin_cambio)} sin cambio · "
        f"{len(report.sin_reuso)} sin reuso · {len(report.hinchado)} hinchados · "
        f"{report.total_activos} activos"
    )


__all__ = [
    "SIN_USAR_DIAS",
    "SIN_CAMBIO_USOS",
    "SIN_REUSO_DIAS",
    "HINCHADO_BYTES",
    "HINCHADO_PARCHES",
    "ESTADOS",
    "UsoSkill",
    "UsoReport",
    "clasificar",
    "medir",
    "resumen",
]
