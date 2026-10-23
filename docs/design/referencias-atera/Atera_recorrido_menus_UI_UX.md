# Atera — Recorrido de menús, pantallas y UI/UX

Oct 22, 2026 · @Helgar Palmieri

## Alcance y metodología

Recorrido en vivo de la cuenta de Atera de Helgar (app.atera.com, plan en periodo de prueba) desde Claude in Chrome, pantalla por pantalla. Todo lo descrito aquí se observó directamente en la interfaz; donde una pantalla estaba vacía por falta de datos, se indica.

- Solo se navegó y se abrieron menús, pestañas y paneles de lectura.
- No se creó, guardó, eliminó, instaló ni compró nada; no se aceptaron términos ni se enviaron formularios.
- Las funciones bloqueadas por plan o que exigen un agente instalado se describen como se muestran (pantalla vacía, upsell o paywall).

Cobertura: 56 pantallas de menú visitadas (15 del menú principal y 41 de Admin), más fichas de detalle y formularios de creación. Tres pantallas no cargaron: **Billing**, **Accounting** y **Products and expenses**. Son páginas del UI antiguo (AngularJS dentro de un iframe `/Admin?miniViewIframe`) y quedaron en blanco con el error `$ is not defined` en consola, también en una recarga manual. Lo mismo pasó con los visores de reportes clásicos antiguos.

Incidentes durante el recorrido:

- En el Dashboard se desmarcó por accidente el widget **Customer tickets** desde *Edit widgets*; volver a marcarlo no lo restauró. Se arregla con *Edit widgets → Reset to default* (no se hizo, porque reescribe todo el layout).
- El ticket de muestra #1 quedó abierto como pestaña en la tira de tickets. No se modificó.

## Mapa de navegación

Atera tiene dos navegaciones: una barra lateral operativa con 14 entradas y un panel **Admin** que reemplaza esa barra con 7 grupos en acordeón y 41 páginas de configuración. Arriba hay una barra global fija (New, búsqueda, Install agent, notificaciones, ayuda, perfil) y un botón flotante de AI Copilot.

```text
Barra global fija: New · Buscar · Install agent · Notificaciones · Ayuda · Perfil · AI Copilot

Barra lateral principal (14 entradas)        Panel Admin (41 páginas en acordeón)
├─ Dashboard                                 ├─ My account ·················· 3
├─ Tickets                                   ├─ Users and security ·········· 5
├─ Customers                                 ├─ Monitoring and automation ··· 9
├─ Devices                                   ├─ Support and ticketing ······· 8
├─ Alerts                                    ├─ Customer service ············ 2
├─ Patch Management                          ├─ Data management ············· 5
├─ Asset Inventory (New)                     ├─ Business administration ····· 8
├─ Network Discovery                         └─ App Center settings ········· 1
├─ Reports › Classic · Advanced
├─ Billing                                   (Patch automation y Thresholds
├─ More › Knowledge Base · Refer a Friend     vuelven a la barra principal)
├─ AI Center (New)
├─ App Center
└─ Admin ──────────────► abre el panel
```

El número junto a cada grupo de Admin es la cantidad de páginas que contiene.

| Grupo | Menú › Pantalla | Ruta |
| --- | --- | --- |
| Principal | Dashboard | `/new/dashboard` |
| Principal | Tickets (pestañas Tickets, Schedule y una por ticket abierto) | `/new/tickets`, `/new/scheduled-tickets`, `/new/ticket/{id}` |
| Principal | Customers (lista y ficha 360°) | `/new/customers`, `/new/customer/{id}` |
| Principal | Devices (lista y ficha de equipo) | `/new/devices`, `/new/rmm/device/{id}` |
| Principal | Alerts | `/new/alerts` |
| Principal | Patch Management | `/new/patch-management` |
| Principal | Asset Inventory (New) | `/new/asset-inventory` |
| Principal | Network Discovery | `/new/networkdiscovery` |
| Principal | Reports › Classic Reports | `/new/reports` |
| Principal | Reports › Advanced Reports | `/new/advanced-reports` |
| Principal | Billing (no cargó) | `/new/billing` |
| Principal | More › Knowledge Base | `/new/kb/center` |
| Principal | More › Refer a Friend | `/new/refer` |
| Principal | AI Center (New) | `/new/ai` |
| Principal | App Center | `/new/applications` |
| Admin › My account | Account settings · My profile · Subscription | `/new/admin/settings`, `/new/myprofile`, `/new/admin/billing/subscription/in-trial-purchase` |
| Admin › Users and security | Technicians · Technician groups · Access roles · Security and authentication · Audit log | `/new/admin/technicians`, `/technician-groups`, `/roles`, `/security`, `/history` |
| Admin › Monitoring and automation | Patch management and IT automation · Configuration policies · Scripts · Thresholds · Alert settings · SNMP templates · Software management · Network Discovery alerts · Remote access settings | `/new/rmm/itautomation/list`, `/new/admin/configuration-policies/list`, `/scripts`, `/new/rmm/threshold/list`, `/new/admin/alerts`, `/snmptemplate/list`, `/software-management`, `/networkdiscovery-advanced-settings`, `/remote-access-settings` |
| Admin › Support and ticketing | Email settings · Ticket automation rules · Ticket settings · Ticket forms · Email templates · Quick Reply templates · Calendar integration · WhatsApp settings (New) | `/new/admin/emails-settings`, `/automationrules`, `/settings?tab=tickets`, `/forms`, `/emailtemplates`, `/quickreplytemplates`, `/calendar-integration`, `/whatsapp-config` |
| Admin › Customer service | Customer Portal · White label | `/new/admin/portal`, `/whitelabel` |
| Admin › Data management | Custom fields · Custom assets · API · Import data · Integrations (New) | `/new/admin/dynamicfields`, `/custom-asset-types/list`, `/api`, `/import`, `/integrations` |
| Admin › Business administration | Contracts · SLAs · Business hours · Accounting (no cargó) · Products and expenses (no cargó) · Contract rates · Contract expiration · Taxes | `/new/admin/contracts`, `/sla`, `/business-hours`, `/accounting`, `/productsandexpenses`, `/rates`, `/settings?tab=expirations`, `/taxlist` |
| Admin | App Center settings | `/new/applications/settings` |

Dos páginas de Admin (Patch management and IT automation, Thresholds) viven bajo `/new/rmm/` y al abrirlas vuelve la barra principal: el usuario sale del contexto Admin sin aviso.

## Detalle por pantalla

Cada pantalla se describe con tres partes: para qué sirve, qué contiene y cómo está diseñada. En UI/UX, **+** marca un acierto y **−** un problema observado.

### Estructura global (shell)

**Para qué sirve:** dar acceso permanente a crear, buscar, instalar agentes y pedir ayuda desde cualquier pantalla.

- **Barra lateral oscura:** 11 destinos operativos arriba; AI Center, App Center y Admin anclados abajo. Se colapsa a un riel de íconos ("Pin menu"); en el riel, los badges *New* se vuelven puntos azules.
- **New ▾:** Ticket, Contact, Customer, Invoice · Asset, Script, Monitored device (SNMP, TCP, HTTP, Generic) · Agent installer.
- **Search Atera:** un panel de alcance explica qué campos busca por entidad (Tickets, Devices, Customers, Contacts, Technicians, Assets); Pages y Knowledge base aparecen como *Coming soon*.
- **Install agent:** asistente modal de 3 pasos (sistema operativo Windows .msi / Mac .pkg / Linux .bash → cliente y carpeta → descargar, línea de comandos o enlace compartible).
- **Iconos:** campana (Notification Center, pestañas All/Unread), triángulo (Unassigned Agents / Alerts), "?" (panel lateral con chat de soporte, Help Center, Academy, Community, Product updates), avatar (disponibilidad, My profile, General settings, Splashtop SOS, Grant access, app móvil, Log out).
- **AI Copilot flotante** (Ctrl+Shift+Space): prompts sugeridos, entrada con "/" para citar ticket, equipo o alerta, adjuntar imagen o captura, micrófono e historial.
- **Banner de prueba** azul con días y agentes restantes y botón *Subscribe now*.

**UI/UX**

- **+** El menú *New* concentra toda la creación en un solo punto; la búsqueda declara exactamente qué campos indexa.
- **+** La IA está a un clic en todas partes (botón flotante, columna AI en tablas, botón Copilot en fichas).
- **−** El banner de prueba aparece en unas cargas y en otras no, y a veces entra después de cargar: desplaza el contenido.
- **−** Varios menús no cierran con Escape y algunos popovers "se comen" el primer clic siguiente.
- **−** El título de la pestaña del navegador es el mismo en todas las páginas.

### Dashboard — `/new/dashboard`

**Para qué sirve:** vista rápida del estado del service desk y del RMM para el MSP o el equipo de TI.

- Cuadrícula de 17 widgets arrastrables, redimensionables y removibles: Ticket status (Open, Pending, Due today, Overdue), Alert status (Warning, Critical), Unassigned tickets, Recent alerts, Availability monitoring (tipo de equipo × online/offline), Customer alerts, Server alerts, Customer tickets, Ticket activity (7 días), Critical and overdue tickets, Product tickets, Map overview, Average ticket SLA statistics, Satisfaction, Dissatisfied customers, Patching status, Recent critical alerts.
- *Edit widgets* (lista de casillas + *Reset to default*) y *Enter full screen*.

**UI/UX**

- **+** Muy personalizable; los estados usan píldoras de color consistentes (azul, amarillo, naranja, rojo).
- **−** Quitar un widget es un clic sin confirmación ni deshacer; al volver a marcarlo no reapareció.
- **−** Los widgets vacíos quedan en blanco, sin mensaje ni paso siguiente; solo un ícono "?" da contexto.
- **−** *Enter full screen* no produjo cambio visible.

### Tickets — `/new/tickets`

**Para qué sirve:** triage y resolución de solicitudes de soporte con SLA, tiempo facturable e IA.

- **Tira de pestañas de trabajo:** Tickets, Schedule y una pestaña por ticket abierto (estilo navegador, con "Close all tabs").
- **Encabezado:** vistas guardadas (*Default view ▾*) y *+ Ticket*.
- **Barra de herramientas:** búsqueda, chip *Filters · 2* (Open y Pending por defecto), ajustes de tabla (densidad Detailed/Compact, columnas, ajustar ancho, reset, navegación por teclado).
- **Acciones masivas:** Delete, Assign ticket, Set status, Set priority, Merge tickets.
- **Columnas:** Details, AI Assistant, SLA, Sentiment (emoji), Assigned group, Assigned technician, Device, Priority, Activity status, Status; opcionales Ticket ID, Customer, Contact, Approval status, Last action date, Creation date. Varias celdas se editan en línea.
- **Filtros (panel lateral):** 18 criterios, entre ellos Tags, Spam, Status, técnico, grupo, fechas, prioridad, impacto, tipo, origen, cliente, equipo y rango del cliente.
- **Schedule:** tickets recurrentes en borrador (Details, Assigned technician, Priority, Frequency).
- **Detalle del ticket (dos paneles):**
  - Izquierda: conversación en burbujas (Conversation / Ticket activity), *Generate article* con IA y editor con Public reply / Internal note, "Generate reply" y *Compose with AI*.
  - Derecha: cronómetro de tiempo (Start, entrada manual, ver entradas) y riel de 4 íconos: Overview, Contact info, Properties, Calendar.
  - Menús: Manage ticket (aprobación, ticket padre/hijo), Copilot, … (Merge, Spam, Delete, Print, Products and expenses, Switch to previous look) y selector de estado Open/Pending/Resolved/Closed.

**UI/UX**

- **+** Lista densa pero legible, edición en línea y pestañas para trabajar varios tickets a la vez.
- **+** El tiempo facturable está siempre visible; la IA acompaña cada paso (sentimiento, respuesta, artículo, resumen).
- **−** Ticket ID y fecha de creación están ocultos por defecto.
- **−** Los íconos del riel derecho no tienen etiqueta, y Overview y Properties repiten campos.
- **−** Las acciones masivas aparecen deshabilitadas sin explicar por qué.

### Customers — `/new/customers`

**Para qué sirve:** ficha 360° de cada cliente del MSP que une PSA, RMM, documentación y facturación.

- **Lista:** botón dividido *New customer ▾* (Import customers), búsqueda, chips Country, Region (deshabilitado) y Rank. Columnas: logo, Name, Unmonitored devices, Phone, Address, Contact, Rank; teléfono, dirección y rango se editan en línea. La cuenta trae el cliente "Unassigned".
- **Ficha** (`/new/customer/{id}`): encabezado con logo, rango y metadatos (business ID, teléfono, dirección, web, dominios).

| Pestaña de la ficha | Contenido |
| --- | --- |
| Overview | Campos de sistema, campos personalizados y notas, editables en línea |
| Contacts | Contactos con badge de contacto principal, columna AI, cargo, correo, teléfonos, agentes asociados |
| Devices | El mismo componente de la página Devices, filtrado al cliente |
| Contracts | Contratos (nombre, inicio, fin, tipo, estado) |
| Assets | Inventario de activos con agrupación y altas masivas |
| Tickets | Tickets del cliente |
| Alerts | Alertas con Delete / Snooze / Resolve |
| Passwords | Bóveda de credenciales del cliente (nombre, URL, usuario, contraseña) |
| Attachments | Archivos adjuntos |
| Work from Home | Acceso remoto de los empleados a su PC de oficina |
| Invoices | Facturas del cliente |

**UI/UX**

- **+** Toda la relación con el cliente en un solo lugar, con estados vacíos consistentes (ilustración, una frase, botón rosa).
- **+** Bóveda de contraseñas por cliente integrada.
- **−** Once pestañas se parten en dos filas y la jerarquía se pierde.
- **−** Los campos editables en línea no muestran señal de edición; los "No phone number" grises parecen texto muerto.

### Devices — `/new/devices`

**Para qué sirve:** inventario de la flota y punto de partida de toda la gestión remota (núcleo del RMM).

- **Encabezado:** vistas guardadas, *+ New device ▾* (Agent installer, SNMP, TCP, HTTP, Generic) y exportar.
- **Filtro con IA:** campo "Describe what you want to filter" + botón *Ask*, además de chips Customers, Favorite y Filters.
- **Acciones masivas:** Run script, Assign automation profile, Software installation, Assign threshold profile.
- **Columnas:** Device, AI, Last login, Availability, Device type, Customer, Folder, Alerts (píldoras por severidad), Available patches, Pending reboot, Remote access; opcionales IP pública, nombre de máquina, último reinicio, edición y versión de SO, versión de agente.
- **Estado vacío:** fila "Sample Device" de ejemplo + "All your devices, under your care" y *+ New agent*.
- **Ficha del equipo** (`/new/rmm/device/{id}`):
  - Encabezado fijo: *Connect* (Splashtop, AnyDesk, TeamViewer, ScreenConnect), *Manage* (parches, inventario e instalación de software, scripts, Service Manager, Task Manager, apagado, CMD, PowerShell, actividad de usuario, apps, transferencia de archivos, Event Viewer, Registry Editor), Copilot y Create ticket.
  - Tarjetas reordenables: estado de alertas (con *Pause alerts*), estado de parches, pestañas Overview / Hardware / Disks (BitLocker) / OS and security / campos / contraseñas / adjuntos / equipos monitoreados, métricas CPU y memoria (24 h), alertas del equipo, perfiles asignados (threshold, automation, configuration policy), apagados programados y log de actividad.

**UI/UX**

- **+** Ficha muy completa: todas las herramientas remotas a un menú de distancia y varios proveedores de control remoto.
- **+** El equipo de muestra enseña el producto antes de instalar un agente; los secretos (claves BitLocker, serial de SO) aparecen enmascarados.
- **−** El contador dice "Displaying 1 of 0 devices" junto a la fila de muestra.
- **−** El menú *Manage* tiene tres niveles de anidación y la ficha es muy larga, con tarjetas de anchos mezclados.

### Alerts — `/new/alerts`

**Para qué sirve:** cola de alertas de monitoreo para revisar, posponer, resolver o convertir en ticket.

- Búsqueda, *Filters · 2*, *Save View*, vistas guardadas y ajustes de tabla.
- Acciones masivas: Delete, Snooze, Resolve.
- Columnas: Details, AI, Created (orden por defecto), Severity, Category, Ticket, Status, Manage device.
- Filtros: clientes, fecha, severidad, estado (Open activo), categoría, tipo de equipo, tipo SNMP (deshabilitado), rango de cliente, "solo alertas con ticket".

**UI/UX**

- **+** Comparte el mismo marco de tabla que Tickets y Devices.
- **−** El estado vacío dice "Try adjusting the filters" aunque no haya ningún equipo; debería llevar a instalar un agente.
- **−** *Save View* existe solo aquí, no en Tickets ni Devices.

### Patch Management — `/new/patch-management`

**Para qué sirve:** medir el cumplimiento de parches de SO y software por cliente, encontrar equipos vulnerables e instalar o reiniciar en lote.

- Pestañas **Overview | Devices | OS patches ▾** (este último es un desplegable con Available, Failed e Installed patches).
- **Overview:** chips de filtro (Customers, Folders deshabilitado, Device types, Availability · 2, Classifications), tarjeta "OS patching status" con % y anillos por Windows PC, Windows Server, Mac y Linux; KPIs de equipos sin parches críticos, sin parches de SO, con reinicio pendiente y sin actualizaciones de software; KPIs de parches fallidos y disponibles; tablas "Top vulnerable devices" y "Top missing OS patches".
- **Devices:** acción masiva Reboot; columnas Device, AI, Last login, Availability, Device type, Customer, Folder, Last patch scan, Available patches, Pending reboot, Software updates.
- **Available / Failed patches:** acción Install; columnas Name, Release date, OS type, Classification, Supported products, Size, CVEs, CVSS, Reboot required, Devices. **Installed:** añade "Uninstall supported".

**UI/UX**

- **+** KPIs + anillos por SO dan una lectura en segundos; CVE y CVSS orientan a seguridad.
- **+** Los filtros activos por defecto se ven ("• 2").
- **−** Las tres vistas de parches quedan escondidas en un desplegable dentro de la fila de pestañas, y ninguna pestaña tiene URL propia.
- **−** Encabezados truncados ("Devic…", "Cust…"); tono de los estados vacíos dispar ("Woohoo!" frente a "No patches found…").
- **−** Las políticas de parcheo están en Admin, no aquí.

### Asset Inventory (New) — `/new/asset-inventory`

**Para qué sirve:** CMDB ligera para registrar activos (incluso sin agente) y vincularlos a equipos monitoreados.

- Encabezado con explicación, *Manage assets ▾* (Asset types, Device type mapping, Asset warranty integration), botón dividido *+ New asset ▾* (Import assets) y exportar.
- Barra: búsqueda, Filters (panel lateral: tipo, carpeta, estado, compra, garantía, cliente, contacto; *Advanced filters* con candado), **Group by** (tipo, carpeta, estado), Edit/Delete masivos.
- Estado vacío con tres caminos: New asset, Create asset type, Import assets.
- Formulario de alta a página completa: tipo de activo (no se puede cambiar después), serial, estado ("In stock"), cliente, contacto, carpeta, fechas de compra y garantía, descripción.

**UI/UX**

- **+** Agrupar por y filtros en panel lateral encajan con un inventario; la creación automática de activos desde agentes es un buen concepto.
- **−** La configuración salta a Admin sin aviso; "You can add up to 0 asset types" convive con un botón de crear tipo; el candado de filtros avanzados no se explica.

### Network Discovery — `/new/networkdiscovery`

**Para qué sirve:** escaneo continuo de la red de cada cliente para hallar estaciones, servidores y equipos SNMP sin gestionar, y exposición a CVE.

- Encabezado con chip de prueba "31 days to try Network Discovery" y *Manage alerts* (lleva a Admin).
- Tabla por cliente: Customer Name, Public IP, Network, Workstations/Servers, SNMP, Scan Status. En Scan Status, menú *Install agent ▾* (descargar agente Windows, copiar script CLI, copiar enlace).

**UI/UX**

- **+** Tabla centrada en el cliente; la instalación aparece justo donde falta.
- **−** Sin onboarding ni estado vacío: una fila de celdas en blanco; "Displaying 1 customers".

### Reports › Classic Reports — `/new/reports`

**Para qué sirve:** catálogo de reportes operativos (tickets, monitoreo, técnicos, rentabilidad, satisfacción) y analíticos.

- La barra lateral cambia a navegación contextual de Reports (← Reports + búsqueda).
- Buscador grande y chips "Recently viewed".

| Grupo | Reportes |
| --- | --- |
| General | General, Load analysis, Top load, SLA, Recent processes |
| Monitoring | System at-a-glance, Customer health, Agent health, Auditor, Microsoft licensing, Patch search & deploy, Patch status summary, Patch & automation feedback, Software inventory |
| Technicians | Technician comparison, Technician performance |
| Profitability | Customer profitability, Top / Bottom retainer customers, Timesheet, Customer periodic, Products and expenses, Block contracts balance |
| Satisfaction | Satisfied customers, Dissatisfied customers |
| Analíticos (presets) | Customer tickets overview, Ticketing summary, Ticket alerts, Hardware inventory, Technician overview, Agents HW info, Software inventory, App Center installation, App Center usage, OS Overview, Windows agents support status, Assets management, AI Readiness |

- My reports, Shared reports, Favorites y **Data explorer** (datasets Asset management, Devices, Tickets) aparecen bajo un velo *Upgrade to Power*; al final, tres video-guías.
- **Visor nuevo** (ej. General report): pestañas Overview / Scheduling, filtro de periodo (Hoy … Custom), *Generate*, *Export ▾* y programación con destinatarios. Los visores antiguos quedaron en blanco.

**UI/UX**

- **+** Cada reporte trae una descripción de una línea; búsqueda y recientes ayudan; programar envíos es una pestaña de primer nivel.
- **−** Página muy cargada (catálogo + upsell + datasets + videos); tarjetas con scroll interno que esconden reportes.
- **−** Errores de texto: clave i18n cruda (`reports-menu.items-description.assets-management`) y "Your General report reports". El menú dice "Classic Reports" y la página "Reports".

### Reports › Advanced Reports — `/new/advanced-reports`

**Para qué sirve:** tableros de BI (presets y reportes propios) en el plan Power.

- Encabezado "← Analytical reports" y *New report*, que abre un paywall ("Upgrade to Atera's Power plan").
- Los presets cargan un tablero **Looker** embebido; durante el recorrido no terminó de cargar.

**UI/UX**

- **+** El BI vive dentro del shell, sin salir de la app.
- **−** Sin estado inicial (solo un spinner), paywall sin aviso previo en el botón y nombres distintos entre menú y página.

### Billing — `/new/billing`

No se pudo documentar: el contenido es una vista antigua dentro de un iframe y quedó en blanco, sin mensaje de error ni reintento. Por los menús de Admin y del ticket, aquí vivirían facturas, contratos y productos *(inferido)*.

### More › Knowledge Base — `/new/kb/center`

**Para qué sirve:** repositorio de artículos internos y para clientes (se publica en el Customer Portal).

- Dos paneles: a la izquierda Overview y categorías (General Information, FAQ, con "+" para crear); a la derecha "All articles".
- *+ New article*, búsqueda, chips Category, Section, Status, Keywords y selector de columnas.
- Columnas: Article, Status, Category, Section, Privacy, Views, Created on. Menú … con *Switch to old look*.

**UI/UX**

- **+** Limpio y con mucho aire.
- **−** Estado vacío "Nothing here" sin explicación ni botón; la tabla necesita scroll horizontal a 1300 px.

### More › Refer a Friend — `/new/refer`

**Para qué sirve:** programa de referidos (crecimiento).

- Hero "Turn referrals into rewards": el referido recibe 3 meses gratis de Network Discovery y quien refiere hasta USD 500 por referido.
- Formulario de correo para desbloquear el enlace y tarjeta de 3 pasos con premios escalonados (USD 250 → 350 → 500).

**UI/UX**

- **+** Jerarquía clara y explicación en tres pasos.
- **−** Pide el correo aunque el usuario ya está autenticado.

### AI Center (New) — `/new/ai`

**Para qué sirve:** centro de control del agente de IA "Robin": sugerencias, acciones, integraciones y fuentes de conocimiento.

El título funciona como selector desplegable entre cuatro secciones que comparten la misma URL.

- **Actionable insights:** KPIs Suggestions, Saved for later, Created; lista con filtro "Insight type". Vacío: Robin construye sugerencias a partir del historial de tickets.
- **Cloud actions:** solo un subtítulo; el cuerpo quedó en blanco.
- **Product integrations:** selector de cliente ("Unassigned"), pestañas Active, Discover y Custom (*Coming soon*). En Discover:
  - Proveedores de identidad (badge *Important*): Microsoft 365 & Entra, Google Workspace, Okta.
  - Integraciones: MxToolbox (conectada), Salesforce, Microsoft Intune, Atera browser connect, y conectores **MCP** de Monday, Asana, Intercom, Box, Notion, Atlassian y Yardi Virtuoso, más *Add custom MCP*.
  - Cada tarjeta abre un modal con las capacidades que gana Robin (en MxToolbox: MX Lookup, SPF Record Check, SMTP Troubleshoot, Global Blacklist Check).
- **Knowledge base (New):** *+ Add source* y tabla Source, "When should Robin use this source", Last edited, Active. Fuente activa: Atera Knowledge Base.

**UI/UX**

- **+** Cuadrícula de tarjetas consistente con etiquetas de color (IDP, Built-in, MCP, Custom MCP); el modal explica qué puede hacer la IA con cada integración. El soporte MCP es un diferenciador.
- **+** Enrutar fuentes con una instrucción "cuándo usar esta fuente" es un patrón muy claro para RAG.
- **−** Ninguna sección tiene URL propia; Cloud actions parece sin terminar.

### App Center — `/new/applications`

**Para qué sirve:** tienda de complementos de socios y de Atera (backup, seguridad, acceso remoto, red).

- Una tarjeta "Work smarter, not harder" con cuadrícula de 16 apps: Acronis, Axcient, Bitdefender, Vicarius, ThreatDown, Splashtop Premium, Splashtop SOS, Work From Home, Webroot, Emsisoft, Ironscales, Zomentum, Cynet, Network Discovery, Keeper, Domotz.
- Cada tarjeta: logo, categoría, descripción, número de instalaciones y duración de la prueba. Al pie, *Submit request* al Features Board.
- **Detalle de app:** *Get started*, *Request migration*, enlaces Support y Website, galería de capturas, pestañas Description y Prices.

**UI/UX**

- **+** Con 16 apps se recorre rápido; las instalaciones funcionan como prueba social.
- **−** Sin búsqueda ni filtro por categoría; imágenes rotas en el detalle.

### Admin — patrón de navegación

Al pulsar **Admin**, la barra principal se reemplaza por un panel oscuro con "← Admin", un buscador y 7 grupos en acordeón; el grupo de la página activa se abre y los demás se cierran. Es un buen patrón para más de 40 ajustes, pero tres cosas lo rompen: dos páginas `/new/rmm/` devuelven al usuario a la barra principal, Ticket settings y Contract expiration abren *Account settings* (cuya miga dice "My account"), y el buscador del panel no respondió en la prueba.

### Admin › My account › Account settings — `/new/admin/settings`

**Para qué sirve:** ajustes globales de la cuenta, de los agentes, de los tickets y de vencimientos.

- Pestañas **General | Agents | Tickets | Expirations**; cada una es una tarjeta con su propio *Save* (deshabilitado hasta que hay un cambio).
- **General:** nombre de empresa, país, estado, ciudad, dirección, código postal, zona horaria e idioma.
- **Agents:** protección contra desinstalación en Windows; marcar equipos como retirados tras N horas sin conexión.
- **Tickets:** cronómetro (automático, manual u oculto), spam automático, tiempo facturable y duración por defecto (00:15), redondeo (ninguno, ¼ h, ½ h, 1 h), tipo de respuesta por defecto (pública o nota interna), secuencia de ID, análisis de sentimiento, auto-etiquetado, nota de resolución obligatoria, aprobadores y qué estados se ven en el portal.
- **Expirations:** aviso por correo de contratos que vencen (Nunca, 1 semana, 2 semanas, 1 mes, 2 meses).

**UI/UX**

- **−** Columna central de \~600 px con márgenes vacíos; la pestaña Tickets es un muro de casillas sin índice ni ayuda por ajuste, y *Save* se pierde al hacer scroll.
- **−** Las pestañas no cambian la URL.

### Admin › My account › My profile — `/new/myprofile`

**Para qué sirve:** datos personales del técnico y preferencias.

- Tarjeta de identidad (correo, teléfono, fecha de alta, rol), Preferences (idioma y página de inicio, por defecto Devices) y firma personal de tickets con editor enriquecido completo.
- *Discard changes* y *Save* a nivel de página.

**UI/UX:** **+** tarjetas apiladas claras. **−** guarda a nivel de página, mientras Account settings guarda por tarjeta.

### Admin › My account › Subscription — `/new/admin/billing/subscription/in-trial-purchase`

**Para qué sirve:** elegir plan y pagar al final de la prueba.

- Stepper Select a plan → Add ons → Checkout; interruptor mensual/anual ("ahorra hasta 16%").

| Plan | Anual (USD por técnico/mes) | Mensual | Lo que añade |
| --- | --- | --- | --- |
| Pro | 129 | 139 | AI Copilot, RMM y alertas, Splashtop (2 sesiones), parches, software, KB, helpdesk, API, SLA, contratos y facturación, app móvil, audit log 1 mes |
| Growth (plan de la prueba) | 159 | 189 | Mac/Linux, Splashtop ilimitado, AnyDesk, 11 reportes, 5 tipos de activo, QuickBooks Online/Xero, audit log 6 meses |
| Power ("Most recommended") | 209 | 249 | Hasta 10 reportes propios, 20 tipos de activo, recuperación de datos, audit log 12 meses |
| Superpower | "Let's talk" | — | SSO, sincronización Azure AD, repositorio privado de software, SSL propio en el portal, Network Discovery, reportes ilimitados, HIPAA BAA, audit log 7 años, SLA 99,9% |

**UI/UX:** **+** tabla bueno-mejor-óptimo estándar con la cinta "Your trial". **−** los límites por plan no se avisan en las pantallas afectadas (Security no menciona SSO; Audit log no muestra su retención).

### Admin › Users and security › Technicians — `/new/admin/technicians`

**Para qué sirve:** alta y gestión de los técnicos (usuarios que pagan licencia).

- KPI "Active technicians", búsqueda e interruptor "Hide deactivated technicians".
- Columnas: Name, Email, Availability (desplegable en línea: Available / Out of office), Status, Role y menú … (Deactivate, Edit role).
- Modal *Add technician*: correo, rol (por defecto Beginner), nombre, apellido, teléfono.

**UI/UX:** **+** cambiar disponibilidad en línea. **−** una tarjeta KPI grande para un solo número.

### Admin › Users and security › Technician groups (New) — `/new/admin/technician-groups`

**Para qué sirve:** agrupar técnicos para asignar tickets, reportes y automatizaciones.

- Tabla Group name, Technicians, Description; estado vacío ilustrado con *Create new group*.
- Creación a página completa: nombre, descripción (500 caracteres), interruptor *Private* (solo los miembros ven sus tickets) y miembros.

**UI/UX:** **+** estado vacío amable y accionable. **−** dos botones de crear con nombre distinto.

### Admin › Users and security › Access roles — `/new/admin/roles`

**Para qué sirve:** control de acceso por roles (RBAC) y por cliente.

- Lista de roles (Admin, Beginner, Technician) con fechas de creación y modificación.
- Editor de rol con pestañas **Permissions** y **Customers access** (todos los clientes o solo algunos).
- Permissions en tres paneles: categorías con contador (habilitados/total), casillas con descripción, y miembros del rol.

| Categoría | Permisos | Admin | Technician | Beginner |
| --- | --- | --- | --- | --- |
| Full admin access | 1 | 1 | 0 | 0 |
| Remote management (conexión remota, scripts, terminal, Registry, Event Viewer, archivos…) | 15 | 15 | 15 | 0 |
| RMM administration | 4 | 4 | 2 | 0 |
| Ticketing | 7 | 7 | 4 | 0 |
| System (AI center, reportes, contraseñas, facturación, KB, móvil…) | 8 | 8 | 1 | 0 |

**UI/UX**

- **+** Permisos agrupados con contadores y un resumen al pasar el cursor; el alcance por cliente es clave para un MSP.
- **−** Panel central angosto con descripciones cortadas; *Save* se activó solo con navegar entre categorías, sin cambios reales.

### Admin › Users and security › Security and authentication — `/new/admin/security`

**Para qué sirve:** endurecer el acceso a la cuenta.

- Cuatro secciones en acordeón con su propio Save/Cancel:
  - **Access list:** todas las IP o solo algunas.
  - **Authentication:** 2FA por usuario con app autenticadora (tabla con su estado).
  - **MFA for protected actions** ("Recommended by Atera"): reautenticación cada 24 h para 11 acciones sensibles (sesión remota, ejecutar script, ver contraseñas, dar rol admin, crear técnicos, crear API keys, exportar datos sensibles…).
  - **Session timeout:** inactividad (60 min) y sesión máxima (7 días).

**UI/UX:** **+** revelado progresivo con resumen del valor actual en cada sección cerrada. **−** SSO (plan Superpower) no aparece ni como opción bloqueada.

### Admin › Users and security › Audit log — `/new/admin/history`

**Para qué sirve:** trazabilidad de quién hizo qué y cuándo.

- *Export*, búsqueda, panel *Filters* (orden, tipo: Created, Modified, Deleted, Login, Remote Actions, View Data; técnico; cantidad) y rango de fechas.
- Lista tipo feed: avatar, frase del evento, autor, píldora de acción y hora.

**UI/UX:** **+** feed fácil de leer. **−** muestra claves internas crudas (`admin.alerts.autoticketbyseverity`) y los eventos del sistema no tienen autor.

### Admin › Monitoring › Patch management and IT automation — `/new/rmm/itautomation/list`

**Para qué sirve:** perfiles programados de parches, software y mantenimiento, más exclusiones globales de parches.

- Pestañas **Profiles | Settings**; *+ Create profile* abre un modal que solo pide el nombre.
- Profiles: Name, Last modified, Active; vacío "Why work late when you can automate?".
- Settings: qué hacer con agentes offline (solo online, o encolar 1 h a 1 mes) y lista de KB excluidos.

**UI/UX:** **+** ajustes globales separados de los perfiles. **−** al abrirla se pierde el panel Admin; los controles de exclusión aparecen deshabilitados sin motivo.

### Admin › Monitoring › Configuration policies — `/new/admin/configuration-policies/list`

**Para qué sirve:** políticas de agente asignables por cliente, carpeta o agente.

- Formulario en dos pasos (Settings, Assignment):
  - **Windows restart:** cómo y cuándo reiniciar tras actualizar, y si Windows Update lo controla Atera o el equipo.
  - **Splashtop settings override (New):** pedir permiso al usuario y contraseña adicional.
  - **Assignment:** sub-pestañas Customers, Folders y Agents.

**UI/UX:** **+** navegación izquierda de dos pasos y radios activados por interruptor. **−** repite opciones de Remote access settings.

### Admin › Monitoring › Scripts — `/new/admin/scripts`

**Para qué sirve:** biblioteca privada de scripts y un mercado comunitario.

- Pestañas **Team scripts (0) | Shared library (1.124)**; botones *Create script* y *Upload script*.
- Team scripts: Script name, Category, Description, Type, **AI access**.
- Shared library: Author, Votes, Clones, Upload date; aviso "Scripts are scanned for malware, not for functionality".
- *Create script* redirigió a Devices con el aviso "Please upgrade your 'Basic' Login and 2FA settings": crear scripts exige 2FA.

**UI/UX:** **+** biblioteca comunitaria con votos y clones, y advertencia de seguridad visible. **−** el bloqueo por 2FA redirige a otra página sin explicar; categorías comunitarias en texto libre y en varios idiomas.

### Admin › Monitoring › Thresholds — `/new/rmm/threshold/list`

**Para qué sirve:** umbrales de métricas que generan alertas y, opcionalmente, ejecutan scripts de autocorrección.

- Lista: **Default Profile** y "None"; menú … (borrar, asignar).
- El perfil usa **reglas en lenguaje natural**, por ejemplo: `[Warning] Create warning alert for CPU Load exceeding 99%, for a 9 minutes period`.
- Perfil por defecto: 15 reglas en tres grupos.
  - Performance: CPU, memoria.
  - Hardware: temperatura de CPU y discos, ventiladores, uso de disco.
  - Exchange: almacenamiento, cola, ítems, tamaño de buzón.
- *Suggested presets*: Personal computer, Mac, Laptop, Linux, Server.
- *New item*, con tres modos:
  - **Presets:** eventos.
  - **Custom:** 29 categorías, entre ellas antivirus, batería, uptime, S.M.A.R.T., eventos, firewall, ancho de banda, procesos y servicios.
  - **Script based:** monitorear cualquier valor con un script.
  - Cada modo admite hasta 3 scripts de *auto-healing*.

**UI/UX:** **+** las reglas en frase son muy fáciles de leer; los presets bajan el esfuerzo y la autocorrección va en el mismo formulario. **−** "Apply preset" reemplaza todas las reglas con un clic; la barra se desborda a \~1300 px; unidades inconsistentes.

### Admin › Monitoring › Alert settings — `/new/admin/alerts`

**Para qué sirve:** cómo llegan las alertas: correo, sonido y creación automática de tickets.

- **Email alerts:** destinatarios, severidades, horario de envío y *Update all*, que reinicia los ajustes de todos los clientes.
- **Sound alerts:** un sonido por severidad.
- **Automated ticket creation:** por severidad y por cliente.
- Nota: las alertas exigen asignar antes un perfil de thresholds.

**UI/UX:** **+** secciones ordenadas con interruptor. **−** no hay botón Guardar ni señal de guardado; *Update all* es destructivo y parece un botón secundario cualquiera.

### Admin › Monitoring › SNMP templates — `/new/admin/snmptemplate/list`

**Para qué sirve:** plantillas reutilizables de monitoreo SNMP para switches, impresoras, NAS, etc.

- Pestañas **My SNMP templates | Shared template library (732)**; filtros Model y Tags.
- Columnas: Template name, Description, Type, Manufacturer, Models, Tags, Author, Votes y botón *Clone* fijo por fila.

**UI/UX:** **+** mismo patrón privado + comunidad que Scripts. **−** el CTA cambia de nombre ("Create template" / "New template") y los metadatos comunitarios vienen vacíos.

### Admin › Monitoring › Software management — `/new/admin/software-management`

**Para qué sirve:** instalar software en lote.

- Pestaña **Software bundles:** paquetes WinGet (Windows) y Homebrew (Mac).
- Pestaña **Software repository:** muro de pago, plan Enterprise, "Contact sales".

**UI/UX:** **−** la pestaña de pago no lleva candado; el texto de bundles menciona un "private repository" que también es de pago.

### Admin › Monitoring › Network Discovery alerts — `/new/admin/networkdiscovery-advanced-settings`

**Para qué sirve:** activar alertas derivadas del escaneo de red.

- Tabla editable en línea con 7 reglas:
  - Equipos o SNMP sin monitorear.
  - Cambio de IP del agente escáner.
  - Escáner offline.
  - CVE de severidad baja, media o alta.
- Cada regla define clientes asignados, severidad y si está activa.

**UI/UX:** **−** todas fijas en "Warning", incluso un CVE alto; la URL dice *advanced settings* y la pantalla *alerts*.

### Admin › Monitoring › Remote access settings — `/new/admin/remote-access-settings`

**Para qué sirve:** elegir la herramienta de control remoto por defecto y configurar cada una.

- Selector de herramienta por defecto y pestañas verticales por proveedor:
  - **Splashtop:** resumen de sesión con Copilot, permiso del usuario, contraseña adicional.
  - **AnyDesk:** desatendido o atendido.
  - **TeamViewer:** licencia propia.
  - **ScreenConnect:** URL, ID de instancia e instalador.

**UI/UX:** **+** maestro-detalle con logos e íconos de SO compatibles; el resumen de sesión con IA es diferencial. **−** Save/Cancel al pie de un panel alto, fuera de la vista.

### Admin › Support › Email settings — `/new/admin/emails-settings`

**Para qué sirve:** convertir correos en tickets, definir el remitente y autenticar dominios.

- Sub-navegación **Addresses & Domains | Advanced Settings**, y dentro pestañas **Addresses | Domains**.
- Addresses:
  - Dirección entrante autogenerada (con botón de copiar).
  - Remitente por defecto (noreply@atera.com).
  - Tabla de direcciones de soporte: Name, Email, Customers, Function, Created.
- Domains: SMTP propio o DKIM; columnas Protocol y Verification status.
- Advanced:
  - Nombre en las respuestas (técnico o personalizado), con **vista previa en vivo del encabezado del correo**.
  - Enviar respuestas por correo.
  - Crear contacto nuevo automáticamente.
  - Correo de bienvenida al contacto.

**UI/UX:** **+** vista previa del remitente y buen estado vacío en Domains. **−** tres niveles de navegación para un área; aquí sí hay *Save* explícito y en Alert settings no.

### Admin › Support › Ticket automation rules — `/new/admin/automationrules`

**Para qué sirve:** flujos automáticos de tickets por evento (tipo IFTTT) con orden de ejecución.

- Banner: las reglas corren en secuencia y por defecto solo la primera; *Always run* permite seguir.
- Tarjetas arrastrables con chips (Trigger, Conditions: n, Actions: n, Always run, Active). Al expandirlas se editan condiciones (campo, operador, valor) y acciones (acción, campo, plantilla, valor).
- 4 reglas por defecto:
  - Reabrir resueltos cuando el contacto responde.
  - Correo al crear ticket.
  - Auto-asignación.
  - Desasignar técnico deshabilitado.
- 8 disparadores:
  - Ticket creado.
  - Cambio de estado.
  - Cerrado o resuelto.
  - Cambio de asignación.
  - Calificación baja.
  - Respuesta del contacto.
  - Cambio de grupo.
  - Cambio de aprobación.

**UI/UX:** **+** los chips resumen cada regla de un vistazo y arrastrar para ordenar refleja la lógica secuencial. **−** "solo corre la primera regla" es contraintuitivo; el subtítulo promete reglas por tiempo que no aparecen entre los disparadores.

### Admin › Support › Ticket settings — `/new/admin/settings?tab=tickets`

Es la pestaña Tickets de *Account settings* (ver arriba). El menú la ubica en Support and ticketing, pero la miga dice "My account".

### Admin › Support › Ticket forms — `/new/admin/forms`

**Para qué sirve:** formularios de ticket por cliente, construidos con campos personalizados.

- Lista con filtros Customers y Status (Active preaplicado).
- Constructor **arrastrar y soltar**: lienzo con campos obligatorios (técnico, tipo, contrato, prioridad, impacto), paleta de campos disponibles a la derecha y asignación por segmento de clientes (New).

**UI/UX:** **+** constructor visual con paleta separada. **−** el filtro Active oculta formularios desactivados sin avisar; "Create new field" saca al usuario a otra página.

### Admin › Support › Email templates — `/new/admin/emailtemplates`

**Para qué sirve:** contenido de los correos automáticos.

- 4 plantillas: Customer Portal Credentials, New Contact Form, New Ticket Created, Support Request Resolved.
- Editor: nombre, descripción, asunto y cuerpo enriquecido.
- *Snippets* de variables como **chips de color por entidad**: ticket en azul, contacto en morado, técnico en rosa, portal en verde azulado, cliente en gris.

**UI/UX:** **+** los chips de variables por color son una gran ayuda. **−** la sintaxis `{[ ]}` es poco común; Save solo arriba en una página larga.

### Admin › Support › Quick Reply templates — `/new/admin/quickreplytemplates`

**Para qué sirve:** respuestas predefinidas para los técnicos.

- Lista y editor enriquecido con las mismas variables de color (aquí llamadas *Custom Variables*).

**UI/UX:** **−** estado vacío sin botón y con gramática rota; el mismo componente tiene dos nombres.

### Admin › Support › Calendar integration — `/new/admin/calendar-integration`

**Para qué sirve:** sincronizar el calendario para agendar trabajo o visitas desde tickets.

- Dos tarjetas: Outlook 365 y Google Calendar, cada una con *Connect*.

**UI/UX:** **−** no muestra estado ni qué se sincroniza; mucho espacio vacío.

### Admin › Support › WhatsApp settings (New) — `/new/admin/whatsapp-config`

**Para qué sirve:** notificar y conversar con usuarios finales por WhatsApp Business, usando las credenciales propias de Meta del cliente.

- Interruptor *Connect WhatsApp Business*.
- 7 campos de Meta: número, IDs del número, de la cuenta, del negocio y de la app; app secret; token. Los secretos van enmascarados.
- Eventos que notifican: ticket creado, comentario nuevo, cambio de estado, cambio de asignación.
- Botón fijo *Update configuration*.

**UI/UX:** **+** agrupación clara y notificaciones bloqueadas hasta conectar. **−** configuración 100% manual, sin asistente de Meta; el botón flotante de IA tapa el botón de guardar.

### Admin › Customer service › Customer Portal — `/new/admin/portal`

**Para qué sirve:** activar el portal de autoservicio (crear y seguir tickets, leer la KB) y definir su URL.

- Sub-pestañas verticales:
  - **Domain:** subdominio de Atera `*.servicedesk.atera.com` o dominio propio, con *Update* y copiar.
  - **Ticketing** y **Knowledge Base:** cada una con interruptor y lista de clientes; vacía significa "todos".

**UI/UX:** **+** enfocado y con un patrón reutilizado. **−** la regla "vacío = todos" es fácil de malinterpretar; no hay vista previa ni botón para abrir el portal; dos modelos de guardado.

### Admin › Customer service › White label — `/new/admin/whitelabel`

**Para qué sirve:** marca propia del MSP en el portal, el ícono del agente y los PDF.

- Pestañas **Logo** (logo del portal, 200×100), **Company Icon** (ICO 16×16 para el Helpdesk Agent y la pestaña del portal) y **Reports & Invoices**.
- Cada pestaña trae una **vista previa en contexto**: barra de tareas de Windows, pestaña del navegador, encabezado de reporte.

**UI/UX:** **+** las vistas previas en contexto. **−** alcance limitado: no hay colores de marca, CSS ni marca en correos; las vistas previas usan fechas de 2020.

### Admin › Data management › Custom fields — `/new/admin/dynamicfields`

**Para qué sirve:** extender el modelo de datos.

- Riel de 10 entidades con contador: Ticket, Customer, Contact, Contract, SLA, Agent, SNMP, TCP, HTTP, Generic. La pestaña activa queda en la URL.
- Campos por defecto (Status, Priority, Impact, Type) y campos propios reordenables arrastrando.
- *Add custom field* abre un panel lateral con tipos Text, Number, Date, Checkbox, Dropdown y Dependent dropdown, y la opción de hacerlo obligatorio.

**UI/UX:** **+** patrón moderno: riel con contadores, arrastrar para ordenar y panel lateral que no saca de contexto. **−** mezcla entidades de negocio con tipos de monitor; convive un *Switch to old UI*.

### Admin › Data management › Custom assets — `/new/admin/custom-asset-types/list`

**Para qué sirve:** tipos y campos de activos, y mapeo automático de equipos con agente a activos.

- Pestañas **Asset types | Asset fields | Type mapping (Beta)**.
- Asset fields: 8 campos de sistema bloqueados.
- Type mapping: tipo de equipo → tipo de activo, con explicación ilustrada de la creación automática por número de serie.

**UI/UX:** **−** "You can add up to 0 asset types" sin explicar ni ofrecer ascenso de plan; contradice que Workstation y Server existan por defecto; \~15 s de carga.

### Admin › Data management › API — `/new/admin/api`

**Para qué sirve:** tokens de API con restricción por IP y vencimiento.

- Columnas: Token name, IP Addresses, Expiration, Status.
- *API documentation* y *New token* aparecen deshabilitados en la prueba; solo un tooltip lo explica.

**UI/UX:** **−** bloqueo por plan solo visible al pasar el cursor; tabla vacía sin estado vacío, parece rota.

### Admin › Data management › Import data — `/new/admin/import`

**Para qué sirve:** carga masiva por CSV.

- Pestañas **Customers | Contacts | Assets**.
- Pasos numerados: descargar plantilla, subir archivo, *Import*.
- Notas de dependencia: el cliente debe existir antes que sus contactos, y el tipo de activo antes que los activos.

**UI/UX:** **+** pasos numerados que se habilitan en orden. **−** no hay mapeo de columnas, vista previa ni historial de importaciones.

### Admin › Data management › Integrations (New) — `/new/admin/integrations`

**Para qué sirve:** conectores fuera del App Center.

- **Zapier:** el panel quedó vacío.
- **Asset Warranty:** Dell ya conectado; Lenovo con *Connect*. Rellena automáticamente fechas de compra y garantía.

**UI/UX:** **−** las integraciones viven en tres lugares (App Center, AI Center, Admin), lo que dispersa el descubrimiento.

### Admin › Business administration › Contracts — `/new/admin/contracts`

**Para qué sirve:** acuerdos de facturación por cliente que alimentan la facturación.

- Columnas: Contract name, Customer, SLA policy, Start date, End date, Status.
- Filtros: tipo de contrato (Retainer/Flat Fee, Block Hours, Hourly, Remote Monitoring, Block Money), activos, gravables, por defecto.
- Formulario a página completa:
  - Detalles con fechas por defecto: hoy y +3 años.
  - Plan y precio: tipo, tarifas, cantidad, periodo.
  - Notas.

**UI/UX:** **+** estado vacío que vende el valor y fechas por defecto inteligentes.

### Admin › Business administration › SLAs — `/new/admin/sla`

**Para qué sirve:** tiempos objetivo de primera respuesta y cierre.

- El subtítulo explica la regla: se aplica la primera política cuyas condiciones coinciden, y se reordenan arrastrando.
- Formulario:
  - Nombre, descripción y calendario de horario laboral.
  - Condiciones: todos los tickets o filtrados.
  - Matriz prioridad (Critical, High, Medium, Low) × primera respuesta × cierre, con unidades.

**UI/UX:** **+** la matriz se lee de un vistazo y la regla de prioridad está explicada en pantalla. **−** no hay botón de crear en el encabezado, solo en el estado vacío; no tiene Cancel.

### Admin › Business administration › Business hours — `/new/admin/business-hours`

**Para qué sirve:** calendarios laborales que usan los SLA.

- "Default Calendar": lunes a viernes de 8:30 a 17:30.
- Pestañas **Work schedule** (personalizado o 24/7, horas por día y "Copy to all selected days") y **Holidays**.

**UI/UX:** **+** cuadrícula semanal clara con total de horas por día. **−** la lista no tiene acciones por fila.

### Admin › Business administration › Accounting y Products and expenses

`/new/admin/accounting` y `/new/admin/productsandexpenses` no cargaron: son vistas antiguas en iframe. Probablemente alojan la integración contable (QuickBooks, Xero) y el catálogo de productos y gastos *(inferido, por el plan Growth y el menú del ticket)*.

### Admin › Business administration › Contract rates — `/new/admin/rates`

**Para qué sirve:** catálogo de tarifas facturables.

- Columnas: Description, Amount, Status. Viene con "Default Rate" de USD 100.
- Menú por fila: Relations (dónde se usa), Archive, Delete.
- Modal de alta: descripción, SKU, monto.

**UI/UX:** **+** *Relations* muestra el impacto antes de cambiar. **−** el menú dice "Contract rates" y la página "Rates".

### Admin › Business administration › Contract expiration — `/new/admin/settings?tab=expirations`

Es la pestaña Expirations de *Account settings*: un solo ajuste de aviso por correo. El menú la pone en Business administration y la miga dice "My account".

### Admin › Business administration › Taxes — `/new/admin/taxlist`

**Para qué sirve:** tasas de impuesto y su asignación a mano de obra, productos y gastos.

- Pestañas con contador: **Tax Types | Products/Services**.
- Modales para agregar un tipo de impuesto (nombre y %) y para asignarlo.

**UI/UX:** **+** contadores en las pestañas. **−** tres verbos distintos para crear (Add, Create, Assign) y un estado vacío en una pestaña pero no en la otra.

### Admin › App Center settings — `/new/applications/settings`

Apps instaladas. Está vacía, con un buen estado vacío y el botón *Explore the App Center*.

## Patrones UI/UX transversales

Atera tiene un sistema de diseño reconocible y bien resuelto en las tablas y en los estados vacíos. Su mayor deuda es la consistencia: conviven dos generaciones de UI, seis maneras de guardar y una arquitectura de información que a veces saca al usuario de su contexto.

| Patrón | Dónde se ve | Evaluación |
| --- | --- | --- |
| Kit de tabla único: búsqueda, chips de filtro con contador ("Filters · 2"), panel lateral de filtros, vistas guardadas, ajustes (densidad, columnas, ajustar ancho, reset, navegación por teclado), exportar, "Displaying x of y", acciones masivas | Tickets, Devices, Alerts, Patch, Assets, Contracts | Fuerte: se aprende una vez y sirve en todo el producto |
| Edición en línea en celdas y campos | Tickets, Customers, Technicians | Eficiente, pero sin señal visual de que el campo es editable |
| Estados vacíos ilustrados (mascotas robot y mecánico) con frase de valor + CTA rosa | Mayoría de pantallas nuevas | Bien cuando trae CTA; hay al menos cuatro estilos de ilustración y varios vacíos sin botón o sin texto |
| IA en todas las capas: Copilot flotante, columna AI en tablas, Copilot en fichas, filtro en lenguaje natural, respuesta, artículo y sentimiento en tickets, resumen de sesión remota, "AI access" en scripts | Global | Diferenciador principal; la IA está en el flujo de trabajo, no en un módulo aparte |
| Biblioteca privada + comunidad con votos y clones | Scripts (1.124), SNMP templates (732) | Diferenciador; ahorra configuración, aunque la taxonomía comunitaria es ruidosa |
| Reglas en lenguaje natural con tokens de color | Thresholds | Muy legible; buen modelo para cualquier motor de reglas |
| Variables como chips de color por entidad | Email y Quick reply templates | Reduce errores de sintaxis |
| Panel Admin en acordeón con buscador | Admin | Escala bien a 41 páginas; lo rompen las rutas `/new/rmm/` y Account settings |
| UI híbrida: shell nuevo + vistas AngularJS en iframe y opciones "Switch to old look" | Billing, Accounting, Products and expenses, reportes clásicos, KB, Custom fields, Email templates, Automation rules | Las vistas antiguas fallan en silencio (página en blanco, sin error ni reintento) |
| Guardado: automático, Save en tarjeta, Save al pie, barra fija, Save/Cancel en encabezado, Update | Alert settings, Account settings, Remote access, WhatsApp, Thresholds, Portal | Inconsistente; el usuario no sabe cuándo quedó guardado |
| Pestañas sin URL propia | Patch Management, AI Center, Account settings, Portal, White label, Import, Taxes | No se puede compartir ni volver a una vista exacta (Custom fields sí usa `?tab=`) |
| Venta adicional: banner de prueba intermitente, chips de días, velos "Upgrade to Power", paywalls al hacer clic, botones deshabilitados con tooltip, "up to 0" | Global | Mezcla de avisos ruidosos y bloqueos mudos; los límites no se anuncian antes del clic |
| Badges New, Beta, Coming soon, Recommended, Important | Navegación y tarjetas | Dan pistas de novedad, pero "Coming soon" dentro de la búsqueda es ruido |
| Calidad de texto | Reports, Network Discovery, Rates, Audit log | Clave i18n cruda, "report reports", "1 customers", nombres de menú distintos al título de la página, claves internas en el log |
| Título de pestaña del navegador idéntico en todas las páginas | Global | Dificulta el historial y el trabajo con varias pestañas |

Paleta: rosa/magenta para la acción primaria, azul para enlaces e información, píldoras de estado amarillo, naranja y rojo, barra lateral negra y contenido en tarjetas blancas sobre fondo gris claro.

## Benchmark para la IT suite propia

La recomendación es copiar la estructura funcional de Atera (ficha de equipo, kit de tabla, reglas en lenguaje natural, IA dentro del flujo) y competir en consistencia: un solo modelo de guardado, todo enlazable por URL y bloqueos por plan anunciados antes del clic. Estas son recomendaciones de criterio a partir de lo observado, no datos medidos.

| Área | Tomar de Atera | Hacerlo mejor |
| --- | --- | --- |
| Navegación | Barra operativa corta + panel Admin en acordeón con buscador | Que ninguna página de configuración saque al usuario del panel; que miga, menú y título usen el mismo nombre; título de pestaña único por página |
| Tablas | Un solo componente con filtros con contador, vistas guardadas, densidad, columnas, acciones masivas, edición en línea | *Save view* en todas las tablas; explicar por qué una acción está deshabilitada; señal visual de celda editable |
| Ficha de equipo | Encabezado fijo con Connect y Manage, tarjetas de alertas, parches, hardware, discos, seguridad, métricas y perfiles asignados | Menú de acciones de máximo dos niveles; navegación interna por anclas en una ficha tan larga |
| Monitoreo | Reglas de umbral escritas como frase, presets por tipo de equipo, scripts de auto-healing en el mismo formulario | Confirmación y deshacer al aplicar un preset; severidad editable en todas las reglas; unidades normalizadas |
| Automatización de tickets | Tarjetas con chips de trigger, condiciones y acciones; orden por arrastre | Ejecutar todas las reglas por defecto con "detener aquí" opcional (en lugar de solo la primera); disparadores por tiempo |
| Scripts y plantillas | Biblioteca privada + comunitaria con votos, clones y aviso de escaneo | Curaduría (verificados, categorías cerradas); explicar en pantalla requisitos como 2FA en vez de redirigir |
| IA | Copilot global con "/" para citar objetos; IA en tablas, tickets y sesiones remotas; fuentes de conocimiento con "cuándo usar"; conectores MCP | Cada sección de IA con URL propia; mostrar qué datos usa la IA y con qué permisos (Atera lo hace en el modal de integraciones) |
| Seguridad | RBAC por categorías con contadores + alcance por cliente; MFA para acciones protegidas; IP allowlist; timeouts | Evitar falsos "cambios sin guardar"; registro de auditoría en lenguaje humano, no claves internas |
| Guardado | — | Un único patrón (por ejemplo, barra fija "Cambios sin guardar · Descartar · Guardar") en todo el producto |
| Estados vacíos | Ilustración + frase de valor + CTA | Que cada vacío diga la causa real ("instala tu primer agente" en lugar de "ajusta los filtros") |
| Monetización | Tabla de planes clara con "tu plan actual" | Candado y plan requerido visibles antes del clic; nada de "up to 0" ni botones mudos |
| Arquitectura | — | No mezclar UI antigua en iframe; si existe, mostrar error y reintento en lugar de una página en blanco |
| Integraciones | Tarjetas con capacidades declaradas y estado conectado | Un solo catálogo (Atera las reparte entre App Center, AI Center y Admin), con búsqueda y categorías |

Siguiente paso sugerido: convertir las filas "Hacerlo mejor" en requisitos de UX del spec de la suite (navegación, kit de tabla, guardado, estados vacíos) antes de empezar los módulos.
