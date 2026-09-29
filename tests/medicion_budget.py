"""Medición real: la etapa 2 bajo concurrencia, en el hogar real del arnés.

Criterio de cierre del diseño:

    "con el techo agotado, ninguna llamada al modelo ocurre"

Y la parte que el aviso heredado pone primero: **la carrera debe resolverse con bloqueo**.
Este script la mide con procesos reales peleando por el mismo techo, y con el libro
viviendo en una ruta realista (bajo un hogar de arnés, no un temporal plano).

No toca nada del arnés: usa un hogar propio y desechable.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from budget import (  # noqa: E402
    KIND_EDIT,
    KIND_MODEL_RUN,
    health,
    iter_rows,
    release,
    reserve,
    status,
)

PROCESOS = 12
TECHO = 3


def _peleador(home_str: str, kind: str, ceiling: int, barrier, out: str) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from budget import reserve as _reserve  # noqa: PLC0415

    try:
        barrier.wait(timeout=60)
        r = _reserve(kind, hermes_home=Path(home_str), max_edits_per_day=ceiling,
                     max_model_runs_per_day=ceiling)
        payload = {"granted": r.granted, "pid": __import__("os").getpid()}
    except Exception as exc:
        payload = {"granted": None, "error": str(exc)}
    with open(out, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


print("=" * 74)
print("MEDICIÓN REAL — etapa 2 bajo concurrencia")
print("=" * 74)

root = Path(tempfile.mkdtemp(prefix="hsh-medicion-budget-"))
try:
    # Hogar realista: la ruta donde el plugin viviría de verdad.
    home = root / "hermes"
    ledger = home / "plugins" / "hermes-skills-helper" / "budget.jsonl"
    print()
    print("  hogar  : %s" % home)
    print("  libro  : %s" % ledger.relative_to(home))

    # --- 1. La carrera --------------------------------------------------------------
    print()
    print("--- 1. LA CARRERA: %d procesos pidiendo %d lugares a la vez" % (PROCESOS, TECHO))
    barrier = mp.Barrier(PROCESOS)
    out = root / "resultados.jsonl"
    out.write_text("", encoding="utf-8")

    procs = [
        mp.Process(target=_peleador,
                   args=(str(home), KIND_EDIT, TECHO, barrier, str(out)))
        for _ in range(PROCESOS)
    ]
    inicio = time.time()
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)
    duracion = time.time() - inicio

    resultados = [
        json.loads(l) for l in out.read_text(encoding="utf-8").splitlines() if l.strip()
    ]
    concedidas = [r for r in resultados if r.get("granted") is True]
    negadas = [r for r in resultados if r.get("granted") is False]
    errores = [r for r in resultados if r.get("granted") is None]

    print("  procesos que respondieron  : %d/%d" % (len(resultados), PROCESOS))
    print("  concedidas                 : %d" % len(concedidas))
    print("  negadas                    : %d" % len(negadas))
    print("  errores                    : %d" % len(errores))
    print("  duración                   : %.2f s" % duracion)

    cargados = [r for r in iter_rows(hermes_home=home)
                if r.get("state") == "charged" and r.get("kind") == KIND_EDIT]
    print("  cargos en el libro         : %d" % len(cargados))

    # --- 2. Los techos son independientes -------------------------------------------
    print()
    print("--- 2. techos independientes")
    s = status(hermes_home=home, max_edits_per_day=TECHO, max_model_runs_per_day=30)
    print("  después de la carrera: %s" % s.summary())
    r_run = reserve(KIND_MODEL_RUN, hermes_home=home, max_edits_per_day=TECHO,
                    max_model_runs_per_day=30)
    print("  con los cambios agotados, una llamada al modelo: %s"
          % ("concedida" if r_run.granted else "negada"))
    r_edit = reserve(KIND_EDIT, hermes_home=home, max_edits_per_day=TECHO,
                     max_model_runs_per_day=30)
    print("  un cambio más: %s" % ("concedido" if r_edit.granted else "negado"))
    print("  razón del rechazo: %s" % r_edit.reason)

    # --- 3. Liberar devuelve el lugar -----------------------------------------------
    print()
    print("--- 3. una llamada que falló devuelve su lugar")
    print("  antes : %s" % status(hermes_home=home, max_model_runs_per_day=30).summary())
    liberado = release(r_run.token, hermes_home=home, reason="medición: falló la llamada")
    print("  liberar: %s" % ("sí" if liberado else "no"))
    print("  después: %s" % status(hermes_home=home, max_model_runs_per_day=30).summary())

    # --- 4. Diagnóstico --------------------------------------------------------------
    print()
    h = health(hermes_home=home)
    print("--- 4. diagnóstico")
    print("  líneas en el libro   : %d" % h["rows"])
    print("  reservas (tokens)    : %d" % h["reservations"])
    print("  cargadas hoy         : %d" % h["charged_today"])
    print("  liberadas            : %d" % h["released"])
    print("  líneas ilegibles     : %d" % h["unreadable_rows"])

    # --- 5. VERIFICACIÓN -------------------------------------------------------------
    print()
    print("=" * 74)
    print("VERIFICACIÓN")
    print("=" * 74)

    ok_exactas = len(concedidas) == TECHO
    ok_libro = len(cargados) == TECHO
    ok_sin_error = not errores
    ok_edit_negado = r_edit.granted is False
    ok_run_concedido = r_run.granted is True
    ok_release = liberado is True
    ok_independientes = s.edits_used == TECHO and s.model_runs_used == 0

    print("  %d procesos, %d concesiones EXACTAS (ni una más) : %s"
          % (PROCESOS, TECHO, "SÍ" if ok_exactas else "NO"))
    print("  el libro registra exactamente el techo          : %s"
          % ("SÍ" if ok_libro else "NO"))
    print("  ningún proceso falló                            : %s"
          % ("SÍ" if ok_sin_error else "NO"))
    print("  con el techo agotado, el cambio se niega        : %s"
          % ("SÍ" if ok_edit_negado else "NO"))
    print("  los techos son independientes                   : %s"
          % ("SÍ" if ok_independientes else "NO"))
    print("  una llamada al modelo sigue permitida           : %s"
          % ("SÍ" if ok_run_concedido else "NO"))
    print("  liberar devuelve el lugar                       : %s"
          % ("SÍ" if ok_release else "NO"))

    print()
    if all([ok_exactas, ok_libro, ok_sin_error, ok_edit_negado, ok_run_concedido,
            ok_release, ok_independientes]):
        print("  CARRERA CERRADA Y TECHO RESPETADO bajo %d procesos concurrentes." % PROCESOS)
    else:
        print("  NO VERIFICADO. La etapa 2 no está autorizada a controlar el gasto.")
        raise SystemExit(1)
finally:
    shutil.rmtree(root, ignore_errors=True)
