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
    subtitle: "Contraseñas y claves, administradas donde corresponde",
    note: "Cada una de estas acciones ejecuta un paso en las páginas de inicio de sesión y te devuelve a ITMS. Esta plataforma nunca maneja contraseñas, segundos factores ni claves de acceso.",
    startFailed: "No se pudo iniciar ese paso, así que no se cambió nada de tu inicio de sesión. {reason}",
    changePassword: {
      label: "Cambiar contraseña",
      hint: "Primero se te pedirá tu contraseña actual. Esta plataforma nunca ve ninguna de las dos.",
    },
    manageMfa: {
      label: "Administrar dispositivo MFA",
      hint: "Añade, reemplaza o elimina tu segundo factor.",
    },
    addPasskey: {
      label: "Añadir una clave de acceso",
      hint: "Una credencial sin contraseña vinculada a este dispositivo.",
    },
  },
};
