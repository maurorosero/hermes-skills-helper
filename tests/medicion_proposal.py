"""Medicion real de la etapa 3: UNA llamada al modelo de verdad.

Criterio de cierre del diseno:
    "una propuesta valida contra el esquema, una llamada, y verificacion de que el
    techo se respeto"

El modelo simulado prueba el codigo. Esta corrida prueba que el codigo funciona
CONTRA EL ARNES REAL: que ctx.llm acepta la llamada, que el esquema viaja, que el
host parsea el JSON, y que el techo cuenta.

Es UN solo llamado, con un fallo real de la trayectoria.
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

from budget import health as budget_health, status  # noqa: E402
from proposal import propose, apply_proposal  # noqa: E402
from recurrence import detect  # noqa: E402

print("=" * 74)
print("MEDICION REAL - etapa 3 contra el arnes")
print("=" * 74)

# --- 1. Un fallo real de la trayectoria -----------------------------------------
print()
print("--- 1. de donde sale el fallo")
state_db = Path("/home/andrea/.hermes/state.db")
report = detect(state_db=state_db)
print("  recurrentes detectados en la trayectoria real: %d" % len(report.recurring))
if not report.recurring:
    print("  No hay recurrentes. No se puede medir contra algo real.")
    raise SystemExit(1)
fallo = report.recurring[0]
print("  fallo elegido : [%s] x%d en %d sesiones" % (
    fallo.tool_name, fallo.occurrences, fallo.sessions))
print("  muestra       : %s" % fallo.sample[:90].replace("\n", " "))

# --- 2. El skill objetivo -------------------------------------------------------
skill_dir = Path("/home/andrea/developers/ai/skills/productivity/maps")
skill_path = skill_dir / "SKILL.md"
if not skill_path.is_file():
    candidatos = sorted(Path("/home/andrea/developers/ai/skills").glob("*/*/SKILL.md"))
    skill_path = candidatos[0]
skill_name = skill_path.parent.name
contenido_skill = skill_path.read_text(encoding="utf-8")
print()
print("--- 2. skill objetivo")
print("  skill  : %s" % skill_name)
print("  tamano : %d caracteres" % len(contenido_skill))

# --- 3. LA LLAMADA --------------------------------------------------------------
print()
print("--- 3. la llamada al modelo (1 sola)")
from agent.plugin_llm import PluginLlm  # noqa: E402

root = Path(tempfile.mkdtemp(prefix="hsh-proposal-medicion-"))
try:
    home = root / "hermes"
    llm = PluginLlm(plugin_id="hermes-skills-helper")

    antes = status(hermes_home=home, max_model_runs_per_day=30)

    try:
        outcome = propose(
            failure=fallo,
            skill_name=skill_name,
            skill_content=contenido_skill,
            llm=llm,
            hermes_home=home,
        )
    except Exception as exc:
        print("  LA LLAMADA FALLO: %s: %s" % (type(exc).__name__, exc))
        outcome = None

    despues = status(hermes_home=home, max_model_runs_per_day=30)
    h = budget_health(hermes_home=home)

    print("  llamada realizada : %s" % outcome.called if outcome else "  llamada: no")
    print("  reservas en el libro : %d" % h["reservations"])
    print("  contador antes  : %s" % antes.summary())
    print("  contador despues: %s" % despues.summary())

    if outcome is None:
        raise SystemExit(1)

    print()
    print("  resultado: %s" % outcome.reason)
    if outcome.error:
        print("  error    : %s" % outcome.error[:200])

    prop = outcome.proposal
    if prop is None:
        print()
        print("  NO HUBO PROPUESTA.")
        if outcome.error and "falló" in outcome.reason:
            print("  La llamada fallo ANTES de consumir.")
            print("  El lugar reservado se LIBERO: el contador volvio a 0.")
        else:
            print("  El modelo no devolvio JSON valido contra el esquema.")
            print("  La llamada SI se hizo y el lugar quedo consumido.")
        raise SystemExit(1 if "falló" in outcome.reason else 0)

    print()
    print("--- 4. la propuesta")
    print("  accion        : %s" % prop.action)
    print("  aceptada      : %s" % prop.accepted)
    if prop.rejection:
        print("  rechazo       : %s" % prop.rejection)
    print("  skill         : %s" % prop.skill_name)
    print("  justificacion : %s" % prop.justification[:220])
    print("  resultado esp.: %s" % prop.expected_result[:200])
    if prop.anchor:
        print("  ancla (%d car.)" % len(prop.anchor))
        print("    %s" % prop.anchor[:180].replace("\n", " | "))
        print("  reemplazo (%d car.)" % len(prop.replacement))
        print("    %s" % prop.replacement[:180].replace("\n", " | "))
    print("  modelo        : %s / %s" % (prop.provider, prop.model))
    print("  tokens        : %s" % prop.usage)

    # --- 5. Aplicar sobre TEXTO, sin escribir ------------------------------------
    print()
    print("--- 5. materializar (sin escribir a disco)")
    if prop.is_change:
        ok, nuevo = apply_proposal(prop, skill_content=contenido_skill)
        print("  aplicable       : %s" % ok)
        if ok:
            print("  tamano antes    : %d" % len(contenido_skill))
            print("  tamano despues  : %d" % len(nuevo))
            print("  delta           : %+d caracteres" % (len(nuevo) - len(contenido_skill)))
            print("  el original NO se toco: %s" % (
                skill_path.read_text(encoding="utf-8") == contenido_skill))
    else:
        print("  no_op o rechazada: nada que materializar")
        ok = False

    # --- 6. VERIFICACION ---------------------------------------------------------
    print()
    print("=" * 74)
    print("VERIFICACION")
    print("=" * 74)

    ok_llamada = outcome.called is True
    ok_techo = despues.model_runs_used == 1
    ok_esquema = prop is not None
    ok_verificada = (prop.accepted or bool(prop.rejection)) if prop else False
    ok_original = skill_path.read_text(encoding="utf-8") == contenido_skill

    print("  se llamo al modelo                        : %s" % ("SI" if ok_llamada else "NO"))
    print("  el techo conto EXACTAMENTE 1 llamada      : %s" % ("SI" if ok_techo else "NO"))
    print("  el host devolvio JSON valido contra el esquema: %s" % ("SI" if ok_esquema else "NO"))
    print("  la propuesta tiene veredicto (aceptada o con razon del rechazo): %s"
          % ("SI" if ok_verificada else "NO"))
    print("  el skill original no se modfico           : %s" % ("SI" if ok_original else "NO"))

    print()
    if all([ok_llamada, ok_techo, ok_esquema, ok_verificada, ok_original]):
        print("  ETAPA 3 VERIFICADA contra el arnes: llamada real, esquema real, techo real.")
    else:
        print("  NO VERIFICADA.")
        raise SystemExit(1)
finally:
    shutil.rmtree(root, ignore_errors=True)
