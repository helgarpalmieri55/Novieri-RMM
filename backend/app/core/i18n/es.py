"""English template → Spanish template. Keys must match tr() literals exactly;
tests/test_i18n_catalog.py fails on a missing, mismatched or stale entry."""

CATALOG: dict[str, str] = {
    # --- core ---
    "no ai action {ai_action_id}": "no existe la acción de IA {ai_action_id}",
    "confirming an AI action requires ai.act.confirm": (
        "para confirmar una acción de IA se necesita ai.act.confirm"
    ),
    "this action is {state}, not awaiting confirmation. A second confirm is a conflict by the "
    "state machine, not an idempotent repeat": (
        "esta acción está {state} y no espera confirmación. Una segunda confirmación es un "
        "conflicto de la máquina de estados, no una repetición idempotente"
    ),
    "the preview expired before it was confirmed; ask again and confirm the new preview": (
        "la vista previa caducó antes de confirmarse; pídela de nuevo y confirma la nueva "
        "vista previa"
    ),
    "the approved action this preview was built from no longer exists in the catalogue": (
        "la acción aprobada con la que se creó esta vista previa ya no existe en el catálogo"
    ),
    "this preview no longer describes what would run: the approved-action catalogue entry has "
    "been edited since it was shown. Ask again and confirm the new preview": (
        "esta vista previa ya no describe lo que se ejecutaría: la entrada del catálogo de "
        "acciones aprobadas se modificó después de mostrarse. Pídela de nuevo y confirma la "
        "nueva vista previa"
    ),
    "this confirmation does not carry the digest of the preview that was issued for this "
    "action, so it is not a confirmation of anything a human was shown. Open the preview again "
    "and confirm that": (
        "esta confirmación no lleva el hash de la vista previa que se emitió para esta acción, "
        "así que no confirma nada que una persona haya visto. Abre la vista previa de nuevo y "
        "confirma esa"
    ),
    "this action's destructiveness has changed since the preview (the pinned script version or "
    "its exemption moved). Ask again and confirm the new preview": (
        "el carácter destructivo de esta acción cambió desde la vista previa (la versión "
        "fijada del script o su exención cambió). Pídela de nuevo y confirma la nueva vista "
        "previa"
    ),
    "this organization's autonomy setting for this assistant has changed since the preview "
    "(now {current}, was {effective_autonomy})": (
        "el ajuste de autonomía de esta organización para este asistente cambió desde la vista "
        "previa (ahora {current}, antes {effective_autonomy})"
    ),
    "{job_type} has no human dispatch route in this platform, so there is no permission that "
    "would authorize running it; it is not confirmable here": (
        "{job_type} no tiene ninguna vía de envío por una persona en esta plataforma, así que "
        "no hay ningún permiso que autorice ejecutarlo; no se puede confirmar aquí"
    ),
    "confirming a {job_type} action additionally requires {required} -- the same permission "
    "that job type's own module route requires": (
        "para confirmar una acción {job_type} se necesita además {required}, el mismo permiso "
        "que exige la ruta del módulo propio de ese tipo de tarea"
    ),
    "no such device, or you do not have permission to see it": (
        "no existe ese dispositivo, o no tienes permiso para verlo"
    ),
    "this action is currently rate-limited: {breach}": (
        "esta acción está limitada por frecuencia en este momento: {breach}"
    ),
    "no approved action {approved_action_id}": "no existe la acción aprobada {approved_action_id}",
    "declining an AI action requires ai.act.propose": (
        "para rechazar una acción de IA se necesita ai.act.propose"
    ),
    "{reason} is not a rejection reason; expected one of {ai_action_rejection_reasons}": (
        "{reason} no es un motivo de rechazo; se esperaba uno de {ai_action_rejection_reasons}"
    ),
    "this action is {state}, not awaiting a decision": (
        "esta acción está {state} y no espera una decisión"
    ),
    "unknown persona {persona}; expected one of {ai_personas}": (
        "persona desconocida {persona}; se esperaba una de {ai_personas}"
    ),
    "unknown risk class {risk_class}; expected one of {risk_classes}": (
        "clase de riesgo desconocida {risk_class}; se esperaba una de {risk_classes}"
    ),
    "{level} is not a settable ceiling; expected one of {ceiling_levels}. 'autonomous' is not "
    "reachable by an AI persona in v1 -- see docs/modules/ai-automation.md §0.4": (
        "{level} no es un límite que se pueda fijar; se esperaba uno de {ceiling_levels}. Una "
        "persona de IA no puede alcanzar 'autonomous' en la v1; consulta "
        "docs/modules/ai-automation.md §0.4"
    ),
    "no model provider is configured on this server": (
        "no hay ningún proveedor de modelos configurado en este servidor"
    ),
    "provider {key} is configured with kind {kind}, which has no client in this build": (
        "el proveedor {key} está configurado con el tipo {kind}, que no tiene cliente en esta "
        "compilación"
    ),
    "{field} must be between {low} and {high}; got {values}": (
        "{field} debe estar entre {low} y {high}; se recibió {values}"
    ),
    "daily_token_budget must be positive; got {budget}": (
        "daily_token_budget debe ser positivo; se recibió {budget}"
    ),
    "unknown ai settings field(s): {unknown}": (
        "campos de configuración de IA desconocidos: {unknown}"
    ),
    "no ai session {session_id}": "no existe la sesión de IA {session_id}",
    "{value} is not permitted in params key {key} for this job type": (
        "{value} no está permitido en la clave de parámetros {key} para este tipo de tarea"
    ),
    "params key {key} contains an unrecognised brace token: {value}": (
        "la clave de parámetros {key} contiene un token entre llaves no reconocido: {value}"
    ),
    "attachment filename must not contain a path segment": (
        "el nombre del archivo adjunto no puede contener un segmento de ruta"
    ),
    "attachment filename is invalid": "el nombre del archivo adjunto no es válido",
    "request body exceeds the {max_bytes}-byte limit": (
        "el cuerpo de la solicitud supera el límite de {max_bytes} bytes"
    ),
    "unknown permission code(s): {unknown}": "códigos de permiso desconocidos: {unknown}",
    "platform operator permission(s) are not grantable from an organization: {operator}": (
        "los permisos de operador de la plataforma no se pueden conceder desde una "
        "organización: {operator}"
    ),
    "{super_admin} is cross-org platform administration and is not readable, editable or "
    "deletable from an organization": (
        "{super_admin} es la administración de la plataforma entre organizaciones y no se "
        "puede leer, editar ni eliminar desde una organización"
    ),
    "no such role": "no existe ese rol",
    "a role named {name} already exists": "ya existe un rol llamado {name}",
    "this change would remove your own {lost}; an administrator cannot lock themselves out of "
    "access control": (
        "este cambio eliminaría tus propios permisos ({lost}); un administrador no puede "
        "quedarse sin acceso al control de accesos"
    ),
    "this change would leave the organization with nobody holding {org_admin_code}; grant it "
    "to another user first": (
        "este cambio dejaría a la organización sin nadie que tenga {org_admin_code}; concédelo "
        "antes a otro usuario"
    ),
    "{name} is a platform-wide role shared by every organization and cannot be deleted; edit "
    "it instead, which takes a copy for this organization": (
        "{name} es un rol de toda la plataforma, compartido por todas las organizaciones, y no "
        "se puede eliminar; edítalo y se creará una copia para esta organización"
    ),
    "{holders} user(s) still hold {name}; reassign them before deleting the role": (
        "{holders} usuario(s) aún tienen {name}; reasígnalos antes de eliminar el rol"
    ),
    "cron field value {value} out of range [{lo}, {hi}] in field {field}": (
        "el valor {value} del campo cron está fuera del rango [{lo}, {hi}] en el campo {field}"
    ),
    "invalid cron expression (expected 5 fields): {cron}": (
        "expresión cron no válida (se esperaban 5 campos): {cron}"
    ),
    "window_cron must be a cron expression of five fields (minute hour day-of-month month "
    "day-of-week), or null for no window": (
        "window_cron debe ser una expresión cron de cinco campos (minuto hora día-del-mes mes "
        "día-de-la-semana), o null para no tener ventana"
    ),
    "no JWKS key for kid {kid}": "no hay clave JWKS para el kid {kid}",
    "token has no kid header": "el token no tiene encabezado kid",
    "this token is not an access token": "este token no es un token de acceso",
    "this token was not issued to the console": "este token no se emitió para la consola",
    "token missing required {oidc_org_claim} claim in multi-tenant mode": (
        "al token le falta el claim obligatorio {oidc_org_claim} en el modo multiorganización"
    ),
    "token missing {oidc_org_claim} claim and no default org configured": (
        "al token le falta el claim {oidc_org_claim} y no hay una organización predeterminada "
        "configurada"
    ),
    "object suffix must be a non-empty relative path": (
        "el sufijo del objeto debe ser una ruta relativa no vacía"
    ),
    "object suffix must not contain a backslash or an empty segment": (
        "el sufijo del objeto no puede contener una barra invertida ni un segmento vacío"
    ),
    "object suffix must not contain a '.' or '..' segment": (
        "el sufijo del objeto no puede contener un segmento '.' ni '..'"
    ),
    "object suffix must not contain control characters": (
        "el sufijo del objeto no puede contener caracteres de control"
    ),
    "unknown agent OS {os_name}; expected one of {agent_release_os}": (
        "sistema operativo del agente desconocido {os_name}; se esperaba uno de "
        "{agent_release_os}"
    ),
    "unknown agent architecture {arch}; expected one of {agent_release_arch}": (
        "arquitectura del agente desconocida {arch}; se esperaba una de {agent_release_arch}"
    ),
    "agent release version must be a single segment of letters, digits, dot, underscore or "
    "hyphen; got {version}": (
        "la versión del agente debe ser un solo segmento de letras, dígitos, punto, guion bajo "
        "o guion; se recibió {version}"
    ),
    "no secret stored for this credential": "no hay ningún secreto guardado para esta credencial",
    "a role named {name} already exists (it is a platform-wide role)": (
        "ya existe un rol llamado {name} (es un rol de toda la plataforma)"
    ),
    "that console email domain would refuse your own address ({caller_email}), so you would be "
    "locked out of the console the moment it was saved": (
        "ese dominio de correo de la consola rechazaría tu propia dirección ({caller_email}), "
        "así que te quedarías sin acceso a la consola en cuanto se guardara"
    ),
    "{noun} cannot be routed to the requester ({spec_ref}): the requester is the ticket's "
    "external customer, and this event is internal to the organisation serving them": (
        "{noun} no se puede dirigir al solicitante ({spec_ref}): el solicitante es el cliente "
        "externo del ticket, y este evento es interno de la organización que lo atiende"
    ),
    "{job_type} is not in the approved-action allowlist. A job type is approvable only when "
    "what it does is bounded by the device the job targets AND a frozen params_template can "
    "fill its params truthfully -- no run id, snapshot, repository key or reconciliation "
    "version minted per dispatch. Adding one is a reviewed change to APPROVABLE_JOB_TYPES": (
        "{job_type} no está en la lista de acciones aprobadas permitidas. Un tipo de tarea "
        "solo se puede aprobar cuando lo que hace queda limitado por el dispositivo al que "
        "apunta la tarea Y un params_template fijo puede rellenar sus parámetros con "
        "veracidad: sin id de ejecución, instantánea, clave de repositorio ni versión de "
        "conciliación generados en cada envío. Añadir uno es un cambio revisado de "
        "APPROVABLE_JOB_TYPES"
    ),
    "agent.cert.renew is one leg of automatic certificate rotation. It is dispatched by the "
    "kernel's own state machine, which holds a device to one rotation at a time and pairs the "
    "CSR it produces with the signing leg (docs/modules/agent/cert-renewal.md). An approved "
    "action firing it would produce a CSR nothing is waiting for, and remediation has no "
    "business minting credentials: rotation already retries on every connect, so there is "
    "nothing here for an approved action to fix": (
        "agent.cert.renew es un tramo de la rotación automática de certificados. Lo envía la "
        "máquina de estados del propio núcleo, que limita a un dispositivo a una rotación a la "
        "vez y empareja la CSR que produce con el tramo de firma "
        "(docs/modules/agent/cert-renewal.md). Una acción aprobada que lo lanzara produciría "
        "una CSR que nada espera, y la corrección no tiene por qué emitir credenciales: la "
        "rotación ya se reintenta en cada conexión, así que aquí no hay nada que una acción "
        "aprobada deba corregir"
    ),
    "agent.cert.install REPLACES AN ENDPOINT'S mTLS IDENTITY, and its params carry a "
    "certificate the kernel minted from that device's own CSR. A frozen params_template can "
    "only hold a certificate that is already superseded -- and a machine-fired action that "
    "pushes an arbitrary certificate onto an endpoint is the exact attack automatic renewal "
    "closes. Rotation is not remediation": (
        "agent.cert.install SUSTITUYE LA IDENTIDAD mTLS DE UN EQUIPO, y sus parámetros llevan "
        "un certificado que el núcleo emitió a partir de la CSR de ese mismo dispositivo. Un "
        "params_template fijo solo puede contener un certificado que ya fue reemplazado, y una "
        "acción lanzada por una máquina que empuja un certificado cualquiera a un equipo es "
        "justo el ataque que la renovación automática cierra. La rotación no es corrección"
    ),
    "patch.reboot RESTARTS AN ENDPOINT. The patch module dispatches it only for a device that "
    "still owes a restart after patching and only inside that device's maintenance window; an "
    "approved action firing it would restart a machine outside its window on an alert's timing": (
        "patch.reboot REINICIA UN EQUIPO. El módulo de parches lo envía solo a un dispositivo "
        "que aún debe un reinicio tras aplicar parches y solo dentro de la ventana de "
        "mantenimiento de ese dispositivo; una acción aprobada que lo lanzara reiniciaría una "
        "máquina fuera de su ventana, al ritmo de una alerta"
    ),
    "job.cancel STOPS FLEET WORK, and a machine may not. It is a denial verb: its whole effect "
    "is to make an endpoint abandon something a human asked for, so an approved action naming "
    "it is a remediation whose remedy is 'do less'. A frozen params_template could not fill it "
    "truthfully either -- its one parameter is the id of a specific in-flight job, which is "
    "state minted per dispatch and cannot exist in a template. It is also the wrong shape for "
    "the autonomy dial: fleet-wide cancellation is a denial of service on patching and backup, "
    "and `max_target_scope='device'` bounds which device the envelope is signed for, not how "
    "much work stops": (
        "job.cancel DETIENE EL TRABAJO DE LA FLOTA, y una máquina no puede hacerlo. Es un "
        "verbo de denegación: todo su efecto es hacer que un equipo abandone algo que pidió "
        "una persona, así que una acción aprobada que lo nombre es una corrección cuyo remedio "
        "es 'hacer menos'. Un params_template fijo tampoco podría rellenarlo con veracidad: su "
        "único parámetro es el id de una tarea concreta en curso, un estado que se genera en "
        "cada envío y no puede existir en una plantilla. Además, tiene la forma equivocada "
        "para el dial de autonomía: la cancelación masiva de la flota es una denegación de "
        "servicio sobre los parches y las copias de seguridad, y `max_target_scope='device'` "
        "acota para qué dispositivo se firma el sobre, no cuánto trabajo se detiene"
    ),
    "site.wake puts a magic packet on a LAN segment, and WHERE it may do so is decided by the "
    "site-server registry: the control plane picks the operator-declared segment containing "
    "the target and computes the broadcast ADDRESS itself, so the endpoint never chooses. An "
    "approved action reaches JobEngine.create with the operator's own frozen params_template, "
    "which would carry a broadcast address nobody re-derived from a current declaration -- a "
    "segment demoted or edited afterwards would not change it. Its target is also a MAC on "
    "some other machine, which `max_target_scope='device'` cannot bound: what this job costs "
    "is not limited to the device the job is signed for. Dispatch a wake through POST "
    "/api/v1/site-servers/{site_server_id}/wake": (
        "site.wake pone un paquete mágico en un segmento de LAN, y DÓNDE puede hacerlo lo "
        "decide el catálogo de servidores de sitio: el plano de control elige el segmento "
        "declarado por el operador que contiene el destino y calcula él mismo la DIRECCIÓN de "
        "difusión, de modo que el equipo nunca elige. Una acción aprobada llega a "
        "JobEngine.create con el params_template fijo del propio operador, que llevaría una "
        "dirección de difusión que nadie volvió a derivar de una declaración vigente: un "
        "segmento degradado o editado después no la cambiaría. Su destino es además una MAC de "
        "otra máquina, que `max_target_scope='device'` no puede acotar: lo que cuesta esta "
        "tarea no se limita al dispositivo para el que se firma. Envía un encendido con POST "
        "/api/v1/site-servers/{site_server_id}/wake"
    ),
    "net.sweep puts packets on somebody else's wire, and where it may do so is decided by "
    "ipam's scan authorization (an `ipam_scan_authorization` row, the `ipam.scope.manage` "
    "permission, and the target set intersected with the subnet) plus an `ipam_sweep_run` row "
    "the result is checked against. An approved action reaches JobEngine.create with the "
    "operator's own template and none of that; its `targets` also name hosts other than the "
    "device the job is signed for, which `max_target_scope='device'` cannot bound. Dispatch a "
    "sweep through ipam's own endpoint": (
        "net.sweep pone paquetes en la red de otra persona, y dónde puede hacerlo lo decide la "
        "autorización de escaneo de ipam (una fila `ipam_scan_authorization`, el permiso "
        "`ipam.scope.manage` y el conjunto de destinos intersecado con la subred) más una fila "
        "`ipam_sweep_run` contra la que se comprueba el resultado. Una acción aprobada llega a "
        "JobEngine.create con la plantilla del propio operador y nada de eso; además, sus "
        "`targets` nombran hosts distintos del dispositivo para el que se firma la tarea, algo "
        "que `max_target_scope='device'` no puede acotar. Envía un barrido con el endpoint "
        "propio de ipam"
    ),
    "health.policy.apply delivers the device's whole monitoring policy and is owned by "
    "health's own policy push and reconciliation, not by remediation: `checks: []` is a legal "
    "policy meaning 'run nothing', so an approved action naming it is a monitoring kill "
    "switch, and its `policy_version` is reconciliation state that an operator-chosen value "
    "forges into 'this device has converged'": (
        "health.policy.apply entrega toda la política de monitorización del dispositivo y le "
        "pertenece al envío y la conciliación de políticas de salud, no a la corrección: "
        "`checks: []` es una política legal que significa 'no ejecutes nada', así que una "
        "acción aprobada que lo nombre es un interruptor para apagar la monitorización, y su "
        "`policy_version` es estado de conciliación que un valor elegido por un operador "
        "falsifica como 'este dispositivo ha convergido'"
    ),
    "backup.run's params carry `repo_key`, the repository encryption key, plus a "
    "`repository_id`/`storage_ref` owned by the backup module. A frozen params_template would "
    "have to hold that key in plaintext JSONB, which invariant #3 forbids (credentials live "
    "encrypted in the vault and are decrypted only at point of use). Schedule backups through "
    "the backup module": (
        "Los parámetros de backup.run llevan `repo_key`, la clave de cifrado del repositorio, "
        "además de un `repository_id`/`storage_ref` que pertenece al módulo de copias de "
        "seguridad. Un params_template fijo tendría que guardar esa clave en JSONB en texto "
        "plano, cosa que prohíbe la invariante #3 (las credenciales viven cifradas en la "
        "bóveda y solo se descifran en el momento de uso). Programa las copias de seguridad "
        "con el módulo de copias de seguridad"
    ),
    "backup.restore's params carry `repo_key`, the repository encryption key, and a "
    "`manifest_ref` naming one snapshot -- state the backup module mints per restore. A frozen "
    "template can only hold a plaintext key and a stale snapshot (invariant #3)": (
        "Los parámetros de backup.restore llevan `repo_key`, la clave de cifrado del "
        "repositorio, y un `manifest_ref` que nombra una instantánea: estado que el módulo de "
        "copias de seguridad genera en cada restauración. Una plantilla fija solo puede "
        "contener una clave en texto plano y una instantánea obsoleta (invariante #3)"
    ),
    "provision.enroll's `provision_run_id` must resolve to a provisioning run the provisioning "
    "module created; a frozen template can only ever name a stale one, and the run row is what "
    "authorized re-imaging that machine": (
        "El `provision_run_id` de provision.enroll debe corresponder a una ejecución de "
        "aprovisionamiento creada por el módulo de aprovisionamiento; una plantilla fija solo "
        "puede nombrar una obsoleta, y la fila de la ejecución es lo que autorizó reinstalar "
        "esa máquina"
    ),
    "agent.push_install is NEVER reachable by any automated, scheduled or AI-driven actor. "
    "That is not a judgement about its params template -- it is `CLAUDE.md` invariant 2a "
    "clause (e), enforced where it is checked rather than asserted in prose. The act is "
    "causing a machine nobody has enrolled to execute this platform's agent installer, using "
    "an administrative credential, against a host that cannot be authenticated on first "
    "contact; spec §1.1 accounts for what that is worth to an attacker who has captured the "
    "actor doing it. It also fails both of this allowlist's own conditions: what it costs is "
    "not bounded by the device the job is signed for (the job targets the site server; the "
    "effect lands on a THIRD machine), and a frozen params_template cannot fill a single-use "
    "enrollment token, a per-push presigned artefact URL or a credential decrypted at "
    "dispatch. Dispatch a push through the module's own endpoint, with a human on it": (
        "agent.push_install NUNCA está al alcance de ningún actor automático, programado o "
        "dirigido por IA. No es un juicio sobre su params_template: es la invariante 2a, "
        "cláusula (e), de `CLAUDE.md`, aplicada donde se comprueba y no afirmada en prosa. El "
        "acto consiste en hacer que un equipo que nadie ha inscrito ejecute el instalador del "
        "agente de esta plataforma, con una credencial administrativa, contra un host que no "
        "se puede autenticar en el primer contacto; la especificación §1.1 explica lo que eso "
        "vale para un atacante que haya capturado al actor que lo hace. Además incumple las "
        "dos condiciones de esta lista de permitidos: lo que cuesta no está acotado por el "
        "dispositivo para el que se firma la tarea (la tarea apunta al servidor de sitio; el "
        "efecto cae en una TERCERA máquina), y un params_template fijo no puede contener un "
        "token de inscripción de un solo uso, una URL prefirmada del artefacto por cada "
        "instalación remota ni una credencial descifrada en el envío. Envía una instalación "
        "remota con el endpoint propio del módulo, con una persona al mando"
    ),
    "agent.push_probe is refused for the same reason as agent.push_install and under the same "
    "clause (e): no automated, scheduled or AI-driven actor may cause this platform to reach "
    "an unenrolled machine. It carries no credential, which bounds what a compromised actor "
    "GETS -- not whether one may fire it. Its effect also lands on a machine other than the "
    "device the job is signed for, which `max_target_scope='device'` cannot bound": (
        "agent.push_probe se rechaza por la misma razón que agent.push_install y bajo la misma "
        "cláusula (e): ningún actor automático, programado o dirigido por IA puede hacer que "
        "esta plataforma llegue a un equipo sin inscribir. No lleva credencial, lo que acota "
        "lo que OBTIENE un actor comprometido, no si puede lanzarlo. Su efecto también cae en "
        "una máquina distinta del dispositivo para el que se firma la tarea, algo que "
        "`max_target_scope='device'` no puede acotar"
    ),
    "agent.update is NEVER reachable by any automated, scheduled or AI-driven actor "
    "(spec-self-update.md §8.5). It replaces the agent -- the process that verifies every "
    "other job -- and its availability risk is managed by rings a person advances and a "
    "failure threshold that pauses the rollout; a frozen params_template would carry a "
    "presigned URL that has expired and a release nobody chose for this fleet. Roll out agent "
    "updates through POST /api/v1/agent/update-rollouts, with a human on it": (
        "agent.update NUNCA está al alcance de ningún actor automático, programado o dirigido "
        "por IA (spec-self-update.md §8.5). Reemplaza el agente, el proceso que verifica todas "
        "las demás tareas, y su riesgo de disponibilidad lo gestionan los anillos que hace "
        "avanzar una persona y un umbral de fallos que pausa el despliegue; un params_template "
        "fijo llevaría una URL prefirmada ya caducada y una versión que nadie eligió para esta "
        "flota. Despliega las actualizaciones del agente con POST "
        "/api/v1/agent/update-rollouts, con una persona al mando"
    ),
    "agent.uninstall is NEVER reachable by any automated, scheduled or AI-driven actor "
    "(install-link spec §3). It removes the agent -- the platform's only presence on that "
    "computer -- and it exists to carry out ONE person's decision that a machine does not "
    "belong here. A frozen template could only ever uninstall a device nobody rejected. Reject "
    "the machine through POST /api/v1/devices/{device_id}/review/reject": (
        "agent.uninstall NUNCA está al alcance de ningún actor automático, programado o "
        "dirigido por IA (especificación del enlace de instalación §3). Quita el agente, la "
        "única presencia de la plataforma en ese equipo, y existe para ejecutar la decisión de "
        "UNA persona de que una máquina no pertenece aquí. Una plantilla fija solo podría "
        "desinstalar un dispositivo que nadie rechazó. Rechaza la máquina con POST "
        "/api/v1/devices/{device_id}/review/reject"
    ),
    "remote.session.open's params carry a `relay_token` -- a per-session credential the remote "
    "module mints from the vault at dispatch -- plus a `relay_url` and a `session_id` that "
    "must resolve to a `remote_session` row carrying the operator's consent decision. A frozen "
    "params_template can hold none of those truthfully, and a remote-control session opened by "
    "a machine with no human at either end is not a remediation. This is the same job type "
    "`SUBSTITUTABLE_PARAM_KEYS` already omits, for the same reason, now said once where it is "
    "enforced": (
        "Los parámetros de remote.session.open llevan un `relay_token`, una credencial por "
        "sesión que el módulo remoto genera desde la bóveda en el envío, además de una "
        "`relay_url` y un `session_id` que debe corresponder a una fila `remote_session` con "
        "la decisión de consentimiento del operador. Un params_template fijo no puede contener "
        "nada de eso con veracidad, y una sesión de control remoto abierta por una máquina sin "
        "ninguna persona en ninguno de los extremos no es una corrección. Es el mismo tipo de "
        "tarea que `SUBSTITUTABLE_PARAM_KEYS` ya omite, por la misma razón, ahora dicho una "
        "sola vez donde se aplica"
    ),
    "remote.screen.open shows and controls a person's screen. Its params carry a one-shot "
    "relay token valid for 60 s and a session_id minted by a technician's RBAC-checked start, "
    "so a frozen template cannot hold them, and screen share is never reachable by an "
    "automated or AI-driven actor (spec-screen-share.md §3.10)": (
        "remote.screen.open muestra y controla la pantalla de una persona. Sus parámetros "
        "llevan un token de retransmisión de un solo uso válido durante 60 s y un session_id "
        "generado por el inicio de un técnico con RBAC comprobado, así que una plantilla fija "
        "no puede contenerlos, y compartir pantalla nunca está al alcance de un actor "
        "automático o dirigido por IA (spec-screen-share.md §3.10)"
    ),
    "email domain not permitted for this console": (
        "dominio de correo no permitido para esta consola"
    ),
    "user account is not active": "la cuenta de usuario no está activa",
    # --- services ---
    "no such device group": "no existe ese grupo de dispositivos",
    "a group cannot be its own parent": "un grupo no puede ser su propio superior",
    "no such parent group in this organization": (
        "no existe ese grupo superior en esta organización"
    ),
    "the parent group is archived": "el grupo superior está archivado",
    "that parent is already beneath this group -- the move would make a cycle": (
        "ese grupo superior ya está dentro de este grupo: el movimiento crearía un ciclo"
    ),
    "that move would put a group {deepest} levels deep, past the {max_group_depth}-level bound "
    "health profile resolution walks": (
        "ese movimiento dejaría un grupo a {deepest} niveles de profundidad, más allá del "
        "límite de {max_group_depth} niveles que recorre la resolución del perfil de estado"
    ),
    "a group of that name already exists": "ya existe un grupo con ese nombre",
    "this group is archived; unarchive it first": "este grupo está archivado; desarchívalo primero",
    "this group is already archived": "este grupo ya está archivado",
    "this group is still in use -- every reference below has to be moved or removed first, "
    "because a device leaving a group does not lose policy, it falls back to the org default": (
        "este grupo sigue en uso: primero hay que mover o quitar todas las referencias de "
        "abajo, porque un dispositivo que sale de un grupo no pierde la política, vuelve a la "
        "predeterminada de la organización"
    ),
    "this group is not archived": "este grupo no está archivado",
    "a live group already holds that name -- rename that one, or this group cannot come back "
    "under its own name": (
        "ya hay un grupo activo con ese nombre: renómbralo, o este grupo no podrá volver con "
        "su propio nombre"
    ),
    "its parent group is archived -- unarchive the parent first, or this group comes back with "
    "nothing above it": (
        "su grupo superior está archivado: desarchiva primero el grupo superior, o este grupo "
        "volverá sin nada por encima"
    ),
    "this group's membership is decided by rules -- a hand placement here is reverted on the "
    "next inventory ingest": (
        "la pertenencia a este grupo la deciden las reglas: una colocación a mano aquí se "
        "revierte en la siguiente ingesta de inventario"
    ),
    "at most {max_members_per_request} devices per request": (
        "como máximo {max_members_per_request} dispositivos por solicitud"
    ),
    "that device is not a member of this group": "ese dispositivo no es miembro de este grupo",
    "no device {device_id} in this organisation": (
        "no hay ningún dispositivo {device_id} en esta organización"
    ),
    "this computer is not waiting for review": "este equipo no está esperando revisión",
    "the list names no computers": "la lista no nombra ningún equipo",
    "at most {approve_all_max} computers can be approved at once": (
        "se pueden aprobar como máximo {approve_all_max} equipos a la vez"
    ),
    "the list sent is not the list that was shown; open Approve all again": (
        "la lista enviada no es la que se mostró; abre Aprobar todos de nuevo"
    ),
    "this computer was not rejected": "este equipo no fue rechazado",
    "this computer's agent has not finished removing itself yet": (
        "el agente de este equipo aún no ha terminado de desinstalarse"
    ),
    "this computer's agent confirmed it removed itself; there is nothing to remove": (
        "el agente de este equipo confirmó que se desinstaló; no hay nada que quitar"
    ),
    "someone has already said they removed this agent": "alguien ya indicó que quitó este agente",
    "a DNS name is at most {max_dns_name} characters": (
        "un nombre DNS tiene como máximo {max_dns_name} caracteres"
    ),
    "that is an IP address; enter the computer's full DNS name, e.g. pc1.corp.example.com": (
        "eso es una dirección IP; escribe el nombre DNS completo del equipo, por ejemplo "
        "pc1.corp.example.com"
    ),
    "a DNS name needs its domain as well, e.g. pc1.corp.example.com rather than pc1": (
        "un nombre DNS necesita también su dominio, por ejemplo pc1.corp.example.com en lugar "
        "de pc1"
    ),
    "each part of a DNS name is 1 to 63 letters, digits or hyphens, and does not start or end "
    "with a hyphen": (
        "cada parte de un nombre DNS tiene de 1 a 63 letras, dígitos o guiones, y no empieza "
        "ni termina con un guion"
    ),
    "the last part of a DNS name cannot be all digits": (
        "la última parte de un nombre DNS no puede ser solo dígitos"
    ),
    "max_uses must be a whole number": "max_uses debe ser un número entero",
    "max_uses must be between 1 and {max_token_uses}; an enrollment token is a credential that "
    "puts new machines into this organisation, and there is no unlimited one": (
        "max_uses debe estar entre 1 y {max_token_uses}; un token de inscripción es una "
        "credencial que mete equipos nuevos en esta organización, y no existe uno ilimitado"
    ),
    "at most {max_networks} networks may be listed": (
        "se pueden indicar como máximo {max_networks} redes"
    ),
    "{item} is not a network ({exc})": "{item} no es una red ({exc})",
    "{item} is every address, not a network: list the networks the computers you are "
    "installing are on": (
        "{item} abarca todas las direcciones, no es una red: indica las redes en las que están "
        "los equipos que vas a instalar"
    ),
    "{item} overlaps {proxy}, the network ITMS's own proxy sits on (ITS_TRUSTED_PROXY_CIDRS). "
    "Requests can appear to come from there, so listing it would let any computer in": (
        "{item} se solapa con {proxy}, la red en la que está el proxy propio de ITMS "
        "(ITS_TRUSTED_PROXY_CIDRS). Las solicitudes pueden parecer venir de ahí, así que "
        "indicarla dejaría entrar a cualquier equipo"
    ),
    "the hourly limit must be between {min_hourly_limit} and {max_hourly_limit}": (
        "el límite por hora debe estar entre {min_hourly_limit} y {max_hourly_limit}"
    ),
    "the automatic switch-off time must include a time zone": (
        "la hora del apagado automático debe incluir una zona horaria"
    ),
    "the automatic switch-off must be in the future": "el apagado automático debe ser en el futuro",
    "at least one network is required to switch on the install link of the site {site}: list "
    "the networks its computers reach the Internet from; an empty list would not mean "
    "'anywhere'": (
        "hace falta al menos una red para activar el enlace de instalación de la sede {site}: "
        "indica las redes desde las que sus equipos salen a Internet; una lista vacía no "
        "significaría 'cualquier lugar'"
    ),
    "no such site": "no existe esa sede",
    "The agent has been told to uninstall itself. Its certificate is revoked as soon as it "
    "confirms, or in 24 hours if it does not.": (
        "Se le ha indicado al agente que se desinstale. Su certificado se revoca en cuanto lo "
        "confirme, o en 24 horas si no lo hace."
    ),
    "This computer's agent is too old to remove itself. Its certificate has been revoked, so "
    "it can no longer connect; remove the agent from the computer by hand.": (
        "El agente de este equipo es demasiado antiguo para desinstalarse solo. Su certificado "
        "se ha revocado, así que ya no puede conectarse; quita el agente del equipo a mano."
    ),
    "this group already holds {held} hand-placed device": (
        "este grupo ya contiene {held} dispositivo colocado a mano"
    ),
    "this group already holds {held} hand-placed devices": (
        "este grupo ya contiene {held} dispositivos colocados a mano"
    ),
    " -- membership is either hand-curated or decided by rules, never both, because a rule "
    "claims a device rather than joining it. Remove them first, or put the rules on a new group": (
        ": la pertenencia es o bien elegida a mano o bien decidida por reglas, nunca ambas, "
        "porque una regla reclama un dispositivo en lugar de unirse a él. Quítalos primero, o "
        "pon las reglas en un grupo nuevo"
    ),
    # --- jobs ---
    "{job_type} is not dispatchable through the generic jobs route. That route may only create "
    "job types no module governs; every other type is dispatched through its module's own "
    "endpoint, which is where the authorization, the record of what was requested and the "
    "bounds on it live. Adding one is a reviewed change to DIRECTLY_DISPATCHABLE_JOB_TYPES": (
        "{job_type} no se puede enviar por la ruta genérica de tareas. Esa ruta solo puede "
        "crear tipos de tarea que ningún módulo gobierna; todos los demás tipos se envían por "
        "el endpoint de su propio módulo, que es donde viven la autorización, el registro de "
        "lo solicitado y sus límites. Añadir uno es un cambio revisado de "
        "DIRECTLY_DISPATCHABLE_JOB_TYPES"
    ),
    "invalid params for {job_type}: {exc}": "parámetros no válidos para {job_type}: {exc}",
    "{job_type} is not re-runnable through the jobs route. That route may only re-run job "
    "types no module governs, plus those whose module has declared how to repoint its own "
    "record at the new job. Adding one is a reviewed change in the owning module": (
        "{job_type} no se puede volver a ejecutar por la ruta de tareas. Esa ruta solo puede "
        "volver a ejecutar tipos de tarea que ningún módulo gobierna, más aquellos cuyo módulo "
        "ha declarado cómo reapuntar su propio registro a la tarea nueva. Añadir uno es un "
        "cambio revisado en el módulo propietario"
    ),
    "this job is {status} and has not finished, so there is nothing to re-run yet. A job that "
    "never reaches its device is re-sent automatically by the platform's own stuck-dispatch "
    "sweep; a job that is running will report its own outcome": (
        "esta tarea está {status} y no ha terminado, así que todavía no hay nada que volver a "
        "ejecutar. Una tarea que nunca llega a su dispositivo la reenvía automáticamente el "
        "barrido de envíos atascados de la plataforma; una tarea en ejecución informará de su "
        "propio resultado"
    ),
    "this job succeeded, and re-running work that worked is a new dispatch rather than a retry "
    "-- it belongs on the route that owns the record of what was asked for and the bounds on "
    "it, not on a verb whose name says 'again'": (
        "esta tarea se completó con éxito, y volver a ejecutar un trabajo que funcionó es un "
        "envío nuevo, no un reintento: corresponde a la ruta que posee el registro de lo "
        "solicitado y sus límites, no a un verbo cuyo nombre dice 'otra vez'"
    ),
    "no such job": "no existe esa tarea",
    "{job_type} has no human dispatch route in this platform, so there is no permission that "
    "would authorize starting one -- and re-running a job is gated on exactly that permission": (
        "{job_type} no tiene ninguna vía de envío por una persona en esta plataforma, así que "
        "no hay ningún permiso que autorice iniciar una; y volver a ejecutar una tarea depende "
        "exactamente de ese permiso"
    ),
    "re-running a {job_type} job requires {required}, the permission its own dispatch route "
    "requires: a retry is a new dispatch and is authorized as one": (
        "volver a ejecutar una tarea {job_type} requiere {required}, el permiso que exige su "
        "propia ruta de envío: un reintento es un envío nuevo y se autoriza como tal"
    ),
    "{job_type} is no longer a registered job type in this platform, so there is nothing to "
    "re-run it as": (
        "{job_type} ya no es un tipo de tarea registrado en esta plataforma, así que no hay "
        "nada con lo que volver a ejecutarlo"
    ),
    "this job's stored parameters no longer satisfy what {job_type} requires, so it cannot be "
    "re-run as it was written. Dispatch a fresh one instead: {exc}": (
        "los parámetros guardados de esta tarea ya no cumplen lo que exige {job_type}, así que "
        "no se puede volver a ejecutar tal como se escribió. Envía una nueva en su lugar: {exc}"
    ),
    "this job {outcome}{when} and cannot be cancelled. A job that has reached a terminal state "
    "is final -- its outcome is what the record says happened, and nothing may rewrite it": (
        "esta tarea {outcome}{when} y no se puede cancelar. Una tarea que ha llegado a un "
        "estado terminal es definitiva: su resultado es lo que el registro dice que ocurrió, y "
        "nada puede reescribirlo"
    ),
    "is {status}": "está {status}",
    "this job {reached}, and {job_type} may not be interrupted once it has got there: stopping "
    "one part-way leaves the endpoint in a state nobody chose -- a half-applied update, a "
    "half-written restore -- which is worse than the job you wanted to stop. Withdrawal is "
    "only offered before a job is sent. The job will run or expire at {expires_at}": (
        "esta tarea {reached}, y {job_type} no se puede interrumpir una vez que ha llegado "
        "ahí: detener una a medias deja el equipo en un estado que nadie eligió (una "
        "actualización a medio aplicar, una restauración a medio escribir), que es peor que la "
        "tarea que querías detener. Solo se puede retirar una tarea antes de enviarla. La "
        "tarea se ejecutará o caducará el {expires_at}"
    ),
    "finished successfully": "terminó correctamente",
    "failed": "falló",
    "expired": "caducó",
    "was refused by the endpoint": "fue rechazada por el equipo",
    " at {moment}": " el {moment}",
    "has already been sent to the device": "ya se envió al dispositivo",
    "is executing on the device": "se está ejecutando en el dispositivo",
    "{job_type} has no human dispatch route in this platform, so there is no permission that "
    "would authorize starting one -- and cancelling a job is gated on exactly that permission": (
        "{job_type} no tiene ninguna vía de envío por una persona en esta plataforma, así que "
        "no hay ningún permiso que autorice iniciar una; y cancelar una tarea depende "
        "exactamente de ese permiso"
    ),
    "cancelling a {job_type} job requires {required}, the permission its own dispatch route "
    "requires: whoever may start a job may stop it": (
        "cancelar una tarea {job_type} requiere {required}, el permiso que exige su propia "
        "ruta de envío: quien puede iniciar una tarea puede detenerla"
    ),
    "a cancellation for this job was already requested at {requested_at}; it is not requested "
    "twice": (
        "ya se solicitó una cancelación de esta tarea el {requested_at}; no se solicita dos "
        "veces"
    ),
    "site.wake is governed by the site-server registry: the broadcast ADDRESS is computed "
    "server-side from the operator-declared `site_server.segments` entry containing the "
    "target, so the endpoint never decides what it may touch, and the act is gated on "
    "`site.wake` separately from `site.manage`. Through the kernel route an arbitrary "
    "broadcast address in `params` would put a datagram on any wire in reach of any promoted "
    "device -- with no registry row consulted, no `site.wake_dispatched` audit event, and the "
    "300s TTL replaced by the kernel default, so a wake could fire an hour after the window it "
    "belonged to. Use POST /api/v1/site-servers/{site_server_id}/wake (site.wake)": (
        "site.wake lo gobierna el catálogo de servidores de sitio: la DIRECCIÓN de difusión se "
        "calcula en el servidor a partir de la entrada `site_server.segments` declarada por el "
        "operador que contiene el destino, de modo que el equipo nunca decide qué puede tocar, "
        "y el acto depende de `site.wake` por separado de `site.manage`. Por la ruta del "
        "núcleo, una dirección de difusión arbitraria en `params` pondría un datagrama en "
        "cualquier red al alcance de cualquier dispositivo promovido, sin consultar ninguna "
        "fila del catálogo, sin evento de auditoría `site.wake_dispatched` y con el TTL de 300 "
        "s sustituido por el predeterminado del núcleo, de modo que un encendido podría "
        "dispararse una hora después de la ventana a la que pertenecía. Usa POST "
        "/api/v1/site-servers/{site_server_id}/wake (site.wake)"
    ),
    "net.sweep is governed by ipam's scan authorization: the targets are intersected with the "
    "org's active `ipam_scan_authorization` rows (deny by default), an `ipam_sweep_run` row is "
    "written for the result to be checked against, `ipam.sweep_dispatched` is audited and the "
    "TTL is clamped. None of that happens here, so a sweep dispatched through the kernel route "
    "puts packets on somebody else's wire unauthorized and unrecorded. Use POST "
    "/api/v1/ipam/subnets/{subnet_id}/sweep (ipam.sweep.run)": (
        "net.sweep lo gobierna la autorización de escaneo de ipam: los destinos se intersecan "
        "con las filas `ipam_scan_authorization` activas de la organización (denegar por "
        "defecto), se escribe una fila `ipam_sweep_run` contra la que comprobar el resultado, "
        "se audita `ipam.sweep_dispatched` y se acota el TTL. Nada de eso ocurre aquí, así que "
        "un barrido enviado por la ruta del núcleo pone paquetes en la red de otra persona sin "
        "autorización y sin registro. Usa POST /api/v1/ipam/subnets/{subnet_id}/sweep "
        "(ipam.sweep.run)"
    ),
    "health.policy.apply delivers a device's WHOLE monitoring policy and is owned by health's "
    "policy push and reconciliation. `checks: []` is a legal policy meaning 'run nothing', so "
    "an operator-authored one is the platform's own monitoring kill switch, and "
    "`policy_version` is reconciliation state that an operator-chosen value forges into 'this "
    "device has converged'. There is no route for it by design: assign a health profile "
    "(health.admin) and the push is queued from the effective policy": (
        "health.policy.apply entrega TODA la política de monitorización de un dispositivo y "
        "pertenece al envío y la conciliación de políticas de salud. `checks: []` es una "
        "política legal que significa 'no ejecutes nada', así que una creada por un operador "
        "es el interruptor de apagado de la propia monitorización de la plataforma, y "
        "`policy_version` es estado de conciliación que un valor elegido por un operador "
        "falsifica como 'este dispositivo ha convergido'. No hay ninguna ruta para ello por "
        "diseño: asigna un perfil de salud (health.admin) y el envío se pone en cola a partir "
        "de la política efectiva"
    ),
    "health.check.now is dispatched against a device's EFFECTIVE health policy and is "
    "rate-limited per device by health's own route, which also refuses to pile a second one on "
    "an in-flight job. Use POST /api/v1/devices/{device_id}/health/check-now "
    "(health.alert.manage)": (
        "health.check.now se envía contra la política de salud EFECTIVA de un dispositivo y la "
        "ruta propia de salud lo limita por dispositivo, y además se niega a apilar una "
        "segunda sobre una tarea en curso. Usa POST "
        "/api/v1/devices/{device_id}/health/check-now (health.alert.manage)"
    ),
    "patch.scan is owned by the patch module, which resolves the device set, audits "
    "`patch.scan_dispatched` per device and is the same path the scheduled scan sweep takes. "
    "Use POST /api/v1/patch/scan (patch.scan)": (
        "patch.scan pertenece al módulo de parches, que resuelve el conjunto de dispositivos, "
        "audita `patch.scan_dispatched` por dispositivo y sigue el mismo camino que el barrido "
        "de escaneo programado. Usa POST /api/v1/patch/scan (patch.scan)"
    ),
    "patch.apply installs software and may reboot. The patch module decides it against the "
    "org's APPROVAL state for each KB and the device's maintenance WINDOW, and records the "
    "run; the kernel route takes an operator's own `kbs` and `reboot` with neither. Use POST "
    "/api/v1/patch/apply (patch.apply)": (
        "patch.apply instala software y puede reiniciar. El módulo de parches lo decide según "
        "el estado de APROBACIÓN de cada KB de la organización y la VENTANA de mantenimiento "
        "del dispositivo, y registra la ejecución; la ruta del núcleo toma los `kbs` y el "
        "`reboot` del propio operador sin ninguna de las dos cosas. Usa POST "
        "/api/v1/patch/apply (patch.apply)"
    ),
    "patch.reboot restarts an endpoint. The patch module dispatches it only for a device that "
    "owes a restart -- after patching, or booked by an operator from the device page "
    "(patch.apply) -- only inside its maintenance window, and tracks it to completion; there "
    "is no route for it by design": (
        "patch.reboot reinicia un equipo. El módulo de parches lo envía solo a un dispositivo "
        "que debe un reinicio (tras aplicar parches, o reservado por un operador desde la "
        "página del dispositivo (patch.apply)), solo dentro de su ventana de mantenimiento, y "
        "lo sigue hasta que se completa; no hay ninguna ruta para ello por diseño"
    ),
    "app.deploy installs a package on an endpoint. The deploy module builds the params from "
    "the stored `package` row -- artifact ref, hash, install arguments -- and records a "
    "`deployment_target`; the kernel route would take an operator-authored artifact reference "
    "instead. Use POST /api/v1/deploy/deployments/{id}/execute (deploy.execute)": (
        "app.deploy instala un paquete en un equipo. El módulo de despliegue construye los "
        "parámetros a partir de la fila `package` guardada (referencia del artefacto, hash, "
        "argumentos de instalación) y registra un `deployment_target`; la ruta del núcleo "
        "tomaría en su lugar una referencia de artefacto creada por el operador. Usa POST "
        "/api/v1/deploy/deployments/{id}/execute (deploy.execute)"
    ),
    "script.run executes arbitrary code as SYSTEM/root. The deploy module builds the params "
    "from the PINNED `scripts` row and writes a `script_run` plus a `script.dispatched` audit "
    "event, so what ran is identifiable afterwards; the kernel route would take an interpreter "
    "and a body straight from the request with no script row, no run record and no audit trail "
    "naming what was executed. Use POST /api/v1/deploy/scripts/{id}/run (deploy.script.run)": (
        "script.run ejecuta código arbitrario como SYSTEM/root. El módulo de despliegue "
        "construye los parámetros a partir de la fila `scripts` FIJADA y escribe un "
        "`script_run` más un evento de auditoría `script.dispatched`, de modo que lo que se "
        "ejecutó se pueda identificar después; la ruta del núcleo tomaría un intérprete y un "
        "cuerpo directamente de la solicitud, sin fila de script, sin registro de ejecución y "
        "sin un rastro de auditoría que nombre lo ejecutado. Usa POST "
        "/api/v1/deploy/scripts/{id}/run (deploy.script.run)"
    ),
    "backup.run's params carry `repo_key`, the repository encryption key, which the backup "
    "module decrypts from the vault at point of use (invariant #3) along with the repository "
    "and source set from the device's policy. A kernel-route caller would have to put a key in "
    "a request body. Use POST /api/v1/backup/devices/{device_id}/run (backup.run)": (
        "Los parámetros de backup.run llevan `repo_key`, la clave de cifrado del repositorio, "
        "que el módulo de copias de seguridad descifra desde la bóveda en el momento de uso "
        "(invariante #3) junto con el repositorio y el conjunto de orígenes de la política del "
        "dispositivo. Quien llamara por la ruta del núcleo tendría que poner una clave en el "
        "cuerpo de una solicitud. Usa POST /api/v1/backup/devices/{device_id}/run (backup.run)"
    ),
    "backup.restore's params carry `repo_key` and a `manifest_ref` naming one snapshot -- "
    "state the backup module mints per restore from the recovery point, having checked it "
    "belongs to this org and this device. Use POST /api/v1/backup/points/{point_id}/restore "
    "(backup.restore)": (
        "Los parámetros de backup.restore llevan `repo_key` y un `manifest_ref` que nombra una "
        "instantánea: estado que el módulo de copias de seguridad genera en cada restauración "
        "a partir del punto de recuperación, tras comprobar que pertenece a esta organización "
        "y a este dispositivo. Usa POST /api/v1/backup/points/{point_id}/restore "
        "(backup.restore)"
    ),
    "provision.enroll re-images a machine. Its `provision_run_id` must resolve to a "
    "provisioning run that authorized doing so, and the module mints that row from the profile "
    "and image; a kernel-route caller can only name a stale one or invent one. Use POST "
    "/api/v1/provisioning/profiles/{profile_id}/run (provisioning.run)": (
        "provision.enroll reinstala una máquina. Su `provision_run_id` debe corresponder a una "
        "ejecución de aprovisionamiento que autorizó hacerlo, y el módulo genera esa fila a "
        "partir del perfil y la imagen; quien llame por la ruta del núcleo solo puede nombrar "
        "una obsoleta o inventar una. Usa POST /api/v1/provisioning/profiles/{profile_id}/run "
        "(provisioning.run)"
    ),
    "agent.cert.renew is one leg of automatic certificate rotation and is dispatched by the "
    "kernel's own state machine at connect (docs/modules/agent/cert-renewal.md §3.1), which "
    "holds it to one rotation per device at a time and pairs it with the CSR-signing leg. An "
    "operator-created one would produce a CSR the state machine never asked for and can no "
    "longer distinguish from the one it is waiting on. There is no route for it by design: "
    "rotation needs no operator action, and a device whose window has passed is re-deployed": (
        "agent.cert.renew es un tramo de la rotación automática de certificados y lo envía la "
        "máquina de estados del propio núcleo al conectar (docs/modules/agent/cert-renewal.md "
        "§3.1), que lo limita a una rotación por dispositivo a la vez y lo empareja con el "
        "tramo de firma de la CSR. Uno creado por un operador produciría una CSR que la "
        "máquina de estados nunca pidió y que ya no podría distinguir de la que está "
        "esperando. No hay ninguna ruta para ello por diseño: la rotación no necesita ninguna "
        "acción del operador, y un dispositivo cuya ventana ha pasado se vuelve a desplegar"
    ),
    "agent.cert.install REPLACES AN ENDPOINT'S mTLS IDENTITY. Its params carry a certificate "
    "that the kernel minted from a CSR that device produced, for that device's own device_id "
    "and org_id, from the internal CA. An operator-authored one would take a certificate PEM "
    "straight from a request body -- any certificate, for any endpoint -- which is the exact "
    "attack automatic renewal otherwise closes. There is no route for it by design": (
        "agent.cert.install SUSTITUYE LA IDENTIDAD mTLS DE UN EQUIPO. Sus parámetros llevan un "
        "certificado que el núcleo emitió a partir de una CSR que produjo ese dispositivo, "
        "para el device_id y el org_id de ese mismo dispositivo, desde la CA interna. Uno "
        "creado por un operador tomaría un certificado PEM directamente del cuerpo de una "
        "solicitud (cualquier certificado, para cualquier equipo), que es justo el ataque que "
        "la renovación automática cierra por otra vía. No hay ninguna ruta para ello por diseño"
    ),
    "job.cancel is minted by the kernel's own cancel route from a job this platform already "
    "signed: its single parameter is the id of that job, and the whole point of the type is "
    "that the target is the SERVER'S OWN statement rather than anything a caller typed. "
    "Through the kernel route an operator could name any job id at all, including one on "
    "another device, with no cancel request recorded against the target, no "
    "`jobs.cancel_requested` audit event and no permission check against the TARGET job's type "
    "-- which is where the authorization for stopping it actually lives. There is no route for "
    "it by design: cancel a job with POST /api/v1/jobs/{job_id}/cancel and the platform mints "
    "the signed command itself": (
        "job.cancel lo genera la ruta de cancelación del propio núcleo a partir de una tarea "
        "que esta plataforma ya firmó: su único parámetro es el id de esa tarea, y el objetivo "
        "del tipo es que el destino sea la declaración PROPIA DEL SERVIDOR y no algo que "
        "escribió quien llama. Por la ruta del núcleo, un operador podría nombrar cualquier id "
        "de tarea, incluso uno de otro dispositivo, sin ninguna solicitud de cancelación "
        "registrada contra el destino, sin evento de auditoría `jobs.cancel_requested` y sin "
        "comprobar el permiso del tipo de la tarea DESTINO, que es donde vive en realidad la "
        "autorización para detenerla. No hay ninguna ruta para ello por diseño: cancela una "
        "tarea con POST /api/v1/jobs/{job_id}/cancel y la plataforma genera ella misma el "
        "comando firmado"
    ),
    "agent.push_install causes a machine NOBODY HAS ENROLLED to run this platform's own agent "
    "installer, and it is governed by four independent bounds that exist nowhere near the "
    "kernel route (`CLAUDE.md` invariant 2a): the target must be a promoted asset row in this "
    "org, an active `push_authorization` must cover its address, the chosen credential must "
    "carry a `push_credential_scope` covering it, and the originator must be a device the "
    "operator declared a site server for that machine's segment -- with no fallback "
    "originator, because the originator holds a plaintext administrative credential in process "
    "memory for the job's duration. The module's own route refuses on the near side of the "
    "signing boundary, records which bound failed, and enforces the batch ceiling; the kernel "
    "route reproduces none of that, so a push created here would present a domain-capable "
    "secret to any address in a request body. The module's route additionally requires "
    "`credentials.checkout`, checked at its service boundary. Use POST "
    "/api/v1/agent-push/requests (agent.push.run)": (
        "agent.push_install hace que un equipo que NADIE HA INSCRITO ejecute el instalador del "
        "agente de esta plataforma, y lo gobiernan cuatro límites independientes que no "
        "existen en ningún lugar cercano a la ruta del núcleo (invariante 2a de `CLAUDE.md`): "
        "el destino debe ser una fila de activo promovida de esta organización, una "
        "`push_authorization` activa debe cubrir su dirección, la credencial elegida debe "
        "llevar un `push_credential_scope` que la cubra, y el originador debe ser un "
        "dispositivo para el que el operador declaró un servidor de sitio en el segmento de "
        "esa máquina, sin originador alternativo, porque el originador guarda una credencial "
        "administrativa en texto plano en la memoria del proceso mientras dura la tarea. La "
        "ruta propia del módulo rechaza en el lado cercano de la frontera de firma, registra "
        "qué límite falló y aplica el tope del lote; la ruta del núcleo no reproduce nada de "
        "eso, así que una instalación remota creada aquí presentaría un secreto con alcance de "
        "dominio a cualquier dirección del cuerpo de una solicitud. La ruta del módulo exige "
        "además `credentials.checkout`, comprobado en su frontera de servicio. Usa POST "
        "/api/v1/agent-push/requests (agent.push.run)"
    ),
    "agent.push_probe opens a transport to an UNENROLLED machine to learn its host key before "
    "any credential is presented, and it is bounded by the same four checks as "
    "agent.push_install for the same reason: it still puts a connection on somebody's wire, "
    "from a declared site server and nowhere else, against one address a human named. It "
    "carries no credential and its params model has no field that could hold one -- which is "
    "what makes it safe to run BEFORE the operator confirms the fingerprint, not what makes it "
    "dispatchable by hand. Like the push itself it additionally requires "
    "`credentials.checkout` at the service boundary. Use POST /api/v1/agent-push/requests "
    "(agent.push.run)": (
        "agent.push_probe abre un transporte hacia un equipo SIN INSCRIBIR para conocer su "
        "clave de host antes de presentar ninguna credencial, y lo acotan las mismas cuatro "
        "comprobaciones que agent.push_install por la misma razón: aun así pone una conexión "
        "en la red de otra persona, desde un servidor de sitio declarado y desde ningún otro, "
        "contra una dirección que una persona nombró. No lleva credencial y su modelo de "
        "parámetros no tiene ningún campo que pudiera contenerla, que es lo que lo hace seguro "
        "de ejecutar ANTES de que el operador confirme la huella, no lo que lo hace enviable a "
        "mano. Como la propia instalación remota, exige además `credentials.checkout` en la "
        "frontera de servicio. Usa POST /api/v1/agent-push/requests (agent.push.run)"
    ),
    "agent.update REPLACES THE AGENT ON AN ENDPOINT. The self-update module builds its params "
    "from the `agent_releases` row the rollout names -- the server-computed digest and size, a "
    "presigned URL and the offline release signature -- dispatches in batches only for a ring "
    "a person started, pauses on the failure threshold, and records every outcome against a "
    "rollout target. The kernel route would take an operator-typed artifact URL and digest "
    "with none of that. Use POST /api/v1/agent/update-rollouts (agent.update.rollout)": (
        "agent.update SUSTITUYE EL AGENTE DE UN EQUIPO. El módulo de autoactualización "
        "construye sus parámetros a partir de la fila `agent_releases` que nombra el "
        "despliegue (el hash y el tamaño calculados por el servidor, una URL prefirmada y la "
        "firma de versión offline), envía por lotes solo para un anillo que inició una "
        "persona, se pausa al superar el umbral de fallos y registra cada resultado contra un "
        "destino del despliegue. La ruta del núcleo tomaría una URL de artefacto y un hash "
        "escritos por el operador sin nada de eso. Usa POST /api/v1/agent/update-rollouts "
        "(agent.update.rollout)"
    ),
    "agent.uninstall REMOVES THE AGENT FROM A COMPUTER. It is the second half of rejecting a "
    "machine that joined through the install link: the device review records who rejected it "
    "and why, holds the machine to this one job type, and revokes its certificate when the "
    "agent confirms or 24 hours later. Through the kernel route it would uninstall an "
    "approved, working agent with none of that, and nothing would revoke the certificate it "
    "leaves behind. Use POST /api/v1/devices/{device_id}/review/reject (devices.review)": (
        "agent.uninstall QUITA EL AGENTE DE UN EQUIPO. Es la segunda mitad de rechazar una "
        "máquina que se unió mediante el enlace de instalación: la revisión del dispositivo "
        "registra quién la rechazó y por qué, limita la máquina a este único tipo de tarea y "
        "revoca su certificado cuando el agente lo confirma o 24 horas después. Por la ruta "
        "del núcleo desinstalaría un agente aprobado y en funcionamiento sin nada de eso, y "
        "nada revocaría el certificado que deja atrás. Usa POST "
        "/api/v1/devices/{device_id}/review/reject (devices.review)"
    ),
    "remote.session.open's params carry a `relay_token` the remote module mints from the vault "
    "per session, plus a `relay_url` and a `session_id` that must resolve to a "
    "`remote_session` row carrying the end user's CONSENT decision. A remote-control session "
    "opened without that row is a session nobody consented to and nobody recorded. Use POST "
    "/api/v1/remote/sessions (remote.control)": (
        "Los parámetros de remote.session.open llevan un `relay_token` que el módulo remoto "
        "genera desde la bóveda en cada sesión, además de una `relay_url` y un `session_id` "
        "que debe corresponder a una fila `remote_session` con la decisión de CONSENTIMIENTO "
        "del usuario final. Una sesión de control remoto abierta sin esa fila es una sesión "
        "que nadie consintió y nadie registró. Usa POST /api/v1/remote/sessions "
        "(remote.control)"
    ),
    "remote.screen.open shares and controls a person's screen. Its relay token and session_id "
    "must match a Redis registration and a remote_session row that the remote module writes "
    "with the consent mode, policy bounds and audit record. A job created without them cannot "
    "connect, and one that could would be an unrecorded session. Use POST "
    "/api/v1/remote/sessions (remote.control)": (
        "remote.screen.open comparte y controla la pantalla de una persona. Su token de "
        "retransmisión y su session_id deben coincidir con un registro en Redis y con una fila "
        "`remote_session` que el módulo remoto escribe con el modo de consentimiento, los "
        "límites de la política y el registro de auditoría. Una tarea creada sin ellos no "
        "puede conectarse, y una que pudiera sería una sesión sin registrar. Usa POST "
        "/api/v1/remote/sessions (remote.control)"
    ),
    "owned by health's own policy push and reconciliation; there is no route for it by design "
    "-- assign a health profile (health.admin) and the push is queued from the effective "
    "policy. `checks: []` is a legal policy meaning 'run nothing', so an operator-authored one "
    "would be the platform's own monitoring kill switch": (
        "pertenece al envío y la conciliación de políticas de salud; no hay ninguna ruta para "
        "ello por diseño: asigna un perfil de salud (health.admin) y el envío se pone en cola "
        "a partir de la política efectiva. `checks: []` es una política legal que significa "
        "'no ejecutes nada', así que una creada por un operador sería el interruptor de "
        "apagado de la propia monitorización de la plataforma"
    ),
    "minted by the kernel's cancel route from a job the platform already signed, never by a "
    "human naming a job id. The authorization for stopping a job is the TARGET job type's own "
    "dispatch permission, checked on that route before this one is created -- so a "
    "`job.cancel` an operator could dispatch directly would be a cancel that skipped the only "
    "check that governs it": (
        "la genera la ruta de cancelación del núcleo a partir de una tarea que la plataforma "
        "ya firmó, nunca una persona que nombre un id de tarea. La autorización para detener "
        "una tarea es el permiso de envío del tipo de tarea DESTINO, comprobado en esa ruta "
        "antes de crear esta; por eso un `job.cancel` que un operador pudiera enviar "
        "directamente sería una cancelación que se salta la única comprobación que la gobierna"
    ),
    "one leg of automatic certificate rotation, dispatched by the kernel's own state machine "
    "at connect, which holds a device to one rotation at a time and pairs the CSR it produces "
    "with the signing leg. There is no route for it by design: rotation needs no operator "
    "action and retries on every connect": (
        "un tramo de la rotación automática de certificados, enviado por la máquina de estados "
        "del propio núcleo al conectar, que limita a un dispositivo a una rotación a la vez y "
        "empareja la CSR que produce con el tramo de firma. No hay ninguna ruta para ello por "
        "diseño: la rotación no necesita ninguna acción del operador y se reintenta en cada "
        "conexión"
    ),
    "dispatched only by the patch module's restart sweep, for a device that owes a restart -- "
    "after patching, or booked by an operator from the device page -- and only inside its "
    "maintenance window. There is no route for it by design: an operator-chosen restart time "
    "would bypass the window": (
        "lo envía solo el barrido de reinicios del módulo de parches, para un dispositivo que "
        "debe un reinicio (tras aplicar parches, o reservado por un operador desde la página "
        "del dispositivo) y solo dentro de su ventana de mantenimiento. No hay ninguna ruta "
        "para ello por diseño: una hora de reinicio elegida por el operador se saltaría la "
        "ventana"
    ),
    "REPLACES AN ENDPOINT'S mTLS IDENTITY, and its params carry a certificate the kernel "
    "minted from that device's own CSR. There is no route for it by design: a route would take "
    "a certificate PEM straight from a request body -- any certificate, for any endpoint -- "
    "which is the exact attack automatic renewal closes": (
        "SUSTITUYE LA IDENTIDAD mTLS DE UN EQUIPO, y sus parámetros llevan un certificado que "
        "el núcleo emitió a partir de la CSR de ese mismo dispositivo. No hay ninguna ruta "
        "para ello por diseño: una ruta tomaría un certificado PEM directamente del cuerpo de "
        "una solicitud (cualquier certificado, para cualquier equipo), que es justo el ataque "
        "que la renovación automática cierra"
    ),
    "{job_type} cannot be re-run from the jobs route: a retry is a NEW job with a new id, and "
    "this module's record of the work is keyed on the OLD one -- so a retry from here would "
    "run on the endpoint and leave the module's record pointing at a job that will never speak "
    "again. Re-run it where that record lives.": (
        "{job_type} no se puede volver a ejecutar desde la ruta de tareas: un reintento es una "
        "tarea NUEVA con un id nuevo, y el registro del trabajo en este módulo está indexado "
        "por la ANTIGUA; por eso un reintento desde aquí se ejecutaría en el equipo y dejaría "
        "el registro del módulo apuntando a una tarea que nunca volverá a dar señales. Vuelve "
        "a ejecutarla donde vive ese registro."
    ),
    "{job_type} has no human dispatch route in this platform, so there is nothing to re-run it "
    "as.": (
        "{job_type} no tiene ninguna vía de envío por una persona en esta plataforma, así que "
        "no hay nada con lo que volver a ejecutarlo."
    ),
    # --- api ---
    "missing bearer token": "falta el token de portador",
    "unknown organization": "organización desconocida",
    "requires {code}": "requiere {code}",
    "X-Client-Id must be one client id": "X-Client-Id debe ser el id de un solo cliente",
    "this account can only use the portal": "esta cuenta solo puede usar el portal",
    "only a technician with access to every client can do this": (
        "solo un técnico con acceso a todos los clientes puede hacer esto"
    ),
    "unknown client": "cliente desconocido",
    "the parent group belongs to another client": "el grupo padre pertenece a otro cliente",
    "that site is archived": "esa sede está archivada",
    "that group belongs to another client": "ese grupo pertenece a otro cliente",
    "a preview needs the group's client: send client_id for a group that is not saved yet": (
        "una vista previa necesita el cliente del grupo: envía client_id para un grupo que "
        "aún no está guardado"
    ),
    "a role cannot be granted on the unassigned client": (
        "no se puede asignar un rol sobre el cliente «Sin asignar»"
    ),
    "that client is archived": "ese cliente está archivado",
    # --- clients, sites and contacts (MSP clients Task 9) ---
    "no such client": "no existe ese cliente",
    "no such contact": "no existe ese contacto",
    "{domain} is not an email domain": "{domain} no es un dominio de correo",
    "{modules} is not an optional module; the optional modules are {allowed}": (
        "{modules} no es un módulo opcional; los módulos opcionales son {allowed}"
    ),
    "a client needs a name": "un cliente necesita un nombre",
    "a site needs a name": "una sede necesita un nombre",
    "{email} is not an email address": "{email} no es una dirección de correo",
    "an email domain already belongs to another client": (
        "un dominio de correo ya pertenece a otro cliente"
    ),
    "a client with that name already exists": "ya existe un cliente con ese nombre",
    "the internal and unassigned clients cannot be archived": (
        "los clientes interno y «Sin asignar» no se pueden archivar"
    ),
    "this client has {count} active devices; retire them before archiving it": (
        "este cliente tiene {count} equipos activos; retíralos antes de archivarlo"
    ),
    "that client already has a site with that name": "ese cliente ya tiene una sede con ese nombre",
    "the unassigned client cannot have sites": "el cliente «Sin asignar» no puede tener sedes",
    "the Principal site of the organization's own client cannot be archived": (
        "la sede Principal del cliente propio de la organización no se puede archivar"
    ),
    "this site has {count} devices; a site with devices cannot be archived": (
        "esta sede tiene {count} equipos; una sede con equipos no se puede archivar"
    ),
    "that email address already belongs to another user of the organization": (
        "esa dirección de correo ya pertenece a otro usuario de la organización"
    ),
    "the unassigned client takes no email domains": (
        "el cliente «Sin asignar» no admite dominios de correo"
    ),
    "the unassigned client has no optional modules": (
        "el cliente «Sin asignar» no tiene módulos opcionales"
    ),
    "the unassigned client cannot have contacts": (
        "el cliente «Sin asignar» no puede tener contactos"
    ),
    "no such credential": "no existe esa credencial",
    "This credential is used only by the backup service and cannot be revealed.": (
        "Esta credencial solo la usa el servicio de copias de seguridad y no se puede revelar."
    ),
    "no such device": "no existe ese dispositivo",
    "no such suggestion": "no existe esa sugerencia",
    "this suggestion was already resolved as {resolution}": (
        "esta sugerencia ya se resolvió como {resolution}"
    ),
    "one of these records no longer exists": "uno de estos registros ya no existe",
    "{machine_owned} is reported by the agent on this device and would be overwritten by the "
    "next inventory run": (
        "{machine_owned} lo informa el agente de este dispositivo y la siguiente ejecución del "
        "inventario lo sobrescribiría"
    ),
    "both records have an agent; they are two enrolled machines": (
        "ambos registros tienen un agente; son dos equipos inscritos"
    ),
    "no such token": "no existe ese token",
    "no such agent": "no existe ese agente",
    "send exactly one of token or link_code": "envía exactamente uno de token o link_code",
    "enrollment rejected": "inscripción rechazada",
    "too many computers joined through this link in the last hour; try again later": (
        "demasiados equipos se unieron a través de este enlace en la última hora; inténtalo de "
        "nuevo más tarde"
    ),
    "error group not found": "no se encontró el grupo de errores",
    "unknown job type {job_type}": "tipo de tarea desconocido {job_type}",
    "no such user": "no existe ese usuario",
    "no fields supplied; send display_name, phone, timezone and/or locale": (
        "no se indicó ningún campo; envía display_name, phone, timezone y/o locale"
    ),
    "display_name cannot be null; send \"\" to clear it": (
        "display_name no puede ser null; envía \"\" para borrarlo"
    ),
    "settings.{key} must be a string": "settings.{key} debe ser una cadena de texto",
    "settings.{key} cannot be empty; send null to clear this organization's value and fall "
    "back to the platform default": (
        "settings.{key} no puede estar vacío; envía null para borrar el valor de esta "
        "organización y volver al predeterminado de la plataforma"
    ),
    "settings.{key} must be at most {max_length} characters": (
        "settings.{key} debe tener como máximo {max_length} caracteres"
    ),
    "settings.{key} must be one of {choices}": "settings.{key} debe ser uno de {choices}",
    "settings.brand_primary_color must be a #RRGGBB hex colour": (
        "settings.brand_primary_color debe ser un color hexadecimal #RRGGBB"
    ),
    "settings document is {supplied_bytes} bytes; the maximum is {max_settings_bytes}": (
        "el documento de configuración pesa {supplied_bytes} bytes; el máximo es "
        "{max_settings_bytes}"
    ),
    "unknown settings key(s): {unknown}; the settable keys are {settings_allowlist}": (
        "claves de configuración desconocidas: {unknown}; las claves que se pueden establecer "
        "son {settings_allowlist}"
    ),
    "merged settings document would be {merged_bytes} bytes; the maximum is "
    "{max_settings_bytes}": (
        "el documento de configuración resultante pesaría {merged_bytes} bytes; el máximo es "
        "{max_settings_bytes}"
    ),
    "no such organization": "no existe esa organización",
    "no fields supplied; send name and/or settings": (
        "no se indicó ningún campo; envía name y/o settings"
    ),
    "settings.{key} must be a boolean": "settings.{key} debe ser un booleano",
    "settings.{key} must be an integer": "settings.{key} debe ser un entero",
    "settings.{key} must be between {low} and {high}": (
        "settings.{key} debe estar entre {low} y {high}"
    ),
    "name cannot be null": "name no puede ser null",
    "settings cannot be null; send {{}} to change nothing": (
        "settings no puede ser null; envía {{}} para no cambiar nada"
    ),
    "The identity provider refused that (HTTP {status_code}).": (
        "El proveedor de identidad lo rechazó (HTTP {status_code})."
    ),
    "email settings cannot be changed on this installation yet: its identity provisioning is "
    "not configured. That is a server setting, not something this screen can fix.": (
        "la configuración del correo todavía no se puede cambiar en esta instalación: su "
        "aprovisionamiento de identidad no está configurado. Es un ajuste del servidor, no "
        "algo que esta pantalla pueda arreglar."
    ),
    "a relay password needs a sign-in username; leave both blank for a relay that needs no "
    "sign-in": (
        "una contraseña del servidor de correo necesita un nombre de usuario para iniciar "
        "sesión; deja ambos en blanco si el servidor no requiere iniciar sesión"
    ),
    "the relay password may be at most {max_password} characters": (
        "la contraseña del servidor de correo puede tener como máximo {max_password} caracteres"
    ),
    "a Microsoft sign-in needs a username: the mailbox the relay sends as": (
        "un inicio de sesión de Microsoft 365 necesita un nombre de usuario: el buzón desde el "
        "que envía el servidor de correo"
    ),
    "a Microsoft sign-in needs an application (client id); choose Password instead for a relay "
    "that signs in with one": (
        "un inicio de sesión de Microsoft 365 necesita una aplicación (id de cliente); elige "
        "Contraseña si el servidor de correo inicia sesión con una"
    ),
    "a Microsoft sign-in needs a token URL": (
        "un inicio de sesión de Microsoft 365 necesita una URL de token"
    ),
    "a Microsoft sign-in does not use a relay password; the saved one is removed when this is "
    "saved": (
        "un inicio de sesión de Microsoft 365 no usa contraseña del servidor de correo; la "
        "guardada se elimina al guardar esto"
    ),
    "a client secret is only used by a Microsoft sign-in, and needs an application (client id)": (
        "un secreto de cliente solo lo usa un inicio de sesión de Microsoft 365 y necesita una "
        "aplicación (id de cliente)"
    ),
    "the client secret may be at most {max_client_secret} characters": (
        "el secreto de cliente puede tener como máximo {max_client_secret} caracteres"
    ),
    "no fields supplied; send name and/or description": (
        "no se indicó ningún campo; envía name y/o description"
    ),
    "the identity provider could not create the first administrator. Nothing was created; the "
    "server log has the detail.": (
        "el proveedor de identidad no pudo crear al primer administrador. No se creó nada; el "
        "registro del servidor tiene el detalle."
    ),
    "The invitation email could not be sent, so no sign-in link has arrived. Email delivery is "
    "most likely not set up on this installation yet.": (
        "No se pudo enviar el correo de invitación, así que no ha llegado ningún enlace de "
        "acceso. Lo más probable es que el envío de correo aún no esté configurado en esta "
        "instalación."
    ),
    "that install code is not correct. It is printed in the server log at first start and "
    "stored beside this installation's other secrets.": (
        "ese código de instalación no es correcto. Se imprime en el registro del servidor en "
        "el primer arranque y se guarda junto a los demás secretos de esta instalación."
    ),
    "this installation cannot create accounts yet: its identity provisioning is not "
    "configured. That is a server setting, not something this screen can fix.": (
        "esta instalación todavía no puede crear cuentas: su aprovisionamiento de identidad no "
        "está configurado. Es un ajuste del servidor, no algo que esta pantalla pueda arreglar."
    ),
    "this installation is still starting up, so it cannot create accounts yet. Wait a moment "
    "and try again. If it says this for more than a few minutes, the installation did not "
    "finish preparing itself and needs attention from whoever set it up.": (
        "esta instalación todavía se está iniciando, así que aún no puede crear cuentas. "
        "Espera un momento e inténtalo de nuevo. Si sigue diciendo esto pasados unos minutos, "
        "la instalación no terminó de prepararse y necesita la atención de quien la configuró."
    ),
    "this installation has already been set up. Sign in, or ask one of its administrators to "
    "add you.": (
        "esta instalación ya está configurada. Inicia sesión, o pide a uno de sus "
        "administradores que te añada."
    ),
    "this installation is missing its built-in roles and cannot be set up.": (
        "a esta instalación le faltan sus roles integrados y no se puede configurar."
    ),
    "an organization with the identifier {slug} already exists on this installation.": (
        "ya existe en esta instalación una organización con el identificador {slug}."
    ),
    "waiting for review": "esperando revisión",
    # --- ipam ---
    "{address} is not a valid address": "{address} no es una dirección válida",
    "source must be one of {observation_sources}": "source debe ser uno de {observation_sources}",
    "method must be one of {observation_methods}": "method debe ser uno de {observation_methods}",
    "confidence must be one of {observation_confidence}": (
        "confidence debe ser uno de {observation_confidence}"
    ),
    "allocation_origin must be one of {observation_allocation_origins}": (
        "allocation_origin debe ser uno de {observation_allocation_origins}"
    ),
    "{mac} is not a valid MAC address": "{mac} no es una dirección MAC válida",
    "drift {drift_id} not found": "no se encontró el hallazgo de deriva {drift_id}",
    "note must be a string of at most {max_note} characters": (
        "note debe ser una cadena de como máximo {max_note} caracteres"
    ),
    "a note is required to suppress a finding: a silenced item with no reason is "
    "indistinguishable from one nobody looked at": (
        "para silenciar un hallazgo hace falta una nota: un elemento silenciado sin motivo no "
        "se distingue de uno que nadie miró"
    ),
    "note must be at most {max_note} characters": (
        "note debe tener como máximo {max_note} caracteres"
    ),
    "suppressed_until must be timezone-aware": "suppressed_until debe incluir zona horaria",
    "suppressed_until must be in the future": "suppressed_until debe estar en el futuro",
    "drift_class must be one of {drift_classes}": "drift_class debe ser uno de {drift_classes}",
    "{address} is not a valid IP address": "{address} no es una dirección IP válida",
    "allocation_method must be one of {allocation_methods}": (
        "allocation_method debe ser uno de {allocation_methods}"
    ),
    "the file is larger than {max_import_bytes} bytes": (
        "el archivo supera los {max_import_bytes} bytes"
    ),
    "a {{csv_column: ipam_field}} mapping is required": (
        "se necesita una correspondencia de columnas {{csv_column: ipam_field}}"
    ),
    "unknown ipam_field(s) {unknown}; the mapping may name only {import_fields}": (
        "ipam_field desconocidos {unknown}; la correspondencia de columnas solo puede nombrar "
        "{import_fields}"
    ),
    "the mapping must name a column for `address`": (
        "la correspondencia de columnas debe indicar una columna para `address`"
    ),
    "two columns map to the same field(s): {duplicated}": (
        "dos columnas se asignan al mismo campo: {duplicated}"
    ),
    "the digest does not match this file, mapping and VRF: preview and apply must be about "
    "the same file, the same column mapping AND the same VRF (§8.1)": (
        "el hash no coincide con este archivo, esta correspondencia de columnas y esta VRF: la "
        "vista previa y la aplicación deben tratar del mismo archivo, de la misma "
        "correspondencia de columnas Y de la misma VRF (§8.1)"
    ),
    "the file is not valid UTF-8: {exc}": "el archivo no es UTF-8 válido: {exc}",
    "the file has more than {max_import_rows} rows": (
        "el archivo tiene más de {max_import_rows} filas"
    ),
    "already assigned to device {device_id}": "ya está asignada al dispositivo {device_id}",
    "already assigned to {asset_label}": "ya está asignada a {asset_label}",
    "no cidr field": "no hay campo cidr",
    "no VLAN with vid {raw_vlan} in this client; vlan_id left null": (
        "no hay ninguna VLAN con vid {raw_vlan} en este cliente; vlan_id se deja en null"
    ),
    "gateway {raw_gateway} is not inside {prefix}; left null": (
        "la puerta de enlace {raw_gateway} no está dentro de {prefix}; se deja en null"
    ),
    "unparseable cidr: {exc}": "cidr no interpretable: {exc}",
    "prefix must be a non-empty string": "prefix debe ser una cadena no vacía",
    "gateway {gateway} is not a valid address": (
        "la puerta de enlace {gateway} no es una dirección válida"
    ),
    "gateway {parsed} is IPv{parsed_version} but the prefix is IPv{prefix_version}": (
        "la puerta de enlace {parsed} es IPv{parsed_version}, pero el prefijo es "
        "IPv{prefix_version}"
    ),
    "gateway {parsed} is not inside {prefix}": (
        "la puerta de enlace {parsed} no está dentro de {prefix}"
    ),
    "{prefix} overlaps the assignable subnet {clash_prefix} in this VRF; at most one "
    "subnet holding addresses may cover an address -- mark the wider one kind='container' "
    "if it exists only to make the tree read": (
        "{prefix} se solapa con la subred asignable {clash_prefix} de esta VRF; solo una "
        "subred que contenga direcciones puede cubrir una dirección -- marca la más amplia "
        "como kind='container' si existe únicamente para que el árbol se lea bien"
    ),
    "subnet {subnet_id} not found": "no se encontró la subred {subnet_id}",
    "kind must be one of {subnet_kinds}; got {kind}": (
        "kind debe ser uno de {subnet_kinds}; se recibió {kind}"
    ),
    "name must be a string of at most {max_name} characters": (
        "name debe ser una cadena de como máximo {max_name} caracteres"
    ),
    "{network} is already registered in this VRF": "{network} ya está registrada en esta VRF",
    "no fields supplied": "no se indicó ningún campo",
    "unknown or server-owned subnet field(s): {unknown}; editable fields are "
    "{subnet_editable_fields}": (
        "campos de subred desconocidos o reservados al servidor: {unknown}; los campos "
        "editables son {subnet_editable_fields}"
    ),
    "kind must be one of {subnet_kinds}; got {merged_kind}": (
        "kind debe ser uno de {subnet_kinds}; se recibió {merged_kind}"
    ),
    "subnet {prefix} still holds {holding}; delete with cascade to release them": (
        "la subred {prefix} todavía contiene {holding}; elimínala con cascade para liberar "
        "esos elementos"
    ),
    "kind must be one of {range_kinds}; got {kind}": (
        "kind debe ser uno de {range_kinds}; se recibió {kind}"
    ),
    "start_address and end_address must be valid addresses": (
        "start_address y end_address deben ser direcciones válidas"
    ),
    "start_address and end_address must be the same family": (
        "start_address y end_address deben ser de la misma familia"
    ),
    "range is IPv{start_version} but {network} is IPv{network_version}": (
        "el rango es IPv{start_version}, pero {network} es IPv{network_version}"
    ),
    "start_address {start} is after end_address {end}": (
        "start_address {start} es posterior a end_address {end}"
    ),
    "{start}-{end} overlaps the existing range {clash_start_address}-{clash_end_address} "
    "in this subnet": (
        "{start}-{end} se solapa con el rango existente "
        "{clash_start_address}-{clash_end_address} de esta subred"
    ),
    "range {range_id} not found": "no se encontró el rango {range_id}",
    "subnet {subnet_prefix} is a container: it holds no addresses (§0.2) and cannot be "
    "swept": (
        "la subred {subnet_prefix} es una subred contenedora: no contiene direcciones (§0.2) y "
        "no se puede barrer"
    ),
    "{literal} is not a valid prefix: {exc}": "{literal} no es un prefijo válido: {exc}",
    "documentation record {doc_record_id} not found": (
        "no se encontró el registro de documentación {doc_record_id}"
    ),
    "{contains} is not a valid address": "{contains} no es una dirección válida",
    "{endpoint} is outside {network}": "{endpoint} está fuera de {network}",
    "sweep_interval_hours must be an integer or null": (
        "sweep_interval_hours debe ser un entero o null"
    ),
    "sweep_interval_hours must be at least 1 (§0.10: a sub-hourly sweep interval is how an "
    "address plan becomes a ping monitor)": (
        "sweep_interval_hours debe ser al menos 1 (§0.10: un intervalo de barrido inferior a "
        "una hora es la forma en que un plan de direcciones se convierte en un monitor de ping)"
    ),
    "device {sweep_device_id} not found": "no se encontró el dispositivo {sweep_device_id}",
    "sweep_interval_hours must be at least {min_sweep_interval_hours} (§0.10: a sub-hourly "
    "sweep interval is how an address plan becomes a ping monitor)": (
        "sweep_interval_hours debe ser al menos {min_sweep_interval_hours} (§0.10: un "
        "intervalo de barrido inferior a una hora es la forma en que un plan de direcciones se "
        "convierte en un monitor de ping)"
    ),
    "mapping must be a JSON object of {{csv_column: ipam_field}}": (
        "la correspondencia de columnas debe ser un objeto JSON de {{csv_column: ipam_field}}"
    ),
    "mapping must be a JSON object: {exc}": (
        "la correspondencia de columnas debe ser un objeto JSON: {exc}"
    ),
    "range {range_id} not found in this subnet": (
        "no se encontró el rango {range_id} en esta subred"
    ),
    "already an asset": "ya es un activo",
    "not a candidate: no current observation for this address": (
        "no es candidata: no hay ninguna observación actual de esta dirección"
    ),
    "VRF": "VRF",
    "VLAN": "VLAN",
    "{field} must be a non-empty string": "{field} debe ser una cadena no vacía",
    "{field} must be at most {maximum} characters": (
        "{field} debe tener como máximo {maximum} caracteres"
    ),
    "unknown or server-owned {noun} field(s): {unknown}; editable fields are {editable}": (
        "campos de {noun} desconocidos o reservados al servidor: {unknown}; los campos "
        "editables son {editable}"
    ),
    "VRF {vrf_id} not found": "no se encontró la VRF {vrf_id}",
    "could not create the default VRF": "no se pudo crear la VRF predeterminada",
    "a VRF named {name} already exists in this client": (
        "ya existe en este cliente una VRF llamada {name}"
    ),
    "site {site_id} not found": "no se encontró la sede {site_id}",
    "VLAN {vlan_id} not found": "no se encontró la VLAN {vlan_id}",
    "vid must be an integer; got {vid}": "vid debe ser un entero; se recibió {vid}",
    "vid must be between 1 and 4094; got {vid}": "vid debe estar entre 1 y 4094; se recibió {vid}",
    "at site {site_id}": "en la sede {site_id}",
    "without a site in this client": "sin sede en este cliente",
    "VLAN {vid} already exists {scope}": "la VLAN {vid} ya existe {scope}",
    "a {noun} named {name} already exists in this client": (
        "ya existe en este cliente un registro de {noun} llamado {name}"
    ),
    # IPAM per client (plan 2026-10-26 Task 10).
    "name the client: a row with no VRF or site takes the one the request names": (
        "indica el cliente: un registro sin VRF ni sede pertenece al que nombra la petición"
    ),
    "client_id {claimed} contradicts the client of the VRF or site named": (
        "client_id {claimed} contradice el cliente de la VRF o la sede indicadas"
    ),
    "IPAM records cannot be filed under Unassigned": (
        "los registros de IPAM no pueden pertenecer a «Sin asignar»"
    ),
    "site {site_id} belongs to another client": "la sede {site_id} es de otro cliente",
    "VLAN {vlan_id} belongs to another client": "la VLAN {vlan_id} es de otro cliente",
    # Sweeps, candidates and promotion inside the client (plan 2026-10-26 Task 11).
    "that machine belongs to another client": "ese equipo es de otro cliente",
    "assign the subnet to a site before adding its machines": (
        "asigna una sede a la subred antes de añadir sus equipos"
    ),
    "this address is in no registered subnet: register its subnet, at a site, before adding "
    "the machine": (
        "esta dirección no está en ninguna subred registrada: registra su subred, con una sede, "
        "antes de añadir el equipo"
    ),
    "{count} subnet": "{count} subred",
    "{count} subnets": "{count} subredes",
    "{count} VLAN": "{count} VLAN",
    "{count} VLANs": "{count} VLAN",
    "{count} address": "{count} dirección",
    "{count} addresses": "{count} direcciones",
    "{count} range": "{count} rango",
    "{count} ranges": "{count} rangos",
    "{count} observation": "{count} observación",
    "{count} observations": "{count} observaciones",
    "{count} drift finding": "{count} hallazgo de deriva",
    "{count} drift findings": "{count} hallazgos de deriva",
    "{count} scan authorization": "{count} autorización de escaneo",
    "{count} scan authorizations": "{count} autorizaciones de escaneo",
    "{count} sweep run": "{count} ejecución de barrido",
    "{count} sweep runs": "{count} ejecuciones de barrido",
    "{noun} {label} is still referenced by {blocking}": (
        "{noun} {label}: todavía hay referencias desde {blocking}"
    ),
    "authorized_by is required: there is no system-authorized scope (§2.9). A human with "
    "ipam.scope.manage puts their name on every packet this module emits": (
        "authorized_by es obligatorio: no existe un ámbito autorizado por el sistema (§2.9). "
        "Una persona con ipam.scope.manage pone su nombre en cada paquete que emite este módulo"
    ),
    "expires_at must be timezone-aware": "expires_at debe incluir zona horaria",
    "{network} is already authorized in this VRF; revoke the existing authorization before "
    "creating another": (
        "{network} ya está autorizada en esta VRF; revoca la autorización existente antes de "
        "crear otra"
    ),
    "scan authorization {authorization_id} not found": (
        "no se encontró la autorización de escaneo {authorization_id}"
    ),
    "scan authorization {authorization_id} is already revoked": (
        "la autorización de escaneo {authorization_id} ya está revocada"
    ),
    "max_targets must be an integer": "max_targets debe ser un entero",
    "max_targets must be between 1 and {max_targets_ceiling}": (
        "max_targets debe estar entre 1 y {max_targets_ceiling}"
    ),
    "state must be one of {address_states} or None; got {state} -- `in_use`, `stale` and "
    "`conflicting` are DERIVED (§0.6), never stored": (
        "state debe ser uno de {address_states} o None; se recibió {state} -- `in_use`, "
        "`stale` y `conflicting` son DERIVADOS (§0.6) y nunca se almacenan"
    ),
    "subnet {subnet_prefix} is a container: it exists so the tree reads and holds no "
    "addresses (§0.2). Utilisation, next-free and allocation apply only to assignable "
    "subnets": (
        "la subred {subnet_prefix} es una subred contenedora: existe para que el árbol se lea "
        "bien y no contiene direcciones (§0.2). La utilización, la siguiente libre y la "
        "asignación solo se aplican a subredes asignables"
    ),
    "count must be between 1 and {max_next_free}": "count debe estar entre 1 y {max_next_free}",
    "subnet too large to enumerate: {prefix} (§0.4's ceiling, and every IPv6 prefix -- "
    "§0.5)": (
        "la subred es demasiado grande para enumerarla: {prefix} (el límite de §0.4 y todos "
        "los prefijos IPv6 -- §0.5)"
    ),
    "an assignment needs exactly one claimant: device_id XOR asset_label (both, or "
    "neither, is how 'assigned to nothing' gets into a plan of record)": (
        "una asignación necesita exactamente un titular: device_id XOR asset_label (con ambos, "
        "o con ninguno, es como «asignada a nadie» entra en un plan de registro)"
    ),
    "{address} is not an address inside {prefix}": (
        "{address} no es una dirección dentro de {prefix}"
    ),
    "subnet {prefix} is a container and holds no addresses": (
        "la subred {prefix} es una subred contenedora y no contiene direcciones"
    ),
    "{parsed} is the network or broadcast address of {prefix} and cannot be assigned": (
        "{parsed} es la dirección de red o de difusión de {prefix} y no se puede asignar"
    ),
    "{parsed} is inside a dhcp_pool range: shrink the pool or carve a reserved range out "
    "of it, then assign": (
        "{parsed} está dentro de un rango dhcp_pool: reduce el pool o saca de él un rango "
        "reservado, y luego asígnala"
    ),
    "{parsed} has an unresolved conflict finding: resolve, acknowledge or suppress it "
    "before assigning": (
        "{parsed} tiene un hallazgo de conflicto sin resolver: resuélvelo, reconócelo o "
        "siléncialo antes de asignarla"
    ),
    "no free address remains in {subnet_prefix}": (
        "no queda ninguna dirección libre en {subnet_prefix}"
    ),
    "limit must be between 1 and 1024": "limit debe estar entre 1 y 1024",
    "offset must be a non-negative integer": "offset debe ser un entero no negativo",
    "device {device_id} not found": "no se encontró el dispositivo {device_id}",
    "{parsed} is already assigned; release it before assigning it again": (
        "{parsed} ya está asignada; libérala antes de volver a asignarla"
    ),
    "trigger must be one of {sweep_triggers}": "trigger debe ser uno de {sweep_triggers}",
    "subnet {subnet_prefix} is a container: it exists so the tree reads and holds no "
    "addresses (§0.2), so there is nothing in it to sweep": (
        "la subred {subnet_prefix} es una subred contenedora: existe para que el árbol se lea "
        "bien y no contiene direcciones (§0.2), así que no hay nada que barrer en ella"
    ),
    "subnet too large to enumerate: {prefix} -- an ICMP sweep of an IPv6 prefix is not a "
    "hard query, it is a category error (§0.5). IPv6 is stored, documented and reconciled; "
    "it is never enumerated": (
        "la subred es demasiado grande para enumerarla: {prefix} -- un barrido ICMP de un "
        "prefijo IPv6 no es una consulta difícil, es un error de categoría (§0.5). IPv6 se "
        "almacena, se documenta y se concilia; nunca se enumera"
    ),
    "limit must be between 1 and 200": "limit debe estar entre 1 y 200",
    "no active scan authorization covers {subnet_prefix} in this VRF (§0.12: deny by "
    "default -- documenting a subnet authorizes nothing). A holder of ipam.scope.manage "
    "must authorize the prefix first": (
        "ninguna autorización de escaneo activa cubre {subnet_prefix} en esta VRF (§0.12: se "
        "deniega por defecto -- documentar una subred no autoriza nada). Quien tenga "
        "ipam.scope.manage debe autorizar primero el prefijo"
    ),
    "address {address_id} not found": "no se encontró la dirección {address_id}",
    "{parsed} is inside no registered subnet in this VRF -- register the subnet first (an "
    "observed address with no subnet is an orphan, §4.2, but a documented one is a typo)": (
        "{parsed} no está dentro de ninguna subred registrada en esta VRF -- registra antes la "
        "subred (una dirección observada sin subred es huérfana, §4.2, pero una documentada es "
        "un error de escritura)"
    ),
    "{parsed} falls in the container subnet {subnet_prefix}, which holds no addresses "
    "(§0.2). Register the assignable subnet inside it first": (
        "{parsed} cae en la subred contenedora {subnet_prefix}, que no contiene direcciones "
        "(§0.2). Registra antes la subred asignable que hay dentro"
    ),
    "{parsed} is the network or broadcast address of {prefix} and cannot be reserved or "
    "assigned": (
        "{parsed} es la dirección de red o de difusión de {prefix} y no se puede reservar ni "
        "asignar"
    ),
    "{canonical} already has a record in this VRF": "{canonical} ya tiene un registro en esta VRF",
    "unknown or server-owned address field(s): {unknown}; editable fields are "
    "{address_editable_fields} -- `state` changes go through "
    "assign/reserve/release/deprecate so §3.2's predicates and §11.3's audit shape cannot "
    "be bypassed": (
        "campos de dirección desconocidos o reservados al servidor: {unknown}; los campos "
        "editables son {address_editable_fields} -- los cambios de `state` pasan por "
        "assign/reserve/release/deprecate para que los predicados de §3.2 y la forma de "
        "auditoría de §11.3 no se puedan eludir"
    ),
    "this address is claimed by a device; releasing it is how the claimant changes": (
        "esta dirección la reclama un dispositivo; liberarla es la forma de cambiar de titular"
    ),
    "{canonical} is deprecated ('we know this is gone'); release it first if it is back in "
    "service -- §3.2 permits `assigned` only from absent, free or reserved": (
        "{canonical} está dada de baja («sabemos que ya no existe»); libérala primero si ha "
        "vuelto a estar en servicio -- §3.2 permite `assigned` solo desde ausente, libre o "
        "reservada"
    ),
    "{canonical} is inside a dhcp_pool range: shrink the pool or carve a reserved range "
    "out of it, then reserve": (
        "{canonical} está dentro de un rango dhcp_pool: reduce el pool o saca de él un rango "
        "reservado, y luego reserva"
    ),
    "{row_address} is already free": "{row_address} ya está libre",
    "search query must not be empty": "la consulta de búsqueda no puede estar vacía",
    # --- health ---
    "approved action {action_key} is inactive": "la acción aprobada {action_key} está inactiva",
    "approved action {action_key} names job type {action_job_type}, which is no longer "
    "registered": (
        "la acción aprobada {action_key} nombra el tipo de tarea {action_job_type}, que ya no "
        "está registrado"
    ),
    "approved action {action_key} does not carry the 'health' persona": (
        "la acción aprobada {action_key} no lleva la persona 'health'"
    ),
    "approved action {action_key} names job type {action_job_type}, which has no human "
    "dispatch route in this platform -- there is no permission that would authorize "
    "running it, so it cannot be dispatched from here either": (
        "la acción aprobada {action_key} nombra el tipo de tarea {action_job_type}, que no "
        "tiene ninguna vía de envío por una persona en esta plataforma -- no existe ningún "
        "permiso que autorice ejecutarlo, así que tampoco se puede enviar desde aquí"
    ),
    "dispatching approved action {action_key} additionally requires {required} -- the same "
    "permission {action_job_type}'s own module route requires. health.remediate is the "
    "floor, not the whole check": (
        "para enviar la acción aprobada {action_key} se necesita además {required} -- el mismo "
        "permiso que exige la ruta del propio módulo de {action_job_type}. health.remediate es "
        "el mínimo, no la comprobación completa"
    ),
    "approved action {action_key} pins script {script_id}, which is gone": (
        "la acción aprobada {action_key} fija el script {script_id}, que ya no existe"
    ),
    "approved action {action_key} pins script version {script_version} but the live script "
    "is version {live}; it was edited since it was approved": (
        "la acción aprobada {action_key} fija la versión {script_version} del script, pero la "
        "versión actual es la {live}; se editó después de aprobarse"
    ),
    "health alert action {action_id} not found": (
        "no se encontró la acción de la alerta de salud {action_id}"
    ),
    "action {action_id} is a {row_kind}, not a remediation": (
        "la acción {action_id} es de tipo {row_kind}, no una corrección"
    ),
    "action {action_id} is {row_state}; only an awaiting_approval remediation can be "
    "approved": (
        "la acción {action_id} está {row_state}; solo se puede aprobar una corrección en "
        "estado awaiting_approval"
    ),
    "action {action_id}'s approval window has expired": (
        "la ventana de aprobación de la acción {action_id} ha caducado"
    ),
    "action {action_id} is {row_state}; only an awaiting_approval remediation can be "
    "refused": (
        "la acción {action_id} está {row_state}; solo se puede rechazar una corrección en "
        "estado awaiting_approval"
    ),
    "health alert {alert_id} not found": "no se encontró la alerta de salud {alert_id}",
    "approved action {action_key} not found": "no se encontró la acción aprobada {action_key}",
    "alert {alert_id} already has a remediation in state {row_state}; one remediation "
    "dispatch per alert, forever": (
        "la alerta {alert_id} ya tiene una corrección en estado {row_state}; solo se envía una "
        "corrección por alerta, para siempre"
    ),
    "alert {alert_id}'s pending remediation approval expired at {expires_at}; it cannot be "
    "answered by dispatching {action_key}": (
        "la aprobación pendiente de la corrección de la alerta {alert_id} caducó el "
        "{expires_at}; no se puede responder enviando {action_key}"
    ),
    "approved action {action_id} not found": "no se encontró la acción aprobada {action_id}",
    "unknown or non-writable approved-action field(s): {unknown}; `destructive` is DERIVED "
    "from job_type/params/exemption and is never accepted from a request body (spec §8.4)": (
        "campos de acción aprobada desconocidos o no escribibles: {unknown}; `destructive` se "
        "DERIVA de job_type, params y exención y nunca se acepta del cuerpo de una solicitud "
        "(especificación §8.4)"
    ),
    "job_type is required": "job_type es obligatorio",
    "job_type {job_type} is not a registered job type; known types are {registered_names}": (
        "job_type {job_type} no es un tipo de tarea registrado; los tipos conocidos son "
        "{registered_names}"
    ),
    "job_type {job_type} may not be named by an approved action: {refusal}": (
        "una acción aprobada no puede nombrar el job_type {job_type}: {refusal}"
    ),
    "params_template may not carry a credential: key(s) {secret_keys} name a secret, and a "
    "params_template is plaintext JSONB on a config row that every holder of `health.read` "
    "can list. Stored credentials live encrypted in the vault and are decrypted only at "
    "point of use (invariant #3)": (
        "params_template no puede llevar una credencial: las claves {secret_keys} nombran un "
        "secreto, y un params_template es JSONB en texto plano en una fila de configuración "
        "que puede listar cualquier titular de `health.read`. Las credenciales almacenadas "
        "viven cifradas en la bóveda y solo se descifran en el momento de usarlas (invariante "
        "n.º 3)"
    ),
    "approved action {key} is still declared as the remediation of {declaring} health "
    "check(s); clear it from them first: {check_ids}": (
        "la acción aprobada {key} sigue declarada como corrección de {declaring} "
        "comprobación(es) de estado; quítala primero de ellas: {check_ids}"
    ),
    "approved action {key} has been dispatched and its history cannot be destroyed; "
    "deactivate it instead (active=false). Alerts still referencing it include: {alert_ids}": (
        "la acción aprobada {key} se ha enviado y su historial no se puede destruir; "
        "desactívala en su lugar (active=false). Entre las alertas que aún la referencian "
        "están: {alert_ids}"
    ),
    "approved action {action_key} is {action_job_type}; only script.run actions carry a "
    "script exemption": (
        "la acción aprobada {action_key} es {action_job_type}; solo las acciones script.run "
        "llevan una exención de script"
    ),
    "script {action_script_id} does not resolve in this org": (
        "el script {action_script_id} no existe en esta organización"
    ),
    "approved action {action_key} pins script version {action_script_version} but the live "
    "script is version {live}; exempting a version that has already been edited away is a "
    "no-op that reads as a grant": (
        "la acción aprobada {action_key} fija la versión {action_script_version} del script, "
        "pero la versión actual es la {live}; eximir una versión que ya se editó es una "
        "operación sin efecto que parece una concesión"
    ),
    "approved action {action_key} is script.run but carries no pin": (
        "la acción aprobada {action_key} es script.run pero no lleva ninguna fijación"
    ),
    "approved action {action_key} is {action_job_type} and carries no script pin, so it "
    "has no exemption to revoke": (
        "la acción aprobada {action_key} es {action_job_type} y no lleva fijación de script, "
        "así que no tiene exención que revocar"
    ),
    "no script exemption exists for {action_script_id} v{action_script_version}": (
        "no existe ninguna exención de script para {action_script_id} v{action_script_version}"
    ),
    "{column} must be one of {allowed}; got {value}": (
        "{column} debe ser uno de {allowed}; se recibió {value}"
    ),
    "{column} must be an integer; got {value}": "{column} debe ser un entero; se recibió {value}",
    "{column} must be at most {ceiling}; got {value}. {why}": (
        "{column} debe ser como máximo {ceiling}; se recibió {value}. {why}"
    ),
    "params_template must be empty for job_type {job_type}: the dispatcher builds "
    "ScriptRunParams from the PINNED scripts row and never from the template, so anything "
    "here is silently ignored at dispatch (spec §8.2). Got keys {rendered}": (
        "params_template debe estar vacío para el job_type {job_type}: al enviar la tarea se "
        "construye ScriptRunParams a partir de la fila de scripts FIJADA y nunca a partir de "
        "la plantilla, así que todo lo que haya aquí se ignora en silencio (especificación "
        "§8.2). Claves recibidas: {rendered}"
    ),
    "params_template is not valid for job_type {job_type}: {exc}. An approved action "
    "stored with params its job type refuses is not a bad row, it is a poison pill: the "
    "dispatcher raises at 03:00 inside the org's sweep transaction and rolls back that "
    "tenant's whole cycle, every 30s": (
        "params_template no es válido para el job_type {job_type}: {exc}. Una acción aprobada "
        "guardada con parámetros que su tipo de tarea rechaza no es una fila errónea, es una "
        "píldora envenenada: el envío falla a las 03:00 dentro de la transacción del barrido "
        "de la organización y revierte todo el ciclo de esa organización, cada 30 s"
    ),
    "script.run approved actions must pin both script_id and script_version: without the "
    "version, approving a script approves its future contents": (
        "las acciones aprobadas script.run deben fijar script_id y script_version: sin la "
        "versión, aprobar un script aprueba su contenido futuro"
    ),
    "script {script_id} does not resolve in this org": (
        "el script {script_id} no existe en esta organización"
    ),
    "script_id/script_version are only valid for job_type='script.run', got {job_type}": (
        "script_id y script_version solo son válidos para job_type='script.run'; se recibió "
        "{job_type}"
    ),
    "it is destructive": "es destructiva",
    "its risk_class is 'high'": "su risk_class es 'high'",
    "its max_target_scope is {scope}": "su max_target_scope es {scope}",
    "autonomy='autonomous' is refused because {reasons}": (
        "autonomy='autonomous' se rechaza porque {reasons}"
    ),
    "{column} must be at least {floor}; got {value}": (
        "{column} debe ser al menos {floor}; se recibió {value}"
    ),
    "{column} must not be negative; got {value}": (
        "{column} no puede ser negativo; se recibió {value}"
    ),
    " and ": " y ",
    "no settings supplied": "no se indicó ninguna configuración",
    "unknown health settings field(s): {unknown}; valid fields are {valid_fields}": (
        "campos de configuración de salud desconocidos: {unknown}; los campos válidos son "
        "{valid_fields}"
    ),
    "auto_resolve_alert_tickets must be a boolean": (
        "auto_resolve_alert_tickets debe ser un booleano"
    ),
    "{field} must be an integer; got {value}": "{field} debe ser un entero; se recibió {value}",
    "{field} must be between {low} and {high}; got {value}": (
        "{field} debe estar entre {low} y {high}; se recibió {value}"
    ),
    "monitoring_deep_link_template must be a string of at most {max_deep_link_length} "
    "characters": (
        "monitoring_deep_link_template debe ser una cadena de como máximo "
        "{max_deep_link_length} caracteres"
    ),
    "{field} cannot be null": "{field} no puede ser null",
    "default_alert_queue_id {queue_id} does not resolve in this org": (
        "default_alert_queue_id {queue_id} no existe en esta organización"
    ),
    "default_ticket_category_id {category_id} does not resolve in this org": (
        "default_ticket_category_id {category_id} no existe en esta organización"
    ),
    "health profile {profile_id} not found": "no se encontró el perfil de estado {profile_id}",
    "posture_interval_s ({posture_interval_s}) must be at least tick_interval_s "
    "({tick_interval_s}); the posture sub-tick is the SLOW one": (
        "posture_interval_s ({posture_interval_s}) debe ser al menos tick_interval_s "
        "({tick_interval_s}); el subciclo de postura es el LENTO"
    ),
    "name must be a non-empty string": "name debe ser una cadena no vacía",
    "name must be at most {max_name} characters": (
        "name debe tener como máximo {max_name} caracteres"
    ),
    "a health profile named {name} already exists in this org": (
        "ya existe en esta organización un perfil de estado llamado {name}"
    ),
    "unknown or server-owned profile field(s): {unknown}; editable fields are "
    "{profile_editable_fields}": (
        "campos de perfil desconocidos o reservados al servidor: {unknown}; los campos "
        "editables son {profile_editable_fields}"
    ),
    "health profile {profile_id} is still assigned; delete these assignments first: "
    "{assignment_ids}": (
        "el perfil de estado {profile_id} sigue asignado; elimina primero estas asignaciones: "
        "{assignment_ids}"
    ),
    "health profile {profile_id} has alerts and cannot be deleted; deactivate it instead "
    "(active=false). Most recent alerts: {alert_ids}": (
        "el perfil de estado {profile_id} tiene alertas y no se puede eliminar; desactívalo en "
        "su lugar (active=false). Alertas más recientes: {alert_ids}"
    ),
    "unknown check field(s): {unknown}": "campos de comprobación desconocidos: {unknown}",
    "check_type {check_type} is not in the closed registry (spec 4.1); adding one is a "
    "code change in both repos": (
        "check_type {check_type} no está en el registro cerrado (especificación 4.1); añadir "
        "uno es un cambio de código en ambos repositorios"
    ),
    "selector must be a string of at most {max_selector} chars": (
        "selector debe ser una cadena de como máximo {max_selector} caracteres"
    ),
    "{check_type} takes no selector; got {selector}": (
        "{check_type} no admite selector; se recibió {selector}"
    ),
    "comparator {comparator} is not legal for {check_type} ({value_kind}); legal "
    "comparators are {legal_comparators}": (
        "el comparador {comparator} no es válido para {check_type} ({value_kind}); los "
        "comparadores válidos son {legal_comparators}"
    ),
    "{check_type} is {value_kind}: it takes exactly {wanted} and nothing else; got "
    "{supplied}": (
        "{check_type} es {value_kind}: admite exactamente {wanted} y nada más; se recibió "
        "{supplied}"
    ),
    "{check_type} requires a boolean threshold_bool": (
        "{check_type} requiere un threshold_bool booleano"
    ),
    "supply for_ticks or for_seconds, not both -- two answers to one question": (
        "indica for_ticks o for_seconds, no ambos -- dos respuestas a una misma pregunta"
    ),
    "for_ticks must be between 1 and 60; got {for_ticks}": (
        "for_ticks debe estar entre 1 y 60; se recibió {for_ticks}"
    ),
    "severity must be one of {severities}; got {severity}": (
        "severity debe ser uno de {severities}; se recibió {severity}"
    ),
    "enabled must be a boolean": "enabled debe ser un booleano",
    "a health profile may hold at most {max_checks_per_profile} checks (spec 0.11); got "
    "{count}": (
        "un perfil de estado puede contener como máximo {max_checks_per_profile} "
        "comprobaciones (especificación 0.11); se recibieron {count}"
    ),
    "an assignment names a device OR a group OR neither (the org default), never both": (
        "una asignación nombra un dispositivo O un grupo O ninguno (el valor predeterminado de "
        "la organización), nunca ambos"
    ),
    "a {level}-level health profile assignment already exists here: assignment "
    "{existing_id} points at profile {existing_profile_id}": (
        "ya existe aquí una asignación de perfil de estado de nivel {level}: la asignación "
        "{existing_id} apunta al perfil {existing_profile_id}"
    ),
    "health profile assignment {assignment_id} not found": (
        "no se encontró la asignación de perfil de estado {assignment_id}"
    ),
    "for_seconds must be a positive integer; got {for_seconds}": (
        "for_seconds debe ser un entero positivo; se recibió {for_seconds}"
    ),
    "os_family must be one of {os_families} or null; got {os_family}": (
        "os_family debe ser uno de {os_families} o null; se recibió {os_family}"
    ),
    "os_family {os_family} cannot answer {check_type} (spec 4.1); it is available on "
    "{available_on}. A check pinned to an OS that cannot answer it never evaluates.": (
        "os_family {os_family} no puede responder a {check_type} (especificación 4.1); está "
        "disponible en {available_on}. Una comprobación fijada a un sistema operativo que no "
        "puede responderla nunca se evalúa."
    ),
    "{field} must be a boolean; got {value}": "{field} debe ser un booleano; se recibió {value}",
    "approved action {action_id} does not resolve in this org; a check can only declare a "
    "remediation this org has already approved (spec 8.2)": (
        "la acción aprobada {action_id} no existe en esta organización; una comprobación solo "
        "puede declarar una corrección que esta organización ya haya aprobado (especificación "
        "8.2)"
    ),
    "approved action {key} does not carry the 'health' persona, so every remediation it "
    "produced would be skipped at dispatch (spec 7.1)": (
        "la acción aprobada {key} no lleva la persona 'health', así que todas las correcciones "
        "que produjera se omitirían al enviar (especificación 7.1)"
    ),
    "duplicate check {key} in one request; the same check against the same selector twice "
    "in one profile is a config mistake, not two checks": (
        "comprobación {key} duplicada en una misma solicitud; la misma comprobación con el "
        "mismo selector dos veces en un perfil es un error de configuración, no dos "
        "comprobaciones"
    ),
    "device_id {device_id} does not resolve in this org": (
        "device_id {device_id} no existe en esta organización"
    ),
    "device_group_id {device_group_id} does not resolve in this org": (
        "device_group_id {device_group_id} no existe en esta organización"
    ),
    "a health profile named {merged_name} already exists in this org": (
        "ya existe en esta organización un perfil de estado llamado {merged_name}"
    ),
    "remediate_approved_action_id must be an approved action's id, or null to declare no "
    "remediation; got {value}": (
        "remediate_approved_action_id debe ser el id de una acción aprobada, o null para no "
        "declarar ninguna corrección; se recibió {value}"
    ),
    "this device was already asked to sample inside the last {cooldown_s}s (job {job_id}); "
    "the report it is already producing answers this request": (
        "a este dispositivo ya se le pidió tomar una muestra en los últimos {cooldown_s} s "
        "(tarea {job_id}); el informe que ya está generando responde a esta solicitud"
    ),
    "{count} {job_type} job(s) have already been dispatched across this organization in "
    "the last hour (limit {limit})": (
        "ya se han enviado {count} tarea(s) {job_type} en toda la organización durante la última "
        "hora (límite {limit})"
    ),
    "{check_type} is {value_kind} and carries no threshold_bool": (
        "{check_type} es {value_kind} y no lleva threshold_bool"
    ),
    "{check_type} is numeric and carries no threshold_num": (
        "{check_type} es numérico y no lleva threshold_num"
    ),
    "cleared_reason must be one of {cleared_reasons}; got {reason}": (
        "cleared_reason debe ser uno de {cleared_reasons}; se recibió {reason}"
    ),
    "alert {alert_id} is {alert_state}; only an active alert can be acknowledged": (
        "la alerta {alert_id} está {alert_state}; solo se puede reconocer una alerta activa"
    ),
    "alert {alert_id} is {alert_state}; only an active or acknowledged alert can be muted": (
        "la alerta {alert_id} está {alert_state}; solo se puede silenciar una alerta activa o "
        "reconocida"
    ),
    "muted_until must be in the future": "muted_until debe estar en el futuro",
    "muted_until must be at most {max_mute_days} days after the alert opened ({limit})": (
        "muted_until debe ser como máximo {max_mute_days} días después de abrirse la alerta "
        "({limit})"
    ),
    "alert {alert_id} is already cleared": "la alerta {alert_id} ya está cerrada",
    "a cooldown longer than 30 days is indistinguishable from switching the action off, "
    "and this value is put straight into a timedelta at dispatch": (
        "un periodo de espera superior a 30 días no se distingue de desactivar la acción, y "
        "este valor se pasa directamente a un timedelta al enviar"
    ),
    "at the default 3600s cooldown a device can be reached at most 24 times a day; this "
    "bounds one endpoint's exposure at roughly four machine-decided jobs an hour": (
        "con el periodo de espera predeterminado de 3600 s, un dispositivo puede recibir como "
        "máximo 24 envíos al día; esto limita la exposición de un equipo a aproximadamente "
        "cuatro tareas decididas por una máquina cada hora"
    ),
    "the sweep dispatches up to 500 rows every 30s, so an unbounded value turns 'one job, "
    "one device' into 60,000 machine-decided jobs an hour for this tenant; an org may not "
    "be authorized for more in an HOUR than the sweep can emit in one CYCLE": (
        "el barrido envía hasta 500 filas cada 30 s, así que un valor sin límite convierte "
        "«una tarea, un dispositivo» en 60 000 tareas decididas por una máquina cada hora para "
        "esta organización; no se puede autorizar a una organización a más en una HORA de lo "
        "que el barrido puede emitir en un CICLO"
    ),
    # --- agent_update ---
    "a rollout has 1 to {max_rings} rings": "un despliegue tiene de 1 a {max_rings} anillos",
    "a device group may appear in one ring only": (
        "un grupo de dispositivos solo puede aparecer en un anillo"
    ),
    "ring 0 must be the pilot ring": "el anillo 0 debe ser el anillo piloto",
    "a ring names a device group that does not exist in this org": (
        "un anillo nombra un grupo de dispositivos que no existe en esta organización"
    ),
    "the pilot ring holds {pilot_total} devices; it may hold at most {pilot_max_devices}": (
        "el anillo piloto contiene {pilot_total} dispositivos; puede contener como máximo "
        "{pilot_max_devices}"
    ),
    "a rollback rollout must name the release it supersedes in `reason`": (
        "un despliegue de reversión debe nombrar en `reason` la versión a la que sustituye"
    ),
    "no published artefact exists for version {target_version}": (
        "no existe ningún artefacto publicado para la versión {target_version}"
    ),
    "no such rollout": "no existe ese despliegue",
    "ring {ring_ordinal} ({ring_name}) is not a pilot ring and version {version} is not "
    "promoted to stable (channel: {channel})": (
        "el anillo {ring_ordinal} ({ring_name}) no es un anillo piloto y la versión {version} "
        "no está promovida a estable (canal: {channel})"
    ),
    "rollout is {rollout_state}, not draft": "el despliegue está {rollout_state}, no en borrador",
    "this org already has an active rollout": "esta organización ya tiene un despliegue activo",
    "rollout is {rollout_state}, not running": "el despliegue está {rollout_state}, no en curso",
    "rollout is {rollout_state}, not paused": "el despliegue está {rollout_state}, no en pausa",
    "rollout is {rollout_state}; only a running rollout advances": (
        "el despliegue está {rollout_state}; solo un despliegue en curso avanza"
    ),
    "the active ring has not finished; close it or wait": (
        "el anillo activo no ha terminado; ciérralo o espera"
    ),
    "there is no further ring": "no hay más anillos",
    "no such ring in this rollout": "no existe ese anillo en este despliegue",
    "ring is {ring_state}; only the active ring can be closed": (
        "el anillo está {ring_state}; solo se puede cerrar el anillo activo"
    ),
    "rollout is {rollout_state}": "el despliegue está {rollout_state}",
    "no release rows for version {version}": "no hay filas de versión para la versión {version}",
    "no such agent release": "no existe esa versión del agente",
    "every ring needs a name": "todos los anillos necesitan un nombre",
    "only ring 0 may be the pilot ring": "solo el anillo 0 puede ser el anillo piloto",
    "only the last ring may take every remaining device": (
        "solo el último anillo puede abarcar todos los dispositivos restantes"
    ),
    "version {version} is not strict semver; agents cannot order it": (
        "la versión {version} no es semver estricta; los agentes no pueden ordenarla"
    ),
    "requires {permission}": "requiere {permission}",
    # --- agent_distribution ---
    "ITS_AGENT_RELEASE_PUBLIC_KEY is not set, so the agent the MSI would carry cannot be "
    "verified. Set it to the agent release's release-signing-key.pub.": (
        "ITS_AGENT_RELEASE_PUBLIC_KEY no está definida, así que no se puede verificar el "
        "agente que llevaría el MSI. Defínela con el release-signing-key.pub de la versión del "
        "agente."
    ),
    "ITS_AGENT_RELEASE_PUBLIC_KEY is not a base64 Ed25519 public key.": (
        "ITS_AGENT_RELEASE_PUBLIC_KEY no es una clave pública Ed25519 en base64."
    ),
    "The stored agent {release_version} for Windows ({label}) does not match its published "
    "SHA-256, so no MSI was built. Publish the release again.": (
        "El agente {release_version} almacenado para Windows ({label}) no coincide con su "
        "SHA-256 publicado, así que no se creó ningún MSI. Publica la versión de nuevo."
    ),
    "{version} is not a release version": "{version} no tiene el formato de una versión publicada",
    "{version} does not fit a Windows Installer product version (at most 255.255.65535)": (
        "{version} no cabe en una versión de producto de Windows Installer (como máximo "
        "255.255.65535)"
    ),
    "the console address must be a bare https origin: a host and an optional port": (
        "la dirección de la consola debe ser un origen https simple: un host y, opcionalmente, "
        "un puerto"
    ),
    "the install link code must be 32 URL-safe characters": (
        "el código del enlace de instalación debe tener 32 caracteres seguros para URL"
    ),
    "{name} is not installed; building the MSI needs msitools (wixl, msibuild, msiinfo)": (
        "{name} no está instalado; para crear el MSI hacen falta msitools (wixl, msibuild, "
        "msiinfo)"
    ),
    "building the MSI took longer than {seconds} seconds": (
        "crear el MSI tardó más de {seconds} segundos"
    ),
    "{name} did not finish within {seconds} seconds": "{name} no terminó en {seconds} segundos",
    "{name} failed with exit code {exit_code}: {stderr}": (
        "{name} falló con el código de salida {exit_code}: {stderr}"
    ),
    "the built MSI has no Template in its summary information": (
        "el MSI creado no tiene Template en su información de resumen"
    ),
    "the built MSI's custom actions are not the ones written: {actions}": (
        "las acciones personalizadas del MSI creado no son las que se escribieron: {actions}"
    ),
    "the built MSI schedules its custom actions differently from the source": (
        "el MSI creado programa sus acciones personalizadas de forma distinta a la del origen"
    ),
    "the built MSI has no {name} action": "el MSI creado no tiene la acción {name}",
    "the built MSI runs its custom actions outside the install script": (
        "el MSI creado ejecuta sus acciones personalizadas fuera del script de instalación"
    ),
    "the built MSI holds {files}, not the agent and its settings file": (
        "el MSI creado contiene {files}, y no el agente y su archivo de configuración"
    ),
    "the built MSI does not stop the its-agent service on install and removal": (
        "el MSI creado no detiene el servicio its-agent al instalar ni al desinstalar"
    ),
    "the built MSI is not marked {template}": "el MSI creado no está marcado como {template}",
    "there is no MSI for the {arch} architecture": "no hay ningún MSI para la arquitectura {arch}",
    "other MSIs are being built right now; try again in a minute": (
        "ahora mismo se están creando otros MSI; inténtalo de nuevo en un minuto"
    ),
    "ITS_PUBLIC_CONSOLE_URL is not set. The MSI takes the address computers join from it, "
    "never from the browser's request, because the file goes to every computer in the "
    "domain. Set it to the console's https address, such as https://itms.example.com.": (
        "ITS_PUBLIC_CONSOLE_URL no está definida. El MSI toma de ella la dirección a la que se "
        "unen los equipos, nunca de la solicitud del navegador, porque el archivo va a todos "
        "los equipos del dominio. Defínela con la dirección https de la consola, por ejemplo "
        "https://itms.example.com."
    ),
    "ITS_PUBLIC_CONSOLE_URL must be an https address, such as https://itms.example.com.": (
        "ITS_PUBLIC_CONSOLE_URL debe ser una dirección https, por ejemplo "
        "https://itms.example.com."
    ),
    "ITS_PUBLIC_CONSOLE_URL must be the console's bare https address, with no path, query "
    "or fragment, such as https://itms.example.com.": (
        "ITS_PUBLIC_CONSOLE_URL debe ser la dirección https simple de la consola, sin ruta, "
        "consulta ni fragmento, por ejemplo https://itms.example.com."
    ),
    "ITS_PUBLIC_CONSOLE_URL must not carry a user name, such as https://itms.example.com.": (
        "ITS_PUBLIC_CONSOLE_URL no debe llevar un nombre de usuario, por ejemplo "
        "https://itms.example.com."
    ),
    "The MSI needs ITMS to be reached by a DNS name, such as itms.example.com, not an IP "
    "address. Set ITS_PUBLIC_CONSOLE_URL to that name.": (
        "El MSI necesita que se acceda a ITMS mediante un nombre DNS, como itms.example.com, y "
        "no una dirección IP. Define ITS_PUBLIC_CONSOLE_URL con ese nombre."
    ),
    "Other MSIs are being built right now. Try again in a minute.": (
        "Ahora mismo se están creando otros MSI. Inténtalo de nuevo en un minuto."
    ),
    "The install link is off. Switch it on before downloading the MSI: computers join "
    "through it.": (
        "El enlace de instalación está desactivado. Actívalo antes de descargar el MSI: los "
        "equipos se unen a través de él."
    ),
    "That is not this organisation's current install link. Paste the link shown when it "
    "was last generated, or generate a new one.": (
        "Ese no es el enlace de instalación actual de esta organización. Pega el enlace que se "
        "mostró la última vez que se generó, o genera uno nuevo."
    ),
    "No stable, release-signed agent {msi_min_agent_version} or later is published for "
    "Windows ({arch_label}). Mark one stable first.": (
        "No hay publicado ningún agente estable y firmado de la versión "
        "{msi_min_agent_version} o posterior para Windows ({arch_label}). Marca uno como "
        "estable primero."
    ),
    "The MSI is built for amd64 (64-bit Intel or AMD) or arm64 (ARM).": (
        "El MSI se crea para amd64 (Intel o AMD de 64 bits) o arm64 (ARM)."
    ),
    "The agent {release_version} for Windows ({label}) does not carry a valid release "
    "signature, so no MSI was built.": (
        "El agente {release_version} para Windows ({label}) no lleva una firma de versión "
        "válida, así que no se creó ningún MSI."
    ),
    "The MSI could not be built: {reason}": "No se pudo crear el MSI: {reason}",
    "The agent {release_version} for Windows could not be read from object storage.": (
        "No se pudo leer el agente {release_version} para Windows desde el almacenamiento de "
        "objetos."
    ),
    "artifact body is empty": "el cuerpo del artefacto está vacío",
    "no release has that version": "ninguna versión tiene ese número",
    "this release has no artifact yet; upload one before generating an install command": (
        "esta versión todavía no tiene artefacto; sube uno antes de generar un comando de "
        "instalación"
    ),
    "this release has no artifact yet; upload one before building a deployment package": (
        "esta versión todavía no tiene artefacto; sube uno antes de crear un paquete de "
        "despliegue"
    ),
    "unknown distribution channel {body_channel}": (
        "canal de distribución desconocido {body_channel}"
    ),
    "a release already exists for this version, OS and architecture": (
        "ya existe una entrada de versión para este número, sistema operativo y arquitectura"
    ),
    "Download the artefact once": "Descarga el artefacto una vez",
    "This link is short-lived and authenticated by the grant that minted it. Download it "
    "here, on your own workstation — the endpoints never call it.": (
        "Este enlace es de corta duración y lo autentica la concesión que lo generó. "
        "Descárgalo aquí, en tu propia estación de trabajo: los equipos nunca lo llaman."
    ),
    "Verify what you downloaded": "Verifica lo que descargaste",
    "Against the SHA-256 this console computed over the bytes it stored. Do this before "
    "the file reaches a share, not after.": (
        "Contra el SHA-256 que esta consola calculó sobre los bytes que almacenó. Hazlo antes "
        "de que el archivo llegue a un recurso compartido, no después."
    ),
    "unknown distribution channel {channel}; expected one of {channels}": (
        "canal de distribución desconocido {channel}; se esperaba uno de {channels}"
    ),
    "A holder can enrol up to {max_uses} machines into this organization, for the next "
    "{hours} hours, until you revoke it.": (
        "Quien lo tenga puede inscribir hasta {max_uses} equipos en esta organización durante "
        "las próximas {hours} horas, hasta que lo revoques."
    ),
    "Each enrolment produces a device record and a device certificate: it appears in the "
    "fleet list and submits inventory this console will believe.": (
        "Cada inscripción produce un registro de dispositivo y un certificado de dispositivo: "
        "aparece en la lista del parque y envía un inventario que esta consola dará por válido."
    ),
    "A rogue device is a device somebody may later target. Any job whose signed parameters "
    "carry a secret delivers that secret to whatever answers as that device — this is the "
    "largest item on this list.": (
        "Un dispositivo falso es un dispositivo al que alguien podría dirigir tareas más "
        "adelante. Cualquier tarea cuyos parámetros firmados lleven un secreto entrega ese "
        "secreto a lo que responda como ese dispositivo: este es el punto más grave de esta "
        "lista."
    ),
    "It cannot forge a job, dispatch anything, read another device's data, promote itself "
    "to a site server, or mint another token.": (
        "No puede falsificar una tarea, enviar nada, leer los datos de otro dispositivo, "
        "promoverse a servidor de sitio ni crear otro token."
    ),
    "Watch the use count on the Enrollment page: {max_uses} uses for a smaller rollout "
    "hides the difference. Size this number to the machines you are actually deploying to.": (
        "Vigila el recuento de usos en la página de Inscripción: {max_uses} usos para un "
        "despliegue más pequeño oculta la diferencia. Ajusta este número a los equipos en los "
        "que realmente vas a desplegar."
    ),
    "Revoking the token on the Enrollment page kills every remaining use at once.": (
        "Revocar el token en la página de Inscripción anula de golpe todos los usos restantes."
    ),
    "Distribute with your own tooling": "Distribuir con tus propias herramientas",
    "Ansible, Salt, a kickstart, or a technician at the desk. Supply the token from your "
    "secret store (an Ansible no_log task, a vault lookup) and never from the artefact.": (
        "Ansible, Salt, un kickstart o un técnico en el puesto. Aporta el token desde tu "
        "almacén de secretos (una tarea Ansible con no_log, una consulta a la bóveda) y nunca "
        "desde el artefacto."
    ),
    "Anyone who can run code as SYSTEM on any domain-joined machine in scope can read this "
    "token — which in practice means any local administrator of any machine in the "
    "deployment.": (
        "Cualquiera que pueda ejecutar código como SYSTEM en cualquier equipo unido al dominio "
        "dentro del alcance puede leer este token, lo que en la práctica significa cualquier "
        "administrador local de cualquier equipo del despliegue."
    ),
    "Intune encrypts the payload at rest and runs it as SYSTEM on the assigned device, so "
    "reading this token needs local administrator on an assigned device, or Intune "
    "administrator.": (
        "Intune cifra la carga en reposo y la ejecuta como SYSTEM en el dispositivo asignado, "
        "así que leer este token requiere ser administrador local de un dispositivo asignado, "
        "o administrador de Intune."
    ),
    "Whoever can read the secret store you put this in can read this token.": (
        "Quien pueda leer el almacén de secretos donde lo pongas puede leer este token."
    ),
    "Stage it on a distribution point": "Colócalo en un punto de distribución",
    "A DFS or SYSVOL path every target machine account can read, and that only your "
    "administrators can write. Copy install.ps1 and its-agent-enroll.ps1 from "
    "agent/packaging/windows/ alongside it.": (
        "Una ruta DFS o SYSVOL que todas las cuentas de equipo de destino puedan leer y que "
        "solo tus administradores puedan escribir. Copia junto a él install.ps1 e "
        "its-agent-enroll.ps1 desde agent/packaging/windows/."
    ),
    "Assign it as a computer startup script": "Asígnalo como script de inicio del equipo",
    "Computer Configuration > Policies > Windows Settings > Scripts (Startup). Startup "
    "scripts run as SYSTEM, which is what the install needs. There is no MSI, so GPO's "
    "Software Installation policy is not the mechanism here — see the spec §5.3.": (
        "Configuración del equipo > Directivas > Configuración de Windows > Scripts (inicio o "
        "apagado). Los scripts de inicio se ejecutan como SYSTEM, que es lo que necesita la "
        "instalación. No hay MSI, así que la instalación de software de la directiva de grupo "
        "no es el mecanismo aquí; consulta la especificación §5.3."
    ),
    "Deliver the token separately": "Entrega el token por separado",
    "Mint a deployment token below and have the startup script read it from a location "
    "only machine accounts can read. It must never be in the script's arguments, and never "
    "in the package.": (
        "Crea abajo un token de despliegue y haz que el script de inicio lo lea desde un lugar "
        "que solo las cuentas de equipo puedan leer. Nunca debe ir en los argumentos del "
        "script ni en el paquete."
    ),
    "Wrap it as a Win32 app": "Empaquétalo como una aplicación Win32",
    "IntuneWinAppUtil over a folder holding the binary, install.ps1 and "
    "its-agent-enroll.ps1. Intune encrypts the payload at rest and delivers it to SYSTEM "
    "on the assigned device.": (
        "IntuneWinAppUtil sobre una carpeta que contenga el binario, install.ps1 e "
        "its-agent-enroll.ps1. Intune cifra la carga en reposo y la entrega como SYSTEM al "
        "dispositivo asignado."
    ),
    "Set the detection rule": "Define la regla de detección",
    "File exists: %ProgramFiles%\\ITSuite\\its-agent.exe. Do not detect on the enrolled "
    "state — an installed-but-unenrolled agent is a normal intermediate state on this path.": (
        "El archivo existe: %ProgramFiles%\\ITSuite\\its-agent.exe. No detectes por el estado de "
        "inscripción: un agente instalado pero sin inscribir es un estado intermedio normal en "
        "esta vía."
    ),
    "Mint a deployment token below and put it in the app's own script content or a linked "
    "key vault. It must never be in the package.": (
        "Crea abajo un token de despliegue y ponlo en el contenido del script de la propia "
        "aplicación o en una bóveda de claves vinculada. Nunca debe ir en el paquete."
    ),
    "Install the ITMS agent": "Instalar el agente de ITMS",
    "macOS and Linux": "macOS y Linux",
    "This installs the agent on the computer you are using now. It appears in ITMS within "
    "a minute and waits there until a technician approves it.": (
        "Esto instala el agente en el equipo que estás usando ahora. Aparece en ITMS en menos "
        "de un minuto y espera allí hasta que un técnico lo apruebe."
    ),
    "The Windows installer needs ITMS to be reached by a DNS name, such as "
    "itms.example.com, not an IP address: the installer learns where to join from the name "
    "of the file it downloads. Ask whoever runs ITMS to set one up.": (
        "El instalador de Windows necesita que se acceda a ITMS mediante un nombre DNS, como "
        "itms.example.com, y no una dirección IP: el instalador sabe a dónde unirse por el "
        "nombre del archivo que descarga. Pide a quien administra ITMS que configure uno."
    ),
    "Run this in a terminal on the computer:": "Ejecuta esto en una terminal del equipo:",
    "No macOS or Linux installer is available yet. Ask whoever runs ITMS to mark an agent "
    "release 1.6.0 or later as stable.": (
        "Todavía no hay ningún instalador de macOS ni de Linux disponible. Pide a quien "
        "administra ITMS que marque como estable una versión del agente 1.6.0 o posterior."
    ),
    "Open the downloaded file and approve the prompt. Keep the file's name as it is: it is "
    "how the installer knows where to join.": (
        "Abre el archivo descargado y acepta el aviso. No cambies el nombre del archivo: así "
        "es como el instalador sabe a dónde unirse."
    ),
    "No Windows installer is available yet. Ask whoever runs ITMS to mark a signed agent "
    "release 1.6.0 or later as stable.": (
        "Todavía no hay ningún instalador de Windows disponible. Pide a quien administra ITMS "
        "que marque como estable una versión firmada del agente 1.6.0 o posterior."
    ),
    "Download for Windows ({arch_label})": "Descargar para Windows ({arch_label})",
    "Download for Windows ({arch_label}) instead": (
        "Descargar para Windows ({arch_label}) en su lugar"
    ),
    "Verify the digest above once, on the file you downloaded, BEFORE you copy it to the "
    "distribution point. That comparison is the last one anybody makes.": (
        "Verifica una vez el hash de arriba, sobre el archivo que descargaste, ANTES de "
        "copiarlo al punto de distribución. Esa comparación es la última que hace nadie."
    ),
    "The endpoint does not verify anything. GPO software installation and Intune copy a "
    "file from a distribution point and run it; neither checks a digest, so the check that "
    "the one-machine install command performs on the endpoint does not happen on this path.": (
        "El equipo no verifica nada. La instalación de software por directiva de grupo e "
        "Intune copian un archivo desde un punto de distribución y lo ejecutan; ninguno "
        "comprueba un hash, así que la comprobación que el comando de instalación de un solo "
        "equipo hace en el equipo no se produce en esta vía."
    ),
    "What protects the file afterwards is the ACL on your distribution point. Anyone who "
    "can write to that share can replace this binary and neither this platform nor Windows "
    "will notice. Restrict write access to the administrators who manage it.": (
        "Lo que protege el archivo después es la ACL de tu punto de distribución. Quien pueda "
        "escribir en ese recurso compartido puede reemplazar este binario y ni esta plataforma "
        "ni Windows lo notarán. Restringe el acceso de escritura a los administradores que lo "
        "gestionan."
    ),
    "This binary is unsigned. There is no Authenticode signature to fall back on "
    "(agent/packaging/README.md, \"What is still unsigned\"), so the share's ACL is not one "
    "control among several — it is the only one.": (
        "Este binario no está firmado. No hay ninguna firma Authenticode a la que recurrir "
        "(agent/packaging/README.md, \"What is still unsigned\"), así que la ACL del recurso "
        "compartido no es un control más entre varios: es el único."
    ),
    "A person runs this command on the machine, so the endpoint-side digest check of the "
    "one-machine install path still happens here.": (
        "Una persona ejecuta este comando en el equipo, así que la comprobación del hash en el "
        "equipo de la vía de instalación de un solo equipo sigue ocurriendo aquí."
    ),
    "This binary is unsigned; the digest comparison is what stands in for a code signature "
    "(agent/packaging/README.md, \"What is still unsigned\").": (
        "Este binario no está firmado; la comparación del hash es lo que sustituye a una firma "
        "de código (agent/packaging/README.md, \"What is still unsigned\")."
    ),
    "64-bit Intel or AMD": "Intel o AMD de 64 bits",
    "ARM": "ARM",
    # --- agent_push ---
    "ITS_PUBLIC_CONSOLE_URL={raw} is not the console's bare https address (for example "
    "https://itms.example.com, with no path). {console_url_unset}": (
        "ITS_PUBLIC_CONSOLE_URL={raw} no es la dirección https simple de la consola (por "
        "ejemplo https://itms.example.com, sin ruta). {console_url_unset}"
    ),
    "a {credential_type} credential's secret must be a JSON object with \"username\" and "
    "\"secret\"": (
        "el secreto de una credencial {credential_type} debe ser un objeto JSON con \"username\" "
        "y \"secret\""
    ),
    "a {credential_type} credential's secret is missing a non-empty \"username\"": (
        "al secreto de una credencial {credential_type} le falta un \"username\" no vacío"
    ),
    "a {credential_type} credential's secret is missing a non-empty \"secret\"": (
        "al secreto de una credencial {credential_type} le falta un \"secret\" no vacío"
    ),
    "no published agent release exists for {os_name}/{arch}. A push has nothing to install "
    "until an artefact is published for the target's OS (spec §1.3 item 4). Publish one, "
    "or install the agent by hand": (
        "no existe ninguna versión del agente publicada para {os_name}/{arch}. Una instalación "
        "remota no tiene nada que instalar hasta que se publique un artefacto para el sistema "
        "operativo del destino (especificación §1.3, punto 4). Publica uno, o instala el "
        "agente a mano"
    ),
    "unknown push kind {kind}": "tipo de instalación remota desconocido {kind}",
    "an install requires an object store to presign the agent artefact; none was supplied "
    "to dispatch_push_batch": (
        "una instalación necesita un almacén de objetos para generar una URL prefirmada del "
        "artefacto del agente; no se proporcionó ninguno a dispatch_push_batch"
    ),
    "the promoted asset at {device_id} declares os_family {os_family}, which push cannot "
    "install onto. Supported: linux, macOS (SSH) and Windows (WinRM). Set the asset's OS": (
        "el activo promovido en {device_id} declara os_family {os_family}, sobre el que la "
        "instalación remota no puede instalar. Admitidos: Linux, macOS (SSH) y Windows "
        "(WinRM). Indica el sistema operativo del activo"
    ),
    "the promoted asset at {device_id} declares arch {arch}; push needs amd64 or arm64 to "
    "select the right artefact (§7.2)": (
        "el activo promovido en {device_id} declara arch {arch}; la instalación remota "
        "necesita amd64 o arm64 para elegir el artefacto correcto (§7.2)"
    ),
    "a {credential_type} credential's secret must be a JSON object {{\"username\": \"...\", "
    "\"secret\": \"...\"}}; the stored value is not JSON": (
        "el secreto de una credencial {credential_type} debe ser un objeto JSON {{\"username\": "
        "\"...\", \"secret\": \"...\"}}; el valor almacenado no es JSON"
    ),
    "a windows_domain credential's {key} must be a string when present": (
        "el campo {key} de una credencial windows_domain debe ser una cadena cuando está "
        "presente"
    ),
    "{canonical} is not one of this client's active push authorizations. A subnet "
    "push is built from one of those, exactly as authorized; a holder of "
    "agent.push.scope.manage authorizes a prefix": (
        "{canonical} no es una de las autorizaciones de instalación remota activas de este "
        "cliente. Una instalación remota por subred se construye a partir de una de "
        "ellas, exactamente como se autorizó; quien tenga agent.push.scope.manage autoriza un "
        "prefijo"
    ),
    "{canonical} holds {addresses} machines somebody vouched for, more than the "
    "{push_run_list_max} one list can show a person. Choose a narrower subnet": (
        "{canonical} contiene {addresses} equipos que alguien ha avalado, más de los "
        "{push_run_list_max} que una lista puede mostrar a una persona. Elige una subred más "
        "pequeña"
    ),
    "{canonical} expands to {ready} machines ready for a push; a run holds at most "
    "{push_run_max}. Choose a narrower subnet": (
        "{canonical} se expande a {ready} equipos listos para una instalación remota; una "
        "ejecución admite como máximo {push_run_max}. Elige una subred más pequeña"
    ),
    "the machines in this subnet changed since the list was shown, so the confirmation no "
    "longer describes them. Nothing was started; read the list again and confirm what it "
    "shows now": (
        "los equipos de esta subred cambiaron desde que se mostró la lista, así que la "
        "confirmación ya no los describe. No se inició nada; vuelve a leer la lista y confirma "
        "lo que muestra ahora"
    ),
    "no machine in this subnet is ready for a push, so there is nothing to confirm": (
        "ningún equipo de esta subred está listo para una instalación remota, así que no hay "
        "nada que confirmar"
    ),
    "this client already has a push run that is running, paused or stopped after "
    "sign-in failures: one active run per client. Let it finish, or cancel it, first": (
        "este cliente ya tiene una ejecución de instalación remota en curso, en pausa o "
        "detenida por fallos de inicio de sesión: solo se permite una ejecución activa por "
        "cliente. Deja que termine, o cancélala, primero"
    ),
    "push run {run_id} not found": "no se encontró la ejecución de instalación remota {run_id}",
    "only a running push run can be paused; this one is {run_state}": (
        "solo se puede poner en pausa una ejecución de instalación remota en curso; esta está "
        "{run_state}"
    ),
    "resuming a push run requires the `credentials.checkout` permission as well as "
    "`agent.push.run`: it presents a stored secret to the machines still waiting": (
        "reanudar una ejecución de instalación remota requiere el permiso "
        "`credentials.checkout` además de `agent.push.run`: presenta un secreto almacenado a "
        "los equipos que siguen esperando"
    ),
    " A cancelled run is final.": " Una ejecución cancelada es definitiva.",
    "only a paused push run, or one stopped after sign-in failures, can be resumed; this "
    "one is {run_state}.{final}": (
        "solo se puede reanudar una ejecución de instalación remota en pausa, o detenida por "
        "fallos de inicio de sesión; esta está {run_state}.{final}"
    ),
    "this push run is already {run_state}; cancelling is final and cannot be repeated": (
        "esta ejecución de instalación remota ya está {run_state}; cancelar es definitivo y no "
        "se puede repetir"
    ),
    "credential {credential_id} not found": "no se encontró la credencial {credential_id}",
    "credential {credential_id} is of type {credential_type}; a push may only use a "
    "credential of type {pushable_credential_types}. The credential is dedicated on "
    "purpose (spec §6.2): shared with remote control or provisioning, one theft costs "
    "every subsystem at once and rotation becomes impossible to reason about": (
        "la credencial {credential_id} es de tipo {credential_type}; una instalación remota "
        "solo puede usar una credencial de tipo {pushable_credential_types}. La credencial es "
        "dedicada a propósito (especificación §6.2): si se comparte con el control remoto o el "
        "aprovisionamiento, un solo robo cuesta todos los subsistemas a la vez y la rotación "
        "se vuelve imposible de razonar"
    ),
    "bound_by is required: there is no system-bound credential scope. A human with "
    "agent.push.scope.manage decides where a secret may be presented": (
        "bound_by es obligatorio: no existe un ámbito de credencial vinculado por el sistema. "
        "Una persona con agent.push.scope.manage decide dónde puede presentarse un secreto"
    ),
    "credential {credential_name} is already bound to {network}": (
        "la credencial {credential_name} ya está vinculada a {network}"
    ),
    "push credential scope {scope_id} not found": (
        "no se encontró el ámbito de credencial de instalación remota {scope_id}"
    ),
    "push credential scope {scope_id} is already unbound": (
        "el ámbito de credencial de instalación remota {scope_id} ya está desvinculado"
    ),
    "credential {credential_id} is dedicated to agent push install and may not be used for "
    "any other purpose (spec §6.2). It authenticates to machines nobody has enrolled, over "
    "a transport that cannot authenticate the target on first contact; sharing it means "
    "one theft costs every subsystem at once": (
        "la credencial {credential_id} es exclusiva de la instalación remota del agente y no "
        "puede usarse para ningún otro fin (especificación §6.2). Se autentica en equipos que "
        "nadie ha inscrito, por un transporte que no puede autenticar el destino en el primer "
        "contacto; compartirla supone que un solo robo cuesta todos los subsistemas a la vez"
    ),
    "this push was refused and nothing was created. Validation is all-or-nothing: an "
    "operator must not discover mid-batch that some of their targets were outside the "
    "authorization or lacked a confirmed host key": (
        "esta instalación remota se rechazó y no se creó nada. La validación es todo o nada: "
        "una persona no debe descubrir a mitad de un lote que algunos de sus destinos quedaban "
        "fuera de la autorización o carecían de una clave de host confirmada"
    ),
    "authorized_by is required: there is no system-authorized push scope (spec §3). A "
    "human with agent.push.scope.manage puts their name on every administrative login this "
    "platform causes": (
        "authorized_by es obligatorio: no existe un ámbito de instalación remota autorizado "
        "por el sistema (especificación §3). Una persona con agent.push.scope.manage pone su "
        "nombre en cada inicio de sesión administrativo que causa esta plataforma"
    ),
    "{network} is already authorized for push install in this client; revoke the "
    "existing authorization before creating another": (
        "{network} ya está autorizada para la instalación remota en este cliente; revoca "
        "la autorización existente antes de crear otra"
    ),
    "a push authorization cannot be filed under Unassigned": (
        "una autorización de instalación remota no se puede archivar en Sin asignar"
    ),
    "a push credential belongs to one client": (
        "una credencial de instalación remota pertenece a un solo cliente"
    ),
    "credential {credential_id} belongs to another client": (
        "la credencial {credential_id} es de otro cliente"
    ),
    "that device group belongs to another client": "ese grupo de dispositivos es de otro cliente",
    "a credential cannot be filed under Unassigned": (
        "una credencial no se puede archivar en Sin asignar"
    ),
    "push authorization {authorization_id} not found": (
        "no se encontró la autorización de instalación remota {authorization_id}"
    ),
    "push authorization {authorization_id} is already revoked": (
        "la autorización de instalación remota {authorization_id} ya está revocada"
    ),
    "no host key has been observed for {parsed}; run a probe first (agent.push_probe) so "
    "there is a fingerprint to confirm": (
        "no se ha observado ninguna clave de host de {parsed}; ejecuta antes un sondeo "
        "(agent.push_probe) para que haya una huella que confirmar"
    ),
    "{address} has a host key in more than one client; choose the client whose machine it "
    "is and try again": (
        "{address} tiene una clave de host en más de un cliente; elige el cliente al que "
        "pertenece el equipo y vuelve a intentarlo"
    ),
    "{parsed} was last probed over WinRM, which has no host key to confirm: a Windows push "
    "authenticates the target with Kerberos (design §2.3). Nothing needs confirming before "
    "a WinRM push": (
        "{parsed} se sondeó por última vez por WinRM, que no tiene ninguna clave de host que "
        "confirmar: una instalación remota en Windows autentica el destino con Kerberos "
        "(diseño §2.3). No hace falta confirmar nada antes de una instalación remota por WinRM"
    ),
    "the fingerprint you are confirming ({fingerprint}) is not the one most recently "
    "observed for {parsed} ({row_observed_fingerprint}). Confirm the key that is actually "
    "there, or re-probe if it has changed": (
        "la huella que estás confirmando ({fingerprint}) no es la observada más recientemente "
        "para {parsed} ({row_observed_fingerprint}). Confirma la clave que realmente está ahí, "
        "o vuelve a sondear si ha cambiado"
    ),
    "{parsed}'s host key {fingerprint} is already confirmed": (
        "la clave de host {fingerprint} de {parsed} ya está confirmada"
    ),
    "targets must be a list of IP address literals": (
        "targets debe ser una lista de direcciones IP literales"
    ),
    "a push must name at least one target address (invariant 2a clause (f): explicitly "
    "named addresses a human selected)": (
        "una instalación remota debe nombrar al menos una dirección de destino (invariante 2a, "
        "cláusula (f): direcciones nombradas explícitamente que una persona eligió)"
    ),
    "a push may name at most {push_batch_max} addresses in one act (got {targets}). The "
    "ceiling is one of four independent bounds on what a single mistaken click can do "
    "(spec §5.1)": (
        "una instalación remota puede nombrar como máximo {push_batch_max} direcciones en un "
        "solo acto (se recibieron {targets}). El límite es uno de cuatro límites "
        "independientes sobre lo que puede causar un solo clic equivocado (especificación §5.1)"
    ),
    "duplicate target addresses: {duplicates}. Every address in a push is confirmed "
    "individually by a human, so the same machine named twice is a list nobody read": (
        "direcciones de destino duplicadas: {duplicates}. Cada dirección de una instalación "
        "remota la confirma individualmente una persona, así que el mismo equipo nombrado dos "
        "veces es una lista que nadie leyó"
    ),
    "a push install requires the `credentials.checkout` permission as well as "
    "`agent.push.run`: a push presents a stored secret to a machine, and decrypting a "
    "stored secret is what `credentials.checkout` governs. Holding only `agent.push.run` "
    "would make this a side door around the credential module's own gate": (
        "una instalación remota requiere el permiso `credentials.checkout` además de "
        "`agent.push.run`: presenta un secreto almacenado a un equipo, y descifrar un secreto "
        "almacenado es lo que gobierna `credentials.checkout`. Tener solo `agent.push.run` "
        "sería una puerta lateral que esquiva la propia barrera del módulo de credenciales"
    ),
    "this push was refused and nothing was created. Validation is all-or-nothing: an "
    "operator must not discover mid-batch that some of their targets were outside the "
    "authorization": (
        "esta instalación remota se rechazó y no se creó nada. La validación es todo o nada: "
        "una persona no debe descubrir a mitad de un lote que algunos de sus destinos quedaban "
        "fuera de la autorización"
    ),
    "A Windows domain account is saved from the console's form.": (
        "Una cuenta de dominio de Windows se guarda desde el formulario de la consola."
    ),
    "A Windows domain account needs its password.": (
        "Una cuenta de dominio de Windows necesita su contraseña."
    ),
    "{upn_required} ({exc})": "{upn_required} ({exc})",
    "A Windows domain account is saved from the console's form (account, password, "
    "optional domain controller and CA certificate).": (
        "Una cuenta de dominio de Windows se guarda desde el formulario de la consola (cuenta, "
        "contraseña y, opcionalmente, controlador de dominio y certificado de CA)."
    ),
    "The domain controller and CA certificate, when given, are text.": (
        "El controlador de dominio y el certificado de CA, cuando se indican, son texto."
    ),
    "a target address must be a string IP literal": (
        "una dirección de destino debe ser una dirección IP literal en forma de cadena"
    ),
    "a target address must be a non-empty IP literal": (
        "una dirección de destino debe ser una dirección IP literal no vacía"
    ),
    "{literal} is not a single IP address. A push may be expressed only against explicitly "
    "named addresses a human selected -- never a CIDR, a hostname, a range, a group or a "
    "saved search (invariant 2a clause (f))": (
        "{literal} no es una dirección IP única. Una instalación remota solo puede expresarse "
        "contra direcciones nombradas explícitamente que una persona eligió -- nunca un CIDR, "
        "un nombre de host, un rango, un grupo ni una búsqueda guardada (invariante 2a, "
        "cláusula (f))"
    ),
    "Set ITS_PUBLIC_CONSOLE_URL so a pushed computer knows where to enroll.": (
        "Define ITS_PUBLIC_CONSOLE_URL para que un equipo al que se instala de forma remota "
        "sepa dónde inscribirse."
    ),
    "a Windows machine is pushed with Kerberos, which needs the computer's full DNS name, "
    "not its address. Set its DNS name on the machine's page (Push install details)": (
        "un equipo Windows recibe la instalación remota con Kerberos, que necesita el nombre "
        "DNS completo del equipo, no su dirección. Indica su nombre DNS en la página del "
        "equipo (Datos de la instalación remota)"
    ),
    "the machine's DNS name is not a usable full DNS name: letters, digits and hyphens, "
    "with its domain, and not an address. Correct it on the machine's page": (
        "el nombre DNS del equipo no es un nombre DNS completo utilizable: letras, dígitos y "
        "guiones, con su dominio, y no una dirección. Corrígelo en la página del equipo"
    ),
    "the chosen credential is the wrong kind for this machine: Windows machines take a "
    "Windows domain account, Linux and macOS machines a push install account. Choose a "
    "credential of the machine's kind": (
        "la credencial elegida no es del tipo adecuado para este equipo: los equipos Windows "
        "usan una cuenta de dominio de Windows, y los equipos Linux y macOS una cuenta de "
        "instalación remota. Elige una credencial del tipo del equipo"
    ),
    "this server's own address is not configured as a plain https address, so a pushed "
    "machine would not know where to enroll. Set ITS_PUBLIC_CONSOLE_URL so a pushed "
    "computer knows where to enroll.": (
        "la dirección propia de este servidor no está configurada como una dirección https "
        "simple, así que un equipo al que se instala de forma remota no sabría dónde "
        "inscribirse. Define ITS_PUBLIC_CONSOLE_URL para que un equipo al que se instala de "
        "forma remota sepa dónde inscribirse."
    ),
    "the machine's DNS name is not inside the Active Directory domain the chosen Windows "
    "account signs in to, and an account is presented only to machines in its own domain. "
    "Check the machine's DNS name, or choose an account from its domain": (
        "el nombre DNS del equipo no está dentro del dominio de Active Directory en el que "
        "inicia sesión la cuenta de Windows elegida, y una cuenta solo se presenta a equipos "
        "de su propio dominio. Revisa el nombre DNS del equipo, o elige una cuenta de su "
        "dominio"
    ),
    "the chosen Windows account was saved before its domain was recorded. Open it under "
    "Credentials and save it again": (
        "la cuenta de Windows elegida se guardó antes de que se registrara su dominio. Ábrela "
        "en Credenciales y vuelve a guardarla"
    ),
    "the chosen Windows account now names a different domain from the one recorded for it. "
    "Open it under Credentials and save it again": (
        "la cuenta de Windows elegida nombra ahora un dominio distinto del que se registró "
        "para ella. Ábrela en Credenciales y vuelve a guardarla"
    ),
    "the machine is filed under an archived site, or under a client that has been archived, "
    "and nothing is enrolled into either. Push only machines of active sites and clients": (
        "el equipo pertenece a una sede archivada, o a un cliente que se archivó, y en "
        "ninguno de los dos se inscribe nada. Instala de forma remota solo equipos de sedes y "
        "clientes activos"
    ),
    "the machine at {address} is filed under a site or client that is archived or not "
    "visible to you; nothing was dispatched": (
        "el equipo en {address} pertenece a una sede o a un cliente archivado o que no puedes "
        "ver; no se envió nada"
    ),
    "the internal client's Principal site is not visible to this request": (
        "la sede Principal del cliente interno no es visible para esta solicitud"
    ),
    "a subnet push requires the `credentials.checkout` permission as well as "
    "`agent.push.run`: it presents a stored secret to every machine it lists": (
        "una instalación remota por subred requiere el permiso `credentials.checkout` además "
        "de `agent.push.run`: presenta un secreto almacenado a cada equipo de la lista"
    ),
    "the machine's operating system or processor is not recorded, or is not one push can "
    "install onto. Set its push install details on the machine's page": (
        "el sistema operativo o el procesador del equipo no está registrado, o no es uno sobre "
        "el que la instalación remota pueda instalar. Indica los datos de la instalación "
        "remota en la página del equipo"
    ),
    "the person who started this run no longer holds both agent.push.run and "
    "credentials.checkout, so nothing more is pushed on their confirmation. Start a new run": (
        "quien inició esta ejecución ya no tiene a la vez agent.push.run y "
        "credentials.checkout, así que no se instala nada más con su confirmación. Inicia una "
        "ejecución nueva"
    ),
    "the run's credential no longer exists, is no longer a push credential, or is not a "
    "credential of this machine's client. Nothing is presented with a secret the operator "
    "has withdrawn, nor to another client's machine. Start a new run with a current "
    "credential of the machine's client": (
        "la credencial de la ejecución ya no existe, ya no es una credencial de instalación "
        "remota, o no es una credencial del cliente de este equipo. No se presenta nada con un "
        "secreto que la persona operadora ha retirado, ni al equipo de otro cliente. Inicia "
        "una ejecución nueva con una credencial vigente del cliente del equipo"
    ),
    "this address now belongs to a different machine record from the one that was "
    "confirmed, or the confirmed record is gone. Confirm it in a new run": (
        "esta dirección pertenece ahora a un registro de equipo distinto del que se confirmó, "
        "o el registro confirmado ya no existe. Confírmalo en una ejecución nueva"
    ),
    "the machine is running the agent now, so there is nothing to install": (
        "el equipo ya está ejecutando el agente, así que no hay nada que instalar"
    ),
    "no published agent release exists for this machine's operating system and processor. "
    "Publish one under Agent packages": (
        "no existe ninguna versión del agente publicada para el sistema operativo y el "
        "procesador de este equipo. Publica una en Paquetes del agente"
    ),
    "the run's credential could not be read from the vault in the form a push needs. Open "
    "it under Credentials and save it again": (
        "la credencial de la ejecución no se pudo leer de la bóveda en la forma que necesita "
        "una instalación remota. Ábrela en Credenciales y vuelve a guardarla"
    ),
    "the site server for this machine's segment is waiting for review, so no job may be "
    "sent to it. Approve it under Devices": (
        "el servidor de sitio del segmento de este equipo está esperando revisión, así que no "
        "se le puede enviar ninguna tarea. Apruébalo en Dispositivos"
    ),
    "no device record that a person stood behind has this address. A push target must be a "
    "record somebody promoted or typed in -- not a row the sweep recorded on its own, "
    "which is an ICMP echo reply and may be a printer, a phone or the coffee machine. Open "
    "the device record and promote it (assets.manage), which is how a human vouches for it": (
        "ningún registro de dispositivo avalado por una persona tiene esta dirección. El "
        "destino de una instalación remota debe ser un registro que alguien promovió o "
        "escribió a mano -- no una fila que el barrido registró por sí solo, que es una "
        "respuesta a un eco ICMP y puede ser una impresora, un teléfono o la cafetera. Abre el "
        "registro del dispositivo y promuévelo (assets.manage), que es como una persona lo "
        "avala"
    ),
    "device records of more than one client, each vouched for by a person, hold this "
    "address, so it does not name one machine and nothing is chosen for you. A push is made "
    "in the client of its credential: inside that client the address names that client's "
    "machine, and only that client's site server may carry the credential to it. Choose a "
    "push credential of the machine's client": (
        "registros de dispositivo de más de un cliente, cada uno avalado por una persona, "
        "tienen esta dirección, así que no nombra un solo equipo y no se elige ninguno por ti. "
        "Una instalación remota se hace en el cliente de su credencial: dentro de ese cliente "
        "la dirección nombra el equipo de ese cliente, y solo un servidor de sitio de ese "
        "cliente puede llevarle la credencial. Elige una credencial de instalación remota del "
        "cliente del equipo"
    ),
    "the machine at this address belongs to another client than the chosen credential, and "
    "a credential is presented only to machines of its own client. Choose a push credential "
    "of the machine's client": (
        "el equipo de esta dirección es de otro cliente que la credencial elegida, y una "
        "credencial solo se presenta a equipos de su propio cliente. Elige una credencial de "
        "instalación remota del cliente del equipo"
    ),
    "no active push authorization covers this address. Deny by default: authorizing an "
    "IPAM scan of the same prefix authorizes nothing here, because authorizing an ICMP "
    "echo is not authorizing an administrative login. A holder of agent.push.scope.manage "
    "must authorize the prefix": (
        "ninguna autorización de instalación remota activa cubre esta dirección. Se deniega "
        "por defecto: autorizar un escaneo de IPAM del mismo prefijo no autoriza nada aquí, "
        "porque autorizar un eco ICMP no es autorizar un inicio de sesión administrativo. "
        "Quien tenga agent.push.scope.manage debe autorizar el prefijo"
    ),
    "the chosen credential is not bound to a prefix containing this address. The "
    "credential carries where it may be presented; binding a new prefix requires "
    "agent.push.scope.manage": (
        "la credencial elegida no está vinculada a un prefijo que contenga esta dirección. La "
        "credencial lleva consigo dónde puede presentarse; vincular un prefijo nuevo requiere "
        "agent.push.scope.manage"
    ),
    "no enabled site server serves this address's segment. Every packet that reaches an "
    "unenrolled machine must originate from a device the operator declared a site server "
    "for that machine's segment, and there is no fallback originator -- not another agent "
    "on the segment, and never the control plane. Promote a device on that segment to site "
    "server (site.manage), or install the agent from the install command": (
        "ningún servidor de sitio habilitado atiende el segmento de esta dirección. Todo "
        "paquete que llega a un equipo sin inscribir debe originarse en un dispositivo que la "
        "persona operadora haya declarado servidor de sitio de ese segmento, y no hay ningún "
        "originador alternativo -- ni otro agente del segmento, ni nunca el plano de control. "
        "Promueve un dispositivo de ese segmento a servidor de sitio (site.manage), o instala "
        "el agente con el comando de instalación"
    ),
    "the only site server serving this segment runs an agent too old to push install: it "
    "cannot tell the machine it installs where to enroll, so the agent it placed would "
    "never appear here. It is skipped rather than used. Upgrade it, or promote a newer "
    "device on that segment to site server (site.manage)": (
        "el único servidor de sitio que atiende este segmento ejecuta un agente demasiado "
        "antiguo para la instalación remota: no puede decirle al equipo en el que instala "
        "dónde inscribirse, así que el agente que colocó nunca aparecería aquí. Se omite en "
        "lugar de usarse. Actualízalo, o promueve un dispositivo más nuevo de ese segmento a "
        "servidor de sitio (site.manage)"
    ),
    "the only site server serving this segment runs an agent too old to push over WinRM: "
    "it does not advertise agent.push_install.winrm, so it cannot reach a Windows target "
    "and is skipped rather than used (design §4). Upgrade it to 1.6.x, or promote a newer "
    "device on that segment to site server (site.manage)": (
        "el único servidor de sitio que atiende este segmento ejecuta un agente demasiado "
        "antiguo para la instalación remota por WinRM: no anuncia agent.push_install.winrm, "
        "así que no puede llegar a un destino Windows y se omite en lugar de usarse (diseño "
        "§4). Actualízalo a la 1.6.x, o promueve un dispositivo más nuevo de ese segmento a "
        "servidor de sitio (site.manage)"
    ),
    "Enter the account as user@your.domain (for example svc-push@corp.example.com). The "
    "domain after the @ is the Active Directory domain a push signs in to; DOMAIN\\user "
    "does not name one.": (
        "Introduce la cuenta como usuario@tu.dominio (por ejemplo svc-push@corp.example.com). "
        "El dominio que va tras la @ es el dominio de Active Directory en el que inicia sesión "
        "una instalación remota; DOMINIO\\usuario no nombra ninguno."
    ),
    # --- review fixes (core, health, agent_distribution) ---
    "max_per_device_per_day: {count} {job_type} job(s) already ran on this device in the "
    "last 24h (limit {limit}); most recent job ids {job_ids}": (
        "max_per_device_per_day: ya se ejecutaron {count} tarea(s) {job_type} en este "
        "dispositivo en las últimas 24 h (límite {limit}); ids de las tareas más recientes "
        "{job_ids}"
    ),
    "max_per_org_per_hour: {count} {job_type} job(s) already ran across this org in the "
    "last hour (limit {limit}); first job ids {job_ids}": (
        "max_per_org_per_hour: ya se ejecutaron {count} tarea(s) {job_type} en toda esta "
        "organización en la última hora (límite {limit}); ids de las primeras tareas {job_ids}"
    ),
    "cooldown: {job_type} last ran on this device inside the action's {cooldown_s}s "
    "cooldown; job ids {job_ids}": (
        "cooldown: {job_type} se ejecutó por última vez en este dispositivo dentro del periodo "
        "de espera de {cooldown_s} s de la acción; ids de las tareas {job_ids}"
    ),
    "mount/volume, '*' = worst": "punto de montaje o volumen; '*' = el peor",
    "service name": "nombre del servicio",
    "process name": "nombre del proceso",
    "device path, '*' = all": "ruta del dispositivo; '*' = todos",
    "schedule name, empty = any schedule": (
        "nombre de la programación; vacío = cualquier programación"
    ),
    "an active agent update rollout still uses this release; complete or cancel it first": (
        "un despliegue activo de actualización del agente todavía usa esta versión; complétalo "
        "o cancélalo primero"
    ),
    # --- email_intake ---
    "the directory (tenant) ID is not a valid ID or domain": (
        "el ID de directorio (inquilino) no es un ID ni un dominio válido"
    ),
    "Microsoft sign-in is not available on this server: ITS_PUBLIC_CONSOLE_URL must be set "
    "to the https address people open the console at. That is a server setting, not "
    "something this screen can fix.": (
        "el inicio de sesión de Microsoft no está disponible en este servidor: "
        "ITS_PUBLIC_CONSOLE_URL debe definirse con la dirección https con la que la gente abre "
        "la consola. Es un ajuste del servidor, no algo que se pueda corregir desde esta "
        "pantalla."
    ),
    "the application (client) ID is empty": "el ID de la aplicación (cliente) está vacío",
    "a client secret is required the first time this application is saved": (
        "se necesita un secreto de cliente la primera vez que se guarda esta aplicación"
    ),
    "mailbox-test rate limit exceeded for this organization; try again shortly": (
        "se superó el límite de pruebas de buzón para esta organización; inténtalo de nuevo en "
        "unos instantes"
    ),
    "the client secret may be at most {max_chars} characters": (
        "el secreto de cliente puede tener como máximo {max_chars} caracteres"
    ),
    "mailbox-test rate limiting is unavailable; try again shortly": (
        "el límite de pruebas de buzón no está disponible; inténtalo de nuevo en unos instantes"
    ),
    "connected to {host}:{port} and listed {folder}": (
        "conectado a {host}:{port} y listada la carpeta {folder}"
    ),
    "sign-in failed: not connected to Microsoft yet": (
        "el inicio de sesión falló: todavía no está conectado con Microsoft"
    ),
    "mailbox {mailbox_id} not found": "no se encontró el buzón {mailbox_id}",
    "that sign-in took too long; start it again": (
        "ese inicio de sesión tardó demasiado; vuelve a empezar"
    ),
    "this mailbox signs in with a password; switch it to Microsoft first": (
        "este buzón inicia sesión con contraseña; cámbialo antes a Microsoft"
    ),
    "set up the Microsoft connection for this organisation first": (
        "configura antes la conexión con Microsoft de esta organización"
    ),
    "that sign-in has already been used, or does not exist": (
        "ese inicio de sesión ya se usó o no existe"
    ),
    "that sign-in was started by somebody else": "ese inicio de sesión lo comenzó otra persona",
    "Microsoft refused that sign-in; start it again": (
        "Microsoft rechazó ese inicio de sesión; vuelve a empezar"
    ),
    "Microsoft returned no refresh token, so this mailbox could only be read for an hour. "
    "Check that the application is allowed offline_access.": (
        "Microsoft no devolvió ningún token de actualización, así que este buzón solo se "
        "podría leer durante una hora. Comprueba que la aplicación tenga permitido "
        "offline_access."
    ),
    "the Microsoft connection has been removed": "se eliminó la conexión con Microsoft",
    "could not reach Microsoft ({error_class}); try again": (
        "no se pudo contactar con Microsoft ({error_class}); inténtalo de nuevo"
    ),
    "Microsoft could not complete the sign-in (HTTP {http_status}); try again": (
        "Microsoft no pudo completar el inicio de sesión (HTTP {http_status}); inténtalo de nuevo"
    ),
    "default_category_id does not resolve in this org": (
        "default_category_id no corresponde a ninguna categoría de esta organización"
    ),
    "default_category_id names a category with no default queue; every message to this "
    "mailbox would be quarantined instead of becoming a ticket": (
        "default_category_id indica una categoría sin cola predeterminada; todos los mensajes "
        "de este buzón quedarían en cuarentena en lugar de convertirse en tickets"
    ),
    "a mailbox for {address} already exists in this org": (
        "ya existe un buzón para {address} en esta organización"
    ),
    "a mailbox that signs in with Microsoft has no password; leave it out": (
        "un buzón que inicia sesión con Microsoft no tiene contraseña; déjala en blanco"
    ),
    "switching this mailbox to a password needs the password": (
        "para cambiar este buzón a contraseña hace falta la contraseña"
    ),
    "mailbox {mailbox_id} has {message_count} processed message(s) and cannot be deleted; "
    "deactivate it instead": (
        "el buzón {mailbox_id} tiene {message_count} mensaje(s) procesado(s) y no se puede "
        "eliminar; desactívalo en su lugar"
    ),
    "only a mailbox that signs in with Microsoft can be disconnected": (
        "solo se puede desconectar un buzón que inicia sesión con Microsoft"
    ),
    "a password is required for this mailbox": "este buzón necesita una contraseña",
    "connect this mailbox to Microsoft before switching it on": (
        "conecta este buzón con Microsoft antes de activarlo"
    ),
    "Microsoft sign-in is no longer valid — reconnect this mailbox": (
        "el inicio de sesión de Microsoft ya no es válido: vuelve a conectar este buzón"
    ),
    "Microsoft refused this organisation's application — check Email Intake → Microsoft "
    "connection (reconnecting the mailbox will not fix this)": (
        "Microsoft rechazó la aplicación de esta organización: revisa Entrada de correo → "
        "Conexión con Microsoft 365 (volver a conectar el buzón no lo arreglará)"
    ),
    "Microsoft refused the application this organisation is registered with. Check Email "
    "Intake → Microsoft connection; reconnecting the mailbox will not fix it.": (
        "Microsoft rechazó la aplicación con la que está registrada esta organización. Revisa "
        "Entrada de correo → Conexión con Microsoft 365; volver a conectar el buzón no lo "
        "arreglará."
    ),
    # --- service_desk ---
    "ticket {ticket_id} is {status} and accepts no new {kind}": (
        "el ticket {ticket_id} está {status} y no acepta {kind} nuevos"
    ),
    "comments": "comentarios",
    "unknown author_type: {author_type}": "author_type desconocido: {author_type}",
    "unknown visibility: {visibility}": "visibilidad desconocida: {visibility}",
    "author_id is required when author_type='user'": (
        "author_id es obligatorio cuando author_type='user'"
    ),
    "author_id must be None when author_type='system'": (
        "author_id debe ser None cuando author_type='system'"
    ),
    "attachment exceeds the {limit}-byte limit": (
        "el archivo adjunto supera el límite de {limit} bytes"
    ),
    "attachments": "archivos adjuntos",
    "attachment {attachment_id} not found": "no se encontró el archivo adjunto {attachment_id}",
    "fields not editable via update_ticket: {detail}; editable fields are {editable}": (
        "campos que no se pueden editar con update_ticket: {detail}; los campos editables son "
        "{editable}"
    ),
    "field not editable via update_ticket: {detail}; editable fields are {editable}": (
        "campo que no se puede editar con update_ticket: {detail}; los campos editables son "
        "{editable}"
    ),
    "ticket {id} is {status} and cannot be edited": (
        "el ticket {id} está {status} y no se puede editar"
    ),
    "user {user_id} not found": "no se encontró el usuario {user_id}",
    "origin='alert' tickets must be type='incident', got {type}": (
        "los tickets con origin='alert' deben ser type='incident', pero llegó {type}"
    ),
    "source_alert_id is only valid for origin='alert', got origin={origin}": (
        "source_alert_id solo es válido con origin='alert', pero llegó origin={origin}"
    ),
    "ticket type {type} does not match category type {category_type}": (
        "el tipo de ticket {type} no coincide con el tipo de categoría {category_type}"
    ),
    "ticket {ticket_id} not found": "no se encontró el ticket {ticket_id}",
    "unknown actor_type: {actor_type}": "actor_type desconocido: {actor_type}",
    "actor_type='system' updates must not carry an actor_id": (
        "las modificaciones con actor_type='system' no deben llevar actor_id"
    ),
    "actor_type='user' updates require an actor_id": (
        "las modificaciones con actor_type='user' requieren actor_id"
    ),
    "{field} (use {redirect})": "{field} (usa {redirect})",
    "category {id} has no default_queue_id and no queue_id was supplied": (
        "la categoría {id} no tiene default_queue_id y no se indicó ningún queue_id"
    ),
    "requester_id is required for origin={origin}": (
        "requester_id es obligatorio con origin={origin}"
    ),
    "actor_id is required for origin={origin}": "actor_id es obligatorio con origin={origin}",
    # The ticket's client (MSP clients plan 2026-10-26 Task 2).
    "the ticket's client is not its device's client": (
        "el cliente del ticket no es el cliente de su equipo"
    ),
    "choose the client this ticket belongs to": "elige el cliente al que pertenece este ticket",
    "tickets reach Unassigned only by email": (
        "los tickets solo llegan a «Sin asignar» por correo"
    ),
    "the requester is a contact of another client": (
        "el solicitante es un contacto de otro cliente"
    ),
    "the assignee cannot see this client's tickets": (
        "la persona asignada no puede ver los tickets de este cliente"
    ),
    # Moving a ticket between clients, and a client's own SLA (Task 3).
    "remove the ticket's device before moving it to another client": (
        "quita el equipo del ticket antes de moverlo a otro cliente"
    ),
    "a ticket opened by an alert or a change stays with its client": (
        "un ticket abierto por una alerta o un cambio se queda en su cliente"
    ),
    "Unassigned has no SLA policies of its own": (
        "«Sin asignar» no tiene políticas de SLA propias"
    ),
    "unknown ticket type: {ticket_type}": "tipo de ticket desconocido: {ticket_type}",
    "illegal {ticket_type} transition: {from_status} -> {to_status}": (
        "transición no permitida en {ticket_type}: {from_status} -> {to_status}"
    ),
    "canned response not found: {response_id}": (
        "no se encontró la respuesta guardada: {response_id}"
    ),
    "a saved reply called {title} already exists": (
        "ya existe una respuesta guardada llamada {title}"
    ),
    "this organisation already has {limit} saved replies": (
        "esta organización ya tiene {limit} respuestas guardadas"
    ),
    "title and body cannot be cleared": "el título y el cuerpo no se pueden dejar vacíos",
    "scope=mine cannot be combined with another assignee": (
        "scope=mine no se puede combinar con otro responsable"
    ),
    "ticket {number} is {status} and accepts no new attachments": (
        "el ticket {number} está {status} y no acepta archivos adjuntos nuevos"
    ),
    "ticket {number} is {status} and cannot be resolved from there": (
        "el ticket {number} está {status} y no se puede resolver desde ahí"
    ),
    "ticket {number} is type={type}; only requests are approved": (
        "el ticket {number} es de tipo {type}; solo se aprueban solicitudes"
    ),
    "ticket {number} has no ticket_approval row": (
        "el ticket {number} no tiene ninguna fila de ticket_approval"
    ),
    "ticket {number}'s approval was already decided: {state}": (
        "la aprobación del ticket {number} ya se decidió: {state}"
    ),
    "a requester may not approve their own request": (
        "un solicitante no puede aprobar su propia solicitud"
    ),
    "actor does not hold the role required to decide this ticket's approval": (
        "quien actúa no tiene el rol necesario para decidir la aprobación de este ticket"
    ),
    "rejecting a request requires a non-empty reason": (
        "rechazar una solicitud requiere un motivo no vacío"
    ),
    "ticket {number}'s category {key} does not require approval": (
        "la categoría {key} del ticket {number} no requiere aprobación"
    ),
    "requires_approval cannot be set on an incident category": (
        "requires_approval no se puede definir en una categoría de incidentes"
    ),
    "queue key already exists: {key}": "ya existe una cola con la clave {key}",
    "ticket queue {queue_id} not found": "no se encontró la cola de tickets {queue_id}",
    "role {role_id} not found": "no se encontró el rol {role_id}",
    "category key already exists: {key}": "ya existe una categoría con la clave {key}",
    "ticket category {category_id} not found": (
        "no se encontró la categoría de tickets {category_id}"
    ),
    "actor_type='system' transitions must not carry an actor_id": (
        "las transiciones con actor_type='system' no deben llevar actor_id"
    ),
    "actor_type='user' transitions require an actor_id": (
        "las transiciones con actor_type='user' requieren actor_id"
    ),
    "{from_status} -> {to_status} may only be driven by an approval decision "
    "(ticket.approve), not the generic status endpoint": (
        "{from_status} -> {to_status} solo puede producirse por una decisión de aprobación "
        "(ticket.approve), no desde el endpoint genérico de estado"
    ),
    "actor_type='system' assignments must not carry an actor_id": (
        "las asignaciones con actor_type='system' no deben llevar actor_id"
    ),
    "actor_type='user' assignments require an actor_id": (
        "las asignaciones con actor_type='user' requieren actor_id"
    ),
    "queue_id cannot be null": "queue_id no puede ser nulo",
    "ticket {number} is {status} and cannot be assigned": (
        "el ticket {number} está {status} y no se puede asignar"
    ),
    # --- sla ---
    "calendar in {timezone} cannot deliver {minutes} working minutes from {start} within "
    "{days} days ({outstanding} minutes still outstanding)": (
        "un calendario en {timezone} no puede ofrecer {minutes} minutos laborales desde "
        "{start} en un plazo de {days} días (faltan {outstanding} minutos)"
    ),
    "auto_close_after_days must be a positive integer; got {days}": (
        "auto_close_after_days debe ser un entero positivo; llegó {days}"
    ),
    "auto_close_after_days must be at most {limit}; got {days}": (
        "auto_close_after_days debe ser como máximo {limit}; llegó {days}"
    ),
    "priority must be one of {priorities} or None; got {priority}": (
        "priority debe ser uno de {priorities} o None; llegó {priority}"
    ),
    "{field} must be a positive integer; got {value}": (
        "{field} debe ser un entero positivo; llegó {value}"
    ),
    "calendar_id {calendar_id} does not resolve in this org": (
        "calendar_id {calendar_id} no corresponde a ningún calendario de esta organización"
    ),
    "calendar_id {calendar_id} is not active; an active policy cannot reference a "
    "deactivated calendar": (
        "calendar_id {calendar_id} no está activo; una política activa no puede usar un "
        "calendario desactivado"
    ),
    "{field} {queue_id} does not resolve in this org": (
        "{field} {queue_id} no corresponde a ninguna cola de esta organización"
    ),
    "policy {policy_id} not found": "no se encontró la política {policy_id}",
    "an active policy for (queue_id={queue_id}, priority={priority}) already exists in "
    "this org": (
        "ya existe en esta organización una política activa para (queue_id={queue_id}, "
        "priority={priority})"
    ),
    "unknown policy field: {unknown}; editable fields are {editable}": (
        "campo de política desconocido: {unknown}; los campos editables son {editable}"
    ),
    "unknown policy fields: {unknown}; editable fields are {editable}": (
        "campos de política desconocidos: {unknown}; los campos editables son {editable}"
    ),
    "policy {policy_id} cannot be deleted: an SLA record exists for ticket {ids} under "
    "this policy; set active=False instead to retire it -- that preserves their history "
    "and frees this (queue, priority) level for a replacement policy": (
        "la política {policy_id} no se puede eliminar: existe un registro de SLA del ticket "
        "{ids} bajo esta política; en su lugar, define active=False para retirarla: así se "
        "conserva su historial y se libera este nivel (cola, prioridad) para una política de "
        "reemplazo"
    ),
    "policy {policy_id} cannot be deleted: an SLA record exists for tickets {ids} under "
    "this policy; set active=False instead to retire it -- that preserves their history "
    "and frees this (queue, priority) level for a replacement policy": (
        "la política {policy_id} no se puede eliminar: existen registros de SLA de los tickets "
        "{ids} bajo esta política; en su lugar, define active=False para retirarla: así se "
        "conserva su historial y se libera este nivel (cola, prioridad) para una política de "
        "reemplazo"
    ),
    "an active policy for (queue_id={effective_queue_id}, priority={effective_priority}) "
    "already exists in this org": (
        "ya existe en esta organización una política activa para "
        "(queue_id={effective_queue_id}, priority={effective_priority})"
    ),
    "policy {policy_id} cannot be deleted: it is still referenced by another record this "
    "organization cannot see": (
        "la política {policy_id} no se puede eliminar: todavía la usa otro registro que esta "
        "organización no puede ver"
    ),
    "unknown SLA target {target}; expected one of {expected}": (
        "objetivo de SLA desconocido {target}; se esperaba uno de {expected}"
    ),
    "ticket_sla {id} belongs to org {record_org_id}, not {org_id}; refusing to mark it "
    "breached": (
        "ticket_sla {id} pertenece a la organización {record_org_id}, no a {org_id}; no se "
        "marcará como incumplido"
    ),
    "the SLA record for ticket {ticket_id} is not visible in org {org_id}; refusing to "
    "mark it breached": (
        "el registro de SLA del ticket {ticket_id} no es visible en la organización {org_id}; "
        "no se marcará como incumplido"
    ),
    "ticket priority {current} is not on PRIORITY_LADDER {ladder}; refusing to guess which "
    "step escalation should raise it to": (
        "la prioridad {current} del ticket no está en PRIORITY_LADDER {ladder}; no se va a "
        "adivinar a qué paso debe subirla la escalada"
    ),
    "{field} must be at most {limit} characters; got {length}": (
        "{field} debe tener como máximo {limit} caracteres; tiene {length}"
    ),
    "calendar {calendar_id} not found": "no se encontró el calendario {calendar_id}",
    "a calendar named {name} already exists in this org": (
        "ya existe un calendario llamado {name} en esta organización"
    ),
    "calendar {calendar_id} is referenced by inactive policy {inactive_ids}; the foreign "
    "key does not distinguish active from inactive policies, so they must be deleted or "
    "repointed before this calendar can be": (
        "el calendario {calendar_id} lo usa la política inactiva {inactive_ids}; la clave "
        "externa no distingue entre políticas activas e inactivas, así que esa política debe "
        "eliminarse o apuntar a otro calendario antes de poder eliminar este"
    ),
    "calendar {calendar_id} is referenced by inactive policies {inactive_ids}; the foreign "
    "key does not distinguish active from inactive policies, so they must be deleted or "
    "repointed before this calendar can be": (
        "el calendario {calendar_id} lo usan las políticas inactivas {inactive_ids}; la clave "
        "externa no distingue entre políticas activas e inactivas, así que esas políticas "
        "deben eliminarse o apuntar a otro calendario antes de poder eliminar este"
    ),
    "calendar {calendar_id} cannot be deleted: an SLA record exists for ticket {ids} on "
    "this calendar": (
        "el calendario {calendar_id} no se puede eliminar: existe un registro de SLA del "
        "ticket {ids} en este calendario"
    ),
    "calendar {calendar_id} cannot be deleted: an SLA record exists for tickets {ids} on "
    "this calendar": (
        "el calendario {calendar_id} no se puede eliminar: existen registros de SLA de los "
        "tickets {ids} en este calendario"
    ),
    "calendar {calendar_id} already has a holiday on {date}": (
        "el calendario {calendar_id} ya tiene un festivo el {date}"
    ),
    "holiday {holiday_id} not found on calendar {calendar_id}": (
        "no se encontró el festivo {holiday_id} en el calendario {calendar_id}"
    ),
    "{tz} is not a valid IANA timezone name": "{tz} no es un nombre de zona horaria IANA válido",
    "weekday {weekday} out of range 0-6 (Monday=0)": (
        "el día de la semana {weekday} está fuera del rango 0-6 (lunes=0)"
    ),
    "window on weekday {weekday} has start {start} >= end {end}; end_time = 00:00 "
    "(midnight-end-of-day) is the one legal exception": (
        "la franja del día de la semana {weekday} empieza en {start}, que no es anterior al "
        "fin {end}; end_time = 00:00 (medianoche al final del día) es la única excepción "
        "permitida"
    ),
    "calendar {calendar_id} is referenced by active policy {active_ids}; deactivate or "
    "delete them before deleting this calendar": (
        "el calendario {calendar_id} lo usa la política activa {active_ids}; desactívala o "
        "elimínala antes de eliminar este calendario"
    ),
    "calendar {calendar_id} is referenced by active policies {active_ids}; deactivate or "
    "delete them before deleting this calendar": (
        "el calendario {calendar_id} lo usan las políticas activas {active_ids}; desactívalas "
        "o elimínalas antes de eliminar este calendario"
    ),
    "calendar {calendar_id} cannot be deleted: it is still referenced by another record "
    "this organization cannot see": (
        "el calendario {calendar_id} no se puede eliminar: todavía lo usa otro registro que "
        "esta organización no puede ver"
    ),
    "overlapping windows on weekday {weekday}: a window starting at minute {start_min} "
    "overlaps one already running until {running_end}": (
        "franjas solapadas el día de la semana {weekday}: una franja que empieza en el minuto "
        "{start_min} se solapa con otra que sigue en curso hasta {running_end}"
    ),
    "Every ticket": "Todos los tickets",
    "{who} in the {queue_name} queue": "{who} en la cola {queue_name}",
    "{who} in any queue": "{who} en cualquier cola",
    "{priority} priority tickets": "Tickets de prioridad {priority}",
    "Low priority tickets": "Tickets de prioridad baja",
    "Normal priority tickets": "Tickets de prioridad normal",
    "High priority tickets": "Tickets de prioridad alta",
    "Urgent tickets": "Tickets urgentes",
    "ticket {id} does not belong to org {org_id}; refusing to track it": (
        "el ticket {id} no pertenece a la organización {org_id}; no se hará su seguimiento"
    ),
    "policy_source {source} may not be written by tracking._materialise; only {allowed} "
    "are legal here -- 'escalation' is Task 9's alone": (
        "tracking._materialise no puede escribir policy_source {source}; aquí solo son válidos "
        "{allowed} ('escalation' es exclusivo de la Task 9)"
    ),
    "ticket {id} already has an SLA record that is not visible in org {org_id}; refusing "
    "to create a second one": (
        "el ticket {id} ya tiene un registro de SLA que no es visible en la organización "
        "{org_id}; no se creará un segundo"
    ),
    "org {org_id} asked this module to act on ticket {ticket_id}'s SLA state, but the "
    "ticket itself is not visible in that org; refusing to act on it": (
        "la organización {org_id} pidió a este módulo actuar sobre el estado de SLA del ticket "
        "{ticket_id}, pero el ticket no es visible en esa organización; no se actuará sobre él"
    ),
    "policy {policy_id} referenced by the SLA record for ticket in org {org_id} is not "
    "visible in that org; refusing to act on it": (
        "la política {policy_id} a la que remite el registro de SLA del ticket en la "
        "organización {org_id} no es visible en esa organización; no se actuará sobre ella"
    ),
    # --- notification ---
    "queue {queue_id} not found": "no se encontró la cola {queue_id}",
    "notification rule {rule_id} not found": "no se encontró la regla de notificación {rule_id}",
    "allowlist entry already exists for {host}:{port}": (
        "ya existe una entrada de la lista de permitidos para {host}:{port}"
    ),
    "allowlist entry {entry_id} not found": (
        "no se encontró la entrada {entry_id} de la lista de permitidos"
    ),
    "user {user_id} is not a member of queue {queue_id}": (
        "el usuario {user_id} no es miembro de la cola {queue_id}"
    ),
    "config must not contain secret keys {keys}; secrets belong in the vault (spec §2.2)": (
        "la configuración no debe contener claves secretas {keys}; los secretos van en la "
        "bóveda (spec §2.2)"
    ),
    "email config requires {missing}": "la configuración de correo requiere {missing}",
    "email config 'port' must be an integer, not a string or other type": (
        "el 'port' de la configuración de correo debe ser un entero, no una cadena ni otro tipo"
    ),
    "email config 'port' must be between 1 and 65535": (
        "el 'port' de la configuración de correo debe estar entre 1 y 65535"
    ),
    "email config 'host' must be a non-empty string": (
        "el 'host' de la configuración de correo debe ser una cadena no vacía"
    ),
    "email config 'from_address' must be a non-empty string": (
        "el 'from_address' de la configuración de correo debe ser una cadena no vacía"
    ),
    "email config 'auth' must be 'password' or 'microsoft'": (
        "el 'auth' de la configuración de correo debe ser 'password' o 'microsoft'"
    ),
    "Microsoft 365 sign-in always sends through {smtp_host} on port {smtp_port} with TLS": (
        "el inicio de sesión de Microsoft 365 siempre envía por {smtp_host} en el puerto "
        "{smtp_port} con TLS"
    ),
    "webhook config requires 'url'": "la configuración del webhook requiere 'url'",
    "unknown event_type {value}": "event_type desconocido {value}",
    "allowlist entries name a host, not a CIDR range": (
        "las entradas de la lista de permitidos nombran un host, no un rango CIDR"
    ),
    "test-send rate limit exceeded for this organization; try again shortly": (
        "se superó el límite de envíos de prueba de esta organización; inténtalo de nuevo en "
        "unos instantes"
    ),
    "give either a username and password or the Microsoft 365 sign-in, not both": (
        "indica un usuario y contraseña o el inicio de sesión de Microsoft 365, no ambos"
    ),
    "give both the username and the password": "indica tanto el usuario como la contraseña",
    "channel name already exists: {name}": "ya existe un canal con el nombre {name}",
    "sign-in details apply to email channels only": (
        "los datos de inicio de sesión solo se aplican a los canales de correo"
    ),
    "a username and password were given for a channel that signs in with Microsoft 365": (
        "se indicaron usuario y contraseña para un canal que inicia sesión con Microsoft 365"
    ),
    "an email channel takes a username and password, or the Microsoft 365 sign-in details, "
    "not a single secret": (
        "un canal de correo admite un usuario y contraseña, o los datos de inicio de sesión de "
        "Microsoft 365, no un único secreto"
    ),
    "notification channel {channel_id} not found": (
        "no se encontró el canal de notificación {channel_id}"
    ),
    "Microsoft 365 details were given for a channel that signs in with a password": (
        "se indicaron datos de Microsoft 365 para un canal que inicia sesión con contraseña"
    ),
    "Microsoft 365 sign-in needs the directory (tenant) ID, application (client) ID and "
    "client secret": (
        "el inicio de sesión de Microsoft 365 necesita el ID de directorio (inquilino), el ID "
        "de aplicación (cliente) y el secreto de cliente"
    ),
    "(ticket)": "(ticket)",
    "an unknown time": "una hora desconocida",
    "health check": "comprobación de salud",
    "(unknown device)": "(dispositivo desconocido)",
    "(no action key)": "(sin clave de acción)",
    "(no registered subnet)": "(sin subred registrada)",
    "drift": "deriva",
    "(unknown address)": "(dirección desconocida)",
    "(unknown check)": "(comprobación desconocida)",
    "not recorded": "sin registrar",
    "a subscription": "una suscripción",
    "(unnamed computer)": "(equipo sin nombre)",
    "(unknown subnet)": "(subred desconocida)",
    "(unnamed credential)": "(credencial sin nombre)",
    "(unknown)": "(desconocido)",
    "(unnamed)": "(sin nombre)",
    "reason not recorded": "motivo sin registrar",
    "yes": "sí",
    "no": "no",
    "[Toeshee] Ticket {line} assigned": "[Toeshee] Ticket {line} asignado",
    "Ticket {line} has been assigned to a technician.": (
        "El ticket {line} se ha asignado a un técnico."
    ),
    "[Toeshee] Ticket {line}: status changed to {to_status}": (
        "[Toeshee] Ticket {line}: el estado cambió a {to_status}"
    ),
    "Ticket {line} changed status from {from_status} to {to_status}.": (
        "El ticket {line} cambió de estado: de {from_status} a {to_status}."
    ),
    "[Toeshee] New comment on ticket #{ticket_number}": (
        "[Toeshee] Comentario nuevo en el ticket #{ticket_number}"
    ),
    "A new comment was added to ticket #{ticket_number}.": (
        "Se añadió un comentario nuevo al ticket #{ticket_number}."
    ),
    "[Toeshee] Internal note added to ticket #{ticket_number}": (
        "[Toeshee] Nota interna añadida al ticket #{ticket_number}"
    ),
    "An internal note was added to ticket #{ticket_number}. Sign in to Toeshee to view it.": (
        "Se añadió una nota interna al ticket #{ticket_number}. Inicia sesión en Toeshee para "
        "verla."
    ),
    "[Toeshee] Approval requested: ticket {line}": (
        "[Toeshee] Aprobación solicitada: ticket {line}"
    ),
    "Ticket {line} requires your approval.": "El ticket {line} necesita tu aprobación.",
    "[Toeshee] Ticket {line}: approval {decision}": (
        "[Toeshee] Ticket {line}: aprobación {decision}"
    ),
    "Ticket {line} was {decision}.": "El ticket {line} fue {decision}.",
    "[Toeshee] Ticket {line} resolved": "[Toeshee] Ticket {line} resuelto",
    "Your ticket {line} has been resolved. If you have further questions, sign in to the "
    "portal and add a comment to this ticket.": (
        "Tu ticket {line} se ha resuelto. Si tienes más preguntas, inicia sesión en el portal "
        "y añade un comentario a este ticket."
    ),
    "[Toeshee] SLA breached: ticket {line}": "[Toeshee] SLA incumplido: ticket {line}",
    "The {target} SLA for ticket {line} was due at {due_at} and has been breached.": (
        "El SLA de {target} del ticket {line} vencía el {due_at} y se ha incumplido."
    ),
    "{subject} on {hostname}": "{subject} en {hostname}",
    "Observed: {value}": "Valor observado: {value}",
    "Threshold: {comparator} {threshold}": "Umbral: {comparator} {threshold}",
    "Severity: {severity}": "Gravedad: {severity}",
    "[Toeshee] Health alert ({severity}): {line}": (
        "[Toeshee] Alerta de salud ({severity}): {line}"
    ),
    "This check is currently flapping.": "Esta comprobación se repite ahora mismo.",
    "A health alert opened for {line}.": "Se abrió una alerta de salud para {line}.",
    "Opened: {opened_at}": "Abierta: {opened_at}",
    "[Toeshee] Health alert ESCALATED ({severity}): {line}": (
        "[Toeshee] Alerta de salud ESCALADA ({severity}): {line}"
    ),
    "The health alert for {line} is still open and has been escalated.": (
        "La alerta de salud de {line} sigue abierta y se ha escalado."
    ),
    "Nobody has acknowledged it. Sign in to Toeshee to review it.": (
        "Nadie la ha reconocido. Inicia sesión en Toeshee para revisarla."
    ),
    "[Toeshee] Remediation awaiting approval: {action_key} on {hostname}": (
        "[Toeshee] Corrección pendiente de aprobación: {action_key} en {hostname}"
    ),
    "A remediation is waiting for approval for {line}.": (
        "Hay una corrección pendiente de aprobación para {line}."
    ),
    "Approved action: {action_key}": "Acción aprobada: {action_key}",
    "Pinned script: {script_name} (version {script_version})": (
        "Script fijado: {script_name} (versión {script_version})"
    ),
    "It will NOT run until someone holding `health.remediate` approves it. Sign in to "
    "Toeshee to approve or refuse it.": (
        "NO se ejecutará hasta que alguien con el permiso `health.remediate` la apruebe. "
        "Inicia sesión en Toeshee para aprobarla o rechazarla."
    ),
    "[Toeshee] IPAM drift ({severity}): {drift_class} at {address}": (
        "[Toeshee] Deriva de IPAM ({severity}): {drift_class} en {address}"
    ),
    "The address plan and the fleet disagree about {address}.": (
        "El plan de direcciones y el parque no coinciden sobre {address}."
    ),
    "Finding: {drift_class}": "Hallazgo: {drift_class}",
    "Confidence: {confidence}": "Confianza: {confidence}",
    "Subnet: {subnet}": "Subred: {subnet}",
    "Documented claimant: device {device_id}": "Titular documentado: dispositivo {device_id}",
    "Seen on two consecutive reconciliation passes. Sign in to Toeshee to review, "
    "document, or acknowledge it.": (
        "Visto en dos pasadas de conciliación consecutivas. Inicia sesión en Toeshee para "
        "revisarlo, documentarlo o reconocerlo."
    ),
    "[Toeshee] IPAM sweep result REJECTED from device {device}": (
        "[Toeshee] Resultado de barrido de IPAM RECHAZADO del dispositivo {device}"
    ),
    "A network sweep result was rejected in full and no observations were recorded from it.": (
        "Un resultado de barrido de red se rechazó por completo y no se registró ninguna "
        "observación de él."
    ),
    "Reason: {reason}": "Motivo: {reason}",
    "Device: {device}": "Dispositivo: {device}",
    "Sweep run: {sweep_run}": "Ejecución de barrido: {sweep_run}",
    "Addresses outside the signed scope: {count}": "Direcciones fuera del ámbito firmado: {count}",
    "Sample: {sample}": "Muestra: {sample}",
    "[Toeshee] Platform problem: {name} is {status}": (
        "[Toeshee] Problema de la plataforma: {name} está {status}"
    ),
    "Check: {name} ({probe})": "Comprobación: {name} ({probe})",
    "Status: {status}": "Estado: {status}",
    "Since: {since}": "Desde: {since}",
    "Sign in to Toeshee and open Platform status for detail. You will be told when it "
    "recovers.": (
        "Inicia sesión en Toeshee y abre Estado de la plataforma para ver el detalle. Se te "
        "avisará cuando se recupere."
    ),
    "The platform's own check '{name}' is healthy again.": (
        "La comprobación propia de la plataforma «{name}» vuelve a estar en buen estado."
    ),
    "Unhealthy since: {since}": "Con problemas desde: {since}",
    "Recovered: {since}": "Recuperada: {since}",
    "Service: {service}": "Servicio: {service}",
    "Renews: {renews_at} (in {days} days)": "Se renueva: {renews_at} (en {days} días)",
    "Notice deadline: {deadline} (in {days} days)": "Plazo de aviso: {deadline} (en {days} días)",
    "Auto-renews: {answer}": "Renovación automática: {answer}",
    "[Toeshee] Cancellation notice deadline for {name}: {deadline}": (
        "[Toeshee] Plazo de aviso de cancelación de {name}: {deadline}"
    ),
    "The last day to give notice before this subscription renews is coming up.": (
        "Se acerca el último día para dar aviso antes de que se renueve esta suscripción."
    ),
    "After the notice deadline the renewal can no longer be cancelled or renegotiated. "
    "Sign in to Toeshee to review it.": (
        "Pasado el plazo de aviso, la renovación ya no se puede cancelar ni renegociar. Inicia "
        "sesión en Toeshee para revisarla."
    ),
    "[Toeshee] Subscription renewal: {name} on {renews_at}": (
        "[Toeshee] Renovación de suscripción: {name} el {renews_at}"
    ),
    "This subscription renews soon.": "Esta suscripción se renueva pronto.",
    "Sign in to Toeshee to review it or record the renewal.": (
        "Inicia sesión en Toeshee para revisarla o registrar la renovación."
    ),
    "[Toeshee] New computer waiting for review: {name}": (
        "[Toeshee] Equipo nuevo pendiente de revisión: {name}"
    ),
    "A computer joined through the install link and is waiting for review.": (
        "Un equipo se unió mediante el enlace de instalación y está pendiente de revisión."
    ),
    "Computer: {name}": "Equipo: {name}",
    "Operating system: {os_family}": "Sistema operativo: {os_family}",
    "Joined from: {source}": "Se unió desde: {source}",
    "Joined: {joined_at}": "Se unió el: {joined_at}",
    "Until someone approves it, it receives only inventory, health monitoring and "
    "certificate renewal. Approve or reject it on the Devices page.": (
        "Hasta que alguien lo apruebe, solo recibe inventario, monitorización de salud y "
        "renovación de certificados. Apruébalo o recházalo en la página Dispositivos."
    ),
    "[Toeshee] Rejected computer still has the agent: {name}": (
        "[Toeshee] Un equipo rechazado sigue teniendo el agente: {name}"
    ),
    "A computer that joined through the install link was rejected. {why}": (
        "Se rechazó un equipo que se unió mediante el enlace de instalación. {why}"
    ),
    "Its agent is too old to remove itself.": (
        "Su agente es demasiado antiguo para desinstalarse solo."
    ),
    "Its agent did not confirm it removed itself.": (
        "Su agente no confirmó que se hubiera desinstalado."
    ),
    "Rejected: {rejected_at}": "Rechazado el: {rejected_at}",
    "Its certificate has been revoked, so it can no longer connect, but the agent may "
    "still be installed. Uninstall it on the computer by hand, then press \"I removed it\" "
    "beside it under Waiting for review on the Devices page.": (
        "Su certificado se ha revocado, así que ya no puede conectarse, pero puede que el "
        "agente siga instalado. Desinstálalo a mano en el equipo y luego pulsa «Ya lo quité» "
        "junto a él, en Pendientes de revisión de la página Dispositivos."
    ),
    "[Toeshee] Push install finished: {prefix}": (
        "[Toeshee] Instalación remota terminada: {prefix}"
    ),
    "A push install run someone confirmed has finished.": (
        "Ha terminado una ejecución de instalación remota que alguien confirmó."
    ),
    "Machines in the run: {count}": "Equipos en la ejecución: {count}",
    "Installed: {count}": "Instalados: {count}",
    "Failed: {count}": "Fallidos: {count}",
    "Refused before anything was sent: {count}": "Rechazados antes de enviar nada: {count}",
    "Finished: {finished_at}": "Terminó el: {finished_at}",
    "Open Devices, then Push runs, to see what happened on each machine.": (
        "Abre Dispositivos y luego Ejecuciones de instalación remota para ver qué pasó en cada "
        "equipo."
    ),
    "[Toeshee] Push install stopped: {prefix}": "[Toeshee] Instalación remota detenida: {prefix}",
    "A push install run was stopped to protect its credential from lockout.": (
        "Se detuvo una ejecución de instalación remota para proteger su credencial de un bloqueo."
    ),
    "The credential {name} was refused by {count} machines, so nothing more was presented "
    "with it.": (
        "{count} equipos rechazaron la credencial {name}, así que no se presentó con ella nada "
        "más."
    ),
    "Installed before the stop: {count}": "Instalados antes de la detención: {count}",
    "Machines still waiting: {count}": "Equipos que siguen esperando: {count}",
    "Stopped: {stopped_at}": "Se detuvo el: {stopped_at}",
    "Check the account's password and whether it is locked, then open Devices, Push runs, "
    "and Resume the run. It starts again with one machine.": (
        "Comprueba la contraseña de la cuenta y si está bloqueada; luego abre Dispositivos, "
        "Ejecuciones de instalación remota y pulsa Reanudar en la ejecución. Vuelve a empezar "
        "con un solo equipo."
    ),
    "Event: {event_type}": "Evento: {event_type}",
    "Ticket: #{ticket_number}": "Ticket: #{ticket_number}",
    "Subject: {subject}": "Asunto: {subject}",
    "Occurred: {occurred_at}": "Ocurrió: {occurred_at}",
    "new": "nuevo",
    "open": "abierto",
    "pending": "pendiente",
    "resolved": "resuelto",
    "closed": "cerrado",
    "cancelled": "cancelado",
    "pending_approval": "pendiente de aprobación",
    "in_progress": "en curso",
    "fulfilled": "completado",
    "rejected": "rechazado",
    "response": "respuesta",
    "resolution": "resolución",
    "info": "información",
    "warning": "advertencia",
    "critical": "crítica",
    "unhealthy": "con problemas",
    "degraded": "degradado",
    "down": "caído",
    "no recent success has been recorded": "no se ha registrado ningún éxito reciente",
    "the last run reported a failure": "la última ejecución informó de un fallo",
    "it did not answer": "no respondió",
    "it answered too slowly": "respondió demasiado despacio",
    "its settings are wrong or unreadable": "su configuración es incorrecta o ilegible",
    "Application database": "Base de datos de la aplicación",
    "Certificate authority": "Autoridad de certificación",
    "Sign-in event forwarding": "Reenvío de eventos de inicio de sesión",
    # --- portal ---
    "not a portal user": "tu cuenta no tiene acceso al portal",
    "article {article_id} not found": "no se encontró el artículo {article_id}",
    # --- people ---
    "person not found": "no se encontró a la persona",
    "someone here already has that email address": (
        "alguien de aquí ya tiene esa dirección de correo"
    ),
    "a sign-in name cannot be blank": "un nombre de inicio de sesión no puede estar en blanco",
    "that sign-in name already belongs to someone else": (
        "ese nombre de inicio de sesión ya es de otra persona"
    ),
    "sign-in name not found": "no se encontró el nombre de inicio de sesión",
    "device not found": "no se encontró el dispositivo",
    "seat not found": "no se encontró el puesto",
    "no such account": "no existe esa cuenta",
    "that account already belongs to someone else": "esa cuenta ya es de otra persona",
    "a person needs a name": "una persona necesita un nombre",
    # A person belongs to one client (MSP clients plan 2026-10-26 Task 5).
    "a person cannot be filed under Unassigned": (
        "una persona no puede pertenecer a «Sin asignar»"
    ),
    "that account belongs to another client": "esa cuenta es de otro cliente",
    "that person belongs to another client": "esa persona es de otro cliente",
    # Licenses and subscriptions belong to one client (plan 2026-10-26 Task 6).
    "a license cannot be filed under Unassigned": (
        "una licencia no puede pertenecer a «Sin asignar»"
    ),
    "a subscription cannot be filed under Unassigned": (
        "una suscripción no puede pertenecer a «Sin asignar»"
    ),
    "nothing is ignored for Unassigned: it has no computers": (
        "no se ignora nada para «Sin asignar»: no tiene equipos"
    ),
    "the allocation target belongs to another client": (
        "el destino de la asignación es de otro cliente"
    ),
    "that license belongs to another client": "esa licencia es de otro cliente",
    # Optional modules per client; changes and BCP plans belong to one client
    # (plan 2026-10-26 Task 7).
    "this client does not use that module": "este cliente no usa ese módulo",
    "a change cannot be filed under Unassigned": (
        "un cambio no puede pertenecer a «Sin asignar»"
    ),
    "a BCP plan cannot be filed under Unassigned": (
        "un plan de continuidad no puede pertenecer a «Sin asignar»"
    ),
    "scope ref belongs to another client: {kind}:{ref_id}": (
        "la referencia del ámbito es de otro cliente: {kind}:{ref_id}"
    ),
    "procedure_ref belongs to another client: {ref_id}": (
        "procedure_ref es de otro cliente: {ref_id}"
    ),
    "that change belongs to another client": "ese cambio es de otro cliente",
    # Budget belongs to one client (plan 2026-10-26 Task 8).
    "nothing is budgeted under Unassigned": "en «Sin asignar» no se presupuesta nada",
    "expense category {category_id} belongs to another client": (
        "la categoría de gasto {category_id} es de otro cliente"
    ),
    "cost center {cost_center_id} belongs to another client": (
        "el centro de costo {cost_center_id} es de otro cliente"
    ),
    "{link_type} {link_id} belongs to another client": "{link_type} {link_id} es de otro cliente",
    "Their sign-in is turned off": "Su inicio de sesión está desactivado",
    "Every computer is in a group": "Todos los equipos están en un grupo",
    "Nothing installed through ITMS yet": "Todavía no se ha instalado nada con ITMS",
    "Holds no subscriptions": "No tiene ninguna suscripción",
    "Holds no licences": "No tiene ninguna licencia",
    "No computer of theirs is in use": "Ningún equipo suyo está en uso",
    "Nothing they revealed is still unchanged": "Nada de lo que revelaron sigue sin cambiarse",
    "No open tickets": "No hay tickets abiertos",
    "no such step": "no existe ese paso",
    "No sign-in yet": "Todavía no tiene inicio de sesión",
    "Their sign-in works": "Su inicio de sesión funciona",
    "No computer yet": "Todavía no tiene equipo",
    "{count} computer": "{count} equipo",
    "{count} computers": "{count} equipos",
    "No licences yet": "Todavía no tiene licencias",
    "{count} licence": "{count} licencia",
    "{count} licences": "{count} licencias",
    "No subscriptions yet": "Todavía no tiene suscripciones",
    "{count} subscription": "{count} suscripción",
    "{count} subscriptions": "{count} suscripciones",
    "They have no sign-in": "No tiene inicio de sesión",
    "Their sign-in still works": "Su inicio de sesión sigue funcionando",
    "checklist not found": "no se encontró la lista de verificación",
    "unknown checklist": "lista de verificación desconocida",
    "finish or cancel the open checklist first": (
        "termina o cancela antes la lista de verificación abierta"
    ),
    "this person has already left": "esta persona ya se fue",
    "this checklist is closed": "esta lista de verificación está cerrada",
    "Add a note saying why this step is skipped.": (
        "Añade una nota que explique por qué se omite este paso."
    ),
    "{count} computer is not in a group": "{count} equipo no está en un grupo",
    "{count} computers are not in a group": "{count} equipos no están en un grupo",
    "{count} install succeeded on their computers": "{count} instalación correcta en sus equipos",
    "{count} installs succeeded on their computers": (
        "{count} instalaciones correctas en sus equipos"
    ),
    "Still holds {count} subscription": "Todavía tiene {count} suscripción",
    "Still holds {count} subscriptions": "Todavía tiene {count} suscripciones",
    "Still holds {count} licence": "Todavía tiene {count} licencia",
    "Still holds {count} licences": "Todavía tiene {count} licencias",
    "{count} computer is still theirs and in use": (
        "{count} equipo sigue siendo suyo y está en uso"
    ),
    "{count} computers are still theirs and in use": (
        "{count} equipos siguen siendo suyos y están en uso"
    ),
    "{count} password they revealed not changed since": (
        "{count} contraseña que reveló no se ha cambiado desde entonces"
    ),
    "{count} passwords they revealed not changed since": (
        "{count} contraseñas que reveló no se han cambiado desde entonces"
    ),
    "{count} ticket still open": "{count} ticket sigue abierto",
    "{count} tickets still open": "{count} tickets siguen abiertos",
    "{evidence}. Add a note saying why this is done.": (
        "{evidence}. Añade una nota que explique por qué este paso está hecho."
    ),
    "Not finished yet: {titles}": "Todavía no está terminado: {titles}",
    "that is not an email address": "eso no es una dirección de correo",
    "requires people.offboard": "requiere people.offboard",
    "Give them a sign-in": "Dale un inicio de sesión",
    "Create their account, or link the one they already have.": (
        "Crea su cuenta o vincula la que ya tiene."
    ),
    "Give them a computer": "Dale un equipo",
    "Link a computer to them. Inventory also does this when they sign in.": (
        "Vincúlale un equipo. El inventario también lo hace cuando inicia sesión."
    ),
    "Put their computer in the right group": "Pon su equipo en el grupo correcto",
    "Groups decide which patches, software and backups it gets.": (
        "Los grupos deciden qué parches, software y copias de seguridad recibe."
    ),
    "Assign their licences": "Asígnale sus licencias",
    "Give them the licences they need.": "Dale las licencias que necesita.",
    "Give them a seat on each service they use.": "Dale un puesto en cada servicio que use.",
    "Install their standard software": "Instala su software estándar",
    "Run your usual deployment on their computer, then tick this.": (
        "Ejecuta tu despliegue habitual en su equipo y luego marca esto."
    ),
    "Turn off their sign-in": "Desactiva su inicio de sesión",
    "They can no longer use the console or the portal.": (
        "Ya no podrá usar la consola ni el portal."
    ),
    "Take back their subscriptions": "Recupera sus suscripciones",
    "Release each seat so someone else can use it.": (
        "Libera cada puesto para que otra persona pueda usarlo."
    ),
    "Take back their licences": "Recupera sus licencias",
    "Release each licence so it returns to the pool.": (
        "Libera cada licencia para que vuelva al conjunto disponible."
    ),
    "Collect or reassign their computers": "Recoge o reasigna sus equipos",
    "Mark each computer as in stock or retired, or give it to someone else.": (
        "Marca cada equipo como en stock o retirado, o dáselo a otra persona."
    ),
    "Change passwords they have seen": "Cambia las contraseñas que ha visto",
    "Change every stored password they revealed.": (
        "Cambia todas las contraseñas guardadas que reveló."
    ),
    "Close their open tickets": "Cierra sus tickets abiertos",
    "Resolve or close anything they raised.": "Resuelve o cierra todo lo que crearon.",
    # --- review fixes (Task 16) ---
    "[Toeshee] Ticket {line}: approval approved": "[Toeshee] Ticket {line}: aprobación aprobada",
    "Ticket {line} was approved.": "El ticket {line} fue aprobado.",
    "[Toeshee] Ticket {line}: approval rejected": "[Toeshee] Ticket {line}: aprobación rechazada",
    "Ticket {line} was rejected.": "El ticket {line} fue rechazado.",
    "[Toeshee] Ticket {line}: approval decided": "[Toeshee] Ticket {line}: aprobación decidida",
    "Ticket {line} was decided.": "El ticket {line} fue decidido.",
    "[Toeshee] Health alert cleared: {line}": "[Toeshee] Alerta de salud cerrada: {line}",
    "The health alert for {line} has cleared.": "La alerta de salud de {line} se ha cerrado.",
    "Cleared: {cleared_at} ({reason})": "Cerrada: {cleared_at} ({reason})",
    "(unknown probe)": "(sonda desconocida)",
    "[Toeshee] Platform recovered: {name} is ok": (
        "[Toeshee] Plataforma recuperada: {name} vuelve a estar en buen estado"
    ),
    "Sign in to Toeshee to review it.": "Inicia sesión en Toeshee para ver el detalle.",
    "An agent reporting outside the scope its job was signed for is either faulty or "
    "controlled by someone else. Sign in to Toeshee to review it.": (
        "Un agente que informa fuera del ámbito para el que se firmó su tarea está averiado o "
        "lo controla otra persona. Inicia sesión en Toeshee para ver el detalle."
    ),
    "The platform's own check '{name}' has reported {status} for {samples} consecutive "
    "samples.": (
        "La comprobación propia de la plataforma «{name}» lleva {samples} muestras "
        "consecutivas en estado «{status}»."
    ),
    "ticket {number}'s approval was already decided: approved": (
        "la aprobación del ticket {number} ya se decidió: aprobada"
    ),
    "ticket {number}'s approval was already decided: rejected": (
        "la aprobación del ticket {number} ya se decidió: rechazada"
    ),
    "Add them to the subscriptions they need": (
        "Añade a la persona a las suscripciones que necesita"
    ),
    "Job result bus": "Redis",
    "Database administration connection": "Administración de la base de datos",
    "Object storage": "Almacenamiento de objetos",
    "Secret vault": "Bóveda",
    "Sign-in key cache": "Claves de inicio de sesión (JWKS)",
    "Job signing key": "Firma de tareas",
    "Nightly platform backup": "Copia de seguridad nocturna",
    "Off-host backup copy": "Copia de seguridad externa",
    "Restore drill": "Simulacro de restauración",
    "cpu_busy_pct": "CPU ocupada",
    "memory_used_pct": "Memoria usada",
    "disk_free_pct": "Disco libre",
    "disk_free_bytes": "Espacio libre en disco",
    "disk_smart_ok": "Salud del disco (SMART)",
    "process_present": "Programa en ejecución",
    "service_state": "Estado del servicio",
    "posture_disk_encryption": "Cifrado de disco activado",
    "posture_firewall_enabled": "Cortafuegos activado",
    "eventlog_disk_error": "Errores de disco en el registro de eventos",
    "eventlog_service_crash": "Servicios que fallan",
    "eventlog_kernel_fault": "Fallos del sistema",
    "script_policy_failed": "Script programado fallido",
    "agent_offline": "Sin contacto del agente",
    "backup_success_age_h": "Horas desde la última copia correcta",
    "backup_last_run_failed": "La última copia falló o no se hizo",
    "backup_offsite_lag_h": "Horas de retraso de la copia externa",
    "backup_restore_test_age_d": "Días desde una restauración de prueba",
    "posture_antivirus_healthy": "Estado del antivirus",
    "service_running": "Servicio en ejecución",
    "cert_days_left": "Caducidad del certificado",
    "gt": "por encima de",
    "gte": "igual o por encima de",
    "lt": "por debajo de",
    "lte": "igual o por debajo de",
    "eq": "igual a",
    "neq": "distinto de",
    "is": "se esperaba",
    "documented_absent": "Documentada, ausente",
    "present_undocumented": "Presente, sin documentar",
    "mac_mismatch": "MAC distinta",
    "claimant_mismatch": "Titular distinto",
    "device_moved": "Dispositivo movido",
    "conflict": "Conflicto",
    "dormant": "En reposo",
    "low": "baja",
    "medium": "media",
    "high": "alta",
    "recovery": "Recuperada",
    "summary_ok": "Recuperada (resumen)",
    "unavailable_timeout": "El sensor desapareció",
    "policy_removed": "Comprobación eliminada",
    "device_inactive": "Dispositivo dado de baja",
    "manual": "Cerrada por una persona",
    "'new'": "«Nuevo»",
    "'open'": "«Abierto»",
    "'pending'": "«Pendiente»",
    "'resolved'": "«Resuelto»",
    "'closed'": "«Cerrado»",
    "'cancelled'": "«Cancelado»",
    "'pending_approval'": "«Pendiente de aprobación»",
    "'in_progress'": "«En curso»",
    "'fulfilled'": "«Completado»",
    "'rejected'": "«Rechazado»",
    "incident": "incidente",
    "request": "solicitud",
    "'incident'": "«Incidente»",
    "'request'": "«Solicitud»",
    # --- budget ---
    "period_index {period_index} out of range 1..{count} for {granularity} budgets": (
        "period_index {period_index} fuera de rango 1..{count} para presupuestos con "
        "granularidad {granularity}"
    ),
    "budget already exists for fiscal_year={fiscal_year} name={name}": (
        "ya existe un presupuesto con fiscal_year={fiscal_year} y name={name}"
    ),
    "budget {budget_id} not found": "no se encontró el presupuesto {budget_id}",
    "budget {budget_id} is closed and cannot be edited": (
        "el presupuesto {budget_id} está cerrado y no se puede editar"
    ),
    "budget {budget_id} is closed: line mutations are rejected": (
        "el presupuesto {budget_id} está cerrado: no se admiten cambios en sus líneas"
    ),
    "budget line {line_id} not found": "no se encontró la línea de presupuesto {line_id}",
    "from_date must not be after to_date": "from_date no puede ser posterior a to_date",
    "materialization window spans {months} months, exceeding the {max_months}-month maximum": (
        "la ventana de generación abarca {months} meses, más que el máximo de {max_months} "
        "meses"
    ),
    "materialization would write {count} rows, exceeding the {max_rows}-row maximum; narrow "
    "the window": (
        "la generación escribiría {count} filas, más que el máximo de {max_rows} filas; reduce "
        "la ventana"
    ),
    "cannot merge colliding projected costs for {link_type} {link_id} in period {period_key}: "
    "currencies differ ({currency} vs {currency2}); refusing to sum across currency codes": (
        "no se pueden combinar los costos proyectados que coinciden para {link_type} {link_id} "
        "en el periodo {period_key}: las monedas difieren ({currency} frente a {currency2}); "
        "no se suman códigos de moneda distintos"
    ),
    "no active expense category with system_key={system_key}; materialization cannot "
    "categorize this cost without one": (
        "no hay ninguna categoría de gasto activa con system_key={system_key}; la generación "
        "no puede categorizar este costo sin una"
    ),
    "fx rate already exists for {from_currency}->{to_currency} on {effective_date}": (
        "ya existe un tipo de cambio de {from_currency}->{to_currency} para el {effective_date}"
    ),
    "fx rate {rate_id} not found": "no se encontró el tipo de cambio {rate_id}",
    "base currency cannot change: at least one non-void expense already carries a converted "
    "amount_base; pass acknowledge_mixed_basis=True to proceed with a mixed basis": (
        "la moneda base no puede cambiar: al menos un gasto no anulado ya tiene un amount_base "
        "convertido; pasa acknowledge_mixed_basis=True para continuar con una base mixta"
    ),
    "months must be >= 1": "months debe ser al menos 1",
    "no active expense category with system_key={system_key}; this report cannot categorize "
    "this projected cost without one": (
        "no hay ninguna categoría de gasto activa con system_key={system_key}; este informe no "
        "puede categorizar este costo proyectado sin una"
    ),
    "expense {expense_id} is void and cannot be edited": (
        "el gasto {expense_id} está anulado y no se puede editar"
    ),
    "vendor {vendor_id} not found": "no se encontró el proveedor {vendor_id}",
    "{link_type} {link_id} not found": "no se encontró {link_type} {link_id}",
    "expense {expense_id} not found": "no se encontró el gasto {expense_id}",
    "expense {expense_id} is auto-fed (source={source}); link_id/link_type are not editable": (
        "el gasto {expense_id} se alimenta automáticamente (origen {source}); link_id y "
        "link_type no se pueden editar"
    ),
    "expense {expense_id} is {expense_status} and cannot be confirmed": (
        "el gasto {expense_id} está {expense_status} y no se puede confirmar"
    ),
    "expense {expense_id} is already void": "el gasto {expense_id} ya está anulado",
    "link_id requires link_type": "link_id requiere link_type",
    "link_id requires a link_type": "link_id requiere un link_type",
    "converted amount {result} (amount={amount}, rate={rate}) exceeds the maximum "
    "representable base amount {max_base_amount}": (
        "el importe convertido {result} (amount={amount}, rate={rate}) supera el importe base "
        "máximo representable {max_base_amount}"
    ),
    "no attachment for this expense": "este gasto no tiene archivo adjunto",
    "expense category {category_id} not found": (
        "no se encontró la categoría de gasto {category_id}"
    ),
    "cost center code already exists: {code}": "ya existe un centro de costo con el código {code}",
    "cost center {cost_center_id} not found": "no se encontró el centro de costo {cost_center_id}",
    "period_index {period_index} out of range 1..{count} for {granularity}": (
        "period_index {period_index} fuera de rango 1..{count} para la granularidad "
        "{granularity}"
    ),
    "unknown granularity {granularity}": "granularidad desconocida {granularity}",
    "budget granularity: month": "mes",
    "budget granularity: quarter": "trimestre",
    "budget expense status: expected": "previsto",
    "budget expense status: confirmed": "confirmado",
    "budget expense status: void": "anulado",
    "budget expense source: manual": "manual",
    "budget expense source: subscription": "suscripción",
    "budget expense source: license": "licencia",
    "budget link type: subscription": "la suscripción",
    "budget link type: license": "la licencia",
    "budget link type: device": "el dispositivo",
    "budget link type: change": "el cambio",
    # --- change_mgmt ---
    "the {names} and {last} groups": "los grupos {names} y {last}",
    "every device": "todos los dispositivos",
    "the {name} group": "el grupo {name}",
    "Approving updates for the {ring_name} ring reaches {where}, which needs an approved "
    "change. Choose an approved change to link, or have one approved first.": (
        "Aprobar actualizaciones para el anillo {ring_name} alcanza {where}, que exige un "
        "cambio aprobado. Elige un cambio aprobado para vincular, o haz que se apruebe uno "
        "primero."
    ),
    "Deployments to {where} need an approved change. Choose an approved change to link, or "
    "have one approved first.": (
        "Desplegar en {where} exige un cambio aprobado. Elige un cambio aprobado para "
        "vincular, o haz que se apruebe uno primero."
    ),
    "no such change": "no existe ese cambio",
    "that change is {change_state}; only an approved change, or one being implemented, can "
    "authorize this": (
        "ese cambio está {change_state}; solo un cambio aprobado, o uno en implementación, "
        "puede autorizar esto"
    ),
    "a gate must cover patch approvals, deployments, or both": (
        "un requisito de cambio debe abarcar las aprobaciones de parches, los despliegues, o "
        "ambos"
    ),
    "no such gate": "no existe ese requisito de cambio",
    "change {change_id} not found": "no se encontró el cambio {change_id}",
    "only a draft change can be edited": "solo se puede editar un cambio en borrador",
    "invalid decision: {decision}": "decisión no válida: {decision}",
    "illegal transition: {action} from {current}": (
        "transición no permitida: {action} desde {current}"
    ),
    "approver cannot be the change requester": "el aprobador no puede ser quien solicitó el cambio",
    "emergency change requires retrospective approval before completion": (
        "un cambio de emergencia necesita aprobación retroactiva antes de completarse"
    ),
    "unknown action_type {action_type}": "action_type desconocido {action_type}",
    "linked action not found": "no se encontró la acción vinculada",
    "action already linked to this change": "la acción ya está vinculada a este cambio",
    "link not found": "no se encontró el vínculo",
    "gate not readable": "no se pudo leer el requisito de cambio",
    "change state: draft": "borrador",
    "change state: submitted": "enviado",
    "change state: approved": "aprobado",
    "change state: implementing": "en implementación",
    "change state: completed": "completado",
    "change state: rejected": "rechazado",
    "change state: cancelled": "cancelado",
    "change action: submit": "enviar a aprobación",
    "change action: approve": "aprobar",
    "change action: reject": "rechazar",
    "change action: begin": "iniciar implementación",
    "change action: complete": "marcar como completado",
    "change action: cancel": "cancelar",
    "change action: revise": "corregir",
    # --- documentation ---
    "organization document storage quota reached ({usage} of {quota} bytes used). Purge or "
    "archive documents to free space.": (
        "se alcanzó la cuota de almacenamiento de documentos de la organización ({usage} de "
        "{quota} bytes usados). Purga o archiva documentos para liberar espacio."
    ),
    "no document {document_id} in this organization": (
        "no hay ningún documento {document_id} en esta organización"
    ),
    "document {document_id} is archived and accepts no new versions": (
        "el documento {document_id} está archivado y no admite versiones nuevas"
    ),
    "document exceeds the {max_document_bytes}-byte limit": (
        "el documento supera el límite de {max_document_bytes} bytes"
    ),
    "no version {version} of document {document_id}": (
        "no existe la versión {version} del documento {document_id}"
    ),
    "unknown target type {target_type}": "tipo de destino desconocido {target_type}",
    "no {target_type} {target_id} in this organization": (
        "no se encontró {target_type} {target_id} en esta organización"
    ),
    "{target_type} {target_id} belongs to another client": (
        "{target_type} {target_id} es de otro cliente"
    ),
    "documentation cannot be filed under Unassigned": (
        "la documentación no puede pertenecer a «Sin asignar»"
    ),
    "unknown record type {record_type}": "tipo de registro desconocido {record_type}",
    "invalid {record_type} record fields: {exc}": (
        "campos no válidos en el registro de tipo {record_type}: {exc}"
    ),
    "no such link": "no existe ese vínculo",
    "version {version} was purged on {purged_at}": "la versión {version} se purgó el {purged_at}",
    "object storage is unavailable, so this document's bytes could not be read; its metadata "
    "is intact ({error_class})": (
        "el almacenamiento de objetos no está disponible, así que no se pudieron leer los "
        "bytes de este documento; sus metadatos están intactos ({error_class})"
    ),
    "object storage is unavailable, so these bytes could not be destroyed; nothing was changed "
    "({error_class})": (
        "el almacenamiento de objetos no está disponible, así que no se pudieron destruir "
        "estos bytes; no se cambió nada ({error_class})"
    ),
    "already linked": "ya está vinculado",
    "no such article": "no existe ese artículo",
    "no such record": "no existe ese registro",
    "unsupported file type. This library accepts: {accepted_description}": (
        "tipo de archivo no admitido. Esta biblioteca acepta: {accepted_description}"
    ),
    "record_type 'network' is superseded by the IPAM address plan: create the subnet with POST "
    "/api/v1/ipam/subnets instead. Existing network records remain readable, editable and "
    "archivable, and POST /api/v1/ipam/import/doc-records converts them": (
        "record_type 'network' lo sustituye el plan de direcciones de IPAM: crea la subred con "
        "POST /api/v1/ipam/subnets. Los registros de red existentes se pueden seguir leyendo, "
        "editando y archivando, y POST /api/v1/ipam/import/doc-records los convierte"
    ),
    "documentation link target: device": "el dispositivo",
    "documentation link target: device_group": "el grupo de dispositivos",
    "documentation link target: credential": "la credencial",
    "documentation link target: record": "el registro",
    "documentation link target: ipam_subnet": "la subred",
    "documentation link target: ipam_address": "la dirección IP",
    "documentation record type: network": "red",
    "documentation record type: service": "servicio",
    "documentation record type: vendor": "proveedor",
    "documentation record type: location": "ubicación",
    "PDF; PNG, JPEG, GIF or WebP images; ZIP and OOXML (.docx/.xlsx/.pptx/.vsdx); legacy "
    "Office (.doc/.xls/.ppt/.vsd); and UTF-8 text of any kind, including Markdown, CSV, HTML, "
    "SVG and XML (stored and shown as source text). UTF-16 text is not accepted -- re-save it "
    "as UTF-8, or zip it.": (
        "PDF; imágenes PNG, JPEG, GIF o WebP; ZIP y OOXML (.docx/.xlsx/.pptx/.vsdx); Office "
        "antiguo (.doc/.xls/.ppt/.vsd); y texto UTF-8 de cualquier tipo, incluidos Markdown, "
        "CSV, HTML, SVG y XML (se guardan y se muestran como texto fuente). El texto UTF-16 no "
        "se acepta: vuelve a guardarlo como UTF-8 o comprímelo en un ZIP."
    ),
    # --- backup ---
    "retention takes daily, weekly, monthly and yearly counts": (
        "la retención admite recuentos diarios, semanales, mensuales y anuales"
    ),
    "Give both the access key and the secret, or neither to keep the stored ones.": (
        "Indica tanto la clave de acceso como la clave secreta, o ninguna de las dos para "
        "conservar las guardadas."
    ),
    "Confirm the private key is stored safely before adding the recovery key.": (
        "Confirma que la clave privada está guardada en un lugar seguro antes de añadir la "
        "clave de recuperación."
    ),
    "no such recovery key": "no existe esa clave de recuperación",
    "the address starts with https:// (or http:// on a private network)": (
        "la dirección empieza por https:// (o por http:// en una red privada)"
    ),
    "An off-site target with that name exists.": (
        "Ya existe un almacenamiento externo con ese nombre."
    ),
    "no such off-site target": "no existe ese almacenamiento externo",
    "no such backup point": "no existe ese punto de copia",
    "That is not a recovery public key.": "Eso no es una clave pública de recuperación.",
    "This recovery key is already registered.": "Esta clave de recuperación ya está registrada.",
    "This recovery key has been retired.": "Esta clave de recuperación se ha retirado.",
    "The check has expired. Ask for a new one and try again.": (
        "La comprobación ha caducado. Pide una nueva e inténtalo de nuevo."
    ),
    "That answer does not match. Check you used the right key.": (
        "Esa respuesta no coincide. Comprueba que has usado la clave correcta."
    ),
    "no such repository": "no existe ese repositorio",
    # A backup repository serves one client (plan 2026-10-26 Task 13).
    "a backup repository cannot be filed under Unassigned": (
        "un repositorio de copias no puede quedar en Sin asignar"
    ),
    "a backup policy cannot be filed under Unassigned": (
        "una política de copias no puede quedar en Sin asignar"
    ),
    "a repository with that name exists in this client": (
        "ya existe un repositorio con ese nombre en este cliente"
    ),
    "that repository belongs to another client": "ese repositorio es de otro cliente",
    "a policy for every client cannot name a repository; give each client its own": (
        "una política para todos los clientes no puede nombrar un repositorio; dale a cada "
        "cliente el suyo"
    ),
    "that off-site target belongs to another client": (
        "ese almacenamiento externo es de otro cliente"
    ),
    "The stored off-site credential is not usable.": (
        "La credencial guardada del almacenamiento externo no se puede usar."
    ),
    "This bucket does not have Object Lock turned on. Off-site copies need a bucket that was "
    "created with Object Lock, so that nobody, including someone who takes over this server, "
    "can delete or change a backup before its lock runs out. Create a new bucket with Object "
    "Lock enabled and use that one.": (
        "Este bucket no tiene Object Lock activado. Las copias externas necesitan un bucket "
        "creado con Object Lock, para que nadie, ni siquiera quien tome el control de este "
        "servidor, pueda borrar ni modificar una copia de seguridad antes de que termine su "
        "bloqueo. Crea un bucket nuevo con Object Lock activado y usa ese."
    ),
    "Set up a recovery key and confirm it before turning on off-site copies. Without it, the "
    "off-site copy cannot be read if this server is lost.": (
        "Configura una clave de recuperación y confírmala antes de activar las copias "
        "externas. Sin ella, la copia externa no se puede leer si se pierde este servidor."
    ),
    "Test the off-site storage successfully before turning on off-site copies.": (
        "Prueba con éxito el almacenamiento externo antes de activar las copias externas."
    ),
    # --- bcp ---
    "bcp plan {plan_id} not found": "no se encontró el plan BCP {plan_id}",
    "bcp test {test_id} not found": "no se encontró la prueba BCP {test_id}",
    "bcp finding {finding_id} not found": "no se encontró el hallazgo BCP {finding_id}",
    "invalid severity {severity}": "gravedad no válida {severity}",
    "unknown scope kind {kind}": "tipo de ámbito desconocido {kind}",
    "scope ref not found: {kind}:{ref_id}": (
        "no se encontró la referencia del ámbito: {kind}:{ref_id}"
    ),
    "procedure_ref not found: {ref_id}": "no se encontró procedure_ref: {ref_id}",
    "invalid plan state {value}": "estado de plan no válido {value}",
    "change_ref not found: {change_ref}": "no se encontró change_ref: {change_ref}",
    "invalid remediation_state {remediation_state}": (
        "remediation_state no válido {remediation_state}"
    ),
    "malformed scope id {scope_id}": "id de ámbito con formato no válido {scope_id}",
    "malformed procedure_ref {ref}": "procedure_ref con formato no válido {ref}",
    "no such bcp plan": "no existe ese plan BCP",
    "no such bcp test": "no existe esa prueba BCP",
    "bcp scope kind: device": "dispositivo",
    "bcp scope kind: group": "grupo",
    # --- deploy ---
    "no such schedule": "no existe esa programación",
    "no such script": "no existe ese script",
    "a schedule with that name already exists": "ya existe una programación con ese nombre",
    "unknown os_family: {os_family}": "os_family desconocido: {os_family}",
    "unknown interpreter: {interpreter}": "intérprete desconocido: {interpreter}",
    "unknown installer_type: {installer_type}": "installer_type desconocido: {installer_type}",
    "package {package_id} is {installer_type} but has no uploaded artifact": (
        "el paquete {package_id} es {installer_type} pero no tiene ningún archivo subido"
    ),
    "package {package_id} has an artifact but no artifact_sha256 to verify it against": (
        "el paquete {package_id} tiene un archivo pero ningún artifact_sha256 con el que "
        "verificarlo"
    ),
    "{installer_type} is manager-sourced and must carry no artifact": (
        "{installer_type} se obtiene del gestor de paquetes y no debe llevar ningún archivo"
    ),
    "no such package": "no existe ese paquete",
    "no such deployment": "no existe ese despliegue",
    "deployment target not found": "no existe el destino del despliegue",
    "a deployment cannot be filed under Unassigned": (
        "un despliegue no puede pertenecer a «Sin asignar»"
    ),
    "a patch approval cannot be filed under Unassigned": (
        "una aprobación de parche no puede pertenecer a «Sin asignar»"
    ),
    "a device always has a site: name the site it moves to": (
        "un dispositivo siempre tiene una sede: indica a qué sede se traslada"
    ),
    "the deployment target changed while it was being created; try again": (
        "el destino del despliegue cambió mientras se creaba; inténtalo de nuevo"
    ),
    "unknown rule type: {rule_type}": "tipo de regla desconocido: {rule_type}",
    # --- remote ---
    "{from_state} -> {to_state}": "{from_state} -> {to_state}",
    "no remote session {session_id}": "no existe la sesión remota {session_id}",
    "screen sessions take no credential": "las sesiones de pantalla no usan ninguna credencial",
    "agent too old to share its screen": (
        "el agente es demasiado antiguo para compartir su pantalla"
    ),
    "unattended sessions require a credential_id": (
        "las sesiones desatendidas requieren un credential_id"
    ),
    "device already has a live screen session": (
        "el dispositivo ya tiene una sesión de pantalla en directo"
    ),
    "{protocol} is retired; start a screen session instead": (
        "{protocol} está retirado; inicia una sesión remota de pantalla en su lugar"
    ),
    "requires one of {items}": "requiere uno de {items}",
    "no such session": "no existe esa sesión",
    "requires session owner or org-admin": (
        "requiere ser el propietario de la sesión o administrador de la organización"
    ),
    "no recording for session": "la sesión no tiene grabación",
    "remote session state: awaiting_consent": "esperando consentimiento",
    "remote session state: connecting": "conectando",
    "remote session state: active": "activa",
    "remote session state: ended": "finalizada",
    # --- site_server ---
    "address {text} is not inside any segment this site server declares ({declared}). Nothing "
    "is dispatched: guessing a segment would put a datagram on a wire nobody authorized": (
        "la dirección {text} no está dentro de ningún segmento que declare este servidor de "
        "sitio ({declared}). No se envía nada: adivinar un segmento pondría un datagrama en "
        "una red que nadie autorizó"
    ),
    "site server {site_server_id} is disabled; enable it before waking through it": (
        "el servidor de sitio {site_server_id} está deshabilitado; habilítalo antes de "
        "encender equipos a través de él"
    ),
    "this site server declares {count} segments ({declared}); name the one {target_mac} is on. "
    "A MAC address carries no topology, so it cannot be located from the address alone, and "
    "picking a segment for you would put a datagram on a wire nobody named": (
        "este servidor de sitio declara {count} segmentos ({declared}); indica en cuál está "
        "{target_mac}. Una dirección MAC no contiene información de la red, así que no se "
        "puede ubicar solo con ella, y elegir un segmento por ti pondría un datagrama en una "
        "red que nadie indicó"
    ),
    "segment {text} is not served by this site server (declared: {declared}). A wake must "
    "originate on the target's own segment, and this server is not on that one": (
        "este servidor de sitio no atiende el segmento {text} (declarados: {declared}). Un "
        "encendido debe originarse en el propio segmento del equipo, y este servidor no está "
        "en ese"
    ),
    "broadcast must be an explicit address, not a CIDR (got {value}): the control plane "
    "computes it from the declared segment so the endpoint never decides what it may touch": (
        "la dirección de difusión debe ser explícita, no un CIDR (se recibió {value}): el plano "
        "de control la calcula a partir del segmento declarado para que el equipo nunca decida "
        "qué puede alcanzar"
    ),
    "invalid broadcast address {value}: {exc}": "dirección de difusión no válida {value}: {exc}",
    "a site server must declare at least one segment: one that serves no segment serves "
    "nothing, which is a configuration mistake rather than a valid row": (
        "un servidor de sitio debe declarar al menos un segmento: uno que no atiende ningún "
        "segmento no sirve para nada, lo cual es un error de configuración y no una fila válida"
    ),
    "at most {max_segments} segments (got {count})": (
        "como máximo {max_segments} segmentos (se recibieron {count})"
    ),
    "a site server must declare at least one role": (
        "un servidor de sitio debe declarar al menos una función"
    ),
    "roles {refused} are not available in v1 — only {supported_roles} is honoured. The "
    "artefact cache and PXE roles require a listening socket, which `CLAUDE.md` invariant #2 "
    "forbids; the amendment that would permit it is drafted in "
    "`docs/modules/site-server/spec.md` §4.4 and HAS NOT BEEN ACCEPTED. Storing the role now "
    "would put a row in the registry claiming a service is running on a machine that binds no "
    "socket": (
        "las funciones {refused} no están disponibles en la v1: solo se admite "
        "{supported_roles}. La caché de artefactos y las funciones PXE requieren un socket a "
        "la escucha, algo que la invariante n.º 2 de `CLAUDE.md` prohíbe; la enmienda que lo "
        "permitiría está redactada en `docs/modules/site-server/spec.md` §4.4 y NO SE HA "
        "ACEPTADO. Guardar la función ahora pondría en el registro una fila que afirma que un "
        "servicio se ejecuta en un equipo que no abre ningún socket"
    ),
    "device {device_id} has no enrolled agent: a site server receives its work as an ordinary "
    "signed job on its own mutual-TLS connection, so a device with no agent can never be sent "
    "one": (
        "el dispositivo {device_id} no tiene ningún agente inscrito: un servidor de sitio "
        "recibe su trabajo como una tarea firmada corriente por su propia conexión mTLS, así "
        "que a un dispositivo sin agente nunca se le puede enviar una"
    ),
    "waiting for review: device {device_id} has not been approved, and a machine nobody has "
    "approved cannot serve a segment": (
        "pendiente de revisión: el dispositivo {device_id} no se ha aprobado, y un equipo que "
        "nadie ha aprobado no puede atender un segmento"
    ),
    "device {device_id} is already a site server ({existing}); edit that row rather than "
    "promoting twice": (
        "el dispositivo {device_id} ya es un servidor de sitio ({existing}); edita esa fila en "
        "lugar de promoverlo dos veces"
    ),
    "site server {site_server_id} not found": (
        "no se encontró el servidor de sitio {site_server_id}"
    ),
    "cannot update {unknown}": "no se puede actualizar {unknown}",
    "segment must be a non-empty CIDR string": "el segmento debe ser una cadena CIDR no vacía",
    "segment {text} is IPv6: a Wake-on-LAN magic packet is carried by an IPv4 UDP broadcast "
    "and IPv6 has no broadcast address, so an IPv6 segment cannot be woken. Declare the IPv4 "
    "segment this server serves": (
        "el segmento {text} es IPv6: un paquete mágico de Wake-on-LAN viaja en un broadcast "
        "UDP de IPv4 e IPv6 no tiene dirección de broadcast, así que no se puede encender un "
        "segmento IPv6. Declara el segmento IPv4 que atiende este servidor"
    ),
    "MAC address must be a string": "la dirección MAC debe ser una cadena",
    "invalid MAC address {value}: expected 12 hex digits, optionally separated by ':', '-' or "
    "'.'": (
        "dirección MAC no válida {value}: se esperaban 12 dígitos hexadecimales, opcionalmente "
        "separados por ':', '-' o '.'"
    ),
    "invalid segment {text}: {exc}": "segmento no válido {text}: {exc}",
    "invalid IPv4 address {value}: {exc}": "dirección IPv4 no válida {value}: {exc}",
    "site server role: wake": "Wake-on-LAN",
    "site server role: cache": "caché de contenido",
    "site server role: pxe": "arranque por PXE",
    # --- licenses ---
    "product {product_id} not found": "no se encontró el producto {product_id}",
    "license {license_id} not found": "no se encontró la licencia {license_id}",
    "license has no key": "la licencia no tiene clave",
    "unknown target_type {target_type}": "target_type desconocido {target_type}",
    "{target_type} target not found: {target_id}": (
        "no se encontró el destino de tipo {target_type}: {target_id}"
    ),
    "license pool exhausted": "se agotaron los derechos de uso de la licencia",
    "allocation {allocation_id} not found": "no se encontró la asignación {allocation_id}",
    "allocation already released": "la asignación ya se liberó",
    "unknown product_id {product_id}": "product_id desconocido {product_id}",
    "unknown vendor_id {vendor_id}": "vendor_id desconocido {vendor_id}",
    "match_rules must be a list": "match_rules debe ser una lista",
    "match rule has invalid type: {rule_type}": (
        "la regla de coincidencia tiene un tipo no válido: {rule_type}"
    ),
    "match rule pattern must be non-empty": (
        "el patrón de la regla de coincidencia no puede estar vacío"
    ),
    "match rule pattern exceeds {max_pattern_len} characters": (
        "el patrón de la regla de coincidencia supera los {max_pattern_len} caracteres"
    ),
    "match rule regex does not compile: {pattern}": (
        "la expresión regular de la regla de coincidencia no se puede compilar: {pattern}"
    ),
    "no such vendor": "no existe ese proveedor",
    "no such product": "no existe ese producto",
    "no such license": "no existe esa licencia",
    "license allocation target: device": "dispositivo",
    "license allocation target: user": "usuario",
    "license allocation target: person": "persona",
    # --- subscriptions ---
    "unknown billing_cycle {billing_cycle}": "billing_cycle desconocido {billing_cycle}",
    "subscription {sub_id} not found": "no se encontró la suscripción {sub_id}",
    "invalid billing_cycle: {billing_cycle}": "billing_cycle no válido: {billing_cycle}",
    "invalid status: {status}": "estado no válido: {status}",
    "subscription already cancelled": "la suscripción ya está cancelada",
    "subscription has no renewal date": "la suscripción no tiene fecha de renovación",
    "no seats available": "no hay puestos disponibles",
    "seat assignment {assignment_id} not found": (
        "no se encontró la asignación de puesto {assignment_id}"
    ),
    "seat already released": "el puesto ya se liberó",
    "vendor not found: {vendor_id}": "no se encontró el proveedor: {vendor_id}",
    "owner user not found: {owner_user_id}": (
        "no se encontró el usuario responsable: {owner_user_id}"
    ),
    "license not found: {license_ref}": "no se encontró la licencia: {license_ref}",
    "cannot reduce seats to {seats}: {active} seats are currently assigned": (
        "no se pueden reducir los puestos a {seats}: ahora hay {active} puesto(s) asignado(s)"
    ),
    "no such subscription": "no existe esa suscripción",
    # --- user_admin ---
    "the identity provider refused to {what} (HTTP {status_code})": (
        "el proveedor de identidad se negó a {what} (HTTP {status_code})"
    ),
    "the identity provider could not {what}": "el proveedor de identidad no pudo {what}",
    "the identity provider was unreachable while trying to {what} ({error_type})": (
        "no se pudo contactar con el proveedor de identidad al intentar {what} ({error_type})"
    ),
    "{role} is cross-org platform administration and cannot be granted from an organization": (
        "{role} es administración de la plataforma entre organizaciones y no se puede conceder "
        "desde una organización"
    ),
    "this is the organization's last active {role} and cannot be {verb}; grant {grant_role} "
    "to somebody else first": (
        "esta cuenta es el último {role} activo de la organización y no se puede {verb}; "
        "concede {grant_role} a otra persona primero"
    ),
    "this account holds {role}, which no console surface can grant back, so it cannot be "
    "{verb} from here": (
        "esta cuenta tiene {role}, que ninguna pantalla de la consola puede volver a conceder, "
        "así que no se puede {verb} desde aquí"
    ),
    "you cannot {verb} your own account": "no puedes {verb} tu propia cuenta",
    "this user has been deleted": "este usuario se ha eliminado",
    "that is not an email address this console can use": (
        "esa no es una dirección de correo electrónico que esta consola pueda usar"
    ),
    "a user with that email address already exists here": (
        "aquí ya existe un usuario con esa dirección de correo electrónico"
    ),
    "the identity was created but the local record conflicts with an existing one": (
        "la identidad se creó, pero el registro local entra en conflicto con uno existente"
    ),
    "this user already holds that role at that scope": (
        "este usuario ya tiene ese rol en ese ámbito"
    ),
    "no such role assignment for this user": (
        "no existe esa asignación de rol para este usuario"
    ),
    "{role} is cross-org platform administration; it cannot be granted from an organization "
    "and so is not revoked from one either": (
        "{role} es administración de la plataforma entre organizaciones; no se puede conceder "
        "desde una organización y por eso tampoco se revoca desde una"
    ),
    "this installation cannot manage accounts yet: its identity provisioning is not "
    "configured. That is a server setting, not something this screen can fix.": (
        "esta instalación todavía no puede administrar cuentas: su aprovisionamiento de "
        "identidad no está configurado. Es un ajuste del servidor, no algo que esta pantalla "
        "pueda arreglar."
    ),
    "the identity provider did not return this platform's service account, so a test message "
    "has no sender to go out as": (
        "el proveedor de identidad no devolvió la cuenta de servicio de esta plataforma, así "
        "que un mensaje de prueba no tiene remitente con el que salir"
    ),
    "no email settings are saved yet, so there is nothing to test": (
        "todavía no hay una configuración de correo guardada, así que no hay nada que probar"
    ),
    "no sending address is saved, so there is nowhere to send a test message": (
        "no hay ninguna dirección de envío guardada, así que no hay adónde enviar un mensaje "
        "de prueba"
    ),
    "{address} is not an email address": "{address} no es una dirección de correo electrónico",
    "a test can only go to the sending address, to your own address (a +tag is fine), or any "
    "address at {domain}": (
        "una prueba solo puede ir a la dirección de envío, a tu propia dirección (se admite "
        "una etiqueta +) o a cualquier dirección de {domain}"
    ),
    "a test can only go to the sending address, to your own address (a +tag is fine)": (
        "una prueba solo puede ir a la dirección de envío o a tu propia dirección (se admite "
        "una etiqueta +)"
    ),
    "{address} is the address of a sign-in account, and the identity provider will not send a "
    "test to it. Add a tag before the @ to reach the same inbox, for example {example}": (
        "{address} es la dirección de una cuenta de inicio de sesión, y el proveedor de "
        "identidad no le enviará una prueba. Añade una etiqueta antes de la @ para llegar a la "
        "misma bandeja de entrada, por ejemplo {example}"
    ),
    "user admin, you cannot ... your own account: disable": "deshabilitar",
    "user admin, you cannot ... your own account: delete": "eliminar",
    "user admin, cannot be ...: disabled": "deshabilitar",
    "user admin, cannot be ...: deleted": "eliminar",
    "user admin, cannot be ...: stripped of the role": "dejar sin ese rol",
    "identity provider action: create the user": "crear el usuario",
    "identity provider action: update the user": "actualizar el usuario",
    "identity provider action: delete the user": "eliminar el usuario",
    "identity provider action: send the enrolment email": "enviar el correo de invitación",
    "identity provider action: send the password-reset email": (
        "enviar el correo de restablecimiento de contraseña"
    ),
    # --- provisioning ---
    "no such image": "no existe esa imagen",
    "no such profile": "no existe ese perfil",
    "no such run": "no existe esa ejecución",
    "wake_on_lan requires the `site.wake` permission as well as `provisioning.run`: deciding to "
    "put a magic packet on a LAN segment is a separate act from starting a provisioning run": (
        "wake_on_lan requiere el permiso `site.wake` además de `provisioning.run`: decidir "
        "enviar un paquete mágico a un segmento de la red local es un acto distinto de iniciar "
        "una ejecución de aprovisionamiento"
    ),
    "no enabled site server serves {segment}: a Wake-on-LAN packet must originate on the "
    "target's own segment, and nothing in this org is declared for it. Promote a site server "
    "on that segment, or start the run without wake_on_lan": (
        "ningún servidor de sitio habilitado atiende {segment}: un paquete de Wake-on-LAN tiene "
        "que originarse en el propio segmento del destino, y nada en esta organización está "
        "declarado para él. Promueve un servidor de sitio en ese segmento o inicia la "
        "ejecución sin wake_on_lan"
    ),
    "the requested segment": "el segmento solicitado",
    "wake_on_lan.segment was not given and this org has {count} enabled site servers: with no "
    "segment there is nothing to filter on, so {target_mac} cannot be located and picking a "
    "server would put the magic packet on whichever LAN happened to be registered first. Name "
    "the segment the target is on": (
        "no se indicó wake_on_lan.segment y esta organización tiene {count} servidores de sitio "
        "habilitados: sin segmento no hay nada por lo que filtrar, así que no se puede ubicar "
        "{target_mac} y elegir un servidor pondría el paquete mágico en la red local que "
        "estuviera registrada primero. Indica el segmento en el que está el destino"
    ),
    "profile {profile_id} not found or disabled": (
        "no se encontró el perfil {profile_id} o está deshabilitado"
    ),
    "image {image_id} not found": "no se encontró la imagen {image_id}",
    "wake_on_lan was requested but no job engine was supplied to start_provision_run; a wake is "
    "a signed job like any other": (
        "se pidió wake_on_lan pero no se proporcionó ningún motor de tareas a "
        "start_provision_run; un encendido es una tarea firmada como cualquier otra"
    ),
    "injected field {name} contains control characters": (
        "el campo inyectado {name} contiene caracteres de control"
    ),
    "linux image requires params.installer in {installers}, got {installer}": (
        "una imagen de linux requiere params.installer en {installers}, pero se recibió "
        "{installer}"
    ),
    "macOS uses ADE/MDM enrollment, not an answer file": (
        "macOS usa la inscripción ADE/MDM, no un archivo de respuestas"
    ),
    "unsupported os_family: {os_family}": "os_family no admitido: {os_family}",
    "invalid (os_family, kind) pairing: ({os_family}, {kind})": (
        "combinación no válida de (os_family, kind): ({os_family}, {kind})"
    ),
    "credential field {field} must be a plaintext string or a sealed marker": (
        "el campo de credencial {field} debe ser una cadena de texto plano o un marcador sellado"
    ),
    "profile {profile_id} not found": "no se encontró el perfil {profile_id}",
    # --- ai_admin ---
    "you may ask at most {count} questions per minute; try again shortly": (
        "puedes hacer como máximo {count} preguntas por minuto; inténtalo de nuevo en breve"
    ),
    "this organization may ask at most {count} questions per hour; try again shortly": (
        "esta organización puede hacer como máximo {count} preguntas por hora; inténtalo de "
        "nuevo en breve"
    ),
    "this organization's daily token budget of {budget} is spent": (
        "se agotó el límite diario de uso de esta organización, de {budget} tokens"
    ),
    "{used} of this organization's daily budget of {budget} tokens has been used.": (
        "se han usado {used} de los {budget} tokens del límite diario de uso de esta organización."
    ),
    "opening an act-capable conversation requires ai.act.propose": (
        "para abrir una conversación con capacidad de actuar se necesita ai.act.propose"
    ),
    "this conversation belongs to a different organization; start a new one here": (
        "esta conversación pertenece a otra organización; inicia una nueva aquí"
    ),
    "turn id {turn_id} has already been used": "el ID de turno {turn_id} ya se ha usado",
    "the model provider is rate limiting this deployment; try again shortly": (
        "el proveedor de modelos está limitando la frecuencia de esta instalación; inténtalo de "
        "nuevo en breve"
    ),
    "the answer stream is unavailable; the answer is still returned by the turn request itself": (
        "la transmisión de la respuesta no está disponible; la respuesta se sigue devolviendo "
        "en la propia solicitud del turno"
    ),
    "reading an AI action additionally requires devices.read: it names a device, its hostname "
    "and what would run on it": (
        "para leer una acción de IA se necesita además devices.read: nombra un dispositivo, su "
        "nombre de host y lo que se ejecutaría en él"
    ),
    # --- ai_copilot ---
    "requires ai.use": "requiere ai.use",
    "no ticket {ticket_id}": "no existe el ticket {ticket_id}",
    "the AI assistant is not available for this organization": (
        "el asistente de IA no está disponible para esta organización"
    ),
    # --- ai_portal ---
    "the assistant is not available for this organization": (
        "el asistente no está disponible para esta organización"
    ),
    # --- patch ---
    "{count} of the patches named are not in this organisation": (
        "{count} de las actualizaciones indicadas no están en esta organización"
    ),
    "no such patch in this organisation": "no existe esa actualización en esta organización",
    "no such approval": "no existe esa aprobación",
    "this device has no agent, so it cannot be restarted from here": (
        "este dispositivo no tiene agente, así que no se puede reiniciar desde aquí"
    ),
    "this computer is waiting for review; approve it before restarting it from here": (
        "este equipo está pendiente de revisión; apruébalo antes de reiniciarlo desde aquí"
    ),
    "this device's agent cannot restart it; update the agent and try again": (
        "el agente de este dispositivo no puede reiniciarlo; actualiza el agente e inténtalo "
        "de nuevo"
    ),
    # --- inventory ---
    "`{prefix}` needs a name after it": "`{prefix}` necesita un nombre a continuación",
    "unknown fact `{value}` — use one of {facts}, or a `facts.<key>` / `software:<name>` path": (
        "dato desconocido `{value}`: usa uno de {facts}, o una ruta `facts.<clave>` / "
        "`software:<nombre>`"
    ),
    "`greater than` and `less than` compare numbers — `{value}` is not one": (
        "`mayor que` y `menor que` comparan números: `{value}` no lo es"
    ),
    # --- reporting ---
    "the report exceeded its statement timeout; narrow the window or the filters and retry": (
        "el informe superó el tiempo máximo de la consulta; acota la ventana o los filtros e "
        "inténtalo de nuevo"
    ),
    "the export matched more rows than its cap; narrow the parameters and retry -- the file is "
    "refused rather than truncated, because a short CSV looks complete": (
        "la exportación coincidió con más filas que su tope; acota los parámetros e inténtalo "
        "de nuevo: el archivo se rechaza en lugar de truncarse, porque un CSV corto parece "
        "completo"
    ),
    "'to' must not be before 'from'": "'to' no puede ser anterior a 'from'",
    "the window may not exceed {max_days} days": "la ventana no puede superar {max_days} días",
    # --- security_logs ---
    "no such event": "no existe ese evento",
    # --- review fixes (Task 17b) ---
    # patch: restart reasons and job statuses, said at the response (stored English)
    "the device restarted": "el dispositivo se reinició",
    "the OS no longer needs a restart": "el sistema operativo ya no necesita reiniciarse",
    "the policy no longer restarts devices": "la política ya no reinicia los dispositivos",
    "the agent cannot restart this device; update the agent": (
        "el agente no puede reiniciar este dispositivo; actualiza el agente"
    ),
    "the restart job {status} three times": (
        "la tarea de reinicio terminó en estado {status} tres veces"
    ),
    "job status: queued": "en cola",
    "job status: dispatched": "despachado",
    "job status: running": "en ejecución",
    "job status: succeeded": "correcto",
    "job status: failed": "fallido",
    "job status: expired": "caducado",
    "job status: refused": "rechazado",
    "job status: cancelled": "cancelado",
    # reporting: the catalogue prose (summaries, descriptions, parameter descriptions, recency)
    "as of each agent's last contact (`agents.last_seen_at`, updated by the gateway on agent "
    "traffic); a device with no agent row has never reported": (
        "a fecha del último contacto de cada agente (`agents.last_seen_at`, que actualiza la "
        "pasarela con el tráfico del agente); un dispositivo sin fila de agente nunca ha informado"
    ),
    "as of each job's own completion (`jobs.finished_at`, written by `JobEngine.complete` when "
    "the handler returns); a job that has not reached a terminal state carries none": (
        "a fecha de la finalización de cada tarea (`jobs.finished_at`, que escribe "
        "`JobEngine.complete` cuando el manejador termina); una tarea que no ha llegado a un "
        "estado final no tiene ninguna"
    ),
    "current alert state: open or cleared as of the last check tick, never a history of alerts "
    "that have since closed": (
        "estado actual de las alertas: abierta o resuelta a fecha de la última comprobación, "
        "nunca un historial de las alertas que ya se cerraron"
    ),
    "current per-device patch state, as last rewritten by a scan; WHEN that scan ran is a "
    "`jobs` fact (`patch.scan`, succeeded), not a `device_patch_status` one -- `detected_at` "
    "dates a patch's first detection on the device and never moves again": (
        "estado actual de las actualizaciones de cada dispositivo, tal como lo reescribió el "
        "último escaneo; CUÁNDO se hizo ese escaneo es un dato de `jobs` (`patch.scan`, "
        "correcto), no de `device_patch_status`: `detected_at` fecha la primera detección de una "
        "actualización en el dispositivo y no vuelve a cambiar"
    ),
    "as of each device's last completed run (`backup_points.taken_at`)": (
        "a fecha de la última ejecución completada de cada dispositivo (`backup_points.taken_at`)"
    ),
    "current, and MUTABLE -- a ticket edited today changes this figure": (
        "actual y MODIFICABLE: un ticket editado hoy cambia esta cifra"
    ),
    "current, and MUTABLE BY DESIGN -- a human re-triage recomputes a deadline from ticket "
    "start, so a ticket that read 'met' last month can read 'breached' today": (
        "actual y MODIFICABLE POR DISEÑO: una nueva clasificación manual recalcula un plazo "
        "desde el inicio del ticket, así que un ticket que el mes pasado figuraba como "
        "'cumplido' hoy puede figurar como 'incumplido'"
    ),
    "Restrict to one OS family, e.g. 'windows'.": (
        "Limita a una familia de sistema operativo, p. ej. 'windows'."
    ),
    "Restrict to devices in one device group.": (
        "Limita a los dispositivos de un grupo de dispositivos."
    ),
    "Maximum rows to return.": "Máximo de filas que devolver.",
    "Rows to skip before the first returned.": "Filas que omitir antes de la primera devuelta.",
    "Restrict to one queue.": "Limita a una cola.",
    "Restrict to one priority.": "Limita a una prioridad.",
    "Inclusive start of the window, matched against ticket creation.": (
        "Inicio de la ventana, incluido, comparado con la creación del ticket."
    ),
    "Exclusive end of the window, matched against ticket creation.": (
        "Fin de la ventana, excluido, comparado con la creación del ticket."
    ),
    "Every device with its outstanding approved patches and when it was last scanned.": (
        "Cada dispositivo con sus actualizaciones aprobadas pendientes y cuándo se escaneó por "
        "última vez."
    ),
    "One row per device: how many patches are approved for that device's ring and still not "
    "applied, how many of those are security patches, how long the oldest has been outstanding, "
    "what was applied or failed inside the window, and when the device was last scanned. A "
    "device that has never been scanned is reported with scan_state 'never' and is counted as "
    "neither compliant nor non-compliant -- absence of a scan is absence of a record. Approval "
    "is recomputed from patch_approvals against each device's resolved ring rather than read "
    "from the device_patch_status cache, so a device moved into an approved ring after the "
    "approval is counted correctly. Computed live; it is a statement about the record at "
    "meta.generated_at.": (
        "Una fila por dispositivo: cuántas actualizaciones están aprobadas para el anillo de ese "
        "dispositivo y siguen sin aplicarse, cuántas de ellas son de seguridad, cuánto tiempo "
        "lleva pendiente la más antigua, qué se aplicó o falló dentro de la ventana y cuándo se "
        "escaneó el dispositivo por última vez. Un dispositivo que nunca se ha escaneado figura "
        "con scan_state 'never' y no se cuenta ni como conforme ni como no conforme: la ausencia "
        "de un escaneo es la ausencia de un registro. La aprobación se recalcula desde "
        "patch_approvals con el anillo resuelto de cada dispositivo, en lugar de leerse de la "
        "caché device_patch_status, así que un dispositivo que pasó a un anillo aprobado después "
        "de la aprobación se cuenta correctamente. Se calcula en directo; es una afirmación sobre "
        "el registro en meta.generated_at."
    ),
    "How far back `applied_in_window` and `failed_in_window` look. Does not filter the device "
    "list: a device with no activity in the window still appears, with zero counts.": (
        "Hasta dónde llegan `applied_in_window` y `failed_in_window`. No filtra la lista de "
        "dispositivos: un dispositivo sin actividad en la ventana sigue apareciendo, con "
        "recuentos en cero."
    ),
    "How many of the device's own policy scan intervals may elapse before its last scan is "
    "reported stale. The interval comes from the device's resolved patch policy, so an org that "
    "scans weekly is not judged against an org that scans daily.": (
        "Cuántos intervalos de escaneo de la política del propio dispositivo pueden transcurrir "
        "antes de que su último escaneo se considere obsoleto. El intervalo sale de la política "
        "de actualizaciones resuelta del dispositivo, así que una organización que escanea cada "
        "semana no se juzga con el criterio de una que escanea cada día."
    ),
    "SLA attainment by queue and priority over a closed window.": (
        "Cumplimiento de SLA por cola y prioridad en una ventana cerrada."
    ),
    "One row per (queue, priority) pair for tickets created inside the window: how many there "
    "were, how many met or breached each clock, how many are void or still running, the two "
    "attainment percentages, and how many breached under a policy applied by a human re-triage "
    "rather than at creation. A breach outranks a met on the same clock, so answering late "
    "cannot launder it, and a void ticket is excluded from both percentages so cancelling "
    "cannot inflate them. Tickets with no SLA row at all -- no policy matched, or the calendar "
    "is misconfigured -- stay in the ticket count and appear in no other column, so a broken "
    "SLA configuration cannot make attainment look better. NOT reproducible: ticket_sla is "
    "mutable by design, so the same window can read differently tomorrow.": (
        "Una fila por par (cola, prioridad) de los tickets creados dentro de la ventana: "
        "cuántos hubo, cuántos cumplieron o incumplieron cada reloj, cuántos están anulados o "
        "siguen en marcha, los dos porcentajes de cumplimiento y cuántos incumplieron con una "
        "política aplicada por una nueva clasificación manual y no al crearse. Un incumplimiento "
        "prevalece sobre un cumplimiento en el mismo reloj, así que responder tarde no lo "
        "borra, y un ticket anulado se excluye de ambos porcentajes para que cancelar no los "
        "infle. Los tickets sin ninguna fila de SLA (ninguna política coincidió, o el calendario "
        "está mal configurado) se quedan en el recuento de tickets y no aparecen en ninguna otra "
        "columna, así que una configuración de SLA rota no puede hacer que el cumplimiento "
        "parezca mejor. NO es reproducible: ticket_sla es modificable por diseño, así que la "
        "misma ventana puede dar otra lectura mañana."
    ),
    "Every device with the backup policy that applies to it and how protected it is.": (
        "Cada dispositivo con la política de copia de seguridad que le corresponde y lo "
        "protegido que está."
    ),
    "One row per device: whether a backup policy applies and whether it is switched on, which "
    "repository it writes to, when the last backup point was taken and in what state, when a "
    "point was last verified, when a restore was last actually exercised, and a single coverage "
    "verdict. A device with no enabled policy is reported unprotected and can never be reported "
    "covered, whatever point history it has -- absence of a schedule is absence of protection. "
    "last_restore_tested_at is the proven-restore evidence the roadmap's commercialization gate "
    "asks for: a covered device with a null there has schedules, not proof. Computed live; it "
    "is a statement about the record at meta.generated_at.": (
        "Una fila por dispositivo: si se aplica una política de copia de seguridad y si está "
        "activada, en qué repositorio escribe, cuándo se hizo el último punto de copia y en qué "
        "estado, cuándo se verificó un punto por última vez, cuándo se ejercitó de verdad una "
        "restauración por última vez y un único veredicto de cobertura. Un dispositivo sin "
        "ninguna política habilitada figura como sin proteger y nunca puede figurar como "
        "cubierto, tenga el historial de puntos que tenga: la ausencia de una programación es la "
        "ausencia de protección. last_restore_tested_at es la prueba de restauración demostrada "
        "que pide la puerta de comercialización de la hoja de ruta: un dispositivo cubierto con "
        "un nulo ahí tiene programaciones, no pruebas. Se calcula en directo; es una "
        "afirmación sobre el registro en meta.generated_at."
    ),
    "A device whose most recent restorable backup point is older than this is reported stale "
    "rather than covered. A device with no restorable point at all is always stale, never "
    "covered.": (
        "Un dispositivo cuyo punto de copia restaurable más reciente es más antiguo que esto "
        "figura como obsoleto y no como cubierto. Un dispositivo sin ningún punto restaurable es "
        "siempre obsoleto, nunca cubierto."
    ),
    "Every device with its agent contact state and its open health alerts.": (
        "Cada dispositivo con el estado de contacto de su agente y sus alertas de salud abiertas."
    ),
    "One row per device: identity, OS, device group, agent status and last contact, whether "
    "that contact is stale, and the count of still-open critical and warning health alerts. "
    "Devices with no agent are reported as stale and never_enrolled rather than omitted, and "
    "devices with no open alerts are reported with zero counts rather than omitted -- so the "
    "device total is the fleet, not the subset that happens to have reported. Computed live; it "
    "is a statement about the record at `meta.generated_at`.": (
        "Una fila por dispositivo: identidad, sistema operativo, grupo de dispositivos, estado "
        "del agente y último contacto, si ese contacto está obsoleto y el recuento de alertas de "
        "salud críticas y de advertencia que siguen abiertas. Los dispositivos sin agente figuran "
        "como obsoletos y never_enrolled en lugar de omitirse, y los dispositivos sin alertas "
        "abiertas figuran con recuentos en cero en lugar de omitirse: así el total de "
        "dispositivos es el parque, no el subconjunto que ha informado. Se calcula en directo; es "
        "una afirmación sobre el registro en `meta.generated_at`."
    ),
    "A device whose agent has not been in contact within this many days is reported stale. A "
    "device with no agent at all is always stale.": (
        "Un dispositivo cuyo agente no ha estado en contacto en este número de días figura como "
        "obsoleto. Un dispositivo sin ningún agente es siempre obsoleto."
    ),
    "Only count open alerts at this severity or above. Does not filter the device list: a "
    "device with no qualifying alert still appears, with zero counts.": (
        "Cuenta solo las alertas abiertas con esta gravedad o superior. No filtra la lista de "
        "dispositivos: un dispositivo sin ninguna alerta que cumpla sigue apareciendo, con "
        "recuentos en cero."
    ),
    # MCP channel (app/api/deps.py, app/mcp/guard.py)
    "this action is never available to an AI app": (
        "esta acción nunca está disponible para una aplicación de IA"
    ),
    "MCP is not available: ITS_PUBLIC_CONSOLE_URL is not set.": (
        "MCP no está disponible: ITS_PUBLIC_CONSOLE_URL no está configurado."
    ),
    "the MCP channel marker is malformed": "la marca del canal MCP está mal formada",
    "this token also grants the console and is not accepted over MCP": (
        "este token también da acceso a la consola y no se acepta por MCP"
    ),
    "this token was issued to the console and is not accepted over MCP": (
        "este token se emitió para la consola y no se acepta por MCP"
    ),
    "MCP access must be granted by a person signing in": (
        "el acceso por MCP debe concederlo una persona al iniciar sesión"
    ),
    "this token is for the MCP server and is accepted only through it": (
        "este token es para el servidor MCP y solo se acepta a través de él"
    ),
    # MCP server (app/mcp/server.py)
    "MCP is turned off for this organization. An org-admin turns it on in "
    "Settings → AI connections.": (
        "MCP está desactivado para esta organización. Un administrador de la organización lo "
        "activa en Configuración → Conexiones de IA."
    ),
    # MCP console admin API (app/mcp/api.py)
    "{client_id} is part of the platform, not an AI app, and cannot be revoked": (
        "{client_id} forma parte de la plataforma, no es una aplicación de IA, y no se "
        "puede revocar"
    ),
    "this AI app's access was revoked; connect it again to continue": (
        "se revocó el acceso de esta aplicación de IA; vuelve a conectarla para continuar"
    ),
    "this MCP server answers only at {resource}": (
        "este servidor MCP solo responde en {resource}"
    ),
    "the MCP server does not accept requests from another web origin": (
        "el servidor MCP no acepta solicitudes desde otro origen web"
    ),
    "this token does not name the AI app it was issued to": (
        "este token no indica la aplicación de IA para la que se emitió"
    ),
    "there is no tool named {name}": "no existe ninguna herramienta llamada {name}",
    "the MCP server accepts only POST requests": (
        "el servidor MCP solo acepta solicitudes POST"
    ),
    # MCP tool calls (app/mcp/invoke.py, app/mcp/server.py)
    "re-authenticate: {detail}": "vuelve a autenticarte: {detail}",
    "not found: {detail}": "no encontrado: {detail}",
    "invalid arguments: {detail}": "argumentos no válidos: {detail}",
    "the platform could not complete this action (status {status})": (
        "la plataforma no pudo completar esta acción (estado {status})"
    ),
    "the action failed (status {status}): {detail}": (
        "la acción falló (estado {status}): {detail}"
    ),
    "the action took longer than {seconds} seconds": (
        "la acción tardó más de {seconds} segundos"
    ),
    "the action returned something other than JSON, which an AI app cannot receive": (
        "la acción devolvió algo que no es JSON, y una aplicación de IA no puede recibirlo"
    ),
    "blocked: the result contained a secret-looking field at {path}": (
        "bloqueado: el resultado contenía un campo que parece secreto en {path}"
    ),
    "at most {count} requests per minute to this MCP server; try again in {seconds} seconds": (
        "como máximo {count} solicitudes por minuto a este servidor MCP; vuelve a intentarlo en "
        "{seconds} segundos"
    ),
    "the action is still running after {seconds} seconds and its outcome is unknown; "
    "check before retrying": (
        "la acción sigue en curso después de {seconds} segundos y no se sabe cómo terminó; "
        "compruébalo antes de reintentarla"
    ),
    "a path value cannot be empty, '.', '..', or contain / or \\": (
        "un valor de ruta no puede estar vacío, ser '.' o '..', ni contener / o \\"
    ),
    "the arguments do not address this tool's own route": (
        "los argumentos no apuntan a la ruta propia de esta herramienta"
    ),
    "the error's details were withheld because they contained a secret-looking field": (
        "se ocultaron los detalles del error porque contenían un campo que parece secreto"
    ),
    "this tool cannot reach that route": "esta herramienta no puede llegar a esa ruta",
}
