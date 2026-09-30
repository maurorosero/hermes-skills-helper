# hermes-skills-helper

**Recolector de señales** para la mejora de los skills de un arnés de agente.

Responde una pregunta que hoy nadie responde: **¿este skill está aprendiendo, o se usa
igual que hace un año?** Mide el uso, el desgaste y los fallos recurrentes, y entrega los
datos con los que decidir si un skill **se crea, se mejora o se da de baja**.

**No escribe sobre los skills. No llama al modelo. No decide.** Reúne y declara hechos.

Es un **plugin de Hermes Agent** — no un skill, no un gancho de shell.

## El problema que mide

Medido el 29-sep-2026 en un arnés real, con 172 skills y 1.242 parches aplicados:

```
andrea-governance              189 parches   100,3 KB   patch_generation = 1
andrea-docs                     93 usos        0 parches
41 skills parcheados         1.242 parches      0 verificaciones de que mejoraran
```

Tres hechos, y los tres son el mismo:

1. **189 escrituras contadas como una sola generación.** Un cambio malo no se puede aislar
   ni revertir, porque no se sabe cuál fue.
2. **Un skill usado 93 veces que nunca cambió.** Puede ser perfecto. Puede estar fallando
   en silencio. Nadie lo sabe.
3. **Cuarenta y un skills acumularon 1.242 parches y ninguno se verificó.**

Y el registro de esos cambios guarda hashes que **nadie lee**: cero consumidores de los
campos `before`/`after` en todo el código del arnés. Una auditoría que nadie audita.

## Por qué este plugin no escribe

Porque escribir sin verificar es lo que produjo los números de arriba. Y hay una razón de
costo, medida: cada cambio propuesto se encolaba para aprobación, y la cola llegó a
**224 pendientes / 416 operaciones** — el 79% concentrado en cuatro skills, sin registrar
el motivo de ninguna.

Un proceso que genera trabajo de revisión sin criterio no ahorra trabajo: lo multiplica.

## Las cuatro señales

Todo determinista. Sale de contar, fechar y comparar. Nada de modelo.

```
sin_usar      creado y nunca usado en 14+ días.
              El agente lo creyó necesario y no lo fue.

sin_cambio    se usa mucho (5+) y nunca se parcheó.
              Puede ser perfecto, o fallar sin ruido.

sin_reuso     se parcheó y no se volvió a usar en 14+ días.
              El parche pudo romperlo.

hinchado      cuerpo de 24+ KB, o 50+ parches sobre el mismo archivo.
              Creció fuera de la estructura.
```

La cuarta viene de dos fuentes que coinciden. DSec —la infraestructura de sandboxes con la
que DeepSeek entrena sus agentes, [arXiv:2609.22978](https://arxiv.org/abs/2609.22978)
§5.1— mide que una imagen monolítica obliga a reconstruir todo para cambiar una parte:
`O(m·N)` contra `O(m)` en capas. Y el propio curador del arnés lo dice en su prompt: *"A
SKILL.md body over ~24k chars is a consolidation target on its own [...] push topic depth
into references/"*.

**DSec mide contenedores, no skills.** La transferencia es la estructura de capas, no un
resultado probado para skills, y así se declara.

## El flujo completo

El plugin es la primera pieza y **sólo la primera**.

```
recolector (este plugin)    mide y recomienda          cero escritura, cero modelo
   ↓
cron                        cruza con el historial git  determinista
   ↓
issue en el repo            la propuesta, con motivo y evidencia
   ↓
revisión                    humano autorizador, en sandbox
   ↓
promoción                   repo → producción            explícita, reversible
```

Lo demás vive fuera: en el repo, en git y en quien revisa. El repo es la memoria del ciclo
(`git log` dice qué cambió, `git diff` qué se tocó exacto, `git revert` vuelve atrás), y
esa memoria vuelve al recolector como señal.

**Nadie escribe nunca sobre lo que está funcionando.** El daño de los 189 parches pasó
porque se escribía directo en producción. Este flujo lo hace imposible por construcción,
no por disciplina.

## Qué registra

```
on_session_end    el barrido determinista, con throttle de 900 s
skills_signals    qué skills tienen señal, y cuál. Sólo lee.
skills_report     el informe completo + un cuerpo Markdown para abrir un issue
```

Ninguno escribe. Ninguno propone un cambio de texto. Ninguno llama al modelo.

El hook corre **por turno**, no por sesión — está documentado en
`agent/turn_finalizer.py` (*"run_conversation() runs once per message"*). Por eso barre con
un intervalo mínimo: el barrido completo tarda ~0,95 s medidos sobre el arnés real (0,69 la
señal de uso, 0,30 la recurrencia), que no es caro pero tampoco
es gratis a cada mensaje.

## Estado

```
uso.py            la señal de uso                 54 tests
recolector.py     el barrido, throttle y estado   25 tests
recurrence.py     recurrencia de fallos           76 tests
trajectory.py     lectura de la trayectoria
effect/           fingerprint + verificación      54 tests
tests/test_informe.py  el markdown y el JSON      37 tests

246 tests. validate 13/13. El barrido corre contra el arnés real (11/11 en el cableado).
```

Las cuatro señales, medidas sobre el arnés real el 29-sep-2026:

```
32 sin usar · 19 sin cambio · 9 sin reuso · 9 hinchados · 108 activos
confiable: True · sin registro: 0

andrea-docs                          93 usos    0 parches    4,5 KB
andrea-gatekeeper                    62 usos    0 parches    5,4 KB
andrea-governance                   305 usos  189 parches  100,3 KB
nextcloud-contacts                   47 usos   30 parches   13,8 KB
```

Y los 28 de esos 32 nunca usados nacieron en dos tandas — 17 el 28-jul y 11 el 22-ago—, que
es un dato sobre el criterio de creación, no sobre 32 skills individuales.

## Lo que este plugin reemplaza

```
curador del arnés   mantiene el catálogo y archiva por reloj — 62 skills en una sola
                    corrida, y 4 volvieron esa misma semana. No mide si algo mejoró:
                    su prompt dice "judge overlap on CONTENT, not on use_count".

Refine Cycle        busca errores repetidos entre sesiones. Mide el efecto de un cambio
                    como "el archivo creció" — estado y bytes, no si el problema paró.
```

Los dos escriben y ninguno cierra el ciclo. **Este recolector no escribe, y por eso puede
medir.**

Y hay un hueco que el core deja por diseño: `background_review` descarta los errores
transitorios de cada sesión (*"Session-specific transient errors that resolved before the
conversation ended"*), así que **el arnés no compara errores entre sesiones**. El
recolector sí — esa es la parte de recurrencia.

## Punto de partida y créditos

El diseño —las cinco etapas, su criterio de verificación y los límites duros— proviene de
**Refine Cycle for Hermes Agent**, de **Taras Boiko** (`Bergschloss`), publicado bajo
licencia MIT.

**Este repositorio no es un fork, ni un derivado, ni su sucesor.** Es un proyecto
independiente: no hay relación de git con el original, no hay upstream que seguir, no hay
merge pendiente, y no se copia código de él. Se reconoce el **diseño** como punto de
partida; el código es propio y se escribe aquí.

De ahí se siguen tres cosas concretas:

- **Su proyecto sigue siendo suyo.** Vive en su repositorio y lo mantiene él.
- **Nuestro trabajo va contra nuestro repositorio** — `maurorosero/hermes-skills-helper`.
- **No se copia código de él.** La licencia MIT no impone obligaciones sobre una
  reimplementación independiente; el crédito es una decisión de atribución, no una
  exigencia legal.

Si este trabajo resulta útil, el mérito del enfoque es de él.

## Qué queda de las cinco etapas

Las cuatro etapas que se fueron existían para **escribir sobre los skills**. Sin escritura
no hay nada que proteger con un techo, nada que registrar con un journal, y la etapa de
efecto —que sólo calificaba los cambios que el propio plugin aplicaba— no tenía objeto.

```
se van   2.530 líneas    budget 549 · journal 672 · pipeline 726 · proposal 583
         ~2.000 en tests  los cuatro archivos que las probaban
llegan     770 líneas    uso 392 · recolector 300 · __init__ +78
quedan   2.630 de 4.390  (60% del original)
```

El neto es −1.760 líneas: se va más de lo que llega, porque lo que llega **mide en vez de
escribir** y medir ocupa menos que escribir.

Lo que queda es la parte que ninguno de los dos sistemas existentes tiene: medir el uso
real y decirlo.

```
señal de uso       uso.py                      ← nuevo
el barrido         recolector.py                ← nuevo: throttle + estado atómico
recurrencia        recurrence.py + trajectory.py
verificación       effect/                     ← fingerprint + tres chequeos
```

Del recorte se rescatan dos piezas ya probadas, y sólo dos: el **throttle** (`should_scan`),
con su razón en ambos sentidos —"no se barrió" tiene que poder distinguirse de "se barrió y
no había nada"— y el **estado atómico** (`read_state`/`write_state`), escritura a temporal
más `os.replace`.

El lock de `budget.py` **no** se rescata: protegía la escritura concurrente del libro de
presupuesto. Sin ese libro no hay nada que proteger.

El código previo queda en la rama `pre-recorte-0.3.0`.

## Uso

```bash
python3 tests/test_uso.py                  # 54 tests de la señal de uso
python3 tests/test_recurrence.py           # 76 tests del filtro de recurrencia
python3 tests/test_effect.py               # 54 tests de la verificación
python3 tests/medicion_uso.py              # las cuatro señales, sobre el arnés real
python3 tests/medicion_recurrencia.py      # recurrencia sobre 69.000+ mensajes reales
python3 tests/medicion_cableado.py         # el plugin cargado por el arnés (11/11)

hermes plugins validate .
```

Los `medicion_*.py` no son tests: se corren a mano y se lee el resultado. Miden contra los
datos que existen en disco, no contra fixtures.

Sin dependencias externas: Python estándar y la biblioteca del arnés cuando está presente.

## Instalación

```bash
hermes plugins install maurorosero/hermes-skills-helper
hermes plugins enable hermes-skills-helper
```

Es **opt-in**: sin `--enable`, el plugin no se activa. El `enable` recarga los hooks en el
gateway en caliente —los tools quedan para la sesión siguiente—, así que **no hace falta
reiniciar nada**.

### Dónde vive el estado

En `<home>/plugin-data/hermes-skills-helper/recolector-state.json`, **nunca** dentro del
árbol del plugin. El host lo pide explícitamente en `plugins/plugin_storage.py`:

> *"Plugins must NOT park state in `<hermes home>/plugins/<name>/` (the install dir, deleted
> by `remove` and git-pulled by `update`)."*

Y no es una preferencia de estilo: `plugins update` reemplaza el árbol completo con
`os.replace`, así que el estado que viva adentro desaparece en la primera actualización. Un
`install` con el directorio ya ocupado, además, falla con *"already exists"*.

## Alcance

El dominio es **skills**, y sólo skills.

- **No escribe en ningún skill.** Tampoco en `MEMORY.md`, `USER.md` ni la memoria
  holográfica: son proyectos aparte.
- **No crea issues.** Redacta el cuerpo; abrirlo es del paso siguiente del flujo.
- **No propone cambios de texto.** Entrega datos; la propuesta es de quien revisa.
- **No da de baja nada.** El curador del arnés archiva; esto sólo mide.

**Restricción de diseño:** el núcleo de Hermes **no se toca**. Todo usa APIs que el arnés
ya expone: `on_session_end`, `ctx.register_tool` y la lectura del `.usage.json` que el
propio arnés mantiene.

## Licencia

MIT © 2026 Mauro Rosero. Ver [`LICENSE`](LICENSE).
