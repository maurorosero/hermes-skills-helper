"""Normalización de errores y *fingerprint* — determinista, sin modelo.

Un mismo fallo se escribe distinto en cada sesión: cambia el id de un registro, el
número de línea, la ruta absoluta, el timestamp. Comparar el texto crudo convertiría
un fallo recurrente en veinte fallos distintos, y el umbral de recurrencia (etapa 1)
nunca se cruzaría.

La *huella* (`fingerprint`) resuelve eso: reduce el mensaje a su forma invariante y lo
hashea, con el nombre de la herramienta como prefijo para que el mismo texto de error
producido por dos herramientas distintas no colapse en uno.

**Escrito desde cero.** Este módulo no copia código del proyecto de origen; implementa
la misma idea con criterio propio. El préstamo es del *diseño*, no de la
implementación — ver la sección de créditos en el README.

Advertencia de estabilidad
--------------------------
La huella es estable **solo contra esta normalización**. Cualquier regla que se agregue
abajo cambia la huella de todo mensaje que esa regla toque. Una huella ya registrada y
comparada en el futuro contra una recalculada con reglas nuevas **deja de coincidir**: la
recurrencia no se puede probar para ese registro viejo. Por eso las reglas no se tocan
sin volver a medir, y por eso la versión de la normalización viaja junto a la huella.
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Optional

#: Sube cuando cambia cualquier regla de normalización de abajo. Una huella guardada
#: con otra versión no es comparable con una recién calculada.
NORMALIZATION_VERSION = 1

# Orden importa: lo más específico primero, para que una regla no consuma el texto
# que otra necesita.
_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # Hermes agrega este diagnóstico a los fallos repetidos de una herramienta. Es
    # instrumentación del arnés, no parte de la forma del error.
    (
        re.compile(
            r"\s*\[Tool loop warning:\s*repeated_exact_failure_warning;[^\]]*\]\s*$",
            re.IGNORECASE,
        ),
        "",
    ),
    # Timestamps ISO y epoch: el momento del fallo no es parte de su forma.
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"), "<ts>"),
    (re.compile(r"\b1[0-9]{9}\b"), "<ts>"),
    # Identificadores: UUID, sha, hex largo, id numérico de fila/recurso.
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<id>"),
    (re.compile(r"\b[0-9a-f]{16,}\b", re.I), "<hash>"),
    (re.compile(r"(?<=[:#/])[0-9]{2,}\b"), "<n>"),
    # Rutas: absolutas y de perfil. El path exacto no distingue el fallo.
    (re.compile(r"(?:/[\w.@+-]+){2,}/?"), "<path>"),
    (re.compile(r"\b[A-Za-z]:\\[^\s]+"), "<path>"),
    # Puertos, versiones y direcciones: varían sin cambiar la causa.
    (re.compile(r"\b(?:localhost|127\.0\.0\.1|\d{1,3}(?:\.\d{1,3}){3})(?::\d+)?\b"), "<host>"),
    (re.compile(r"\bv?\d+\.\d+(?:\.\d+)*\b"), "<ver>"),
    # Números sueltos al final del mensaje (contadores, tamaños, duraciones).
    (re.compile(r"\b\d+\b"), "<n>"),
)


def normalize_error(content: str) -> str:
    """Reduce un mensaje de error a su forma invariante.

    ``HTTP 429 for /users/8821`` y ``HTTP 429 for /users/9134`` normalizan ambos a
    ``http <n> for <path>``: un patrón, no dos.

    Devuelve cadena vacía para entrada vacía — un mensaje vacío no es un patrón.
    """
    if not content:
        return ""
    text = content.strip()
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    # Colapsar espacios y unificar mayúsculas: el mismo error, distinta capitalización
    # o distinto espaciado, es el mismo error.
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def fingerprint(tool_name: str, content: str) -> str:
    """Identificador corto y estable de la forma de un error, con alcance por herramienta.

    Hashea el texto normalizado **completo**, no un prefijo: dos errores que comparten
    un encabezado largo pero difieren en la cola siguen siendo patrones distintos.

    La herramienta entra en la clave porque el mismo texto producido por dos
    herramientas distintas no es el mismo fallo.
    """
    key = f"{tool_name or ''}|{normalize_error(content)}"
    return hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()[:12]


def believable_ts(value: Any, *, now: Optional[float] = None) -> Optional[float]:
    """Un timestamp del host, o ``None`` cuando no se puede creer.

    ``messages.timestamp`` es entrada del host y se compara contra un reloj propio del
    plugin. Una fila con fecha futura queda dentro de cualquier ventana para siempre;
    una no positiva queda invisible para siempre. En ambos casos el conteo miente.

    ``None`` significa exactamente eso: **no hay tiempo**. Quien llama debe distinguir
    "no medido" de "cero medido" — leer un valor faltante como ``0`` reportaría silencio
    con confianza en lugar de ausencia de medición.

    Tolerancia de ``DRIFT_TOLERANCE_S``: entre dos máquinas reales el desfase NTP es de
    segundos, no de horas.
    """
    if value is None:
        return None
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    reference = time.time() if now is None else now
    if ts > reference + DRIFT_TOLERANCE_S:
        return None
    return ts


#: Desfase máximo tolerado entre el reloj del host y el del plugin, en segundos.
DRIFT_TOLERANCE_S = 300.0

#: Tope de texto que se normaliza. Un mensaje gigante no aporta forma nueva y encarece
#: el cálculo; el recorte es determinista para que la huella siga siendo reproducible.
MAX_NORMALIZED_CHARS = 4000


def bounded(content: str) -> str:
    """Recorta el texto de entrada a :data:`MAX_NORMALIZED_CHARS`, de forma determinista."""
    if not content:
        return ""
    if len(content) <= MAX_NORMALIZED_CHARS:
        return content
    return content[:MAX_NORMALIZED_CHARS]


def fingerprint_of(tool_name: str, content: str) -> str:
    """``fingerprint`` con el recorte aplicado — el punto de entrada recomendado."""
    return fingerprint(tool_name, bounded(content))


__all__ = [
    "NORMALIZATION_VERSION",
    "DRIFT_TOLERANCE_S",
    "MAX_NORMALIZED_CHARS",
    "normalize_error",
    "fingerprint",
    "fingerprint_of",
    "believable_ts",
    "bounded",
]
