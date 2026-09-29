"""Tests de la etapa 4 — journal y rollback.

El requisito del diseño es duro:

    "sin rollback verificado no hay aplicación de cambios"

Así que estos tests no verifican que el journal *anote*: verifican que **restaura**. El
caso central es escribir un skill real, cambiarlo, revertir, y comprobar que el archivo en
disco quedó **byte a byte** como estaba — con el hash como árbitro, no la impresión.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from effect.checker import sha256_text  # noqa: E402
# El estado ``FAILED`` del journal se importa con alias: este archivo ya usa el nombre
# ``FAILED`` para su propia lista de tests que fallaron, y sin el alias la comparación
# quedaría contra esa lista (comparaba 'failed' == []  y siempre daba falso).
from journal import FAILED as STATE_FAILED  # noqa: E402
from journal import (  # noqa: E402
    APPLIED,
    PENDING,
    ROLLED_BACK,
    STAGED,
    mark_staged,
    reconcile_staged,
    find_entry,
    health,
    iter_backups,
    last_applied,
    read_entries,
    record_after_write,
    record_before_write,
    rollback,
    rollback_last,
)

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


ORIGINAL = """---
name: demo-skill
description: Use when probando. Un skill de prueba.
---

# Demo

Contenido original.
"""

CAMBIADO = """---
name: demo-skill
description: Use when probando. Un skill de prueba.
---

# Demo

Contenido NUEVO, con la mejora aplicada.
"""


class Sandbox:
    """Hogar temporal con un skill real en disco."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="hsh-test-"))
        self.home = self.root / "hermes"
        self.skill_dir = self.root / "skills" / "demo-skill"
        self.skill_dir.mkdir(parents=True, exist_ok=True)
        self.skill_path = self.skill_dir / "SKILL.md"
        self.skill_path.write_text(ORIGINAL, encoding="utf-8")

    def apply_change(self, new_content: str = CAMBIADO, **kwargs) -> tuple:
        """Simula el ciclo completo: respaldar → escribir → registrar."""
        entry = record_before_write(
            skill_name="demo-skill",
            skill_path=self.skill_path,
            action="patch",
            hermes_home=self.home,
            reason="test",
            **kwargs,
        )
        self.skill_path.write_text(new_content, encoding="utf-8")
        applied = record_after_write(entry, new_content=new_content, hermes_home=self.home)
        return entry, applied

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def test_full_cycle_restores_bytes() -> None:
    print("\ncierre del ciclo: la restauración es byte a byte")

    box = Sandbox()
    try:
        original_hash = sha256_text(ORIGINAL)
        check("el skill arranca con el contenido original",
              sha256_text(box.skill_path.read_text(encoding="utf-8")) == original_hash)

        entry, applied = box.apply_change()
        check("el cambio se aplicó", applied.status == APPLIED)
        check("el hash posterior es el del contenido nuevo",
              applied.after_hash == sha256_text(CAMBIADO))
        check("se guardó el hash anterior",
              applied.before_hash == original_hash)

        result = rollback(entry.entry_id, hermes_home=box.home)
        check("el rollback tuvo éxito", result.success is True, result.message)
        check("el rollback declara el hash restaurado",
              result.restored_hash == original_hash)

        on_disk = box.skill_path.read_text(encoding="utf-8")
        check("EL ARCHIVO EN DISCO VOLVIÓ AL CONTENIDO ORIGINAL",
              on_disk == ORIGINAL, f"largo={len(on_disk)} vs {len(ORIGINAL)}")
        check("hash en disco == hash original (el árbitro, no la impresión)",
              sha256_text(on_disk) == original_hash)
        check("la mejora ya no está en el archivo", "NUEVA" not in on_disk)

        final = find_entry(entry.entry_id, hermes_home=box.home)
        check("la entrada quedó marcada como revertida", final.status == ROLLED_BACK)
        check("quedó registrado cuándo se revirtió", final.rolled_back_ts is not None)
    finally:
        box.cleanup()


def test_journal_is_append_only() -> None:
    print("\nel journal es append-only")

    box = Sandbox()
    try:
        entry = record_before_write(
            skill_name="demo-skill", skill_path=box.skill_path, action="patch",
            hermes_home=box.home,
        )
        box.skill_path.write_text(CAMBIADO, encoding="utf-8")
        record_after_write(entry, new_content=CAMBIADO, hermes_home=box.home)

        antes = read_entries(hermes_home=box.home)
        rollback(entry.entry_id, hermes_home=box.home)
        despues = read_entries(hermes_home=box.home)

        check("revertir AGREGA una entrada, no reescribe la anterior",
              len(despues) > len(antes), f"{len(antes)} → {len(despues)}")
        check("el hecho de que estuvo aplicado sigue en el journal",
              any(e.status == APPLIED for e in despues))
        check("y también que fue revertido",
              any(e.status == ROLLED_BACK for e in despues))
    finally:
        box.cleanup()


def test_refuses_to_clobber_third_party_edit() -> None:
    print("\nse niega a pisar una edición ajena")

    box = Sandbox()
    try:
        entry, _ = box.apply_change()
        # Un tercero edita el skill después de nuestro cambio.
        box.skill_path.write_text("Un tercero lo editó.\n", encoding="utf-8")

        result = rollback(entry.entry_id, hermes_home=box.home)
        check("se niega a revertir sin force", result.success is False)
        check("la razón nombra el riesgo de borrar trabajo ajeno",
              "trabajo" in result.message or "cambió" in result.message, result.message)
        check("NO tocó el archivo",
              box.skill_path.read_text(encoding="utf-8") == "Un tercero lo editó.\n")

        # Con force explícito sí lo impone: la decisión es del llamador.
        forced = rollback(entry.entry_id, hermes_home=box.home, force=True)
        check("con force sí revierte", forced.success is True, forced.message)
        check("y restaura el original",
              box.skill_path.read_text(encoding="utf-8") == ORIGINAL)
    finally:
        box.cleanup()


def test_refuses_without_backup() -> None:
    print("\nsin respaldo no se adivina")

    box = Sandbox()
    try:
        # Skill que no existía: no hay nada que respaldar.
        nuevo = box.root / "skills" / "nuevo" / "SKILL.md"
        nuevo.parent.mkdir(parents=True, exist_ok=True)
        entry = record_before_write(
            skill_name="nuevo", skill_path=nuevo, action="create", hermes_home=box.home,
        )
        check("una creación no tiene respaldo", entry.backup_file is None)
        check("y el hash previo es None, no un hash vacío", entry.before_hash is None)

        nuevo.write_text("contenido nuevo\n", encoding="utf-8")
        record_after_write(entry, new_content="contenido nuevo\n", hermes_home=box.home)

        state = find_entry(entry.entry_id, hermes_home=box.home)
        check("una creación no es revertible por restauración",
              state.is_revertible is False)

        result = rollback(entry.entry_id, hermes_home=box.home)
        check("el rollback se niega y lo dice", result.success is False)
        check("la razón explica que no hay respaldo",
              "respaldo" in result.message, result.message)
    finally:
        box.cleanup()


def test_detects_corrupt_backup() -> None:
    print("\nun respaldo corrupto no puede restaurar")

    box = Sandbox()
    try:
        entry, _ = box.apply_change()
        backup = next(iter(iter_backups(hermes_home=box.home)))
        backup.write_text("contenido adulterado\n", encoding="utf-8")

        result = rollback(entry.entry_id, hermes_home=box.home)
        check("detecta el respaldo corrupto", result.success is False)
        check("la razón lo nombra", "corrupto" in result.message, result.message)
        check("NO sobrescribió el skill con basura",
              box.skill_path.read_text(encoding="utf-8") == CAMBIADO)
    finally:
        box.cleanup()


def test_pending_is_visible_not_hidden() -> None:
    print("\nuna escritura incompleta queda a la vista")

    box = Sandbox()
    try:
        record_before_write(
            skill_name="demo-skill", skill_path=box.skill_path, action="patch",
            hermes_home=box.home,
        )
        # La escritura nunca se completa: no se llama a record_after_write.

        h = health(hermes_home=box.home)
        check("el journal lo reporta como pending", h["pending_entries"] == 1)
        check("expone el id para poder investigarlo", len(h["pending_ids"]) == 1)

        entries = read_entries(hermes_home=box.home)
        check("una entrada pending NO se hace pasar por aplicada",
              entries[0].status == PENDING)
        check("y no se ofrece como revertible",
              last_applied(hermes_home=box.home) is None)

        # Un cambio YA aplicado no debe figurar como pendiente aunque su línea
        # ``pending`` siga en el archivo: el journal es append-only.
        entry2, _ = box.apply_change()
        h2 = health(hermes_home=box.home)
        check("un cambio aplicado NO se reporta como escritura incompleta",
              h2["pending_entries"] == 1, f"pending={h2['pending_entries']}")
        check("el diagnóstico distingue líneas de cambios",
              h2["changes"] == 2 and h2["entries"] > h2["changes"],
              f"entries={h2['entries']} changes={h2['changes']}")
        check("cuenta el cambio aplicado como revertible",
              h2["revertible_entries"] == 1, f"{h2['revertible_entries']}")
    finally:
        box.cleanup()


def test_staged_is_not_a_broken_write() -> None:
    print("\nun cambio en cola NO es una escritura rota")

    box = Sandbox()
    try:
        contenido_propuesto = CAMBIADO
        entry = record_before_write(
            skill_name="demo-skill", skill_path=box.skill_path, action="patch",
            hermes_home=box.home,
        )
        # El arnés encola: el archivo NO cambia.
        mark_staged(entry, proposed_content=contenido_propuesto, pending_id="abc123",
                    hermes_home=box.home)

        h = health(hermes_home=box.home)
        check("NO se reporta como escritura incompleta",
              h["pending_entries"] == 0, f"pending={h['pending_entries']}")
        check("se reporta como encolado", h["staged_entries"] == 1,
              f"staged={h['staged_entries']}")
        check("expone el id del encolado", len(h["staged_ids"]) == 1)

        estado = find_entry(entry.entry_id, hermes_home=box.home)
        check("el estado es staged", estado.status == STAGED, estado.status)
        check("guarda el hash de lo propuesto", bool(estado.proposed_hash))
        check("guarda el pending_id del arnés", estado.pending_id == "abc123")
        check("NO es revertible todavía", estado.is_revertible is False)

        r = rollback(entry.entry_id, hermes_home=box.home)
        check("revertirlo se rechaza con la razón correcta", r.success is False)
        check("la razón explica que espera aprobación",
              "cola de aprobación" in r.message, r.message)
    finally:
        box.cleanup()


def test_reconcile_detects_approval() -> None:
    print("\nla reconciliación detecta que el arnés aprobó")

    box = Sandbox()
    try:
        entry = record_before_write(
            skill_name="demo-skill", skill_path=box.skill_path, action="patch",
            hermes_home=box.home,
        )
        mark_staged(entry, proposed_content=CAMBIADO, pending_id="p1",
                    hermes_home=box.home)

        # La cola se inyecta: sin esto el test dependería del disco de quien lo corre.
        sigue_encolado = lambda pid, sub: True  # noqa: E731

        # Todavía sin aprobar: el archivo está como estaba.
        r1 = reconcile_staged(hermes_home=box.home, pending_lookup=sigue_encolado)
        check("sin aprobar, sigue en cola", r1 and r1[0][1] == "todavía en cola",
              str(r1 and r1[0][1]))
        check("y NO se marcó como aplicada",
              find_entry(entry.entry_id, hermes_home=box.home).status == STAGED)

        # Mauro aprueba: el host escribe el contenido propuesto.
        box.skill_path.write_text(CAMBIADO, encoding="utf-8")

        r2 = reconcile_staged(hermes_home=box.home, pending_lookup=sigue_encolado)
        check("detecta la aprobación", r2 and r2[0][1] == "aprobado",
              str(r2 and r2[0][1]))
        estado = find_entry(entry.entry_id, hermes_home=box.home)
        check("la entrada pasa a aplicada", estado.status == APPLIED, estado.status)
        check("con el hash posterior puesto", bool(estado.after_hash))
        check("y ahora SÍ es revertible", estado.is_revertible is True)
        check("el journal conservó que estuvo encolada",
              any(e.status == STAGED for e in read_entries(hermes_home=box.home)))

        check("una segunda reconciliación no duplica nada",
              not [x for x in reconcile_staged(hermes_home=box.home,
                                              pending_lookup=sigue_encolado)
                   if x[1] == "aprobado"])
    finally:
        box.cleanup()


def test_reconcile_closes_discarded_pending() -> None:
    print("\nuna propuesta descartada NO queda esperando para siempre")

    box = Sandbox()
    try:
        entry = record_before_write(
            skill_name="demo-skill", skill_path=box.skill_path, action="patch",
            hermes_home=box.home,
        )
        # Un pending que la cola ya NO tiene: se descartó.
        mark_staged(entry, proposed_content=CAMBIADO, pending_id="pid1",
                    hermes_home=box.home)
        check("arranca en staged",
              find_entry(entry.entry_id, hermes_home=box.home).status == STAGED)

        r = reconcile_staged(hermes_home=box.home,
                             pending_lookup=lambda pid, sub: False)
        check("se detecta el descarte", r and r[0][1] == "descartado",
              str(r and r[0][1]))
        estado = find_entry(entry.entry_id, hermes_home=box.home)
        check("la entrada se cierra como failed, no queda en staged",
              estado.status == STATE_FAILED, estado.status)
        check("la razón lo declara", "descartado" in estado.reason, estado.reason)
        check("NO se marca aplicada", estado.status != APPLIED)
        check("no es revertible (nunca se aplicó)", estado.is_revertible is False)

        h = health(hermes_home=box.home)
        check("ya no cuenta como encolada", h["staged_entries"] == 0,
              f"staged={h['staged_entries']}")
    finally:
        box.cleanup()


def test_reconcile_detects_third_party_edit() -> None:
    print("\nun tercero que toca el archivo se declara, no se asume")

    box = Sandbox()
    try:
        entry = record_before_write(
            skill_name="demo-skill", skill_path=box.skill_path, action="patch",
            hermes_home=box.home,
        )
        mark_staged(entry, proposed_content=CAMBIADO, pending_id="p1",
                    hermes_home=box.home)
        box.skill_path.write_text("lo editó otro proceso\n", encoding="utf-8")

        r = reconcile_staged(hermes_home=box.home,
                             pending_lookup=lambda pid, sub: True)
        check("se declara como cambio por otra vía", r and r[0][1] == "cambió por otra vía",
              str(r and r[0][1]))
        check("NO se marca aplicada en falso",
              find_entry(entry.entry_id, hermes_home=box.home).status == STAGED)
    finally:
        box.cleanup()


def test_double_rollback_refused() -> None:
    print("\nno se revierte dos veces")

    box = Sandbox()
    try:
        entry, _ = box.apply_change()
        first = rollback(entry.entry_id, hermes_home=box.home)
        check("el primero funciona", first.success is True)
        second = rollback(entry.entry_id, hermes_home=box.home)
        check("el segundo se niega", second.success is False)
        check("la razón lo dice", "ya fue revertida" in second.message, second.message)
    finally:
        box.cleanup()


def test_rollback_last() -> None:
    print("\nrevertir el último cambio")

    box = Sandbox()
    try:
        check("sin nada aplicado no hay nada que revertir",
              rollback_last(hermes_home=box.home).success is False)

        box.apply_change(CAMBIADO)
        box.apply_change(CAMBIADO + "\n\nOtra mejora.\n")

        result = rollback_last(hermes_home=box.home)
        check("revierte el último", result.success is True, result.message)
        check("deja el contenido del paso anterior",
              box.skill_path.read_text(encoding="utf-8") == CAMBIADO)

        result2 = rollback_last(hermes_home=box.home)
        check("y se puede seguir revirtiendo hacia atrás", result2.success is True)
        check("hasta volver al original",
              box.skill_path.read_text(encoding="utf-8") == ORIGINAL)
    finally:
        box.cleanup()


def test_missing_entry_and_paths() -> None:
    print("\ncasos borde")

    box = Sandbox()
    try:
        result = rollback("inexistente", hermes_home=box.home)
        check("id inexistente → se niega", result.success is False)
        check("la razón lo nombra", "no existe" in result.message, result.message)

        entry, _ = box.apply_change()
        box.skill_path.unlink()
        gone = rollback(entry.entry_id, hermes_home=box.home)
        check("si el skill desapareció, se niega en lugar de recrearlo a medias",
              gone.success is False)
        check("y lo explica", "ya no está" in gone.message, gone.message)
    finally:
        box.cleanup()


def test_health_does_not_create_anything() -> None:
    print("\nel diagnóstico no inventa su propia evidencia")

    box = Sandbox()
    try:
        h = health(hermes_home=box.home)
        check("informa que el journal no existe", h["exists"] is False)
        check("y NO lo creó al consultarlo",
              not Path(h["journal_path"]).exists())
        check("cuenta cero entradas", h["entries"] == 0)
    finally:
        box.cleanup()


def test_journal_survives_corrupt_line() -> None:
    print("\nuna línea ilegible no borra el resto")

    box = Sandbox()
    try:
        entry, _ = box.apply_change()
        path = Path(health(hermes_home=box.home)["journal_path"])
        with path.open("a", encoding="utf-8") as handle:
            handle.write("{esto no es json valido\n")
        entry2, _ = box.apply_change(CAMBIADO + "\nx\n")

        entries = read_entries(hermes_home=box.home)
        check("las entradas válidas sobreviven", len(entries) >= 2, f"{len(entries)}")
        check("las dos entradas legítimas están",
              any(e.entry_id == entry.entry_id for e in entries)
              and any(e.entry_id == entry2.entry_id for e in entries))
    finally:
        box.cleanup()


def main() -> int:
    print("=" * 68)
    print("hermes-skills-helper — etapa 4 (journal y rollback)")
    print("=" * 68)

    for fn in (
        test_full_cycle_restores_bytes,
        test_journal_is_append_only,
        test_refuses_to_clobber_third_party_edit,
        test_refuses_without_backup,
        test_detects_corrupt_backup,
        test_pending_is_visible_not_hidden,
        test_staged_is_not_a_broken_write,
        test_reconcile_detects_approval,
        test_reconcile_closes_discarded_pending,
        test_reconcile_detects_third_party_edit,
        test_double_rollback_refused,
        test_rollback_last,
        test_missing_entry_and_paths,
        test_health_does_not_create_anything,
        test_journal_survives_corrupt_line,
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
