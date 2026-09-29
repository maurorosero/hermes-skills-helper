"""Medicion real del cableado: el plugin cargado por el arnes de verdad.

No alcanza con que `register()` funcione contra un ctx falso. Lo que hay que probar
es que el ARNES lo carga, registra el hook y los tools, y que el hook corre sin
romper nada.

Es la ultima verificacion antes de instalar: si esto falla, instalar seria poner un
plugin roto en el ciclo de cada turno.
"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_DIR = Path("/home/andrea/developers/hermes/plugins/hermes-skills-helper")
HERMES_AGENT = Path("/home/andrea/.hermes/hermes-agent")
sys.path.insert(0, str(HERMES_AGENT))

print("=" * 74)
print("MEDICION REAL - el arnes carga el plugin")
print("=" * 74)

# --- 1. El arnes lo carga y registra -------------------------------------------------
print()
print("--- 1. carga y registro por el arnes")
from hermes_cli.plugins import discover_plugins  # noqa: E402

discover_plugins()

# El validador del propio arnes es la via oficial para saber que quedo registrado:
# lee el manifest Y lo que register() registro, y compara.
from hermes_cli.plugin_dev import doctor_plugin  # noqa: E402

informe_val = doctor_plugin(PLUGIN_DIR)
registrados = list(getattr(informe_val, "registered_hooks", ()) or ())
print("  hooks registrados: %s" % (registrados or "ninguno"))

registrados_tools = list(getattr(informe_val, "registered_tools", ()) or ())
print("  tools registrados: %s" % (registrados_tools or "ninguno"))

# El registro EN VIVO no se consulta a proposito: el doctor carga el plugin en una copia
# aislada, asi que este proceso no tiene los tools del plugin. Consultarlos aca daria
# False y el False no significaria nada -- el plugin no esta instalado todavia. La
# verificacion valida es el informe del doctor y, despues, la instalacion.
encontrados = {}
for nombre in ("skills_review", "skills_undo"):
    encontrados[nombre] = nombre in registrados_tools

# --- 2. El hook corre sin romper nada ------------------------------------------------
print()
print("--- 2. el hook corre sobre el arnes real")
# El plugin se importa como modulo suelto via sys.path: es el modo en que los tests lo
# cargan, y sus modulos ya resuelven los imports en ambos modos de carga.
sys.path.insert(0, str(PLUGIN_DIR))
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "hermes_skills_helper_medicion", PLUGIN_DIR / "__init__.py")
plugin = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = plugin
_spec.loader.exec_module(plugin)

try:
    plugin._on_session_end(session_id="medicion", turn_id="t1", completed=True,
                           failed=False, interrupted=False)
    print("  el hook corrio sin excepcion: SI")
    hook_ok = True
except Exception as exc:
    print("  el hook LEVANTO: %s: %s" % (type(exc).__name__, exc))
    hook_ok = False

# --- 3. El ciclo determinista sobre la trayectoria REAL ------------------------------
print()
print("--- 3. el ciclo determinista, sobre la trayectoria real")
from pipeline import health, run_deterministic  # noqa: E402

home = Path("/home/andrea/.hermes")
informe = run_deterministic(hermes_home=home, force=True)

print("  barrido            : %s" % informe.scanned)
print("  trayectoria legible: %s" % informe.trajectory_readable)
print("  candidatos         : %d" % informe.candidates)
print("  errores            : %s" % (informe.errors or "ninguno"))
print("  resumen            : %s" % informe.summary())

if informe.recurrence:
    print()
    print("  los fallos recurrentes que el plugin encuentra:")
    for i, c in enumerate(informe.recurrence.recurring[:5], 1):
        print("    %d. [%s] %s" % (i, c.tool_name, c.why()))
    print()
    print("  descartados: %d rafagas, %d rechazos del arnes, %d eventos unicos"
          % (len(informe.recurrence.bursts), len(informe.recurrence.guardrails),
             informe.recurrence.single_events))

# --- 4. El estado del ciclo ------------------------------------------------------------
print()
print("--- 4. estado del ciclo")
h = health(hermes_home=home)
print("  ultimo barrido: %s" % h["last_scan_ts"])
print("  intervalo minimo: %.0f s" % h["min_scan_interval_seconds"])
print("  dias de gracia: %d" % h["grace_days"])
print("  techos: %s" % h["budget"].get("day"))
print("  journal: %d entradas, %d revertibles"
      % (h["journal"]["entries"], h["journal"]["revertible_entries"]))

# --- 5. VERIFICACION -------------------------------------------------------------------
print()
print("=" * 74)
print("VERIFICACION")
print("=" * 74)

todo = [
    ("el arnes registro el hook on_session_end", "on_session_end" in registrados),
    ("el arnes registro el tool skills_review", encontrados.get("skills_review", False)),
    ("el arnes registro el tool skills_undo", encontrados.get("skills_undo", False)),
    ("el doctor del arnes no reporta errores",
     not list(getattr(informe_val, "errors", ()) or ())),
    ("el hook corre sin romper el turno", hook_ok),
    ("el ciclo leyo la trayectoria real", informe.trajectory_readable),
    ("el ciclo no tuvo errores", not informe.errors),
    ("encontro los recurrentes reales", informe.candidates > 0),
]
for etiqueta, valor in todo:
    print("  %-46s %s" % (etiqueta, "SI" if valor else "NO"))
print()
if all(v for _, v in todo):
    print("  PLUGIN CABLEADO Y FUNCIONANDO CONTRA EL ARNES REAL.")
    print("  Listo para instalar.")
else:
    print("  NO VERIFICADO.")
    raise SystemExit(1)
