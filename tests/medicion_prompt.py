"""Medicion: cuanto se desperdiciaba en el prompt, sobre el candidato REAL.

Sin tocar nada de la trayectoria: lee el candidato vivo de la etapa 1 y compara el prompt
que se armaba antes con el que se arma ahora.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from proposal import MAX_SAMPLES, build_prompt  # noqa: E402
from recurrence import detect  # noqa: E402
from trajectory import scan_failures  # noqa: E402

STATE_DB = Path("/home/andrea/.hermes/state.db")


def _viejo_comportamiento(failure, muestras):
    """Como se armaba el prompt antes: tomar las primeras N sin mirar si se repiten."""
    crudas = [m for m in muestras if m][:MAX_SAMPLES]
    partes = [
        "## Fallo que se repite",
        f"Herramienta: {failure.tool_name}",
        f"Apariciones: {failure.occurrences} en {failure.sessions} sesiones distintas",
    ]
    for i, m in enumerate(crudas, 1):
        partes.append(f"Muestra {i}: {m}")
    return "\n".join(partes)


def main() -> int:
    if not STATE_DB.exists():
        print("no hay state.db: nada que medir")
        return 0

    rep = detect(state_db=STATE_DB)
    if not rep.recurring:
        print("no hay candidatos vivos hoy: nada que medir")
        return 0

    scan = scan_failures(state_db=STATE_DB)
    grupos: dict = defaultdict(list)
    for f in scan.failures:
        grupos[f.fingerprint].append(f)

    print("=" * 78)
    print("PROMPT DE LA ETAPA 3 — cuanto se desperdiciaba")
    print("=" * 78)

    total_antes = 0
    total_ahora = 0
    redundantes_total = 0

    for i, cand in enumerate(rep.recurring, 1):
        grupo = sorted(grupos.get(cand.fingerprint, []), key=lambda f: f.ts)
        muestras = [f.sample for f in grupo if f.sample]

        texto_viejo = _viejo_comportamiento(cand, muestras)
        bloques = build_prompt(
            failure=cand, skill_name="(skill)", skill_content="# skill\n",
            samples=muestras,
        )
        texto_nuevo = bloques[0]["text"]

        # Cuantas de las muestras elegidas eran copias
        elegidas_viejas = muestras[:MAX_SAMPLES]
        unicas = len({m.strip() for m in elegidas_viejas})
        redundantes = len(elegidas_viejas) - unicas

        total_antes += len(texto_viejo)
        total_ahora += len(texto_nuevo)
        redundantes_total += redundantes

        print(f"\n[{i}] {cand.tool_name} — {cand.occurrences} apariciones")
        print(f"    fallo: {cand.sample[:64]}")
        print(f"    muestras elegidas : {len(elegidas_viejas)}")
        print(f"    distintas         : {unicas}")
        print(f"    COPIAS desperdiciadas: {redundantes}")

    print()
    print("-" * 78)
    print(f"caracteres con el criterio viejo : {total_antes}")
    print(f"caracteres ahora                 : {total_ahora}")
    ahorro = total_antes - total_ahora
    pct = (100.0 * ahorro / total_antes) if total_antes else 0.0
    print(f"ahorro                           : {ahorro} ({pct:.0f}%)")
    print(f"muestras que eran copias         : {redundantes_total}")
    print("-" * 78)
    print()
    print("  El ahorro no es lo importante: lo importante es que la evidencia que")
    print("  ocupa el lugar ahora es evidencia DISTINTA. Antes el modelo veia tres")
    print("  veces la misma linea y creia tener tres datos.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
