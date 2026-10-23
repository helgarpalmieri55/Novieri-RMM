import type { Shape } from "../types";
import type { aiAdmin as en } from "../en/aiAdmin";

export const aiAdmin: Shape<typeof en> = {
  egress: {
    heading: "Qué sale de esta red cuando responde el asistente",
    leaves1: "El prompt del sistema, que es una constante.",
    leaves2: "Los esquemas de las herramientas: la forma de las API de los módulos, es decir, estructura y no datos de clientes.",
    leaves3: "El texto de la pregunta del administrador.",
    leaves4:
      "Los resultados de las herramientas: datos reales de clientes. Nombres de host, asuntos y cuerpos de tickets, inventario de software, texto de alertas, nombres de licencias y de proveedores, cifras de gasto y nombres de las acciones de auditoría.",
    never1: "El contenido de la bóveda: credenciales almacenadas y claves de licencia.",
    never2: "Los bytes de los archivos adjuntos, las grabaciones de sesiones y los certificados de los agentes.",
    never3: "Todo lo que pertenezca a otra organización.",
    contractual:
      "Las condiciones de no entrenamiento y de no conservación con un proveedor alojado son contractuales. Esta plataforma no puede hacerlas cumplir y no finge que puede.",
    selfHosted:
      "Lo predeterminado es un proveedor autoalojado y compatible con OpenAI en tu propia red: con él, no sale nada.",
  },
  provider: {
    offSummary:
      "El asistente está desactivado para todo el mundo en este servidor porque no hay ningún proveedor de modelos configurado. Alguien con acceso al servidor tiene que configurar uno.",
    adminHeading: "Qué tiene que configurar el administrador del servidor",
    adminDetail:
      "Define ITS_AI_PROVIDERS y las variables correspondientes ITS_AI_<KEY>_KIND, _BASE_URL y _MODEL en el plano de control, y luego reinícialo. Se puede nombrar exactamente un proveedor.",
  },
  settings: {
    title: "Configuración del asistente",
    description: "Lo que envía el asistente al exterior y lo que ha marcado.",
    whatLeaves: "Lo que sale",
    whatNeverLeaves: "Lo que nunca sale",
    providerTitle: "Proveedor",
    providerSubtitle: "Configuración del servidor, igual para todas las organizaciones",
    loading: "Cargando…",
    modelProvider: "Proveedor de modelos",
    serverConfig:
      "Esto es configuración del servidor y el mismo proveedor atiende a todas las organizaciones de este despliegue. No se elige aquí, y aquí no se muestra ni se introduce ninguna clave de API ni URL base. Cambiarlo implica editar el entorno del plano de control y reiniciarlo.",
    otherSettings:
      "Cuánto tiempo se conservan las conversaciones, los límites de preguntas por persona y por organización y el límite diario de uso están en Configuración → Asistente.",
  },
  chat: {
    title: "Asistente",
    description:
      "Pregunta por tus dispositivos, alertas y tickets. Lee lo que la plataforma ya sabe y nunca ejecuta nada en un dispositivo.",
    checking: "Comprobando si el asistente está disponible…",
    unavailable: "el asistente no está disponible aquí",
    settingsLink: "Configuración del asistente",
    openSettings: "Abrir la configuración del asistente",
    turnedOnBy:
      "El asistente lo activa quien administra este servidor, no se hace desde la consola. Alguien con permiso para administrar el asistente puede ver cómo está configurado.",
    invitation: "¿Qué te gustaría saber?",
    examples:
      "Prueba con «qué dispositivos no tienen la actualización acumulativa de septiembre» o «por qué falló la copia de seguridad de fs-archive-01». Las respuestas son de solo lectura y están limitadas por tus propios permisos.",
    askLabel: "Haz una pregunta",
    askPlaceholder: "Pregunta por el parque",
    asking: "Preguntando…",
    ask: "Preguntar",
    recentTitle: "Sesiones recientes",
    recentSubtitle:
      "Cada pregunta, lo que consultó el asistente y lo que propuso queda en el registro de auditoría",
    noSessions: "Aún no hay sesiones.",
    name: "Nombre",
    when: "Cuándo",
    sessionSub: {
      one: "{persona} · {count} mensaje",
      other: "{persona} · {count} mensajes",
    },
  },
  messages: {
    injection:
      "El contenido de {records} incluye texto dirigido a un lector automatizado. Se trató como datos y no se actuó sobre él.",
    partial: "Esta respuesta es parcial: {because}.",
    partialUnknown: "Esta respuesta es parcial: el turno se detuvo antes de terminar.",
    stop: {
      tool_budget: "se alcanzó el límite de llamadas a herramientas de este turno",
      byte_budget: "se alcanzó el límite de datos recuperados de este turno",
      token_budget: "se alcanzó el límite de tokens de salida de este turno",
      deadline: "se alcanzó el tiempo límite de este turno",
      malformed: "pidió varias veces la misma información de forma incorrecta",
      nonce_leak: "el contenido recuperado incluía una marca interna y se detuvo el turno",
      provider_error: "el servicio del modelo de lenguaje no respondió",
      session_expired: "tu sesión caducó mientras trabajaba",
    },
  },
  trace: {
    status: {
      forbidden: "no tienes permiso",
      not_found: "no existe ese registro",
      unknown_tool: "el asistente pidió una herramienta que no existe",
      invalid_arguments: "el asistente la pidió de forma incorrecta",
      blocked: "retenido: el resultado podía contener material secreto",
      timeout: "se agotó el tiempo",
      upstream_error: "falló",
      session_expired: "tu sesión caducó",
    },
    records: {
      one: "{count} registro",
      other: "{count} registros",
    },
    recordsTruncated: {
      one: "{count} registro; no se mostraron más",
      other: "{count} registros; no se mostraron más",
    },
  },
  flags: {
    title: "Contenido recuperado marcado",
    subtitle: "Texto dirigido a un lector automatizado, tratado como datos y sin actuar sobre él",
    unavailable: "El contenido marcado no está disponible. {sentence}",
    empty: "No se ha marcado ningún contenido recuperado.",
    record: "Registro",
    rule: "Regla",
    excerpt: "Fragmento",
    foundWhile: "encontrado con la herramienta «{tool}»",
    unnamedRule: "Regla sin nombre",
  },
};
