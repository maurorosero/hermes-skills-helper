"""hermes-skills-helper — punto de entrada del plugin.

Capa de medición para la auto-mejora de los skills del arnés: encuentra fallos que se
repiten entre sesiones, propone UN cambio mínimo en un skill y califica si sirvió.

Las cinco etapas
----------------
```
5. ¿SIRVIÓ?      effect/        tres chequeos deterministas + veredicto
1. RECURRENCIA   recurrence.py + trajectory.py
4. JOURNAL       journal.py     registro append-only + rollback verificado
2. TECHO         budget.py      techos diarios, con la carrera cerrada
3. PROPUESTA     proposal.py    la única con modelo
```

``pipeline.py`` las orquesta. Todo está implementado y verificado contra el arnés real.

Qué registra el plugin, y qué no
--------------------------------
```
on_session_end    SÍ   el ciclo DETERMINISTA: etapas 1, 2 y 5. Nunca llama al modelo.
skills_review     SÍ   el tool que propone. Es el único camino que gasta presupuesto.
skills_undo       SÍ   el tool que revierte el último cambio aplicado (etapa 4).
```

**Sin hook de propuesta.** Verificado en el arnés: ``on_session_end`` corre *por turno*,
no por sesión — está documentado en ``agent/turn_finalizer.py`` (*"run_conversation() runs
once per message"*). Un gancho por turno que llamara al modelo gastaría el techo de costo
en la primera hora de conversación. El hook corre sólo lo determinista, y con un intervalo
mínimo entre barridos: el barrido completo tarda ~0,30 s medidos — no es caro, pero
tampoco gratis a cada mensaje.

Por qué la aplicación pasa por el arnés y no por escritura directa
-----------------------------------------------------------------
Verificado en ejecución que el arnés **ya tiene** el gate de aprobación encendido
(``skills.write_approval: true``): ``skill_manage(action='create', ...)`` devuelve
``{"success": true, "staged": true, "pending_id": ...}`` y el archivo **no se modifica** —
queda en cola para que Mauro lo revise.

Es exactamente el mecanismo que este proyecto iba a construir, ya en uso. Escribir directo
para "no depender del host" se saltaría la aprobación de Mauro, que es la parte que no se
negocia. Por eso, si la vía del arnés no está disponible, el plugin falla y lo dice.

Restricción de diseño
---------------------
El núcleo de Hermes **no se toca**. Todo usa APIs que el arnés ya expone
(``on_session_end``, ``ctx.llm.complete_structured``, ``ctx.register_tool``). El gate de
aprobación también es del host: el plugin lo usa, no lo reimplementa.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

__version__ = "0.3.0"

logger = logging.getLogger(__name__)

#: Nombres de los tools que el plugin expone.
TOOL_REVIEW = "skills_review"
TOOL_UNDO = "skills_undo"

HOOK_SESSION_END = "on_session_end"


def _hermes_home() -> Path:
    """El hogar del arnés, por la vía oficial y con respaldo.

    Se prefiere el helper del host; si no está, ``HERMES_HOME``; y si tampoco, la ruta por
    defecto. Se resuelve con ``expanduser()``/``resolve()`` en **todos** los caminos: un
    ``~`` literal crearía un directorio llamado ``~`` en el directorio de trabajo, y el
    plugin quedaría escribiendo su estado en el lugar equivocado sin fallar.
    """
    try:
        from hermes_constants import get_hermes_home  # noqa: PLC0415

        return Path(get_hermes_home()).expanduser().resolve()
    except Exception:
        env = os.environ.get("HERMES_HOME", "").strip()
        if env:
            return Path(env).expanduser().resolve()
        return Path(os.path.expanduser("~/.hermes")).resolve()


def _skills_dirs(hermes_home: Path) -> list[Path]:
    """Directorios donde buscar skills: los externos configurados, más el por defecto.

    El hub de Mauro vive fuera de ``~/.hermes/skills``. Buscar sólo en la ruta por defecto
    no encontraría el SKILL.md de la mayoría de los skills, y el fallo sería silencioso:
    el plugin diría "no se encontró el skill" para skills que existen.
    """
    dirs: list[Path] = []
    try:
        from hermes_cli.config import load_config_readonly  # noqa: PLC0415

        config = load_config_readonly() or {}
        candidatos = list(config.get("external_dirs") or [])
        skills_cfg = config.get("skills")
        if isinstance(skills_cfg, dict):
            candidatos += list(skills_cfg.get("external_dirs") or [])
        for clave in candidatos:
            if isinstance(clave, str) and clave.strip():
                dirs.append(Path(clave).expanduser().resolve())
    except Exception as exc:
        logger.debug("No se pudieron leer los directorios externos: %s", exc)

    por_defecto = (hermes_home / "skills").resolve()
    if por_defecto not in dirs:
        dirs.append(por_defecto)
    return dirs


def _on_session_end(**kwargs: Any) -> None:
    """Hook del ciclo determinista. **Nunca llama al modelo.**

    Un hook no debe romper el turno. Toda excepción se registra y se traga: fallar acá no
    puede costarle a Mauro su respuesta.
    """
    try:
        from . import pipeline  # noqa: PLC0415

        home = _hermes_home()
        informe = pipeline.run_deterministic(
            hermes_home=home, skills_dirs=_skills_dirs(home))
        if informe.scanned:
            logger.info("hermes-skills-helper: %s", informe.summary())
            if informe.candidates:
                # Un aviso, no una acción: el plugin no propone solo.
                logger.info(
                    "hermes-skills-helper: %d fallo(s) recurrente(s) listos para revisar "
                    "con el tool %s", informe.candidates, TOOL_REVIEW,
                )
        else:
            logger.debug("hermes-skills-helper: %s", informe.summary())
    except Exception as exc:
        logger.warning("hermes-skills-helper: el ciclo determinista falló: %s", exc)


def _skills_manage():
    """La vía del arnés para escribir skills, con su gate de aprobación.

    Se importa perezosamente y se devuelve ``None`` si no está: el llamador lo trata como
    "no hay vía" y falla en lugar de escribir por atajo.
    """
    try:
        from tools.skill_manager_tool import skill_manage  # noqa: PLC0415

        return skill_manage
    except Exception as exc:
        logger.warning("No se pudo importar la vía de escritura del arnés: %s", exc)
        return None


def _tool_review(**kwargs: Any) -> str:
    """Tool ``skills_review``: propone (y opcionalmente aplica) UN cambio mínimo.

    Es el único camino que gasta presupuesto de inferencia. Sin ``apply``, propone y se
    detiene — modo por defecto, porque un revisor que aplica solo es exactamente el
    revisor que este proyecto vino a reemplazar.
    """
    try:
        from . import pipeline  # noqa: PLC0415

        home = _hermes_home()
        dirs = _skills_dirs(home)

        informe = pipeline.run_deterministic(
            hermes_home=home, skills_dirs=dirs, force=True)
        if not informe.recurrence or not informe.recurrence.recurring:
            return json.dumps(
                {"ok": True, "stage": "sin candidatos",
                 "message": "no hay fallos recurrentes sostenidos que justifiquen un cambio",
                 "summary": informe.summary()},
                ensure_ascii=False,
            )

        candidatos = informe.recurrence.recurring
        try:
            indice = int(kwargs.get("index", 0) or 0)
        except (TypeError, ValueError):
            indice = 0
        if indice < 0 or indice >= len(candidatos):
            return json.dumps(
                {"ok": False, "stage": "índice fuera de rango",
                 "message": f"hay {len(candidatos)} candidato(s); el índice {indice} no existe",
                 "available": [c.why() for c in candidatos[:10]]},
                ensure_ascii=False,
            )
        candidato = candidatos[indice]
        skill_name = str(kwargs.get("skill") or candidato.tool_name)

        from agent.plugin_llm import PluginLlm  # noqa: PLC0415

        llm = PluginLlm(plugin_id="hermes-skills-helper")
        resultado = pipeline.review_candidate(
            failure=candidato,
            skill_name=skill_name,
            llm=llm,
            hermes_home=home,
            skills_dirs=dirs,
            apply=bool(kwargs.get("apply")),
            skills_manage=_skills_manage(),
        )
        resultado["candidate"] = candidato.why()
        return json.dumps(resultado, ensure_ascii=False, default=str)
    except Exception as exc:
        logger.warning("hermes-skills-helper: %s falló: %s", TOOL_REVIEW, exc)
        return json.dumps(
            {"ok": False, "stage": "error", "message": f"{type(exc).__name__}: {exc}"},
            ensure_ascii=False,
        )


def _tool_undo(**kwargs: Any) -> str:
    """Tool ``skills_undo``: revierte el último cambio aplicado (etapa 4)."""
    try:
        from . import pipeline  # noqa: PLC0415

        resultado = pipeline.undo_last(hermes_home=_hermes_home())
        return json.dumps(resultado, ensure_ascii=False, default=str)
    except Exception as exc:
        logger.warning("hermes-skills-helper: %s falló: %s", TOOL_UNDO, exc)
        return json.dumps(
            {"ok": False, "message": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)


_REVIEW_SCHEMA = {
    "name": TOOL_REVIEW,
    "description": (
        "Revisa los fallos que se repiten en la trayectoria del arnés y propone UN cambio "
        "mínimo en un skill para evitar que se repitan. Si no corresponde ningún cambio, "
        "responde no_op — que es una respuesta válida, no un error. Por defecto sólo "
        "propone: para aplicar hay que pedirlo explícitamente, y la escritura queda sujeta "
        "al gate de aprobación del arnés."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "index": {
                "type": "integer",
                "description": "Cuál de los fallos recurrentes revisar (0 = el más frecuente).",
            },
            "skill": {
                "type": "string",
                "description": (
                    "El skill a revisar. Si se omite, se usa el nombre de la herramienta "
                    "del fallo."
                ),
            },
            "apply": {
                "type": "boolean",
                "description": (
                    "Si es true, aplica el cambio propuesto. Por defecto false: propone y "
                    "se detiene. La escritura queda sujeta al gate de aprobación del arnés."
                ),
            },
        },
        "required": [],
    },
}

_UNDO_SCHEMA = {
    "name": TOOL_UNDO,
    "description": (
        "Revierte el último cambio que hermes-skills-helper aplicó a un skill, "
        "restaurándolo byte a byte a su estado previo. Verifica el hash restaurado."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}


def register(ctx: Any) -> None:
    """Registra el hook determinista y los dos tools.

    El manifest declara exactamente esto. Un hook o tool declarado y no registrado produce
    un aviso del validador, y anunciar una capacidad que no existe es la clase de detalle
    que hace desconfiar del resto.
    """
    ctx.register_hook(HOOK_SESSION_END, _on_session_end)

    ctx.register_tool(
        name=TOOL_REVIEW,
        toolset="skills-helper",
        schema=_REVIEW_SCHEMA,
        handler=_tool_review,
        description=_REVIEW_SCHEMA["description"],
        emoji="🔬",
    )
    ctx.register_tool(
        name=TOOL_UNDO,
        toolset="skills-helper",
        schema=_UNDO_SCHEMA,
        handler=_tool_undo,
        description=_UNDO_SCHEMA["description"],
        emoji="↩️",
    )
    logger.info(
        "hermes-skills-helper %s: hook '%s' + tools '%s', '%s'",
        __version__, HOOK_SESSION_END, TOOL_REVIEW, TOOL_UNDO,
    )


__all__ = ["register", "__version__", "TOOL_REVIEW", "TOOL_UNDO", "HOOK_SESSION_END"]
