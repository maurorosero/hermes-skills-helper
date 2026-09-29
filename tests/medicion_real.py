"""Medición real: la etapa 5 contra el registro del arnés, no contra fixtures.

No es un test: es la verificación de que el módulo funciona sobre los datos que existen
en disco. Se corre a mano y se lee el resultado.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from effect.usage import count_uses_from_record

HOME = Path("/home/andrea/.hermes")
data = json.loads((HOME / "skills" / ".usage.json").read_text(encoding="utf-8"))

con_uso = {k: v for k, v in data.items()
           if isinstance(v, dict) and (v.get("use_count") or 0) > 0}
con_cero = {k: v for k, v in data.items()
            if isinstance(v, dict) and (v.get("use_count") or 0) == 0}

print("=== REGISTRO REAL DEL ARNES ===")
print("total de skills con registro : %d" % len(data))
print("con use_count > 0            : %d" % len(con_uso))
print("con use_count = 0            : %d" % len(con_cero))

ordenados = sorted(con_uso.items(), key=lambda kv: kv[1]["last_used_at"], reverse=True)
print("\nlos 5 de uso mas reciente:")
for name, rec in ordenados[:5]:
    print("  %-42s use_count=%-4d %s" % (name[:42], rec["use_count"], rec["last_used_at"][:19]))

print("\n=== LA DISTINCION QUE DECIDE EL VEREDICTO ===")
r = count_uses_from_record("skill-que-no-existe-jamas-xyz", since_ts=0.0, hermes_home=HOME)
print("skill SIN entrada en el registro  -> count=%s scope=%s" % (r.count, r.scope))
print("   (correcto: no medible, NO cero)")

if ordenados:
    name, rec = ordenados[0]
    r1 = count_uses_from_record(name, since_ts=0.0, hermes_home=HOME)
    print("\n%-34s desde t=0    -> count=%s scope=%s" % (name[:34], r1.count, r1.scope))
    print("   (su ultimo uso es posterior a t=0: hay un piso, no un total inventado)")

    r2 = count_uses_from_record(name, since_ts=time.time(), hermes_home=HOME)
    print("\n%-34s desde AHORA  -> count=%s scope=%s" % (name[:34], r2.count, r2.scope))
    print("   (no hubo uso despues de este instante: cero EXACTO, no una suposicion)")

if con_cero:
    name = next(iter(con_cero))
    r3 = count_uses_from_record(name, since_ts=time.time(), hermes_home=HOME)
    print("\n%-34s (use_count=0) -> count=%s scope=%s" % (name[:34], r3.count, r3.scope))
