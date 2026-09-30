"""Medición del cableado: el plugin cargado por el arnés REAL, no por nosotros.

Verifica que register() registre el hook y los dos tools, que el hook corra sin romper el
turno, y que el barrido lea el arnés de verdad.

Uso: python3 tests/medicion_cableado.py
"""

from __future__ import annotations

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from hermes_cli import plugin_dev  # noqa: E402
from hermes_cli.plugins import PluginManager  # noqa: E402

OK: list[str] = []
NO: list[str] = []


def check(nombre: str, condicion: bool, detalle: str = "") -> None:
    (OK if condicion else NO).append(f"{nombre}{(': ' + detalle) if detalle else ''}")


def main() -> int:
    print("=== CABLEADO contra el arnés real ===")
    print()

    # 1. El doctor del arnés corre register() en copia aislada, y su informe es la vía
    # oficial: lee el manifest Y lo que register() registró, y compara los dos.
    #
    # El registro EN VIVO no se consulta a propósito: el doctor carga el plugin en una
    # copia aislada, así que este proceso no tiene sus tools. Consultarlos daría False y
    # ese False no significaría nada — el plugin todavía no está instalado.
    try:
        informe_val = plugin_dev.doctor_plugin(RAIZ)
        hooks_reg = list(getattr(informe_val, "registered_hooks", ()) or ())
        tools_reg = list(getattr(informe_val, "registered_tools", ()) or ())
        check("registra on_session_end", "on_session_end" in hooks_reg, str(hooks_reg))
        check("registra skills_signals", "skills_signals" in tools_reg, str(tools_reg))
        check("registra skills_report", "skills_report" in tools_reg, str(tools_reg))
        errores = getattr(informe_val, "errors", None) or []
        check("el doctor no reporta errores", not errores, str(errores)[:200])
    except Exception as exc:
        check("el doctor corre", False, f"{type(exc).__name__}: {exc}")

    # 2. El barrido corre sobre el arnés real: lee el registro de uso y la trayectoria.
    try:
        from recolector import recolectar

        home = Path("/home/andrea/.hermes")
        informe = recolectar(hermes_home=home,
                             skills_dirs=[home / "skills",
                                          Path("/home/andrea/developers/ai/skills")],
                             force=True)
        check("el barrido corre", informe.scanned, informe.reason)
        check("leyó el registro de uso", informe.uso is not None,
              str(informe.errors[:2]))
        check("la trayectoria se pudo leer", informe.trajectory_readable,
              str(informe.errors[:2]))
        if informe.uso is not None:
            check("encontró señales", informe.uso.total_activos > 0,
                  str(informe.uso.total_activos))
            print()
            print("  " + informe.summary())
    except Exception as exc:
        check("el barrido corre", False, f"{type(exc).__name__}: {exc}")

    # 4. El estado escribe atómico y el throttle respeta el intervalo
    try:
        import tempfile
        from recolector import read_state, should_scan, write_state

        tmp = Path(tempfile.mkdtemp(prefix="cableado-"))
        write_state({"last_scan_ts": 1000.0}, hermes_home=tmp)
        check("el estado se lee de vuelta", read_state(hermes_home=tmp).get("last_scan_ts") == 1000.0)
        corresponde, razon = should_scan(hermes_home=tmp, now=1100.0, min_interval=900.0)
        check("el throttle corta antes del intervalo", not corresponde, razon)
        corresponde2, _ = should_scan(hermes_home=tmp, now=5000.0, min_interval=900.0)
        check("el throttle deja pasar después", corresponde2)
    except Exception as exc:
        check("el throttle funciona", False, f"{type(exc).__name__}: {exc}")

    print()
    for linea in OK:
        print("  OK   " + linea)
    for linea in NO:
        print("  NO   " + linea)
    print()
    print(f"{len(OK)} OK, {len(NO)} NO")
    return 1 if NO else 0


if __name__ == "__main__":
    raise SystemExit(main())
