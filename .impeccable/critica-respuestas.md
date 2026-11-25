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
