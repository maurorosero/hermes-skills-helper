"""Tests de la etapa 1 — recurrencia.

Criterio de cierre del diseño: *"reproducir la detección sobre trayectoria real y comprobar
que el umbral descarta el ruido de un solo evento"*.

Estos tests fijan tres cosas que deciden si el filtro sirve o estorba:

1. **El ruido de un solo evento se descarta.** Un error que pasó una vez no justifica un
   cambio permanente.
2. **Un bucle dentro de una sesión no es un patrón.** Diez apariciones en una sesión no
   valen lo mismo que dos en dos sesiones — por eso hay dos criterios, no uno.
3. **La ventana recorta.** Un fallo frecuente pero viejo no es recurrente hoy.

Sin dependencias externas: ``python3 tests/test_recurrence.py``.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from effect.fingerprint import fingerprint  # noqa: E402
from recurrence import (  # noqa: E402
    DEFAULT_WINDOW_DAYS,
    MIN_OCCURRENCES,
    MIN_SESSIONS,
    MIN_SPAN_DAYS,
    detect,
    find_recurring,
    group_failures,
    is_guardrail,
    is_recurring,
)
from trajectory import (  # noqa: E402
    DEFAULT_ROW_LIMIT,
    Failure,
    TrajectoryScan,
    scan_failures,
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


def _failure(tool: str, msg: str, ts: float, session: str) -> Failure:
    return Failure(
        tool_name=tool,
        fingerprint=fingerprint(tool, msg),
        ts=ts,
        session_id=session,
        sample=msg[:200],
    )


def _scan(failures: list[Failure], *, until: float, discarded: int = 0) -> TrajectoryScan:
    return TrajectoryScan(
        failures=tuple(sorted(failures, key=lambda f: f.ts)),
        rows_scanned=len(failures) + discarded,
        discarded_ts=discarded,
        scanned_until=until,
    )


# -- Umbral --------------------------------------------------------------------------

def test_threshold() -> None:
    print("\numbral")

    check("1 aparición, 1 sesión → no recurrente",
          is_recurring(occurrences=1, sessions=1) is False)
    check("2 apariciones, 1 sesión → no (bucle, no patrón)",
          is_recurring(occurrences=2, sessions=1) is False)
    check("3 apariciones, 1 sesión → sí (cuenta)",
          is_recurring(occurrences=3, sessions=1) is True)
    check("1 aparición en 2 sesiones → sí (cruza sesiones)",
          is_recurring(occurrences=1, sessions=2) is True)
    check("2 apariciones en 2 sesiones → sí",
          is_recurring(occurrences=2, sessions=2) is True)
    check("cero apariciones → no", is_recurring(occurrences=0, sessions=0) is False)

    check("los umbrales por defecto son 3 y 2",
          (MIN_OCCURRENCES, MIN_SESSIONS) == (3, 2))


# -- Agrupación ----------------------------------------------------------------------

def test_grouping() -> None:
    print("\nagrupación por huella")

    now = time.time()
    fs = [
        _failure("terminal", "HTTP 500 for /a/1", now - 300, "s1"),
        _failure("terminal", "HTTP 500 for /b/2", now - 200, "s1"),   # misma forma
        _failure("terminal", "disk full", now - 100, "s1"),            # otra forma
    ]
    groups = group_failures(tuple(fs))
    check("dos formas → dos grupos", len(groups) == 2, f"{len(groups)}")
    largest = max(groups.values(), key=len)
    check("ids distintos agrupan juntos", len(largest) == 2)
    check("orden temporal dentro del grupo",
          [f.ts for f in largest] == sorted(f.ts for f in largest))


# -- Los tres casos que deciden el filtro --------------------------------------------

def test_single_event_discarded() -> None:
    print("\ncaso 1 — el ruido de un solo evento se descarta")

    now = time.time()
    scan = _scan([_failure("terminal", "unique failure xyz", now - 3600, "s1")], until=now)
    report = find_recurring(scan=scan)

    check("sin recurrentes", report.has_recurrence is False)
    check("se contó como evento único", report.single_events == 1)
    check("la ventana se considera confiable", report.trustworthy is True)


def test_loop_in_one_session_is_not_a_pattern() -> None:
    print("\ncaso 2 — un bucle en una sesión no es un patrón")

    now = time.time()
    # Diez apariciones del mismo error, todas en la MISMA sesión, repartidas en 10 días
    # para que el span no las descalifique por ráfaga (eso se prueba aparte).
    fs = [
        _failure("terminal", "retry failed connection", now - 86400 * (10 - i), "s1")
        for i in range(10)
    ]
    report = find_recurring(scan=_scan(fs, until=now))

    check("10 apariciones en 1 sesión SÍ cruzan el umbral de cuenta",
          report.has_recurrence is True,
          "el criterio de cuenta las acepta a propósito")
    if report.recurring:
        r = report.recurring[0]
        check("se reporta 1 sesión, no varias", r.sessions == 1, f"sessions={r.sessions}")
        check("la razón nombra la cuenta, no las sesiones",
              "apariciones" in r.why(), r.why())

    # El mismo error, pero en dos sesiones: ahí sí es un patrón.
    fs2 = [
        _failure("terminal", "retry failed connection", now - 86400 * 6, "s1"),
        _failure("terminal", "retry failed connection", now - 86400 * 3, "s2"),
        _failure("terminal", "retry failed connection", now - 86400 * 1, "s2"),
    ]
    report2 = find_recurring(scan=_scan(fs2, until=now))
    check("3 apariciones en 2 sesiones → recurrente", report2.has_recurrence is True)
    if report2.recurring:
        check("se reportan 2 sesiones",
              report2.recurring[0].sessions == 2, f"{report2.recurring[0].sessions}")


def test_window_excludes_old_failures() -> None:
    print("\ncaso 3 — la ventana recorta lo viejo")

    now = time.time()
    # Frecuente, pero hace 90 días: fuera de la ventana por defecto (30).
    viejo = [
        _failure("terminal", "old failure abc", now - 86400 * (90 - i * 5), f"s{i}")
        for i in range(5)
    ]
    report = find_recurring(scan=_scan(viejo, until=now))
    check("fallo frecuente pero de hace 90 días → descartado",
          report.has_recurrence is False,
          f"recurrentes={len(report.recurring)}")

    # El mismo conjunto con una ventana amplia: ahora sí.
    report2 = find_recurring(scan=_scan(viejo, until=now), window_days=120)
    check("con ventana de 120 días → reconocido", report2.has_recurrence is True)

    check("la ventana por defecto es 30 días", DEFAULT_WINDOW_DAYS == 30.0)


# -- Orden y reporte -----------------------------------------------------------------

def test_ordering_and_report() -> None:
    print("\norden y reporte")

    now = time.time()
    fs = []
    # El más frecuente: 6 apariciones repartidas en 12 días.
    fs += [
        _failure("terminal", "boom alpha", now - 86400 * (12 - i * 2), f"s{i}")
        for i in range(6)
    ]
    # Menos frecuente pero también recurrente: 3 apariciones en 6 días.
    fs += [
        _failure("read_file", "boom beta", now - 86400 * (6 - i * 2), f"s{i}")
        for i in range(3)
    ]
    # Un único evento.
    fs.append(_failure("patch", "boom gamma", now - 50, "s9"))

    report = find_recurring(scan=_scan(fs, until=now))

    check("2 recurrentes", len(report.recurring) == 2, f"{len(report.recurring)}")
    check("1 evento único", report.single_events == 1)
    check("ordenado por frecuencia descendente",
          report.recurring[0].occurrences >= report.recurring[1].occurrences)
    check("cada recurrente trae su muestra",
          all(r.sample for r in report.recurring))
    check("cada recurrente explica por qué pasó",
          all(r.why() for r in report.recurring))


# -- Barrido de trayectoria ----------------------------------------------------------

def _make_db(tmp: Path, rows: list[tuple]) -> Path:
    """Crea un ``state.db`` mínimo con la forma que el lector espera."""
    db = tmp / "state.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, role TEXT, active INTEGER, "
        "tool_name TEXT, content TEXT, timestamp REAL, session_id TEXT)"
    )
    con.executemany(
        "INSERT INTO messages (role, active, tool_name, content, timestamp, session_id) "
        "VALUES (?,?,?,?,?,?)",
        rows,
    )
    con.commit()
    con.close()
    return db


def _tool_result(payload: str) -> str:
    return payload


def test_scan_structure_not_text() -> None:
    print("\nbarrido — criterio estructural, no textual")

    tmp = Path(tempfile.mkdtemp(prefix="skh-scan-"))
    try:
        now = time.time()
        rows = [
            # (role, active, tool_name, content, timestamp, session_id)
            # El error va en el contenido JSON, pero NO es un fallo.
            ("tool", 1, "terminal", '{"output": "el informe menciona un error", "exit_code": 0}', now - 100, "s1"),
            # Fallo declarado por success=false.
            ("tool", 1, "terminal", '{"success": false, "error": "boom one"}', now - 90, "s1"),
            # Fallo declarado por exit_code.
            ("tool", 1, "terminal", '{"output": "no such file", "exit_code": 2}', now - 80, "s1"),
            # Fallo declarado por campo error.
            ("tool", 1, "read_file", '{"error": "access denied: /x/y"}', now - 70, "s1"),
            # No es JSON: se ignora.
            ("tool", 1, "terminal", "salida suelta sin json", now - 60, "s1"),
            # Fila inactiva: se ignora.
            ("tool", 0, "terminal", '{"success": false, "error": "no deberia contar"}', now - 50, "s1"),
            # Fila que no es de herramienta: se ignora.
            ("assistant", 1, None, '{"success": false}', now - 40, "s1"),
            # Fecha futura: no creíble, se descarta y se cuenta.
            ("tool", 1, "terminal", '{"success": false, "error": "future"}', now + 999999, "s1"),
        ]
        db = _make_db(tmp, rows)
        scan = scan_failures(state_db=db, since_ts=0.0, now=now)

        check("detecta 3 fallos reales", len(scan.failures) == 3, f"{len(scan.failures)}")
        check("la mención textual de 'error' NO cuenta",
              all("informe" not in f.sample for f in scan.failures))
        check("descartó 1 fila por fecha increíble", scan.discarded_ts == 1,
              f"discarded={scan.discarded_ts}")
        # 6 y no 8: la consulta ya filtra role='tool' AND active=1, así que la fila
        # inactiva y la del asistente nunca se leen. ``rows_scanned`` cuenta lo leído.
        check("barrió las 6 filas de herramienta activas",
              scan.rows_scanned == 6, f"{scan.rows_scanned}")

        huellas = {f.fingerprint for f in scan.failures}
        check("3 formas distintas → 3 huellas", len(huellas) == 3, f"{len(huellas)}")

        # El mismo error de acceso, escrito distinto, es una sola forma.
        rows2 = [
            ("tool", 1, "read_file", '{"error": "access denied: /a/one"}', now - 90, "s1"),
            ("tool", 1, "read_file", '{"error": "access denied: /b/two"}', now - 80, "s2"),
        ]
        (tmp / "sub").mkdir()
        db2 = _make_db(tmp / "sub", rows2)
        scan2 = scan_failures(state_db=db2, since_ts=0.0, now=now)
        check("rutas distintas del mismo error → 1 huella",
              len({f.fingerprint for f in scan2.failures}) == 1,
              f"{len({f.fingerprint for f in scan2.failures})}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_scan_survives_missing_db() -> None:
    print("\nbarrido — trayectoria ausente")

    scan = scan_failures(state_db=Path("/no/existe/state.db"))
    check("sin base → barrido vacío, sin excepción", len(scan.failures) == 0)
    check("no reporta filas", scan.rows_scanned == 0)
    check("se declara no usable", scan.usable is False)


def test_discarded_ts_affects_trust() -> None:
    print("\ndescartes por fecha afectan la confianza")

    now = time.time()
    # Un solo fallo y muchas filas descartadas: la conclusión negativa no es sostenible.
    scan = _scan([_failure("t", "x", now - 10, "s1")], until=now, discarded=50)
    report = find_recurring(scan=scan)
    check("muchos descartes → no confiable", report.trustworthy is False)

    scan2 = _scan([_failure("t", "x", now - 10, "s1")], until=now, discarded=0)
    check("sin descartes → confiable", find_recurring(scan=scan2).trustworthy is True)


def test_detect_on_real_or_empty() -> None:
    print("\ndetect() — punta a punta")

    tmp = Path(tempfile.mkdtemp(prefix="skh-det-"))
    try:
        now = time.time()
        rows = []
        # Un error recurrente en 3 sesiones, sostenido en 10 días.
        for i in range(4):
            rows.append((
                "tool", 1, "terminal",
                '{"success": false, "error": "connection refused to host"}',
                now - 86400 * (10 - i * 3), f"s{i % 3}",
            ))
        # Un error que apareció una vez.
        rows.append((
            "tool", 1, "patch", '{"error": "unique glitch here"}', now - 1800, "s0",
        ))
        db = _make_db(tmp, rows)

        report = detect(state_db=db, now=now)

        check("detecta 1 recurrente", len(report.recurring) == 1, f"{len(report.recurring)}")
        if report.recurring:
            r = report.recurring[0]
            check("es la conexión rechazada", "connection refused" in r.sample.lower(),
                  r.sample)
            check("4 apariciones", r.occurrences == 4, f"{r.occurrences}")
            check("3 sesiones", r.sessions == 3, f"{r.sessions}")
        check("el evento único quedó fuera",
              report.single_events == 1, f"{report.single_events}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_guardrail_is_not_an_agent_failure() -> None:
    print("\nrechazo del arnés ≠ fallo del agente")

    now = time.time()
    # Un rechazo del arnés repetido en 4 sesiones, sostenido en el tiempo: por el
    # criterio bruto pasaría el filtro. NO debe pasar.
    fs = [
        _failure("execute_code", "BLOCKED: execute_code runs arbitrary local Python", now - 86400 * 3, f"s{i}")
        for i in range(4)
    ]
    report = find_recurring(scan=_scan(fs, until=now))

    check("un rechazo del arnés NO es recurrente",
          report.has_recurrence is False,
          f"recurrentes={len(report.recurring)}")
    check("se reporta como guardarraíl, no se descarta en silencio",
          len(report.guardrails) == 1, f"{len(report.guardrails)}")
    if report.guardrails:
        check("el guardarraíl conserva su muestra y su cuenta",
              report.guardrails[0].occurrences == 4 and bool(report.guardrails[0].sample))

    # Un fallo propio del agente, misma forma de repetición: ESE sí pasa.
    fs2 = [
        _failure("read_file", "File not found: /home/andrea/wiki/X.md",
                 now - 86400 * (8 - i * 2), f"s{i}")
        for i in range(4)
    ]
    report2 = find_recurring(scan=_scan(fs2, until=now))
    check("un fallo propio del agente SÍ es recurrente",
          report2.has_recurrence is True)
    check("no se confundió con guardarraíl", len(report2.guardrails) == 0)


def test_burst_is_not_a_pattern() -> None:
    print("\nráfaga ≠ patrón sostenido")

    now = time.time()
    # 12 apariciones en 33 sesiones distintas, pero todas en 2,9 HORAS.
    # Este es el caso real medido: cruzaría el criterio de sesiones con holgura.
    inicio = now - 0.12 * 86400
    fs = [
        _failure("execute_code", "real agent failure here", inicio + i * 60, f"s{i}")
        for i in range(12)
    ]
    report = find_recurring(scan=_scan(fs, until=now))

    check("12 apariciones en 12 sesiones pero en 2,9 h → NO es recurrente",
          report.has_recurrence is False,
          f"recurrentes={len(report.recurring)}")
    check("se reporta como ráfaga", len(report.bursts) == 1, f"{len(report.bursts)}")
    if report.bursts:
        check("la ráfaga declara su span en días",
              report.bursts[0].span_days < MIN_SPAN_DAYS,
              f"span={report.bursts[0].span_days:.3f}")

    # El mismo volumen repartido en 10 días: ahí sí es patrón.
    fs2 = [
        _failure("execute_code", "real agent failure here", now - 86400 * 10 + i * 86400, f"s{i}")
        for i in range(10)
    ]
    report2 = find_recurring(scan=_scan(fs2, until=now))
    check("el mismo volumen repartido en 10 días → recurrente",
          report2.has_recurrence is True)
    check("no quedó clasificado como ráfaga", len(report2.bursts) == 0)


def test_is_guardrail_criterion() -> None:
    print("\ncriterio de guardarraíl")

    check("BLOCKED → guardarraíl", is_guardrail("BLOCKED: no se puede") is True)
    check("Access denied → guardarraíl", is_guardrail("Access denied: /x") is True)
    check("Refusing to → guardarraíl", is_guardrail("Refusing to write config") is True)
    check("mayúsculas indistintas", is_guardrail("blocked: por el arnes") is True)
    check("un fallo normal NO es guardarraíl",
          is_guardrail("File not found: /x/y") is False)
    check("texto vacío → no", is_guardrail("") is False)
    check("es determinista",
          is_guardrail("BLOCKED: x") == is_guardrail("BLOCKED: x"))


def test_summary_repartition() -> None:
    print("\nreparto en el resumen")

    now = time.time()
    fs = []
    # recurrente sostenido
    fs += [_failure("read_file", "File not found: /a/b", now - 86400 * 4 + i * 86400, f"s{i}")
           for i in range(3)]
    # ráfaga
    base = now - 0.05 * 86400
    fs += [_failure("terminal", "real failure burst", base + i * 60, f"b{i}") for i in range(4)]
    # guardarraíl
    fs += [_failure("terminal", "BLOCKED: flagged as dangerous", now - 86400 * 2, "g1")]
    # evento único
    fs.append(_failure("patch", "unique glitch", now - 100, "u1"))

    report = find_recurring(scan=_scan(fs, until=now))
    s = report.summary()
    check("el resumen nombra los cuatro cubos",
          all(word in s for word in ("recurrentes", "ráfagas", "rechazos", "únicos")), s)
    check("1 recurrente", len(report.recurring) == 1, f"{len(report.recurring)}")
    check("1 ráfaga", len(report.bursts) == 1, f"{len(report.bursts)}")
    check("1 guardarraíl", len(report.guardrails) == 1, f"{len(report.guardrails)}")
    check("1 evento único", report.single_events == 1, f"{report.single_events}")


def main() -> int:
    print("=" * 68)
    print("hermes-skills-helper — etapa 1 (recurrencia)")
    print("=" * 68)

    for fn in (
        test_threshold,
        test_grouping,
        test_single_event_discarded,
        test_loop_in_one_session_is_not_a_pattern,
        test_window_excludes_old_failures,
        test_ordering_and_report,
        test_scan_structure_not_text,
        test_scan_survives_missing_db,
        test_discarded_ts_affects_trust,
        test_detect_on_real_or_empty,
        test_guardrail_is_not_an_agent_failure,
        test_burst_is_not_a_pattern,
        test_is_guardrail_criterion,
        test_summary_repartition,
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
