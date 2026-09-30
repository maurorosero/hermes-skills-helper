"""hermes-skills-helper — recolector de señales para la mejora de skills.

**Qué es esto, en una frase:** el plugin **reúne los datos** que hacen falta para decidir
si un skill hay que crearlo, mejorarlo o darlo de baja. No lo decide, no lo escribe, y no
llama al modelo.

El flujo completo
-----------------
```
recolector (acá)      mide y recomienda               cero escritura, cero modelo
   ↓
cron                  cruza con el historial git      determinista
   ↓
issue en el repo      la propuesta, con motivo y evidencia
   ↓
revisión externa      humano autorizador, en sandbox
   ↓
promoción             repo → ~/.hermes/skills         explícita, reversible
```

El plugin es la primera pieza y sólo la primera. Lo demás vive fuera: en el repo, en git
y en quien revisa.

Por qué no escribe
------------------
Escribir sobre skills con verificación pobre es lo que degradó el catálogo. Medido el
29-sep-2026 en el arnés de Mauro:

```
andrea-governance     189 parches    100,3 KB    patch_generation = 1
41 skills parcheados  1.242 parches  0 verificaciones de que mejoraran
```

189 escrituras contadas como **una sola generación**. Un cambio malo no se puede aislar ni
revertir, porque no se sabe cuál fue.

Y hay una razón de costo: cada cambio propuesto se encolaba para aprobación. La cola llegó
a **224 pendientes / 416 operaciones**, el 79% concentrado en cuatro skills, sin registrar
el motivo de ninguna. Un proceso que genera trabajo de revisión sin criterio no ahorra
trabajo: lo multiplica.

Qué expone
----------
```
on_session_end    el barrido determinista, con throttle de 900 s
skills_signals    responde: qué skills tienen señal y cuál. Sólo lee.
skills_report     el informe completo, con un cuerpo Markdown para abrir un issue
```

Ninguno escribe, ninguno propone un cambio de texto, ninguno llama al modelo.

Qué reemplaza
-------------
```
curador del arnés   mantiene el catálogo; archiva por reloj (62 en una corrida, 4
                    volvieron esa misma semana) y no mide si algo mejoró
Refine Cycle        busca errores repetidos; mide el efecto como "el archivo creció"
```

Los dos escriben y ninguno cierra el ciclo. Este recolector no escribe — y por eso puede
medir.

Restricción de diseño
---------------------
El núcleo de Hermes **no se toca**. Todo usa APIs que el arnés ya expone: ``on_session_end``,
``ctx.register_tool`` y la lectura del ``.usage.json`` que el propio arnés mantiene.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

__version__ = "0.4.1"

logger = logging.getLogger(__name__)

#: Tools que el plugin expone. Los dos leen; ninguno escribe.
TOOL_SIGNALS = "skills_signals"
TOOL_REPORT = "skills_report"

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

    El hub vive fuera de ``~/.hermes/skills``. Buscar sólo en la ruta por defecto no
    encontraría el SKILL.md de la mayoría de los skills, y el fallo sería silencioso: el
    plugin reportaría "no se encontró" para skills que existen.

    El orden importa: el arnés resuelve **local primero** (``agent/skill_utils.py:420``,
    docstring *"local ... first"*), así que la ruta por defecto va al final para que el
    tamaño medido sea el del archivo que realmente se carga.
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
    """Hook del barrido determinista. **Nunca llama al modelo y no escribe en skills.**

    Un hook no debe romper el turno. Toda excepción se registra y se traga: fallar acá no
    puede costarle a Mauro su respuesta.
    """
    try:
        from . import recolector  # noqa: PLC0415

        home = _hermes_home()
        informe = recolector.recolectar(hermes_home=home, skills_dirs=_skills_dirs(home))
        if informe.scanned:
            logger.info("hermes-skills-helper: %s", informe.summary())
        else:
            logger.debug("hermes-skills-helper: %s", informe.summary())
    except Exception as exc:
        logger.warning("hermes-skills-helper: el barrido falló: %s", exc)


def _senales_json(informe: Any, *, limite: int = 20) -> dict:
    """Las señales del barrido en forma serializable, para los dos tools."""
    salida: dict[str, Any] = {
        "ok": True,
        "scanned": informe.scanned,
        "reason": informe.reason,
        "summary": informe.summary(),
    }
    if informe.limitation:
        salida["limitation"] = informe.limitation
    if informe.errors:
        salida["errors"] = informe.errors

    if informe.uso is not None:
        r = informe.uso
        salida["uso"] = {
            "activos": r.total_activos,
            "sin_registro": r.sin_registro,
            "confiable": r.trustworthy,
            "sin_usar": [{"nombre": s.nombre, "motivo": s.motivo}
                         for s in r.sin_usar[:limite]],
            "sin_cambio": [{"nombre": s.nombre, "usos": s.use_count, "motivo": s.motivo}
                           for s in r.sin_cambio[:limite]],
            "sin_reuso": [{"nombre": s.nombre, "parches": s.patch_count, "motivo": s.motivo}
                          for s in r.sin_reuso[:limite]],
            "hinchados": [{"nombre": s.nombre, "parches": s.patch_count,
                           "bytes": s.tamano_bytes, "motivo": s.motivo}
                          for s in r.hinchado[:limite]],
            "conteos": {
                "sin_usar": len(r.sin_usar),
                "sin_cambio": len(r.sin_cambio),
                "sin_reuso": len(r.sin_reuso),
                "hinchados": len(r.hinchado),
            },
        }
    if informe.recurrence is not None:
        rec = informe.recurrence
        salida["recurrencia"] = {
            "recurrentes": len(rec.recurring),
            "rafagas": len(rec.bursts),
            "guardarrailes": len(rec.guardrails),
            "caducos": len(rec.stale),
            "confiable": rec.trustworthy,
            # El fallo mismo, no sólo su estadística: "5 apariciones" sin decir qué falló
            # obliga a ir a buscarlo aparte, y el tool queda a medias.
            "detalle": [
                {
                    "tool": c.tool_name,
                    "huella": c.fingerprint,
                    "evidencia": c.why(),
                    "muestra": " ".join((c.sample or "").split())[:300],
                    "primera": c.first_ts,
                    "ultima": c.last_ts,
                }
                for c in rec.recurring[:limite]
            ],
        }
    return salida


def _tool_signals(**kwargs: Any) -> str:
    """Tool ``skills_signals``: qué skills tienen señal, y cuál.

    Lee y responde. No propone cambios, no escribe, no llama al modelo. Es la consulta
    directa a lo que el recolector midió.
    """
    try:
        from . import recolector  # noqa: PLC0415

        home = _hermes_home()
        informe = recolector.recolectar(
            hermes_home=home, skills_dirs=_skills_dirs(home), force=bool(kwargs.get("force")))
        return json.dumps(_senales_json(informe, limite=int(kwargs.get("limit") or 20)),
                          ensure_ascii=False, default=str)
    except Exception as exc:
        logger.warning("hermes-skills-helper: %s falló: %s", TOOL_SIGNALS, exc)
        return json.dumps(
            {"ok": False, "message": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)


def _tool_report(**kwargs: Any) -> str:
    """Tool ``skills_report``: el informe completo, listo para abrir un issue.

    Devuelve el mismo dato que ``skills_signals`` más un bloque de texto pensado para
    pegarse en un issue del repo de skills. El plugin redacta; el issue lo abre quien
    corresponda.
    """
    try:
        from . import recolector  # noqa: PLC0415

        home = _hermes_home()
        informe = recolector.recolectar(
            hermes_home=home, skills_dirs=_skills_dirs(home), force=bool(kwargs.get("force")))
        salida = _senales_json(informe, limite=int(kwargs.get("limit") or 20))
        salida["markdown"] = _markdown_issue(informe)
        salida["nota"] = (
            "El plugin no abre el issue: lo redacta. Abrirlo en rosero-skills es del paso "
            "siguiente del flujo, y no escribe sobre ningun skill."
        )
        return json.dumps(salida, ensure_ascii=False, default=str)
    except Exception as exc:
        logger.warning("hermes-skills-helper: %s falló: %s", TOOL_REPORT, exc)
        return json.dumps(
            {"ok": False, "message": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)


def _markdown_issue(informe: Any) -> str:
    """Redacta el cuerpo de un issue con las señales del barrido."""
    lineas = ["## Señales del recolector (`hermes-skills-helper`)", ""]
    lineas.append(f"**Resumen:** {informe.summary()}")
    if informe.limitation:
        lineas.append(f"**Limitación:** {informe.limitation}")
    lineas.append("")

    r = informe.uso
    if r is not None:
        cubos = [
            ("Sin usar — creados y nunca usados", r.sin_usar,
             lambda s: f"- `{s.nombre}` — {s.motivo}"),
            ("Sin cambio — se usan mucho y nunca se parchearon", r.sin_cambio,
             lambda s: f"- `{s.nombre}` — {s.motivo}"),
            ("Sin reuso — parcheados y no vueltos a usar", r.sin_reuso,
             lambda s: f"- `{s.nombre}` — {s.motivo}"),
            ("Hinchados — desgaste acumulado", r.hinchado,
             lambda s: f"- `{s.nombre}` — {s.motivo}"),
        ]
        for titulo, items, fmt in cubos:
            lineas.append(f"### {titulo} ({len(items)})")
            lineas.append("")
            if items:
                lineas.extend(fmt(s) for s in items[:30])
            else:
                lineas.append("_ninguno_")
            lineas.append("")

    rec = informe.recurrence
    if rec is not None:
        lineas.append(f"### Fallos recurrentes ({len(rec.recurring)})")
        lineas.append("")
        if rec.recurring:
            for c in rec.recurring[:30]:
                # El fallo mismo, no sólo su estadística: un lector que ve "5 apariciones"
                # sin saber qué falló tiene que ir a buscarlo aparte, y entonces el
                # informe no cumple su función.
                lineas.append(f"- **`{c.tool_name}`** — {c.why()}")
                muestra = " ".join((c.sample or "").split())
                if muestra:
                    recorte = muestra[:300] + ("…" if len(muestra) > 300 else "")
                    lineas.append(f"  > {recorte}")
        else:
            lineas.append("_ninguno_")
        lineas.append("")

    lineas.append("---")
    lineas.append("")
    lineas.append("_Generado por el recolector. No propone cambios de texto ni escribe "
                  "sobre ningún skill: los datos son el insumo de la revisión._")
    return "\n".join(lineas)


_SIGNALS_SCHEMA = {
    "name": TOOL_SIGNALS,
    "description": (
        "Informa qué skills del catálogo tienen señal de mejora, y cuál. Cuatro cubos: "
        "sin usar (creados y nunca usados), sin cambio (se usan mucho y nunca se "
        "parchearon), sin reuso (parcheados y no vueltos a usar) e hinchados (cuerpo o "
        "número de parches fuera de rango). Sólo lee: no escribe, no propone cambios de "
        "texto, no llama al modelo."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "force": {
                "type": "boolean",
                "description": "Barre ahora sin respetar el intervalo mínimo de 900 s.",
            },
            "limit": {
                "type": "integer",
                "description": "Máximo de skills listados por cubo (por defecto 20).",
            },
        },
        "required": [],
    },
}

_REPORT_SCHEMA = {
    "name": TOOL_REPORT,
    "description": (
        "El informe completo del recolector, con las señales de uso y los fallos "
        "recurrentes, más un cuerpo en Markdown listo para abrir un issue en el repo de "
        "skills. Sólo lee y redacta: no abre el issue, no escribe sobre ningún skill y no "
        "llama al modelo."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "force": {
                "type": "boolean",
                "description": "Barre ahora sin respetar el intervalo mínimo de 900 s.",
            },
            "limit": {
                "type": "integer",
                "description": "Máximo de skills listados por cubo (por defecto 20).",
            },
        },
        "required": [],
    },
}


def register(ctx: Any) -> None:
    """Registra el hook de barrido y los dos tools de consulta.

    El manifest declara exactamente esto. Un hook o tool declarado y no registrado produce
    un aviso del validador, y anunciar una capacidad que no existe es la clase de detalle
    que hace desconfiar del resto.
    """
    ctx.register_hook(HOOK_SESSION_END, _on_session_end)

    ctx.register_tool(
        name=TOOL_SIGNALS,
        toolset="skills-helper",
        schema=_SIGNALS_SCHEMA,
        handler=_tool_signals,
        description=_SIGNALS_SCHEMA["description"],
        emoji="📊",
    )
    ctx.register_tool(
        name=TOOL_REPORT,
        toolset="skills-helper",
        schema=_REPORT_SCHEMA,
        handler=_tool_report,
        description=_REPORT_SCHEMA["description"],
        emoji="📝",
    )
    logger.info(
        "hermes-skills-helper %s: hook '%s' + tools '%s', '%s' (recolector: cero escritura)",
        __version__, HOOK_SESSION_END, TOOL_SIGNALS, TOOL_REPORT,
    )


__all__ = [
    "register",
    "__version__",
    "TOOL_SIGNALS",
    "TOOL_REPORT",
    "HOOK_SESSION_END",
]
