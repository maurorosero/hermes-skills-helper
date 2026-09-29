"""Tests de la etapa 3 — propuesta.

El criterio de cierre del diseño:

    "una propuesta válida contra el esquema, una llamada, y verificación de que el techo
    se respetó"

Los tres se verifican acá. El modelo se **simula** (`FakeLlm`): un test que depende de la
salida de un modelo real no prueba el código, prueba el humor del modelo ese día. Lo que
se prueba es qué hace el módulo con cada respuesta posible —la buena, la inválida, la que
describe un archivo que no existe, la que no es JSON— y que no se gaste una llamada de más.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from budget import KIND_MODEL_RUN, status  # noqa: E402
from proposal import (  # noqa: E402
    ACTION_CREATE,
    ACTION_NO_OP,
    ACTION_PATCH,
    MAX_EVIDENCE_CHARS,
    MAX_PROPOSAL_CHARS,
    PROPOSAL_SCHEMA,
    apply_proposal,
    build_prompt,
    propose,
    validate_proposal,
)
from recurrence import RecurringFailure  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ✓ {label}")
    else:
        FAILED.append(label)
        print(f"  ✗ {label}  {detail}")


SKILL = """---
name: maps
description: Use when consultando mapas. Geocodifica y rutea.
---

# Maps

## Uso

Llamar `maps` con una dirección.

Para rutas, pasar origen y destino.
"""


def _failure(**kwargs) -> RecurringFailure:
    base = dict(
        fingerprint="abc123",
        tool_name="read_file",
        occurrences=13,
        sessions=9,
        first_ts=0.0,
        last_ts=0.0,
        sample="File not found: /home/andrea/wiki/SCHEMA.md",
    )
    base.update(kwargs)
    return RecurringFailure(**base)


class FakeResult:
    def __init__(self, parsed=None, text="", model="fake-model", provider="fake") -> None:
        self.parsed = parsed
        self.text = text
        self.model = model
        self.provider = provider
        self.usage = type("U", (), {"input_tokens": 100, "output_tokens": 50})()


class FakeLlm:
    """Modelo simulado. Registra cuántas veces se lo llamó."""

    def __init__(self, result=None, raises: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.result = result
        self.raises = raises

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise self.raises
        return self.result


class Sandbox:
    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="hsh-proposal-"))
        self.home = self.root / "hermes"

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def _valid_response(action=ACTION_PATCH, anchor="", replacement="", **over) -> dict:
    data = {
        "action": action,
        "skill_name": "maps",
        "anchor": anchor,
        "replacement": replacement,
        "justification": "Agregar el paso que faltaba evita el error.",
        "expected_result": "El error no reaparece en las próximas sesiones.",
    }
    data.update(over)
    return data


def test_validation_accepts_clean_patch() -> None:
    print("\nuna propuesta limpia se acepta")

    ancla = "Para rutas, pasar origen y destino."
    p = validate_proposal(
        _valid_response(anchor=ancla, replacement=ancla + " Verificar el resultado."),
        skill_content=SKILL, target_skill="maps",
    )
    check("se acepta", p.accepted is True, p.rejection)
    check("la acción es patch", p.action == ACTION_PATCH)
    check("guarda el ancla y el reemplazo", bool(p.anchor) and bool(p.replacement))
    check("no es no_op", p.is_no_op is False)
    check("sí es un cambio", p.is_change is True)
    check("el resumen describe el delta",
          "caracteres" in p.summary(), p.summary())


def test_rejects_anchor_that_does_not_exist() -> None:
    print("\nun ancla inventada se rechaza — el modo de fallo característico")

    p = validate_proposal(
        _valid_response(anchor="Este texto no está en el skill.", replacement="otra cosa"),
        skill_content=SKILL, target_skill="maps",
    )
    check("se rechaza", p.accepted is False)
    check("la razón explica que el ancla no aparece",
          "NO aparece" in p.rejection, p.rejection)
    check("y nombra la causa real: describió el archivo como cree que es",
          "cree" in p.rejection, p.rejection)
    check("no queda como cambio aplicable", p.is_change is False)


def test_rejects_ambiguous_anchor() -> None:
    print("\nun ancla ambigua se rechaza")

    repetido = "## Uso"
    contenido = SKILL + "\n\n## Uso\n\nOtra sección con el mismo título.\n"
    p = validate_proposal(
        _valid_response(anchor=repetido, replacement="## Uso distinto"),
        skill_content=contenido, target_skill="maps",
    )
    check("se rechaza", p.accepted is False)
    check("la razón dice cuántas veces aparece", "veces" in p.rejection, p.rejection)
    check("y por qué importa: sería arbitrario", "arbitrari" in p.rejection, p.rejection)


def test_rejects_empty_anchor_and_deleting_patch() -> None:
    print("\nancla vacía y patch que solo borra")

    p = validate_proposal(_valid_response(anchor="", replacement="x"),
                          skill_content=SKILL, target_skill="maps")
    check("ancla vacía se rechaza", p.accepted is False)
    check("la razón lo nombra", "sin ancla" in p.rejection, p.rejection)

    ancla = "Para rutas, pasar origen y destino."
    p2 = validate_proposal(_valid_response(anchor=ancla, replacement="   "),
                           skill_content=SKILL, target_skill="maps")
    check("un patch que solo borra se rechaza", p2.accepted is False)
    check("la razón lo nombra", "borra" in p2.rejection, p2.rejection)


def test_rejects_oversized_replacement() -> None:
    print("\nun reemplazo enorme no es un cambio mínimo")

    ancla = "Para rutas, pasar origen y destino."
    p = validate_proposal(
        _valid_response(anchor=ancla, replacement="x" * (MAX_PROPOSAL_CHARS + 1)),
        skill_content=SKILL, target_skill="maps",
    )
    check("se rechaza por tamaño", p.accepted is False)
    check("la razón cita el techo y que no es mínimo",
          "mínimo" in p.rejection and str(MAX_PROPOSAL_CHARS) in p.rejection, p.rejection)


def test_no_op_is_legitimate() -> None:
    print("\nno_op es una respuesta legítima, no un error")

    p = validate_proposal(
        _valid_response(action=ACTION_NO_OP,
                        justification="El fallo es de red, no de instrucciones."),
        skill_content=SKILL, target_skill="maps",
    )
    check("se ACEPTA", p.accepted is True, p.rejection)
    check("es no_op", p.is_no_op is True)
    check("pero no produce un cambio", p.is_change is False)
    check("el resumen lo dice sin tratarlo como fallo",
          p.summary().startswith("no_op"), p.summary())

    ok, msg = apply_proposal(p, skill_content=SKILL)
    check("aplicarlo no materializa nada", ok is False)
    check("y lo explica", "no_op" in msg, msg)


def test_rejects_create_on_existing_skill() -> None:
    print("\nno se crea lo que ya existe")

    p = validate_proposal(
        _valid_response(action=ACTION_CREATE, replacement="contenido nuevo"),
        skill_content=SKILL, target_skill="maps", skill_exists=True,
    )
    check("se rechaza", p.accepted is False)
    check("la razón propone la alternativa correcta",
          "patch" in p.rejection and "no_op" in p.rejection, p.rejection)

    p2 = validate_proposal(
        _valid_response(action=ACTION_CREATE, replacement="contenido nuevo"),
        skill_content="", target_skill="nuevo", skill_exists=False,
    )
    check("crear uno que no existe sí se acepta", p2.accepted is True, p2.rejection)

    p3 = validate_proposal(
        _valid_response(action=ACTION_CREATE, replacement=""),
        skill_content="", target_skill="nuevo", skill_exists=False,
    )
    check("crear sin contenido se rechaza", p3.accepted is False, p3.rejection)


def test_rejects_malformed_shapes() -> None:
    print("\nformas inválidas antes de mirar el contenido")

    check("no es objeto",
          validate_proposal("texto suelto", skill_content=SKILL,
                            target_skill="maps").accepted is False)
    check("acción inventada",
          validate_proposal(_valid_response(action="reescribir"),
                            skill_content=SKILL, target_skill="maps").accepted is False)
    check("sin skill_name",
          validate_proposal(_valid_response(anchor="x", replacement="y",
                                            skill_name=""),
                            skill_content=SKILL, target_skill="maps").accepted is False)
    r = validate_proposal({**_valid_response(), "justification": ""},
                          skill_content=SKILL, target_skill="maps")
    check("justificación vacía se rechaza", r.accepted is False)
    check("la razón nombra el campo", "justification" in r.rejection, r.rejection)


def test_patch_on_unreadable_skill() -> None:
    print("\nno se parchea lo que no se pudo leer")

    p = validate_proposal(
        _valid_response(anchor="algo", replacement="otra cosa"),
        skill_content="", target_skill="maps",
    )
    check("se rechaza", p.accepted is False)
    check("la razón explica que no se pudo leer",
          "leer" in p.rejection, p.rejection)


def test_prompt_contains_the_skill() -> None:
    print("\nel prompt incluye el skill real, para que el ancla pueda ser literal")

    prompt = build_prompt(failure=_failure(), skill_name="maps", skill_content=SKILL)
    check("devuelve bloques, no texto suelto",
          all(isinstance(b, dict) and b.get("type") == "text" for b in prompt), str(prompt[:1]))
    texto = prompt[0]["text"]
    check("incluye el contenido del skill", "Para rutas, pasar origen y destino." in texto)
    check("incluye el fallo", "File not found" in texto)
    check("incluye la cuenta de apariciones", "13" in texto and "9 sesiones" in texto)
    check("nombra el skill", "maps" in texto)
    check("devuelve una lista de bloques", isinstance(prompt, list) and len(prompt) >= 1)

    muchos = build_prompt(failure=_failure(), skill_name="maps", skill_content=SKILL,
                          samples=[f"muestra {i}" for i in range(20)])
    check("limita las muestras a 3", muchos[0]["text"].count("Muestra ") <= 3,
          str(muchos[0]["text"].count("Muestra ")))


def test_prompt_respects_size_ceiling() -> None:
    print("\nel prompt tiene techo de tamaño")

    enorme = SKILL * 2000
    prompt = build_prompt(failure=_failure(), skill_name="maps", skill_content=enorme)
    texto = prompt[0]["text"]
    check("se recorta por debajo del techo + el aviso",
          len(texto) < MAX_EVIDENCE_CHARS + 200, f"{len(texto)}")
    check("el recorte se declara", "recortado" in texto, texto[-120:])
    check("conserva el principio del skill, donde viven las instrucciones",
          texto.startswith("## Fallo que se repite"))


def test_propose_calls_model_once_and_reserves() -> None:
    print("\nuna llamada, y el techo se respetó")

    box = Sandbox()
    try:
        ancla = "Para rutas, pasar origen y destino."
        llm = FakeLlm(FakeResult(parsed=_valid_response(
            anchor=ancla, replacement=ancla + " Verificar.")))

        antes = status(hermes_home=box.home, max_model_runs_per_day=30)
        outcome = propose(failure=_failure(), skill_name="maps", skill_content=SKILL,
                          llm=llm, hermes_home=box.home)
        despues = status(hermes_home=box.home, max_model_runs_per_day=30)

        check("se llamó al modelo", outcome.called is True)
        check("EXACTAMENTE una vez", len(llm.calls) == 1, f"{len(llm.calls)}")
        check("se reservó un lugar", despues.model_runs_used == antes.model_runs_used + 1,
              despues.summary())
        check("la propuesta es válida", outcome.proposal.accepted is True,
              outcome.proposal.rejection)
        check("llegó el esquema al arnés",
              llm.calls[0].get("json_schema") is PROPOSAL_SCHEMA)
        check("llegó el nombre del esquema",
              llm.calls[0].get("schema_name") == "skill_proposal")
        check("llegó el prompt con el skill",
              "Para rutas" in llm.calls[0]["input"][0]["text"])
        check("cada bloque de entrada es un dict tipado, como exige el arnés",
              all(isinstance(b, dict) and b.get("type") == "text"
                  for b in llm.calls[0]["input"]),
              str(llm.calls[0]["input"][:1]))
        check("se registraron modelo y proveedor",
              outcome.proposal.model == "fake-model"
              and outcome.proposal.provider == "fake",
              f"{outcome.proposal.model}/{outcome.proposal.provider}")
        check("se registró el uso de tokens",
              outcome.proposal.usage.get("input_tokens") == 100,
              str(outcome.proposal.usage))
    finally:
        box.cleanup()


def test_no_call_without_budget() -> None:
    print("\nSIN PRESUPUESTO NO SE LLAMA AL MODELO")

    box = Sandbox()
    try:
        llm = FakeLlm(FakeResult(parsed=_valid_response(anchor="x", replacement="y")))
        outcome = propose(failure=_failure(), skill_name="maps", skill_content=SKILL,
                          llm=llm, hermes_home=box.home, max_model_runs_per_day=1)

        check("la primera sí llama", outcome.called is True and len(llm.calls) == 1)

        outcome2 = propose(failure=_failure(), skill_name="maps", skill_content=SKILL,
                           llm=llm, hermes_home=box.home, max_model_runs_per_day=1)
        check("la segunda NO llama", outcome2.called is False)
        check("el modelo sigue con UNA sola llamada", len(llm.calls) == 1,
              f"{len(llm.calls)}")
        check("el motivo nombra el techo",
              "presupuesto" in outcome2.reason and "agotado" in outcome2.reason,
              outcome2.reason)
        check("no hay propuesta", outcome2.proposal is None)
    finally:
        box.cleanup()


def test_failed_call_releases_the_slot() -> None:
    print("\nuna llamada que falló devuelve su lugar")

    box = Sandbox()
    try:
        llm = FakeLlm(raises=RuntimeError("la red se cayó"))
        outcome = propose(failure=_failure(), skill_name="maps", skill_content=SKILL,
                          llm=llm, hermes_home=box.home, max_model_runs_per_day=30)

        check("la falla se reporta, no se traga",
              outcome.called is True and "falló" in outcome.reason, outcome.reason)
        check("el error se conserva", "red" in outcome.error, outcome.error)

        s = status(hermes_home=box.home, max_model_runs_per_day=30)
        check("el lugar se devolvió: el contador quedó en 0",
              s.model_runs_used == 0, s.summary())

        # Y el presupuesto sigue disponible para un intento que sí va a funcionar.
        llm2 = FakeLlm(FakeResult(parsed=_valid_response(action=ACTION_NO_OP)))
        outcome2 = propose(failure=_failure(), skill_name="maps", skill_content=SKILL,
                           llm=llm2, hermes_home=box.home, max_model_runs_per_day=30)
        check("el siguiente intento sí llama", outcome2.called is True)
    finally:
        box.cleanup()


def test_no_retry_on_invalid_response() -> None:
    print("\nno hay reintento automático: una respuesta inválida se reporta")

    box = Sandbox()
    try:
        llm = FakeLlm(FakeResult(parsed=_valid_response(
            anchor="ancla que no existe", replacement="x")))
        outcome = propose(failure=_failure(), skill_name="maps", skill_content=SKILL,
                          llm=llm, hermes_home=box.home)

        check("una sola llamada, sin reintentos", len(llm.calls) == 1, f"{len(llm.calls)}")
        check("la propuesta queda rechazada",
              outcome.proposal.accepted is False)
        check("el motivo es explícito", "rechazada" in outcome.reason, outcome.reason)
        check("el rechazo viaja en la propuesta, para que sea auditable",
              bool(outcome.proposal.rejection))
    finally:
        box.cleanup()


def test_non_json_response_is_not_interpreted() -> None:
    print("\nprosa libre no se interpreta como una edición")

    box = Sandbox()
    try:
        llm = FakeLlm(FakeResult(parsed=None, text="Creo que deberías agregar un paso."))
        outcome = propose(failure=_failure(), skill_name="maps", skill_content=SKILL,
                          llm=llm, hermes_home=box.home)

        check("hay llamada", outcome.called is True)
        check("pero no hay propuesta", outcome.proposal is None)
        check("la razón explica que no se interpreta texto libre",
              "texto libre" in outcome.reason, outcome.reason)
        check("el texto crudo se conserva para diagnóstico",
              "agregar un paso" in outcome.error, outcome.error)
    finally:
        box.cleanup()


def test_apply_materializes_verified_text() -> None:
    print("\naplicar materializa el texto sin escribir a disco")

    ancla = "Para rutas, pasar origen y destino."
    nuevo = ancla + " Verificar que el origen exista."
    p = validate_proposal(_valid_response(anchor=ancla, replacement=nuevo),
                          skill_content=SKILL, target_skill="maps")
    ok, contenido = apply_proposal(p, skill_content=SKILL)

    check("se aplica", ok is True, contenido)
    check("el reemplazo está en el contenido nuevo", nuevo in contenido)
    check("el ancla original ya no está", ancla not in contenido.replace(nuevo, ""))
    check("el resto del skill se conservó",
          contenido.count("---") == SKILL.count("---")
          and "## Uso" in contenido)
    check("el tamaño cambió por el delta exacto",
          len(contenido) == len(SKILL) + (len(nuevo) - len(ancla)),
          f"{len(contenido)} vs {len(SKILL)}")
    check("no modificó el original (es texto, no disco)", SKILL.count(nuevo) == 0)


def test_apply_rejects_if_skill_changed() -> None:
    print("\nentre proponer y aplicar puede pasar tiempo")

    ancla = "Para rutas, pasar origen y destino."
    p = validate_proposal(_valid_response(anchor=ancla, replacement=ancla + " X."),
                          skill_content=SKILL, target_skill="maps")
    check("la propuesta es válida", p.accepted is True)

    cambiado = SKILL.replace(ancla, "El texto lo editó otro proceso.")
    ok, msg = apply_proposal(p, skill_content=cambiado)
    check("aplicar sobre un skill cambiado se niega", ok is False)
    check("la razón explica que el skill cambió",
          "ya no aparece" in msg and "cambió" in msg, msg)


def test_apply_rejects_unaccepted() -> None:
    print("\nuna propuesta rechazada no se aplica")

    p = validate_proposal(_valid_response(anchor="no existe", replacement="x"),
                          skill_content=SKILL, target_skill="maps")
    ok, msg = apply_proposal(p, skill_content=SKILL)
    check("se niega", ok is False)
    check("la razón es la del rechazo", "no aceptada" in msg, msg)


def test_schema_is_self_consistent() -> None:
    print("\nel esquema es coherente consigo mismo")

    check("es un objeto", PROPOSAL_SCHEMA["type"] == "object")
    check("no admite propiedades extra", PROPOSAL_SCHEMA["additionalProperties"] is False)
    acciones = PROPOSAL_SCHEMA["properties"]["action"]["enum"]
    check("el enum incluye las tres acciones",
          set(acciones) == {ACTION_PATCH, ACTION_CREATE, ACTION_NO_OP}, str(acciones))
    check("no_op está en el enum (no es un caso de error)",
          ACTION_NO_OP in acciones)
    for campo in PROPOSAL_SCHEMA["required"]:
        check(f"'{campo}' es requerido y está declarado",
              campo in PROPOSAL_SCHEMA["properties"])


def main() -> int:
    print("=" * 68)
    print("hermes-skills-helper — etapa 3 (propuesta, con modelo)")
    print("=" * 68)

    for fn in (
        test_validation_accepts_clean_patch,
        test_rejects_anchor_that_does_not_exist,
        test_rejects_ambiguous_anchor,
        test_rejects_empty_anchor_and_deleting_patch,
        test_rejects_oversized_replacement,
        test_no_op_is_legitimate,
        test_rejects_create_on_existing_skill,
        test_rejects_malformed_shapes,
        test_patch_on_unreadable_skill,
        test_prompt_contains_the_skill,
        test_prompt_respects_size_ceiling,
        test_schema_is_self_consistent,
        test_propose_calls_model_once_and_reserves,
        test_no_call_without_budget,
        test_failed_call_releases_the_slot,
        test_no_retry_on_invalid_response,
        test_non_json_response_is_not_interpreted,
        test_apply_materializes_verified_text,
        test_apply_rejects_if_skill_changed,
        test_apply_rejects_unaccepted,
    ):
        fn()

    print("\n" + "=" * 68)
    print(f"{PASSED} pasaron, {len(FAILED)} fallaron")
    if FAILED:
        for name in FAILED:
            print(f"  FALLO: {name}")
        return 1
    print("todo verde")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
