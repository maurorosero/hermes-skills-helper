"""Tests de la etapa 2 — techo de presupuesto.

El criterio de cierre del diseño:

    "con el techo agotado, ninguna llamada al modelo ocurre"

Y el requisito que el aviso heredado pone en primer plano: **la carrera debe resolverse
con bloqueo, no ignorarse**. Así que estos tests no verifican que el contador cuente:
verifican que **dos procesos concurrentes no puedan pasarse del techo**.

El test de concurrencia lanza procesos reales (no hilos): la carrera que importa es entre
canales distintos del arnés, que son procesos distintos.
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
    CHARGED,
    KIND_EDIT,
    KIND_MODEL_RUN,
    RELEASED,
    ceilings_from_config,
    health,
    iter_rows,
    release,
    reserve,
    status,
    today,
)

PASSED = 0
FAILED: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ✓ {label}")
    else:
        FAILED.append(label)
        print(f"  ✗ {label}  {detail}")


class Sandbox:
    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="hsh-budget-"))
        self.home = self.root / "hermes"

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


# -- El trabajador de la prueba de concurrencia --------------------------------------
# Debe ser de nivel de módulo para poder usarse con multiprocessing (spawn).

def _reserve_worker(home_str: str, kind: str, ceiling: int, barrier, results_path: str) -> None:
    """Pide una reserva y anota si se la concedieron. Todos arrancan a la vez."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from budget import reserve as _reserve  # noqa: PLC0415

    home = Path(home_str)
    try:
        barrier.wait(timeout=30)  # maximizar la superposición
        result = _reserve(kind, hermes_home=home, max_edits_per_day=ceiling,
                          max_model_runs_per_day=ceiling)
        granted = result.granted
    except Exception as exc:  # pragma: no cover - solo diagnóstico
        granted = f"error: {exc}"
    with open(results_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"granted": granted}) + "\n")


def test_ceiling_blocks_when_exhausted() -> None:
    print("\ncon el techo agotado no se concede nada más")

    box = Sandbox()
    try:
        concedidas = []
        for i in range(5):
            r = reserve(KIND_EDIT, hermes_home=box.home, max_edits_per_day=3)
            concedidas.append(r.granted)
        check("las 3 primeras se conceden", concedidas[:3] == [True, True, True],
              f"{concedidas}")
        check("la 4ª y la 5ª se niegan", concedidas[3:] == [False, False], f"{concedidas}")

        r4 = reserve(KIND_EDIT, hermes_home=box.home, max_edits_per_day=3)
        check("la razón nombra el techo de cambios",
              "cambios" in r4.reason and "3/3" in r4.reason, r4.reason)
        check("declara usados y techo", r4.used == 3 and r4.ceiling == 3)
        check("no queda lugar", r4.remaining == 0)
        check("una reserva denegada NO devuelve token", r4.token is None)

        s = status(hermes_home=box.home, max_edits_per_day=3)
        check("el estado confirma el techo agotado",
              s.edits_used == 3 and s.has_edits_left is False)
        check("el otro techo no se tocó",
              s.model_runs_used == 0 and s.has_model_runs_left is True, s.summary())
    finally:
        box.cleanup()


def test_two_ceilings_are_independent() -> None:
    print("\nlos dos techos son independientes")

    box = Sandbox()
    try:
        for _ in range(3):
            reserve(KIND_EDIT, hermes_home=box.home, max_edits_per_day=3,
                    max_model_runs_per_day=10)
        check("cambios agotados",
              reserve(KIND_EDIT, hermes_home=box.home, max_edits_per_day=3).granted is False)
        check("pero todavía hay llamadas al modelo",
              reserve(KIND_MODEL_RUN, hermes_home=box.home, max_edits_per_day=3,
                      max_model_runs_per_day=10).granted is True)

        s = status(hermes_home=box.home, max_edits_per_day=3, max_model_runs_per_day=10)
        check("el estado lo refleja por separado",
              s.edits_used == 3 and s.model_runs_used == 1, s.summary())
    finally:
        box.cleanup()


def test_release_returns_the_slot() -> None:
    print("\nuna llamada que falló devuelve su lugar")

    box = Sandbox()
    try:
        r1 = reserve(KIND_MODEL_RUN, hermes_home=box.home, max_model_runs_per_day=2)
        r2 = reserve(KIND_MODEL_RUN, hermes_home=box.home, max_model_runs_per_day=2)
        check("dos reservas con techo 2", r1.granted and r2.granted)
        check("la tercera se niega",
              reserve(KIND_MODEL_RUN, hermes_home=box.home,
                      max_model_runs_per_day=2).granted is False)

        check("liberar la primera funciona", release(r1.token, hermes_home=box.home) is True)
        s = status(hermes_home=box.home, max_model_runs_per_day=2)
        check("el contador bajó", s.model_runs_used == 1, s.summary())

        r3 = reserve(KIND_MODEL_RUN, hermes_home=box.home, max_model_runs_per_day=2)
        check("ahora sí hay lugar de nuevo", r3.granted is True, r3.reason)

        check("liberar dos veces el mismo token no infla el libro",
              release(r1.token, hermes_home=box.home) is False)
        check("liberar un token inexistente devuelve False",
              release("no-existe", hermes_home=box.home) is False)
        check("token vacío devuelve False", release("", hermes_home=box.home) is False)
    finally:
        box.cleanup()


def test_release_is_append_only() -> None:
    print("\nliberar agrega una marca, no reescribe la reserva")

    box = Sandbox()
    try:
        r = reserve(KIND_MODEL_RUN, hermes_home=box.home, max_model_runs_per_day=5)
        antes = len(list(iter_rows(hermes_home=box.home)))
        release(r.token, hermes_home=box.home)
        despues = list(iter_rows(hermes_home=box.home))

        check("el libro creció", len(despues) > antes, f"{antes} → {len(despues)}")
        check("la reserva original sigue ahí con estado charged",
              any(row["token"] == r.token and row["state"] == CHARGED for row in despues))
        check("y también la marca de liberación",
              any(row["token"] == r.token and row["state"] == RELEASED for row in despues))

        h = health(hermes_home=box.home)
        check("el diagnóstico cuenta 1 reserva, no 2",
              h["reservations"] == 1, f"{h['reservations']}")
    finally:
        box.cleanup()


def test_yesterdays_charges_do_not_count() -> None:
    print("\nel techo es de hoy: ayer no consume")

    box = Sandbox()
    try:
        ayer = time.time() - 86400
        for _ in range(3):
            reserve(KIND_EDIT, hermes_home=box.home, max_edits_per_day=3, now=ayer)

        s_ayer = status(hermes_home=box.home, max_edits_per_day=3, now=ayer)
        check("ayer quedaron 3 cargados", s_ayer.edits_used == 3, s_ayer.summary())

        s_hoy = status(hermes_home=box.home, max_edits_per_day=3)
        check("hoy el contador arranca en 0", s_hoy.edits_used == 0, s_hoy.summary())
        check("y hay lugar otra vez", s_hoy.has_edits_left is True)
        check("las reservas viejas siguen en el libro",
              len(list(iter_rows(hermes_home=box.home))) == 3)
    finally:
        box.cleanup()


def test_unreadable_row_counts_as_used() -> None:
    print("\nuna línea ilegible no puede tapar un gasto")

    box = Sandbox()
    try:
        reserve(KIND_EDIT, hermes_home=box.home, max_edits_per_day=2)
        path = Path(health(hermes_home=box.home)["ledger_path"])
        with path.open("a", encoding="utf-8") as handle:
            handle.write("{esto no es json\n")

        s = status(hermes_home=box.home, max_edits_per_day=2)
        check("se reporta la línea ilegible", s.unreadable_rows == 1, s.summary())
        check("cuenta como usada en el techo de cambios",
              s.edits_used == 2, s.summary())
        check("y también en el de llamadas (dirección conservadora)",
              s.model_runs_used == 1, s.summary())
        check("el resumen lo declara",
              "ilegible" in s.summary(), s.summary())

        r = reserve(KIND_EDIT, hermes_home=box.home, max_edits_per_day=2)
        check("con lo ilegible, el techo ya está agotado", r.granted is False, r.reason)
        check("la razón lo explica", "ilegible" in r.reason, r.reason)
    finally:
        box.cleanup()


def test_corrupt_line_does_not_deadlock_forever() -> None:
    print("\nun byte raro cuesta un lugar hoy, no la función para siempre")

    box = Sandbox()
    try:
        path = box.home / "plugins" / "hermes-skills-helper"
        path.mkdir(parents=True, exist_ok=True)
        (path / "budget.jsonl").write_text("basura ilegible\n", encoding="utf-8")

        hoy = status(hermes_home=box.home, max_edits_per_day=3)
        check("hoy el techo está tocado", hoy.edits_used == 1, hoy.summary())

        manana = status(hermes_home=box.home, max_edits_per_day=3,
                        now=time.time() + 86400)
        check("mañana arranca limpio sin borrar nada", manana.edits_used == 0,
              manana.summary())
        check("la línea ilegible sigue en el libro (la evidencia no se borra)",
              "basura" in (path / "budget.jsonl").read_text(encoding="utf-8"))
    finally:
        box.cleanup()


def test_race_is_closed_with_real_processes() -> None:
    print("\nLA CARRERA: procesos reales no pueden pasarse del techo")

    box = Sandbox()
    try:
        techo = 3
        procesos = 8
        barrier = mp.Barrier(procesos)
        results_path = box.root / "results.jsonl"
        results_path.write_text("", encoding="utf-8")

        workers = [
            mp.Process(
                target=_reserve_worker,
                args=(str(box.home), KIND_EDIT, techo, barrier, str(results_path)),
            )
            for _ in range(procesos)
        ]
        for w in workers:
            w.start()
        for w in workers:
            w.join(timeout=60)

        lineas = [l for l in results_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        resultados = [json.loads(l)["granted"] for l in lineas]
        concedidas = [r for r in resultados if r is True]
        negadas = [r for r in resultados if r is False]
        errores = [r for r in resultados if not isinstance(r, bool)]

        check("todos los procesos terminaron", len(resultados) == procesos,
              f"{len(resultados)}/{procesos}")
        check("ninguno falló", not errores, f"{errores}")
        check(f"con techo {techo} y {procesos} procesos, se conceden EXACTAMENTE {techo}",
              len(concedidas) == techo, f"concedidas={len(concedidas)}")
        check("el resto se niega", len(negadas) == procesos - techo,
              f"negadas={len(negadas)}")

        s = status(hermes_home=box.home, max_edits_per_day=techo)
        check("el libro registra exactamente el techo, ni uno más",
              s.edits_used == techo, s.summary())
        filas = [r for r in iter_rows(hermes_home=box.home) if r["state"] == CHARGED]
        check("no hay cargos fantasma en el libro", len(filas) == techo, f"{len(filas)}")
    finally:
        box.cleanup()


def test_second_channel_race_mixed_kinds() -> None:
    print("\nla carrera con los dos tipos de cargo a la vez")

    box = Sandbox()
    try:
        procesos = 6
        techo = 2
        barrier = mp.Barrier(procesos)
        results_path = box.root / "both.jsonl"
        results_path.write_text("", encoding="utf-8")

        workers = []
        for i in range(procesos):
            kind = KIND_EDIT if i % 2 == 0 else KIND_MODEL_RUN
            workers.append(
                mp.Process(
                    target=_reserve_worker,
                    args=(str(box.home), kind, techo, barrier, str(results_path)),
                )
            )
        for w in workers:
            w.start()
        for w in workers:
            w.join(timeout=60)

        resultados = [
            json.loads(l)["granted"]
            for l in results_path.read_text(encoding="utf-8").splitlines() if l.strip()
        ]
        s = status(hermes_home=box.home, max_edits_per_day=techo,
                   max_model_runs_per_day=techo)
        check("cada techo respetó su límite por separado",
              s.edits_used <= techo and s.model_runs_used <= techo, s.summary())
        check("se concedieron como máximo 2 de cada tipo",
              len([r for r in resultados if r is True]) <= 2 * techo)
    finally:
        box.cleanup()


def test_unknown_kind_is_rejected() -> None:
    print("\nun tipo de cargo desconocido se rechaza")

    box = Sandbox()
    try:
        try:
            reserve("inventado", hermes_home=box.home)
            check("debería haber levantado ValueError", False)
        except ValueError as exc:
            check("levanta ValueError", True)
            check("la razón lista los válidos", "edit" in str(exc), str(exc))
    finally:
        box.cleanup()


def test_ceilings_from_config() -> None:
    print("\nlos techos se leen de la configuración, con los defectos como piso")

    class Ctx:
        def __init__(self, values: dict) -> None:
            self.values = values

        def get_config(self, key: str, default=None):
            return self.values.get(key, default)

    edits, runs = ceilings_from_config(Ctx({}))
    check("sin configuración usa 3 y 30", (edits, runs) == (3, 30), f"{edits},{runs}")

    edits, runs = ceilings_from_config(Ctx({"max_edits_per_day": 7,
                                            "max_model_runs_per_day": 100}))
    check("respeta los valores configurados", (edits, runs) == (7, 100), f"{edits},{runs}")

    edits, runs = ceilings_from_config(Ctx({"max_edits_per_day": 0}))
    check("un 0 no apaga el aprendizaje: cae al defecto", edits == 3, f"{edits}")
    edits, runs = ceilings_from_config(Ctx({"max_edits_per_day": -5}))
    check("un negativo tampoco", edits == 3, f"{edits}")
    edits, runs = ceilings_from_config(Ctx({"max_edits_per_day": "no es numero"}))
    check("un valor no numérico cae al defecto", edits == 3, f"{edits}")

    edits, runs = ceilings_from_config(Ctx({"max_edits_per_day": "6"}))
    check("un número como texto se acepta", edits == 6, f"{edits}")

    class SinGetter:
        pass

    check("un ctx sin get_config no rompe",
          ceilings_from_config(SinGetter()) == (3, 30))


def test_health_does_not_create() -> None:
    print("\nel diagnóstico no inventa su propia evidencia")

    box = Sandbox()
    try:
        h = health(hermes_home=box.home)
        check("informa que el libro no existe", h["exists"] is False)
        check("y NO lo creó al consultarlo", not Path(h["ledger_path"]).exists())
        check("reporta cero reservas", h["reservations"] == 0)
    finally:
        box.cleanup()


def test_today_format() -> None:
    print("\nel día se guarda como etiqueta, no se deriva de restas")

    check("formato AAAA-MM-DD", len(today()) == 10 and today().count("-") == 2, today())
    check("es determinista con el mismo instante",
          today(now=1700000000) == today(now=1700000000))
    check("es la fecha local", today() == time.strftime("%Y-%m-%d"))


def main() -> int:
    print("=" * 68)
    print("hermes-skills-helper — etapa 2 (techo de presupuesto)")
    print("=" * 68)

    for fn in (
        test_ceiling_blocks_when_exhausted,
        test_two_ceilings_are_independent,
        test_release_returns_the_slot,
        test_release_is_append_only,
        test_yesterdays_charges_do_not_count,
        test_unreadable_row_counts_as_used,
        test_corrupt_line_does_not_deadlock_forever,
        test_unknown_kind_is_rejected,
        test_ceilings_from_config,
        test_health_does_not_create,
        test_today_format,
        test_race_is_closed_with_real_processes,
        test_second_channel_race_mixed_kinds,
    ):
        fn()

    print("\n" + "=" * 68)
    print(f"{PASSED} pasaron, {len(FAILED)} fallaron")
    if FAILED:
        for name in FAILED:
            print(f"  FALLO: {name}")
        return 1
    print("todo verde")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
