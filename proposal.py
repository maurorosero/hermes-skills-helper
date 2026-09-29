"""Etapa 3 — Propuesta. **La única etapa con modelo.**

**Pregunta:** ¿cuál es el cambio mínimo que evita que este fallo se repita?

Entrada: la evidencia de la etapa 1. Salida: **una** propuesta.

Por qué una y no varias
-----------------------
Más propuestas no es más ayuda. Cada cambio potencial altera la conducta del agente en
todas las sesiones futuras, así que el cuello de botella no es generar candidatos: es
**elegir**. Pedir tres propuestas y aplicar la mejor delegaría en el plugin la decisión
que él no puede medir todavía, y multiplicaría por tres el gasto de la única etapa que
consume inferencia.

`no_op` es una salida legítima
-----------------------------
Poder responder *"no corresponde ningún cambio"* es lo que separa un revisor de un
generador de parches. Un pipeline que sólo sabe proponer cambios encuentra cambios en
cualquier entrada — y con el historial de este proyecto (un revisor que creaba skills sin
control), ese es el modo de fallo a evitar.

Por eso `no_op` no es un caso de error: es una de las tres acciones válidas, con el mismo
tratamiento que las otras.

La propuesta se verifica, no se cree
------------------------------------
El modelo devuelve el texto del cambio, y **nada de lo que devuelve se aplica sin
comprobar**. Tres verificaciones deterministas antes de que una propuesta se considere
válida:

```
la acción es una de las tres      patch | create | no_op
el ancla existe EXACTAMENTE una vez  una edición de patch dice "reemplazá esto por esto".
                                  Si el ancla no aparece, el modelo inventó el contenido
                                  actual del skill. Si aparece dos veces, la edición es
                                  ambigua y aplicarla elegiría una arbitrariamente.
el cambio es mínimo               una propuesta que reescribe el archivo entero no es el
                                  cambio mínimo, aunque compile.
```

Un `patch` con ancla inexistente es la falla característica de un modelo escribiendo una
edición: describe el archivo como cree que es. Detectarlo es barato y determinista.

Un solo lugar donde el gasto es visible
---------------------------------------
`propose` **pide presupuesto antes de llamar** (etapa 2) y **lo libera si la llamada
falla**. El orden importa: si se reservara después, dos llamadas concurrentes podrían
pasarse del techo de costo; y sin liberar, un error de red costaría un lugar del
presupuesto — el gasto se iría en intentos en lugar de en trabajo.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

try:  # cargado como paquete por el arnés
    from .budget import (  # noqa: F401  (BudgetStatus se reexporta para quien llama)
        BudgetStatus,
        KIND_MODEL_RUN,
        release,
        reserve,
    )
    from .recurrence import RecurringFailure
except ImportError:  # cargado como módulo suelto (tests, ejecución directa)
    from budget import (  # type: ignore  # noqa: F401
        BudgetStatus,
        KIND_MODEL_RUN,
        release,
        reserve,
    )
    from recurrence import RecurringFailure  # type: ignore

logger = logging.getLogger(__name__)

#: Acciones válidas. `no_op` está al mismo nivel que las otras dos, a propósito.
ACTION_PATCH = "patch"
ACTION_CREATE = "create"
ACTION_NO_OP = "no_op"
VALID_ACTIONS = (ACTION_PATCH, ACTION_CREATE, ACTION_NO_OP)

#: Techo de tamaño para el texto de una propuesta. Un cambio "mínimo" que ocupa más que
#: esto no es mínimo: es una reescritura disfrazada. Es una verificación determinista, no
#: un juicio estético.
MAX_PROPOSAL_CHARS = 4000

#: Techo de tamaño del material de entrada. La etapa 1 puede traer decenas de muestras; el
#: prompt no puede crecer con ellas sin límite.
MAX_EVIDENCE_CHARS = 12000

#: Cuántas muestras del fallo se incluyen. El modelo necesita ver el fallo, no el historial
#: completo: veinte repeticiones del mismo error no agregan información sobre la primera.
MAX_SAMPLES = 3

#: Esquema de la propuesta. Se le pasa al arnés **y** se revalida acá: una salida parseada
#: por el host sigue siendo salida de un modelo, y la verificación que importa es la que
#: corre en el código propio.
PROPOSAL_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action", "skill_name", "justification", "expected_result"],
    "properties": {
        "action": {"type": "string", "enum": list(VALID_ACTIONS)},
        "skill_name": {"type": "string"},
        "anchor": {
            "type": "string",
            "description": (
                "Para action=patch: el fragmento EXACTO del skill que se va a reemplazar, "
                "copiado textualmente. Debe aparecer una sola vez. Vacío si no aplica."
            ),
        },
        "replacement": {
            "type": "string",
            "description": (
                "Para action=patch: el texto que reemplaza al ancla. Vacío si no aplica."
            ),
        },
        "justification": {
            "type": "string",
            "description": "Por qué este cambio evita que el fallo se repita.",
        },
        "expected_result": {
            "type": "string",
            "description": "Cómo se sabrá si sirvió, en términos observables.",
        },
    },
}

_SYSTEM_PROMPT = """\
Sos un revisor de skills de un agente de IA. Tu trabajo es leer un fallo que se repite y \
decidir si corresponde un cambio en un skill, y cuál es el cambio MÍNIMO.

Reglas:
- Si el fallo no se evita con una instrucción en un skill, respondé action="no_op". \
No inventes un cambio para tener algo que decir: "no_op" es una respuesta correcta y \
frecuente.
- Una sola instrucción. No reescribas el skill: cambiá lo mínimo que evite el fallo.
- En "anchor", copiá TEXTUALMENTE el fragmento del skill que vas a reemplazar. Si no \
podés copiarlo exactamente, respondé action="no_op".
- Escribí en español, sin adornos, en el mismo registro que el skill."""


@dataclass(frozen=True)
class Proposal:
    """Una propuesta, con su veredicto de validación.

    ``accepted`` es lo único que habilita a aplicarla. Una propuesta rechazada conserva
    ``rejection`` con el motivo: quien lea el informe tiene que poder ver **por qué** el
    modelo falló, no sólo que falló.
    """

    action: str
    skill_name: str
    anchor: str = ""
    replacement: str = ""
    justification: str = ""
    expected_result: str = ""
    accepted: bool = False
    rejection: str = ""
    model: str = ""
    provider: str = ""
    usage: dict = field(default_factory=dict)

    @property
    def is_no_op(self) -> bool:
        return self.action == ACTION_NO_OP

    @property
    def is_change(self) -> bool:
        return self.action in (ACTION_PATCH, ACTION_CREATE) and self.accepted

    def summary(self) -> str:
        if self.action == ACTION_NO_OP:
            return f"no_op: no corresponde un cambio ({self.justification[:80]})"
        if not self.accepted:
            return f"{self.action} RECHAZADA: {self.rejection}"
        delta = len(self.replacement) - len(self.anchor)
        return (f"{self.action} en '{self.skill_name}': {delta:+d} caracteres "
                f"({len(self.anchor)} → {len(self.replacement)})")

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "skill_name": self.skill_name,
            "anchor": self.anchor,
            "replacement": self.replacement,
            "justification": self.justification,
            "expected_result": self.expected_result,
            "accepted": self.accepted,
            "rejection": self.rejection,
            "model": self.model,
            "provider": self.provider,
            "usage": self.usage,
        }


@dataclass(frozen=True)
class ProposalOutcome:
    """Resultado completo del intento, incluido cuando **no** hubo llamada.

    ``called`` distingue "el modelo dijo no_op" de "no se llamó al modelo". Son cosas
    distintas y confundirlas reportaría como decisión del modelo lo que fue falta de
    presupuesto.
    """

    proposal: Optional[Proposal]
    called: bool
    reason: str
    budget: Optional[BudgetStatus] = None
    error: str = ""


def build_prompt(
    *,
    failure: RecurringFailure,
    skill_name: str,
    skill_content: str,
    samples: Optional[Sequence[str]] = None,
) -> list:
    """Arma los bloques de entrada, con techos de tamaño explícitos.

    Devuelve **bloques**, no texto suelto. Verificado contra el arnés: `complete_structured`
    normaliza cada entrada con `_normalize_input_block`, que acepta un
    `PluginLlmTextInput` o un dict `{"type": "text", "text": ...}` y **levanta
    `ValueError` ante un `str`**. Se usa el dict plano en lugar de la clase del host para
    no acoplar el módulo a internos que pueden cambiar, y para que los tests corran sin el
    arnés presente.

    El material de la etapa 1 puede ser arbitrariamente grande. Acá se recorta con un
    límite declarado: un prompt que crece con el historial vuelve la etapa impredecible en
    costo, que es justo lo que la etapa 2 existe para controlar.

    Se incluye el **skill completo** porque el modelo necesita ver el texto real para
    copiar un ancla literal. Sin eso, cualquier ancla que devuelva sería inventada.
    """
    muestras = list(samples or [failure.sample])
    muestras = [m for m in muestras if m][:MAX_SAMPLES]

    partes = [
        "## Fallo que se repite",
        f"Herramienta: {failure.tool_name}",
        f"Apariciones: {failure.occurrences} en {failure.sessions} sesiones distintas",
    ]
    for i, m in enumerate(muestras, 1):
        partes.append(f"Muestra {i}: {m}")

    partes.append("")
    partes.append("## Skill a revisar")
    partes.append(f"Nombre: {skill_name}")
    partes.append("```markdown")
    partes.append(skill_content)
    partes.append("```")

    texto = "\n".join(partes)
    if len(texto) > MAX_EVIDENCE_CHARS:
        # Se recorta por el FINAL del skill, no por el principio: el encabezado del skill
        # es donde viven las instrucciones que un cambio mínimo editaría.
        exceso = len(texto) - MAX_EVIDENCE_CHARS
        texto = texto[: MAX_EVIDENCE_CHARS] + (
            f"\n\n[recortado: {exceso} caracteres omitidos por el techo de entrada]"
        )
    return [{"type": "text", "text": texto}]


def _check_schema_shape(data: Any) -> str:
    """Verificación estructural propia, independiente de la del arnés.

    Se hace acá y no se delega: el arnés puede no tener `jsonschema` instalado, y aunque lo
    tenga, la forma que este módulo necesita es un requisito suyo.
    """
    if not isinstance(data, dict):
        return f"la respuesta no es un objeto (llegó {type(data).__name__})"
    action = data.get("action")
    if action not in VALID_ACTIONS:
        return f"acción inválida: {action!r} (válidas: {VALID_ACTIONS})"
    for key in ("skill_name", "justification", "expected_result"):
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            return f"falta '{key}' o está vacío"
    for key in ("anchor", "replacement"):
        if key in data and not isinstance(data[key], str):
            return f"'{key}' debe ser texto"
    return ""


def validate_proposal(
    data: Any,
    *,
    skill_content: str,
    target_skill: str,
    skill_exists: Optional[bool] = None,
) -> Proposal:
    """Convierte la respuesta cruda del modelo en una propuesta **verificada**.

    Las verificaciones que deciden son deterministas y están acá, no en el prompt:
    pedirle al modelo que "verifique" es pedirle que se autoevalúe, y un examinador que se
    toma su propio examen no examina nada.
    """
    shape_error = _check_schema_shape(data)
    if shape_error:
        return Proposal(
            action=str(data.get("action", "")) if isinstance(data, dict) else "",
            skill_name=str(data.get("skill_name", "")) if isinstance(data, dict) else "",
            accepted=False,
            rejection=shape_error,
        )

    action = data["action"]
    skill_name = data["skill_name"].strip()
    anchor = (data.get("anchor") or "").strip()
    replacement = data.get("replacement") or ""
    justification = data["justification"].strip()
    expected = data["expected_result"].strip()

    base = dict(
        action=action,
        skill_name=skill_name,
        anchor=anchor,
        replacement=replacement,
        justification=justification,
        expected_result=expected,
    )

    if action == ACTION_NO_OP:
        # `no_op` se acepta tal cual. Es una respuesta legítima y la más segura: no hay
        # nada que verificar porque no hay nada que aplicar.
        return Proposal(**base, accepted=True)

    if action == ACTION_CREATE:
        if skill_exists:
            return Proposal(
                **base, accepted=False,
                rejection=(
                    f"propuso crear '{skill_name}', que ya existe: para un skill "
                    f"existente corresponde patch o no_op"
                ),
            )
        if not replacement.strip():
            return Proposal(
                **base, accepted=False,
                rejection="propuso crear un skill sin contenido",
            )
        return Proposal(**base, accepted=True)

    # action == patch
    if not anchor:
        return Proposal(
            **base, accepted=False,
            rejection="propuso un patch sin ancla: no se puede saber qué reemplazar",
        )
    if not skill_content:
        return Proposal(
            **base, accepted=False,
            rejection=f"propuso un patch sobre '{skill_name}', cuyo contenido no se pudo leer",
        )

    ocurrencias = skill_content.count(anchor)
    if ocurrencias == 0:
        return Proposal(
            **base, accepted=False,
            rejection=(
                "el ancla NO aparece en el skill: el modelo describió el archivo como cree "
                "que es. Aplicarla no tendría efecto o fallaría"
            ),
        )
    if ocurrencias > 1:
        return Proposal(
            **base, accepted=False,
            rejection=(
                f"el ancla aparece {ocurrencias} veces: la edición es ambigua y aplicarla "
                f"elegiría una arbitrariamente"
            ),
        )
    if not replacement.strip():
        return Proposal(
            **base, accepted=False,
            rejection="propuso un patch que borra el ancla sin poner nada en su lugar",
        )

    if len(replacement) > MAX_PROPOSAL_CHARS:
        return Proposal(
            **base, accepted=False,
            rejection=(
                f"el reemplazo ocupa {len(replacement)} caracteres, más que el techo de "
                f"{MAX_PROPOSAL_CHARS}: no es un cambio mínimo"
            ),
        )

    return Proposal(**base, accepted=True)


def propose(
    *,
    failure: RecurringFailure,
    skill_name: str,
    skill_content: str,
    llm: Any,
    hermes_home: Path,
    max_edits_per_day: int = 3,
    max_model_runs_per_day: int = 30,
    samples: Optional[Sequence[str]] = None,
    skill_exists: Optional[bool] = None,
) -> ProposalOutcome:
    """Pide presupuesto, hace **una** llamada al modelo, y verifica la propuesta.

    Una sola llamada. No hay reintento automático: si el modelo devuelve algo inválido, la
    respuesta correcta es reportarlo, no gastar otro lugar del techo probando de nuevo
    hasta que acierte. Un reintento silencioso es un techo que no se respeta.
    """
    reserva = reserve(KIND_MODEL_RUN, hermes_home=hermes_home,
                      max_edits_per_day=max_edits_per_day,
                      max_model_runs_per_day=max_model_runs_per_day)
    if not reserva.granted:
        return ProposalOutcome(
            proposal=None, called=False,
            reason=f"sin presupuesto, no se llamó al modelo: {reserva.reason}",
        )

    prompt = build_prompt(
        failure=failure, skill_name=skill_name, skill_content=skill_content, samples=samples,
    )

    try:
        result = llm.complete_structured(
            instructions=_SYSTEM_PROMPT,
            input=prompt,
            json_schema=PROPOSAL_SCHEMA,
            schema_name="skill_proposal",
            max_tokens=1200,
            purpose="hermes-skills-helper: propuesta de cambio mínimo",
        )
    except Exception as exc:
        # La llamada no consumió: devolver el lugar. Sin esto, un error de red gastaría
        # presupuesto en intentos en lugar de en trabajo.
        liberado = release(reserva.token or "", hermes_home=hermes_home,
                           reason=f"la llamada falló: {exc}")
        return ProposalOutcome(
            proposal=None, called=True,
            reason=(
                "la llamada al modelo falló"
                + ("" if liberado else " (no se pudo liberar el lugar reservado)")
            ),
            error=str(exc),
        )

    parsed = getattr(result, "parsed", None)
    if parsed is None:
        # Sin JSON parseable no hay propuesta: el texto libre no se interpreta a la
        # ligera. Convertir prosa en una edición sería adivinar.
        return ProposalOutcome(
            proposal=None, called=True,
            reason=(
                "el modelo no devolvió JSON válido contra el esquema; no se interpreta "
                "texto libre como una edición"
            ),
            error=(getattr(result, "text", "") or "")[:500],
        )

    proposal = validate_proposal(
        parsed, skill_content=skill_content, target_skill=skill_name,
        skill_exists=skill_exists,
    )
    # Los metadatos del modelo no afectan la validez: solo explican de dónde salió.
    proposal = Proposal(
        **{**proposal.to_dict(),
           "model": getattr(result, "model", ""),
           "provider": getattr(result, "provider", ""),
           "usage": {
               "input_tokens": getattr(getattr(result, "usage", None), "input_tokens", 0),
               "output_tokens": getattr(getattr(result, "usage", None), "output_tokens", 0),
           }}
    )
    return ProposalOutcome(
        proposal=proposal,
        called=True,
        reason=(
            "no_op: el modelo no propuso un cambio"
            if proposal.is_no_op
            else ("propuesta verificada" if proposal.accepted
                  else f"propuesta rechazada: {proposal.rejection}")
        ),
    )


def apply_proposal(proposal: Proposal, *, skill_content: str) -> tuple[bool, str]:
    """Materializa el cambio en texto, **sin escribir a disco**.

    Devuelve el contenido nuevo para que el llamador lo pase por el journal (etapa 4) y
    recién entonces lo escriba. Separar "calcular el texto" de "escribir" es lo que permite
    que la etapa 4 respalde antes de que nada cambie.

    Revalida las condiciones del patch: entre generar la propuesta y aplicarla puede haber
    pasado tiempo, y el skill pudo cambiar.
    """
    if not proposal.accepted:
        return False, f"propuesta no aceptada: {proposal.rejection}"
    if proposal.is_no_op:
        return False, "no_op no produce un cambio materializable"

    if proposal.action == ACTION_CREATE:
        return True, proposal.replacement

    if proposal.action != ACTION_PATCH:
        return False, f"acción desconocida: {proposal.action!r}"

    ocurrencias = skill_content.count(proposal.anchor)
    if ocurrencias != 1:
        return False, (
            f"el ancla ya no aparece exactamente una vez en el skill (aparece "
            f"{ocurrencias}): el skill cambió desde que se generó la propuesta"
        )
    actualizado = skill_content.replace(proposal.anchor, proposal.replacement, 1)
    return True, actualizado


def health() -> dict:
    """Estática del módulo, para diagnóstico."""
    return {
        "valid_actions": list(VALID_ACTIONS),
        "max_proposal_chars": MAX_PROPOSAL_CHARS,
        "max_evidence_chars": MAX_EVIDENCE_CHARS,
        "max_samples": MAX_SAMPLES,
        "schema_keys": sorted(PROPOSAL_SCHEMA["required"]),
    }


__all__ = [
    "ACTION_PATCH",
    "ACTION_CREATE",
    "ACTION_NO_OP",
    "VALID_ACTIONS",
    "MAX_PROPOSAL_CHARS",
    "MAX_EVIDENCE_CHARS",
    "MAX_SAMPLES",
    "PROPOSAL_SCHEMA",
    "Proposal",
    "ProposalOutcome",
    "build_prompt",
    "validate_proposal",
    "propose",
    "apply_proposal",
    "health",
]
