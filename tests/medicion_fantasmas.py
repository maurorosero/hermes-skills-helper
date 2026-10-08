"""Medición real (no test): los fantasmas del recolector, contados contra el arnés real.

Mide contra el arnés de Mauro, no contra fixtures. Se corre a mano y se lee el resultado.

Los fantasmas se cuentan **dos veces** y las dos cifras importan, porque no son lo mismo:

    solo activos (+/- 0)   lo que el reporte puede dar de baja. Es la cifra accionable.
    con archivados         incluye entradas cuyo único SKILL.md vive en .archive/, que no
                           es una fuente del plugin. Ahí ``ruta = None`` es correcto: el
                           skill está archivado, no perdido.

El estado final de T-0021 son **0 fantasmas activos**: los 2 reales se resolvieron
sumando fuentes, y los 3 muertos salieron de ``.usage.json``.

Uso:
    python3 tests/medicion_fantasmas.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

_spec = importlib.util.spec_from_file_location(
    "hermes_skills_helper", RAIZ / "__init__.py", submodule_search_locations=[str(RAIZ)])
plugin = importlib.util.module_from_spec(_spec)
sys.modules["hermes_skills_helper"] = plugin
assert _spec is not None and _spec.loader is not None
_spec.loader.exec_module(plugin)

from hermes_skills_helper import uso as umod  # noqa: E402

from hermes_constants import get_default_hermes_root  # noqa: E402

HOGAR = get_default_hermes_root()


def _fantasmas(dirs, *, incluir_archivados: bool):
    rep = umod.medir(hermes_home=HOGAR, skills_dirs=list(dirs), incluir_archivados=incluir_archivados)
    return rep, sorted(s.nombre for s in rep.skills if s.ruta is None)


def main() -> int:
    dirs = plugin._skills_dirs(HOGAR)
    print("=== FUENTES QUE EL PLUGIN MIRA ===")
    for d in dirs:
        print(f"   {'OK ' if d.is_dir() else 'NO '} {d}")
    print()

    rep, activos = _fantasmas(dirs, incluir_archivados=False)
    print(f"=== FANTASMAS, SOLO ACTIVOS ({len(activos)}) ===")
    for n in activos:
        print("   ", n)
    print(f"    (activos medidos: {rep.total_activos})")
    print()

    rep_t, todos = _fantasmas(dirs, incluir_archivados=True)
    print(f"=== SIN RUTA CONTANDO ARCHIVADOS ({len(todos)}) — incluye .archive/, que no es fuente ===")
    for n in todos:
        print("   ", n)
    print()

    esperados: set[str] = set()
    if set(activos) == esperados:
        print("OK: 0 fantasmas activos — los 2 reales los resolvieron las fuentes nuevas")
        print("    y los 3 muertos salieron del registro.")
        return 0
    print("NO: se esperaba", sorted(esperados), "y hay", activos)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
