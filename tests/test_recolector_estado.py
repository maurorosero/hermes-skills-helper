"""Tests del estado del recolector — dónde vive y cómo se escribe.

Este archivo existe por un defecto real: la primera versión escribía el estado en
`<home>/plugins/<name>/`, que es el directorio de instalación del plugin. Dos
consecuencias, y las dos se verificaron contra el código del host:

1. `hermes plugins install` falla con "already exists" si el directorio ya está, y con
   `--force` el swap (`os.replace` del directorio entero) **se lleva el estado**.
2. `hermes plugins update` reemplaza el árbol completo: el estado se pierde en silencio.

El host lo dice explícitamente en `plugins/plugin_storage.py`: *"Plugins must NOT park
state in `<hermes home>/plugins/<name>/` (the install dir, deleted by `remove` and
git-pulled by `update`)"*. La convención es `<home>/plugin-data/<name>/`.

El defecto pasó los 184 tests porque ninguno miraba dónde vive el estado. Estos lo miran.

Todo determinista: fixtures en disco, reloj inyectado, sin modelo y sin red.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import recolector  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(nombre: str, condicion: bool, detalle: str = "") -> None:
    global PASSED
    if condicion:
        PASSED += 1
    else:
        FAILED.append(f"{nombre}{(': ' + detalle) if detalle else ''}")


def home_temporal() -> Path:
    return Path(tempfile.mkdtemp(prefix="recolector-test-"))


# -- Dónde vive el estado ------------------------------------------------------------

def test_no_escribe_dentro_del_arbol_del_plugin() -> None:
    """La regresión que motivó este archivo.

    El estado NO puede quedar bajo `plugins/`, porque ese es el directorio que el
    instalador reemplaza y que bloquea la instalación si existe.
    """
    home = home_temporal()
    ruta = recolector.state_path(hermes_home=home)

    partes = ruta.parts
    check("el estado no está bajo plugins/", "plugins" not in partes,
          f"ruta={ruta}")
    check("el estado está bajo plugin-data/", "plugin-data" in partes, f"ruta={ruta}")
    check("lleva el namespace del plugin", recolector.PLUGIN_DATA_NAME in partes,
          f"ruta={ruta}")
    check("termina con el nombre del archivo",
          ruta.name == recolector.STATE_FILE_NAME, f"ruta={ruta}")


def test_la_ruta_cuelga_del_home_que_recibe() -> None:
    """El home es un parámetro, no una constante global: los tests deben poder aislarlo."""
    uno = recolector.state_path(hermes_home=Path("/tmp/home-a"))
    dos = recolector.state_path(hermes_home=Path("/tmp/home-b"))

    check("dos homes dan dos rutas", uno != dos, f"{uno} vs {dos}")
    check("la ruta es relativa al home recibido",
          str(uno).startswith("/tmp/home-a/"), str(uno))


# -- Escritura y lectura -------------------------------------------------------------

def test_escritura_crea_el_directorio() -> None:
    """La ruta nueva no existe de entrada: si `write_state` no la crea, nada funciona."""
    home = home_temporal()
    ruta = recolector.state_path(hermes_home=home)

    check("el directorio no existe antes de escribir", not ruta.parent.exists(), str(ruta))

    recolector.write_state({"last_scan_ts": 123.0}, hermes_home=home)

    check("el directorio existe después", ruta.parent.is_dir(), str(ruta))
    check("el archivo existe después", ruta.is_file(), str(ruta))


def test_ida_y_vuelta() -> None:
    home = home_temporal()
    datos = {"last_scan_ts": 1_700_000_000.5, "last_scan_reason": "barrido forzado"}

    recolector.write_state(datos, hermes_home=home)
    leido = recolector.read_state(hermes_home=home)

    check("vuelve el mismo contenido", leido == datos, f"{leido}")


def test_leer_sin_estado_no_crea_nada() -> None:
    """Un lector no materializa lo que inspecciona."""
    home = home_temporal()

    check("sin estado devuelve vacío", recolector.read_state(hermes_home=home) == {})
    check("y no creó el directorio",
          not recolector.state_path(hermes_home=home).parent.exists())


def test_estado_corrupto_no_rompe() -> None:
    """Un JSON a medias no puede tumbar el barrido: se trata como ausente."""
    home = home_temporal()
    ruta = recolector.state_path(hermes_home=home)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text("{esto no es json", encoding="utf-8")

    check("estado corrupto devuelve vacío", recolector.read_state(hermes_home=home) == {})


def test_json_valido_pero_no_dict() -> None:
    """Una lista es JSON válido y no es estado."""
    home = home_temporal()
    ruta = recolector.state_path(hermes_home=home)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text("[1, 2, 3]", encoding="utf-8")

    check("lista en vez de dict devuelve vacío",
          recolector.read_state(hermes_home=home) == {})


def test_la_escritura_es_atomica() -> None:
    """Se escribe a temporal y se reemplaza: nunca hay un archivo a medias en su lugar."""
    home = home_temporal()
    ruta = recolector.state_path(hermes_home=home)

    recolector.write_state({"last_scan_ts": 1.0}, hermes_home=home)
    recolector.write_state({"last_scan_ts": 2.0}, hermes_home=home)

    check("quedó el último valor",
          recolector.read_state(hermes_home=home).get("last_scan_ts") == 2.0)
    check("no quedó el temporal", not ruta.with_suffix(".json.tmp").exists())
    check("no quedaron sobras en el directorio",
          sorted(p.name for p in ruta.parent.iterdir()) == [ruta.name],
          str(sorted(p.name for p in ruta.parent.iterdir())))


def test_el_estado_es_legible_como_json() -> None:
    """El archivo se inspecciona a mano: tiene que ser legible, no una línea."""
    home = home_temporal()
    ruta = recolector.state_path(hermes_home=home)

    recolector.write_state({"b": 2, "a": 1}, hermes_home=home)
    texto = ruta.read_text(encoding="utf-8")

    check("es JSON válido", json.loads(texto) == {"a": 1, "b": 2})
    check("tiene saltos de línea", texto.count("\n") >= 2, repr(texto[:40]))
    check("las claves salen ordenadas", texto.index('"a"') < texto.index('"b"'))


# -- El throttle lee ese estado ------------------------------------------------------

def test_el_throttle_usa_la_ruta_nueva() -> None:
    """El throttle y la escritura tienen que hablar del mismo archivo."""
    home = home_temporal()

    corresponde, razon = recolector.should_scan(hermes_home=home, now=1000.0)
    check("sin estado previo, barre", corresponde, razon)

    recolector.write_state({"last_scan_ts": 1000.0}, hermes_home=home)

    corresponde2, razon2 = recolector.should_scan(hermes_home=home, now=1100.0)
    check("con estado reciente, no barre", not corresponde2, razon2)

    corresponde3, _ = recolector.should_scan(hermes_home=home, now=5000.0)
    check("pasado el intervalo, barre", corresponde3)


def test_un_barrido_no_pisa_el_estado_de_otro_home() -> None:
    """Dos homes aislados no se ven entre sí: es lo que hace usable el aislamiento."""
    home_a = home_temporal()
    home_b = home_temporal()

    recolector.write_state({"last_scan_ts": 999.0}, hermes_home=home_a)

    check("el otro home sigue sin estado",
          recolector.read_state(hermes_home=home_b) == {})
    check("el suyo sí lo tiene",
          recolector.read_state(hermes_home=home_a).get("last_scan_ts") == 999.0)


def main() -> int:
    pruebas = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for prueba in pruebas:
        try:
            prueba()
        except Exception as exc:  # noqa: BLE001 — un test que revienta es un fallo, no un crash
            FAILED.append(f"{prueba.__name__} REVENTÓ: {type(exc).__name__}: {exc}")

    print(f"{PASSED} pasaron, {len(FAILED)} fallaron")
    for linea in FAILED:
        print("  FALLO " + linea)
    return 1 if FAILED else 0


if __name__ == "__main__":
    print("=== estado del recolector: dónde vive y cómo se escribe ===")
    raise SystemExit(main())
