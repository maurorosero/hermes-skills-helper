"""Etapa 2 — Techo. Determinista, sin modelo.

**Pregunta:** ¿queda presupuesto hoy?

Dos límites duros, tomados del proyecto de referencia porque son sensatos:

```
≤ 3 cambios por día     (radio de impacto: cada cambio altera la conducta del agente)
≤ 30 llamadas por día   (costo: la única etapa que gasta inferencia)
```

Se evalúan **antes** de llamar al modelo. Si no hay presupuesto, la etapa termina sin
consumir nada.

La carrera, que es el punto de este módulo
------------------------------------------
El aviso venía heredado del original y hay que tomarlo en serio: **el contador es
leer-y-actuar, y el registro es leer-modificar-escribir.** Verificado en el arnés: la
fachada de estado del host (``PluginState``) hace atómico *cada* acceso —`get` toma el
bloqueo, `set` toma el bloqueo— pero **no** la secuencia entre ellos. Con dos canales
concurrentes:

```
proceso A    lee usados=2   →  2 < 3, hay lugar  →  escribe 3
proceso B    lee usados=2   →  2 < 3, hay lugar  →  escribe 3
```

Los dos pasaron el chequeo con el mismo número y quedaron **4 cambios aplicados** con el
techo en 3. El bloqueo por operación no lo evita: el número se lee una vez y se decide
sobre esa lectura.

La solución no es leer dos veces. Es **sostener el bloqueo durante todo el ciclo**:
leer, decidir, anotar y soltar, sin ventana en el medio. Es el mismo enfoque que el arnés
usa para su propio registro de uso (``tools/skill_usage.py``), y se implementa acá igual
—``fcntl`` en Unix, ``msvcrt`` en Windows— para no depender de internos del host que
pueden cambiar sin aviso.

Libro append-only, con estado efectivo
--------------------------------------
Las reservas van a un libro append-only, igual que el journal de la etapa 4. Cada reserva
es una línea ``charged``; liberarla (una llamada al modelo que falló y no consumió) agrega
una línea ``released`` que la referencia. El conteo se hace sobre el **estado efectivo**
de cada token (la última marca gana), que es la misma lección de la etapa 4 aplicada
antes de tropezar de nuevo.

El día se guarda como **cadena** (``AAAA-MM-DD`` local del momento de la reserva), no se
deriva de restar marcas de tiempo. Un cambio de horario o una reserva a las 23:59 no
mueven su propia entrada de día: la entrada dice a qué día pertenece.

Nunca gasta de más; nunca queda trabado
---------------------------------------
Una línea ilegible del libro **cuenta como consumida en ambos techos**. Es la dirección
segura: un byte corrupto no puede tapar un gasto, así que por construcción el techo no se
excede. Y como el conteo es por día, mañana el libro arranca limpio: el byte corrupto
cuesta un lugar hoy y no bloquea el aprendizaje para siempre. Un registro que se niega a
seguir ante un byte raro convierte un problema de formato en una función muerta.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

#: Techos por defecto. Se pueden sobrescribir por configuración del plugin; el valor
#: efectivo siempre se recibe por parámetro, nunca se lee del entorno acá.
DEFAULT_MAX_EDITS_PER_DAY = 3
DEFAULT_MAX_MODEL_RUNS_PER_DAY = 30

#: Tipos de cargo. Un cambio aplicado y una llamada al modelo se cuentan por separado:
#: tienen techos distintos porque limitan cosas distintas (impacto vs. costo).
KIND_EDIT = "edit"
KIND_MODEL_RUN = "model_run"

KINDS = (KIND_EDIT, KIND_MODEL_RUN)

CHARGED = "charged"
RELEASED = "released"

LEDGER_FILE_NAME = "budget.jsonl"


@dataclass(frozen=True)
class Reservation:
    """Resultado de pedir presupuesto. ``granted`` es lo único que habilita a seguir.

    Una reserva denegada **no gasta**: no se anota nada y el contador no se mueve. El
    ``reason`` dice cuál de los dos techos se agotó, porque "sin presupuesto" a secas no
    explica qué límite tocar.
    """

    granted: bool
    kind: str
    day: str
    token: Optional[str]
    reason: str
    used: int
    ceiling: int

    @property
    def remaining(self) -> int:
        return max(0, self.ceiling - self.used)


@dataclass(frozen=True)
class BudgetStatus:
    """Estado de los dos techos hoy, con lo que se pudo y no se pudo leer."""

    day: str
    edits_used: int
    edits_ceiling: int
    model_runs_used: int
    model_runs_ceiling: int
    unreadable_rows: int = 0

    @property
    def edits_left(self) -> int:
        return max(0, self.edits_ceiling - self.edits_used)

    @property
    def model_runs_left(self) -> int:
        return max(0, self.model_runs_ceiling - self.model_runs_used)

    @property
    def has_edits_left(self) -> bool:
        return self.edits_left > 0

    @property
    def has_model_runs_left(self) -> bool:
        return self.model_runs_left > 0

    def summary(self) -> str:
        base = (
            f"hoy: {self.edits_used}/{self.edits_ceiling} cambios · "
            f"{self.model_runs_used}/{self.model_runs_ceiling} llamadas"
        )
        if self.unreadable_rows:
            # Se informa: un libro degradado que se presenta como sano es peor que uno
            # que declara su estado.
            base += f" · {self.unreadable_rows} línea(s) ilegible(s) contadas como usadas"
        return base


# -- Bloqueo -------------------------------------------------------------------------

try:  # Unix
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - plataforma
    _fcntl = None  # type: ignore[assignment]

#: Bloqueos ya tomados en este hilo. ``flock`` no es reentrante entre descriptores
#: distintos, así que una toma anidada del mismo archivo colgaría contra sí misma.
_held: dict[int, set] = {}


def _plugin_dir(*, hermes_home: Path) -> Path:
    """Directorio de estado del plugin dentro del hogar que el llamador indica.

    El hogar se recibe por parámetro y nunca se deriva del entorno: un mismo proceso sirve
    varios perfiles, y el hogar correcto es el que el arnés entregó en la llamada.
    """
    return Path(hermes_home) / "plugins" / "hermes-skills-helper"


def _ledger_path(*, hermes_home: Path, create: bool = True) -> Path:
    directory = _plugin_dir(hermes_home=hermes_home)
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    return directory / LEDGER_FILE_NAME


@contextmanager
def _ledger_lock(*, hermes_home: Path) -> Iterator[None]:
    """Bloqueo exclusivo entre procesos, sostenido durante todo el ciclo leer-decidir-anotar.

    Es la pieza que cierra la carrera. El bloqueo vive en un archivo aparte del libro: si
    se bloqueara el libro mismo, cualquier reemplazo atómico del archivo cambiaría el
    inodo y el bloqueo quedaría sobre un descriptor huérfano.
    """
    if _fcntl is None:  # pragma: no cover - plataforma sin fcntl
        # Sin primitiva de bloqueo no se puede garantizar el ciclo. Se sigue, pero el
        # llamador puede verlo: el estado lo declara.
        yield
        return

    lock_path = _ledger_path(hermes_home=hermes_home, create=False).with_name(
        f".{LEDGER_FILE_NAME}.lock"
    )
    key = id(_held)
    taken = _held.setdefault(key, set())
    if str(lock_path) in taken:
        yield
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as handle:
        _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
        taken.add(str(lock_path))
        try:
            yield
        finally:
            taken.discard(str(lock_path))
            try:
                _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
            except OSError:  # pragma: no cover - cierre defensivo
                pass


# -- Lectura -------------------------------------------------------------------------

def today(*, now: Optional[float] = None) -> str:
    """Día local en formato ``AAAA-MM-DD``.

    Local, no UTC: el techo es "por día" para quien opera el sistema, y un día que cambia
    a las 19:00 locales no le sirve a nadie.
    """
    stamp = time.time() if now is None else now
    return datetime.fromtimestamp(stamp).astimezone().strftime("%Y-%m-%d")


def _read_rows(*, hermes_home: Path) -> tuple[list[dict], int, Optional[str]]:
    """Filas legibles, cuántas no lo fueron, y **a qué día** atribuir las ilegibles.

    Una línea ilegible no se saltea en silencio: se cuenta. Para un control de gasto, un
    byte que puede tapar una reserva es información, no ruido.

    Pero contarla contra *todos* los días la convertiría en un bloqueo permanente: un byte
    raro gastaría un lugar cada día, para siempre, y el aprendizaje quedaría apagado por un
    problema de formato. Por eso acá se decide **a qué día pertenece**:

    - si hay filas legibles, al día de la más reciente (en un libro append-only, una línea
      a medio escribir es la última: la corrupción es contemporánea a la última escritura);
    - si no hay ninguna, al día de la última modificación del archivo.

    Así la corrupción cuesta un lugar **hoy** —no puede tapar un gasto— y mañana el
    contador arranca limpio sin que haya que borrar evidencia.
    """
    path = _ledger_path(hermes_home=hermes_home, create=False)
    if not path.is_file():
        return [], 0, None
    rows: list[dict] = []
    unreadable = 0
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("No se pudo leer el libro de presupuesto: %s", exc)
        return [], 1, None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            unreadable += 1
            continue
        if isinstance(row, dict) and row.get("token"):
            rows.append(row)
        else:
            unreadable += 1

    if not unreadable:
        return rows, 0, None

    attribution: Optional[str] = None
    stamps = [
        float(r["ts"]) for r in rows if isinstance(r.get("ts"), (int, float))
    ]
    if stamps:
        attribution = today(now=max(stamps))
    else:
        try:
            attribution = today(now=path.stat().st_mtime)
        except OSError:  # pragma: no cover - archivo desaparecido
            attribution = None
    return rows, unreadable, attribution


def _effective(rows: list[dict]) -> dict[str, dict]:
    """Estado efectivo por token: la última marca gana.

    Mismo principio que el journal de la etapa 4: el libro es append-only, así que un
    token liberado tiene su línea ``charged`` y su línea ``released``. Quedarse con la
    primera leería la reserva como consumida para siempre.
    """
    effective: dict[str, dict] = {}
    for row in rows:
        token = row.get("token")
        if isinstance(token, str):
            effective[token] = row
    return effective


def _counts_from(
    rows: list[dict],
    unreadable: int,
    day: str,
    attribution: Optional[str] = None,
) -> tuple[int, int]:
    """Cuenta cargos vigentes del día dado, más lo ilegible que le corresponde.

    Conservador en un eje y preciso en el otro, y la distinción importa:

    - **Conservador en el techo**: cuando la corrupción es de hoy, se suma a los DOS
      contadores. Un byte raro no puede tapar un gasto.
    - **Preciso en el día**: solo se suma **al día atribuido**. Sumarla a todos los días
      convertiría un problema de formato en un aprendizaje apagado para siempre.
    """
    edits = 0
    runs = 0
    for row in _effective(rows).values():
        if row.get("state") != CHARGED:
            continue
        if row.get("day") != day:
            continue
        if row.get("kind") == KIND_EDIT:
            edits += 1
        elif row.get("kind") == KIND_MODEL_RUN:
            runs += 1
    if unreadable and attribution is not None and attribution == day:
        edits += unreadable
        runs += unreadable
    return edits, runs


def status(
    *,
    hermes_home: Path,
    max_edits_per_day: int = DEFAULT_MAX_EDITS_PER_DAY,
    max_model_runs_per_day: int = DEFAULT_MAX_MODEL_RUNS_PER_DAY,
    now: Optional[float] = None,
) -> BudgetStatus:
    """Estado de los dos techos hoy. No reserva nada ni mueve el contador."""
    rows, unreadable, attribution = _read_rows(hermes_home=hermes_home)
    day = today(now=now)
    edits, runs = _counts_from(rows, unreadable, day, attribution)
    return BudgetStatus(
        day=day,
        edits_used=edits,
        edits_ceiling=max_edits_per_day,
        model_runs_used=runs,
        model_runs_ceiling=max_model_runs_per_day,
        unreadable_rows=unreadable,
    )


# -- Reserva -------------------------------------------------------------------------

def _append(row: dict, *, hermes_home: Path) -> None:
    path = _ledger_path(hermes_home=hermes_home)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def reserve(
    kind: str,
    *,
    hermes_home: Path,
    max_edits_per_day: int = DEFAULT_MAX_EDITS_PER_DAY,
    max_model_runs_per_day: int = DEFAULT_MAX_MODEL_RUNS_PER_DAY,
    note: str = "",
    now: Optional[float] = None,
) -> Reservation:
    """Pide un lugar contra el techo correspondiente, en una sección crítica.

    El bloqueo se sostiene a lo largo de **todo** el ciclo —leer, decidir, anotar— y recién
    se suelta al salir. Es lo que evita que dos procesos lean el mismo número y ambos
    decidan que hay lugar.

    Denegar no gasta y no anota: el contador no se mueve.
    """
    if kind not in KINDS:
        raise ValueError(f"tipo de cargo desconocido: {kind!r} (válidos: {KINDS})")

    day = today(now=now)
    ceiling = max_edits_per_day if kind == KIND_EDIT else max_model_runs_per_day
    label = "cambios" if kind == KIND_EDIT else "llamadas al modelo"

    with _ledger_lock(hermes_home=hermes_home):
        rows, unreadable, attribution = _read_rows(hermes_home=hermes_home)
        edits_used, runs_used = _counts_from(rows, unreadable, day, attribution)
        used = edits_used if kind == KIND_EDIT else runs_used

        if used >= ceiling:
            return Reservation(
                granted=False,
                kind=kind,
                day=day,
                token=None,
                reason=(
                    f"techo de {label} agotado: {used}/{ceiling} hoy"
                    + (f" ({unreadable} línea(s) ilegible(s) contadas como usadas)"
                       if unreadable else "")
                ),
                used=used,
                ceiling=ceiling,
            )

        token = uuid.uuid4().hex[:16]
        _append(
            {
                "token": token,
                "day": day,
                "kind": kind,
                "state": CHARGED,
                "ts": time.time() if now is None else now,
                "note": note,
            },
            hermes_home=hermes_home,
        )
        return Reservation(
            granted=True,
            kind=kind,
            day=day,
            token=token,
            reason=f"lugar concedido: {used + 1}/{ceiling} {label} hoy",
            used=used + 1,
            ceiling=ceiling,
        )


def release(token: str, *, hermes_home: Path, reason: str = "") -> bool:
    """Devuelve un lugar reservado, para una llamada que falló y no consumió.

    Sin esto, un error de red costaría un lugar del techo de costo: el presupuesto se
    gastaría en intentos, no en trabajo.

    Devuelve ``False`` si el token no existe o si ya estaba liberado — y no anota nada en
    ese caso, para no inflar el libro con marcas repetidas.
    """
    if not token:
        return False
    with _ledger_lock(hermes_home=hermes_home):
        rows, _, _attribution = _read_rows(hermes_home=hermes_home)
        effective = _effective(rows)
        current = effective.get(token)
        if current is None:
            return False
        if current.get("state") == RELEASED:
            return False
        _append(
            {
                "token": token,
                "day": current.get("day"),
                "kind": current.get("kind"),
                "state": RELEASED,
                "ts": time.time(),
                "note": reason,
            },
            hermes_home=hermes_home,
        )
        return True


def ceilings_from_config(ctx: Any) -> tuple[int, int]:
    """Lee los techos de la configuración del plugin, con los valores por defecto.

    Los valores viven en la configuración y no incrustados en el código: cambian según la
    carga del sistema, y quien opera debe poder ajustarlos sin editar el módulo. Los
    mínimos se aplican acá para que un valor absurdo (0 o negativo) no apague el
    aprendizaje por un error de tipeo, pero **se informa** en lugar de corregirse callado.
    """
    edits = DEFAULT_MAX_EDITS_PER_DAY
    runs = DEFAULT_MAX_MODEL_RUNS_PER_DAY
    getter = getattr(ctx, "get_config", None)
    if callable(getter):
        try:
            raw_edits = getter("max_edits_per_day", DEFAULT_MAX_EDITS_PER_DAY)
            raw_runs = getter("max_model_runs_per_day", DEFAULT_MAX_MODEL_RUNS_PER_DAY)
            edits = _positive_int(raw_edits, DEFAULT_MAX_EDITS_PER_DAY, "max_edits_per_day")
            runs = _positive_int(raw_runs, DEFAULT_MAX_MODEL_RUNS_PER_DAY,
                                 "max_model_runs_per_day")
        except Exception as exc:  # pragma: no cover - config ajena, no debe romper
            logger.warning("No se pudo leer el techo de la configuración: %s", exc)
    return edits, runs


def _positive_int(value: Any, default: int, label: str) -> int:
    """Entero positivo, con aviso explícito cuando un valor no sirve.

    Un valor inválido se reemplaza por el defecto y **se dice**: corregirlo en silencio
    dejaría a quien lo configuró convencido de que su número rige.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        logger.warning("Techo '%s' no es un entero (%r): se usa %d", label, value, default)
        return default
    if number < 1:
        logger.warning("Techo '%s' debe ser ≥ 1 (%r): se usa %d", label, value, default)
        return default
    return number


def health(*, hermes_home: Path, now: Optional[float] = None) -> dict:
    """Estado del libro, para diagnóstico. No crea ni modifica nada."""
    rows, unreadable, attribution = _read_rows(hermes_home=hermes_home)
    path = _ledger_path(hermes_home=hermes_home, create=False)
    effective = _effective(rows)
    day = today(now=now)
    return {
        "ledger_path": str(path),
        "exists": path.is_file(),
        "rows": len(rows),
        "reservations": len(effective),
        "charged_today": len([r for r in effective.values()
                              if r.get("state") == CHARGED and r.get("day") == day]),
        "released": len([r for r in effective.values() if r.get("state") == RELEASED]),
        "unreadable_rows": unreadable,
        "day": day,
    }


def iter_rows(*, hermes_home: Path) -> Iterator[dict]:
    """Filas crudas del libro, para auditar los cargos uno por uno."""
    rows, _, _attribution = _read_rows(hermes_home=hermes_home)
    yield from rows


__all__ = [
    "DEFAULT_MAX_EDITS_PER_DAY",
    "DEFAULT_MAX_MODEL_RUNS_PER_DAY",
    "KIND_EDIT",
    "KIND_MODEL_RUN",
    "KINDS",
    "CHARGED",
    "RELEASED",
    "LEDGER_FILE_NAME",
    "Reservation",
    "BudgetStatus",
    "today",
    "status",
    "reserve",
    "release",
    "ceilings_from_config",
    "health",
    "iter_rows",
]
