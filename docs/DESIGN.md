# Diseño — hermes-skills-helper

Recolector de señales para la mejora de los skills de un arnés de agente.

**Mide y declara. No escribe, no llama al modelo, no decide.** Todo lo que sigue se ordena
alrededor de esa restricción.

---

## Restricción que ordena todo el diseño

El núcleo de Hermes **no se toca**. Un parche al núcleo tiene tres costos concretos, y los
tres están documentados en el proyecto de origen:

1. `hermes update` reemplaza esos archivos y el parche desaparece sin aviso.
2. Hay que rehacerlo en cada versión — el original rebaseó siete veces en seis semanas.
3. Un rebase mal verificado rompe en silencio.

Todo lo que hay acá usa APIs que el arnés **ya expone**.

## APIs del arnés utilizadas

```
on_session_end              el hook de barrido (agent/turn_finalizer.py)
ctx.register_tool           los dos tools de consulta
tools/skill_usage.py        lectura del .usage.json: use_count, patch_count,
                            patch_generation, last_reused_patch_generation,
                            last_used_at, last_patched_at, created_at, archived_at
state.db                    la trayectoria: role='tool', mensajes y fallos
```

**Lectura solamente.** Ninguna de estas APIs se llama para escribir.

---

## La señal de uso — `uso.py`

Es la pieza nueva, y la que justifica el plugin. Sin ella, el catálogo entero es opaco: se
sabe cuántos skills hay, no si alguno funciona.

### Las cuatro señales

```
sin_usar      use_count == 0 y edad >= 14 d
sin_cambio    use_count >= 5 y patch_count == 0
sin_reuso     patch_count > 0, patch_generation > last_reused_patch_generation
              y días desde last_patched_at >= 14
hinchado      bytes del SKILL.md >= 24 KB  o  patch_count >= 50
```

### El orden de evaluación es el de urgencia

Un skill que nunca se usó **y** tiene 189 parches se reporta como `sin_usar`, no como
`hinchado`. Lo primero es más accionable: un skill que nadie usa no necesita análisis, el
veredicto es inmediato.

### `None` no es `0`

Esta distinción es la razón de existir del módulo, no un detalle de implementación:

```
use_count = None   no hay registro: no se puede afirmar NADA
use_count = 0      medición real de cero usos
```

Rellenar el primero con el segundo convierte "no lo medí" en "no se usa", que es
exactamente la conclusión que este módulo existe para no inventar. Un registro con
`use_count` ilegible devuelve estado `desconocido`, no `sano`.

### `reuse_after_patch` — la señal que ya existía

`tools/skill_usage.py` mantiene `last_reused_patch_generation` desde siempre, y emite
`reuse_after_patch` por el hook `on_skill_lifecycle`. Verificado: **cero menciones en el
log del arnés**. Nadie lo escucha.

Su lógica, en el propio código del arnés:

```python
reuse_after_patch = uses > 0 and gen > last_reused
```

Es decir: **¿se volvió a usar este skill después del último parche?** Es la respuesta a
"¿sirvió el cambio?", disponible sin llamar al modelo y sin que el plugin haya escrito
nada. El recolector la lee.

### Por qué el tamaño se mide del archivo, no del registro

`.usage.json` no guarda bytes. El tamaño sale de un `stat()` sobre el `SKILL.md` real. El
orden de los directorios importa: el arnés resuelve **local primero**
(`agent/skill_utils.py:420`, docstring *"local ... first"*), así que se busca en el mismo
orden y el tamaño medido es el del archivo que realmente se carga.

### Las fuentes del `SKILL.md` — y los fantasmas

El registro es un mapa **nombre → registro**: dice qué skills existen, no dónde vive cada
uno. La ruta la resuelve `_skills_dirs()`. Una entrada cuyo `SKILL.md` no aparece en ninguna
de esas fuentes es un **fantasma**: una señal sin archivo que la respalde.

La lista completa, en orden:

```
external_dirs configurados   <home>/skills     ← éstos ya estaban
<raíz>/profiles/*/skills     core/optional-skills   ← y éstos faltaban
```

Faltaban los dos últimos, y el fallo era silencioso: skills que **existen en disco** —y que
el arnés carga— se medían como inexistentes. En el arnés de Mauro eran dos reales, ambos en
`optional-skills` (`audiocraft-audio-generation`, `segment-anything-model`), que además
tenían `use_count = 0`: el reporte los habría dado de baja por "nunca usados" cuando el
archivo estaba ahí.

Las dos fuentes nuevas van **después** del default a propósito. Sólo aportan lo que ningún
directorio previo resolvía, así que ningún tamaño ya medido cambia de dueño — el arreglo
suma sin correr nada de lugar. Y la raíz se pide a `get_default_hermes_root()`: con
`HERMES_HOME=<raíz>/profiles/<nombre>`, esa función devuelve `<raíz>`, que es lo que hace
falta para ver a los **demás** perfiles.

Ojo con la conclusión que esto habilita: que una entrada no aparezca en ninguna fuente no
prueba por sí sola que el skill no exista — prueba que no está donde el plugin mira. Los
tres fantasmas que quedaban en el arnés de Mauro
(`andrea-gatekeeper`, `email-mailbox-cleanup`, `knowledge-management`) sí eran entradas
muertas, pero eso se confirmó aparte: el `LEEME` del wiki documenta los dos últimos como
retirados y con su contenido rescatado. Lo mismo vale para los archivados — entran en
`.archive/`, que no es una fuente, y se reportan como `ruta = None` sin que eso sea un
defecto.

### El umbral de hinchado, y de dónde sale

De dos fuentes que coinciden, y ninguna de las dos mide skills:

- **DSec** ([arXiv:2609.22978](https://arxiv.org/abs/2609.22978) §5.1) mide que una imagen
  monolítica obliga a reconstruir todo para cambiar una parte: `O(m·N)` contra `O(m)` en
  capas. Mide contenedores.
- **El curador del arnés** lo dice en su propio prompt: *"A SKILL.md body over ~24k chars is
  a consolidation target on its own [...] push topic depth into references/"*.

La transferencia es la **estructura**, no un resultado probado para skills. Se declara así.

---

## La recurrencia de fallos — `recurrence.py` + `trajectory.py`

Se mantiene del diseño original. Responde: **¿este fallo se repite, o pasó una sola vez?**

Cuatro cubos, y la separación es el aporte del módulo:

```
recurring    fallos del agente que se repiten en el TIEMPO. Candidatos.
bursts       concentrados en horas. Episodios, no patrones.
guardrails   rechazos del propio arnés. No son errores del agente.
stale        patrones que ya dejaron de ocurrir. Un fallo caduco no se arregla
             con un cambio de hoy.
```

Un fallo es recurrente con **≥3 apariciones** o en **≥2 sesiones**. El segundo criterio
existe porque el primero se satisface dentro de una sola sesión: un bucle de reintentos
produce diez apariciones en dos minutos y no por eso es un patrón.

**Esto cubre un hueco que el core deja por diseño.** El `background_review` del arnés
descarta los errores transitorios de cada sesión (*"Session-specific transient errors that
resolved before the conversation ended"*), así que **no compara errores entre sesiones**.
El recolector sí.

### `trustworthy`

Un barrido donde se descartó más de la mitad de las filas por fecha increíble no sostiene la
conclusión negativa. `trustworthy == False` obliga a quien llama a declarar la limitación en
lugar de reportar silencio como respuesta.

---

## La verificación — `effect/`

Se mantiene del diseño original: fingerprint de errores y tres chequeos deterministas
(sha256 del archivo, conteo de usos, huella del error).

### La normalización de huellas

Un defecto real, corregido: la normalización tapaba la ruta entera, así que cinco fallos
distintos compartían huella.

```
File not found: /home/andrea/wiki/SCHEMA.md       →  9a99e73e28c7
File not found: /home/andrea/.ssh/config          →  9a99e73e28c7   ← la misma
File not found: /home/andrea/wiki/log.md          →  9a99e73e28c7   ← la misma
```

`File not found: <path>` colapsaba 18 fallos distintos en 1, y mezclaba `exit_code=1` con
`exit_code=124`. Eso era el **16% de toda la trayectoria**.

El daño no es cosmético: un grupo Frankenstein mantiene "vivo" a otro y el cambio no le
corresponde a ninguno. La corrección distingue rutas.

---

## El barrido — `recolector.py`

Orquesta las dos lecturas y anota el estado.

### El throttle

El hook `on_session_end` corre **por turno**, no por sesión. Verificado en
`agent/turn_finalizer.py`: *"run_conversation() runs once per message"*. Sin throttle
barrería a cada mensaje.

```
MIN_SCAN_INTERVAL_SECONDS = 900
```

El barrido tarda ~0,95 s medidos sobre el arnés real —0,69 la señal de uso, 0,30 la
recurrencia—. No es caro, pero tampoco es gratis a cada mensaje. Con el throttle, el coste
cae a **0,000 s** en los turnos salteados, que es la mayoría.

`should_scan` devuelve **la razón en ambos casos**, para que un barrido salteado sea
auditable: "no se barrió" tiene que poder distinguirse de "se barrió y no había nada".

Si el reloj retrocede, se barre en lugar de bloquear. Un salto de NTP no debe dejar el
plugin mudo por 15 minutos.

### El estado es atómico

Escritura a temporal más `os.replace`: o está el estado anterior entero, o está el nuevo
entero. Sin esto, un proceso interrumpido dejaría un JSON a medias y el throttle podría
leerlo y barrer de más.

El estado se anota **después** de barrer, y sólo si se barrió. Marcarlo antes dejaría al
throttle convencido de un barrido que no ocurrió.

Rescatado del recorte junto con el throttle. Son las dos únicas piezas que sobreviven
porque las dos protegen **lectura y estado propio**, no escritura sobre skills.

### Cada fuente, envuelta por separado

Una que falla no impide que la otra corra. Un barrido abortado entero porque la trayectoria
no se pudo leer dejaría también sin medir el uso de los skills, que es la señal principal.

Y las dos banderas de legibilidad (`usage_readable`, `trajectory_readable`) convierten un
informe vacío en una limitación declarada.

---

## El flujo completo

El plugin es la primera pieza y **sólo la primera**.

```
recolector (esta pieza)     mide y recomienda          cero escritura, cero modelo
   ↓
cron                        cruza con el historial git  determinista
   ↓
issue en el repo            la propuesta, con motivo y evidencia
   ↓
revisión                    humano autorizador, en sandbox
   ↓
promoción                   repo → ~/.hermes/skills     explícita, reversible
```

### Por qué el repo es la memoria del ciclo

```
git log      qué cambió y cuándo
git diff     qué se tocó exacto     (el ledger del arnés guarda hashes que nadie lee)
git revert   volver atrás           (el curador reescribe todo el árbol)
blame        qué parche rompió algo
```

Caso real, del historial del repo de skills:

```
b9af656  harness(infisical): restaura 3 bloques perdidos al separar los skills
```

Un agente reescribió un skill y se llevó tres bloques que no le correspondían. Se
recuperaron porque el contenido previo estaba en git. Sin el repo, no existían más.

**La promoción es explícita.** La rama de producción es `~/.hermes/skills`, y el arnés
resuelve local primero: lo que corre es lo revisado, y el repo es la mesa de trabajo. Nadie
escribe sobre lo que está funcionando.

---

## Alcance: skills, y nada más

- **No escribe en ningún skill.** No toca `MEMORY.md`, `USER.md` ni la memoria holográfica:
  son proyectos aparte.
- **No crea issues.** Redacta el cuerpo; abrirlo es del paso siguiente.
- **No propone cambios de texto.** Entrega datos; la propuesta es de quien revisa.
- **No da de baja nada.** El curador del arnés archiva; esto sólo mide.

## Lo que este plugin NO hace, y por qué

### No escribe sobre los skills

Escribir sin verificar es lo que degradó el catálogo. Medido el 29-sep-2026:

```
andrea-governance     189 parches   100,3 KB    patch_generation = 1
41 skills parcheados  1.242 parches  0 verificaciones de que mejoraran
```

189 escrituras contadas como **una sola generación**. Un cambio malo no se puede aislar ni
revertir, porque no se sabe cuál fue.

Y el costo de la escritura, medido: cada cambio propuesto se encolaba para aprobación. La
cola llegó a **224 pendientes / 416 operaciones**, el 79% concentrado en cuatro skills, sin
registrar el motivo de ninguna.

### No llama al modelo

Nada de lo que hace necesita uno. Todo sale de contar, fechar y comparar. Poner un modelo
donde alcanza una comparación cambia una verificación reproducible por una opinión — y el
mismo `.usage.json` daría veredictos distintos en dos corridas.

### Las etapas que se fueron

```
ETAPA 2  TECHO         ≤3 cambios/día · ≤30 llamadas/día
ETAPA 3  PROPUESTA     la única con modelo
ETAPA 4  JOURNAL       registro + rollback por hash
         PIPELINE      orquestaba las cinco etapas
```

Las cuatro existían para **escribir sobre los skills**. Sin escritura no hay nada que
proteger con un techo, nada que registrar con un journal, y la etapa de efecto —que sólo
calificaba los cambios que el propio plugin aplicaba— no tenía objeto.

```
se van    2.530 líneas     budget 549 · journal 672 · pipeline 726 · proposal 583
          ~2.000 en tests  los cuatro archivos que las probaban
llegan      770 líneas     uso 392 · recolector 300 · __init__ +78
quedan    2.630 de 4.390   (60% del original) · neto −1.760
```

Lo que queda es la parte que ninguno de los dos sistemas existentes tiene: medir el uso
real y decirlo.

```
señal de uso     uso.py                        ← nuevo
el barrido       recolector.py                 ← nuevo: throttle + estado atómico
recurrencia      recurrence.py + trajectory.py
verificación     effect/                       ← fingerprint + tres chequeos
```

Del recorte se rescatan **dos piezas, y sólo dos**, porque ambas protegen *lectura y estado
propio* y no escritura sobre skills:

```
should_scan          el throttle, con su razón en ambos sentidos
read_state/write_state   el estado atómico: temporal + os.replace
```

El lock de `budget.py` **no** se rescata: protegía la escritura concurrente del libro de
presupuesto, y sin ese libro no hay nada que proteger.

El código previo queda en la rama `pre-recorte-0.3.0`.

---

## Orden de implementación

Ya ejecutado, en este orden:

1. **`uso.py`** — la señal de uso, con sus cuatro cubos y sus 54 tests.
2. **`recolector.py`** — el barrido, el throttle y el estado atómico.
3. **Cableado** — el hook y los dos tools, verificados contra el arnés real.
4. **El recorte** — se fueron las cuatro etapas que escribían.

Pendiente, y fuera de este plugin:

5. **El cron** que cruza las señales con `git log` y abre el issue.
6. **La promoción explícita** de repo a producción.

## Créditos

El diseño de las etapas de recurrencia y verificación proviene de **Refine Cycle for Hermes
Agent**, de **Taras Boiko** (`Bergschloss`), MIT.

La estructura en capas para el umbral de hinchado se inspira en **DSec**
([arXiv:2609.22978](https://arxiv.org/abs/2609.22978)), que mide contenedores, no skills.
La transferencia es estructural y así se declara.

Este repositorio no es un fork ni un derivado. No se copia código de ninguno de los dos.
