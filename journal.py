"""Etapa 4 — Journal y rollback. Determinista, sin modelo.

Todo cambio aplicado queda registrado y es **reversible**. El requisito es duro y está en
el diseño: **sin rollback verificado no hay aplicación de cambios** — se propone y se
detiene.

Por qué no se delega al host
----------------------------
El arnés no ofrece un backup con restauración de un skill. Verificado: no hay directorio
de backups ni API de rollback en ``tools/skill_usage.py`` ni en el gestor de skills. Lo
más cercano es el *ledger* del arnés, que es telemetría sin restauración.

De ahí las dos piezas de este módulo:

```
journal   registro append-only de qué se cambió, con el contenido ANTERIOR
rollback  un comando que devuelve el skill a su estado previo, byte a byte
```

El journal es append-only a propósito: es la evidencia de lo que se hizo, y una evidencia
que se puede editar no sirve como evidencia.

Qué se guarda antes de escribir
-------------------------------
El **contenido completo** anterior, no un diff. Un diff depende de que el archivo esté en
el estado que el diff espera; si un tercero lo tocó en el medio, aplicarlo en reversa
produce algo que no es ni el estado viejo ni el nuevo. El contenido completo restaura de
forma determinista.

Los hashes van en ambos extremos para que el rollback pueda **verificar** que restauró lo
correcto, en lugar de afirmarlo.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional

try:  # cargado como paquete por el arnés
    from .effect.checker import sha256_text
except ImportError:  # cargado como módulo suelto (tests, ejecución directa)
    from effect.checker import sha256_text  # type: ignore

logger = logging.getLogger(__name__)

#: Nombre del archivo de journal dentro del directorio de estado del plugin.
JOURNAL_FILE_NAME = "journal.jsonl"

#: Nombre del subdirectorio de respaldos.
BACKUPS_DIR_NAME = "backups"

#: Estados de una entrada. ``applied`` es el único que habilita un rollback.
#:
#: ``pending`` y ``staged`` NO son lo mismo, y confundirlos produce dos errores a la vez:
#:
#: ``pending``  la escritura se empezó y no se completó. Es un problema: hay un respaldo
#:              sin cambio, y el diagnóstico debe reportarlo.
#: ``staged``   la escritura no fue rechazada: quedó **en cola de aprobación del arnés**.
#:              No es un problema y no hay nada que revertir — todavía. Cuando Mauro
#:              apruebe (o rechace), el skill cambia sin que este módulo se entere: el
#:              gate es del host. De ahí ``proposed_hash`` y la reconciliación.
#:
#: Reportar un ``staged`` como ``pending`` mostraría una escritura rota para cada
#: propuesta esperando aprobación — una alarma falsa en el diagnóstico que más se mira.
PENDING = "pending"
STAGED = "staged"
APPLIED = "applied"
ROLLED_BACK = "rolled_back"
FAILED = "failed"


@dataclass(frozen=True)
class JournalEntry:
    """Un cambio aplicado, con todo lo necesario para revertirlo.

    ``before_hash`` y ``after_hash`` describen los extremos. El rollback comprueba que el
    contenido actual coincide con ``after_hash`` antes de restaurar: si no coincide, un
    tercero editó el skill y revertir pisaría ese trabajo — se niega en lugar de destruir.
    """

    entry_id: str
    skill_name: str
    skill_path: str
    action: str
    before_hash: Optional[str]
    after_hash: Optional[str]
    backup_file: Optional[str]
    applied_ts: float
    status: str = APPLIED
    rolled_back_ts: Optional[float] = None
    reason: str = ""
    metadata: dict = field(default_factory=dict)
    #: Hash del contenido que se PROPUSO, cuando el arnés dejó el cambio en cola. Es lo
    #: que permite reconciliar después: el gate del host aplica el cambio sin avisar a
    #: este plugin, así que la única forma de saber si se aprobó es comparar el archivo
    #: contra lo que se propuso.
    proposed_hash: Optional[str] = None
    #: Identificador que el arnés le dio al cambio en su cola, para poder correlacionarlo.
    pending_id: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @staticmethod
    def from_dict(data: dict) -> "JournalEntry":
        known = {f for f in JournalEntry.__dataclass_fields__}  # type: ignore[attr-defined]
        return JournalEntry(**{k: v for k, v in data.items() if k in known})

    @property
    def is_revertible(self) -> bool:
        """¿Se puede revertir? Solo si se aplicó, no se revirtió, y hay respaldo."""
        return self.status == APPLIED and bool(self.backup_file)

    @property
    def is_staged(self) -> bool:
        """¿Espera aprobación del arnés? Sin aplicar todavía, nada que revertir."""
        return self.status == STAGED


@dataclass(frozen=True)
class RollbackResult:
    """Resultado de un intento de rollback, con su razón."""

    success: bool
    entry_id: str
    message: str
    restored_hash: Optional[str] = None


def journal_dir(*, hermes_home: Path) -> Path:
    """Directorio de estado del plugin, dentro del hogar que el llamador indica.

    ``hermes_home`` se recibe por parámetro y nunca se deriva del entorno: un proceso
    sirve varios perfiles y el hogar correcto es el que el arnés entregó en la llamada.
    """
    return Path(hermes_home) / "plugins" / "hermes-skills-helper"


def _journal_path(*, hermes_home: Path, create: bool = True) -> Path:
    directory = journal_dir(hermes_home=hermes_home)
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    return directory / JOURNAL_FILE_NAME


def _backups_dir(*, hermes_home: Path, create: bool = True) -> Path:
    directory = journal_dir(hermes_home=hermes_home) / BACKUPS_DIR_NAME
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    return directory


def read_entries(*, hermes_home: Path) -> tuple[JournalEntry, ...]:
    """Todas las entradas del journal, en orden de escritura.

    Una línea corrupta no invalida las demás: se saltea y se registra. Perder el journal
    entero por un byte mal escrito convertiría un problema de formato en una pérdida de
    evidencia.
    """
    path = _journal_path(hermes_home=hermes_home, create=False)
    if not path.is_file():
        return ()
    entries: list[JournalEntry] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(JournalEntry.from_dict(json.loads(line)))
        except (ValueError, TypeError) as exc:
            logger.warning("Línea de journal ilegible, se saltea: %s", exc)
    return tuple(entries)


def _append(entry: JournalEntry, *, hermes_home: Path) -> None:
    """Agrega una entrada. Append-only: nunca se reescribe el archivo."""
    path = _journal_path(hermes_home=hermes_home)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(entry.to_json() + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def append_rollback_marker(entry: JournalEntry, *, hermes_home: Path) -> None:
    """Registra que una entrada fue revertida, **sin** modificar la original.

    El journal es append-only: el estado nuevo es una línea nueva que referencia a la
    vieja. Reescribir la entrada original borraría el hecho de que el cambio estuvo
    aplicado, que es justamente lo que el journal existe para conservar.
    """
    _append(entry, hermes_home=hermes_home)


def _state_of(entry_id: str, entries: tuple[JournalEntry, ...]) -> Optional[JournalEntry]:
    """Estado efectivo de una entrada: la última marca gana."""
    latest: Optional[JournalEntry] = None
    for entry in entries:
        if entry.entry_id == entry_id:
            latest = entry
    return latest


def find_entry(entry_id: str, *, hermes_home: Path) -> Optional[JournalEntry]:
    """Estado efectivo de una entrada, o ``None`` si no existe."""
    return _state_of(entry_id, read_entries(hermes_home=hermes_home))


def last_applied(*, hermes_home: Path, skill_name: Optional[str] = None) -> Optional[JournalEntry]:
    """Última entrada revertible, opcionalmente filtrando por skill.

    Se toma el **estado efectivo** de cada entrada, no la última línea. El journal es
    append-only: tras un rollback siguen en el archivo tanto la línea ``applied`` original
    como la ``rolled_back``. Mirar la última línea que coincide con el filtro devolvería
    la ``applied`` vieja una y otra vez, y el segundo rollback fallaría con "ya fue
    revertida" en lugar de retroceder al cambio anterior.
    """
    effective: dict[str, JournalEntry] = {}
    for entry in read_entries(hermes_home=hermes_home):
        effective[entry.entry_id] = entry  # la última marca gana

    for entry in reversed(tuple(effective.values())):
        if not entry.is_revertible:
            continue
        if skill_name is not None and entry.skill_name != skill_name:
            continue
        return entry
    return None


def take_backup(
    *,
    skill_path: Path,
    skill_name: str,
    hermes_home: Path,
    entry_id: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Guarda el contenido actual y devuelve ``(nombre_del_respaldo, hash)``.

    Se llama **antes** de escribir. Si no hay nada que respaldar (el skill es nuevo), el
    respaldo es ``None`` y el hash también: el rollback de una creación es un borrado, y
    eso se distingue de restaurar contenido.

    El respaldo se escribe con el hash en el nombre: un respaldo cuyo contenido no se
    puede correlacionar con su hash no sirve para verificar una restauración.
    """
    if not skill_path.is_file():
        return None, None
    try:
        content = skill_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("No se pudo respaldar %s: %s", skill_path, exc)
        return None, None

    digest = sha256_text(content)
    token = entry_id or uuid.uuid4().hex[:12]
    backup_name = f"{skill_name}.{token}.{digest[:12]}.bak"
    backup_path = _backups_dir(hermes_home=hermes_home) / backup_name
    try:
        backup_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        logger.warning("No se pudo escribir el respaldo de %s: %s", skill_name, exc)
        return None, None
    return backup_name, digest


def record_before_write(
    *,
    skill_name: str,
    skill_path: Path,
    action: str,
    hermes_home: Path,
    reason: str = "",
    metadata: Optional[dict] = None,
) -> JournalEntry:
    """Respalda y registra una entrada ``pending`` **antes** de que el cambio ocurra.

    El orden importa y es el punto del módulo: si se registrara después, una escritura que
    falla a mitad dejaría un cambio sin evidencia ni forma de revertirse.
    """
    entry_id = uuid.uuid4().hex[:16]
    backup_name, before_hash = take_backup(
        skill_path=skill_path, skill_name=skill_name, hermes_home=hermes_home,
        entry_id=entry_id,
    )
    entry = JournalEntry(
        entry_id=entry_id,
        skill_name=skill_name,
        skill_path=str(skill_path),
        action=action,
        before_hash=before_hash,
        after_hash=None,
        backup_file=backup_name,
        applied_ts=time.time(),
        status=PENDING,
        reason=reason,
        metadata=metadata or {},
    )
    _append(entry, hermes_home=hermes_home)
    return entry


def record_after_write(entry: JournalEntry, *, new_content: str, hermes_home: Path) -> JournalEntry:
    """Marca la entrada como ``applied`` con el hash del contenido nuevo.

    Una entrada que queda en ``pending`` significa que la escritura no se completó. Es un
    estado legítimo y distinguible: no se la hace pasar por aplicada.
    """
    applied = JournalEntry(
        **{
            **asdict(entry),
            "after_hash": sha256_text(new_content),
            "status": APPLIED,
        }
    )
    _append(applied, hermes_home=hermes_home)
    return applied


def mark_staged(
    entry: JournalEntry,
    *,
    proposed_content: str,
    pending_id: Optional[str],
    hermes_home: Path,
) -> JournalEntry:
    """Marca que el arnés **encoló** el cambio en lugar de aplicarlo.

    Un ``staged`` no es una escritura incompleta: es una propuesta esperando la aprobación
    de Mauro. Se guarda el hash de lo propuesto porque el gate es del host y aplicará el
    cambio **sin avisar a este módulo**: comparar el archivo contra ese hash es la única
    forma de saber, después, si se aprobó.
    """
    staged = JournalEntry(
        **{
            **asdict(entry),
            "status": STAGED,
            "proposed_hash": sha256_text(proposed_content),
            "pending_id": pending_id,
        }
    )
    append_rollback_marker(staged, hermes_home=hermes_home)
    return staged


def _pending_lookup_default(pending_id: str, subsystem: str) -> bool:
    """¿Sigue ese pending en la cola del arnés? La consulta real.

    Si la cola no se puede consultar, se responde ``True``: es la lectura conservadora.
    Asumir que algo fue descartado cuando no se pudo verificar cerraría una entrada que
    todavía podía aprobarse.
    """
    try:
        from tools.write_approval import get_pending  # noqa: PLC0415

        return get_pending(subsystem, pending_id) is not None
    except Exception as exc:
        logger.debug("No se pudo consultar la cola del arnés: %s", exc)
        return True


def _pending_state(
    pending_id: Optional[str],
    *,
    subsystem: str = "skills",
    lookup: Optional[Callable[[str, str], bool]] = None,
) -> str:
    """Estado del cambio en la cola del arnés: ``encolado`` / ``descartado`` / ``sin-id``.

    Distinguir "sigue en cola" de "ya no está" es lo que evita que una entrada quede
    esperando para siempre.

    La consulta es **inyectable** (``lookup``) y no se llama directo: sin eso, la
    reconciliación dependería del estado real de la cola del arnés, y sus tests pasarían o
    fallarían según lo que hubiera en el disco de quien los corre. Un test que depende del
    entorno no prueba el código, prueba la máquina.
    """
    if not pending_id:
        return "sin-id"
    consulta = lookup or _pending_lookup_default
    return "encolado" if consulta(pending_id, subsystem) else "descartado"


def reconcile_staged(
    *,
    hermes_home: Path,
    pending_lookup: Optional[Callable[[str, str], bool]] = None,
) -> list:
    """Detecta los cambios encolados que el arnés aplicó o rechazó, y los registra.

    El gate de aprobación es del host: cuando Mauro aprueba en ``/skills pending``, el
    archivo cambia y este módulo no se entera. Sin esta reconciliación, el journal
    quedaría diciendo ``staged`` para siempre, y la etapa 5 —que califica los cambios
    **aplicados**— nunca vería nada que medir.

    Devuelve las entradas reconciliadas, con su desenlace.
    """
    resultado = []
    efectivas: dict[str, JournalEntry] = {}
    for entry in read_entries(hermes_home=hermes_home):
        efectivas[entry.entry_id] = entry

    for entry in efectivas.values():
        if entry.status != STAGED or not entry.proposed_hash:
            continue
        skill_path = Path(entry.skill_path)
        if not skill_path.is_file():
            continue
        try:
            actual = sha256_text(skill_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("No se pudo reconciliar %s: %s", entry.entry_id, exc)
            continue

        # ¿El pending sigue en la cola del arnés? Sin esto, una propuesta que Mauro
        # DESCARTÓ (o que se perdió con su cola) se reportaría como "todavía en cola"
        # para siempre, y el journal mentiría sobre un cambio que ya no va a ocurrir.
        estado_cola = _pending_state(entry.pending_id, lookup=pending_lookup)

        if actual == entry.proposed_hash:
            # Se aprobó: el archivo ahora tiene lo propuesto. El hash "posterior" es el
            # propuesto, y desde acá la entrada vuelve a ser un cambio normal, calificable
            # por la etapa 5.
            aplicada = JournalEntry(
                **{
                    **asdict(entry),
                    "status": APPLIED,
                    "after_hash": entry.proposed_hash,
                    "reason": (entry.reason + " | aprobado por el arnés").strip(" |"),
                }
            )
            append_rollback_marker(aplicada, hermes_home=hermes_home)
            resultado.append((aplicada, "aprobado"))
        elif actual == entry.before_hash:
            if estado_cola == "descartado":
                # El pending ya no está en la cola y el archivo sigue intacto: se
                # descartó. Se cierra la entrada para que no quede esperando algo que no
                # va a pasar — un ``staged`` eterno es una mentira silenciosa.
                cerrada = JournalEntry(
                    **{
                        **asdict(entry),
                        "status": FAILED,
                        "reason": (entry.reason + " | descartado en la cola del arnés"
                                   ).strip(" |"),
                    }
                )
                append_rollback_marker(cerrada, hermes_home=hermes_home)
                resultado.append((cerrada, "descartado"))
            else:
                # Sigue intacto y el pending sigue encolado: la aprobación todavía no
                # ocurrió. No se toca nada, y NO se reporta como problema: esperar no es
                # fallar.
                resultado.append((entry, "todavía en cola"))
        else:
            # El archivo cambió por otra vía: ni lo propuesto ni lo previo. Alguien más lo
            # tocó. Se declara en lugar de asumir cualquiera de los dos desenlaces.
            resultado.append((entry, "cambió por otra vía"))
    return resultado


def rollback(entry_id: str, *, hermes_home: Path, force: bool = False) -> RollbackResult:
    """Devuelve un skill a su estado previo, byte a byte.

    Tres comprobaciones antes de tocar nada:

    1. La entrada existe, fue aplicada, y tiene respaldo.
    2. El contenido actual coincide con ``after_hash``. Si no coincide, un tercero editó
       el skill después del cambio: revertir borraría ese trabajo. Se **niega** salvo
       ``force``.
    3. El respaldo existe y su hash coincide con el registrado. Un respaldo corrupto no
       puede restaurar nada, y descubrirlo *después* de sobrescribir sería tarde.

    La restauración se verifica: se relee el archivo y se compara el hash con
    ``before_hash``. Un rollback que dice haber restaurado sin verificarlo es una
    afirmación, no una restauración.
    """
    entry = find_entry(entry_id, hermes_home=hermes_home)
    if entry is None:
        return RollbackResult(False, entry_id, "no existe una entrada con ese id")
    if entry.status == ROLLED_BACK:
        return RollbackResult(False, entry_id, "esa entrada ya fue revertida")
    if entry.is_staged:
        return RollbackResult(
            False, entry_id,
            "el cambio está en la cola de aprobación del arnés, no aplicado: no hay nada "
            "que revertir todavía",
        )
    if entry.status != APPLIED:
        return RollbackResult(
            False, entry_id,
            f"la entrada está en estado '{entry.status}': el cambio no llegó a aplicarse",
        )
    # El chequeo del respaldo va antes que cualquier otro descarte: el motivo REAL por el
    # que esta entrada no se puede revertir es que no hay contenido anterior que
    # restaurar. Un mensaje genérico obligaría a leer el código para entenderlo.
    if not entry.backup_file:
        return RollbackResult(
            False, entry_id,
            "no hay respaldo del contenido anterior: no se puede restaurar sin adivinar",
        )

    backup_path = _backups_dir(hermes_home=hermes_home, create=False) / entry.backup_file
    if not backup_path.is_file():
        return RollbackResult(False, entry_id, f"falta el respaldo {entry.backup_file}")

    try:
        backup_content = backup_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return RollbackResult(False, entry_id, f"no se pudo leer el respaldo: {exc}")

    if entry.before_hash and sha256_text(backup_content) != entry.before_hash:
        return RollbackResult(
            False, entry_id,
            "el respaldo no coincide con el hash registrado: está corrupto",
        )

    skill_path = Path(entry.skill_path)
    if not skill_path.is_file():
        return RollbackResult(False, entry_id, f"el skill ya no está en {entry.skill_path}")

    try:
        current = skill_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return RollbackResult(False, entry_id, f"no se pudo leer el skill: {exc}")

    current_hash = sha256_text(current)
    if entry.after_hash and current_hash != entry.after_hash and not force:
        return RollbackResult(
            False, entry_id,
            "el contenido cambió desde que se aplicó el cambio: revertir borraría ese "
            "trabajo. Usar force para imponerlo",
        )

    try:
        skill_path.write_text(backup_content, encoding="utf-8")
    except OSError as exc:
        return RollbackResult(False, entry_id, f"no se pudo escribir la restauración: {exc}")

    restored = sha256_text(skill_path.read_text(encoding="utf-8"))
    if entry.before_hash and restored != entry.before_hash:
        return RollbackResult(
            False, entry_id,
            "la restauración no coincidió con el hash previo: el archivo quedó en un "
            "estado distinto al original",
            restored_hash=restored,
        )

    rolled = JournalEntry(
        **{
            **asdict(entry),
            "status": ROLLED_BACK,
            "rolled_back_ts": time.time(),
            "reason": (entry.reason + " | revertido").strip(" |"),
        }
    )
    append_rollback_marker(rolled, hermes_home=hermes_home)
    return RollbackResult(
        True, entry_id, "restaurado y verificado contra el hash previo",
        restored_hash=restored,
    )


def rollback_last(*, hermes_home: Path, skill_name: Optional[str] = None) -> RollbackResult:
    """Revierte el último cambio aplicado, o el último de un skill dado."""
    entry = last_applied(hermes_home=hermes_home, skill_name=skill_name)
    if entry is None:
        return RollbackResult(False, "", "no hay cambios revertibles en el journal")
    return rollback(entry.entry_id, hermes_home=hermes_home)


def health(*, hermes_home: Path) -> dict:
    """Estado del journal, para diagnóstico.

    No crea nada: un diagnóstico que materializa lo que vino a inspeccionar destruye su
    propia evidencia.
    """
    entries = read_entries(hermes_home=hermes_home)
    path = _journal_path(hermes_home=hermes_home, create=False)

    # Estado EFECTIVO, no recuento de líneas. El journal es append-only: una entrada que
    # se aplicó tiene su línea ``pending`` y su línea ``applied`` en el archivo. Contar
    # líneas reportaría cada cambio terminado como una escritura incompleta — una alarma
    # falsa en el diagnóstico que más se va a mirar.
    effective: dict[str, JournalEntry] = {}
    for entry in entries:
        effective[entry.entry_id] = entry

    estados = tuple(effective.values())
    revertibles = [e for e in estados if e.is_revertible]
    pendientes = [e for e in estados if e.status == PENDING]
    encolados = [e for e in estados if e.is_staged]
    return {
        "journal_path": str(path),
        "exists": path.is_file(),
        "entries": len(entries),
        "changes": len(estados),
        "revertible_entries": len(revertibles),
        # ``pending`` es una escritura que no se completó: un hallazgo.
        "pending_entries": len(pendientes),
        # ``staged`` es una propuesta esperando aprobación: NO es un problema. Van en
        # campos distintos porque significan cosas distintas.
        "staged_entries": len(encolados),
        "staged_ids": [e.entry_id for e in encolados],
        "rolled_back": len([e for e in estados if e.status == ROLLED_BACK]),
        "pending_ids": [e.entry_id for e in pendientes],
    }


def iter_backups(*, hermes_home: Path) -> Iterator[Path]:
    """Respaldos en disco. Para auditar que no se acumulen sin control."""
    directory = _backups_dir(hermes_home=hermes_home, create=False)
    if not directory.is_dir():
        return
    yield from sorted(directory.glob("*.bak"))


__all__ = [
    "JOURNAL_FILE_NAME",
    "BACKUPS_DIR_NAME",
    "PENDING",
    "STAGED",
    "APPLIED",
    "ROLLED_BACK",
    "FAILED",
    "JournalEntry",
    "RollbackResult",
    "journal_dir",
    "read_entries",
    "find_entry",
    "last_applied",
    "take_backup",
    "record_before_write",
    "record_after_write",
    "FAILED",
    "mark_staged",
    "reconcile_staged",
    "rollback",
    "rollback_last",
    "health",
    "iter_backups",
]
