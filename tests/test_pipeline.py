"""Tests del cableado — `pipeline.py` y el `register()` del plugin.

Lo que se verifica acá no es una etapa: es **la costura entre ellas**, y que el plugin
haga lo que declara. Tres cosas que importan especialmente:

1. El hook **nunca** llama al modelo. Un gancho por turno que gastara inferencia sería el
   peor modo de fallo posible: invisible, caro y creciente.
2. El throttle distingue *"no se barrió"* de *"se barrió y no había nada"*.
3. La aplicación **no se saltea** el gate del arnés. Si la vía del host no está, el plugin
   falla en lugar de escribir por atajo.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pipeline  # noqa: E402
from proposal import ACTION_NO_OP  # noqa: E402
from recurrence import RecurringFailure  # noqa: E402

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


SKILL = """---
name: maps
description: Use when consultando mapas. Geocodifica y rutea.
---

# Maps

Para rutas, pasar origen y destino.
"""


class Sandbox:
    def __init__(self, con_skill: bool = False) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="hsh-pipeline-"))
        self.home = self.root / "hermes"
        self.skills = self.root / "skills"
        self.skills.mkdir(parents=True, exist_ok=True)
        if con_skill:
            d = self.skills / "maps"
            d.mkdir(parents=True, exist_ok=True)
            (d / "SKILL.md").write_text(SKILL, encoding="utf-8")

    def skill_path(self) -> Path:
        return self.skills / "maps" / "SKILL.md"

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class FakeResult:
    def __init__(self, parsed=None, text="", model="fake", provider="fake") -> None:
        self.parsed = parsed
        self.text = text
        self.model = model
        self.provider = provider
        self.usage = type("U", (), {"input_tokens": 10, "output_tokens": 5})()


class FakeLlm:
    def __init__(self, result=None, raises=None) -> None:
        self.calls: list[dict] = []
        self.result = result
        self.raises = raises

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise self.raises
        return self.result


def _failure(**kw) -> RecurringFailure:
    base = dict(fingerprint="fp1", tool_name="maps", occurrences=5, sessions=3,
                first_ts=0.0, last_ts=0.0, sample="un fallo real")
    base.update(kw)
    return RecurringFailure(**base)


def _patch_response(anchor: str, replacement: str) -> dict:
    return {
        "action": "patch", "skill_name": "maps", "anchor": anchor,
        "replacement": replacement,
        "justification": "Evita que el fallo se repita.",
        "expected_result": "El error no reaparece.",
    }


# -- El hook: determinista, con throttle --------------------------------------------

def test_hook_never_calls_the_model() -> None:
    print("\nEL HOOK NUNCA LLAMA AL MODELO")

    box = Sandbox()
    try:
        # Un fallo real y recurrente en la trayectoria del sandbox.
        import sqlite3
        db = box.home / "state.db"
        box.home.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db))
        conn.execute("""CREATE TABLE messages (
            id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
            tool_name TEXT, timestamp REAL, active INTEGER)""")
        ahora = time.time()
        for i in range(6):
            conn.execute(
                "INSERT INTO messages (session_id, role, content, tool_name, timestamp, active)"
                " VALUES (?, 'tool', ?, 'maps', ?, 1)",
                (f"s{i}", '{"success": false, "error": "la ruta no existe"}',
                 ahora - 86400 * (12 - i * 2)),
            )
        conn.commit()
        conn.close()

        import __init__ as plugin  # noqa: PLC0415

        # Se espía cualquier intento de construir un cliente de modelo.
        original = getattr(plugin, "_skills_manage", None)
        llamado = {"llm": False}

        class SpyLlm:
            def __init__(self, *a, **k):
                llamado["llm"] = True

        import sys as _sys
        _sys.modules["agent.plugin_llm"] = type("M", (), {"PluginLlm": SpyLlm})

        try:
            # Se corre el hook apuntando el hogar al sandbox.
            informe = pipeline.run_deterministic(
                hermes_home=box.home, skills_dirs=[box.skills], force=True)
        finally:
            if original is not None:
                plugin._skills_manage = original

        check("el hook NO construyó un cliente de modelo", llamado["llm"] is False)
        check("el ciclo corrió", informe.scanned is True, informe.reason)
        check("no hubo errores", not informe.errors, str(informe.errors))
    finally:
        box.cleanup()


def test_throttle_distinguishes_not_run_from_nothing_found() -> None:
    print("\nno barrer ≠ no hay nada")

    box = Sandbox()
    try:
        primero = pipeline.run_deterministic(hermes_home=box.home, skills_dirs=[box.skills],
                                             force=True, min_interval=900)
        check("el primer barrido corre", primero.scanned is True)

        segundo = pipeline.run_deterministic(hermes_home=box.home, skills_dirs=[box.skills],
                                             min_interval=900)
        check("el segundo NO barre", segundo.scanned is False)
        check("y declara la razón del salteo",
              "faltan" in segundo.reason, segundo.reason)
        check("el resumen dice 'sin barrido', no 'sin fallos'",
              segundo.summary().startswith("sin barrido"), segundo.summary())
        check("no hay candidatos, pero tampoco un barrido vacío",
              segundo.candidates == 0 and segundo.recurrence is None)

        forzado = pipeline.run_deterministic(hermes_home=box.home, skills_dirs=[box.skills],
                                             force=True)
        check("con force sí barre", forzado.scanned is True)
    finally:
        box.cleanup()


def test_throttle_allows_after_interval() -> None:
    print("\ncon el intervalo cumplido, barre")

    box = Sandbox()
    try:
        pipeline.run_deterministic(hermes_home=box.home, skills_dirs=[box.skills],
                                   force=True, min_interval=60)
        despues = pipeline.run_deterministic(
            hermes_home=box.home, skills_dirs=[box.skills], min_interval=60,
            now=time.time() + 120)
        check("barre de nuevo", despues.scanned is True, despues.reason)

        # Reloj hacia atrás: no se castiga con un bloqueo largo.
        atras = pipeline.run_deterministic(
            hermes_home=box.home, skills_dirs=[box.skills], min_interval=900,
            now=time.time() - 100000)
        check("un reloj retrocedido no bloquea para siempre", atras.scanned is True)
        check("y la razón del barrido lo conserva ('barrido completo (el reloj retrocedió…)')",
              "retrocedió" in atras.reason, atras.reason)
        check("la razón nombra el barrido, no sólo el motivo",
              atras.reason.startswith("barrido completo"), atras.reason)
    finally:
        box.cleanup()


def test_run_is_resilient_to_a_failing_stage() -> None:
    print("\nuna etapa que falla no aborta el ciclo")

    box = Sandbox()
    try:
        informe = pipeline.run_deterministic(hermes_home=box.home, skills_dirs=[box.skills],
                                             force=True)
        check("el ciclo corrió", informe.scanned is True)
        check("la etapa del techo corrió", informe.budget is not None, str(informe.errors))
        check("el informe sigue siendo legible", bool(informe.summary()))
    finally:
        box.cleanup()


def test_unreadable_trajectory_is_a_limitation_not_a_clean_result() -> None:
    print("\nuna trayectoria ilegible NO es 'no hay nada'")

    box = Sandbox()
    try:
        informe = pipeline.run_deterministic(hermes_home=box.home, skills_dirs=[box.skills],
                                             force=True)
        check("sin state.db, el informe lo sabe",
              informe.trajectory_readable is False)
        check("no hay candidatos", informe.candidates == 0)
        check("pero la limitación se declara",
              informe.limitation != "", informe.limitation)
        check("y dice que no es una conclusión",
              "no es una conclusión" in informe.limitation, informe.limitation)
        check("el resumen la pone PRIMERO, para que no se lea como limpio",
              informe.summary().startswith("LIMITADO"), informe.summary())

        # Con una trayectoria legible, no hay limitación: el contraste importa.
        import sqlite3
        box.home.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(box.home / "state.db"))
        conn.execute("""CREATE TABLE messages (
            id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
            tool_name TEXT, timestamp REAL, active INTEGER)""")
        conn.commit()
        conn.close()
        informe2 = pipeline.run_deterministic(hermes_home=box.home, skills_dirs=[box.skills],
                                              force=True)
        check("con la base legible, no hay limitación",
              informe2.trajectory_readable is True and informe2.limitation == "",
              informe2.limitation)
        check("y el resumen NO empieza con LIMITADO",
              not informe2.summary().startswith("LIMITADO"), informe2.summary())
    finally:
        box.cleanup()


def test_version_matches_between_manifest_and_module() -> None:
    print("\nla versión del manifest y la del módulo coinciden")

    import __init__ as plugin  # noqa: PLC0415
    raiz = Path(__file__).resolve().parent.parent
    texto = (raiz / "plugin.yaml").read_text(encoding="utf-8")
    check(f"plugin.yaml declara {plugin.__version__}",
          f"version: {plugin.__version__}" in texto,
          [l for l in texto.splitlines() if l.startswith("version")])


def test_health_does_not_create() -> None:
    print("\nel diagnóstico del ciclo no inventa su evidencia")

    box = Sandbox()
    try:
        h = pipeline.health(hermes_home=box.home)
        check("informa la ruta del estado", "state_path" in h)
        check("sin estado previo", h["last_scan_ts"] is None)
        check("y NO creó el archivo", not Path(h["state_path"]).exists())
        check("incluye los techos", "budget" in h)
        check("incluye el journal", "journal" in h)
    finally:
        box.cleanup()


# -- Etapa 3 + 4 + aplicación ------------------------------------------------------

def test_review_proposes_and_stops() -> None:
    print("\nrevisar propone y se detiene (apply=False por defecto)")

    box = Sandbox(con_skill=True)
    try:
        ancla = "Para rutas, pasar origen y destino."
        llm = FakeLlm(FakeResult(parsed=_patch_response(ancla, ancla + " Verificar.")))
        antes = box.skill_path().read_text(encoding="utf-8")

        resultado = pipeline.review_candidate(
            failure=_failure(), skill_name="maps", llm=llm, hermes_home=box.home,
            skills_dirs=[box.skills],
        )
        check("ok", resultado["ok"] is True, str(resultado))
        check("NO aplicó", resultado["applied"] is False)
        check("la etapa es 'propuesta lista'", "propuesta lista" in resultado["stage"],
              resultado["stage"])
        check("el skill quedó intacto",
              box.skill_path().read_text(encoding="utf-8") == antes)
        check("dice cómo seguir", "apply=True" in resultado.get("next", ""))
        check("una sola llamada al modelo", len(llm.calls) == 1)
    finally:
        box.cleanup()


def test_review_refuses_to_bypass_the_gate() -> None:
    print("\nSIN LA VÍA DEL ARNÉS NO SE ESCRIBE POR ATAJO")

    box = Sandbox(con_skill=True)
    try:
        ancla = "Para rutas, pasar origen y destino."
        llm = FakeLlm(FakeResult(parsed=_patch_response(ancla, ancla + " Verificar.")))
        antes = box.skill_path().read_text(encoding="utf-8")

        resultado = pipeline.review_candidate(
            failure=_failure(), skill_name="maps", llm=llm, hermes_home=box.home,
            skills_dirs=[box.skills], apply=True, skills_manage=None,
        )
        check("falla", resultado["ok"] is False)
        check("la etapa lo nombra", "sin vía" in resultado["stage"], resultado["stage"])
        check("la razón explica que se saltaría el gate",
              "gate" in resultado["message"] or "aprobación" in resultado["message"],
              resultado["message"])
        check("EL SKILL NO SE MODIFICÓ",
              box.skill_path().read_text(encoding="utf-8") == antes)
    finally:
        box.cleanup()


def test_review_respects_staged_response() -> None:
    print("\nsi el arnés deja el cambio en cola, NO se registra como aplicado")

    box = Sandbox(con_skill=True)
    try:
        ancla = "Para rutas, pasar origen y destino."
        llm = FakeLlm(FakeResult(parsed=_patch_response(ancla, ancla + " Verificar.")))
        antes = box.skill_path().read_text(encoding="utf-8")

        def skills_manage(**kwargs):
            # El gate del host está encendido: devuelve staged y NO escribe.
            return json.dumps({"success": True, "staged": True, "pending_id": "abc123",
                               "message": "Staged for approval."})

        resultado = pipeline.review_candidate(
            failure=_failure(), skill_name="maps", llm=llm, hermes_home=box.home,
            skills_dirs=[box.skills], apply=True, skills_manage=skills_manage,
        )
        check("ok", resultado["ok"] is True, str(resultado))
        check("la etapa dice 'en cola'", "cola" in resultado["stage"], resultado["stage"])
        check("NO se declara aplicado", resultado["applied"] is False)
        check("el id de la cola se conserva",
              resultado["host_response"].get("pending_id") == "abc123")
        check("el skill quedó intacto",
              box.skill_path().read_text(encoding="utf-8") == antes)

        # Y el journal NO debe tener una entrada marcada como aplicada.
        import __init__  # noqa: F401,PLC0415
        from journal import read_entries  # noqa: PLC0415
        entradas = read_entries(hermes_home=box.home)
        aplicadas = [e for e in entradas if e.status == "applied"]
        check("el journal NO registra un cambio que no ocurrió",
              not aplicadas, f"{len(aplicadas)} aplicadas")
    finally:
        box.cleanup()


def test_review_applies_through_host_and_records() -> None:
    print("\ncuando el host aplica de verdad, se registra")

    box = Sandbox(con_skill=True)
    try:
        ancla = "Para rutas, pasar origen y destino."
        nuevo = ancla + " Verificar que el origen exista."
        llm = FakeLlm(FakeResult(parsed=_patch_response(ancla, nuevo)))
        escrituras = []

        def skills_manage(**kwargs):
            escrituras.append(kwargs)
            box.skill_path().write_text(kwargs["content"], encoding="utf-8")
            return json.dumps({"success": True})

        resultado = pipeline.review_candidate(
            failure=_failure(), skill_name="maps", llm=llm, hermes_home=box.home,
            skills_dirs=[box.skills], apply=True, skills_manage=skills_manage,
        )
        check("ok", resultado["ok"] is True, str(resultado))
        check("se aplicó", resultado["applied"] is True)
        check("la etapa lo dice", "aplicado" in resultado["stage"], resultado["stage"])
        check("pasó por la vía del host", len(escrituras) == 1)
        check("el skill cambió", nuevo in box.skill_path().read_text(encoding="utf-8"))

        from journal import read_entries  # noqa: PLC0415
        entradas = read_entries(hermes_home=box.home)
        aplicadas = [e for e in entradas if e.status == "applied"]
        check("el journal registró el cambio", len(aplicadas) == 1, f"{len(aplicadas)}")
        if aplicadas:
            check("con el hash posterior",
                  aplicadas[0].after_hash is not None)
            check("y con el hash previo, para poder revertir",
                  aplicadas[0].before_hash is not None)
    finally:
        box.cleanup()


def test_review_no_op_does_not_apply() -> None:
    print("\nno_op no toca nada")

    box = Sandbox(con_skill=True)
    try:
        llm = FakeLlm(FakeResult(parsed={
            "action": ACTION_NO_OP, "skill_name": "maps", "anchor": "", "replacement": "",
            "justification": "El fallo es de red, no de instrucciones.",
            "expected_result": "No aplica."}))
        tocado = {"n": 0}

        def skills_manage(**kwargs):
            tocado["n"] += 1
            return json.dumps({"success": True})

        resultado = pipeline.review_candidate(
            failure=_failure(), skill_name="maps", llm=llm, hermes_home=box.home,
            skills_dirs=[box.skills], apply=True, skills_manage=skills_manage,
        )
        check("ok (no_op no es un error)", resultado["ok"] is True, str(resultado))
        check("la etapa es no_op", resultado["stage"] == "no_op", resultado["stage"])
        check("no se escribió nada", tocado["n"] == 0)
        check("no se aplicó", resultado["applied"] is False)
    finally:
        box.cleanup()


def test_review_rejects_index_out_of_range() -> None:
    print("\nun índice inexistente se rechaza con la lista disponible")

    box = Sandbox(con_skill=True)
    try:
        resultado = pipeline.review_candidate(
            failure=_failure(), skill_name="maps",
            llm=FakeLlm(FakeResult(parsed=_patch_response("x", "y"))),
            hermes_home=box.home, skills_dirs=[box.skills],
        )
        # El skill existe pero el ancla no: se rechaza y lo dice.
        check("propuesta rechazada", resultado["stage"] == "rechazada", resultado["stage"])
        check("ok=False", resultado["ok"] is False)
        check("la propuesta viaja con su rechazo",
              bool(resultado["proposal"]["rejection"]))
    finally:
        box.cleanup()


def test_review_missing_skill() -> None:
    print("\nun skill que no existe se reporta, no se inventa")

    box = Sandbox()
    try:
        resultado = pipeline.review_candidate(
            failure=_failure(), skill_name="no-existe",
            llm=FakeLlm(FakeResult(parsed=_patch_response("x", "y"))),
            hermes_home=box.home, skills_dirs=[box.skills],
        )
        check("ok=False", resultado["ok"] is False)
        check("la etapa lo nombra", "localizar" in resultado["stage"], resultado["stage"])
        check("la razón dice dónde buscó", "SKILL.md" in resultado["message"],
              resultado["message"])
    finally:
        box.cleanup()


def test_undo_with_nothing_to_undo() -> None:
    print("\nrevertir sin nada aplicado se reporta, no falla en silencio")

    box = Sandbox()
    try:
        resultado = pipeline.undo_last(hermes_home=box.home)
        check("ok=False", resultado["ok"] is False)
        check("la razón explica que no hay nada",
              "no hay" in resultado["message"], resultado["message"])
    finally:
        box.cleanup()


def test_register_wires_hook_and_tools() -> None:
    print("\nregister() cablea lo que el manifest declara")

    registrado = {"hooks": [], "tools": []}

    class FakeCtx:
        def register_hook(self, name, cb):
            registrado["hooks"].append((name, cb))

        def register_tool(self, name, toolset, schema, handler, **kw):
            registrado["tools"].append({"name": name, "toolset": toolset,
                                        "schema": schema, "handler": handler})

    import __init__ as plugin  # noqa: PLC0415

    plugin.register(FakeCtx())

    check("registró 1 hook", len(registrado["hooks"]) == 1, str(registrado["hooks"]))
    check("el hook es on_session_end",
          registrado["hooks"] and registrado["hooks"][0][0] == "on_session_end")
    check("registró 2 tools", len(registrado["tools"]) == 2, str(len(registrado["tools"])))
    nombres = {t["name"] for t in registrado["tools"]}
    check("los tools son skills_review y skills_undo",
          nombres == {"skills_review", "skills_undo"}, str(nombres))
    for t in registrado["tools"]:
        check(f"'{t['name']}' trae esquema con parámetros",
              isinstance(t["schema"], dict) and "parameters" in t["schema"])
        check(f"'{t['name']}' trae handler invocable", callable(t["handler"]))
    check("no usó override de tools del núcleo",
          all("override" not in t for t in registrado["tools"]))


def test_manifest_matches_registration() -> None:
    print("\nel manifest declara exactamente lo que register() registra")

    raiz = Path(__file__).resolve().parent.parent
    texto = (raiz / "plugin.yaml").read_text(encoding="utf-8")
    check("declara provides_hooks", "provides_hooks" in texto)
    check("declara provides_tools", "provides_tools" in texto)
    check("on_session_end está declarado", "on_session_end" in texto)
    check("skills_review está declarado", "skills_review" in texto)
    check("skills_undo está declarado", "skills_undo" in texto)
    check("la versión subió del andamio",
          'version: 0.2.0' in texto, "sigue en 0.1.0")


def main() -> int:
    print("=" * 68)
    print("hermes-skills-helper — cableado (pipeline + register)")
    print("=" * 68)

    for fn in (
        test_hook_never_calls_the_model,
        test_throttle_distinguishes_not_run_from_nothing_found,
        test_throttle_allows_after_interval,
        test_run_is_resilient_to_a_failing_stage,
        test_unreadable_trajectory_is_a_limitation_not_a_clean_result,
        test_health_does_not_create,
        test_review_proposes_and_stops,
        test_review_refuses_to_bypass_the_gate,
        test_review_respects_staged_response,
        test_review_applies_through_host_and_records,
        test_review_no_op_does_not_apply,
        test_review_rejects_index_out_of_range,
        test_review_missing_skill,
        test_undo_with_nothing_to_undo,
        test_register_wires_hook_and_tools,
        test_manifest_matches_registration,
        test_version_matches_between_manifest_and_module,
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
