"""Spanish (es) strings."""

STRINGS: dict[str, str] = {
    # -- welcome / help --
    "welcome": (
        "👋 ¡Bienvenido a *{bot_name}*!\n\n"
        "Envío recordatorios de las Santas Convocaciones de Dios, "
        "servicios especiales y eventos.\n\n{commands}"
    ),
    "user_commands": (
        "*Comandos disponibles:*\n"
        "/help — mostrar este mensaje\n"
        "/events — próximos eventos (próximos 30 días)\n"
        "/exportcalendar — descargar un archivo de calendario ICS\n"
        "/appointment — solicitar una reunión con un oficial de la iglesia\n"
        "/myappointments — ver tus citas\n"
        "/cancelappointment — cancelar una cita pendiente o confirmada\n"
        "/reschedule — proponer una nueva hora para una cita próxima\n"
        "/settimezone — establecer tu zona horaria para las horas mostradas\n"
        "/language — elegir tu idioma\n"
        "/notifications — elegir qué recordatorios recibes\n"
        "/donate — apoyar a la congregación con una ofrenda\n"
        "/stop — cancelar la suscripción a las notificaciones"
    ),
    "share_contact_prompt": (
        "Para personalizar tu experiencia, comparte tu contacto "
        "(toca el botón de abajo). Puedes tocar Omitir si prefieres no hacerlo."
    ),
    "share_contact_button": "📱 Compartir mi contacto",
    "unsubscribed": "Has cancelado la suscripción. Envía /start para volver a suscribirte.",
    "bcast_individual_subscribers": "Suscriptores individuales",

    # -- events --
    "events_header": "*Próximos eventos (próximos 30 días):*\n",
    "events_none": "No hay eventos en los próximos 30 días.",

    # -- my appointments --
    "myappts_header": "*Tus citas:*",
    "myappts_none": "No tienes citas registradas.",
    "section_upcoming": "\n*Próximas:*",
    "section_past": "\n*Pasadas:*",
    "appt_line": "• Con: {counterparty}\n   {when} — *{status}*\n   _ID: {id}_",

    # -- cancel appointment --
    "cancel_none": "No tienes citas activas para cancelar.",
    "cancel_list_header": "*Tus citas activas:*\nElige una para cancelar:",
    "cancel_confirm_prompt": (
        "¿Cancelar la cita con *{official}* el {when}?\n\n"
        "Toca ✅ Sí para confirmar la cancelación, o ✖️ No para conservarla."
    ),
    "cancel_aborted": "Cancelación anulada.",
    "cancel_past": "Esa cita ya tuvo lugar y no se puede cancelar.",

    # -- reschedule appointment --
    "resched_none": "No tienes citas próximas para reprogramar.",
    "resched_list_header": "*Reprogramar una cita*\nElige una para reprogramar:",
    "resched_ask_time": "Ingresa la nueva fecha y hora (YYYY-MM-DD HH:MM):",
    "resched_bad_format": "Usa el formato YYYY-MM-DD HH:MM:",
    "resched_past": "Esa fecha/hora ya pasó. Ingresa una hora futura (YYYY-MM-DD HH:MM):",
    "resched_overlap": "Esa hora se solapa con otra de tus citas. Elige otra hora (YYYY-MM-DD HH:MM):",
    "resched_no_longer": "Esa cita ya no se puede reprogramar.",
    "resched_sent": "✅ Tu solicitud de reprogramación ha sido enviada. Se te notificará cuando sea aceptada o rechazada.",
    "cancel_done_by_official_to_user": (
        "❌ Tu cita (ID: `{id}`) con *{official}* "
        "ha sido cancelada por el oficial."
    ),
    "cancel_done_official_ack": (
        "✅ Cita `{id}` cancelada. Se ha notificado al solicitante."
    ),
    "cancel_done_requester_ack": "✅ Cita `{id}` cancelada.",
    "cancel_done_requester_ack_notified": (
        "✅ Cita `{id}` cancelada. Se ha notificado al oficial."
    ),

    # -- appointment request flow --
    "appt_choose_official": "*Solicitar una cita*\n\n¿Con quién te gustaría reunirte?",
    "appt_invalid_number": "Selección no válida.",
    "appt_limit_reached": (
        "Has alcanzado el límite de {max} citas con {official} "
        "en cualquier período de {days} días.\n\n"
        "Elige una fecha fuera de ese período, o cancela una cita "
        "existente con /cancelappointment."
    ),
    "appt_ask_date": "Fecha deseada (YYYY-MM-DD):",
    "appt_bad_date": "Usa el formato YYYY-MM-DD:",
    "appt_ask_time": "Hora deseada (HH:MM, formato 24 horas):",
    "appt_bad_time": "Usa el formato HH:MM:",
    "appt_bad_datetime": "Esa fecha/hora no es válida. Vuelve a ingresar la fecha (YYYY-MM-DD):",
    "appt_past": "Esa fecha/hora ya pasó. Ingresa una fecha futura (YYYY-MM-DD):",
    "appt_too_far": (
        "Las citas se pueden reservar con un máximo de {months} meses de antelación "
        "(hasta {until}). Ingresa una fecha más cercana (YYYY-MM-DD):"
    ),
    "appt_overlap": (
        "Esa hora se solapa con tu cita existente con {official} "
        "el {when} (ID: `{id}`).\n\nElige otra fecha/hora (YYYY-MM-DD):"
    ),
    "appt_ask_desc": "Breve descripción del motivo de la reunión (máximo 128 caracteres):",
    "appt_summary": (
        "*Resumen de la solicitud de cita:*\n"
        "Con: {official}\n"
        "Cuándo: {when}\n"
        "Descripción: {desc}\n\n"
        "¿Enviar? (sí/no)"
    ),
    "appt_request_cancelled": "Solicitud cancelada.",
    "appt_overlap_not_submitted": (
        "Esa hora se solapa con tu cita con {official} "
        "(ID: `{id}`). Solicitud no enviada."
    ),
    "appt_limit_not_submitted": (
        "Has alcanzado el límite de {max} citas con {official} "
        "en cualquier período de {days} días. Solicitud no enviada."
    ),
    "appt_cooldown": (
        "⏳ Acabas de hacer un cambio de cita. Espera unos "
        "{seconds} segundo(s) más antes de enviar otra solicitud."
    ),
    "appt_too_many_pending": (
        "📋 Ya tienes {max} solicitud(es) pendiente(s) esperando respuesta. "
        "Espera a que una sea confirmada o rechazada antes de enviar otra."
    ),
    "appt_submitted": (
        "✅ *¡Solicitud enviada!* (ID: `{id}`)\n"
        "Te notificaré cuando tu solicitud sea aceptada, rechazada "
        "o se sugiera una nueva hora."
    ),
    "appt_confirmed_user": (
        "✅ *¡Tu cita (ID: `{id}`) ha sido confirmada!*\n"
        "Con: {official}\n"
        "Cuándo: {when}\n\n"
        "Se adjunta un archivo de calendario ICS."
    ),
    "appt_ics_caption": "Importa este archivo en tu aplicación de calendario.",

    # -- notifications --
    "notif_reminder_title": "🔔 *Recordatorio: {name}*",
    "notif_service_begins": "El servicio comienza: {when}",
    "notif_join": "🔗 Unirse: {url}",
    "notif_announcements_header": "⚠️ *Anuncios:*",

    # -- /settimezone --
    "tz_prompt": (
        "*Establece tu zona horaria*\n\n"
        "Toca una zona abajo o escribe cualquier nombre de zona IANA "
        "(p. ej. `America/New_York`):"
    ),
    "tz_invalid": "Esa no es una zona horaria reconocida. Inténtalo de nuevo (o /cancel):",
    "tz_set": "✅ Tu zona horaria se ha establecido a *{tz}*.\nHora local actual: {now}",

    # -- /language --
    "lang_prompt": "*Elige tu idioma*",
    "lang_set": "✅ Idioma establecido a *{language}*.",

    # -- /notifications (recordatorios personales opcionales) --
    "notif_cat_convocations": "Sábado y otras Santas Convocaciones",
    "notif_cat_sunday_prayer": "Oración del domingo por la mañana",
    "notif_cat_special": "Eventos especiales",
    "notif_prefs_prompt": (
        "*Recordatorios personales*\n\n"
        "Elige qué recordatorios quieres recibir directamente. "
        "Es útil si no estás en un chat de grupo de la iglesia. "
        "Toca para activar/desactivar cada uno y luego toca Listo."
    ),
    "notif_prefs_done": "✔️ Listo",
    "notif_prefs_none": (
        "Has desactivado todos los recordatorios personales. "
        "Puedes volver a activarlos en cualquier momento con /notifications."
    ),
    "notif_prefs_saved": "✅ Recibirás recordatorios personales de:\n{list}",

    # -- temas de /help --
    "help_topic_hint": "Para más detalles de un comando, envía `/help <comando>` (p. ej. `/help appointment`).",
    "help_unknown_topic": "No tengo ayuda para eso. Prueba con: {topics}",
    "help_appointment": (
        "*/appointment* — Solicita una reunión con un oficial de la iglesia.\n\n"
        "Elige al oficial y luego ingresa una fecha y hora. Él (o su representante) "
        "confirmará, rechazará o sugerirá otra hora. Una vez confirmada recibes un "
        "archivo de calendario."
    ),
    "help_myappointments": (
        "*/myappointments* — Muestra tus citas próximas y pasadas y su estado."
    ),
    "help_cancelappointment": (
        "*/cancelappointment* — Cancela una cita próxima. Elígela de la lista y confirma. "
        "Se notifica a la otra parte. Las citas pasadas no se pueden cancelar."
    ),
    "help_reschedule": (
        "*/reschedule* — Propone una nueva hora para una cita próxima. Elígela, ingresa "
        "la nueva fecha/hora, y la otra parte acepta o rechaza. Si se rechaza, se mantiene "
        "la hora original. Las nuevas horas no pueden estar en el pasado."
    ),
    "help_events": (
        "*/events* — Muestra las convocaciones, servicios y eventos de los próximos 30 días, "
        "con sus enlaces para unirse."
    ),
    "help_exportcalendar": (
        "*/exportcalendar* — Descarga un archivo de calendario ICS de los próximos eventos "
        "para importarlo a tu aplicación de calendario."
    ),
    "help_settimezone": (
        "*/settimezone* — Establece tu zona horaria para ver fechas y horas en tu hora local. "
        "Toca una zona común o escribe un nombre IANA (p. ej. `America/New_York`)."
    ),
    "help_language": "*/language* — Elige el idioma que el bot usa contigo.",
    "help_notifications": (
        "*/notifications* — Elige qué recordatorios recibes como mensajes personales "
        "(Sábado y Santas Convocaciones, Oración del domingo por la mañana, Eventos especiales). "
        "Útil si no estás en un chat de grupo de la iglesia."
    ),
    "help_donate": (
        "*/donate* — Apoya a la congregación con una ofrenda. Abre una página "
        "segura de donaciones donde puedes contribuir."
    ),

    # -- /donate --
    "donate_message": (
        "🙏 *Apoya a Christ of God Ministries*\n\n"
        "Tu generosidad ayuda a sostener nuestro ministerio y alcance. "
        "Si deseas contribuir, toca el botón de abajo — "
        "¡gracias, y que el SEÑOR te bendiga!"
    ),
    "donate_button": "💝 Donar ahora",
    "donate_not_configured": (
        "Las donaciones en línea aún no están configuradas. Habla con el "
        "Anciano L. Williams sobre las formas de contribuir."
    ),

    # -- recordatorios de citas --
    "appt_reminder_user": (
        "⏰ *Recordatorio de cita*\n\n"
        "Tu cita con *{counterparty}* es {when}.\n_ID: {id}_"
    ),
    "appt_reminder_official": (
        "⏰ *Recordatorio de cita*\n\n"
        "Tu cita con *{counterparty}* es {when}.\n_ID: {id}_"
    ),

    # -- appointment statuses --
    "status_pending": "pendiente",
    "status_confirmed": "confirmada",
    "status_counter_proposed": "hora propuesta",
    "status_cancelled": "cancelada",
    "status_declined": "rechazada",
}
