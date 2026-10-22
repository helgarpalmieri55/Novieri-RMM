/**
 * GLOSSARY: one place to change a term (spec §6). Every Spanish catalogue
 * uses these choices:
 *   device → dispositivo     enroll → inscribir     agent → agente
 *   push install → instalación remota     tenant/organization → organización
 *   job → tarea     ticket → ticket     rollout → despliegue
 *   approve → aprobar     review → revisión     subnet → subred
 *   computer → equipo     (a machine on the list, "Add a computer"; "device" stays dispositivo)
 *   manage → administrar     alert → alerta     patch → parche
 *   backup → copia de seguridad     credential → credencial
 *   sweep → barrido     audit log → registro de auditoría
 *   service desk → mesa de ayuda     settings → configuración
 *   online/offline → en línea/sin conexión     healthy → en buen estado
 *   enrolled → inscrito     pending → pendiente     failed → fallido
 *   probe → sonda     tile (dashboard widget) → mosaico     fingerprint → huella
 *   hash chain → cadena de hashes     heartbeat → latido     vault → bóveda
 *   triage (a ticket or error group) → clasificar / triaje     reopen → reabrir
 *   check out (a credential) → retirar     checked out → retirada     secret → secreto
 *   withdraw (a queued job) → retirar     cancel → cancelar     retry → reintentar
 *   token (enrollment) → token     certificate → certificado     revoke → revocar
 *   endpoint (a machine) → equipo     asset → activo     range (addresses) → rango
 *   group (of devices) → grupo     precedence → precedencia     membership → pertenencia
 *   lifecycle (of an asset) → ciclo de vida
 *   rule-driven / hand-picked (group) → por reglas / elegidos a mano
 *   release (of the agent) → versión     channel (stable / pilot) → canal (estable / piloto)
 *   ring (of a rollout) → anillo     publish → publicar     install link → enlace de instalación
 *   site server → servidor de sitio     vouch for (an asset) → avalar
 *   host key → clave de host     probe (verb) → sondear     target (of a push) → destino
 *   bind / unbind (a credential scope) → vincular / desvincular     scope → ámbito
 *   claim (a discovered machine) → reclamar
 *   add → añadir (never agregar)     wake (Wake-on-LAN) → encender / encendido     estate (the fleet of machines) → parque
 *   segment (of a network) → segmento     enabled / disabled (a server) → habilitado / deshabilitado
 *   update (a patch) → actualización     severity → gravedad     outstanding → pendiente
 *   restart / reboot → reinicio     maintenance window → ventana de mantenimiento
 *   scan (a fleet scan for updates) → escaneo / escanear     apply (install updates) → instalar
 *   imaging (disk imaging) → imágenes de disco     firewall → cortafuegos     software / hardware / CPU stay as the loanwords Spanish uses
 *   attended / unattended (a remote session) → asistida / desatendida     end (a session) → finalizar     recording → grabación     playback → reproducción
 *   deployment (of software) → despliegue     package → paquete     installer → instalador     interpreter → intérprete
 *   schedule (a script on a timetable) → programación     script stays script (a loanword)
 *   acknowledge (an alert) → reconocer     mute → silenciar     check (a health check) → comprobación     health profile → perfil de estado
 *   remediation / fix → corrección     repeating (a flapping alert) → se repite     threshold → umbral     breaching → incumpliendo
 *   approved action → acción aprobada     exemption → exención     health (as a subject) → salud
 *   relay → servidor de retransmisión     start (a remote session) → iniciar sesión remota (never bare "iniciar sesión", which means sign in)
 *   drift (the plan and the fleet disagree about an address) → deriva     finding (one drift result) → hallazgo     dormant (address) → en reposo
 *   orphan (an address inside no registered subnet) → huérfana     claimant (who holds an address) → titular     registry (VRFs, sites, VLANs) → catálogo
 *   scan authorization → autorización de escaneo     DHCP pool → pool DHCP     digest (of an import file) → hash     dry run → simulación
 *   derived status / documented state (an address) → situación (derivada) / estado (documentado): two words, never one under the other's heading
 *   provision (a machine) → aprovisionar     run (of a provisioning build) → ejecución     build (a machine from an image) → construcción / construir
 *   answer file → archivo de respuestas     image (OS) → imagen
 *   backup point → punto de copia     restore (files) → restaurar / restauración     repository → repositorio     coverage → cobertura
 *   off-site (copy / storage) → externa / externo     recovery key → clave de recuperación     bucket (S3) stays bucket
 *   DR test → prueba de recuperación     failover → conmutación por error     tabletop (exercise) → ejercicio de mesa     RTO / RPO stay acronyms
 *   evidence snapshot → instantánea de evidencia     step (of a test) → paso     n/a → n/d
 *   queue → cola     requester → solicitante     assignee (who owns a ticket) → responsable     priority → prioridad (baja / normal / alta / urgente)
 *   incident → incidente     request (a ticket type) → solicitud     category (of a ticket) → categoría     saved reply → respuesta guardada
 *   internal note → nota interna     public reply → respuesta pública     copilot → copiloto     support mailbox → buzón de soporte
 *   SLA stays SLA ("Sin SLA")     first response → primera respuesta     resolve (a ticket) → resolver     breached (an SLA) → incumplido     met → cumplido
 *   overdue (a ticket past its deadline) → con retraso     fulfilled (a request) → completado     in progress → en curso     filed / raised (a ticket) → crear / creado
 *   portal (end users) → portal     update (a staff note on a requester's ticket) → novedad     approval (outcome of a request) → aprobada / rechazada / pendiente
 *   urgency (how soon the requester needs help) → urgencia     attachment → archivo adjunto
 *   business calendar (SLA) → calendario laboral     business / working hours → horario laboral     working window → franja laboral     holiday → festivo
 *   policy (SLA) → política     target (SLA response / resolution) → objetivo     deadline → plazo     breach (noun) → incumplimiento     promise (an SLA's) → compromiso
 *   voided (a promise) → anulado     timezone → zona horaria     clock (SLA) → reloj     running (an SLA clock) → en marcha     service levels → niveles de servicio
 *   mailbox (any, not only support) → buzón     quarantine / quarantined (held mail) → cuarentena / en cuarentena     disposition (what became of a message) → resultado     poll (a mailbox) → comprobar
 *   inbound message → mensaje entrante     sender → remitente     message log → registro de mensajes     return address (OAuth redirect) → dirección de retorno     tenant (Azure directory) → inquilino     client secret → secreto de cliente
 *   notification channel (webhook / mail server) → canal     rule (notification routing) → regla     route (an event to a channel) → enrutar     delivery (one attempt to reach a recipient) → entrega
 *   recipient → destinatario     allowlist → lista de permitidos     gave up (a delivery, retries exhausted) → abandonada     event → evento     bearer token → token de portador     webhook stays webhook
 *   role (of a user) → rol     permission → permiso     directory (of users) → directorio     invitation (a new account's enrolment mail) → invitación (never "inscripción", which is for devices)
 *   retention → retención     brand / branding → marca     API key → clave de API     identity provider → proveedor de identidad     mail server → servidor de correo
 *   effective permissions → permisos efectivos     tenant (the platform's own word for an organization) → organización, never inquilino (inquilino is only the Azure directory)
 *   person (someone the console tracks) → persona     link (a seat, licence or device to a person) → vincular     seat (of a subscription) → puesto     licence → licencia
 *   starter / leaver (the two checklists) → nueva incorporación / salida     checklist → lista de verificación     step (of a checklist) → paso     skip → omitir     mark done → marcar como hecho
 *   release (give a seat or licence back to the pool) → liberar     take back → recuperar
 *   article (a written runbook) → artículo     document (a stored file) → documento     record (a network, service, vendor or location entry) → registro     runbook → manual de procedimiento
 *   archive (take out of the library) → archivar     purge (destroy a stored version's bytes) → purgar / destruir los bytes     upload → subir     download → descargar     history (of versions) → historial
 *   unlink → desvincular     links (of a document) → vínculos     target id → ID de destino     sanitiser's "link removed" → vínculo eliminado
 *   report (a built-in query over the estate) → informe     export (a report as CSV) → exportar     evidence (compliance evidence) → evidencias     run (a report) → ejecutar     totals (of a report) → totales     figure (a chart) → gráfico
 *   estate posture (the fleet-posture report) → postura del parque     attainment (SLA) → cumplimiento     compliance (patch) → cumplimiento     report template → plantilla de informe
 *   entitlement (what a licence allows) → derecho de uso     entitled → autorizado     vendor → proveedor     publisher (of software) → fabricante     cost → costo (never coste)
 *   map (an installed software title to a product) → asociar     over-deployed → excedido     compliant → conforme     allocation (of a licence to a device or person) → asignación     reconcile → conciliar
 *   owner (of a subscription) → responsable     seat holder → titular de un puesto     idle (seat) → sin asignar     spend → gasto     run rate → gasto recurrente     notice period / deadline (to cancel) → plazo de aviso     renewal → renovación
 *   period → periodo (never período)
 *   budget → presupuesto     expense → gasto     cost center → centro de costo     FX rate → tipo de cambio     base currency → moneda base     fiscal year → año fiscal     variance → variación
 *   planned / actual / committed / projected → planificado / real / comprometido / proyectado     void (an expense) → anular / anulado     position (budget) → posición     unconvertible (expense) → no convertible
 *   expected (an expense) → previsto     amount → importe     retired → retirado / retirada     granularity → granularidad
 *   change (a managed change to the estate) → cambio     change request → solicitud de cambio (feminine)     approver → aprobador     retrospective (an emergency change's approval) → retroactiva
 *   risk → riesgo (bajo / medio / alto)     rollback plan → plan de reversión     maintenance window → ventana de mantenimiento     lifecycle (of a change) → ciclo de vida
 *   change gate (patches or deployments that need an approved change) → requisito de cambio aprobado     revise (a rejected change) → corregir     emergency (change type) → emergencia     standard (change type) → estándar
 *   persona (the kind of user the assistant answers) → tipo de usuario (never "perfil", which is a user's profile or a provisioning profile)     autonomy ceiling → límite de autonomía     dial position: off / suggest only / needs approval → desactivado / solo sugerir / requiere aprobación
 *   proposal (an action the assistant prepared) → acción propuesta     decline (a proposal) → rechazar     preview (of an action) → vista previa     rationale → motivo
 *   model provider (the AI service the assistant calls) → proveedor de modelos     self-hosted → autoalojado     egress (what leaves the network) → lo que sale de la red     prompt stays prompt (a loanword)
 *   token (a unit of model output) → token     tool (an assistant's tool call) → herramienta     turn (one question and its answer) → turno     flagged content → contenido marcado     injection (hostile text addressed to the assistant) → inyección
 *   excerpt (of flagged text) → fragmento     control plane → plano de control
 *   audit log labels (auditVocabulary.ts) → the event as it happened ("Tarea despachada", "Equipo inscrito"), the participle agreeing with the noun the code is about
 *   job succeeded (a finished job) → completada correctamente     seeded (default rows the platform creates) → predeterminado / predeterminada     mirror (accounts into people) → sincronizar
 *   demote (a site server back to a plain device) → revertir a dispositivo     passkey → clave de acceso     materialise (expected expenses) → generar     remediation refused → rechazada
 *   AI app (an app connected over MCP) → aplicación de IA (feminine: revocada)     connection → conexión     connector (in the AI app) → conector     MCP stays MCP
 *   seeded roles (SeededRole) → Superadministrador / Administrador de la organización / Técnico / Auditor / Solo lectura / Solicitante; a role an operator made keeps the name they gave it
 *   "autorizado" agrees with its noun: licencias autorizadas, derechos de uso autorizados
 *   Form of address: tú, with impersonal wording where it reads as well
 *   ("Tu perfil", "Pide acceso a un administrador").
 */
import type { Shape } from "../types";
import type { Catalog } from "../en";
import { common } from "./common";
import { components } from "./components";
import { enums } from "./enums";
import { lib } from "./lib";
import { setup } from "./setup";
import { onboarding } from "./onboarding";
import { profile } from "./profile";
import { platformStatus } from "./platformStatus";
import { errorMonitor } from "./errorMonitor";
import { audit } from "./audit";
import { dashboard } from "./dashboard";
import { shell } from "./shell";
import { time } from "./time";
import { validation } from "./validation";
import { credentials } from "./credentials";
import { jobs } from "./jobs";
import { enrollment } from "./enrollment";
import { devices } from "./devices";
import { agentPush } from "./agentPush";
import { agentReleases } from "./agentReleases";
import { siteServers } from "./siteServers";
import { patch } from "./patch";
import { inventory } from "./inventory";
import { remote } from "./remote";
import { deploy } from "./deploy";
import { health } from "./health";
import { ipam } from "./ipam";
import { provisioning } from "./provisioning";
import { backup } from "./backup";
import { bcp } from "./bcp";
import { serviceDesk } from "./serviceDesk";
import { portal } from "./portal";
import { sla } from "./sla";
import { emailIntake } from "./emailIntake";
import { notification } from "./notification";
import { security } from "./security";
import { settings } from "./settings";
import { people } from "./people";
import { docs } from "./docs";
import { reporting } from "./reporting";
import { licenses } from "./licenses";
import { subscriptions } from "./subscriptions";
import { budget } from "./budget";
import { change } from "./change";
import { aiAutomation } from "./aiAutomation";
import { aiAdmin } from "./aiAdmin";

export const es: Shape<Catalog> = { common, components, setup, onboarding, profile, platformStatus, errorMonitor, audit, dashboard, shell, time, enums, lib, validation, credentials, jobs, enrollment, devices, agentPush, agentReleases, siteServers, patch, inventory, remote, deploy, health, ipam, provisioning, backup, bcp, serviceDesk, portal, sla, emailIntake, notification, security, settings, people, docs, reporting, licenses, subscriptions, budget, change, aiAutomation, aiAdmin };
