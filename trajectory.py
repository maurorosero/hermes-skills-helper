"""Lectura de la trayectoria — de dónde sale la evidencia. Determinista, solo lectura.

El arnés guarda cada turno en ``state.db``. Este módulo extrae de ahí **los fallos**: los
resultados de herramienta que traen señal de error, con su herramienta, su momento y su
huella.

Por qué es un módulo aparte
---------------------------
Dos etapas necesitan lo mismo: la recurrencia (etapa 1) busca fallos repetidos en el
tiempo, y la verificación (etapa 5) busca si un error volvió después de un cambio. Escribir
el lector dos veces sería garantizar que una de las dos versiones quede desactualizada.

Reglas de lectura
-----------------
* **Solo lectura.** La base se abre ``mode=ro`` y con ``query_only``. Medir nunca debe
  poder alterar lo medido.
* **Timestamps creíbles o nada.** ``messages.timestamp`` es entrada del host: una fila con
  fecha futura entraría en toda ventana para siempre. Las filas con fecha increíble se
  descartan y **se cuentan**, para poder reportar cuánto no se pudo usar.
* **Solo filas activas.** Una fila compactada o inactiva ya no representa lo que pasó.

Qué cuenta como fallo
---------------------
No toda salida que contiene la palabra "error" es un fallo: un ``grep`` que devuelve la
palabra, o un informe que la menciona, no lo son. Por eso el criterio es **estructural**
sobre el JSON que el arnés escribe:

```
success: false          → fallo declarado
error: "<no vacío>"     → fallo declarado
exit_code != 0          → fallo declarado
```

El escaneo de texto queda descartado a propósito: produce falsos positivos que inflarían
la recurrencia y harían proponer cambios sobre fallos que nunca ocurrieron.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

try:  # cargado como paquete por el arnés
    from .effect.fingerprint import bounded, believable_ts, fingerprint_of
except ImportError:  # cargado como módulo suelto (tests, ejecución directa)
    from effect.fingerprint import bounded, believable_ts, fingerprint_of  # type: ignore

logger = logging.getLogger(__name__)

#: Cuánto texto de un fallo se conserva como muestra. Alcanza para reconocer el patrón
#: sin arrastrar un volcado entero a la memoria del proceso.
MAX_SAMPLE_CHARS = 400

#: Tope de filas que se leen en una pasada. Una trayectoria larga no debe convertir una
#: medición en una operación sin fin; quien necesite más, pagina con ``since_ts``.
DEFAULT_ROW_LIMIT = 20_000


@dataclass(frozen=True)
class Failure:
    """Un fallo observado en la trayectoria."""

    tool_name: str
    fingerprint: str
    ts: float
    session_id: str
    sample: str


@dataclass(frozen=True)
class TrajectoryScan:
    """Resultado de una lectura, con lo que se pudo y no se pudo usar.

    ``discarded_ts`` no es un detalle: si muchas filas tienen fecha increíble, la
    conclusión "este fallo no se repite" es en realidad "no se pudo mirar". Reportarlo es
    la diferencia entre medir y aparentar medir.
    """

    failures: tuple[Failure, ...]
    rows_scanned: int
    discarded_ts: int
    scanned_until: float

    @property
    def usable(self) -> bool:
        """¿Hubo al menos una fila que se pudo fechar?"""
        return self.rows_scanned > self.discarded_ts


def _is_failure(tool_name: str, content: str) -> Optional[str]:
    """Mensaje de error si el resultado de herramienta declara un fallo; si no, ``None``.

    Criterio estructural, no textual (ver el docstring del módulo). Un resultado que no
    sea JSON se ignora: sin estructura no hay forma de distinguir un fallo de un informe
    que habla de fallos.
    """
    if not content or not content.lstrip().startswith("{"):
        return None
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    if data.get("success") is False:
        return _first_text(data, ("error", "message", "output", "result")) or "success=false"

    error = data.get("error")
    if isinstance(error, str) and error.strip():
        return error.strip()
    if isinstance(error, dict) and error:
        return json.dumps(error, ensure_ascii=False)[:MAX_SAMPLE_CHARS]

    code = data.get("exit_code")
    if isinstance(code, int) and code != 0:
        return _first_text(data, ("output", "stderr", "message")) or f"exit_code={code}"

    return None


def _first_text(data: dict, keys: tuple[str, ...]) -> str:
    """Primer valor textual no vacío entre ``keys``."""
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list) and value:
            joined = " ".join(str(item) for item in value if item)
            if joined.strip():
                return joined.strip()
    return ""


def scan_failures(
    *,
    state_db: Optional[Path],
    since_ts: float = 0.0,
    until_ts: Optional[float] = None,
    row_limit: int = DEFAULT_ROW_LIMIT,
    now: Optional[float] = None,
) -> TrajectoryScan:
    """Lee los fallos de la trayectoria entre ``since_ts`` y ``until_ts``.

    El filtro por fecha se aplica **en Python**, no en el ``WHERE``: el ``timestamp`` es
    del host y una fila con fecha futura satisfaría cualquier comparación ``>`` para
    siempre. Filtrar después de validar es el único orden que no se puede burlar.
    """
    reference = time.time() if now is None else now
    upper = reference if until_ts is None else until_ts

    if state_db is None or not Path(state_db).is_file():
        logger.debug("Sin trayectoria legible en %s", state_db)
        return TrajectoryScan((), 0, 0, reference)

    try:
        connection = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        logger.debug("No se pudo abrir la trayectoria: %s", exc)
        return TrajectoryScan((), 0, 0, reference)

    rows: list[tuple] = []
    try:
        connection.execute("PRAGMA query_only = 1")
        cursor = connection.execute(
            "SELECT tool_name, content, timestamp, session_id FROM messages "
            "WHERE active = 1 AND role = 'tool' "
            "ORDER BY id DESC LIMIT ?",
            (int(row_limit),),
        )
        rows = cursor.fetchall()
    except sqlite3.Error as exc:
        logger.debug("Consulta de trayectoria fallida: %s", exc)
        return TrajectoryScan((), 0, 0, reference)
    finally:
        connection.close()

    failures: list[Failure] = []
    scanned = 0
    discarded = 0

    for tool_name, content, raw_ts, session_id in rows:
        scanned += 1
        ts = believable_ts(raw_ts, now=reference)
        if ts is None:
            discarded += 1
            continue
        if ts < since_ts or ts > upper:
            continue
        message = _is_failure(str(tool_name or ""), str(content or ""))
        if message is None:
            continue
        failures.append(
            Failure(
                tool_name=str(tool_name or ""),
                fingerprint=fingerprint_of(str(tool_name or ""), message),
                ts=ts,
                session_id=str(session_id or ""),
                sample=bounded(message)[:MAX_SAMPLE_CHARS],
            )
        )

    failures.sort(key=lambda f: f.ts)
    return TrajectoryScan(tuple(failures), scanned, discarded, upper)


def iter_fingerprints(scan: TrajectoryScan) -> Iterator[tuple[str, float]]:
    """``(huella, momento)`` por fallo, para alimentar la verificación de la etapa 5."""
    for failure in scan.failures:
        yield failure.fingerprint, failure.ts


__all__ = [
    "Failure",
    "TrajectoryScan",
    "scan_failures",
    "iter_fingerprints",
    "MAX_SAMPLE_CHARS",
    "DEFAULT_ROW_LIMIT",
]
