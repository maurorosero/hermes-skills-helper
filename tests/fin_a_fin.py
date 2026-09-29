"""Fin a fin: el ciclo completo sobre un caso real, por la via del tool.

Es la prueba que faltaba. No mide una etapa: mide la COSTURA de las cinco, sobre
la trayectoria real de Mauro y un skill real.

Se invoca `_tool_review`, que es exactamente el codigo que corre cuando un tool
call lo llama. No un atajo, no un mock: el handler real con el modelo real.

Lo que se verifica:
  1. el ciclo determinista encuentra el fallo
  2. el modelo propone (o dice no_op)
  3. la propuesta se verifica: el ancla existe literalmente
  4. la aplicacion pasa por el gate del arnes y queda EN COLA
  5. el skill NO se toco
  6. el journal NO registra un cambio que no ocurrio
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PLUGIN_DIR = Path("/home/andrea/developers/hermes/plugins/hermes-skills-helper")
HERMES_AGENT = Path("/home/andrea/.hermes/hermes-agent")
HOME = Path("/home/andrea/.hermes")

sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(HERMES_AGENT))

print("=" * 74)
print("FIN A FIN - el ciclo completo, sobre un caso real")
print("=" * 74)

from effect.checker import sha256_text  # noqa: E402
from journal import health as journal_health, read_entries  # noqa: E402
from pipeline import grade_applied_changes, run_deterministic  # noqa: E402

# --- 0. Foto previa ------------------------------------------------------------
skill_path = Path("/home/andrea/developers/ai/skills/wiki/wiki-llm-ingesta/SKILL.md")
if not skill_path.is_file():
    print("  El skill objetivo no existe: %s" % skill_path)
    raise SystemExit(1)

contenido_antes = skill_path.read_text(encoding="utf-8")
hash_antes = sha256_text(contenido_antes)
cola_antes = sorted(Path("/home/andrea/.hermes/pending/skills").glob("*.json"))
jornal_antes = journal_health(hermes_home=HOME)

print()
print("--- 0. foto previa")
print("  skill          : %s" % skill_path.parent.name)
print("  tamano         : %d caracteres" % len(contenido_antes))
print("  hash           : %s" % hash_antes[:32])
print("  cola de aprob. : %d entradas" % len(cola_antes))
print("  journal        : %d lineas, %d revertibles"
      % (jornal_antes["entries"], jornal_antes["revertible_entries"]))

# --- 1. El ciclo determinista --------------------------------------------------
print()
print("--- 1. ciclo determinista (etapas 1 y 2)")
informe = run_deterministic(hermes_home=HOME, force=True)
print("  %s" % informe.summary())
if not informe.recurrence or not informe.recurrence.recurring:
    print("  Sin candidatos: el ciclo no puede seguir.")
    raise SystemExit(1)

candidato = informe.recurrence.recurring[0]
print("  candidato elegido: [%s] x%d en %d sesiones" % (
    candidato.tool_name, candidato.occurrences, candidato.sessions))

# --- 2. El tool real, con apply=True ------------------------------------------
print()
print("--- 2. el tool real (skill=wiki-llm-ingesta, apply=True)")
print("  (una sola llamada al modelo; el techo lo controla la etapa 2)")

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("hsh_e2e", PLUGIN_DIR / "__init__.py")
plugin = importlib.util.module_from_spec(_spec)
sys.modules["hsh_e2e"] = plugin
_spec.loader.exec_module(plugin)

crudo = plugin._tool_review(index=0, skill="wiki-llm-ingesta", apply=True)
resultado = json.loads(crudo)

print()
print("--- 3. resultado")
print("  ok     : %s" % resultado.get("ok"))
print("  etapa  : %s" % resultado.get("stage"))
print("  llamado: %s" % resultado.get("called"))
print("  razon  : %s" % resultado.get("reason"))
if resultado.get("candidate"):
    print("  cand.  : %s" % resultado["candidate"])

prop = resultado.get("proposal")
if prop:
    print()
    print("  accion    : %s" % prop.get("action"))
    print("  aceptada  : %s" % prop.get("accepted"))
    if prop.get("rejection"):
        print("  rechazo   : %s" % prop["rejection"])
    print("  justif.   : %s" % (prop.get("justification") or "")[:240])
    print("  esperado  : %s" % (prop.get("expected_result") or "")[:200])
    print("  modelo    : %s / %s  tokens=%s" % (
        prop.get("provider"), prop.get("model"), prop.get("usage")))
    if prop.get("anchor"):
        print()
        print("  ANCLA (%d car.):" % len(prop["anchor"]))
        for l in prop["anchor"].splitlines()[:6]:
            print("    | %s" % l)
        print("  ocurrencias literales en el skill: %d"
              % contenido_antes.count(prop["anchor"]))
        print("  reemplazo: %d car." % len(prop.get("replacement") or ""))

resp = resultado.get("host_response")
if resp:
    print()
    print("  respuesta del arnes:")
    for k in ("success", "staged", "pending_id", "message"):
        if k in resp:
            print("    %s: %s" % (k, str(resp[k])[:120]))
print("  journal_entry: %s" % resultado.get("journal_entry"))
print("  applied: %s" % resultado.get("applied"))

# --- 4. Verificacion del estado ------------------------------------------------
print()
print("--- 4. el skill y la cola")
contenido_despues = skill_path.read_text(encoding="utf-8")
hash_despues = sha256_text(contenido_despues)
cola_despues = sorted(Path("/home/andrea/.hermes/pending/skills").glob("*.json"))
jornal_despues = journal_health(hermes_home=HOME)

print("  hash antes   : %s" % hash_antes[:32])
print("  hash despues : %s" % hash_despues[:32])
print("  EL SKILL NO SE TOCO : %s" % ("SI" if hash_antes == hash_despues else "NO"))
print("  cola: %d -> %d entradas" % (len(cola_antes), len(cola_despues)))
print("  journal: %d -> %d lineas; revertibles %d"
      % (jornal_antes["entries"], jornal_despues["entries"],
         jornal_despues["revertible_entries"]))
print("  pendientes en el journal: %d" % jornal_despues["pending_entries"])

entradas = read_entries(hermes_home=HOME)
aplicadas = [e for e in entradas if e.status == "applied"]
print("  entradas del journal marcadas como APLICADAS: %d" % len(aplicadas))

# --- 5. La etapa 5, ahora mismo ------------------------------------------------
print()
print("--- 5. la etapa 5, corriendo ahora")
efectos = grade_applied_changes(hermes_home=HOME,
                                skills_dirs=[Path("/home/andrea/developers/ai/skills")])
if not efectos:
    print("  Sin veredictos: no hay cambio aplicado que calificar.")
    print("  (Correcto: el cambio quedo EN COLA, no aplicado.)")
else:
    for e in efectos:
        print("  %s" % e.verdict)

# --- 6. VERIFICACION -----------------------------------------------------------
print()
print("=" * 74)
print("VERIFICACION DEL FIN A FIN")
print("=" * 74)

checks = [
    ("el ciclo encontro el fallo real", bool(informe.recurrence and informe.recurrence.recurring)),
    ("el tool llamo al modelo", resultado.get("called") is True),
    ("el resultado tiene una etapa explicita", bool(resultado.get("stage"))),
    ("el skill NO se modifico", hash_antes == hash_despues),
    ("no se registro como aplicado", resultado.get("applied") is False),
    ("la etapa 5 no invento un veredicto", not efectos),
]
for etiqueta, valor in checks:
    print("  %-44s %s" % (etiqueta, "SI" if valor else "NO"))

# El estado esperado depende de lo que decidio el modelo.
accion = (prop or {}).get("action")
print()
if accion == "no_op":
    print("  EL MODELO DIJO no_op. El ciclo funciono, pero no hay cambio que probar.")
    print("  Probar la rama de aplicacion requiere un skill donde SI corresponda.")
elif accion == "patch" and resp and resp.get("staged"):
    print("  EL CICLO COMPLETO FUNCIONO hasta el gate del arnes:")
    print("  propuesta verificada -> en cola de aprobacion -> skill intacto.")
    print("  Falta un solo paso, y es de Mauro: aprobar en /skills pending.")
    print("  pending_id: %s" % resp.get("pending_id"))
elif accion == "patch" and resp and resp.get("success"):
    print("  SE APLICO de verdad. Se puede medir en 3 dias.")
else:
    print("  Resultado inesperado. accion=%r, respuesta=%r" % (accion, resp))
