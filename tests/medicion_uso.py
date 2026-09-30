"""Medición real de la señal de uso contra el arnés de Mauro.

No es un test: lee el `.usage.json` real y muestra el reparto. Sirve para (a) verificar
que el módulo funciona contra datos que no escribimos nosotros, y (b) dejar registrado el
estado del catálogo en el momento de la medición.

Uso:
    python3 tests/medicion_uso.py
"""

from __future__ import annotations

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import uso  # noqa: E402

HOGAR = Path("/home/andrea/.hermes")
DIRS = [HOGAR / "skills", Path("/home/andrea/developers/ai/skills")]


def main() -> int:
    report = uso.medir(hermes_home=HOGAR, skills_dirs=DIRS)

    print("=== SEÑAL DE USO — medición sobre el arnés real ===")
    print(uso.resumen(report))
    print(f"confiable: {report.trustworthy}  ·  sin registro: {report.sin_registro}")
    print()

    cubos = [
        ("SIN USAR — creados y nunca usados", report.sin_usar),
        ("SIN CAMBIO — se usan mucho, 0 parches", report.sin_cambio),
        ("SIN REUSO — parcheados y no reusados", report.sin_reuso),
        ("HINCHADOS — desgaste acumulado", report.hinchado),
    ]
    for etiqueta, items in cubos:
        print(f"=== {etiqueta}: {len(items)}")
        for s in sorted(items, key=lambda x: -(x.use_count or 0))[:8]:
            tam = f"{s.tamano_bytes / 1024:5.1f} KB" if s.tamano_bytes else "    ?    "
            print(f"   {s.nombre[:36]:<36} usos={str(s.use_count):<5} "
                  f"parches={str(s.patch_count):<5} {tam}")
            print(f"      {s.motivo}")
        print()

    total = sum(len(c[1]) for c in cubos)
    print(f"=== TOTAL CON SEÑAL: {total} de {report.total_activos} activos")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
