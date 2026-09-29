"""Etapa 1 — Recurrencia. Determinista, sin modelo.

**Pregunta:** ¿este fallo se repite, o pasó una sola vez?

Un error que ocurrió una vez no justifica un cambio permanente en las instrucciones del
agente. Solo se propone un cambio sobre fallos **recurrentes**. Este es el filtro más
barato del pipeline y corre **antes** de gastar una sola llamada al modelo.

Dos criterios, y alcanza con uno
--------------------------------
Un fallo es recurrente si:

* apareció **≥ N veces** en total, o
* apareció en **≥ M sesiones distintas**.

El segundo criterio existe porque el primero se puede satisfacer dentro de una sola
sesión: un bucle de reintentos produce diez apariciones del mismo error en dos minutos y
no por eso es un patrón que merezca un cambio de conducta. Que el fallo cruce sesiones es
evidencia más fuerte que su cuenta bruta.

Por qué determinista
--------------------
La pregunta es *"¿cuántas veces aparece esta huella en la ventana?"*. Se responde con un
conteo. Poner un modelo acá no mejoraría la respuesta — la haría no reproducible, y el
mismo historial daría veredictos distintos en dos corridas. La medición tiene que poder
recalcularse.

Lo que este módulo NO decide
----------------------------
No decide **qué** cambio corresponde — eso es la etapa 3, la única con modelo. Acá solo se
decide **sobre qué** vale la pena pensar.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:  # cargado como paquete por el arnés
    from .trajectory import Failure, TrajectoryScan, scan_failures
except ImportError:  # cargado como módulo suelto (tests, ejecución directa)
    from trajectory import Failure, TrajectoryScan, scan_failures  # type: ignore

# -- Umbrales ------------------------------------------------------------------------

#: Apariciones mínimas para considerar un fallo recurrente.
MIN_OCCURRENCES = 3

#: Sesiones distintas mínimas. Un fallo en una sola sesión puede ser un bucle de
#: reintentos; en varias sesiones es un patrón.
MIN_SESSIONS = 2

#: Ventana por defecto, en días. Un fallo de hace ocho meses no dice nada sobre la
#: conducta de hoy, y proponer sobre él sería arreglar algo que ya no ocurre.
DEFAULT_WINDOW_DAYS = 30.0

#: Días mínimos que debe abarcar un grupo para contar como patrón sostenido.
#:
#: Sin esto, **una ráfaga se confunde con un patrón**: medido sobre la trayectoria real,
#: 37 apariciones del mismo rechazo repartidas en 33 sesiones distintas caben en **2,9
#: horas** — una tarde de trabajo donde el arnés bloqueó muchas veces la misma acción. Por
#: el criterio de sesiones cruzaba el umbral con holgura (33 ≥ 2), y sin embargo no es
#: recurrencia *entre* sesiones en el sentido que importa: es un episodio.
#:
#: Un patrón sostenido se repite en el tiempo, no solo en cantidad.
MIN_SPAN_DAYS = 1.0

#: Días máximos de silencio para que un candidato siga siendo accionable.
#:
#: Un patrón que **dejó de ocurrir** no se arregla con un cambio de hoy. Medido sobre la
#: trayectoria real: **5 de 8 candidatos** llevaban más de 7 días sin aparecer — uno de
#: ellos, 14,5 días, porque el archivo que faltaba (`wiki/SCHEMA.md`) se creó *después* de
#: su última aparición. Proponer un cambio ahí es arreglar algo que ya no puede pasar: gasta
#: presupuesto, toca un skill, y el "éxito" posterior no prueba nada porque el fallo ya
#: estaba muerto antes del cambio.
#:
#: Se mide contra el silencio, no contra el span: un fallo puede llevar meses vivo y
#: aparecer cada tres semanas. Lo que decide es **cuándo fue la última vez**.
MAX_SILENCE_DAYS = 7.0

#: Marcadores de que el resultado es un **rechazo del arnés**, no un fallo del agente.
#:
#: Es la corrección más importante del filtro, y salió de medir: **42 % de los fallos de
#: la ventana (97 de 233) son el arnés negándose a ejecutar algo**. Un guardarraíl
#: funcionando no es un error del agente; proponer un cambio de conducta por él sería
#: tratar el mecanismo de seguridad como si fuera el problema.
#:
#: El criterio es determinista y acotado: estos marcadores son literales que el host
#: escribe. No se infiere intención.
GUARDRAIL_MARKERS = (
    "blocked:",
    "access denied",
    "refusing to",
    "requires interactive authentication",
    "is a hermes credential store",
    "cannot be read directly",
    "flagged as dangerous",
)


@dataclass(frozen=True)
class RecurringFailure:
    """Un fallo que superó el filtro, con la evidencia que lo sustenta."""

    fingerprint: str
    tool_name: str
    occurrences: int
    sessions: int
    first_ts: float
    last_ts: float
    sample: str

    @property
    def span_days(self) -> float:
        """Días entre la primera y la última aparición."""
        return max(0.0, (self.last_ts - self.first_ts) / 86400.0)

    def why(self) -> str:
        """Por qué pasó el filtro, en una línea — para la propuesta y el journal."""
        if self.sessions >= MIN_SESSIONS and self.occurrences >= MIN_OCCURRENCES:
            return (
                f"{self.occurrences} apariciones en {self.sessions} sesiones "
                f"a lo largo de {self.span_days:.1f} días"
            )
        if self.sessions >= MIN_SESSIONS:
            return (
                f"apareció en {self.sessions} sesiones distintas "
                f"a lo largo de {self.span_days:.1f} días"
            )
        return (
            f"{self.occurrences} apariciones dentro de una sesión "
            f"a lo largo de {self.span_days:.1f} días"
        )


@dataclass(frozen=True)
class Burst:
    """Apariciones concentradas en el tiempo: un episodio, no un patrón.

    Se reportan aparte en lugar de descartarse en silencio. Una ráfaga puede indicar algo
    real —una tarde donde una tarea concreta falló una y otra vez—, pero **no es la
    evidencia que justifica un cambio permanente de conducta**. Mezclarlas haría que el
    volumen de una sesión difícil se leyera como un patrón instalado.
    """

    fingerprint: str
    tool_name: str
    occurrences: int
    sessions: int
    span_days: float
    sample: str


@dataclass(frozen=True)
class GuardrailEvent:
    """Apariciones de un **rechazo del arnés**, no de un fallo del agente.

    El guardarraíl funcionando es el sistema protegiendo algo, no un error que el agente
    deba corregir. Se reportan para que el descarte sea auditable: quien lea el informe
    puede ver qué se dejó fuera y por qué.
    """

    fingerprint: str
    tool_name: str
    occurrences: int
    sample: str


@dataclass(frozen=True)
class StaleFailure:
    """Un patrón que ya dejó de ocurrir.

    Supera los umbrales de recurrencia, pero su última aparición quedó lejos: el fallo no
    está vivo. Se reporta aparte en lugar de proponerse, para que el descarte sea auditable
    — y para que "0 candidatos" sea distinguible de "0 candidatos *porque revisé y todos
    estaban muertos*".
    """

    fingerprint: str
    tool_name: str
    occurrences: int
    sessions: int
    span_days: float
    silence_days: float
    sample: str


@dataclass(frozen=True)
class RecurrenceReport:
    """Resultado del filtro, con todo lo descartado y por qué.

    Tres cubos, y la separación es el aporte del módulo:

    ``recurring``   fallos del agente que se repiten **en el tiempo**. Candidatos.
    ``bursts``      fallos concentrados en horas. Episodios, no patrones.
    ``guardrails``  rechazos del propio arnés. No son errores del agente.
    ``stale``       patrones que ya dejaron de ocurrir. Accionar sobre ellos gasta
                    presupuesto en un fallo que no puede volver.

    ``discarded_ts`` importa tanto como los recurrentes: si muchas filas no se pudieron
    fechar, la conclusión "no hay fallos recurrentes" es en realidad "no se pudo mirar".
    """

    recurring: tuple[RecurringFailure, ...]
    bursts: tuple[Burst, ...]
    guardrails: tuple[GuardrailEvent, ...]
    stale: tuple[StaleFailure, ...]
    single_events: int
    total_failures: int
    sessions_seen: int
    discarded_ts: int
    window_days: float
    scanned_until: float

    @property
    def has_recurrence(self) -> bool:
        return bool(self.recurring)

    @property
    def trustworthy(self) -> bool:
        """¿La ventana se pudo leer lo suficiente como para creer en un "no hay nada"?

        Un barrido donde se descartó más de la mitad de las filas por fecha increíble no
        sostiene la conclusión negativa. ``False`` obliga a quien llama a declarar la
        limitación en lugar de reportar silencio como respuesta.
        """
        if self.total_failures == 0:
            return True
        return self.discarded_ts <= self.total_failures

    def summary(self) -> str:
        """Una línea con el reparto, para el informe y el journal."""
        return (
            f"{len(self.recurring)} recurrentes · {len(self.bursts)} ráfagas · "
            f"{len(self.stale)} ya no ocurren · "
            f"{len(self.guardrails)} rechazos del arnés · {self.single_events} eventos únicos"
        )


def is_guardrail(sample: str) -> bool:
    """¿El texto es un rechazo del arnés?

    Criterio por marcadores literales del host (ver :data:`GUARDRAIL_MARKERS`), no por
    inferencia: el mismo texto produce siempre el mismo resultado.
    """
    if not sample:
        return False
    lowered = sample.lower()
    return any(marker in lowered for marker in GUARDRAIL_MARKERS)


def is_recurring(
    *,
    occurrences: int,
    sessions: int,
    min_occurrences: int = MIN_OCCURRENCES,
    min_sessions: int = MIN_SESSIONS,
) -> bool:
    """¿Supera el umbral? Alcanza con un criterio, y hay que superar el umbral estricto.

    ``min_occurrences = 3`` significa tres, no dos: se compara con ``>=`` sobre el
    parámetro, pero el parámetro por defecto es el número que se quiere exigir.
    """
    return occurrences >= min_occurrences or sessions >= min_sessions


def group_failures(
    failures: tuple[Failure, ...],
) -> dict[str, list[Failure]]:
    """Agrupa por huella, preservando el orden temporal dentro de cada grupo."""
    grouped: dict[str, list[Failure]] = {}
    for failure in failures:
        grouped.setdefault(failure.fingerprint, []).append(failure)
    for group in grouped.values():
        group.sort(key=lambda f: f.ts)
    return grouped


def find_recurring(
    *,
    scan: TrajectoryScan,
    min_occurrences: int = MIN_OCCURRENCES,
    min_sessions: int = MIN_SESSIONS,
    window_days: float = DEFAULT_WINDOW_DAYS,
    min_span_days: float = MIN_SPAN_DAYS,
    max_silence_days: float = MAX_SILENCE_DAYS,
) -> RecurrenceReport:
    """Aplica el filtro sobre una lectura de trayectoria ya hecha.

    Recibe el barrido en lugar de leerlo, para que quien llame pueda medir una vez y usar
    el mismo resultado en varias etapas.

    Clasifica cada forma en **uno** de tres destinos, en este orden:

    1. **Rechazo del arnés** si el texto es un guardarraíl. Se excluye siempre: no es un
       fallo del agente. (Medido: 42 % de los fallos de una ventana real.)
    2. **Ráfaga** si supera el umbral pero sus apariciones caben en menos de
       ``min_span_days``. Es un episodio, no un patrón.
    3. **Caduco** si su última aparición quedó a más de ``max_silence_days``. El patrón
       existe en el histórico pero ya no está vivo.
    4. **Recurrente** si supera el umbral, se sostiene en el tiempo y sigue ocurriendo.

    Las apariciones se cuentan **solo dentro de la ventana**: un fallo que ocurrió tres
    veces hace un año no es recurrente hoy.
    """
    grouped = group_failures(scan.failures)
    recurring: list[RecurringFailure] = []
    bursts: list[Burst] = []
    guardrails: list[GuardrailEvent] = []
    stale: list[StaleFailure] = []
    single_events = 0
    sessions: set[str] = set()

    for fingerprint, group in grouped.items():
        sessions.update(f.session_id for f in group if f.session_id)

        in_window = [
            f for f in group
            if (scan.scanned_until - f.ts) / 86400.0 <= window_days
        ]
        if not in_window:
            continue  # solo apariciones viejas: no dice nada sobre hoy

        distinct_sessions = len({f.session_id for f in in_window if f.session_id}) or 1
        occurrences = len(in_window)
        first, last = in_window[0], in_window[-1]
        span = (last.ts - first.ts) / 86400.0

        # 1. Guardarraíl: el arnés negándose. No es un error del agente.
        if is_guardrail(first.sample):
            guardrails.append(
                GuardrailEvent(
                    fingerprint=fingerprint,
                    tool_name=first.tool_name,
                    occurrences=occurrences,
                    sample=first.sample,
                )
            )
            continue

        if not is_recurring(
            occurrences=occurrences,
            sessions=distinct_sessions,
            min_occurrences=min_occurrences,
            min_sessions=min_sessions,
        ):
            single_events += 1
            continue

        # 2. Ráfaga: supera el umbral, pero todo pasó en horas.
        if span < min_span_days:
            bursts.append(
                Burst(
                    fingerprint=fingerprint,
                    tool_name=first.tool_name,
                    occurrences=occurrences,
                    sessions=distinct_sessions,
                    span_days=span,
                    sample=first.sample,
                )
            )
            continue

        # 3. Caduco: se repitió, pero dejó de ocurrir. Un cambio de hoy no lo arregla —
        # ya está arreglado, o ya no aplica. Y el "éxito" posterior no probaría nada,
        # porque el fallo estaba muerto antes del cambio.
        silence = (scan.scanned_until - last.ts) / 86400.0
        if silence > max_silence_days:
            stale.append(
                StaleFailure(
                    fingerprint=fingerprint,
                    tool_name=first.tool_name,
                    occurrences=occurrences,
                    sessions=distinct_sessions,
                    span_days=span,
                    silence_days=silence,
                    sample=first.sample,
                )
            )
            continue

        # 4. Recurrente sostenido y vivo.
        recurring.append(
            RecurringFailure(
                fingerprint=fingerprint,
                tool_name=first.tool_name,
                occurrences=occurrences,
                sessions=distinct_sessions,
                first_ts=first.ts,
                last_ts=last.ts,
                sample=first.sample,
            )
        )

    # Orden: primero lo más frecuente, y a igual frecuencia lo más reciente. Es el orden
    # en que conviene mirarlos, no una preferencia estética.
    recurring.sort(key=lambda r: (-r.occurrences, -r.last_ts))
    bursts.sort(key=lambda b: -b.occurrences)
    guardrails.sort(key=lambda g: -g.occurrences)

    return RecurrenceReport(
        recurring=tuple(recurring),
        bursts=tuple(bursts),
        guardrails=tuple(guardrails),
        stale=tuple(stale),
        single_events=single_events,
        total_failures=len(scan.failures),
        sessions_seen=len(sessions),
        discarded_ts=scan.discarded_ts,
        window_days=window_days,
        scanned_until=scan.scanned_until,
    )


def detect(
    *,
    state_db: Optional[Path],
    window_days: float = DEFAULT_WINDOW_DAYS,
    min_occurrences: int = MIN_OCCURRENCES,
    min_sessions: int = MIN_SESSIONS,
    min_span_days: float = MIN_SPAN_DAYS,
    max_silence_days: float = MAX_SILENCE_DAYS,
    now: Optional[float] = None,
) -> RecurrenceReport:
    """Lee la trayectoria y aplica el filtro en una llamada.

    Punto de entrada para quien no necesita el barrido crudo.
    """
    reference = time.time() if now is None else now
    since = reference - (window_days * 86400.0)
    scan = scan_failures(state_db=state_db, since_ts=since, now=reference)
    return find_recurring(
        scan=scan,
        min_occurrences=min_occurrences,
        min_sessions=min_sessions,
        window_days=window_days,
        min_span_days=min_span_days,
        max_silence_days=max_silence_days,
    )


__all__ = [
    "MIN_OCCURRENCES",
    "MIN_SESSIONS",
    "MIN_SPAN_DAYS",
    "MAX_SILENCE_DAYS",
    "StaleFailure",
    "DEFAULT_WINDOW_DAYS",
    "GUARDRAIL_MARKERS",
    "RecurringFailure",
    "Burst",
    "GuardrailEvent",
    "RecurrenceReport",
    "is_recurring",
    "is_guardrail",
    "group_failures",
    "find_recurring",
    "detect",
]
