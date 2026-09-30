"""Recolector — lo que sobrevive del ciclo, y por qué.

Este módulo reemplaza a `pipeline.py`. El ciclo de cinco etapas que orquestaba quedó
recortado por decisión de diseño, y el motivo importa tanto como el recorte.

**Lo que se fue, y por qué**

```
ETAPA 2  budget.py    techos de cambios y llamadas al modelo
ETAPA 3  proposal.py  la única con modelo: pide un patch y lo propone
ETAPA 4  journal.py   registra la escritura y la revierte por hash
         pipeline.py  orquesta las cinco etapas
```

Las cuatro etapas existen para **escribir** sobre los skills. Y escribir sin verificar es
exactamente lo que degradó el catálogo de Mauro (medido el 29-sep-2026):

```
andrea-governance      189 parches   100,3 KB   patch_generation = 1
41 skills parcheados   1.242 parches   0 verificaciones de que mejoraron
```

El techo de 3 cambios/día no lo evitó porque **nadie lo cobraba**. Y el journal registraba
hashes que nadie releía: cero consumidores de `before`/`after` en todo el arnés.

**Lo que se queda, y por qué**

El recolector no escribe, no llama al modelo y no decide. Mide y declara hechos. Es la
parte que ninguno de los dos sistemas que ya existen tiene:

```
curador del arnés    mantiene el catálogo y archiva por reloj (62 en una corrida,
                     y 4 volvieron esa misma semana). No mide si algo mejoró.
Refine Cycle         busca errores repetidos. Mide el efecto como "el archivo creció".
```

Ninguno cierra el ciclo. Y los dos escriben.

**El flujo que este recolector habilita**

```
recolector       mide y recomienda     (acá, cero escritura)
   ↓
cron             cruza con git log     (determinista, sin modelo)
   ↓
issue en el repo la propuesta con motivo y evidencia
   ↓
revisión         maquila + humano autorizador, en sandbox
   ↓
promoción        repo → ~/.hermes/skills, explícita y reversible
```

El repo es privado (`maurorosero/rosero-skills`) y **la rama de producción es
`~/.hermes/skills`**: el arnés resuelve local primero (verificado en
`agent/skill_utils.py:420-428`, docstring *"local ... first"*). Eso es una virtud, no un
problema: lo que corre es lo revisado, y el repo es la mesa de trabajo.

**Qué rescata este módulo del recorte**

Sólo dos cosas, y las dos estaban probadas:

* el **throttle** (`should_scan`), con su razón en ambos sentidos: "no se barrió" tiene que
  poder distinguirse de "se barrió y no había nada".
* el **estado atómico** (`read_state`/`write_state`): escritura a temporal más
  `os.replace`, para que un proceso interrumpido no deje el estado a medias.

El lock de `budget.py` y la escritura append-only de `journal.py` no se rescatan porque
protegían la escritura concurrente del libro de presupuesto y del journal de cambios. Sin
esos dos, no hay nada que proteger: el recolector escribe un solo archivo de estado.

Restricción de diseño: el núcleo de Hermes **no se toca**.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:  # cargado como paquete por el arnés
    from . import uso
    from .recurrence import RecurrenceReport, detect
except ImportError:  # cargado como módulo suelto (tests, ejecución directa)
    import uso  # type: ignore
    from recurrence import RecurrenceReport, detect  # type: ignore

logger = logging.getLogger(__name__)

#: Nombre del archivo de estado, dentro de la carpeta del plugin.
STATE_FILE_NAME = "recolector-state.json"

#: Intervalo mínimo entre barridos. El hook `on_session_end` corre **por turno**
#: (verificado en `agent/turn_finalizer.py`), no por sesión: sin throttle barrería a cada
#: mensaje. El barrido tarda ~0,30 s medidos — no es caro, pero tampoco es gratis.
MIN_SCAN_INTERVAL_SECONDS = 900.0

#: Ventana de la recurrencia de fallos, en días.
DEFAULT_WINDOW_DAYS = 30.0


@dataclass
class RecolectorReport:
    """Lo que produjo un barrido del recolector.

    ``scanned`` distingue *"se barrió y no hay nada"* de *"no se barrió"*. Sin esa
    distinción, un informe vacío por throttle se leería como ausencia de señales.

    ``errors`` y las dos banderas de legibilidad existen por la misma razón: una fuente
    que no se pudo leer hace que "0 señales" signifique *"no se pudo mirar"*.
    """

    scanned: bool
    reason: str
    ts: float = 0.0
    uso: Optional[Any] = None
    recurrence: Optional[RecurrenceReport] = None
    usage_readable: bool = True
    trajectory_readable: bool = True
    errors: list = field(default_factory=list)

    @property
    def limitation(self) -> str:
        """Lo que impide sostener la conclusión, si algo la impide."""
        partes: list[str] = []
        if not self.usage_readable:
            partes.append("el registro de uso no se pudo leer")
        if not self.trajectory_readable:
            partes.append("la trayectoria no se pudo leer")
        if self.uso is not None and not self.uso.trustworthy:
            partes.append(f"{self.uso.sin_registro} skills activos sin registro")
        if self.recurrence is not None and not self.recurrence.trustworthy:
            partes.append(
                f"se descartaron {self.recurrence.discarded_ts} de "
                f"{self.recurrence.total_failures} filas por fecha increíble"
            )
        return "; ".join(partes)

    def summary(self) -> str:
        """Una línea con el reparto, para el log del hook."""
        if not self.scanned:
            return f"sin barrido ({self.reason})"
        partes = []
        if self.uso is not None:
            partes.append(uso.resumen(self.uso))
        if self.recurrence is not None:
            partes.append(self.recurrence.summary())
        texto = " · ".join(partes) if partes else "sin señales"
        if self.limitation:
            texto += f" · LIMITACION: {self.limitation}"
        return texto


# -- Estado --------------------------------------------------------------------------

def state_path(*, hermes_home: Path) -> Path:
    """Ruta del estado del recolector, dentro de la carpeta del plugin."""
    return Path(hermes_home) / "plugins" / "hermes-skills-helper" / STATE_FILE_NAME


def read_state(*, hermes_home: Path) -> dict:
    """Estado del recolector. No crea nada: un lector no materializa lo que inspecciona."""
    path = state_path(hermes_home=hermes_home)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Estado del recolector ilegible: %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


def write_state(data: dict, *, hermes_home: Path) -> None:
    """Escritura atómica: a temporal y `os.replace`.

    Un proceso interrumpido a mitad de la escritura no deja el estado truncado — o está
    el anterior entero, o está el nuevo entero. Sin esto, el throttle podría leer un JSON
    a medias y barrer de más.
    """
    path = state_path(hermes_home=hermes_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)


def should_scan(*, hermes_home: Path, now: Optional[float] = None,
                min_interval: float = MIN_SCAN_INTERVAL_SECONDS) -> tuple[bool, str]:
    """¿Corresponde barrer ahora? El throttle, con su razón.

    Devuelve la razón en ambos casos para que un barrido salteado sea auditable: "no se
    barrió" tiene que poder distinguirse de "se barrió y no había nada".
    """
    marca = time.time() if now is None else now
    ultimo = read_state(hermes_home=hermes_home).get("last_scan_ts")
    if not isinstance(ultimo, (int, float)):
        return True, "no hay barrido previo registrado"
    transcurrido = marca - float(ultimo)
    if transcurrido < 0:
        # El reloj se movió hacia atrás. No se castiga con un bloqueo largo: se barre.
        return True, "el reloj retrocedió respecto del último barrido"
    if transcurrido < min_interval:
        faltan = min_interval - transcurrido
        return False, f"faltan {faltan:.0f} s para el próximo barrido ({min_interval:.0f} s)"
    return True, f"pasaron {transcurrido:.0f} s desde el último barrido"


def state_db_path(*, hermes_home: Path) -> Path:
    """Ruta de la base de trayectoria del arnés."""
    return Path(hermes_home) / "state.db"


# -- El barrido ----------------------------------------------------------------------

def recolectar(
    *,
    hermes_home: Path,
    skills_dirs: Optional[list] = None,
    ahora: Optional[float] = None,
    force: bool = False,
    min_interval: float = MIN_SCAN_INTERVAL_SECONDS,
    window_days: float = DEFAULT_WINDOW_DAYS,
    **umbrales_uso: Any,
) -> RecolectorReport:
    """Corre el barrido completo: señal de uso + recurrencia de fallos.

    **No llama al modelo nunca, y no escribe sobre ningún skill.** Sólo lee y, si barrió,
    anota la marca de tiempo en su propio archivo de estado.

    Cada fuente está envuelta por separado: una que falla no impide que la otra corra. Un
    barrido abortado entero porque la trayectoria no se pudo leer dejaría también sin
    medir el uso de los skills, que es la señal principal.
    """
    marca = time.time() if ahora is None else ahora
    home = Path(hermes_home)
    dirs = [Path(d) for d in (skills_dirs or [home / "skills"])]
    report = RecolectorReport(scanned=False, reason="", ts=marca)

    if not force:
        corresponde, razon = should_scan(hermes_home=home, now=marca,
                                         min_interval=min_interval)
        if not corresponde:
            report.reason = razon
            return report
        report.reason = razon
    else:
        report.reason = "barrido forzado"

    # Señal de uso: la principal. Si el registro no se pudo leer, se declara.
    try:
        report.uso = uso.medir(hermes_home=home, skills_dirs=dirs,
                               ahora=marca, **umbrales_uso)
    except Exception as exc:  # noqa: BLE001 — un barrido no puede romper el turno
        report.usage_readable = False
        report.errors.append(f"uso: {type(exc).__name__}: {exc}")
        logger.warning("El barrido de uso falló: %s", exc)

    # Recurrencia de fallos: la señal complementaria. El core del arnés no compara errores
    # entre sesiones por diseño (descarta los transitorios en `background_review`), así que
    # esta lectura cruza sesiones donde él no lo hace.
    try:
        report.recurrence = detect(state_db=state_db_path(hermes_home=home),
                                   window_days=window_days, now=marca)
    except Exception as exc:  # noqa: BLE001
        report.trajectory_readable = False
        report.errors.append(f"recurrencia: {type(exc).__name__}: {exc}")
        logger.warning("El barrido de recurrencia falló: %s", exc)

    report.scanned = True
    # El estado se anota **después** de barrer, y sólo si se barrió: marcar antes dejaría
    # el throttle convencido de un barrido que no ocurrió.
    try:
        estado = read_state(hermes_home=home)
        estado["last_scan_ts"] = marca
        estado["last_scan_reason"] = report.reason
        write_state(estado, hermes_home=home)
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"estado: {type(exc).__name__}: {exc}")
        logger.warning("No se pudo anotar el barrido: %s", exc)

    return report


__all__ = [
    "STATE_FILE_NAME",
    "MIN_SCAN_INTERVAL_SECONDS",
    "DEFAULT_WINDOW_DAYS",
    "RecolectorReport",
    "state_path",
    "read_state",
    "write_state",
    "should_scan",
    "state_db_path",
    "recolectar",
]
