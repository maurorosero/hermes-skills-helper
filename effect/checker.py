"""Etapa 5 — ¿sirvió? Determinista, sin modelo.

Tres comparaciones reproducibles sobre un cambio previamente aplicado:

1. **¿sobrevivió?** ``sha256`` de lo que escribimos contra ``sha256`` de lo que hay.
2. **¿se usó?** usos posteriores al cambio (ver :mod:`.usage`).
3. **¿el error volvió?** huella del error registrado contra las huellas posteriores.

Por qué sin modelo: la pregunta es *"¿el cambio sobrevivió y se usó?"*, no *"¿qué te
parece el cambio?"*. La primera se responde con hashes y conteos; la segunda, con una
opinión. Cambiar la primera por la segunda degrada una medición reproducible a un juicio
y es, exactamente, el defecto que este plugin viene a corregir.

Por qué el chequeo 1 existe
---------------------------
**El propio agente edita los mismos skills.** Sin la comparación de hashes, el plugin se
acreditaría un cambio que en realidad hizo otro proceso o una sesión posterior. Es
honestidad de medición, no desconfianza.

Límite declarado
----------------
Estas tres comparaciones **no** responden *"¿el error paró por esta causa?"*. Responden
*"el cambio sobrevivió, se usó, y el error no reapareció"*. Atribuir causalidad exige
razonar sobre trayectorias completas, que es otra cosa y queda fuera de alcance.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .usage import count_uses  # noqa: F401  (reexportado para el evaluador)

logger = logging.getLogger(__name__)

# -- Veredictos ---------------------------------------------------------------------

WORKING = "working"
"""Sobrevivió, se usó, y el error no reapareció. La señal más fuerte disponible."""

UNUSED = "unused"
"""Sobrevivió y el error no volvió, pero **no se usó**. No se puede acreditar."""

UNRELIABLE = "unreliable"
"""El error volvió a ocurrir después del cambio, o el cambio no sobrevivió."""

TOO_NEW = "too_new"
"""Sin ventana suficiente para concluir. **No es lo mismo que "sin efecto".**"""

VERDICTS = (WORKING, UNUSED, UNRELIABLE, TOO_NEW)

#: Ventana mínima, en días, antes de atreverse a emitir un veredicto distinto de
#: ``too_new``. Por debajo de esto, "no se usó" y "no volvió el error" no distinguen
#: *falta de efecto* de *falta de tiempo*.
MIN_WINDOW_DAYS = 7.0


def sha256_text(content: str) -> str:
    """Hash del contenido, con normalización de fin de línea.

    Se normaliza ``\\r\\n`` → ``\\n`` porque un archivo reescrito por una herramienta que
    cambia los finales de línea es el mismo contenido: reportarlo como "no sobrevivió"
    sería un falso negativo del chequeo.
    """
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8", "replace")).hexdigest()


@dataclass(frozen=True)
class SurvivedCheck:
    """El cambio, ¿sigue tal como lo dejamos?"""

    survived: bool
    """``True`` si el contenido coincide con el hash registrado."""

    measurable: bool
    """``False`` cuando no se pudo leer el archivo. Distinto de "no sobrevivió"."""

    current_hash: Optional[str] = None

    @property
    def note(self) -> str:
        if not self.measurable:
            return "no se pudo leer el skill para compararlo"
        return "el contenido coincide con el registrado" if self.survived else (
            "el contenido cambió desde que se aplicó — el cambio no es atribuible a este plugin"
        )


@dataclass(frozen=True)
class UseCheck:
    """¿Se usó después del cambio?"""

    count: Optional[int]
    scope: str

    @property
    def measurable(self) -> bool:
        return self.count is not None

    @property
    def used(self) -> bool:
        return self.count is not None and self.count > 0


@dataclass(frozen=True)
class RecurrenceCheck:
    """¿Volvió a ocurrir el error que el cambio debía evitar?"""

    recurred: Optional[bool]
    """``None`` cuando no hubo ventana posterior suficiente para mirar."""

    occurrences: int = 0
    window_days: float = 0.0


@dataclass(frozen=True)
class EffectVerdict:
    """Resultado completo de la etapa 5, con su evidencia y su límite."""

    verdict: str
    survived: SurvivedCheck
    usage: UseCheck
    recurrence: RecurrenceCheck
    age_days: float
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_conclusive(self) -> bool:
        """¿El veredicto permite actuar, o es una espera?"""
        return self.verdict != TOO_NEW

    @property
    def causal_claim(self) -> str:
        """Lo que este veredicto **no** dice. Se expone para que nadie lo sobre-lea."""
        return (
            "no se afirma causalidad: se afirma que el cambio sobrevivió, se usó "
            "y el error no reapareció — no que el error paró por esta causa"
        )


def check_survival(skill_path: Optional[Path], expected_hash: str) -> SurvivedCheck:
    """Compara el contenido actual contra el hash registrado al aplicar el cambio.

    Un archivo ausente o ilegible devuelve ``measurable = False``, nunca
    ``survived = False``: no poder medir no es evidencia de que el cambio se perdió.
    """
    if skill_path is None or not Path(skill_path).is_file():
        return SurvivedCheck(survived=False, measurable=False)
    try:
        content = Path(skill_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.debug("No se pudo leer %s: %s", skill_path, exc)
        return SurvivedCheck(survived=False, measurable=False)
    current = sha256_text(content)
    return SurvivedCheck(
        survived=(current == expected_hash),
        measurable=True,
        current_hash=current,
    )


def check_recurrence(
    fingerprints_after: Iterable[str],
    *,
    target_fingerprint: str,
    since_ts: float,
    timestamps: Optional[Iterable[float]] = None,
    now: Optional[float] = None,
) -> RecurrenceCheck:
    """¿Reapareció la huella objetivo después del cambio?

    ``timestamps`` permite acompañar cada huella con su momento. Sin timestamps se
    asume que todas las huellas pertenecen a la ventana posterior —lo que quien llama
    debe haber garantizado al filtrarlas—.
    """
    reference = time.time() if now is None else now
    window_days = max(0.0, (reference - since_ts) / 86400.0)
    if window_days < MIN_WINDOW_DAYS:
        return RecurrenceCheck(recurred=None, occurrences=0, window_days=window_days)

    count = 0
    for index, fp in enumerate(fingerprints_after):
        if fp != target_fingerprint:
            continue
        if timestamps is not None:
            try:
                ts = float(list(timestamps)[index])
            except (IndexError, TypeError, ValueError):
                ts = None
            if ts is not None and ts <= since_ts:
                continue
        count += 1
    return RecurrenceCheck(recurred=count > 0, occurrences=count, window_days=window_days)


def decide(
    *,
    survived: SurvivedCheck,
    usage: UseCheck,
    recurrence: RecurrenceCheck,
    age_days: float,
) -> tuple[str, tuple[str, ...]]:
    """Combina los tres chequeos en un veredicto, con las razones que lo sustentan.

    El orden de evaluación no es arbitrario. Primero lo que **invalida** (el cambio se
    perdió, o el error volvió): un cambio que no sobrevivió no se puede acreditar aunque
    se haya usado, y un cambio tras el cual el error reaparece no funcionó aunque siga
    ahí. Después lo que **califica con reservas**.
    """
    reasons: list[str] = []

    # -- Invalidantes ---------------------------------------------------------------
    if survived.measurable and not survived.survived:
        return UNRELIABLE, ("el contenido cambió desde que se aplicó; el cambio no sobrevivió",)

    if recurrence.recurred:
        reasons.append(
            f"el error reapareció {recurrence.occurrences} vez/veces "
            f"en {recurrence.window_days:.0f} días posteriores"
        )
        return UNRELIABLE, tuple(reasons)

    # -- Sin ventana suficiente ------------------------------------------------------
    if age_days < MIN_WINDOW_DAYS:
        reasons.append(
            f"pasaron {age_days:.1f} días, por debajo de la ventana mínima de "
            f"{MIN_WINDOW_DAYS:.0f}; el silencio no distingue falta de efecto de falta de tiempo"
        )
        return TOO_NEW, tuple(reasons)

    if recurrence.recurred is None:
        reasons.append("no hay ventana posterior suficiente para saber si el error volvió")
        return TOO_NEW, tuple(reasons)

    # -- Calificación ----------------------------------------------------------------
    if not usage.measurable:
        reasons.append(
            "no se pudo medir el uso; sin esa señal no se puede acreditar ni descartar"
        )
        return TOO_NEW, tuple(reasons)

    if usage.used:
        reasons.append(f"se usó {usage.count} vez/veces desde el cambio ({usage.scope})")
        if survived.survived:
            reasons.append("el contenido sobrevivió tal como se aplicó")
        reasons.append("el error no reapareció en la ventana observada")
        return WORKING, tuple(reasons)

    reasons.append("sobrevivió y el error no volvió, pero no se usó desde el cambio")
    reasons.append("sin uso no hay evidencia de efecto: no se acredita")
    return UNUSED, tuple(reasons)


def evaluate(
    *,
    skill_name: str,
    skill_path: Optional[Path],
    expected_hash: str,
    applied_ts: float,
    target_fingerprint: str,
    fingerprints_after: Iterable[str] = (),
    hermes_home: Optional[Path] = None,
    state_db: Optional[Path] = None,
    now: Optional[float] = None,
) -> EffectVerdict:
    """Corre los tres chequeos y devuelve el veredicto con su evidencia.

    Punto de entrada de la etapa 5. No escribe nada: mide y reporta.
    """
    reference = time.time() if now is None else now
    age_days = max(0.0, (reference - applied_ts) / 86400.0)

    survived = check_survival(skill_path, expected_hash)
    counted = count_uses(
        skill_name, since_ts=applied_ts, hermes_home=hermes_home, state_db=state_db
    )
    usage = UseCheck(count=counted.count, scope=counted.scope)
    recurrence = check_recurrence(
        fingerprints_after,
        target_fingerprint=target_fingerprint,
        since_ts=applied_ts,
        now=reference,
    )

    verdict, reasons = decide(
        survived=survived, usage=usage, recurrence=recurrence, age_days=age_days
    )
    return EffectVerdict(
        verdict=verdict,
        survived=survived,
        usage=usage,
        recurrence=recurrence,
        age_days=age_days,
        reasons=reasons,
    )


__all__ = [
    "WORKING",
    "UNUSED",
    "UNRELIABLE",
    "TOO_NEW",
    "VERDICTS",
    "MIN_WINDOW_DAYS",
    "SurvivedCheck",
    "UseCheck",
    "RecurrenceCheck",
    "EffectVerdict",
    "sha256_text",
    "check_survival",
    "check_recurrence",
    "decide",
    "evaluate",
]
