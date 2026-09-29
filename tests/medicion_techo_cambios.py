"""Medicion del techo de CAMBIOS: el defecto que quedaba abierto.

El techo de llamadas al modelo se cobraba. El de CAMBIOS no: `KIND_EDIT` solo se usaba en
los tests del propio budget. Consecuencia medida: con el techo de cambios en 3, se podian
aplicar cambios sin limite mientras quedara presupuesto de inferencia — el numero "<= 3
cambios/dia" estaba escrito en el diseno y nunca se usaba.

Esta medicion corre el pipeline real sobre un sandbox y muestra los cargos.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import budget  # noqa: E402
import pipeline  # noqa: E402
from proposal import ACTION_NO_OP  # noqa: E402
from recurrence import RecurringFailure  # noqa: E402


class FakeResult:
    def __init__(self, parsed=None):
        self.parsed = parsed
        self.text = json.dumps(parsed or {})
        self.model = "fake"
        self.provider = "fake"
        self.usage = {}


class FakeLlm:
    def __init__(self, parsed):
        self._parsed = parsed

    def complete_structured(self, **kwargs):
        return FakeResult(self._parsed)


def _failure():
    return RecurringFailure(
        fingerprint="abc123def456", tool_name="terminal",
        occurrences=5, sessions=4, first_ts=0.0, last_ts=1.0,
        sample="boom: fallo que se repite",
    )


def main() -> int:
    raiz = Path(tempfile.mkdtemp())
    home = raiz / "hermes"
    skills = raiz / "skills"
    (skills / "maps").mkdir(parents=True)
    skill = skills / "maps" / "SKILL.md"
    skill.write_text(
        "# maps\n\nPara rutas, pasar origen y destino.\n", encoding="utf-8"
    )
    home.mkdir(parents=True, exist_ok=True)

    ancla = "Para rutas, pasar origen y destino."
    # El esquema de la propuesta exige estos nombres exactos: sin ``skill_name`` el
    # verificador la rechaza y no se llega a aplicar nada (medido, no supuesto).
    patch_ok = {
        "action": "patch",
        "skill_name": "maps",
        "anchor": ancla,
        "replacement": ancla + " Verificar que el origen exista.",
        "justification": "el fallo muestra que el origen no se verifica",
        "expected_result": "menos fallos de ruta",
    }

    print("=" * 74)
    print("TECHO DE CAMBIOS — el defecto que quedaba abierto")
    print("=" * 74)

    estado0 = budget.status(hermes_home=home)
    print(f"\nantes de aplicar nada:")
    print(f"  cambios usados  : {estado0.edits_used}/{estado0.edits_ceiling}")
    print(f"  llamadas usadas : {estado0.model_runs_used}/{estado0.model_runs_ceiling}")

    def correr(accion=patch_ok, apply=True):
        def skills_manage(**kw):
            skill.write_text(kw["content"], encoding="utf-8")
            return json.dumps({"success": True})

        return pipeline.review_candidate(
            failure=_failure(), skill_name="maps",
            llm=FakeLlm(accion), hermes_home=home,
            skills_dirs=[skills], apply=apply, skills_manage=skills_manage,
        )

    print("\n--- 1. un cambio que se aplica ---")
    r1 = correr()
    e1 = budget.status(hermes_home=home)
    print(f"  aplicado        : {r1.get('applied')}")
    print(f"  cambios usados  : {e1.edits_used}/{e1.edits_ceiling}   <-- antes quedaba en 0")

    print("\n--- 2. se agotan los 3 lugares ---")
    correr()
    correr()
    e2 = budget.status(hermes_home=home)
    print(f"  cambios usados  : {e2.edits_used}/{e2.edits_ceiling}")

    print("\n--- 3. el cuarto cambio (el techo ya esta lleno) ---")
    r4 = correr()
    e3 = budget.status(hermes_home=home)
    print(f"  aplicado        : {r4.get('applied')}")
    print(f"  etapa           : {r4.get('stage')}")
    print(f"  mensaje         : {str(r4.get('message'))[:96]}")
    print(f"  cambios usados  : {e3.edits_used}/{e3.edits_ceiling}  (no se cobro de mas)")

    print("\n--- 4. no_op NO gasta el techo de cambios ---")
    home2 = raiz / "h2"
    home2.mkdir()
    skills2 = raiz / "s2"
    (skills2 / "maps").mkdir(parents=True)
    (skills2 / "maps" / "SKILL.md").write_text("x", encoding="utf-8")

    def manage2(**kw):
        return json.dumps({"success": True})

    pipeline.review_candidate(
        failure=_failure(), skill_name="maps",
        llm=FakeLlm({"action": ACTION_NO_OP, "skill_name": "maps",
                     "justification": "no corresponde",
                     "expected_result": "nada"}),
        hermes_home=home2, skills_dirs=[skills2], apply=True, skills_manage=manage2,
    )
    e4 = budget.status(hermes_home=home2)
    print(f"  cambios usados  : {e4.edits_used}/{e4.edits_ceiling}  <-- 0, correcto")

    print("\n--- 5. encolado tampoco (todavia no ocurrio) ---")
    home3 = raiz / "h3"
    home3.mkdir()
    skills3 = raiz / "s3"
    (skills3 / "maps").mkdir(parents=True)
    (skills3 / "maps" / "SKILL.md").write_text(
        "# maps\n\n" + ancla + "\n", encoding="utf-8")

    def manage3(**kw):
        return json.dumps({"success": True, "staged": True, "pending_id": "p1"})

    r5 = pipeline.review_candidate(
        failure=_failure(), skill_name="maps", llm=FakeLlm(patch_ok),
        hermes_home=home3, skills_dirs=[skills3], apply=True, skills_manage=manage3,
    )
    e5 = budget.status(hermes_home=home3)
    print(f"  aplicado        : {r5.get('applied')}")
    print(f"  etapa           : {r5.get('stage')}")
    print(f"  cambios usados  : {e5.edits_used}/{e5.edits_ceiling}  <-- 0, no se castiga")

    print()
    print("=" * 74)
    veredictos = [
        ("el cambio aplicado COBRA el techo de cambios", e1.edits_used == 1),
        ("los tres lugares se agotan de verdad", e2.edits_used == 3),
        ("el cuarto NO se aplica", r4.get("applied") is not True),
        ("y el motivo lo nombra", "cambio" in str(r4.get("message", "")).lower()),
        ("no se cobro de mas al rechazar", e3.edits_used == 3),
        ("no_op no gasta el techo", e4.edits_used == 0),
        ("encolado no gasta el techo", e5.edits_used == 0),
    ]
    for etiqueta, ok in veredictos:
        print(f"  {'SI' if ok else 'NO':>2}  {etiqueta}")
    print()
    print("  TODOS OK" if all(v for _, v in veredictos) else "  HAY FALLOS")
    print("=" * 74)

    shutil.rmtree(raiz, ignore_errors=True)
    return 0 if all(v for _, v in veredictos) else 1


if __name__ == "__main__":
    raise SystemExit(main())
