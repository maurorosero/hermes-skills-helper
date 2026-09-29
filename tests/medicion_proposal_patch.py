"""Medicion del segundo camino: un skill que SI corresponde al fallo.

La primera medicion dio no_op (correctamente: el fallo de SCHEMA.md no pertenece a
hello-world). Falta probar el camino donde el modelo SI debe proponer un patch, y
verificar lo mas importante:

  que su ancla aparezca LITERALMENTE en el skill.

Un ancla inventada es el modo de fallo caracteristico de un modelo escribiendo una
edicion: describe el archivo como cree que es. Si `validate_proposal` lo detecta
contra el modelo REAL, la verificacion sirve para algo.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

PLUGIN = Path("/home/andrea/developers/hermes/plugins/hermes-skills-helper")
HERMES_AGENT = Path("/home/andrea/.hermes/hermes-agent")
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(HERMES_AGENT))

from agent.plugin_llm import PluginLlm  # noqa: E402
from budget import health as budget_health, status  # noqa: E402
from proposal import apply_proposal, propose  # noqa: E402
from recurrence import detect  # noqa: E402

print("=" * 74)
print("MEDICION REAL - etapa 3, camino PATCH")
print("=" * 74)

state_db = Path("/home/andrea/.hermes/state.db")
report = detect(state_db=state_db)

# El fallo de SCHEMA.md: el skill que deberia llevar esa instruccion es el de ingesta.
objetivo = None
for fallo in report.recurring:
    objetivo = fallo
    break
print()
print("  fallo: [%s] x%d  %s" % (objetivo.tool_name, objetivo.occurrences,
                                objetivo.sample[:70].replace("\n", " ")))

# El skill cuyo dominio SI cubre este fallo.
candidatos = [
    Path("/home/andrea/developers/ai/skills/wiki/wiki-llm-ingesta/SKILL.md"),
    Path("/home/andrea/developers/ai/skills/wiki/wiki-llm-rosero/SKILL.md"),
]
skill_path = next((c for c in candidatos if c.is_file()), None)
if skill_path is None:
    print("  No hay skill de wiki disponible.")
    raise SystemExit(1)

skill_name = skill_path.parent.name
contenido = skill_path.read_text(encoding="utf-8")
print("  skill objetivo: %s (%d caracteres)" % (skill_name, len(contenido)))

root = Path(tempfile.mkdtemp(prefix="hsh-patch-medicion-"))
try:
    home = root / "hermes"
    llm = PluginLlm(plugin_id="hermes-skills-helper")
    antes = status(hermes_home=home, max_model_runs_per_day=30)

    outcome = propose(failure=objetivo, skill_name=skill_name, skill_content=contenido,
                      llm=llm, hermes_home=home)

    despues = status(hermes_home=home, max_model_runs_per_day=30)
    print()
    print("  contador: %s -> %s" % (antes.summary(), despues.summary()))
    print("  resultado: %s" % outcome.reason)

    prop = outcome.proposal
    if prop is None:
        print("  Sin propuesta. Error: %s" % outcome.error[:200])
        raise SystemExit(1)

    print()
    print("--- la propuesta")
    print("  accion   : %s" % prop.action)
    print("  aceptada : %s" % prop.accepted)
    print("  skill    : %s" % prop.skill_name)
    if prop.rejection:
        print("  rechazo  : %s" % prop.rejection)
    print("  justif.  : %s" % prop.justification[:260])
    print("  esperado : %s" % prop.expected_result[:220])
    print("  modelo   : %s / %s  tokens=%s" % (
        prop.provider, prop.model, prop.usage))

    if prop.anchor:
        print()
        print("--- EL ANCLA (lo que se verifica)")
        print("  largo: %d caracteres" % len(prop.anchor))
        for linea in prop.anchor.splitlines()[:8]:
            print("    | %s" % linea)
        print("  ocurrencias literales en el skill: %d"
              % contenido.count(prop.anchor))
        print("  reemplazo: %d caracteres" % len(prop.replacement))

    print()
    print("--- materializar")
    ok = False
    if prop.is_change:
        ok, nuevo = apply_proposal(prop, skill_content=contenido)
        print("  aplicable: %s" % ok)
        if ok:
            print("  delta: %+d caracteres" % (len(nuevo) - len(contenido)))
            print("  el original NO se toco: %s"
                  % (skill_path.read_text(encoding="utf-8") == contenido))
    else:
        print("  %s: nada que materializar" % prop.action)

    print()
    print("=" * 74)
    print("VERIFICACION")
    print("=" * 74)
    ok_nunca_inventado = (prop.action != "patch") or (contenido.count(prop.anchor) == 1)
    ok_veredicto = prop.accepted or bool(prop.rejection)
    ok_techo = despues.model_runs_used == 1
    ok_original = skill_path.read_text(encoding="utf-8") == contenido

    print("  el ancla (si hubo patch) aparece EXACTAMENTE 1 vez en el skill: %s"
          % ("SI" if ok_nunca_inventado else "NO"))
    print("  la propuesta tiene veredicto explicito            : %s"
          % ("SI" if ok_veredicto else "NO"))
    print("  el techo conto 1 llamada                         : %s"
          % ("SI" if ok_techo else "NO"))
    print("  el skill original no se modifico                 : %s"
          % ("SI" if ok_original else "NO"))
    print()
    print("  accion devuelta: %s" % prop.action)
    if prop.action == "patch" and ok:
        print("  EL MODELO PROPUSO UN PATCH APLICABLE sobre el skill real.")
    elif prop.action == "patch":
        print("  El modelo propuso un patch que NO se pudo aplicar; el veredicto lo dice.")
    else:
        print("  El modelo no propuso un cambio para este skill tampoco.")
finally:
    shutil.rmtree(root, ignore_errors=True)
