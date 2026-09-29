"""Tests de la etapa 5 — los cuatro veredictos sobre casos construidos.

Criterio de cierre del diseño: *"los cuatro veredictos se reproducen sobre casos
construidos"*. Estos tests lo ejecutan, y además fijan las dos propiedades que hacen
honesta la medición:

1. **No medir ≠ medir cero.** Un fracaso de lectura no puede producir un ``0`` que
   después se lea como "no se usó".
2. **La huella es estable.** El mismo error escrito distinto entre sesiones es un solo
   patrón; dos errores distintos siguen siendo dos.

Sin dependencias externas: se corre con ``python3 tests/test_effect.py``.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from effect import (  # noqa: E402
    MIN_WINDOW_DAYS,
    TOO_NEW,
    UNRELIABLE,
    UNUSED,
    WORKING,
    believable_ts,
    check_recurrence,
    check_survival,
    decide,
    evaluate,
    fingerprint,
    fingerprint_of,
    normalize_error,
    sha256_text,
)
from effect.usage import (  # noqa: E402
    SINCE_APPROX,
    SINCE_EXACT,
    SINCE_FLOOR,
    UNAVAILABLE,
    UseCount,
    count_uses_from_record,
    count_uses_from_trajectory,
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


# -- Normalización y huella ---------------------------------------------------------

def test_normalization() -> None:
    print("\nnormalización del error")

    a = normalize_error("HTTP 429 for /users/8821")
    b = normalize_error("HTTP 429 for /users/9134")
    check("ids distintos colapsan al mismo patrón", a == b, f"{a!r} != {b!r}")

    check("vacío devuelve vacío", normalize_error("") == "")
    check("espacios colapsan", normalize_error("  a   b  ") == "a b")
    check("mayúsculas unifican", normalize_error("ERROR X") == normalize_error("error x"))

    ts1 = normalize_error("failed at 2026-09-29T10:00:00Z")
    ts2 = normalize_error("failed at 2026-09-28T22:13:45Z")
    check("timestamps distintos colapsan", ts1 == ts2, f"{ts1!r} != {ts2!r}")

    p1 = normalize_error("cannot open /home/andrea/developers/a/b.md")
    p2 = normalize_error("cannot open /home/andrea/other/b.md")
    check("el mismo archivo en otro directorio colapsa", p1 == p2, f"{p1!r} != {p2!r}")

    # El último segmento de la ruta ES la identidad del fallo (medido sobre state.db
    # real: tapar la ruta entera agrupaba 18 fallos sin relación bajo una huella).
    q1 = normalize_error("File not found: /home/andrea/wiki/SCHEMA.md")
    q2 = normalize_error("File not found: /home/andrea/wiki/log.md")
    check("archivos distintos NO colapsan", q1 != q2, f"{q1!r} == {q2!r}")
    check("y el nombre del archivo sobrevive", "schema.md" in q1 and "log.md" in q2,
          f"{q1!r} / {q2!r}")

    # exit_code no es un contador: es la identidad del fallo.
    e1 = normalize_error("exit_code=1")
    e2 = normalize_error("exit_code=124")
    check("exit_code distintos NO colapsan", e1 != e2, f"{e1!r} == {e2!r}")
    check("el código sobrevive", "124" in e2, e2)
    check("pero un contador corriente sí colapsa",
          normalize_error("read 5 rows") == normalize_error("read 9 rows"))

    # El aviso de bucle de herramientas es instrumentación del arnés: se quita.
    with_warning = normalize_error(
        "boom: disk full [Tool loop warning: repeated_exact_failure_warning; count=3]"
    )
    without = normalize_error("boom: disk full")
    check("el aviso de bucle del arnés se elimina", with_warning == without,
          f"{with_warning!r} != {without!r}")


def test_fingerprint() -> None:
    print("\nhuella (fingerprint)")

    f1 = fingerprint("terminal", "HTTP 500 for /jobs/12")
    f2 = fingerprint("terminal", "HTTP 500 for /jobs/99")
    check("misma forma → misma huella", f1 == f2, f"{f1} != {f2}")

    f3 = fingerprint("terminal", "disk full")
    check("forma distinta → huella distinta", f1 != f3)

    f4 = fingerprint("read_file", "HTTP 500 for /jobs/12")
    check("misma forma, otra herramienta → huella distinta", f1 != f4)

    check("la huella es corta y estable", len(f1) == 12 and f1 == fingerprint("terminal", "HTTP 500 for /jobs/12"))

    # Cola distinta con prefijo largo: no debe colapsar.
    long_a = "x" * 500 + " final A"
    long_b = "x" * 500 + " final B"
    check("prefijo común largo, cola distinta → huellas distintas",
          fingerprint("t", long_a) != fingerprint("t", long_b))

    # Recorte determinista.
    check("el recorte es determinista",
          fingerprint_of("t", "y" * 9000) == fingerprint_of("t", "y" * 9000))


def test_believable_ts() -> None:
    print("\ntimestamps creíbles")

    now = 1_800_000_000.0
    check("futuro lejano → no creíble", believable_ts(now + 86400, now=now) is None)
    check("desfase mínimo se tolera", believable_ts(now + 60, now=now) is not None)
    check("cero → no creíble", believable_ts(0, now=now) is None)
    check("negativo → no creíble", believable_ts(-5, now=now) is None)
    check("basura → no creíble", believable_ts("ayer", now=now) is None)
    check("None → no creíble", believable_ts(None, now=now) is None)
    check("pasado razonable → creíble", believable_ts(now - 3600, now=now) is not None)


# -- Chequeo 1: supervivencia -------------------------------------------------------

def test_survival() -> None:
    print("\nchequeo 1 — ¿sobrevivió?")

    tmp = Path(tempfile.mkdtemp(prefix="skh-surv-"))
    try:
        skill = tmp / "SKILL.md"
        skill.write_text("# skill\ncontenido\n", encoding="utf-8")
        h = sha256_text(skill.read_text(encoding="utf-8"))

        s = check_survival(skill, h)
        check("contenido igual → sobrevivió", s.survived and s.measurable)

        skill.write_text("# skill\neditado por otro\n", encoding="utf-8")
        s2 = check_survival(skill, h)
        check("contenido editado → no sobrevivió", s2.survived is False and s2.measurable)

        missing = check_survival(tmp / "no-existe.md", h)
        check("archivo ausente → NO medible (no 'no sobrevivió')",
              missing.measurable is False and missing.survived is False)

        # Fin de línea: el mismo contenido en CRLF es el mismo contenido.
        skill.write_text("# skill\r\ncontenido\r\n", encoding="utf-8")
        check("CRLF cuenta como el mismo contenido",
              check_survival(skill, h).survived is True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -- Chequeo 2: uso -----------------------------------------------------------------

def test_usage_record() -> None:
    print("\nchequeo 2 — ¿se usó? (registro del arnés)")

    import json

    tmp = Path(tempfile.mkdtemp(prefix="skh-use-"))
    try:
        skills = tmp / "skills"
        skills.mkdir()
        now = time.time()
        old = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 86400 * 30))
        recent = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 60))

        data = {
            "usado-antes": {"use_count": 9, "last_used_at": old},
            "usado-despues": {"use_count": 12, "last_used_at": recent},
            "nunca-usado": {"use_count": 0},
            "sin-registro-viejo": {"use_count": 3, "last_used_at": ""},
        }
        (skills / ".usage.json").write_text(json.dumps(data), encoding="utf-8")

        since = now - 86400  # el cambio fue hace 1 día

        r = count_uses_from_record("usado-antes", since_ts=since, hermes_home=tmp)
        check("último uso anterior al cambio → 0 EXACTO",
              r.count == 0 and r.scope == SINCE_EXACT, str(r))

        r = count_uses_from_record("usado-despues", since_ts=since, hermes_home=tmp)
        check("uso posterior al cambio → piso (no total inventado)",
              r.count is None and r.scope == SINCE_FLOOR, str(r))

        r = count_uses_from_record("nunca-usado", since_ts=since, hermes_home=tmp)
        check("nunca usado → 0 exacto", r.count == 0 and r.scope == SINCE_EXACT, str(r))

        r = count_uses_from_record("sin-registro-viejo", since_ts=since, hermes_home=tmp)
        check("marca de uso vacía e histórico > 0 → no medible",
              r.count is None and r.scope == UNAVAILABLE, str(r))

        r = count_uses_from_record("inexistente", since_ts=since, hermes_home=tmp)
        check("skill sin registro → no medible (no cero)",
              r.count is None and r.scope == UNAVAILABLE, str(r))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_usage_trajectory() -> None:
    print("\nchequeo 2 — trayectoria (state.db)")

    import sqlite3

    tmp = Path(tempfile.mkdtemp(prefix="skh-traj-"))
    try:
        db = tmp / "state.db"
        con = sqlite3.connect(db)
        con.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, active INTEGER, "
            "tool_name TEXT, content TEXT, timestamp REAL)"
        )
        now = time.time()
        rows = [
            (1, "skill_view", "cargando /skills/demo/SKILL.md", now - 3600),      # posterior
            (1, "skill_view", "cargando /skills/demo/SKILL.md", now - 7200),      # posterior
            (1, "skill_view", "cargando /skills/demo/SKILL.md", now - 86400 * 40), # anterior
            (0, "skill_view", "cargando /skills/demo/SKILL.md", now - 100),        # inactiva
            (1, "skill_view", "cargando /skills/otro/SKILL.md", now - 100),        # otro skill
            (1, None, "sin /skills/demo/SKILL.md acá", now + 999999),              # futura
        ]
        con.executemany(
            "INSERT INTO messages (active, tool_name, content, timestamp) VALUES (?,?,?,?)",
            rows,
        )
        con.commit()
        con.close()

        since = now - 86400
        r = count_uses_from_trajectory("demo", since_ts=since, state_db=db)
        check("cuenta solo las posteriores y activas (futura descartada)",
              r.count == 2 and r.scope == SINCE_APPROX, str(r))

        r = count_uses_from_trajectory("fantasma", since_ts=since, state_db=db)
        check("sin coincidencias → 0 aproximado", r.count == 0 and r.scope == SINCE_APPROX, str(r))

        r = count_uses_from_trajectory("demo", since_ts=since, state_db=tmp / "no.db")
        check("base ausente → no medible", r.count is None and r.scope == UNAVAILABLE, str(r))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -- Chequeo 3: recurrencia ---------------------------------------------------------

def test_recurrence() -> None:
    print("\nchequeo 3 — ¿volvió el error?")

    now = time.time()
    since = now - 86400 * 30
    target = fingerprint("terminal", "disk full")

    r = check_recurrence([], target_fingerprint=target, since_ts=since, now=now)
    check("sin huellas posteriores → no recurrió",
          r.recurred is False and r.occurrences == 0)

    r = check_recurrence([target, "otra"], target_fingerprint=target, since_ts=since, now=now)
    check("huella objetivo presente → recurrió",
          r.recurred is True and r.occurrences == 1)

    r = check_recurrence(["otra"], target_fingerprint=target, since_ts=since, now=now)
    check("solo huellas distintas → no recurrió", r.recurred is False)

    # Ventana insuficiente: no se atreve a concluir.
    r = check_recurrence([], target_fingerprint=target, since_ts=now - 3600, now=now)
    check("ventana < mínimo → None (ni sí ni no)", r.recurred is None,
          f"recurred={r.recurred}, ventana={r.window_days:.2f} días")


# -- Veredicto ----------------------------------------------------------------------

def _mk(survived=True, measurable=True, used=None, scope=SINCE_EXACT, recurred=False, occ=0):
    from effect import RecurrenceCheck, SurvivedCheck, UseCheck

    return (
        SurvivedCheck(survived=survived, measurable=measurable),
        UseCheck(count=used, scope=scope),
        RecurrenceCheck(recurred=recurred, occurrences=occ, window_days=30.0),
    )


def test_verdicts() -> None:
    print("\nlos cuatro veredictos")

    # WORKING: sobrevivió, se usó, no volvió.
    s, u, rc = _mk(used=5)
    v, why = decide(survived=s, usage=u, recurrence=rc, age_days=30)
    check("sobrevivió + se usó + no volvió → working", v == WORKING, f"{v}: {why}")

    # UNUSED: sobrevivió, no se usó.
    s, u, rc = _mk(used=0)
    v, why = decide(survived=s, usage=u, recurrence=rc, age_days=30)
    check("sobrevivió + NO se usó → unused", v == UNUSED, f"{v}: {why}")

    # UNRELIABLE por recurrencia.
    s, u, rc = _mk(used=5, recurred=True, occ=3)
    v, why = decide(survived=s, usage=u, recurrence=rc, age_days=30)
    check("el error volvió → unreliable", v == UNRELIABLE, f"{v}: {why}")

    # UNRELIABLE por pérdida del cambio: gana sobre el uso.
    s, u, rc = _mk(survived=False, used=9)
    v, why = decide(survived=s, usage=u, recurrence=rc, age_days=30)
    check("no sobrevivió + se usó → unreliable (el no-sobrevivir manda)",
          v == UNRELIABLE, f"{v}: {why}")

    # TOO_NEW por edad.
    s, u, rc = _mk(used=0)
    v, why = decide(survived=s, usage=u, recurrence=rc, age_days=1.0)
    check("edad < ventana mínima → too_new", v == TOO_NEW, f"{v}: {why}")

    # TOO_NEW por uso no medible: no se puede acreditar ni descartar.
    s, u, rc = _mk(used=None, scope=UNAVAILABLE)
    v, why = decide(survived=s, usage=u, recurrence=rc, age_days=30)
    check("uso no medible → too_new (no 'unused')", v == TOO_NEW, f"{v}: {why}")

    # TOO_NEW por falta de ventana de recurrencia.
    s, u, rc = _mk(used=5)
    from effect import RecurrenceCheck

    rc_none = RecurrenceCheck(recurred=None, window_days=1.0)
    v, why = decide(survived=s, usage=u, recurrence=rc_none, age_days=30)
    check("recurrencia indeterminada → too_new", v == TOO_NEW, f"{v}: {why}")

    # too_new no es concluyente; working sí.
    check("MIN_WINDOW_DAYS es un número positivo", MIN_WINDOW_DAYS > 0)


def test_evaluate_end_to_end() -> None:
    print("\nevaluate() de punta a punta")

    import json

    tmp = Path(tempfile.mkdtemp(prefix="skh-e2e-"))
    try:
        skills = tmp / "skills" / "demo"
        skills.mkdir(parents=True)
        skill_md = skills / "SKILL.md"
        skill_md.write_text("# demo\nnueva regla\n", encoding="utf-8")
        h = sha256_text(skill_md.read_text(encoding="utf-8"))

        now = time.time()
        applied = now - 86400 * 30
        (tmp / "skills" / ".usage.json").write_text(
            json.dumps({
                "demo": {
                    "use_count": 4,
                    "last_used_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 3600)
                    ),
                }
            }),
            encoding="utf-8",
        )

        v = evaluate(
            skill_name="demo",
            skill_path=skill_md,
            expected_hash=h,
            applied_ts=applied,
            target_fingerprint=fingerprint("terminal", "disk full"),
            fingerprints_after=[],
            hermes_home=tmp,
            now=now,
        )
        check("caso completo → working", v.verdict == WORKING, f"{v.verdict}: {v.reasons}")
        check("el veredicto trae razones", len(v.reasons) >= 1)
        check("declara que no afirma causalidad",
              "no se afirma causalidad" in v.causal_claim)
        check("working es concluyente", v.is_conclusive is True)

        # El mismo caso, con el skill editado después: no se acredita.
        skill_md.write_text("# demo\notra cosa\n", encoding="utf-8")
        v2 = evaluate(
            skill_name="demo",
            skill_path=skill_md,
            expected_hash=h,
            applied_ts=applied,
            target_fingerprint="nada",
            fingerprints_after=[],
            hermes_home=tmp,
            now=now,
        )
        check("skill editado después → unreliable", v2.verdict == UNRELIABLE, v2.verdict)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    print("=" * 68)
    print("hermes-skills-helper — etapa 5 (¿sirvió?)")
    print("=" * 68)

    for fn in (
        test_normalization,
        test_fingerprint,
        test_believable_ts,
        test_survival,
        test_usage_record,
        test_usage_trajectory,
        test_recurrence,
        test_verdicts,
        test_evaluate_end_to_end,
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
