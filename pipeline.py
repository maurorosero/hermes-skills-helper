"""El ciclo completo: las cinco etapas en orden.

Este módulo es lo que convierte los cinco módulos verificados en un plugin que
**hace** algo. Y su diseño está gobernado por tres hallazgos medidos contra el arnés,
no por lo que parecía razonable antes de mirar.

Hallazgo 1 — `on_session_end` corre POR TURNO, no por sesión
-----------------------------------------------------------
El comentario del propio arnés (`agent/turn_finalizer.py`) lo dice sin ambigüedad:
*"run_conversation() runs once per message"*. El hook se llama en cada mensaje, con
`session_id`, `turn_id`, `completed`, `failed`, `interrupted`.

Consecuencia de diseño: **el hook no puede ser el que propone.** Un gancho por turno que
llame al modelo gastaría el techo de costo en la primera hora de conversación. El hook
hace sólo el trabajo **determinista y barato** —y ni siquiera eso en cada turno, por el
throttle de abajo.

Hallazgo 2 — el techo existe para que el gasto sea una decisión, no un efecto
---------------------------------------------------------------------------
El barrido completo de la trayectoria tarda **0,30 s** medidos (234 fallos, 7
recurrentes). No es caro, pero tampoco gratis: a un hook por turno, 0,30 s por mensaje es
impuesto. De ahí el **intervalo mínimo** entre barridos.

Hallazgo 3 — el arnés YA tiene el gate de aprobación, y está encendido
---------------------------------------------------------------------
`skills.write_approval: true` en la configuración, y verificado en ejecución:
`skill_manage(action='create', ...)` devuelve

    {"success": true, "staged": true, "pending_id": "4e23e8c8",
     "message": "Staged for approval (skills.write_approval is on)."}

El cambio **no se aplica**: queda en cola para que Mauro lo revise. Es exactamente el
mecanismo que este proyecto iba a inventar, ya construido y en uso. Inventar el propio
sería agregar un segundo lugar donde aprobar, y el riesgo de que uno de los dos se
saltee.

Por eso la aplicación pasa por `skill_manage`. Y por eso, si esa vía no está
disponible, el plugin **no escribe por atajo**: falla y lo dice. Escribir directo para
"no depender del host" se saltaría la aprobación de Mauro, que es justamente la parte
que no se negocia.

El orden de las etapas y por qué
--------------------------------
El hook corre (con throttle):

```
1. recurrencia     barrido determinista → qué fallos se repiten
2. techo           cuánto queda hoy → se informa, no se gasta
5. ¿sirvió?        los cambios aplicados hace días: ¿sobrevivieron, se usaron, el
                   error volvió?
```

La etapa 3 (la única con modelo) **no corre en el hook**. Corre cuando se la pide, con
el tool `skills_review`, y gasta un lugar del techo de llamadas. Separar "medir" de
"proponer" es lo que hace que el gasto sea una decisión explícita.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

try:  # cargado como paquete por el arnés
    from .budget import (
        DEFAULT_MAX_EDITS_PER_DAY,
        DEFAULT_MAX_MODEL_RUNS_PER_DAY,
        BudgetStatus,
        ceilings_from_config,
        health as budget_health,
        status as budget_status,
    )
    from .effect import evaluate
    from .effect.usage import read_record as read_usage_record
    from .journal import (
        health as journal_health,
        last_applied,
        rollback_last,
        record_after_write,
        record_before_write,
        rollback,
    )
    from .proposal import Proposal, ProposalOutcome, apply_proposal, propose
    from .recurrence import RecurrenceReport, detect
    from .trajectory import scan_failures
except ImportError:  # cargado como módulo suelto (tests, ejecución directa)
    from budget import (  # type: ignore
        DEFAULT_MAX_EDITS_PER_DAY,
        DEFAULT_MAX_MODEL_RUNS_PER_DAY,
        BudgetStatus,
        ceilings_from_config,
        health as budget_health,
        status as budget_status,
    )
    from effect import evaluate  # type: ignore
    from effect.usage import read_record as read_usage_record  # type: ignore
    from journal import (  # type: ignore
        health as journal_health,
        last_applied,
        rollback_last,
        record_after_write,
        record_before_write,
        rollback,
    )
    from proposal import Proposal, ProposalOutcome, apply_proposal, propose  # type: ignore
    from recurrence import RecurrenceReport, detect  # type: ignore
    from trajectory import scan_failures  # type: ignore

logger = logging.getLogger(__name__)

#: Intervalo mínimo entre barridos. El hook corre por turno; sin esto, cada mensaje de
#: una conversación pagaría los 0,30 s del barrido.
MIN_SCAN_INTERVAL_SECONDS = 900.0  # 15 minutos

#: Nombre del archivo donde se guarda el último informe y el estado del ciclo.
STATE_FILE_NAME = "pipeline-state.json"

#: Días de gracia antes de calificar un cambio aplicado. Un cambio recién aplicado no
#: tiene uso medible todavía: preguntarle "¿sirvió?" el mismo día es preguntarle a la
#: nada, y produciría el veredicto `too_new` en masa.
GRACE_DAYS = 3


@dataclass
class PipelineReport:
    """Lo que produjo una corrida determinista del ciclo.

    ``scanned`` distingue *"se barrió y no hay nada"* de *"no se barrió"*. Sin esa
    distinción, un informe vacío por throttle se leería como ausencia de fallos.
    """

    scanned: bool
    reason: str
    recurrence: Optional[RecurrenceReport] = None
    effects: list = field(default_factory=list)
    budget: Optional[BudgetStatus] = None
    ts: float = 0.0
    errors: list = field(default_factory=list)
    trajectory_readable: bool = True

    @property
    def limitation(self) -> str:
        """Lo que impide sostener la conclusión, si algo la impide.

        Una trayectoria ilegible hace que "0 recurrentes" signifique *"no se pudo mirar"*,
        no *"no hay nada"*. Sin esta distinción, el pipeline reportaría como buena noticia
        el hecho de no haber podido leer.
        """
        if not self.trajectory_readable:
            return "la trayectoria no se pudo leer: 'sin candidatos' no es una conclusión"
        if self.recurrence is not None and not self.recurrence.trustworthy:
            return (
                f"se descartaron {self.recurrence.discarded_ts} de "
                f"{self.recurrence.total_failures} filas por fecha increíble"
            )
        return ""

    @property
    def candidates(self) -> int:
        """Cuántos fallos justificarían una propuesta (los recurrentes sostenidos)."""
        return len(self.recurrence.recurring) if self.recurrence else 0

    def summary(self) -> str:
        if not self.scanned:
            return f"sin barrido: {self.reason}"
        # La limitación va PRIMERO cuando existe: un informe que la escondiera al final
        # se leería como un resultado limpio.
        limite = self.limitation
        prefijo = f"LIMITADO — {limite} · " if limite else ""
        partes = [f"{prefijo}{self.candidates} fallo(s) recurrente(s) sostenido(s)"]
        if self.recurrence:
            partes.append(
                f"{len(self.recurrence.guardrails)} rechazo(s) del arnés y "
                f"{len(self.recurrence.bursts)} ráfaga(s) descartados"
            )
        if self.effects:
            partes.append(f"{len(self.effects)} cambio(s) calificado(s)")
        if self.budget:
            partes.append(self.budget.summary())
        if self.errors:
            partes.append(f"{len(self.errors)} error(es)")
        return " · ".join(partes)


def state_path(*, hermes_home: Path) -> Path:
    return Path(hermes_home) / "plugins" / "hermes-skills-helper" / STATE_FILE_NAME


def read_state(*, hermes_home: Path) -> dict:
    """Estado del ciclo. No crea nada: un lector no materializa lo que inspecciona."""
    path = state_path(hermes_home=hermes_home)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Estado del pipeline ilegible: %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


def write_state(data: dict, *, hermes_home: Path) -> None:
    path = state_path(hermes_home=hermes_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)


def should_scan(*, hermes_home: Path, now: Optional[float] = None,
                min_interval: float = MIN_SCAN_INTERVAL_SECONDS) -> tuple[bool, str]:
    """¿Corresponde barrer ahora? El throttle, con su razón.

    Devuelve la razón en ambos casos para que un barrido salteado sea auditable: "no se
    barrió" tiene que poder distinguirse de "se barrió y no había nada".
    """
    marca = time.time() if now is None else now
    ultimo = read_state(hermes_home=hermes_home).get("last_scan_ts")
    if not isinstance(ultimo, (int, float)):
        return True, "no hay barrido previo registrado"
    transcurrido = marca - float(ultimo)
    if transcurrido < 0:
        # El reloj se movió hacia atrás. No se castiga con un bloqueo largo: se barre.
        return True, "el reloj retrocedió respecto del último barrido"
    if transcurrido < min_interval:
        faltan = min_interval - transcurrido
        return False, f"faltan {faltan:.0f} s para el próximo barrido ({min_interval:.0f} s)"
    return True, f"pasaron {transcurrido:.0f} s desde el último barrido"


def state_db_path(*, hermes_home: Path) -> Path:
    return Path(hermes_home) / "state.db"


def _skill_path(*, skill_name: str, skills_dirs: list) -> Optional[Path]:
    """Ubica el SKILL.md de un skill, buscando en los directorios dados.

    Se buscan los directorios externos que el arnés tiene configurados: el hub de Mauro
    vive fuera de `~/.hermes/skills`, y buscar sólo en la ruta por defecto encontraría
    nada para la mayoría de los skills.
    """
    for base in skills_dirs:
        base = Path(base)
        directo = base / skill_name / "SKILL.md"
        if directo.is_file():
            return directo
        # Categorizado: <base>/<categoria>/<skill>/SKILL.md
        for candidato in sorted(base.glob(f"*/{skill_name}/SKILL.md")):
            return candidato
    return None


def run_deterministic(
    *,
    hermes_home: Path,
    skills_dirs: Optional[list] = None,
    ctx: Any = None,
    now: Optional[float] = None,
    force: bool = False,
    min_interval: float = MIN_SCAN_INTERVAL_SECONDS,
) -> PipelineReport:
    """Corre las etapas deterministas: 1 (recurrencia), 2 (techo) y 5 (¿sirvió?).

    **No llama al modelo nunca.** Es lo que el hook puede ejecutar en cada turno sin
    gastar presupuesto ni latencia apreciable.

    Cada etapa está envuelta: una que falla no impide que las otras corran. Un ciclo que
    se aborta entero porque la trayectoria no se pudo leer dejaría también sin medir los
    cambios ya aplicados.
    """
    marca = time.time() if now is None else now
    dirs = list(skills_dirs or [Path(hermes_home) / "skills"])
    report = PipelineReport(scanned=False, reason="", ts=marca)

    scan_reason = "barrido forzado"
    if not force:
        corresponde, razon = should_scan(hermes_home=hermes_home, now=marca,
                                         min_interval=min_interval)
        if not corresponde:
            report.reason = razon
            return report
        scan_reason = razon

    report.scanned = True
    # La razón del barrido se conserva. Sobreescribirla con un "barrido completo" genérico
    # perdería el dato de que el reloj retrocedió: es la clase de detalle que sólo se
    # necesita cuando algo ya salió mal, y para entonces ya no está.
    report.reason = f"barrido completo ({scan_reason})"

    # --- Etapa 2: el techo (barata, y el informe la necesita) --------------------
    try:
        edits_ceiling, runs_ceiling = (
            ceilings_from_config(ctx) if ctx is not None
            else (DEFAULT_MAX_EDITS_PER_DAY, DEFAULT_MAX_MODEL_RUNS_PER_DAY)
        )
        report.budget = budget_status(
            hermes_home=hermes_home, max_edits_per_day=edits_ceiling,
            max_model_runs_per_day=runs_ceiling, now=marca,
        )
    except Exception as exc:
        report.errors.append(f"techo: {exc}")

    # --- Etapa 1: la recurrencia -------------------------------------------------
    db = state_db_path(hermes_home=hermes_home)
    # Se comprueba la legibilidad ANTES de barrer: el lector devuelve un barrido vacío
    # cuando no puede abrir la base, y ese vacío es indistinguible de "no había fallos".
    report.trajectory_readable = db.is_file()
    try:
        report.recurrence = detect(state_db=db, now=marca)
    except Exception as exc:
        report.errors.append(f"recurrencia: {exc}")
        report.trajectory_readable = False

    # --- Etapa 5: ¿sirvió? -------------------------------------------------------
    try:
        report.effects = grade_applied_changes(
            hermes_home=hermes_home, skills_dirs=dirs, now=marca)
    except Exception as exc:
        report.errors.append(f"efecto: {exc}")

    try:
        write_state(
            {
                "last_scan_ts": marca,
                "last_report": {
                    "candidates": report.candidates,
                    "summary": report.summary(),
                    "generated_at": marca,
                },
            },
            hermes_home=hermes_home,
        )
    except Exception as exc:
        report.errors.append(f"estado: {exc}")

    return report


def grade_applied_changes(
    *,
    hermes_home: Path,
    skills_dirs: list,
    now: Optional[float] = None,
) -> list:
    """Etapa 5 sobre cada cambio aplicado que ya cumplió la gracia.

    Un cambio aplicado hoy no se califica hoy: no tiene uso medible todavía. La gracia
    existe para que el veredicto signifique algo en lugar de producir `too_new` en masa.
    """
    marca = time.time() if now is None else now
    resultados = []
    entrada = last_applied(hermes_home=hermes_home)
    if entrada is None:
        return resultados

    edad_dias = (marca - entrada.applied_ts) / 86400.0 if entrada.applied_ts else 0.0
    if edad_dias < GRACE_DAYS:
        return resultados

    skill_path = _skill_path(skill_name=entrada.skill_name, skills_dirs=skills_dirs)
    if skill_path is None:
        return resultados

    try:
        metadatos = entrada.metadata or {}
        veredicto = evaluate(
            skill_name=entrada.skill_name,
            skill_path=skill_path,
            expected_hash=entrada.after_hash or "",
            applied_ts=entrada.applied_ts,
            # El fingerprint del fallo que motivó el cambio. Sin él, el tercer chequeo
            # (¿el error volvió?) no tendría contra qué comparar. Se lee de los metadatos
            # que la etapa 3 dejó al aplicar.
            target_fingerprint=metadatos.get("target_fingerprint", ""),
            fingerprints_after=_fingerprints_after(
                hermes_home=hermes_home, since=entrada.applied_ts, now=marca),
            hermes_home=hermes_home,
            state_db=state_db_path(hermes_home=hermes_home),
            now=marca,
        )
        resultados.append(veredicto)
    except Exception as exc:
        logger.warning("No se pudo calificar %s: %s", entrada.skill_name, exc)
    return resultados


def _fingerprints_after(*, hermes_home: Path, since: float, now: float) -> tuple:
    """Huellas de los fallos posteriores al cambio, para el tercer chequeo.

    Se reusa el lector de la etapa 1 (`scan_failures`) en lugar de consultar la base por
    segunda vez: los fingerprints ya salen normalizados por el mismo módulo, y una
    segunda implementación de la normalización produciría huellas que no coinciden entre
    sí — con lo que el chequeo de recurrencia compararía cosas incomparables y diría
    siempre que el error no volvió.
    """
    try:
        barrido = scan_failures(
            state_db=state_db_path(hermes_home=hermes_home), since_ts=since, now=now)
    except Exception:
        return ()
    return tuple(f.fingerprint for f in barrido.failures)


def review_candidate(
    *,
    failure,
    skill_name: str,
    llm: Any,
    hermes_home: Path,
    skills_dirs: Optional[list] = None,
    ctx: Any = None,
    apply: bool = False,
    skills_manage: Any = None,
    now: Optional[float] = None,
) -> dict:
    """Etapa 3 (con modelo) + 4 (journal) + aplicación por la vía del host.

    Es el único camino que llama al modelo, y por eso **no lo llama el hook**: se invoca
    desde el tool `skills_review`.

    ``apply=False`` es el modo por defecto: propone y se detiene. El diseño lo pide
    (*"sin rollback verificado no hay aplicación de cambios"*) y la práctica también — un
    revisor que aplica solo es el revisor que este proyecto vino a reemplazar.

    Cuando ``apply=True``, el cambio se escribe por ``skills_manage``, que es la vía del
    arnés **con su gate de aprobación**. Si esa vía no está, se falla en lugar de escribir
    por atajo: saltarse la aprobación de Mauro no es una optimización.
    """
    marca = time.time() if now is None else now
    dirs = list(skills_dirs or [Path(hermes_home) / "skills"])
    edits_ceiling, runs_ceiling = (
        ceilings_from_config(ctx) if ctx is not None
        else (DEFAULT_MAX_EDITS_PER_DAY, DEFAULT_MAX_MODEL_RUNS_PER_DAY)
    )

    skill_path = _skill_path(skill_name=skill_name, skills_dirs=dirs)
    if skill_path is None:
        return {
            "ok": False,
            "stage": "localizar",
            "message": f"no se encontró el SKILL.md de '{skill_name}' en {dirs}",
        }
    contenido = skill_path.read_text(encoding="utf-8")

    outcome: ProposalOutcome = propose(
        failure=failure,
        skill_name=skill_name,
        skill_content=contenido,
        llm=llm,
        hermes_home=hermes_home,
        max_edits_per_day=edits_ceiling,
        max_model_runs_per_day=runs_ceiling,
    )

    resultado = {
        "ok": True,
        "stage": "propuesta",
        "called": outcome.called,
        "reason": outcome.reason,
        "error": outcome.error,
        "applied": False,
    }
    if outcome.proposal is None:
        resultado["ok"] = outcome.called
        return resultado

    prop: Proposal = outcome.proposal
    resultado["proposal"] = prop.to_dict()
    resultado["summary"] = prop.summary()

    if prop.is_no_op:
        resultado["stage"] = "no_op"
        return resultado
    if not prop.accepted:
        resultado["stage"] = "rechazada"
        resultado["ok"] = False
        return resultado
    if not apply:
        resultado["stage"] = "propuesta lista (sin aplicar)"
        resultado["next"] = (
            "revisar y volver a invocar con apply=True. La escritura pasa por el gate "
            "de aprobación del arnés"
        )
        return resultado

    ok, nuevo_contenido = apply_proposal(prop, skill_content=contenido)
    if not ok:
        resultado["stage"] = "no materializable"
        resultado["ok"] = False
        resultado["message"] = nuevo_contenido
        return resultado

    if skills_manage is None:
        # Sin la vía del host no se escribe. Escribir directo se saltaría el gate.
        resultado["stage"] = "sin vía de escritura"
        resultado["ok"] = False
        resultado["message"] = (
            "no hay vía para aplicar el cambio por el arnés; no se escribe por atajo "
            "porque eso se saltaría el gate de aprobación"
        )
        return resultado

    if prop.action == "patch":
        entrada = record_before_write(
            skill_name=skill_name, skill_path=skill_path, action="patch",
            hermes_home=hermes_home, reason=prop.justification[:200],
        )
    else:
        entrada = record_before_write(
            skill_name=skill_name, skill_path=skill_path, action="create",
            hermes_home=hermes_home, reason=prop.justification[:200],
        )

    try:
        respuesta = skills_manage(
            action=prop.action, name=skill_name, content=nuevo_contenido,
            old_string=prop.anchor if prop.action == "patch" else None,
            new_string=prop.replacement if prop.action == "patch" else None,
        )
    except Exception as exc:
        resultado["stage"] = "la escritura falló"
        resultado["ok"] = False
        resultado["message"] = str(exc)
        resultado["journal_entry"] = entrada.entry_id
        return resultado

    try:
        parsed = json.loads(respuesta) if isinstance(respuesta, str) else respuesta
    except ValueError:
        parsed = {"raw": str(respuesta)[:400]}

    resultado["host_response"] = parsed
    resultado["journal_entry"] = entrada.entry_id

    # Si el arnés deja el cambio en cola, el archivo NO se modificó: registrar el estado
    # ``applied`` sería afirmar un cambio que no ocurrió.
    if isinstance(parsed, dict) and parsed.get("staged"):
        resultado["stage"] = "en cola de aprobación del arnés"
        resultado["applied"] = False
        resultado["message"] = parsed.get("message", "queda pendiente de aprobación")
        return resultado

    if isinstance(parsed, dict) and parsed.get("success"):
        record_after_write(entrada, new_content=nuevo_contenido, hermes_home=hermes_home)
        resultado["stage"] = "aplicado y registrado"
        resultado["applied"] = True
        return resultado

    resultado["stage"] = "el arnés rechazó la escritura"
    resultado["ok"] = False
    return resultado


def undo_last(*, hermes_home: Path) -> dict:
    """Revierte el último cambio aplicado, por la vía del journal (etapa 4).

    `rollback_last` se importa en el bloque de arriba, junto al resto: un import relativo
    dentro de la función funciona cuando el arnés carga el plugin como paquete, pero falla
    con ``attempted relative import with no known parent package`` cuando el módulo se
    carga suelto. Un import que sólo funciona en uno de los dos modos de carga es un fallo
    que aparece en producción y no en los tests.
    """
    resultado = rollback_last(hermes_home=hermes_home)
    return {
        "ok": resultado.success,
        "entry_id": resultado.entry_id,
        "message": resultado.message,
        "restored_hash": resultado.restored_hash,
    }


def health(*, hermes_home: Path) -> dict:
    """Estado del ciclo completo, para diagnóstico. No crea ni modifica nada."""
    estado = read_state(hermes_home=hermes_home)
    return {
        "state_path": str(state_path(hermes_home=hermes_home)),
        "last_scan_ts": estado.get("last_scan_ts"),
        "last_report": estado.get("last_report"),
        "min_scan_interval_seconds": MIN_SCAN_INTERVAL_SECONDS,
        "grace_days": GRACE_DAYS,
        "budget": budget_health(hermes_home=hermes_home),
        "journal": journal_health(hermes_home=hermes_home),
    }


__all__ = [
    "MIN_SCAN_INTERVAL_SECONDS",
    "GRACE_DAYS",
    "STATE_FILE_NAME",
    "PipelineReport",
    "run_deterministic",
    "review_candidate",
    "grade_applied_changes",
    "grade_applied_changes",
    "review_candidate",
    "undo_last",
    "should_scan",
    "read_state",
    "write_state",
    "state_path",
    "state_db_path",
    "health",
]
