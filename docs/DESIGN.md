# Diseño — hermes-skills-helper

Documento de diseño. La **etapa 5 ya está implementada y probada**; las demás se
implementan en el orden fijado abajo, cada una con sus tests antes de pasar a la
siguiente.

## Restricción que ordena todo el diseño

> El núcleo de Hermes no se toca.

Todo lo que sigue usa **únicamente** APIs que el arnés ya expone. Si una etapa necesita
algo que el host no tiene, la etapa se rediseña — no se parchea el motor.

## APIs del arnés utilizadas

Verificadas contra el host en uso (Hermes 0.21.4):

| Necesidad | API | Estado |
|:---|:---|:---|
| Punto de entrada al cerrar sesión | hook `on_session_end` | existe en `VALID_HOOKS` |
| Llamada al modelo activo del usuario | `ctx.llm` → `agent.plugin_llm.PluginLlm` | existe |
| Salida estructurada del modelo | `ctx.llm.complete_structured(...)` | existe |
| Leer trayectoria pasada | SQLite del arnés, abierto `mode=ro` | existe |

`PluginLlm` se documenta en el propio host como *"completions on the user's active
model/auth"*. Es exactamente lo que la etapa 3 necesita, y **no requiere el parche**.

## Etapa 1 — Recurrencia (determinista)

**Pregunta:** ¿este fallo se repite, o pasó una sola vez?

Un error que ocurrió una vez no justifica un cambio permanente en las instrucciones del
agente. Solo se propone un cambio sobre fallos **recurrentes**.

```
leer errores de la trayectoria (state.db, mode=ro)
    ↓
normalizar → fingerprint (no el texto crudo: el mismo fallo se escribe distinto)
    ↓
¿aparece ≥ N veces, o en ≥ 2 sesiones distintas?
    ├── no  → STOP (sin propuesta, sin costo de modelo)
    └── sí  → evidencia para la etapa 3
```

**Por qué primero:** es el filtro más barato y el que evita el problema que motivó este
proyecto — un revisor que propone cambios sin control. El filtro corre antes de gastar
una sola llamada al modelo.

**Criterio de cierre:** reproducir la detección sobre trayectoria real y comprobar que
el umbral descarta el ruido de un solo evento.

### Estado de la implementación (etapa 1)

Implementada en `recurrence.py` + `trajectory.py`, con `tests/test_recurrence.py`
(64 tests) y `tests/medicion_recurrencia.py` sobre la trayectoria real.

**Dos correcciones que salieron de medir, no de razonar.** La primera versión del filtro
daba **17 recurrentes** sobre la trayectoria real; solo **7** eran patrones legítimos.
Las otras dos clases no son fallos del agente y merecían cubos propios:

```
guardarraíles   el arnés negándose a ejecutar algo (BLOCKED:, Access denied,
                Refusing to). MEDIDO: 42 % de los fallos de la ventana — 97 de 233.
                Un guardarraíl funcionando no es un error que el agente deba corregir.
                Proponer un cambio de conducta por él sería tratar el mecanismo de
                seguridad como si fuera el problema.

ráfagas         apariciones concentradas en horas. El caso que lo destapó: 37
                apariciones del mismo rechazo repartidas en 33 sesiones distintas
                cabían en 2,9 HORAS. Por el criterio de sesiones cruzaba el umbral con
                holgura (33 ≥ 2); por el de cuenta también (37 ≥ 3). No es recurrencia
                entre sesiones: es un episodio. De ahí MIN_SPAN_DAYS.
```

Ambos se reportan en lugar de descartarse en silencio, para que el descarte sea auditable.

**Tercera corrección, del ciclo fin a fin.** Con los dos filtros anteriores, el ciclo
corrió completo por primera vez y **propuso un cambio sobre un fallo muerto**: un
`File not found: wiki/SCHEMA.md` cuya última aparición había sido 14,5 días atrás — porque
el archivo se creó *después*. Proponer ahí gasta presupuesto, toca un skill, y el "éxito"
posterior no prueba nada: el fallo ya estaba resuelto antes del cambio.

```
caducos     el patrón existe en el histórico pero ya no ocurre. Se mide contra el
            SILENCIO (cuándo fue la última vez), no contra el span: un fallo puede
            llevar meses vivo apareciendo cada tres semanas. De ahí MAX_SILENCE_DAYS.
            MEDIDO: 5 de 8 candidatos llevaban más de 7 días sin aparecer.
```

**Cuarta corrección — las huellas mezclaban fallos distintos.** Medido sobre la
trayectoria real: la normalización tapaba la ruta entera y los números sueltos, así que
`File not found: /a/SCHEMA.md` y `File not found: /b/log.md` producían **la misma huella**
(25 apariciones → **18 fallos sin relación** bajo una sola). Igual con `exit_code=1` y
`exit_code=124` (51 apariciones → 8 fallos distintos). 16 % del total. Un grupo así se
vuelve un candidato Frankenstein: el fallo A lo mantiene "vivo" mientras el B se sigue
rompiendo, y el cambio propuesto no corresponde a ninguno de los dos. Se conserva el
último segmento de la ruta y se exceptúan `exit_code`/`exit_status`.

**Efecto sobre el resultado real:**

```
antes     17 "recurrentes"  (7 legítimos + 7 rechazos del arnés + 3 ráfagas)
después    7 recurrentes · 3 ráfagas · 16 rechazos del arnés · 83 eventos únicos
ahora      3 candidatos vivos · 5 caducos · 3 ráfagas · 16 rechazos del arnés
```

Los 3 candidatos vivos son fallos propios y recientes: un error de forma en `tool_call`,
un `Traceback` de `execute_code`, y una advertencia de envoltorio de shell en `terminal`.
Un `SCHEMA.md` que ya existe, un timeout de 60s que no se repite desde hace diez días y un
tope de memoria que ya no se toca quedaron en el cubo `stale` — con su motivo, no
borrados.

### Correcciones posteriores, del ciclo fin a fin

**El techo de cambios no se cobraba.** `KIND_EDIT` sólo aparecía en los tests del propio
`budget`: el límite "≤ 3 cambios/día" estaba escrito en el diseño y nunca se aplicaba.
Mientras hubiera presupuesto de inferencia se podían aplicar cambios sin tope. Ahora el
lugar se reserva antes de la primera escritura, y se libera en los tres casos en que el
cambio no ocurre: la escritura falla, el arnés lo deja en cola, o el arnés lo rechaza.

**La etapa 5 calificaba un solo cambio.** El docstring decía "cada cambio aplicado" y el
código usaba `last_applied`, que devuelve uno: con tres cambios aplicados, dos quedaban sin
veredicto de forma permanente. Se agregó `applied_entries` (todas las revertibles, con su
estado efectivo por `entry_id`) y `grade_applied_changes` ahora recorre esa lista.

**Las muestras del fallo se repetían.** Medido sobre el candidato real: las tres muestras que
el modelo recibía eran el mismo texto (1 distinta de 3), así que dos tercios del presupuesto
de evidencia se gastaban en copias y el modelo creía tener tres datos donde había uno. Ahora
se deduplican antes de recortar, conservando el orden de aparición.

**El modelo proponía a ciegas.** `build_prompt` ahora incluye los intentos previos sobre ese
skill, leídos del journal: rechazados, fallidos y revertidos. El dato se venía registrando
desde la etapa 4 y nadie lo leía. Es la pieza que WikiSkill pone en su `skill-impact.md`
(*"includes full content of rejected proposals — DO NOT repeat rejected approaches"*), y el
journal ya tenía todo lo necesario para reconstruirla.

## Etapa 2 — Techo (determinista)

**Pregunta:** ¿queda presupuesto hoy?

Límites duros, tomados del proyecto original porque son sensatos:

```
≤ 3 cambios por día       (radio de impacto)
≤ 30 llamadas por día     (costo)
```

Se evalúan **antes** de llamar al modelo. Si no hay presupuesto, la etapa termina sin
consumir nada.

**Advertencia de concurrencia (heredada del original):** el contador es
leer-y-actuar, y el registro es leer-modificar-escribir. Bajo canales concurrentes hay
ventana de carrera. Debe resolverse con bloqueo, no ignorarse.

**Criterio de cierre:** con el techo agotado, ninguna llamada al modelo ocurre.

### Estado de la implementación (etapa 2)

Implementada en `budget.py`, con `tests/test_budget.py` (59 tests),
`tests/medicion_budget.py` (concurrencia real) y `tests/control_negativo_budget.py`.

**La carrera, medida y cerrada.** El aviso venía heredado; acá está el número. Verificado
en el arnés que la fachada de estado del host (`PluginState`) hace atómico *cada* acceso
pero **no** la secuencia entre ellos: `get` toma el bloqueo y lo suelta, `set` toma el
bloqueo y lo suelta. La ventana queda en el medio. Con 12 procesos y el techo en 3:

```
sin bloqueo     12 cargos en disco · 12 concesiones · techo 3  →  9 de más
con bloqueo      3 cargos en disco ·  3 concesiones · techo 3  →  exacto
```

El control negativo existe para que el test positivo signifique algo: un test de
concurrencia que pasa no prueba nada si el escenario no tenía carrera.

**La ventana cerrada** es la secuencia entera —leer, decidir, anotar y soltar— dentro de
una sección crítica, con el bloqueo en un archivo propio (si se bloqueara el libro mismo,
un reemplazo atómico cambiaría el inodo y el bloqueo quedaría sobre un descriptor
huérfano). Mismo enfoque que el arnés usa para su registro de uso, implementado acá con
`fcntl`/`msvcrt` para no depender de internos que pueden cambiar sin aviso.

**Un defecto que solo apareció midiendo.** La primera versión sumaba las líneas ilegibles
al contador de **todos** los días. Consecuencia: un byte corrupto gastaba un lugar cada
día, para siempre — el aprendizaje habría quedado apagado por un problema de formato. La
versión correcta atribuye la corrupción **a un día** (el de la última fila legible, o el
de la última modificación del archivo si no hay ninguna): cuesta un lugar hoy, no puede
tapar un gasto, y mañana el libro arranca limpio sin borrar evidencia.

**Contar sobre estado efectivo, otra vez.** Las reservas van a un libro append-only, así
que liberar un token (una llamada que falló y no consumió) agrega una línea `released` en
lugar de reescribir la `charged`. El conteo se hace sobre el estado efectivo por token —
la última marca gana—, que es la misma lección de la etapa 4 aplicada antes de tropezar
con ella.

**El día es una etiqueta, no una resta.** Se guarda `AAAA-MM-DD` local en la reserva. Un
cambio de horario o una reserva a las 23:59 no mueven su propia entrada de día.

**Techos por configuración, con piso explícito.** `ceilings_from_config` lee
`max_edits_per_day` y `max_model_runs_per_day` con los defectos 3 y 30. Un valor absurdo
(0, negativo, no numérico) cae al defecto **y se avisa**: corregirlo en silencio dejaría a
quien lo configuró convencido de que su número rige.

## Etapa 3 — Propuesta (la única con modelo)

**Pregunta:** ¿cuál es el cambio mínimo que evita que este fallo se repita?

Entrada: la evidencia de la etapa 1. Salida: **una** propuesta, validada contra un
esquema:

```
action      : patch | create | no_op
target      : skill
nombre      : del skill afectado
contenido   : el cambio propuesto, mínimo
justificación
resultado esperado (cómo se sabrá si sirvió)
```

Se implementa con `ctx.llm.complete_structured(...)`, que devuelve salida validada
contra esquema. **Es la pieza que reemplaza al parche**: el proyecto original inventó
una API (`PluginInvocationRoute`) para enrutar la llamada dentro del turno; el host ya
ofrece una vía soportada.

**`no_op` es una salida legítima.** Poder responder "no corresponde ningún cambio" es
lo que separa un revisor de un generador de parches.

**Criterio de cierre:** una propuesta válida contra el esquema, una llamada, y
verificación de que el techo se respetó.

### Estado de la implementación (etapa 3)

Implementada en `proposal.py`, con `tests/test_proposal.py` (92 tests, modelo simulado),
`tests/medicion_proposal.py` y `tests/medicion_proposal_patch.py` (**llamadas reales**).

**Los dos caminos, medidos contra el arnés.** Dos corridas reales, un llamado cada una:

```
no_op     fallo: read_file → /home/andrea/wiki/SCHEMA.md x13   skill: hello-world
          respuesta: action="no_op"
          justificación: "El skill hello-world es una prueba mínima del pipeline del
          hub: no menciona rutas, ni wiki, ni SCHEMA.md, ni indica al agente leer
          ningún archivo."
          → el modelo NO inventó un cambio para tener algo que decir

patch     mismo fallo                                        skill: wiki-llm-ingesta
          respuesta: action="patch", ancla de 115 caracteres
          "2. Leer `SCHEMA.md` — los **types y tags vigentes**..."
          ocurrencias literales del ancla en el skill: 1
          delta: +172 caracteres
          → propuesta válida, materializable, sin escribir a disco
```

El segundo caso es el que importa: el modelo copió un ancla **literal** del skill real,
que aparece exactamente una vez. Es el resultado que la verificación determinista puede
comprobar — y contra el que un ancla inventada habría sido rechazada.

**Un defecto que solo apareció llamando de verdad.** `build_prompt` devolvía una lista de
strings. El arnés normaliza cada entrada con `_normalize_input_block`, que acepta un
`PluginLlmTextInput` o un dict `{"type": "text", "text": ...}` y **levanta
`ValueError` ante un `str`**: la primera corrida real murió con `Unsupported input block:
str`. El modelo simulado de los tests no lo detectaba porque aceptaba cualquier cosa. Es
exactamente el tipo de fallo que la simulación no puede ver: la interfaz real del host.

Se usa el dict plano y no la clase del host, para no acoplar el módulo a internos que
pueden cambiar sin aviso y para que los tests corran sin el arnés presente.

**La verificación no la hace el modelo.** `validate_proposal` comprueba en código propio:

```
el ancla existe EXACTAMENTE una vez   0 → el modelo describió el archivo como cree que
                                      es; >1 → la edición sería arbitraria
el cambio es mínimo                   un reemplazo sobre el techo de 4000 caracteres no
                                      es mínimo aunque compile
create sobre algo que ya existe       se rechaza y se propone la alternativa correcta
forma del JSON                        propia, sin depender de que el arnés tenga
                                      `jsonschema` instalado
```

Pedirle al modelo que verifique su propia salida sería pedirle que se autoevalúe, y un
examinador que se toma su propio examen no examina nada.

**Una llamada, sin reintentos.** Si el modelo devuelve algo inválido, se reporta. Reintentar
en silencio sería un techo que no se respeta — y el historial del proyecto (un revisor que
creaba skills sin control) muestra a dónde lleva eso.

**Presupuesto antes y después.** `propose` reserva **antes** de llamar (si reservara
después, dos llamadas concurrentes podrían pasarse del techo de costo) y **libera** el
lugar si la llamada falla. Verificado en la primera corrida: el lugar se liberó y el
contador volvió a cero.

## Etapa 4 — Journal y rollback (determinista)

Todo cambio aplicado queda registrado y es **reversible**.

```
journal: quién, qué, cuándo, contenido anterior (hash), contenido nuevo (hash)
rollback: un comando revierte el último cambio
```

**Requisito duro:** antes de escribir se guarda el estado anterior. Sin rollback
verificado no hay aplicación de cambios — se propone y se detiene.

**Criterio de cierre:** aplicar y revertir, comprobando que el contenido vuelve byte a
byte al estado previo.

### Estado de la implementación (etapa 4)

Implementada en `journal.py`, con `tests/test_journal.py` (51 tests) y
`tests/medicion_journal.py`, que hace el ciclo completo sobre un skill real del hub
(`harness/hello-world`) y verifica la restauración **por hash**:

```
1. respaldar    hello-world.baaad28153714f27.83a8f130f22b.bak  (1267 bytes)
2. aplicar      1250 → 1295 bytes
3. revertir     hash restaurado 83a8f130f22bf36ccc63b8e5d5e95d64
4. verificar    byte a byte idéntico al original : SÍ
                hash coincide                    : SÍ
                el cambio ya no está en el archivo: SÍ
                el original del hub no se tocó   : SÍ
```

**Por qué no se delega al host:** verificado que el arnés no tiene API de respaldo con
restauración para skills (el *ledger* es telemetría, sin rollback). De ahí que el módulo
implemente las dos piezas.

**Decisiones que sostienen el diseño:**

```
contenido completo, no diff     un diff depende de que el archivo esté en el estado que el
                                diff espera; si un tercero lo tocó en el medio, aplicarlo
                                en reversa produce algo que no es ni el estado viejo ni el
                                nuevo. El contenido completo restaura de forma determinista.

append-only                     una evidencia que se puede editar no sirve como evidencia.
                                Revertir AGREGA una línea que referencia a la vieja, en
                                lugar de reescribirla: así queda registrado que el cambio
                                estuvo aplicado, que es lo que hay que conservar.

se niega a pisar trabajo ajeno  si el contenido actual no coincide con el hash posterior
                                registrado, un tercero editó el skill. Revertir borraría ese
                                trabajo: se niega y lo explica, salvo ``force`` explícito.
```

**Un defecto que solo apareció midiendo.** El journal append-only guarda dos líneas por
cambio (`pending` + `applied`), así que un diagnóstico que contara líneas reportaría cada
cambio terminado como **escritura incompleta**. La medición real sobre el skill del hub lo
mostró (`pendientes: 1` con el cambio ya aplicado). El mismo error estaba en
`last_applied`, que devolvía una y otra vez la línea `applied` vieja de un cambio ya
revertido — el segundo rollback fallaba con *"ya fue revertida"* en lugar de retroceder al
anterior. Ambos se corrigen leyendo el **estado efectivo** (la última marca por
`entry_id`), no la última línea que coincide con el filtro. Los dos tienen test.

## Etapa 5 — ¿Sirvió? (determinista)

**Pregunta:** el cambio se hizo. ¿Funcionó?

Esta etapa **no lleva modelo**, y ese es un punto de diseño, no una omisión. Tres
comparaciones reproducibles:

```
1. ¿el cambio sobrevivió?     sha256 del contenido que escribimos
                              vs sha256 de lo que hay ahora
                              → si el agente lo editó después, no nos acreditamos

2. ¿se usó?                   conteo de usos sobre el registro del arnés
                              desde el momento del cambio

3. ¿el error volvió?          fingerprint del error registrado
                              vs fingerprint de los errores posteriores
```

Veredicto: `working` / `unused` / `unreliable` / `too_new`.

**Por qué determinista:** la pregunta es *"¿el cambio sobrevivió y se usó?"* — no
*"¿qué opinás del cambio?"*. La primera se responde con hashes y conteos; la segunda con
una opinión. Cambiar la primera por la segunda degrada la medición.

El chequeo 1 existe porque **el propio agente edita los mismos skills**. Sin él, el
plugin se acreditaría un cambio que en realidad hizo otro. Es honestidad de medición.

**Límite honesto:** estas tres comparaciones no responden *"¿el error paró por esta
causa?"*. Responden *"el cambio sobrevivió, se usó, y el error no reapareció"*. Atribuir
causalidad requiere leer las sesiones posteriores — razonamiento sobre trayectorias, no
una comparación. Queda fuera del alcance de esta versión, y se declara.

**Criterio de cierre:** los cuatro veredictos se reproducen sobre casos construidos.

### Estado de la implementación (etapa 5)

Implementada en `effect/`, sin dependencias externas:

```
effect/fingerprint.py   normalización del error + huella estable
effect/usage.py         conteo de usos, con su ALCANCE declarado
effect/checker.py       los tres chequeos, el veredicto y su evidencia
tests/test_effect.py    49 tests, los cuatro veredictos y los bordes
tests/medicion_real.py  la etapa 5 contra el registro real del arnés
```

**Un hallazgo que cambió el diseño.** El arnés **no expone** un "cuántas veces se usó
desde el momento X": expone un acumulado (`use_count` + `last_used_at`). De ahí una
asimetría que el módulo respeta en lugar de disimular:

```
last_used_at <= since_ts   →  0 usos. EXACTO.
last_used_at >  since_ts   →  hubo ≥ 1. El total NO es derivable del registro.
```

Para el segundo caso se consulta la trayectoria y el resultado se etiqueta
`since_approx`. Un conteo aproximado presentado como exacto haría que la etapa 5 decidiera
sobre un número que no significa lo que aparenta.

**Segunda distinción, la que decide el veredicto:** *no medir* no es *medir cero*. Un
skill sin entrada en el registro devuelve `unavailable`, nunca `0` — y `unavailable`
produce `too_new` (una espera), no `unused` (un veredicto). Confundirlas descartaría
cambios por falta de datos en lugar de por falta de efecto.

Se implementó primero y se probó contra el registro real (172 skills, 114 con uso).

## Alcance: skills, y nada más

El dominio de este plugin es **la gestión y adecuación de skills**. No es una decisión
de nomenclatura, es lo que el diseño puede sostener:

```
target del esquema        skill          (un solo valor)
fuente de la evidencia    state.db → messages   (la trayectoria)
señal de uso (etapa 5)    registro de uso de skills

MEMORY.md                 lectura de capacidad, NO target
holográfica               fuera de alcance
prompt                    fuera de alcance
```

**Por qué el enum no incluye `memoria` ni `prompt`.** La etapa 5 califica cada cambio
con `count_uses(...)`, y su señal existe **solo para skills**. El proyecto de origen lo
declara en su propio código: `usage_is_measurable = meta.get("kind", "skill") == "skill"`.
Un target sin señal de uso escribiría cambios que la etapa 5 no puede evaluar — es
decir, el revisor sin medición que este proyecto viene a reemplazar.

De ahí que el nombre del repositorio sea `hermes-skills-helper`: describe el dominio
real, no una aspiración más amplia.

**MEMORY.md se lee, no se escribe.** Aparece únicamente como dato de capacidad —cuánto
espacio libre queda— para que una propuesta no apunte a algo que el tope del host va a
rechazar. Es higiene de presupuesto, no dominio: el aprendizaje variable de este
ecosistema va a la capa holográfica, que es un proyecto aparte.

## Lo que este plugin NO hace

- **No toca el núcleo de Hermes.** Si una versión futura lo exigiera, se rediseña.
- **No escribe en MEMORY.md ni en USER.md**, y no toca la capa holográfica.
- **No aplica cambios sin poder revertirlos.**
- **No propone sobre un fallo que ocurrió una sola vez.**
- **No supera el techo diario**, aunque haya evidencia abundante.
- **No usa un modelo para decidir si un cambio sirvió.**
- **No conversa con el usuario** ni participa del turno: corre al cierre.

## Orden de implementación

```
1. Etapa 5  (¿sirvió?)      ← sin ella, no hay forma de saber si el resto sirve
2. Etapa 1  (recurrencia)
3. Etapa 4  (journal + rollback)
4. Etapa 2  (techo)
5. Etapa 3  (propuesta)     ← la única con modelo, y la última en escribirse
```

Se empieza por la medición: sin poder calificar un cambio, aplicar cambios es
exactamente el problema que este proyecto viene a resolver.

## Créditos

El diseño de las cinco etapas proviene de **Refine Cycle for Hermes Agent**, de
**Taras Boiko** (`Bergschloss`), MIT. Ver la sección de créditos en el
[`README`](../README.md).
