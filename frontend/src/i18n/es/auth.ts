import type { Shape } from "../types";
import type { auth as en } from "../en/auth";

/**
 * Glossary (`es/index.ts`): passkey → clave de acceso; sign in → iniciar
 * sesión; computer → equipo; recovery codes → códigos de recuperación;
 * invitation → invitación; tú.
 */
export const auth: Shape<typeof en> = {
  common: {
    email: "Correo electrónico",
    password: "Contraseña",
    working: "Un momento…",
    startOver: "Empezar de nuevo",
    backToSignIn: "Volver al inicio de sesión",
    tooMany: "Demasiados intentos. Espera un minuto y vuelve a intentarlo.",
    wait: {
      one: "Demasiados intentos. Espera {count} segundo y vuelve a intentarlo.",
      other: "Demasiados intentos. Espera {count} segundos y vuelve a intentarlo.",
    },
    timedOut: "Este inicio de sesión caducó. Empieza de nuevo desde la página de inicio de sesión.",
  },
  signIn: {
    title: "Iniciar sesión",
    intro: "Usa el correo electrónico y la contraseña de tu cuenta. Después viene tu segundo factor.",
    submit: "Continuar",
    rejected: "Ese correo electrónico y esa contraseña no corresponden a ninguna cuenta de aquí.",
    forgot: "¿Olvidaste tu contraseña?",
  },
  portalSignIn: {
    title: "Entrar al portal",
    intro: "Usa una clave de acceso, o tu correo y tu contraseña.",
    usePasskey: "Entrar con una clave de acceso",
    passkeyRejected: "No se aceptó esa clave de acceso. Inténtalo de nuevo, o usa tu correo y tu contraseña.",
  },
  factor: {
    title: "Confirma que eres tú",
    intro: "Usa tu segundo factor para terminar de iniciar sesión.",
    passkeyTitle: "Clave de acceso",
    passkeyHint: "La clave de acceso de este dispositivo, de tu teléfono o de una llave de seguridad.",
    usePasskey: "Usar una clave de acceso",
    totpTitle: "Aplicación de autenticación",
    totpLabel: "Código de seis dígitos de tu aplicación de autenticación",
    totpSubmit: "Verificar el código",
    showRecovery: "Usar un código de recuperación",
    recoveryTitle: "Uno de tus códigos de recuperación",
    recoveryLabel: "Código de recuperación",
    recoveryHint:
      "Uno de los diez códigos que guardaste al configurar tu segundo factor. Cada uno sirve una sola vez.",
    recoverySubmit: "Usar el código de recuperación",
    remember: {
      one: "No volver a preguntar en este equipo durante {count} día",
      other: "No volver a preguntar en este equipo durante {count} días",
    },
    rememberHint: "Déjalo sin marcar en un equipo que usan otras personas.",
    rejected: "No se aceptó. Vuelve a intentarlo.",
  },
  enroll: {
    title: "Añade un segundo factor",
    intro:
      "Tu cuenta necesita un segundo factor antes de que se abra la consola. Una clave de acceso es lo más rápido; una aplicación de autenticación funciona en cualquier teléfono.",
    passkeyTitle: "Clave de acceso (recomendada)",
    passkeyHint:
      "Se guarda en este dispositivo, en tu teléfono o en una llave de seguridad, y se desbloquea con tu huella, tu cara o tu PIN.",
    passkeyName: "Nombre de esta clave de acceso",
    passkeyDefaultName: "Mi clave de acceso",
    createPasskey: "Crear una clave de acceso",
    totpTitle: "Aplicación de autenticación",
    totpHint: "Cualquier aplicación de autenticación que muestre códigos de seis dígitos que cambian cada 30 segundos.",
    startTotp: "Configurar una aplicación de autenticación",
    setupKey: "En tu aplicación, añade una cuenta e introduce esta clave de configuración:",
    openInApp: "Abrir en una aplicación de autenticación",
    totpLabel: "Código de seis dígitos que muestra ahora la aplicación",
    confirmTotp: "Confirmar el código",
    rejected: "No se aceptó ese código. Comprueba la hora de tu teléfono e introduce el código más reciente.",
    passkeyRejected: "No se aceptó esa clave de acceso. Inténtalo de nuevo o usa una aplicación de autenticación.",
    factorSaved: "Tu segundo factor está guardado. Aún no se pudieron generar los códigos de recuperación.",
    retryFinish: "Reintentar",
    alreadySignedIn:
      "Ya iniciaste sesión: la primera respuesta se perdió por el camino. Tus códigos de recuperación se generaron, pero no se pueden volver a mostrar; genera otros nuevos en Tu seguridad.",
    finishExpired:
      "Se acabó el tiempo para terminar este paso. Empieza de nuevo desde la página de inicio de sesión: entrarás con el segundo factor que acabas de añadir y podrás generar códigos de recuperación en Tu seguridad.",
  },
  codes: {
    title: "Guarda tus códigos de recuperación",
    intro:
      "Si pierdes tu segundo factor, cada uno de estos códigos te deja iniciar sesión una vez. Guárdalos en un lugar seguro, lejos de este equipo: solo se muestran ahora.",
    list: "Tus códigos de recuperación",
    copy: "Copiar los códigos",
    copied: "Los códigos están en el portapapeles.",
    copyFailed: "El navegador no quiso copiarlos. Selecciona los códigos y cópialos tú.",
    saved: "He guardado estos códigos en un lugar seguro",
    continue: "Continuar a la consola",
    none: "No hay códigos que mostrar. Los códigos de recuperación aparecen una sola vez, justo después de crearse; puedes crear otros nuevos en Inicio de sesión y seguridad.",
  },
  forgot: {
    title: "Restablece tu contraseña",
    intro:
      "Introduce el correo con el que inicias sesión. Si corresponde a una cuenta de aquí, se le envía un enlace para elegir una contraseña nueva.",
    submit: "Enviar el enlace",
    sent: "Si ese correo corresponde a una cuenta de aquí, el enlace va de camino. Sirve una sola vez, durante 30 minutos. Después se te sigue pidiendo tu segundo factor.",
  },
  reset: {
    title: "Elige una contraseña nueva",
    submit: "Cambiar la contraseña",
    done: "Tu contraseña cambió. Inicia sesión con ella; se te sigue pidiendo tu segundo factor.",
    signIn: "Iniciar sesión",
    invalid: "Este enlace caducó o ya se usó.",
    askAgain: "Pedir un enlace nuevo",
  },
  invite: {
    heading: "Tu invitación",
    loading: "Abriendo tu invitación…",
    title: "Te damos la bienvenida, {name}",
    intro: "Te invitaron a esta consola como <b>{email}</b>. Elige una contraseña; después añadirás tu segundo factor.",
    submit: "Guardar la contraseña y continuar",
    invalid: "Esta invitación caducó, ya se usó o se canceló. Pide una nueva a quien te invitó.",
  },
  recover: {
    heading: "Recuperación de la cuenta",
    loading: "Abriendo tu enlace de recuperación…",
    title: "Recupera tu cuenta",
    intro: "Este enlace se creó en el servidor para <b>{email}</b>.",
    what: "Al recuperarla se quitan los segundos factores y los códigos de recuperación de esta cuenta. Después añades un segundo factor nuevo y guardas códigos nuevos antes de que se abra la consola.",
    newPassword:
      "Tu contraseña se puso por última vez con un enlace enviado por correo. Elige una contraseña nueva para terminar de recuperar tu cuenta; la anterior deja de funcionar.",
    optionalPassword:
      "Opcional: elige ahora una contraseña nueva, y la anterior deja de funcionar. Deja los dos campos vacíos para conservar tu contraseña actual.",
    submit: "Recuperar mi cuenta",
    invalid: "Este enlace de recuperación caducó o ya se usó. Alguien con acceso al servidor puede crear otro.",
  },
  password: {
    new: "Contraseña nueva",
    confirm: "La misma contraseña otra vez",
    rules: "Al menos 12 caracteres, los que quieras, espacios incluidos. Una frase que recuerdes funciona bien.",
    mismatch: "Las dos contraseñas no son iguales.",
    too_short: "Usa al menos 12 caracteres.",
    too_long: "Usa como mucho 128 caracteres.",
  },
  passkey: {
    unsupported: "Este navegador no puede usar claves de acceso. Usa una aplicación de autenticación.",
    hiddenUseAppOrCode:
      "Este navegador no puede usar claves de acceso, así que aquí no se ofrece la tuya. Usa tu aplicación de autenticación o uno de tus códigos de recuperación.",
    hiddenUseApp:
      "Este navegador no puede usar claves de acceso, así que aquí no se ofrece la tuya. Usa tu aplicación de autenticación.",
    hiddenUseCode:
      "Este navegador no puede usar claves de acceso, así que aquí no se ofrece la tuya. Usa uno de tus códigos de recuperación, o un navegador que admita claves de acceso.",
    hiddenNoOther:
      "Este navegador no puede usar claves de acceso, y tu cuenta no tiene otro segundo factor. Inicia sesión desde un navegador que admita claves de acceso, o pide a un administrador que restablezca tus segundos factores.",
    cancelled: "La ventana de la clave de acceso se cerró o caducó, así que no se usó nada.",
    alreadyRegistered: "Este dispositivo ya tiene una clave de acceso para tu cuenta.",
    noUserVerification:
      "Esta clave de acceso o este dispositivo no puede confirmar que eres tú con huella, rostro o PIN, y es obligatorio. Usa otra clave de acceso o una aplicación de autenticación.",
    aborted: "Otra solicitud de clave de acceso reemplazó a esta. Inténtalo de nuevo.",
    generic: "No se pudo usar la clave de acceso en este dispositivo. Inténtalo de nuevo o usa otro método.",
  },
};
