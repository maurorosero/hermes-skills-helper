"""Etapa 5 — *¿sirvió?* Determinista, sin modelo.

La pregunta que este paquete responde es acotada a propósito: **el cambio sobrevivió,
se usó, y el error no reapareció**. No responde *"¿el error paró por esta causa?"* — eso
exigiría razonar sobre trayectorias completas, y queda declarado como fuera de alcance.

Por qué determinista
--------------------
Cambiar una comparación reproducible (hash, conteo, huella) por una opinión degrada la
medición. Un veredicto que no se puede volver a calcular sobre los mismos datos no es un
veredicto: es una impresión con formato de dato.

Componentes
-----------
``fingerprint``  normalización del error y huella estable, para comparar el mismo fallo
                 escrito de distinta manera entre sesiones.
``usage``        conteo de usos posteriores al cambio, con su **alcance** explícito —
                 exacto, aproximado, piso, o no disponible.
``checker``      los tres chequeos, el veredicto y su evidencia.

Regla que atraviesa los tres
----------------------------
**No medir no es medir cero.** Cada resultado distingue *"no se pudo medir"* de *"se midió
y dio cero"*, porque llevan a conclusiones opuestas: la primera es una espera, la segunda
puede ser un veredicto.
"""

from __future__ import annotations

from .checker import (
    MIN_WINDOW_DAYS,
    TOO_NEW,
    UNRELIABLE,
    UNUSED,
    VERDICTS,
    WORKING,
    EffectVerdict,
    RecurrenceCheck,
    SurvivedCheck,
    UseCheck,
    check_recurrence,
    check_survival,
    decide,
    evaluate,
    sha256_text,
)
from .fingerprint import (
    DRIFT_TOLERANCE_S,
    NORMALIZATION_VERSION,
    believable_ts,
    fingerprint,
    fingerprint_of,
    normalize_error,
)
from .usage import (
    SINCE_APPROX,
    SINCE_EXACT,
    SINCE_FLOOR,
    UNAVAILABLE,
    UseCount,
    count_uses,
)

__all__ = [
    # checker
    "evaluate",
    "decide",
    "check_survival",
    "check_recurrence",
    "sha256_text",
    "EffectVerdict",
    "SurvivedCheck",
    "UseCheck",
    "RecurrenceCheck",
    "WORKING",
    "UNUSED",
    "UNRELIABLE",
    "TOO_NEW",
    "VERDICTS",
    "MIN_WINDOW_DAYS",
    # fingerprint
    "fingerprint",
    "fingerprint_of",
    "normalize_error",
    "believable_ts",
    "NORMALIZATION_VERSION",
    "DRIFT_TOLERANCE_S",
    # usage
    "count_uses",
    "UseCount",
    "SINCE_EXACT",
    "SINCE_APPROX",
    "SINCE_FLOOR",
    "UNAVAILABLE",
]
