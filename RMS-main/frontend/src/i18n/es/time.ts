export const time = {
  justNow: "ahora mismo",
  ago: {
    minute: { one: "hace {count} minuto", other: "hace {count} minutos" },
    hour: { one: "hace {count} hora", other: "hace {count} horas" },
    day: { one: "hace {count} día", other: "hace {count} días" },
  },
  in: {
    minute: { one: "en {count} minuto", other: "en {count} minutos" },
    hour: { one: "en {count} hora", other: "en {count} horas" },
    day: { one: "en {count} día", other: "en {count} días" },
  },
  units: {
    second: { one: "{count} segundo", other: "{count} segundos" },
    minute: { one: "{count} minuto", other: "{count} minutos" },
    hour: { one: "{count} hora", other: "{count} horas" },
    day: { one: "{count} día", other: "{count} días" },
  },
} as const;
