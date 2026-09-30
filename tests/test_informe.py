"""Tests del informe que el plugin entrega — el markdown y el JSON.

Este archivo existe por un defecto real: la sección de fallos recurrentes imprimía
sólo la estadística (*"5 apariciones en 4 sesiones"*) y **omitía el fallo mismo**. Un
lector que veía eso tenía que ir a buscarlo aparte, y entonces el informe no cumplía su
función — que es ser el insumo de la revisión sin trabajo adicional.

El defecto pasó los 209 tests anteriores porque ninguno miraba el generador del informe:
se probaban las señales y la recurrencia por separado, nunca la última milla, que es lo
que la persona (o el cron, o el issue) realmente lee.

Determinista y sin tocar el arnés: los informes se arman a mano.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

# El plugin usa imports relativos; cargarlo como paquete es lo que hace el arnés.
_spec = importlib.util.spec_from_file_location(
    "hermes_skills_helper", RAIZ / "__init__.py", submodule_search_locations=[str(RAIZ)])
plugin = importlib.util.module_from_spec(_spec)
sys.modules["hermes_skills_helper"] = plugin
_spec.loader.exec_module(plugin)

from recolector import RecolectorReport  # noqa: E402
from recurrence import RecurrenceReport, RecurringFailure  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(nombre: str, condicion: bool, detalle: str = "") -> None:
    global PASSED
    if condicion:
        PASSED += 1
    else:
        FAILED.append(f"{nombre}{(': ' + detalle) if detalle else ''}")


def fallo(*, tool: str = "terminal", ocurrencias: int = 5, sesiones: int = 4,
          muestra: str = "BOOM: algo falló", fingerprint: str = "abc123") -> RecurringFailure:
    return RecurringFailure(
        fingerprint=fingerprint, tool_name=tool, occurrences=ocurrencias,
        sessions=sesiones, first_ts=1_800_000_000.0, last_ts=1_800_000_000.0 + 9.2 * 86400,
        sample=muestra,
    )


def informe_con(*fallos: RecurringFailure, muestras: int = 0) -> RecolectorReport:
    """Un RecolectorReport con los fallos dados y los cubos de uso vacíos."""
    rec = RecurrenceReport(
        recurring=tuple(fallos), bursts=(), guardrails=(), stale=(),
        single_events=0, total_failures=len(fallos), sessions_seen=len(fallos),
        discarded_ts=0, window_days=30.0, scanned_until=1_800_000_000.0,
    )
    return RecolectorReport(scanned=True, reason="barrido forzado", ts=1_800_000_000.0,
                            recurrence=rec)


# -- El markdown: el fallo tiene que estar, no sólo la estadística --------------------

def test_el_markdown_dice_que_fallo() -> None:
    """La regresión que motivó este archivo."""
    md = plugin._markdown_issue(informe_con(fallo(muestra="No such file or directory: /x")))

    check("nombra el tool", "terminal" in md, md[-400:])
    check("incluye la muestra del fallo", "No such file or directory" in md, md[-400:])
    check("y sigue dando la evidencia",
          "5 apariciones" in md and "4 sesiones" in md, md[-400:])


def test_el_markdown_cita_la_muestra() -> None:
    """La muestra va como cita para distinguirla de la estadística."""
    md = plugin._markdown_issue(informe_con(fallo(muestra="un fallo concreto")))
    lineas = [l for l in md.split("\n") if "un fallo concreto" in l]

    check("hay una linea con la muestra", len(lineas) == 1, str(lineas))
    check("va como cita", lineas[0].startswith("  > "), lineas[0] if lineas else "")


def test_la_muestra_se_recorta() -> None:
    """Una muestra de 50 KB no puede inundar el issue."""
    md = plugin._markdown_issue(informe_con(fallo(muestra="x" * 5_000)))
    linea = [l for l in md.split("\n") if l.startswith("  > ")][0]

    check("la cita queda corta", len(linea) < 400, str(len(linea)))
    check("marca el recorte", linea.rstrip().endswith("…"), linea[-40:])


def test_la_muestra_se_normaliza() -> None:
    """Saltos de línea y tabulaciones romperían la cita en markdown."""
    md = plugin._markdown_issue(informe_con(fallo(muestra="linea1\n\nlinea2\t\ttabulado")))
    cita = [l for l in md.split("\n") if l.startswith("  > ")][0]

    check("la cita es una sola linea", "\n" not in cita, repr(cita))
    check("sin tabulaciones", "\t" not in cita, repr(cita))
    check("el contenido sobrevive", "linea1" in cita and "linea2" in cita, cita)


def test_fallo_sin_muestra_no_rompe() -> None:
    """`sample` vacío o ausente: se sigue reportando el fallo."""
    for muestra in ("", None):
        md = plugin._markdown_issue(informe_con(fallo(muestra=muestra)))
        check(f"sin muestra ({muestra!r}) igual nombra el tool", "terminal" in md, md[-300:])
        check(f"sin muestra ({muestra!r}) no agrega cita vacía",
              "  > \n" not in md and "  > " not in md.split("### Fallos")[-1].split("\n")[2],
              md[-300:])


def test_sin_fallos_dice_ninguno() -> None:
    md = plugin._markdown_issue(informe_con())
    check("dice _ninguno_", "_ninguno_" in md, md[-300:])


def test_varios_fallos_se_listan_todos() -> None:
    fallos = [fallo(tool="terminal", muestra="uno", fingerprint="a"),
              fallo(tool="execute_code", muestra="dos", fingerprint="b"),
              fallo(tool="write_file", muestra="tres", fingerprint="c")]
    md = plugin._markdown_issue(informe_con(*fallos))

    for token in ("terminal", "execute_code", "write_file", "uno", "dos", "tres"):
        check(f"aparece {token}", token in md, md[-500:])


# -- El JSON: lo mismo, para quien consume el tool programáticamente ----------------

def test_el_json_trae_el_fallo_y_no_solo_el_texto() -> None:
    salida = plugin._senales_json(informe_con(fallo(muestra="un fallo concreto")))
    detalle = salida["recurrencia"]["detalle"]

    check("hay un detalle", len(detalle) == 1, str(detalle))
    d = detalle[0]
    check("trae el tool", d.get("tool") == "terminal", str(d))
    check("trae la muestra", "un fallo concreto" in str(d.get("muestra")), str(d))
    check("trae la huella", d.get("huella") == "abc123", str(d))
    check("trae la evidencia", "5 apariciones" in str(d.get("evidencia")), str(d))
    check("trae las fechas", isinstance(d.get("primera"), float), str(d))


def test_el_json_respeta_el_limite() -> None:
    fallos = [fallo(tool=f"t{i}", muestra=f"m{i}", fingerprint=f"f{i}") for i in range(10)]
    salida = plugin._senales_json(informe_con(*fallos), limite=3)

    check("el conteo total es el real", salida["recurrencia"]["recurrentes"] == 10,
          str(salida["recurrencia"]["recurrentes"]))
    check("el detalle respeta el limite", len(salida["recurrencia"]["detalle"]) == 3,
          str(len(salida["recurrencia"]["detalle"])))


def test_el_json_es_serializable() -> None:
    """El tool devuelve JSON: si no serializa, se rompe el consumidor."""
    salida = plugin._senales_json(informe_con(fallo()))

    try:
        texto = json.dumps(salida, ensure_ascii=False, default=str)
        check("serializa a JSON", json.loads(texto)["recurrencia"]["recurrentes"] == 1)
    except Exception as exc:
        check("serializa a JSON", False, f"{type(exc).__name__}: {exc}")


def test_el_json_declara_que_no_barrio() -> None:
    """Un informe por throttle no puede leerse como ausencia de señales."""
    salida = plugin._senales_json(RecolectorReport(scanned=False, reason="faltan 800 s"))

    check("scanned es False", salida["scanned"] is False)
    check("la razon viaja", "faltan 800" in salida["reason"], salida["reason"])
    check("no inventa cubos de uso", "uso" not in salida, str(sorted(salida)))


# -- El informe completo, end to end ------------------------------------------------

def test_el_tool_report_devuelve_markdown_y_nota() -> None:
    """`skills_report` tal como lo consume un lector: markdown listo + la aclaración."""
    informe = informe_con(fallo(muestra="un fallo concreto"))
    salida = plugin._senales_json(informe)
    salida["markdown"] = plugin._markdown_issue(informe)
    salida["nota"] = "El plugin no abre el issue: lo redacta."

    check("trae markdown", "#" in salida["markdown"] and len(salida["markdown"]) > 100)
    check("el markdown tiene el fallo", "un fallo concreto" in salida["markdown"])
    check("aclara que no abre el issue", "no abre el issue" in salida["nota"])
    check("todo junto serializa", isinstance(json.dumps(salida, default=str), str))


def main() -> int:
    pruebas = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for prueba in pruebas:
        try:
            prueba()
        except Exception as exc:  # noqa: BLE001 — un test que revienta es un fallo
            FAILED.append(f"{prueba.__name__} REVENTÓ: {type(exc).__name__}: {exc}")

    print(f"{PASSED} pasaron, {len(FAILED)} fallaron")
    for linea in FAILED:
        print("  FALLO " + linea)
    return 1 if FAILED else 0


if __name__ == "__main__":
    print("=== el informe que el plugin entrega ===")
    raise SystemExit(main())
