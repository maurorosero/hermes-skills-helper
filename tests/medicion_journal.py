"""Medición real: el ciclo completo de la etapa 4 sobre un skill de verdad.

Criterio de cierre del diseño:

    "sin rollback verificado no hay aplicación de cambios"

Este script lo comprueba sobre un skill real del hub, en una **copia** — nunca sobre el
original. El orden es el de la vida real: respaldar, cambiar, revertir, y confirmar con
hash que el archivo volvió a su estado previo. Si algo acá falla, el plugin no está
autorizado a aplicar cambios.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from effect.checker import sha256_text  # noqa: E402
from journal import (  # noqa: E402
    APPLIED,
    ROLLED_BACK,
    find_entry,
    health,
    iter_backups,
    read_entries,
    record_after_write,
    record_before_write,
    rollback,
)

SKILLS_HUB = Path("/home/andrea/developers/ai/skills")

print("=" * 74)
print("MEDICIÓN REAL — etapa 4 sobre un skill del hub")
print("=" * 74)

# --- Elegir un skill real que exista ------------------------------------------------
candidatos = sorted(SKILLS_HUB.glob("*/*/SKILL.md")) or sorted(SKILLS_HUB.glob("*/SKILL.md"))
if not candidatos:
    print("  No hay skills en %s: no se puede medir contra algo real." % SKILLS_HUB)
    raise SystemExit(1)

origen = candidatos[0]
print()
print("  skill elegido : %s" % origen.relative_to(SKILLS_HUB))
print("  tamaño real   : %d bytes" % origen.stat().st_size)

# --- Trabajar sobre una COPIA -------------------------------------------------------
root = Path(tempfile.mkdtemp(prefix="hsh-medicion-"))
try:
    home = root / "hermes"
    copia_dir = root / "skills" / origen.parent.name
    copia_dir.mkdir(parents=True, exist_ok=True)
    copia = copia_dir / "SKILL.md"
    shutil.copy2(origen, copia)

    original = copia.read_text(encoding="utf-8")
    hash_original = sha256_text(original)
    print("  hash original : %s" % hash_original[:32])

    # --- 1. Respadar y aplicar un cambio --------------------------------------------
    print()
    print("--- 1. respaldar y aplicar un cambio")
    entry = record_before_write(
        skill_name=origen.parent.name,
        skill_path=copia,
        action="patch",
        hermes_home=home,
        reason="medición real: verificar que el rollback restaura byte a byte",
    )
    print("  entrada       : %s (estado %s)" % (entry.entry_id, entry.status))
    print("  respaldo      : %s" % entry.backup_file)
    respaldo = next(iter(iter_backups(hermes_home=home)))
    print("  respaldo en disco: %d bytes" % respaldo.stat().st_size)

    nuevo = original.rstrip() + "\n\n## Sección agregada por la medición\n\nTexto.\n"
    copia.write_text(nuevo, encoding="utf-8")
    record_after_write(entry, new_content=nuevo, hermes_home=home)
    print("  cambio aplicado: %d → %d bytes" % (len(original), len(nuevo)))

    # --- 2. Verificar el estado ANTES de revertir ------------------------------------
    print()
    print("--- 2. estado antes de revertir")
    h = health(hermes_home=home)
    print("  líneas en el journal   : %d  (append-only: pending + applied)" % h["entries"])
    print("  cambios registrados    : %d" % h["changes"])
    print("  revertibles            : %d" % h["revertible_entries"])
    print("  pendientes (incompletas): %d" % h["pending_entries"])
    print("  el skill difiere del original: %s"
          % (sha256_text(copia.read_text(encoding="utf-8")) != hash_original))

    # --- 3. Revertir ------------------------------------------------------------------
    print()
    print("--- 3. revertir")
    resultado = rollback(entry.entry_id, hermes_home=home)
    print("  éxito         : %s" % resultado.success)
    print("  mensaje       : %s" % resultado.message)
    print("  hash restaurado: %s" % (resultado.restored_hash or "")[:32])

    # --- 4. VERIFICAR, que es el punto de todo esto -----------------------------------
    print()
    print("=" * 74)
    print("VERIFICACIÓN")
    print("=" * 74)

    en_disco = copia.read_text(encoding="utf-8")
    hash_final = sha256_text(en_disco)

    ok_bytes = en_disco == original
    ok_hash = hash_final == hash_original
    ok_nuevo = "Sección agregada" not in en_disco
    ok_estado = find_entry(entry.entry_id, hermes_home=home).status == ROLLED_BACK
    ok_append = len(read_entries(hermes_home=home)) > 2

    print("  byte a byte idéntico al original : %s" % ("SÍ" if ok_bytes else "NO"))
    print("  hash coincide                    : %s" % ("SÍ" if ok_hash else "NO"))
    print("  el cambio ya no está en el archivo: %s" % ("SÍ" if ok_nuevo else "NO"))
    print("  la entrada quedó como revertida  : %s" % ("SÍ" if ok_estado else "NO"))
    print("  el journal conservó la evidencia : %s" % ("SÍ" if ok_append else "NO"))
    print("  el original del hub no se tocó   : %s"
          % ("SÍ" if sha256_text(origen.read_text(encoding="utf-8")) == hash_original
             else "NO"))

    print()
    if all([ok_bytes, ok_hash, ok_nuevo, ok_estado, ok_append]):
        print("  ROLLBACK VERIFICADO. El plugin está autorizado a aplicar cambios.")
    else:
        print("  ROLLBACK NO VERIFICADO. El plugin NO debe aplicar cambios.")
        raise SystemExit(1)
finally:
    shutil.rmtree(root, ignore_errors=True)
