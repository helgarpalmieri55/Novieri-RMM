# Respuestas a las preguntas de las críticas

Archivo del coordinador (plan del rediseño, «Cierre de cada oleada», paso 3). Las preguntas de una crítica de
impeccable no van al operador: el coordinador las contesta con `DESIGN.md`, el diseño aprobado y las decisiones
del operador, y las respuestas quedan aquí como vigentes. Cada oleada lee este archivo antes de su crítica y
añade al final las respuestas de las suyas; solo sube al operador, a través del coordinador, la pregunta que
estas fuentes no zanjan. Cada respuesta dice qué se eligió, por qué y en qué regla se apoya.

---

## `/__preview` (`frontend/src/preview/PreviewPage.tsx`), Task 3

Crítica `.impeccable/critique/2026-11-24T20-11-53Z__frontend-src-preview-previewpage-tsx.md` (28/40, cerrada).
Respondidas por el coordinador el 2026-11-24 y aplicadas en la ronda 2 de correcciones de la Task 3.

### 1. La ficha y el formulario dentro de la tarjeta «Kit v4»

- **Respuesta: (c).** Tres secciones, cada una dibujada como la dibujarán sus páginas: la lista en un panel
  desnudo (`Panel bare`), y la ficha (cabecera, pestañas y los paneles Detalles y Actividad) y el formulario
  (la sección y la barra de guardado) sobre el fondo de la página. El banco no muestra ninguna tarjeta dentro
  de otra.
- **Por qué.** El banco es la referencia que copian las ocho oleadas y el lugar donde `a11y-browser.mjs` mide
  cada pieza en píxeles. Dentro de la tarjeta, la ficha se medía sobre blanco cuando las páginas la ponen sobre
  el fondo de la página, y enseñaba una composición que ninguna página debe repetir.
- **Regla.** `DESIGN.md`, «Elevation & Depth»: una sola sombra, sin escala de elevación (una tarjeta dentro de
  otra es una segunda elevación); `DESIGN.md`, «Lists», «Records (detail)» y «Forms»: cómo se dibuja cada
  patrón; el suelo de oficio de impeccable: «nested cards are always wrong».

### 2. Los estados de fallo y de bloqueo de la barra de guardado

- **Respuesta: (c).** `SaveBar` acepta un nombre de región opcional (`label`). El banco dibuja la barra con un
  cambio, la que falló, la que no puede guardar y la de «Guardado.», una debajo de otra, y cada región tiene su
  nombre, así que harmony y `a11y-browser.mjs` las miden sin romper `landmark-unique`.
- **Por qué.** Son los estados que más importan cuando un guardado sale mal (la línea de fallo en tinta
  crítica, el motivo en tinta atenuada) y ninguna pasada los veía. Una página dibuja una sola barra y no la
  nombra: el nombre por defecto sigue siendo «Cambios sin guardar».
- **Regla.** `PRODUCT.md`, «Accessibility & Inclusion»: WCAG 2.2 AA medido, no afirmado; `DESIGN.md`, «Forms»:
  una sola barra para toda la consola; axe `landmark-unique`.

### 3. «Cerrar sesión» en el marco del portal incrustado

- **Respuesta: (c).** Oculto cuando `embedded`. La oleada O2 lo conserva cuando repinte el portal.
- **Por qué.** Un marco incrustado es una imagen del marco, sin sesión que cerrar. Un botón activo cerraría la
  sesión de quien desarrolla, y uno desactivado sería un control muerto.
- **Regla.** `DESIGN.md`, «Don't ship a dead control»; `PRODUCT.md`, principio 2: «Every control leads somewhere
  real».

---

## El armazón (`frontend/src/shell/Sidebar.tsx`, `AppShell.tsx`, `AreaPage.tsx`), Task 4

Críticas `.impeccable/critique/2026-11-24T23-59-19Z__frontend-src-shell-sidebar-tsx.md` (28/40),
`…-appshell-tsx.md` (29/40) y `…-areapage-tsx.md` (31/40), las tres cerradas. Contestadas el 2026-11-25 con
`DESIGN.md`, el diseño aprobado y las decisiones del operador, y aplicadas en el pulido de la Task 4. Las tres
preguntas de `AreaPage.tsx` no las zanjaban estas fuentes: las contestó el coordinador el 2026-11-25 (puntos 7
a 9, aplicadas en la ronda 1 de correcciones de la Task 4).

### 1. En español, el panel de Administración corta el nombre de sus páginas

- **Respuesta: (a).** El nombre de una página de una carpeta o de Administración pasa a una segunda línea en
  lugar de cortarse (`.nav-item.sub .lbl { white-space: normal; }`). No se estrecha la sangría ni se ensancha
  el riel.
- **Por qué.** Empieza a 77 px del borde y «Registro de auditoría», «Registros de seguridad» o «Portal del
  usuario final» no cabían: en `/audit` la página actual se leía «Registro de aud…». El operador trabaja en
  español.
- **Regla.** `DESIGN.md`, «Layout»: el riel mide 232 px; la maqueta de la dirección A fija la sangría en
  40 px; «The Floor Is The Ramp's Smallest Step» impide bajar la letra. Solo queda la segunda línea.

### 2. La sección abierta del panel de Administración al llegar a otra página suya

- **Respuesta: (b).** Se abre la sección que contiene la página actual cada vez que esta cambia, aunque el
  panel ya estuviera abierto.
- **Por qué.** Llegando desde fuera del panel (la búsqueda, una tarjeta de `/admin`), la página actual quedaba
  dentro de una sección cerrada y su marca no se veía.
- **Regla.** Decisión D10 (el panel abre la sección de la página actual) y el panel de Atera que el diseño
  §1.1 toma («el grupo de la página activa se abre», recorrido, línea 343).

### 3. El título «Administración» del panel, que enlaza a `/admin`

- **Respuesta: (b).** Se subraya al pasar el puntero, como los enlaces de la miga.
- **Por qué.** Era texto blanco sin ninguna señal de ser un enlace.
- **Regla.** `shell.css`, `.crumb a:hover`: el enlace de texto del armazón se subraya; D10: el título enlaza a
  la portada. (Los números de página de cada sección se quedan: son los del panel de Atera, recorrido, línea 44.)

### 4. El botón redondo del Asistente fuera de toda región (axe `region`, en todas las rutas)

- **Respuesta: (a).** Es lo último de `<main>`: sigue fijo en la esquina, se alcanza después de la página y
  queda dentro de una región.
- **Por qué.** Dibujado detrás de `</main>`, axe lo daba como contenido fuera de toda región en las 424
  capturas de cada idioma; una región propia necesitaría un nombre que repite el del botón.
- **Regla.** D12 (un enlace a `/assistant`, no un panel); `PRODUCT.md`, «Accessibility & Inclusion»: WCAG 2.2
  AA medido, no afirmado.

### 5. El menú «Nuevo» y el tabulador

- **Respuesta: (a).** Sus entradas no son paradas del tabulador (las flechas las recorren) y el menú se cierra
  cuando el foco sale de él.
- **Por qué.** Con Tab el foco salía y el menú quedaba abierto sobre la búsqueda.
- **Regla.** El patrón de botón de menú de WAI-ARIA que `NewMenu.tsx` declara; `PRODUCT.md`, «Accessibility &
  Inclusion».

### 6. Añadir un equipo desde la barra

- **Respuesta: (a).** Se queda en «Nuevo → Equipo» y en la cabecera de Equipos; ningún botón propio en la
  barra.
- **Regla.** Diseño §1.2: «Atera tiene "Instalar agente" como botón propio; aquí entra en el menú "Nuevo" y
  en la página de Equipos»; decisión D11.

### 7. La búsqueda de la portada de Administración (`/admin`), sola en un panel

- **Respuesta del coordinador: (b).** El campo va solo, encima de las tarjetas, sin la franja blanca del panel
  (`.admin-index-search`, con el ancho de la búsqueda de una lista). Y va en la URL: `?q=`, que sustituye la
  entrada del historial como en toda lista (`useUrlFilters` del kit). La búsqueda del panel del riel sigue
  siendo local: es parte del armazón, no de la página.
- **Por qué.** Un panel de 1158 px alrededor de un solo campo de 670 px era una caja para un control; el patrón
  de la barra de herramientas del kit es para la cabecera de una lista, y aquí no hay lista debajo.
- **Regla.** Global Constraints, «Todo con URL»; `DESIGN.md`, «Elevation & Depth» (una superficie es un
  contenido, no un marco para un control).

### 8. Las tarjetas de la portada de Administración, de alturas muy distintas

- **Respuesta del coordinador: (c).** Columnas de CSS (`columns: 2 320px`, una sola por debajo de 1024 px) y
  ninguna tarjeta partida (`break-inside: avoid`). El orden del DOM es el de lectura: la primera columna de
  arriba abajo y después la segunda.
- **Por qué.** En la rejilla de dos columnas, Automatización (dos páginas) junto a Equipos (cinco) dejaba
  huecos, y Plataforma quedaba sola en su fila.

### 9. El panel de Administración en `/admin`

- **Respuesta del coordinador: (b).** No abre ninguna sección: en la portada no hay página actual. La Task 4
  aplica la misma razón al panel abierto con su botón desde una página de fuera de Administración: tampoco hay
  página actual, y tampoco abre ninguna. En una página de Administración abre la sección que la contiene
  (respuesta 2).
- **Por qué.** Abrir la primera sección repetía la primera tarjeta y no respondía a nada que la persona
  estuviera mirando.


---

## Clientes: la lista y la ficha 360 (`frontend/src/modules/clients/ClientsPage.tsx`, `ClientDetailPage.tsx`), Task 5

Críticas `.impeccable/critique/2026-11-25T02-07-50Z__frontend-src-modules-clients-clientspage-tsx.md` (30/40) y
`…-clientdetailpage-tsx.md` (27/40), las dos cerradas. Las preguntas que zanjan `DESIGN.md`, el diseño aprobado,
el glosario y las decisiones del operador se contestan aquí y se aplicaron en el pulido de la Task 5; la que no
zanjan (punto 5) va al coordinador en el informe de la tarea.

### 1. La frase de la cabecera de la lista, de 98 caracteres

- **Respuesta: (a).** Una frase de 40 a 70 caracteres: «The companies you serve, with their sites, contacts
  and modules.» / «Las empresas que atiendes, con sus sedes, contactos y módulos.».
- **Por qué.** A 1440 dejaba «uses.» sola en la segunda línea (el detector, `straggler`).
- **Regla.** Receta L, punto 1: «una frase de 40 a 70 caracteres que dice qué es la página».

### 2. Los equipos de un cliente, con dos nombres en la misma pantalla

- **Respuesta: (a).** Los equipos de un cliente se llaman igual en la lista, en las tarjetas, en Datos y en la
  pestaña Sedes: «Equipos» en español; en inglés, «Computers» en toda la ficha 360 (la palabra de su pestaña y
  de su tarjeta) y «Devices» en la columna de la lista, la palabra del riel.
- **Por qué.** La tarjeta decía «Equipos 12» encima de la fila «Dispositivos 12» de Datos.
- **Regla.** El glosario de `src/i18n/es/index.ts`: «computer → equipo … la lista de máquinas, /devices, es
  "Equipos" en el riel y en su página» (D13).

### 3. Las comprobaciones de la pestaña Alertas, por su clave de protocolo

- **Respuesta: (a).** Cada comprobación por su nombre (`checkLabel`, «CPU ocupada»), el valor observado debajo y
  el equipo en una sola línea, como en la consola de alertas.
- **Por qué.** La pestaña escribía `cpu.busy_pct` y `service.running`, en inglés en la página en español.
- **Regla.** `PRODUCT.md`, principio 5 («every word exists in both»); el comentario de `checkLabel` en
  `modules/health/healthState.ts` (el título de la fila son las palabras; la clave es para los registros).

### 4. La pestaña elegida, fuera de la tira en un teléfono

- **Respuesta: (a).** La tira sigue siendo de una línea y se desplaza; es `Tabs`, del kit, quien lleva la
  pestaña elegida a la vista al llegar. La Task 5 lo pide al kit en su informe con el código; no toca
  `shell/components.tsx`.
- **Regla.** `DESIGN.md`, «Records (detail)»: «one row of tabs that change the URL»; «Archivos compartidos»,
  clase 1: `shell/**` es de las Tasks 1 a 4.

### 5. Datos repite el estado, el tipo y los dos números de las tarjetas

- **Sin respuesta en las fuentes.** `DESIGN.md` («Records») dice qué lleva la cabecera, las tarjetas y las
  pestañas, no qué lleva la pestaña Resumen. Opciones: (a) Datos conserva todos sus datos, como ahora; (b) Datos
  quita los cuatro que ya están arriba. Sube al coordinador en el informe de la Task 5.

### Ronda 1 de correcciones de la Task 5: lo que decidió el coordinador (2026-11-25)

- **Punto 5, respuesta del coordinador: (b).** Datos quita el tipo, el estado y los dos números: la cabecera y
  las tarjetas ya los dicen. Queda lo que no está en ninguna otra parte de la página.
- **Punto 2, en inglés: «Devices».** El diseño conserva el catálogo inglés y el riel dice «Devices», así que la
  ficha 360 también dice «Devices» (pestaña, tarjeta, columnas, «See every device of this client»); «Add a
  computer» sigue siendo el nombre del recorrido. En español, «Equipos» en todas partes, como en el punto 2.
- **Punto 4, hecho en el kit.** `Tabs` con `scroll` lleva la pestaña elegida a la vista, solo de lado (sin
  desplazar la página) y antes de pintar.

---

## Equipos (wave O1, Task 6): `/devices` and the ten pages around it

Críticas `.impeccable/critique/2026-11-25T04-19-0*Z__…` (once, una por página, todas cerradas). Las preguntas que
zanjan `DESIGN.md`, el diseño y las decisiones se contestan aquí y se aplicaron en el pulido de la O1; las que no
zanjan van al coordinador en el informe de la Task 6.

### 1. `/devices`: el pie de una lista de una sola página

- **Respuesta: (a).** El pie es el `Pager` del kit, con el rango en palabras: en una sola página no dibuja nada y
  la cuenta es «Mostrando x de y» de la barra; con varias páginas aparece con el rango y sus dos botones.
- **Por qué.** En una sola página dibujaba «Dispositivos anteriores» y «Dispositivos siguientes» desactivados junto
  a un rango que repetía la cuenta de la barra, y a 390 px el rango se cortaba por la izquierda.
- **Regla.** `DESIGN.md`, «Don't ship a dead control»; Receta L, 2 («el pie (Pager) si la página ya paginaba»).

### 2. `/devices`: cinco acciones en la cabecera

- **Sin respuesta en las fuentes.** `DESIGN.md` fija una sola principal, no cuántas secundarias. Opciones: (a)
  dejar las cinco (Añadir activo, Exportar inventario, Instalar el agente de forma remota…, Ejecuciones de
  instalación remota, Añadir un equipo); (b) «Ejecuciones…» como enlace fantasma; (c) un menú «Más», que el kit no
  tiene. La O1 deja (a), lo más cercano a la página de hoy, y lo sube al coordinador.

### 3. `/devices`: la búsqueda ocupa toda la primera fila de la barra

- **Sin respuesta en las fuentes; es del kit.** `ListToolbar` da a la búsqueda todo el ancho y los chips bajan a
  tres filas más. Cambiarlo en una página la separaría de las demás listas: la O1 lo pide al coordinador como
  cambio del kit (la base de flex de la búsqueda) y no lo toca.

### 4. `/devices/onboard` y `/agent-packages`: dos frases

- **Respuesta: (a) en las dos.** «Aparece en Equipos» (no «Dispositivos», D13) y la nota de «Muchos equipos en
  una red» empieza ya por «Esta plataforma nunca se conecta…», sin repetir el título de su sección. En
  `/agent-packages` la línea de las versiones dice «las versiones anteriores se ven con su filtro», sin «arriba»:
  el filtro está debajo de esa línea.
- **Regla.** El glosario de `src/i18n/es/index.ts` (D13); `PRODUCT.md`, la voz: decir lo que hay, sin señalar a
  donde no está.
---

## Oleada O2: Inicio, tickets y portal (Task 7)

Críticas `.impeccable/critique/2026-11-25T04-26-2[45]Z__…` de las once páginas (de `dashboardpage` a
`portalticketdetailpage`), todas cerradas. Contestadas por la oleada con `DESIGN.md`, el encargo y las respuestas de
arriba; las dos que no zanjan van al coordinador en el informe de la tarea (puntos 4 y 5).

### 1. Los estados de la cola de tickets, once chips

- **Respuesta: un `chip-select` con su nombre («Estado»), junto a los chips del alcance.** El encargo pedía
  `FilterChips «Estado»`; en 1440 los once chips ocupaban tres líneas y bajaban el primer ticket fuera de la vista.
- **Regla.** `DESIGN.md`, «Lists» (una barra de herramientas, no cinco); el cliente se dibuja igual (`ClientFilter`).
  El vocabulario sigue cerrado y la clave de la URL (`?status=`) no cambia.

### 2. Dos interruptores iguales que guardaban de dos maneras en Notificaciones

- **Respuesta: los dos son ajustes de la página.** Las reglas y los canales comparten un borrador, una barra y una
  guarda (`switches.tsx`); cada uno guarda con el mismo `PATCH { active }` de hoy.
- **Regla.** `DESIGN.md`, «Forms»: una sola barra para toda la consola; el encargo 2.8 («los ajustes con SaveBar»).

### 3. «Añadir buzón» primario en Entrada de correo

- **Respuesta: secundario.** El primario del cuerpo es el «Guardar» de la barra o el del diálogo abierto.
- **Regla.** `DESIGN.md`, «Buttons»: «One primary per page body».

### 4. Los guardados propios que quedan en Niveles de servicio (abierta)

- **Sin respuesta en las fuentes.** «Guardar configuración» (días de cierre automático) y «Guardar semana» del
  calendario siguen con su botón; el encargo 2.6 no los nombra. Opciones: (a) dejarlos; (b) una barra para la página.
  La oleada dejó (a) y lo sube al coordinador.

### 5. El marco del portal, sin `AppShell`, y el título de la pestaña (abierta, menor)

- **La oleada eligió**: cada página del portal nombra la pestaña solo dentro del marco del contacto
  (`portalTitle.tsx`); dentro de la consola (un técnico con un código del portal) la nombra el armazón, y el marco
  incrustado de `/__preview` no la toca. Sube al coordinador por si prefiere que el armazón del portal la nombre
  desde la ruta.
---

## Oleada O3 (Task 8): alertas, parches, software, sesiones remotas, trabajos y registros

Críticas `.impeccable/critique/2026-11-25T04-21-4*Z__…` (14, una por página, todas cerradas tras el pulido). Las
preguntas que zanjan `DESIGN.md`, el plan y las decisiones del operador se contestan aquí; las que no, van al
coordinador en el informe de la Task 8 (puntos marcados «sin respuesta en las fuentes»).

### 1. Alertas: ¿«Cerradas» necesita ser un chip, o solo el destino de los enlaces del informe?

- **Respuesta: (a) se queda como chip**, junto a «Abiertas» y los tres estados abiertos.
- **Por qué.** El selector de estado lo ofrecía, el informe enlaza a `?state=cleared&resolution=…`, y un filtro que
  la URL aplica sin un chip pulsado que lo diga es un filtro invisible.
- **Regla.** Global Constraints, «Comportamiento» (lo que la página hacía no cambia); Receta L, punto 5.

### 2. Alertas: ¿el título de la página pasa a «Alertas», como el riel?

- **Sin respuesta en las fuentes.** D13 cambia tres entradas del riel en español y nombra qué oleada cambia cada
  título; «Alertas» no está entre ellas y el plan no pide a la O3 cambiar `health.page.title`. Se deja «Salud y
  alertas» / «Health & alerts» y sube al coordinador.

### 3. Parches: ¿aprobaciones, reinicios y cumplimiento pasan a vistas como las de Alertas?

- **Respuesta: (b) no.** Quedan debajo de las pestañas de plataforma, como «el registro» de toda la flota.
- **Por qué.** D21 es una decisión para Alertas; el módulo documenta por qué ese registro no es de una plataforma y
  el plan no lo mueve.
- **Regla.** D21 (solo `/health`); Receta L (la página conserva su forma salvo lo que la oleada nombra).

### 4. Políticas de parches: ¿una política nueva en una página propia (`/patch/policies/new`)?

- **Respuesta: (b) no.** El formulario vive bajo la lista, en `?policy=new`, con su barra de guardado.
- **Regla.** Global Constraints: «Este plan no mueve ninguna» ruta; el plan define `/patch/policies` como la página
  FO.

### 5. Despliegues: ¿la vista «últimos 7 días» en un grupo de chips propio?

- **Respuesta: (b) no.** Sigue entre los chips de estado.
- **Por qué.** La tarjeta «Desplegados esta semana» enlaza a `?state=week`, y el parámetro no cambia.
- **Regla.** Receta L, punto 5: el filtro pasa a chips «con el mismo parámetro: la URL no cambia».

### 6. Paquetes: ¿decir por paquete si ya se puede desplegar?

- **Respuesta: (b) no en esta oleada.** Sería una función nueva (leer las reglas de detección); la fila ya dice «Sin
  archivo subido».
- **Regla.** Receta L; Global Constraints, «Comportamiento».

### 7. Scripts: ¿chips de intérprete solo para los intérpretes en uso?

- **Respuesta: (b) no.** Un chip por valor de `INTERPRETERS`, como pide el plan (O3 3.5).

### 8. Monitor de errores: ¿la antigüedad como selector, como el orden?

- **Respuesta: (b) no.** El periodo son chips (24 h, 7 días, 30 días); el orden no es un filtro y va en un
  `.chip-select`.
- **Regla.** Plan, O3 3.8.

### 9. Registros de seguridad: ¿la exportación de evidencias se estrecha al cliente del chip?

- **Respuesta: (b) no.** La exportación es la de hoy.
- **Regla.** Plan, O3 3.10, «Acepta: la evidencia exportada es la de hoy»; D20 (el chip solo estrecha la lista).
---

## Oleada O4: copias, continuidad, imágenes, servidores de sitio e inscripción, Task 9

Críticas `.impeccable/critique/2026-11-25T04-37-41Z__…` de las quince páginas de la oleada (de 27/36 a 30/40),
todas cerradas tras el pulido. Tres tenían tres asuntos prioritarios y por tanto preguntas: la copia externa
(`backup/OffsitePage.tsx`), el registro de una prueba (`bcp/TestRecorder.tsx`) y la inscripción
(`enrollment/EnrollmentPage.tsx`). Las fuentes las zanjan todas; se aplicaron en el pulido de la oleada. Las
demás críticas tenían menos de tres y llevan `Questions skipped`.

### Copia externa 1. La frase de la cabecera, de 79 caracteres (104 en español)

- **Respuesta: (a).** «A second, locked copy that this server cannot delete.» / «Una segunda copia bloqueada, que este
  servidor no puede borrar.».
- **Por qué.** A 390 ocupaba tres líneas y dejaba «borrarla.» sola (el detector, `straggler`).
- **Regla.** Receta L, punto 1 (40 a 70 caracteres), aplicada a toda cabecera de página; la respuesta 1 de los
  clientes, Task 5.

### Copia externa 2. El editor de un almacenamiento, que ninguna captura enseña

- **Respuesta: (a).** Se pide al coordinador una variante de harmony con una sesión que tenga
  `backup.offsite.manage` (la sesión de prueba vive en `fixtures/core.ts`, clase 1); mientras tanto lo miden
  las pruebas de `__tests__/offsiteKit.test.tsx`.
- **Regla.** «Archivos compartidos», clase 1 (`fixtures/**` es de la Task 3): una oleada no lo edita, lo pide.

### Copia externa 3. Un paso de la prueba escrito por su clave («reach»)

- **Respuesta: (a).** El dato de prueba usa la clave que el servidor escribe (`connect`, `backup/offsite.py`).
- **Regla.** `PRODUCT.md`, principio 5 (cada palabra en los dos idiomas); el dato de prueba sigue al servidor,
  como ya pide `coverage.fixture.test.tsx` para el informe de cobertura.

### Registro de una prueba 1. Tres botones primarios en el cuerpo de una página

- **Respuesta: (a).** «Registrar prueba» es el único primario; «Guardar paso» y «Añadir hallazgo» van en
  contorno (`FormActions variant="outline"`).
- **Regla.** `DESIGN.md`, «Buttons»: «One primary per page body».

### Registro de una prueba 2. Los tres números de la instantánea, una tarjeta dentro de otra

- **Respuesta: (a).** Una lista de definiciones sin marco (`KV`) dentro del panel.
- **Regla.** `DESIGN.md`, «Elevation & Depth» (una sola sombra, sin segunda elevación); la respuesta 1 del banco
  `/__preview` («nested cards are always wrong»).

### Registro de una prueba 3. El resultado dos veces

- **Respuesta: (a).** Sale de la tira de datos: lo dice la insignia de la cabecera. Lo mismo en la ficha de un
  plan con su estado.
- **Regla.** La decisión del coordinador sobre los Datos de la ficha 360 (Task 5, punto 5): lo que la cabecera
  y las tarjetas ya dicen no se repite.

### Inscripción 1. «Mostrando x de y»

- **Respuesta: (a).** Al final de la barra de filtros de cada pestaña: los tokens sobre el total de la
  organización para esa vista (`total`), los certificados sobre los equipos que la pestaña leyó.
- **Regla.** `DESIGN.md`, «Lists»: «"Showing x of y" at the end of the toolbar»; Receta L, punto 2.

### Inscripción 2. La frase de la cabecera, de 93 caracteres en español

- **Respuesta: (a).** «The tokens that enroll computers, and the certificates they get.» / «Los tokens que inscriben
  equipos y los certificados que reciben.».
- **Regla.** Receta L, punto 1.

### Inscripción 3. Las cuatro tarjetas sobre tres pestañas

- **Respuesta: (a).** Se quedan: cada una enlaza a la vista que cuenta (la de tokens activos, a su pestaña).
- **Regla.** `DESIGN.md`, «Records (detail)» y la regla de la consola de que un número lleva a donde vive; el
  pulido no rediseña (`reference/polish.md`).
---

## Oleada O7: administración y cuenta (`settings`, `profile`, `ai-admin`, `ai-automation`), Task 12

Críticas `.impeccable/critique/2026-11-25T06-40-33Z__…` de `SettingsPage.tsx` (29/40), `ProfilePage.tsx` (30/40),
`SecurityPage.tsx` (31/40), `AiSettingsPage.tsx` (29/40), `ChatPanel.tsx` (30/40), `AutonomyDialPage.tsx` (31/40) y
`ActionsPage.tsx` (30/40), todas cerradas tras el pulido. Las preguntas que zanjan `DESIGN.md`, el diseño y las
decisiones se contestan aquí; las que no, van al coordinador en el informe de la tarea.

### 1. El estado «Configurado» en la pestaña de Configuración

- **Respuesta: (a).** Se queda en las dos formas, «Correo electrónico · Configurado» y «· Sin configurar», como
  parte del nombre de la pestaña.
- **Por qué.** Las pruebas del estado (Task 7 de su ciclo) fijan las dos frases; el color nunca es el único
  portador (`DESIGN.md`, «Un acento y cinco severidades»), así que el estado es texto y no un punto.
- **Regla.** `DESIGN.md`, «el color nunca es el único portador»; Global Constraints, «Ninguna prueba existente se
  borra». Que la tira lo dibuje en una ranura propia es una petición al kit (`TabItem.note`).

### 2. Los chips de estado de una lista usan las palabras de las etiquetas de sus filas

- **Respuesta: (a).** Usuarios: Activo, Invitación pendiente, Deshabilitado, Eliminado (las etiquetas de la fila);
  Acciones del asistente: Pendiente de aprobación, Ejecutado, Rechazado, Caducado, con «Todos» concordando con
  «Estado».
- **Por qué.** Dos palabras para un estado en una pantalla («Desactivadas» encima de «deshabilitado»).
- **Regla.** `PRODUCT.md`, principio 5 («every word exists in both»); el glosario de `src/i18n/es/index.ts`.

### 3. Notas y pistas a lo ancho de una sección

- **Respuesta: (a).** Una nota larga dentro de una sección se limita a la medida de los avisos (76ch).
- **Regla.** `components.css`, `.notice { max-width: 76ch }` (la medida del subtítulo de página, 70ch de texto).

### 4. Dos marcos en una página (Tu perfil)

- **Respuesta: (a).** El bloque «Inicio de sesión y seguridad» es una sección como «Tus datos», no un panel con
  cabecera rayada.
- **Regla.** `DESIGN.md`, «Forms»: secciones plegables; una página no mezcla dos estilos de tarjeta.
---

## Las pantallas antes de una sesión (`frontend/src/modules/auth/*Page.tsx`, `frontend/src/modules/setup/SetupPage.tsx`), Task 13

Críticas `.impeccable/critique/2026-11-25T05-34-19Z__…` de las doce páginas (de 24/32 a 32/40), todas cerradas tras el
pulido. Las preguntas que zanjan el encargo de la oleada, `DESIGN.md`, las reglas generales del coordinador y las
Global Constraints se contestan aquí; la que no zanjan (punto 3) va al coordinador en el informe de la tarea.

### 1. Los botones desactivados hasta que el formulario está completo

- **Respuesta: se quedan activos y la pulsación dice lo que falta**, en los seis formularios de más de un campo:
  inicio de sesión y el del portal («Introduce tu correo electrónico y tu contraseña.»), el enlace de cambio, la
  invitación y la recuperación con contraseña obligatoria (la regla que incumple: «Usa al menos 12 caracteres.»), y
  el alta del dueño («Rellena todos los campos para terminar.»). No se envía nada. Los de un solo campo (código de la
  aplicación, código de recuperación, nombre de la clave de acceso, correo de la contraseña olvidada) y la casilla de
  los códigos guardados siguen desactivados mientras están vacíos: el campo de al lado es el motivo.
- **Por qué.** Desactivados, no decían por qué; en el alta del dueño, con siete campos sin marcar, una persona que
  dejaba uno vacío solo veía un botón gris.
- **Regla.** Reglas generales del coordinador, 8 y su aclaración 8a (2026-11-25): solo un formulario de un campo
  puede dejar el botón desactivado sin motivo escrito; los demás dejan el botón activo e informan de lo que falta al
  enviar. Ninguna llamada a la API cambia y ningún texto de error existente cambia; los dos textos nuevos solo dicen
  lo que falta.

### 2. El titular «Entrando con Microsoft» sobre un fallo

- **Respuesta: se queda.** El aviso de debajo dice que no terminó.
- **Por qué.** Un titular por estado sería un texto nuevo de la página, y el encargo conserva los textos de cada
  pantalla de acceso.
- **Regla.** Encargo de la Task 13: «Cada página conserva sus textos, sus llamadas y sus errores; solo usa el marco».

### 3. ¿La acción principal a lo ancho de la tarjeta en todos los formularios del marco?

- **Sin respuesta en las fuentes.** El encargo la pone a lo ancho en el inicio de sesión y en el del portal (8.1,
  8.10) y dice de las demás «solo usa el marco»; `DESIGN.md` no dice nada del ancho de un botón. Opciones: (a) solo
  los dos inicios de sesión, como ahora; (b) toda pantalla de un solo formulario (contraseña olvidada, enlace de
  cambio, invitación, recuperación, alta del dueño). La oleada deja (a), lo literal del encargo, y lo sube al
  coordinador.

### 4. «No volver a preguntar en este equipo» como enlace de texto

- **Respuesta: sigue siendo una casilla.** Solo «Usar un código de recuperación» pasa a enlace (`?use=recovery`).
- **Por qué.** La casilla es una elección que se envía con el factor (`remember_device`); un enlace no puede llevarla.
  Convertirla cambiaría la llamada a la API.
- **Regla.** Global Constraints, «Comportamiento» (las llamadas no cambian); encargo 8.2 («el código se envía igual»).

### 5. El aviso de lo que permite aprobar, dentro de la tarjeta del consentimiento (`nested-cards`)

- **Respuesta: falso positivo; el aviso se queda donde está.** Orden para el coordinador:
  `impeccable ignores add-value nested-cards "*" --file "frontend/src/modules/auth/ConsentPage.tsx" --reason "the .notice.gold is the design's inline warning (role note), placed in the card beside Approve by plan Task 13 8.11 and invariant 1c; a notice is a message, not a card" --shared`.
- **Por qué.** Un `.notice` es el mensaje en línea del diseño, no un contenedor; el encargo pide exactamente ese aviso
  dentro de la tarjeta, junto a «Aprobar», y el aviso de «solo en este equipo» siempre fue el mismo elemento.
- **Regla.** Invariante 1c de `CLAUDE.md`; encargo 8.11; la respuesta 1 de `/__preview` trata de paneles dentro de
  paneles, no de avisos.
## Oleada 6: documentación, cambios, presupuesto, personas, credenciales e informes, Task 11

Quince críticas de 2026-11-25T06-58-35Z (`.impeccable/critique/2026-11-25T06-58-35Z__…`), de 28/40 a 30/40,
todas en modo `⚠️ DEGRADED: single-context`. Solo tres tienen tres asuntos prioritarios o más y por eso
preguntas: la documentación (`DocsPage.tsx`), la lista de cambios (`ChangesPage.tsx`) y el resumen del
presupuesto (`BudgetDashboard.tsx`); las otras doce escriben `Questions skipped: <n> priority issues`. Las
respuestas que zanjan las fuentes van aquí; las dos que no, al coordinador en el informe de la Task 11.

### 1. Documentación: ¿el lector junto a la lista es el `DetailGrid` del kit?

- **Sin respuesta en las fuentes.** El encargo pide `DetailGrid` con la lista a la izquierda y el lector a la
  derecha; el `DetailGrid` del kit pone su columna estrecha (1fr) a la derecha, así que la biblioteca quedaría
  a la derecha del lector. La página conserva su rejilla de dos columnas (lista a la izquierda, como la dibujan
  Atera y el encargo) y pide al kit una opción para poner la columna estrecha primero. Sube al coordinador.

### 2. Documentación: la nota bajo la búsqueda, ¿merece su línea?

- **Respuesta: sí, se queda.** Dice qué no busca la búsqueda.
- **Regla.** La especificación del módulo, §11.6, pide que la página lo diga.

### 3. Documentación: en un teléfono, las cuatro tarjetas antes de la biblioteca

- **Respuesta: sin cambio en la página.** El kit pliega todas las páginas igual.
- **Regla.** `PRODUCT.md`, principio 1 (las páginas se parecen a propósito); `StatsGrid` es del kit.

### 4. Cambios: ¿una lista de estados rompe «los filtros de estado como chips»?

- **Respuesta: no; se aplica en el pulido.** «Todos» y «Abiertos» (las vistas que abren las tarjetas) siguen
  siendo chips; los siete estados y los tres tipos pasan a dos chips con lista (`.chip-select`, como el chip
  «Cliente»), sobre las mismas claves `?state=` y `?type=`, cada opción con su número.
- **Por qué.** Trece chips eran dos filas a 1440 y siete a 390 antes del primer cambio (P1 de la crítica).
- **Regla.** `DESIGN.md` (una decisión con pocas opciones a la vista); el chip con lista es un chip del kit
  (`shell/listKit.tsx`, `chip chip-select`).

### 5. Cambios: ¿se esconde un estado que abriría una vista vacía?

- **Respuesta: no.** Esconderlo haría que un enlace a ese estado pareciera roto; su número ya dice que está
  vacío, y el vacío dice por qué con «Quitar filtros».
- **Regla.** Receta L, punto 8 (el vacío dice su causa).

### 6. Cambios: el panel de requisitos ocupa la primera pantalla

- **Respuesta: sin cambio.** Es el panel compartido del módulo y se pliega cuando no falta nada.

### 7. Presupuesto: ¿la tabla de desviación sigue al periodo del gráfico?

- **Sin respuesta en las fuentes.** Lista los doce periodos bajo un gráfico de uno. La regla de la oleada es que
  lo que una página muestra no cambia: se deja como está y sube al coordinador.

### 8. Presupuesto: «Elige un cliente arriba» es un h3 bajo el h1

- **Respuesta: h2.** Es la única sección de la página con «Todos mis clientes»; `BudgetPickClient` dibuja su
  título con `headingLevel={2}`.
- **Regla.** axe `heading-order` (moderado) en las cinco páginas del presupuesto; la escala de `DESIGN.md`.

### 9. Presupuesto: el chip del presupuesto es ancho en la cabecera

- **Respuesta: se queda.** Nombra el presupuesto que se ve.
- **Regla.** El encargo, 6.3: el presupuesto elegido como `.chip-select` en la cabecera.
---

## Activos: licencias, suscripciones y direcciones IP (oleada O5), Task 10

Críticas `.impeccable/critique/2026-11-25T07-00-05Z__…` y `…07-00-06Z__…` (las dieciocho páginas de la oleada,
28 a 30/40, todas cerradas). Las preguntas que zanjan `DESIGN.md`, el diseño aprobado, las decisiones del operador
y las reglas generales del coordinador se contestan aquí y se aplicaron en el pulido; las que no zanjan (puntos 6
a 8) van al coordinador en el informe de la tarea.

### 1. Las fichas repiten en Detalles lo que dicen las tarjetas o la cabecera

- **Respuesta: quitarlo de Detalles.** La licencia ya no repite «Derechos de uso» bajo la tarjeta «Con derecho»;
  la suscripción no repite costo, puestos ni fecha de renovación; la subred no repite la puerta de enlace en el
  contexto de la cabecera (la dicen los datos).
- **Regla.** La respuesta del coordinador a la ficha 360 (punto 5, (b)): Detalles conserva lo que no está en
  ninguna otra parte de la página.

### 2. El gasto dibujaba los totales dos veces (tarjetas y tabla «Por moneda»)

- **Respuesta: solo las tarjetas.** Cada tarjeta lleva el cliente, la moneda, el total mensual y el anual, que son
  las columnas de la tabla; la frase «Nunca se suma entre clientes ni entre monedas» va bajo las tarjetas.
- **Regla.** `DESIGN.md`, «Elevation & Depth» y el suelo de oficio (un hecho, un lugar); el encargo de la oleada
  («cada total un StatCard»).

### 3. Un producto sin regla de coincidencia decía «0 instalaciones»

- **Respuesta: «sin datos de uso».** Nada puede detectarlo, así que el 0 es «nadie miró»; el catálogo y el
  cumplimiento lo dicen igual.
- **Regla.** The Absent Zero Rule (`DESIGN.md`, `PRODUCT.md`).

### 4. Cuatro tarjetas en Licencias y cinco en Suscripciones, no las cinco y seis del encargo

- **Respuesta: las de hoy.** «Gasto anual», «Asientos sin usar» y «Ahorro identificado» no tienen una cifra honesta
  detrás (los docstrings de cada página) y una tarjeta sin lista detrás es la tarjeta muerta que el operador pidió
  dejar de enviar.
- **Regla.** `PRODUCT.md`, principio 2 («Every control leads somewhere real»); la regla de las tarjetas con `to`.

### 5. La cola de revisión sin barra de acciones en lote

- **Respuesta: sin `BulkBar`.** La cola no tiene ninguna acción en lote hoy, y la receta solo la dibuja donde la
  hay.
- **Regla.** Receta L, punto 2.

### 6. Cumplimiento: «cada cifra un StatCard» (sin respuesta en las fuentes)

- La página conserva su tabla (las mismas cifras) y hace de cada cifra («58 / 50») el enlace a las licencias de
  ese producto y cliente. Una tarjeta por posición (cliente, producto) no escala a cuarenta productos. Opciones:
  (a) la cifra como enlace, como queda; (b) una `StatCard` por posición. Sube al coordinador.

### 7. Deriva con chip «Cliente», contra el «Sin chip» del encargo (sin respuesta en las fuentes)

- La regla general 2 pide que la tarjeta «Con deriva» del árbol, estrechada por el chip, abra los hallazgos que
  contó con `client=`; la página de deriva lee el chip para poder hacerlo. Opciones: (a) el chip en Deriva, como
  queda; (b) la tarjeta del árbol cuenta la deriva sin estrechar y enlaza sin cliente. Sube al coordinador.

### 8. Autorizaciones de escaneo sin chip «Cliente» (sin respuesta en las fuentes)

- El encargo dice «Sin chip» (invariante 2a(d)); el mensaje del coordinador nombra `/ipam/scan-authorizations`
  entre las páginas cuyo `?client=` es del chip. La página no lo dibuja. Sube al coordinador.
