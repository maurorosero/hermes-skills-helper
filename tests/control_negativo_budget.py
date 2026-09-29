"""Control negativo: ¿la carrera existe de verdad sin el bloqueo?

Un test de concurrencia que pasa no prueba nada si el escenario no tenía carrera. Acá se
mide la **misma** secuencia leer-decidir-anotar pero sin sostener el bloqueo: si los
procesos se pasan del techo, entonces la ventana existe y el bloqueo de `budget.py` es
lo que la cierra. Si no se pasaran, el test de la etapa 2 no estaría midiendo nada.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROCESOS = 12
TECHO = 3


def _sin_bloqueo(home_str: str, ceiling: int, barrier, out: str) -> None:
    """La secuencia ingenua: leer, decidir, anotar -- sin sección crítica."""
    home = Path(home_str)
    libro = home / "budget.jsonl"
    libro.parent.mkdir(parents=True, exist_ok=True)
    try:
        barrier.wait(timeout=60)

        # Leer
        usados = 0
        if libro.is_file():
            for linea in libro.read_text(encoding="utf-8").splitlines():
                if linea.strip():
                    try:
                        row = json.loads(linea)
                    except ValueError:
                        continue
                    if row.get("state") == "charged":
                        usados += 1

        # Decidir sobre la lectura
        hora = time.time()
        time.sleep(0.01)  # agranda la ventana: es el peor caso, no el típico
        concedido = usados < ceiling

        # Anotar
        if concedido:
            with libro.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "token": f"{os.getpid()}-{hora}",
                    "day": time.strftime("%Y-%m-%d"),
                    "kind": "edit",
                    "state": "charged",
                    "ts": hora,
                }) + "\n")
        payload = {"granted": concedido}
    except Exception as exc:
        payload = {"granted": None, "error": str(exc)}
    with open(out, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


print("=" * 74)
print("CONTROL NEGATIVO — la carrera sin bloqueo, para saber que el test mide algo")
print("=" * 74)

root = Path(tempfile.mkdtemp(prefix="hsh-sin-bloqueo-"))
try:
    home = root / "hermes"
    barrier = mp.Barrier(PROCESOS)
    out = root / "res.jsonl"
    out.write_text("", encoding="utf-8")

    procs = [
        mp.Process(target=_sin_bloqueo, args=(str(home), TECHO, barrier, str(out)))
        for _ in range(PROCESOS)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)

    resultados = [
        json.loads(l)["granted"]
        for l in out.read_text(encoding="utf-8").splitlines() if l.strip()
    ]
    concedidas = len([r for r in resultados if r is True])

    libro = home / "budget.jsonl"
    cargados = 0
    if libro.is_file():
        cargados = len([l for l in libro.read_text(encoding="utf-8").splitlines()
                        if l.strip()])

    print()
    print("  procesos        : %d" % PROCESOS)
    print("  techo           : %d" % TECHO)
    print("  concedidas      : %d" % concedidas)
    print("  cargos en disco : %d" % cargados)
    print()
    print("  ¿se pasó del techo?: %s" % ("SÍ" if cargados > TECHO else "NO"))
    print()
    if cargados > TECHO:
        print("  La ventana EXISTE: %d cargos con el techo en %d (%d de más)."
              % (cargados, TECHO, cargados - TECHO))
        print("  Por lo tanto el bloqueo de budget.py es lo que la cierra, y el test de")
        print("  la etapa 2 mide algo real.")
    else:
        print("  La ventana NO se manifestó en esta corrida. Puede ser suerte de")
        print("  planificación: conviene repetir antes de concluir que el test no mide.")
finally:
    shutil.rmtree(root, ignore_errors=True)
