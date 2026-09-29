"""Medición real: la etapa 1 contra la trayectoria del arnés.

Criterio de cierre del diseño para la etapa 1:

    "reproducir la detección sobre trayectoria real y comprobar que el umbral descarta
     el ruido de un solo evento"

Esto lo ejecuta sobre ``state.db`` — 69.000+ mensajes reales, no fixtures. Se corre a mano
y se lee el resultado.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from recurrence import DEFAULT_WINDOW_DAYS, MIN_OCCURRENCES, MIN_SESSIONS, detect
from trajectory import scan_failures

STATE_DB = Path("/home/andrea/.hermes/state.db")
now = time.time()

print("=" * 74)
print("ETAPA 1 — RECURRENCIA SOBRE LA TRAYECTORIA REAL")
print("=" * 74)
print("ventana: %.0f días · umbrales: >=%d apariciones o >=%d sesiones"
      % (DEFAULT_WINDOW_DAYS, MIN_OCCURRENCES, MIN_SESSIONS))
print()

# -- Cuántos fallos hay, en total y en la ventana --------------------------------
print("--- VOLUMEN DE LA SEÑAL")
for dias in (7, 30, 90):
    scan = scan_failures(state_db=STATE_DB, since_ts=now - dias * 86400, now=now)
    print("  últimos %3d días: %5d fallos   (filas leídas: %d, descartadas por fecha: %d)"
          % (dias, len(scan.failures), scan.rows_scanned, scan.discarded_ts))

print()
print("--- EL FILTRO, EN LA VENTANA POR DEFECTO")
report = detect(state_db=STATE_DB, now=now)
print("  fallos en la ventana          : %d" % report.total_failures)
print("  RECURRENTES (candidatos)      : %d" % len(report.recurring))
print("  ráfagas (episodio, no patrón) : %d" % len(report.bursts))
print("  rechazos del arnés            : %d" % len(report.guardrails))
print("  descartados como evento único : %d" % report.single_events)
print("  sesiones distintas vistas     : %d" % report.sessions_seen)
print("  barrido confiable             : %s" % report.trustworthy)
print()
print("  reparto: %s" % report.summary())

if report.recurring:
    print()
    print("--- RECURRENTES SOSTENIDOS (esto es lo que justifica un cambio permanente)")
    for i, r in enumerate(report.recurring[:8], 1):
        print()
        print("  %d. [%s]  %s" % (i, r.tool_name, r.why()))
        print("     huella: %s" % r.fingerprint)
        print("     muestra: %s" % r.sample[:100].replace("\n", " "))
else:
    print()
    print("--- SIN RECURRENTES SOSTENIDOS")
    print("  Ninguna forma de fallo superó el umbral dentro de la ventana.")

if report.bursts:
    print()
    print("--- RÁFAGAS (se reportan, NO justifican un cambio permanente)")
    for b in report.bursts[:5]:
        print("   [%-13s] x%-3d %5.2f días en %d sesiones  %s"
              % (b.tool_name[:13], b.occurrences, b.span_days, b.sessions,
                 b.sample[:52].replace("\n", " ")))

if report.guardrails:
    print()
    print("--- RECHAZOS DEL ARNÉS (el guardarraíl funcionando, no un error del agente)")
    for g in report.guardrails[:5]:
        print("   [%-13s] x%-3d %s"
              % (g.tool_name[:13], g.occurrences, g.sample[:60].replace("\n", " ")))

print()
print("=" * 74)
print("LECTURA")
print("=" * 74)
if not report.trustworthy:
    print("  La ventana NO se pudo leer bien: la conclusión negativa no es sostenible.")
elif report.recurring:
    print("  %d patrones sostenidos superaron el filtro. Son los candidatos a la etapa 3."
          % len(report.recurring))
    print("  Quedaron fuera %d ráfagas (episodios), %d rechazos del arnés y %d eventos"
          % (len(report.bursts), len(report.guardrails), report.single_events))
    print("  únicos. Ninguno de esos tres justifica un cambio permanente de conducta.")
else:
    print("  Cero recurrentes sostenidos. Es un resultado válido: significa que no hay")
    print("  nada que proponer, y es exactamente lo que el filtro debe poder decir.")
