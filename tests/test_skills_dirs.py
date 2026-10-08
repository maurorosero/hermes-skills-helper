"""Tests de ``_skills_dirs()`` — las fuentes donde el plugin busca el SKILL.md.

Este archivo existe por un defecto real: el registro de uso (``.usage.json``) enumera
nombres, no rutas, y el plugin sólo miraba dos directorios (los externos configurados y
``<home>/skills``). Todo skill que no viviera ahí se medía como si no existiera — un
"fantasma" —, aunque el archivo estuviera en disco:

    audiocraft-audio-generation   existe en core/optional-skills
    segment-anything-model        existe en core/optional-skills

Los dos se reportaban como inexistentes. La causa no era el registro: era la lista de
directorios, que no incluía los **perfiles de los agentes** ni los **optional-skills del
core**.

Hermético a propósito: el arnés entero (raíz, perfiles, core y optional-skills) es un
fixture en un temporal, y ``hermes_constants`` se reemplaza por un stub mientras dura cada
prueba. Un test que leyera el arnés real diría cosas distintas según la máquina — y el
que importa acá es el contrato, no la foto de un día.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

# El plugin usa imports relativos; cargarlo como paquete es lo que hace el arnés.
_spec = importlib.util.spec_from_file_location(
    "hermes_skills_helper", RAIZ / "__init__.py", submodule_search_locations=[str(RAIZ)])
plugin = importlib.util.module_from_spec(_spec)
sys.modules["hermes_skills_helper"] = plugin
assert _spec is not None and _spec.loader is not None
_spec.loader.exec_module(plugin)

PASSED = 0
FAILED: list[str] = []


def check(nombre: str, condicion: bool, detalle: str = "") -> None:
    global PASSED
    if condicion:
        PASSED += 1
    else:
        FAILED.append(f"{nombre}{(': ' + detalle) if detalle else ''}")


class Arnes:
    """Un arnés de mentira: raíz con perfiles, core y optional-skills.

    ``get_default_hermes_root()`` devuelve la **raíz** cuando el hogar es un perfil
    (``<raíz>/profiles/<n>``). Esa es justo la pieza que el fixture tiene que reproducir:
    parado en un perfil, el plugin tiene que ver a los DEMÁS perfiles.
    """

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="skills-dirs-test-"))
        (self.tmp / "skills").mkdir(parents=True)

        self.perfiles: list[Path] = []
        for nombre in ("alfa", "beta"):
            sd = self.tmp / "profiles" / nombre / "skills"
            sd.mkdir(parents=True)
            self.perfiles.append(sd)

        self.core = self.tmp / "hermes-agent"
        (self.core / "skills").mkdir(parents=True)
        self.optional = self.core / "optional-skills"
        self.optional.mkdir(parents=True)

        self._previo = sys.modules.get("hermes_constants")

    def instalar_stub(self) -> None:
        """``hermes_constants`` de mentira, apuntando al fixture."""
        stub = types.ModuleType("hermes_constants")
        stub.__file__ = str(self.core / "hermes_constants.py")
        stub.get_default_hermes_root = lambda: self.tmp
        stub.get_optional_skills_dir = lambda default=None: (
            self.optional if default is None else Path(default))
        sys.modules["hermes_constants"] = stub

    def cerrar(self) -> None:
        if self._previo is None:
            sys.modules.pop("hermes_constants", None)
        else:
            sys.modules["hermes_constants"] = self._previo


# -- Las dos fuentes que faltaban -----------------------------------------------------

def test_los_perfiles_entran_en_los_dirs() -> None:
    """El caso que producía los fantasmas: un skill que sólo vive en un perfil."""
    a = Arnes()
    a.instalar_stub()
    try:
        dirs = plugin._skills_dirs(a.tmp)
        for esperado in a.perfiles:
            check(f"el skills/ del perfil {esperado.parts[-3]} está en los dirs",
                  esperado.resolve() in dirs, f"dirs={dirs}")
    finally:
        a.cerrar()


def test_los_perfiles_se_ven_desde_un_perfil() -> None:
    """Con el hogar siendo un perfil, los OTROS perfiles también se resuelven.

    Es donde importa ``get_default_hermes_root()``: derivar la raíz del path del hogar
    dejaría afuera a los demás perfiles, y seguirían siendo fantasmas.
    """
    a = Arnes()
    a.instalar_stub()
    try:
        hogar = a.tmp / "profiles" / "alfa"
        dirs = plugin._skills_dirs(hogar)
        beta = (a.tmp / "profiles" / "beta" / "skills").resolve()
        check("desde el perfil 'alfa' se ve el skills/ de 'beta'", beta in dirs, f"dirs={dirs}")
        check("y también el de su propio perfil",
              hogar.joinpath("skills").resolve() in dirs, f"dirs={dirs}")
    finally:
        a.cerrar()


def test_los_optional_skills_del_core_entran() -> None:
    """El otro fantasma real: el skill está en optional-skills y el registro lo nombra."""
    a = Arnes()
    a.instalar_stub()
    try:
        dirs = plugin._skills_dirs(a.tmp)
        extra = plugin._dir_optional_skills()
        check("_dir_optional_skills() devuelve el directorio del core",
              extra == [a.optional.resolve()], f"{extra}")
        check("y está en los dirs del plugin", a.optional.resolve() in dirs, f"dirs={dirs}")
    finally:
        a.cerrar()


# -- Lo que el arreglo NO puede romper ------------------------------------------------

def test_los_dirs_no_traen_directorios_inexistentes() -> None:
    """Una fuente que no existe no se agenda: buscar ahí sería trabajo que no da nada."""
    a = Arnes()
    a.instalar_stub()
    try:
        dirs = plugin._skills_dirs(a.tmp)
        for d in dirs:
            check(f"{d.name} existe", d.is_dir(), f"se agendó {d}, que no existe")
    finally:
        a.cerrar()


def test_sin_duplicados_y_las_fuentes_nuevas_al_final() -> None:
    """El orden decide qué archivo se mide.

    El arnés resuelve local primero (``agent/skill_utils.py:420``, *"local ... first"*), y
    lo que se mide tiene que ser el archivo que realmente se carga. Por eso el default va
    antes que las dos fuentes nuevas: éstas sólo aportan lo que nadie resolvía, y ningún
    tamaño ya medido cambia de dueño.
    """
    a = Arnes()
    a.instalar_stub()
    try:
        dirs = plugin._skills_dirs(a.tmp)
        check("sin duplicados", len(dirs) == len(set(dirs)), f"dirs={dirs}")
        por_defecto = (a.tmp / "skills").resolve()
        check("el skills/ del hogar está", por_defecto in dirs, f"dirs={dirs}")
        nuevas = plugin._dirs_de_perfiles(a.tmp) + plugin._dir_optional_skills()
        check("las fuentes nuevas van después del default",
              all(dirs.index(d) > dirs.index(por_defecto) for d in nuevas), f"dirs={dirs}")
    finally:
        a.cerrar()


def test_sin_hermes_constants_el_barrido_sigue() -> None:
    """Un helper del host que falte no puede dejar al plugin sin directorios.

    Se degrada a la lista que ya tenía, y el fallo se declara por log en vez de propagarse:
    el barrido corre en ``on_session_end``, donde una excepción le costaría el turno a Mauro.
    """
    a = Arnes()
    a.cerrar()  # sin stub: el import real puede o no existir, pero no puede lanzar
    try:
        extra_perfiles = plugin._dirs_de_perfiles(a.tmp)
        extra_opcional = plugin._dir_optional_skills()
        check("_dirs_de_perfiles() devuelve lista", isinstance(extra_perfiles, list),
              f"{type(extra_perfiles).__name__}")
        check("_dir_optional_skills() devuelve lista", isinstance(extra_opcional, list),
              f"{type(extra_opcional).__name__}")
        check("_dirs_de_perfiles() con una raíz inexistente devuelve lista",
              isinstance(plugin._dirs_de_perfiles(a.tmp / "no-existe"), list))
    finally:
        a.cerrar()


def main() -> int:
    pruebas = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for prueba in pruebas:
        try:
            prueba()
        except Exception as exc:  # noqa: BLE001
            FAILED.append(f"{prueba.__name__} lanzó {type(exc).__name__}: {exc}")
    print(f"{PASSED} pasaron, {len(FAILED)} fallaron")
    for f in FAILED:
        print("  FALLO:", f)
    if not FAILED:
        print("todo verde")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
