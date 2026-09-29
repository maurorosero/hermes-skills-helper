"""¿Se usó el skill desde que cambió? — determinista, sin modelo.

Este es el chequeo 2 de la etapa 5 y el más delicado de los tres, por una razón que
conviene declarar de frente:

**El arnés no expone un "cuántas veces se usó desde el momento X".**

Lo que expone es un registro acumulado por skill (``.usage.json``): un ``use_count``
total y un ``last_used_at``. De ahí se sigue una asimetría que este módulo respeta en
lugar de disimular:

```
last_used_at <= since_ts   →  0 usos desde el cambio.  EXACTO.
                               (si el último uso fue antes, no hubo ninguno después)

last_used_at >  since_ts   →  Hubo al menos 1. El total exacto NO es derivable
                               del registro.
```

Para el segundo caso se recurre a la trayectoria (``state.db``, tabla ``messages``) como
fuente aproximada, y **el resultado se etiqueta con su alcance**. Un conteo aproximado
presentado como exacto es peor que no tener conteo: la etapa 5 decidiría con un número
que no significa lo que aparenta.

Escalas de alcance
------------------
===============  =========================================================
``since_exact``  Deducido del registro del arnés. Es el único concluyente.
``since_approx`` Contado sobre la trayectoria. Sirve como indicio, no como prueba.
``since_floor``  Solo se sabe que hubo ≥ 1. Un piso, no un total.
``unavailable``  No se pudo medir. **Distinto de cero.**
===============  =========================================================

``unavailable`` nunca debe leerse como ``0``: "no medí" y "medí cero" son conclusiones
opuestas y la etapa 5 las trata distinto.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from .fingerprint import believable_ts

logger = logging.getLogger(__name__)

SINCE_EXACT = "since_exact"
SINCE_APPROX = "since_approx"
SINCE_FLOOR = "since_floor"
UNAVAILABLE = "unavailable"

#: Alcances que permiten concluir algo. ``unavailable`` no está acá a propósito.
CONCLUSIVE_SCOPES = frozenset({SINCE_EXACT})


@dataclass(frozen=True)
class UseCount:
    """Resultado de contar usos. ``count`` es ``None`` si y solo si no se pudo medir."""

    count: Optional[int]
    scope: str

    @property
    def measured(self) -> bool:
        """¿Hubo medición? ``False`` no significa cero."""
        return self.count is not None

    @property
    def conclusive(self) -> bool:
        """¿El número es exacto, y por lo tanto apto para calificar la etapa 5?"""
        return self.count is not None and self.scope in CONCLUSIVE_SCOPES

    def __str__(self) -> str:  # pragma: no cover - representación
        return f"{self.count if self.count is not None else 'sin medir'} ({self.scope})"


def _usage_file(hermes_home: Optional[Path]) -> Optional[Path]:
    """Ruta del registro de uso.

    ``hermes_home`` se recibe por parámetro y no se lee del entorno: un proceso sirve
    varios perfiles a la vez, y el hogar correcto es el que el arnés entrega en la
    llamada, no el del lanzamiento.
    """
    if hermes_home is None:
        return None
    candidate = Path(hermes_home) / "skills" / ".usage.json"
    return candidate if candidate.is_file() else None


def read_record(skill_name: str, *, hermes_home: Optional[Path]) -> Optional[dict]:
    """Registro de un skill, o ``None`` si **no existe entrada** para él.

    ``None`` no es lo mismo que un registro con ``use_count = 0``: lo primero significa
    que la herramienta no está siendo contabilizada (y por lo tanto no se puede afirmar
    nada), lo segundo es una medición real de cero usos. Esa distinción decide el
    veredicto de la etapa 5, así que este lector nunca devuelve un valor por defecto.

    Dos caminos, y el orden importa:

    * ``hermes_home`` explícito → se lee **ese** hogar. Un llamador que pasa un hogar
      quiere medir ese hogar; ir a la API viva del host ignoraría el parámetro y mediría
      la instalación en curso, que es un error silencioso de medición.
    * ``hermes_home`` ausente → se usa el registro vivo del host.
    """
    if hermes_home is not None:
        return _read_record_from_file(skill_name, Path(hermes_home))

    try:
        from tools import skill_usage as usage  # type: ignore

        loader = getattr(usage, "load_usage", None)
        if callable(loader):
            data = loader()
            if isinstance(data, dict):
                entry = data.get(skill_name)
                return entry if isinstance(entry, dict) else None
    except Exception as exc:  # host ausente, o API distinta en otra versión
        logger.debug("API de uso no disponible para %s: %s", skill_name, exc)

    return None


def _read_record_from_file(skill_name: str, hermes_home: Path) -> Optional[dict]:
    """Lee una entrada concreta del ``.usage.json`` de un hogar dado.

    Devuelve ``None`` tanto si el archivo no está como si el skill no tiene entrada:
    en ambos casos la respuesta honesta es "no hay registro", no "cero usos".
    """
    path = _usage_file(hermes_home)
    if path is None:
        return None
    try:
        import json

        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        entry = data.get(skill_name) if isinstance(data, dict) else None
        return entry if isinstance(entry, dict) else None
    except Exception as exc:
        logger.debug("No se pudo leer el registro de %s: %s", skill_name, exc)
        return None


def count_uses_from_record(
    skill_name: str, *, since_ts: float, hermes_home: Optional[Path]
) -> UseCount:
    """Deduce los usos desde ``since_ts`` a partir del registro acumulado.

    Es el camino preferido porque es el único que produce un número **exacto**: si el
    último uso registrado es anterior al cambio, no hubo ninguno después.
    """
    record = read_record(skill_name, hermes_home=hermes_home)
    if record is None:
        return UseCount(None, UNAVAILABLE)

    last_used = record.get("last_used_at")
    if not last_used:
        # Sin marca de último uso no hay nada que deducir. Un skill nunca usado tiene
        # use_count 0, pero eso es el total histórico, no el conteo desde el cambio.
        total = record.get("use_count")
        if isinstance(total, int) and total <= 0:
            return UseCount(0, SINCE_EXACT)
        return UseCount(None, UNAVAILABLE)

    ts = _parse_iso(last_used)
    if ts is None:
        return UseCount(None, UNAVAILABLE)

    if ts <= since_ts:
        return UseCount(0, SINCE_EXACT)
    # Se usó después del cambio, pero el registro no dice cuántas veces.
    return UseCount(None, SINCE_FLOOR)


def count_uses_from_trajectory(
    skill_name: str, *, since_ts: float, state_db: Optional[Path]
) -> UseCount:
    """Cuenta aproximada sobre la trayectoria (``state.db``), abierta de solo lectura.

    Se filtran las filas en Python en lugar de confiar en el ``WHERE timestamp > ?``:
    ``messages.timestamp`` es entrada del host, y una fila con fecha futura satisfaría
    esa condición para siempre. El filtro en Python es el arreglo honesto —el camino es
    una medición, no un bucle caliente—.
    """
    if state_db is None or not Path(state_db).is_file():
        return UseCount(None, UNAVAILABLE)

    escaped = (
        str(skill_name).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    try:
        connection = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        logger.debug("No se pudo abrir la trayectoria para %s: %s", skill_name, exc)
        return UseCount(None, UNAVAILABLE)

    try:
        connection.execute("PRAGMA query_only = 1")
        rows = connection.execute(
            "SELECT timestamp FROM messages WHERE active = 1 "
            "AND (tool_name = ? OR content LIKE ? ESCAPE '\\')",
            (skill_name, f"%/{escaped}%"),
        ).fetchall()
    except sqlite3.Error as exc:
        logger.debug("Consulta de trayectoria fallida para %s: %s", skill_name, exc)
        return UseCount(None, UNAVAILABLE)
    finally:
        connection.close()

    now = time.time()
    count = 0
    for (raw_ts,) in rows:
        trusted = believable_ts(raw_ts, now=now)
        if trusted is not None and trusted > since_ts:
            count += 1
    return UseCount(count, SINCE_APPROX)


def count_uses(
    skill_name: str,
    *,
    since_ts: float,
    hermes_home: Optional[Path] = None,
    state_db: Optional[Path] = None,
) -> UseCount:
    """Mejor conteo disponible de usos posteriores a ``since_ts``.

    Orden: registro del arnés (exacto) → trayectoria (aproximado) → piso conocido.

    Nunca devuelve ``count = 0`` salvo que el registro lo pruebe. La diferencia entre
    "no lo medí" y "medí cero" decide el veredicto de la etapa 5, así que un fracaso de
    medición se declara como tal en lugar de rellenarse con un cero tranquilizador.
    """
    exact = count_uses_from_record(skill_name, since_ts=since_ts, hermes_home=hermes_home)
    if exact.count is not None:
        return exact

    approx = count_uses_from_trajectory(skill_name, since_ts=since_ts, state_db=state_db)
    if approx.measured:
        return approx

    if exact.scope == SINCE_FLOOR:
        # El registro prueba que hubo al menos un uso; solo falta el total.
        return UseCount(1, SINCE_FLOOR)

    return UseCount(None, UNAVAILABLE)


def _parse_iso(value: str) -> Optional[float]:
    """Convierte un timestamp ISO del registro a epoch, o ``None`` si no se puede.

    Acepta el sufijo ``Z`` (UTC) y la ausencia de zona. Una marca sin zona se interpreta
    como UTC —que es lo que escribe el arnés— y nunca se asume hora local, que correría
    el conteo varias horas en una dirección arbitraria.
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        from datetime import datetime

        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        from datetime import timezone

        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


__all__ = [
    "UseCount",
    "count_uses",
    "count_uses_from_record",
    "count_uses_from_trajectory",
    "read_record",
    "SINCE_EXACT",
    "SINCE_APPROX",
    "SINCE_FLOOR",
    "UNAVAILABLE",
    "CONCLUSIVE_SCOPES",
]
