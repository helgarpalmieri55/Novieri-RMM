import type { Shape } from "../types";
import type { security as en } from "../en/security";

export const security: Shape<typeof en> = {
  page: {
    title: "Registros de seguridad",
    description: "Inicios y cierres de sesión y uso de derechos de administrador, tal como los notificó el agente de cada dispositivo.",
    exportEvidence: "Exportar CSV de evidencias ({range})",
    copyLink: "Copiar enlace a esta vista",
    csvCovers:
      "El CSV abarca {window} e incluye los filtros de usuario y de tipo de evento de esta pantalla, así que el archivo es lo que estás viendo.",
    linkCopied: "Enlace copiado. Vuelve a abrir esta búsqueda con los mismos filtros.",
    exportFailed: "La exportación ha fallado. {reason}",
    window24h: "las últimas 24 horas",
    window7d: "los últimos 7 días",
    window30d: "los últimos 30 días",
    loadedNote: {
      one: "Sobre {count} evento cargado",
      other: "Sobre {count} eventos cargados",
    },
    tiles: {
      events: "Eventos en el periodo",
      eventsZero: "No se recogió nada en este periodo",
      failed: "Inicios de sesión fallidos",
      failedZero: "Ningún inicio de sesión fallido en este periodo",
      accounts: "Cuentas vistas",
      accountsZero: "No aparece ninguna cuenta en este periodo",
    },
    filters: {
      usernamePlaceholder: "Usuario",
      usernameLabel: "Buscar por usuario",
      eventTypeLabel: "Filtrar por tipo de evento",
      allEventTypes: "Todos los tipos de evento",
      timeRange: "Periodo",
    },
    emptyFiltered: {
      title: "Ningún evento coincide con estos filtros",
      body: "No hay nada en {window} que coincida con {filters}. Puede que el filtro sea más estrecho que la evidencia, o que esté escrito de forma distinta al nombre que notificó el equipo.",
      filterUsername: "usuario «{username}»",
      filterEventType: "tipo de evento «{eventType}»",
      widen: "Ampliar a {range}",
      clear: "Quitar filtros",
    },
    empty: {
      title: "No hay eventos en este periodo",
      body: "Ningún evento de seguridad llegó a esta organización en {window}. Aquí un registro vacío y un agente que no recoge nada se ven igual.",
    },
    columns: {
      event: "Evento",
      host: "Equipo",
      user: "Usuario",
      when: "Cuándo",
    },
    noAccount: "Sin cuenta registrada",
    raw: {
      title: "Evento sin procesar",
      subtitle: "Campos almacenados de la fila seleccionada",
      loading: "Cargando…",
      noFields: "Este evento no tiene campos almacenados.",
      noneTitle: "Ningún evento seleccionado",
      noneBody: "Elige una fila de la lista de eventos para ver los campos almacenados con ella.",
    },
  },
  prerequisites: {
    title: "Antes de poder recoger eventos de seguridad",
    label: "Un equipo Windows o Mac ejecuta el agente",
    done: {
      one: "Equipo que ejecuta el agente: {windows} Windows y {mac} Mac.",
      other: "Equipos que ejecutan el agente: {windows} Windows y {mac} Mac.",
    },
    missing:
      "El agente recoge estos eventos solo en equipos Windows y Mac, y ninguno ejecuta todavía el agente, así que aquí no llegará nada.",
    cta: "Añadir un equipo Windows o Mac",
  },
};
