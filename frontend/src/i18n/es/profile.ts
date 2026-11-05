import type { Shape } from "../types";
import type { profile as en } from "../en/profile";

export const profile: Shape<typeof en> = {
  page: {
    title: "Tu perfil",
    description: "Cómo te muestra esta consola y dónde estás.",
    loading: "Cargando…",
    signedInAs:
      "Has iniciado sesión como {email}. Aquí no se cambia tu dirección de inicio de sesión: para hacerlo hace falta una reautenticación y una verificación que esta consola no realiza. Pídeselo a un administrador.",
    displayName: "Nombre para mostrar",
    phone: "Teléfono",
    timezone: "Zona horaria",
    timezonePlaceholder: "p. ej., Europe/Lisbon",
    smsNote: "Nada en esta plataforma envía SMS. Un número de teléfono aquí es información de directorio, no un segundo factor.",
    noChanges: "No hay cambios sin guardar",
    languageNotMirrored: "Guardado. Las páginas de inicio de sesión no se pudieron cambiar a este idioma y podrían seguir apareciendo en el anterior.",
  },
  security: {
    title: "Inicio de sesión y seguridad",
    subtitle: "Tu contraseña, tus segundos factores y los equipos en los que confías",
    note: "Tu contraseña, tus claves de acceso, tu aplicación de autenticación y tus códigos de recuperación tienen su propia página. Cada cambio allí te pide antes confirmar tu segundo factor.",
    open: "Administrar el inicio de sesión y la seguridad",
  },
};
