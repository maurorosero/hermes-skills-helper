"""Tests de la señal de uso — la etapa 1 nueva.

Todo determinista: fixtures en disco, reloj inyectado, sin modelo y sin red.
``tests/medicion_uso.py`` corre lo mismo contra los datos reales de Mauro.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import uso  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(nombre: str, condicion: bool, detalle: str = "") -> None:
    global PASSED
    if condicion:
        PASSED += 1
    else:
        FAILED.append(f"{nombre}{(': ' + detalle) if detalle else ''}")


DIA = 86400.0
AHORA = 1_800_000_000.0


def iso(dias_atras: float) -> str:
    """Timestamp ISO en UTC, ``dias_atras`` días antes de AHORA."""
    from datetime import datetime, timezone

    marca = datetime.fromtimestamp(AHORA - dias_atras * DIA, tz=timezone.utc)
    return marca.isoformat().replace("+00:00", "Z")


def con_home(registros: dict, *, archivos: dict | None = None):
    """Hogar temporal con ``.usage.json`` y opcionalmente SKILL.md de tamaños dados."""
    tmp = Path(tempfile.mkdtemp(prefix="uso-test-"))
    skills = tmp / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    (skills / ".usage.json").write_text(json.dumps(registros), encoding="utf-8")
    for nombre, tamano in (archivos or {}).items():
        carpeta = skills / nombre
        carpeta.mkdir(parents=True, exist_ok=True)
        (carpeta / "SKILL.md").write_text("x" * tamano, encoding="utf-8")
    return tmp


# -- Fechas y enteros: la distinción entre "no medí" y "medí cero" ---------------------

def test_parse_iso_acepta_z_y_naive() -> None:
    con_z = uso._parse_iso("2026-09-28T10:00:00Z")
    sin_z = uso._parse_iso("2026-09-28T10:00:00")
    check("ISO con Z se parsea", con_z is not None and con_z > 0)
    check("ISO sin zona se interpreta UTC, igual que con Z", con_z == sin_z)


def test_parse_iso_rechaza_basura() -> None:
    for valor in [None, "", "   ", "ayer", 12345, [], {}]:
        check(f"parse_iso({valor!r}) es None", uso._parse_iso(valor) is None)


def test_entero_no_rellena_con_cero() -> None:
    check("entero válido pasa", uso._entero(7) == 7)
    check("bool NO es entero", uso._entero(True) is None)
    check("negativo es None", uso._entero(-3) is None)
    check("texto es None", uso._entero("7") is None)
    check("None es None", uso._entero(None) is None)


# -- Las cuatro señales ---------------------------------------------------------------

def test_sin_registro_no_es_cero_usos() -> None:
    """Sin registro no se puede afirmar nada: ni sano ni sin_usar."""
    tmp = con_home({})
    rep = uso.medir(hermes_home=tmp, ahora=AHORA)
    check("sin registros → reporte vacío", len(rep.skills) == 0)
    check("sin registros → 0 activos", rep.total_activos == 0)


def test_sin_usar_solo_si_es_viejo() -> None:
    """Creado y nunca usado, pero reciente, no es señal: la tarea puede no haber llegado."""
    registros = {
        "viejo-sin-usar": {"use_count": 0, "created_at": iso(30), "patch_count": 0},
        "nuevo-sin-usar": {"use_count": 0, "created_at": iso(2), "patch_count": 0},
    }
    rep = uso.medir(hermes_home=con_home(registros), ahora=AHORA)
    estados = {s.nombre: s.estado for s in rep.skills}
    check("creado hace 30 d y sin usar → sin_usar", estados["viejo-sin-usar"] == "sin_usar")
    check("creado hace 2 d y sin usar → sano", estados["nuevo-sin-usar"] == "sano")
    check("el motivo dice la edad",
          any("30 d" in s.motivo for s in rep.sin_usar), str([s.motivo for s in rep.sin_usar]))


def test_sin_cambio_necesita_usos() -> None:
    """0 parches con pocos usos es casualidad; con muchos, es señal."""
    registros = {
        "muy-usado-sin-parches": {"use_count": 93, "patch_count": 0,
                                  "created_at": iso(60), "last_used_at": iso(1)},
        "poco-usado-sin-parches": {"use_count": 2, "patch_count": 0,
                                   "created_at": iso(60), "last_used_at": iso(1)},
    }
    rep = uso.medir(hermes_home=con_home(registros), ahora=AHORA)
    estados = {s.nombre: s.estado for s in rep.skills}
    check("93 usos y 0 parches → sin_cambio", estados["muy-usado-sin-parches"] == "sin_cambio")
    check("2 usos y 0 parches → sano", estados["poco-usado-sin-parches"] == "sano")


def test_sin_reuso_detecta_parche_no_reusado() -> None:
    """Parcheado, sin reuso posterior y ya pasó la ventana → el parche pudo romperlo."""
    registros = {
        "parcheado-no-reusado": {
            "use_count": 20, "patch_count": 3, "patch_generation": 3,
            "last_reused_patch_generation": 1,
            "last_patched_at": iso(30), "last_used_at": iso(29),
            "created_at": iso(200),
        },
        "parcheado-y-reusado": {
            "use_count": 20, "patch_count": 3, "patch_generation": 3,
            "last_reused_patch_generation": 3,
            "last_patched_at": iso(30), "last_used_at": iso(1),
            "created_at": iso(200),
        },
    }
    rep = uso.medir(hermes_home=con_home(registros), ahora=AHORA)
    estados = {s.nombre: s.estado for s in rep.skills}
    check("parcheado y no reusado → sin_reuso",
          estados["parcheado-no-reusado"] == "sin_reuso")
    check("parcheado y reusado → sano", estados["parcheado-y-reusado"] == "sano")


def test_sin_reuso_todavia_sin_veredicto() -> None:
    """Parcheado ayer: no hay veredicto. Declararlo fallo sería adivinar."""
    registros = {
        "parcheado-ayer": {
            "use_count": 20, "patch_count": 1, "patch_generation": 1,
            "last_reused_patch_generation": 0,
            "last_patched_at": iso(1), "created_at": iso(200),
        },
    }
    rep = uso.medir(hermes_home=con_home(registros), ahora=AHORA)
    check("parcheado hace 1 d → sano, sin veredicto", rep.skills[0].estado == "sano")
    check("el motivo lo dice", "sin veredicto" in rep.skills[0].motivo, rep.skills[0].motivo)


def test_hinchado_por_tamano_y_por_parches() -> None:
    """Dos caminos al mismo estado: cuerpo enorme, o parches sin fin."""
    registros = {
        "cuerpo-enorme": {"use_count": 50, "patch_count": 1, "patch_generation": 1,
                          "last_reused_patch_generation": 1,
                          "last_patched_at": iso(2), "created_at": iso(200)},
        "muchos-parches": {"use_count": 50, "patch_count": 189, "patch_generation": 1,
                           "last_reused_patch_generation": 0,
                           "last_patched_at": iso(100), "created_at": iso(200)},
    }
    rep = uso.medir(hermes_home=con_home(registros, archivos={"cuerpo-enorme": 100_000}),
                    ahora=AHORA)
    estados = {s.nombre: s.estado for s in rep.skills}
    check("cuerpo de 100 KB → hinchado", estados["cuerpo-enorme"] == "hinchado")
    check("189 parches → hinchado", estados["muchos-parches"] == "hinchado")
    check("el motivo nombra el tamaño",
          any("KB" in s.motivo for s in rep.hinchado))


def test_tamano_se_mide_del_archivo_real() -> None:
    """El tamaño sale del SKILL.md, no del registro: el registro no lo guarda."""
    registros = {"con-archivo": {"use_count": 1, "patch_count": 0, "created_at": iso(1)}}
    rep = uso.medir(hermes_home=con_home(registros, archivos={"con-archivo": 1234}),
                    ahora=AHORA)
    check("tamaño leído del disco", rep.skills[0].tamano_bytes == 1234,
          str(rep.skills[0].tamano_bytes))
    check("ruta registrada", rep.skills[0].ruta is not None)


def test_archivado_sale_del_reporte_por_defecto() -> None:
    registros = {
        "archivado": {"use_count": 0, "patch_count": 0, "created_at": iso(90),
                      "archived_at": iso(10)},
        "activo": {"use_count": 5, "patch_count": 0, "created_at": iso(90)},
    }
    rep = uso.medir(hermes_home=con_home(registros), ahora=AHORA)
    check("sin archivar en el reporte por defecto",
          all(s.nombre != "archivado" for s in rep.skills))
    rep2 = uso.medir(hermes_home=con_home(registros), ahora=AHORA, incluir_archivados=True)
    check("con incluir_archivados aparece", any(s.nombre == "archivado" for s in rep2.skills))
    check("los activos se cuentan aparte", rep2.total_activos == 1, str(rep2.total_activos))


def test_orden_de_precedencia_entre_senales() -> None:
    """Un skill que nunca se usó Y tiene 189 parches: se reporta por lo primero.

    El orden es de urgencia. Un skill que nadie usa es más accionable que uno que creció.
    """
    registros = {
        "nunca-usado-pero-hinchado": {
            "use_count": 0, "patch_count": 189, "patch_generation": 1,
            "created_at": iso(200), "last_patched_at": iso(100),
        },
    }
    rep = uso.medir(hermes_home=con_home(registros), ahora=AHORA)
    check("gana sin_usar sobre hinchado", rep.skills[0].estado == "sin_usar",
          rep.skills[0].estado)


# -- Robusteza ------------------------------------------------------------------------

def test_registro_corrupto_no_rompe() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="uso-test-"))
    skills = tmp / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    (skills / ".usage.json").write_text("{no es json", encoding="utf-8")
    rep = uso.medir(hermes_home=tmp, ahora=AHORA)
    check("json roto → reporte vacío, sin excepción", len(rep.skills) == 0)


def test_json_que_no_es_dict_no_rompe() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="uso-test-"))
    skills = tmp / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    (skills / ".usage.json").write_text("[1, 2, 3]", encoding="utf-8")
    rep = uso.medir(hermes_home=tmp, ahora=AHORA)
    check("lista en vez de dict → reporte vacío", len(rep.skills) == 0)


def test_sin_usage_json_no_rompe() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="uso-test-"))
    rep = uso.medir(hermes_home=tmp, ahora=AHORA)
    check("sin archivo → reporte vacío", len(rep.skills) == 0)


def test_registro_con_campos_rarsos_no_rompe() -> None:
    registros = {
        "campos-raros": {"use_count": "muchos", "patch_count": None,
                         "last_used_at": "ayer", "created_at": 42},
    }
    rep = uso.medir(hermes_home=con_home(registros), ahora=AHORA)
    check("campos inválidos no lanzan", len(rep.skills) == 1)
    check("use_count ilegible queda None", rep.skills[0].use_count is None)
    check("y el estado lo declara", rep.skills[0].estado != "sano",
          rep.skills[0].estado)


def test_trustworthy_es_false_si_la_mayoria_no_tiene_registro() -> None:
    """Un reporte vacío por no poder leer no es "no hay nada"."""
    rep_vacio = uso.UsoReport(skills=(), sin_registro=0, total_activos=0,
                              scanned_until=AHORA)
    check("sin activos → confiable", rep_vacio.trustworthy)

    rep_ok = uso.UsoReport(skills=(), sin_registro=0, total_activos=10,
                           scanned_until=AHORA)
    check("10 activos, 0 sin registro → confiable", rep_ok.trustworthy)

    rep_malo = uso.UsoReport(skills=(), sin_registro=11, total_activos=10,
                             scanned_until=AHORA)
    check("más sin registro que activos → NO confiable", not rep_malo.trustworthy)


def test_resumen_menciona_los_cuatro_cubos() -> None:
    registros = {
        "a": {"use_count": 93, "patch_count": 0, "created_at": iso(60)},
        "b": {"use_count": 0, "patch_count": 0, "created_at": iso(30)},
    }
    rep = uso.medir(hermes_home=con_home(registros), ahora=AHORA)
    texto = uso.resumen(rep)
    for palabra in ["sin usar", "sin cambio", "sin reuso", "hinchados", "activos"]:
        check(f"el resumen nombra '{palabra}'", palabra in texto, texto)


def test_solo_lectura_no_toca_nada() -> None:
    """El recolector no escribe: ni en el registro ni en los skills."""
    registros = {"x": {"use_count": 5, "patch_count": 0, "created_at": iso(10)}}
    tmp = con_home(registros, archivos={"x": 500})
    usage = tmp / "skills" / ".usage.json"
    skill_md = tmp / "skills" / "x" / "SKILL.md"
    antes_u = usage.stat().st_mtime_ns
    antes_s = skill_md.stat().st_mtime_ns
    contenido_antes = skill_md.read_text(encoding="utf-8")

    uso.medir(hermes_home=tmp, ahora=AHORA)

    check("el .usage.json no se toca", usage.stat().st_mtime_ns == antes_u)
    check("el SKILL.md no se toca", skill_md.stat().st_mtime_ns == antes_s)
    check("el contenido del SKILL.md intacto",
          skill_md.read_text(encoding="utf-8") == contenido_antes)
    check("no se crearon archivos nuevos en skills/",
          sorted(p.name for p in (tmp / "skills").iterdir()) == [".usage.json", "x"],
          str(sorted(p.name for p in (tmp / "skills").iterdir())))


# -- Umbrales inyectables -------------------------------------------------------------

def test_umbrales_son_inyectables() -> None:
    registros = {"s": {"use_count": 0, "patch_count": 0, "created_at": iso(5)}}
    rep_default = uso.medir(hermes_home=con_home(registros), ahora=AHORA)
    check("con 14 d por defecto, 5 d es sano", rep_default.skills[0].estado == "sano")
    rep_estricto = uso.medir(hermes_home=con_home(registros), ahora=AHORA,
                             sin_usar_dias=3.0)
    check("con 3 d, 5 d es sin_usar", rep_estricto.skills[0].estado == "sin_usar")


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
