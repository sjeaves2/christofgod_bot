"""Lightweight localization (i18n) for user-facing strings.

Adding a language means adding a new entry to CATALOG and listing it in
AVAILABLE_LANGUAGES — no code changes elsewhere. Any missing key falls back to
English. Conventions kept consistent across languages:
  - Slash-command names (e.g. "/events") are left untranslated.
  - The literal date/time format tokens "YYYY-MM-DD" and "HH:MM" are kept as-is
    so they match what the parser expects.
  - {placeholders} must be preserved exactly.

Note: values substituted at runtime — event names, appointment statuses, and
formatted dates ({when}) — are not themselves translated.

Usage:
    from localization import t
    t("events_none", lang)                      # simple lookup
    t("appt_confirmed_user", lang, id="ABC", when="...")  # with placeholders
"""

from __future__ import annotations

from datetime import datetime

from babel.dates import format_date, format_time

DEFAULT_LANG = "en"

# Languages offered to users via /language (code -> display name).
AVAILABLE_LANGUAGES: dict[str, str] = {
    "en": "English",
    "es": "Español",
    "fr": "Français",
    "zu": "isiZulu",
}


def localized_datetime(dt: datetime, lang: str | None = None) -> str:
    """Format an (already tz-aware, already tz-converted) datetime for *lang*.

    Uses Babel/CLDR locale data for correct weekday & month names and ordering,
    then appends the timezone abbreviation (e.g. EDT). Falls back to English on
    any unknown locale.
    """
    locale = lang if lang in AVAILABLE_LANGUAGES else DEFAULT_LANG
    try:
        date_part = format_date(dt, format="full", locale=locale)
        time_part = format_time(dt, format="short", locale=locale)
    except Exception:
        date_part = format_date(dt, format="full", locale=DEFAULT_LANG)
        time_part = format_time(dt, format="short", locale=DEFAULT_LANG)
    tz_abbr = dt.strftime("%Z")
    result = f"{date_part}, {time_part} {tz_abbr}".strip()
    # Babel/CLDR uses narrow/no-break spaces (e.g. before AM/PM); normalize to
    # plain spaces for predictable display and matching.
    return result.translate({0x202F: " ", 0x00A0: " "})


def status_label(status: str | None, lang: str | None = None) -> str:
    """Localized label for an appointment status, falling back to the raw value."""
    if not status:
        return ""
    return t(f"status_{status}", lang) if f"status_{status}" in CATALOG[DEFAULT_LANG] else status

CATALOG: dict[str, dict[str, str]] = {
    "en": {
        # -- welcome / help --
        "welcome": (
            "👋 Welcome to *{bot_name}*!\n\n"
            "I send reminders for God's Holy Convocations, special services, "
            "and events.\n\n{commands}"
        ),
        "user_commands": (
            "*Available commands:*\n"
            "/help — show this message\n"
            "/events — upcoming events (next 30 days)\n"
            "/exportcalendar — download an ICS calendar file\n"
            "/appointment — request a meeting with a church official\n"
            "/myappointments — list your appointments\n"
            "/cancelappointment — cancel a pending or confirmed appointment\n"
            "/reschedule — propose a new time for an upcoming appointment\n"
            "/settimezone — set your time zone for displayed times\n"
            "/language — choose your language\n"
            "/notifications — choose which reminders you receive\n"
            "/stop — unsubscribe from notifications"
        ),
        "share_contact_prompt": (
            "To personalise your experience, please share your contact "
            "(tap the button below). You can tap Skip if you prefer not to."
        ),
        "share_contact_button": "📱 Share my contact",
        "unsubscribed": "You have been unsubscribed. Send /start to re-subscribe.",
        "bcast_individual_subscribers": "Individual subscribers",

        # -- events --
        "events_header": "*Upcoming Events (next 30 days):*\n",
        "events_none": "No events in the next 30 days.",

        # -- my appointments --
        "myappts_header": "*Your Appointments:*",
        "myappts_none": "You have no appointments on record.",
        "section_upcoming": "\n*Upcoming:*",
        "section_past": "\n*Past:*",
        "appt_line": "• With: {counterparty}\n   {when} — *{status}*\n   _ID: {id}_",

        # -- cancel appointment --
        "cancel_none": "You have no active appointments to cancel.",
        "cancel_list_header": "*Your Active Appointments:*\nChoose one to cancel:",
        "cancel_confirm_prompt": (
            "Cancel appointment with *{official}* on {when}?\n\n"
            "Tap ✅ Yes to confirm the cancellation, or ✖️ No to keep it."
        ),
        "cancel_aborted": "Cancellation aborted.",
        "cancel_past": "That appointment has already taken place and can't be cancelled.",

        # -- reschedule appointment --
        "resched_none": "You have no upcoming appointments to reschedule.",
        "resched_list_header": "*Reschedule an Appointment*\nChoose one to reschedule:",
        "resched_ask_time": "Enter the new date and time (YYYY-MM-DD HH:MM):",
        "resched_bad_format": "Please use the format YYYY-MM-DD HH:MM:",
        "resched_past": "That date/time is in the past. Please enter a future time (YYYY-MM-DD HH:MM):",
        "resched_overlap": "That time overlaps another of your appointments. Please choose a different time (YYYY-MM-DD HH:MM):",
        "resched_no_longer": "That appointment can no longer be rescheduled.",
        "resched_sent": "✅ Your reschedule request has been sent. You'll be notified when it's accepted or declined.",
        "cancel_done_by_official_to_user": (
            "❌ Your appointment (ID: `{id}`) with *{official}* "
            "has been cancelled by the official."
        ),
        "cancel_done_official_ack": (
            "✅ Appointment `{id}` cancelled. The requester has been notified."
        ),
        "cancel_done_requester_ack": "✅ Appointment `{id}` cancelled.",
        "cancel_done_requester_ack_notified": (
            "✅ Appointment `{id}` cancelled. The official has been notified."
        ),

        # -- appointment request flow --
        "appt_choose_official": "*Request an Appointment*\n\nWho would you like to meet with?",
        "appt_invalid_number": "Invalid selection.",
        "appt_limit_reached": (
            "You've reached the limit of {max} appointments with {official} "
            "in any {days}-day period.\n\n"
            "Please choose a date outside that window, or cancel an existing "
            "appointment with /cancelappointment."
        ),
        "appt_ask_date": "Desired date (YYYY-MM-DD):",
        "appt_bad_date": "Please use YYYY-MM-DD format:",
        "appt_ask_time": "Desired time (HH:MM, 24-hour):",
        "appt_bad_time": "Please use HH:MM format:",
        "appt_bad_datetime": "That date/time isn't valid. Please re-enter the date (YYYY-MM-DD):",
        "appt_past": "That date/time is in the past. Please enter a future date (YYYY-MM-DD):",
        "appt_too_far": (
            "Appointments can be booked at most {months} months ahead "
            "(through {until}). Please enter an earlier date (YYYY-MM-DD):"
        ),
        "appt_overlap": (
            "That time overlaps your existing appointment with {official} "
            "on {when} (ID: `{id}`).\n\nPlease choose a different date/time (YYYY-MM-DD):"
        ),
        "appt_ask_desc": "Brief description of the meeting purpose (128 characters max):",
        "appt_summary": (
            "*Appointment Request Summary:*\n"
            "With: {official}\n"
            "When: {when}\n"
            "Description: {desc}\n\n"
            "Submit? (yes/no)"
        ),
        "appt_request_cancelled": "Request cancelled.",
        "appt_overlap_not_submitted": (
            "That time overlaps your appointment with {official} "
            "(ID: `{id}`). Request not submitted."
        ),
        "appt_limit_not_submitted": (
            "You've reached the limit of {max} appointments with {official} "
            "in any {days}-day period. Request not submitted."
        ),
        "appt_cooldown": (
            "⏳ You've just made an appointment change. Please wait about "
            "{seconds} more second(s) before submitting another request."
        ),
        "appt_too_many_pending": (
            "📋 You already have {max} pending request(s) awaiting a response. "
            "Please wait for one to be confirmed or declined before submitting another."
        ),
        "appt_submitted": (
            "✅ *Request submitted!* (ID: `{id}`)\n"
            "I will notify you when your request is accepted, declined, "
            "or a new time is suggested."
        ),
        "appt_confirmed_user": (
            "✅ *Your appointment (ID: `{id}`) has been confirmed!*\n"
            "With: {official}\n"
            "When: {when}\n\n"
            "An ICS calendar file is attached."
        ),
        "appt_ics_caption": "Import this file into your calendar app.",

        # -- notifications --
        "notif_reminder_title": "🔔 *Reminder: {name}*",
        "notif_service_begins": "Service begins: {when}",
        "notif_join": "🔗 Join: {url}",
        "notif_announcements_header": "⚠️ *Announcements:*",

        # -- /settimezone --
        "tz_prompt": (
            "*Set Your Time Zone*\n\n"
            "Tap a zone below, or type any IANA zone name "
            "(e.g. `America/New_York`):"
        ),
        "tz_invalid": "That isn't a recognised time zone. Please try again (or /cancel):",
        "tz_set": "✅ Your time zone is set to *{tz}*.\nCurrent local time: {now}",

        # -- /language --
        "lang_prompt": "*Choose Your Language*",
        "lang_set": "✅ Language set to *{language}*.",

        # -- /notifications (opt-in personal reminders) --
        "notif_cat_convocations": "Sabbath and other Holy Convocations",
        "notif_cat_sunday_prayer": "Sunday Morning Prayer",
        "notif_cat_special": "Special events",
        "notif_prefs_prompt": (
            "*Personal Reminders*\n\n"
            "Choose which reminders you'd like sent to you directly. "
            "This is handy if you're not in a church group chat. "
            "Tap to turn each on/off, then tap Done."
        ),
        "notif_prefs_done": "✔️ Done",
        "notif_prefs_none": (
            "You've turned off all personal reminders. "
            "You can turn them back on any time with /notifications."
        ),
        "notif_prefs_saved": "✅ You'll receive personal reminders for:\n{list}",

        # -- /help topics --
        "help_topic_hint": "For details on a command, send `/help <command>` (e.g. `/help appointment`).",
        "help_unknown_topic": "I don't have help for that. Try one of: {topics}",
        "help_appointment": (
            "*/appointment* — Request a meeting with a church official.\n\n"
            "Pick the official, then enter a date and time. They (or their proxy) "
            "will confirm, decline, or suggest a different time. Once confirmed you "
            "get a calendar file."
        ),
        "help_myappointments": (
            "*/myappointments* — List your upcoming and past appointments and their status."
        ),
        "help_cancelappointment": (
            "*/cancelappointment* — Cancel an upcoming appointment. Pick it from the "
            "list and confirm. The other party is notified. Past appointments can't be cancelled."
        ),
        "help_reschedule": (
            "*/reschedule* — Propose a new time for an upcoming appointment. Pick it, "
            "enter the new date/time, and the other party accepts or declines. If declined, "
            "the original time is kept. New times can't be in the past."
        ),
        "help_events": (
            "*/events* — Show upcoming convocations, services and events for the next 30 days, "
            "with any join links."
        ),
        "help_exportcalendar": (
            "*/exportcalendar* — Download an ICS calendar file of upcoming events to import "
            "into your calendar app."
        ),
        "help_settimezone": (
            "*/settimezone* — Set your time zone so dates and times are shown in your local time. "
            "Tap a common zone or type any IANA name (e.g. `America/New_York`)."
        ),
        "help_language": "*/language* — Choose the language the bot uses when talking to you.",
        "help_notifications": (
            "*/notifications* — Choose which reminders you receive as personal messages "
            "(Sabbath and Holy Convocations, Sunday Morning Prayer, Special events). Useful "
            "if you're not in a church group chat."
        ),

        # -- appointment statuses --
        "status_pending": "pending",
        "status_confirmed": "confirmed",
        "status_counter_proposed": "time proposed",
        "status_cancelled": "cancelled",
        "status_declined": "declined",
    },

    "es": {
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

        # -- appointment statuses --
        "status_pending": "pendiente",
        "status_confirmed": "confirmada",
        "status_counter_proposed": "hora propuesta",
        "status_cancelled": "cancelada",
        "status_declined": "rechazada",
    },

    "fr": {
        # -- welcome / help --
        "welcome": (
            "👋 Bienvenue sur *{bot_name}* !\n\n"
            "J'envoie des rappels pour les Saintes Convocations de Dieu, "
            "les services spéciaux et les événements.\n\n{commands}"
        ),
        "user_commands": (
            "*Commandes disponibles :*\n"
            "/help — afficher ce message\n"
            "/events — événements à venir (30 prochains jours)\n"
            "/exportcalendar — télécharger un fichier de calendrier ICS\n"
            "/appointment — demander un rendez-vous avec un responsable de l'église\n"
            "/myappointments — voir vos rendez-vous\n"
            "/cancelappointment — annuler un rendez-vous en attente ou confirmé\n"
            "/reschedule — proposer une nouvelle heure pour un rendez-vous à venir\n"
            "/settimezone — définir votre fuseau horaire pour les heures affichées\n"
            "/language — choisir votre langue\n"
            "/notifications — choisir les rappels que vous recevez\n"
            "/stop — vous désabonner des notifications"
        ),
        "share_contact_prompt": (
            "Pour personnaliser votre expérience, veuillez partager votre contact "
            "(appuyez sur le bouton ci-dessous). Vous pouvez appuyer sur Ignorer "
            "si vous préférez ne pas le faire."
        ),
        "share_contact_button": "📱 Partager mon contact",
        "unsubscribed": "Vous avez été désabonné. Envoyez /start pour vous réabonner.",
        "bcast_individual_subscribers": "Abonnés individuels",

        # -- events --
        "events_header": "*Événements à venir (30 prochains jours) :*\n",
        "events_none": "Aucun événement dans les 30 prochains jours.",

        # -- my appointments --
        "myappts_header": "*Vos rendez-vous :*",
        "myappts_none": "Vous n'avez aucun rendez-vous enregistré.",
        "section_upcoming": "\n*À venir :*",
        "section_past": "\n*Passés :*",
        "appt_line": "• Avec : {counterparty}\n   {when} — *{status}*\n   _ID : {id}_",

        # -- cancel appointment --
        "cancel_none": "Vous n'avez aucun rendez-vous actif à annuler.",
        "cancel_list_header": "*Vos rendez-vous actifs :*\nChoisissez-en un à annuler :",
        "cancel_confirm_prompt": (
            "Annuler le rendez-vous avec *{official}* le {when} ?\n\n"
            "Appuyez sur ✅ Oui pour confirmer l'annulation, ou ✖️ Non pour le conserver."
        ),
        "cancel_aborted": "Annulation abandonnée.",
        "cancel_past": "Ce rendez-vous a déjà eu lieu et ne peut pas être annulé.",

        # -- reschedule appointment --
        "resched_none": "Vous n'avez aucun rendez-vous à venir à reprogrammer.",
        "resched_list_header": "*Reprogrammer un rendez-vous*\nChoisissez-en un à reprogrammer :",
        "resched_ask_time": "Saisissez la nouvelle date et heure (YYYY-MM-DD HH:MM) :",
        "resched_bad_format": "Veuillez utiliser le format YYYY-MM-DD HH:MM :",
        "resched_past": "Cette date/heure est déjà passée. Veuillez saisir une heure future (YYYY-MM-DD HH:MM) :",
        "resched_overlap": "Cette heure chevauche un autre de vos rendez-vous. Veuillez choisir une autre heure (YYYY-MM-DD HH:MM) :",
        "resched_no_longer": "Ce rendez-vous ne peut plus être reprogrammé.",
        "resched_sent": "✅ Votre demande de reprogrammation a été envoyée. Vous serez informé de son acceptation ou de son refus.",
        "cancel_done_by_official_to_user": (
            "❌ Votre rendez-vous (ID : `{id}`) avec *{official}* "
            "a été annulé par le responsable."
        ),
        "cancel_done_official_ack": (
            "✅ Rendez-vous `{id}` annulé. Le demandeur a été informé."
        ),
        "cancel_done_requester_ack": "✅ Rendez-vous `{id}` annulé.",
        "cancel_done_requester_ack_notified": (
            "✅ Rendez-vous `{id}` annulé. Le responsable a été informé."
        ),

        # -- appointment request flow --
        "appt_choose_official": "*Demander un rendez-vous*\n\nAvec qui souhaitez-vous vous rencontrer ?",
        "appt_invalid_number": "Sélection non valide.",
        "appt_limit_reached": (
            "Vous avez atteint la limite de {max} rendez-vous avec {official} "
            "sur toute période de {days} jours.\n\n"
            "Veuillez choisir une date en dehors de cette période, ou annuler "
            "un rendez-vous existant avec /cancelappointment."
        ),
        "appt_ask_date": "Date souhaitée (YYYY-MM-DD) :",
        "appt_bad_date": "Veuillez utiliser le format YYYY-MM-DD :",
        "appt_ask_time": "Heure souhaitée (HH:MM, format 24 h) :",
        "appt_bad_time": "Veuillez utiliser le format HH:MM :",
        "appt_bad_datetime": "Cette date/heure n'est pas valide. Veuillez ressaisir la date (YYYY-MM-DD) :",
        "appt_past": "Cette date/heure est déjà passée. Veuillez saisir une date future (YYYY-MM-DD) :",
        "appt_too_far": (
            "Les rendez-vous peuvent être pris au maximum {months} mois à l'avance "
            "(jusqu'au {until}). Veuillez saisir une date plus proche (YYYY-MM-DD) :"
        ),
        "appt_overlap": (
            "Cette heure chevauche votre rendez-vous existant avec {official} "
            "le {when} (ID : `{id}`).\n\nVeuillez choisir une autre date/heure (YYYY-MM-DD) :"
        ),
        "appt_ask_desc": "Brève description de l'objet de la réunion (128 caractères maximum) :",
        "appt_summary": (
            "*Récapitulatif de la demande de rendez-vous :*\n"
            "Avec : {official}\n"
            "Quand : {when}\n"
            "Description : {desc}\n\n"
            "Envoyer ? (oui/non)"
        ),
        "appt_request_cancelled": "Demande annulée.",
        "appt_overlap_not_submitted": (
            "Cette heure chevauche votre rendez-vous avec {official} "
            "(ID : `{id}`). Demande non envoyée."
        ),
        "appt_limit_not_submitted": (
            "Vous avez atteint la limite de {max} rendez-vous avec {official} "
            "sur toute période de {days} jours. Demande non envoyée."
        ),
        "appt_cooldown": (
            "⏳ Vous venez de modifier un rendez-vous. Veuillez patienter environ "
            "{seconds} seconde(s) de plus avant d'envoyer une autre demande."
        ),
        "appt_too_many_pending": (
            "📋 Vous avez déjà {max} demande(s) en attente de réponse. "
            "Attendez qu'une soit confirmée ou refusée avant d'en envoyer une autre."
        ),
        "appt_submitted": (
            "✅ *Demande envoyée !* (ID : `{id}`)\n"
            "Je vous informerai lorsque votre demande sera acceptée, refusée "
            "ou qu'une nouvelle heure sera proposée."
        ),
        "appt_confirmed_user": (
            "✅ *Votre rendez-vous (ID : `{id}`) a été confirmé !*\n"
            "Avec : {official}\n"
            "Quand : {when}\n\n"
            "Un fichier de calendrier ICS est joint."
        ),
        "appt_ics_caption": "Importez ce fichier dans votre application de calendrier.",

        # -- notifications --
        "notif_reminder_title": "🔔 *Rappel : {name}*",
        "notif_service_begins": "Le service commence : {when}",
        "notif_join": "🔗 Rejoindre : {url}",
        "notif_announcements_header": "⚠️ *Annonces :*",

        # -- /settimezone --
        "tz_prompt": (
            "*Définir votre fuseau horaire*\n\n"
            "Appuyez sur un fuseau ci-dessous ou saisissez un nom de fuseau IANA "
            "(p. ex. `America/New_York`) :"
        ),
        "tz_invalid": "Ce fuseau horaire n'est pas reconnu. Veuillez réessayer (ou /cancel) :",
        "tz_set": "✅ Votre fuseau horaire est défini sur *{tz}*.\nHeure locale actuelle : {now}",

        # -- /language --
        "lang_prompt": "*Choisissez votre langue*",
        "lang_set": "✅ Langue définie sur *{language}*.",

        # -- /notifications (rappels personnels optionnels) --
        "notif_cat_convocations": "Sabbat et autres Saintes Convocations",
        "notif_cat_sunday_prayer": "Prière du dimanche matin",
        "notif_cat_special": "Événements spéciaux",
        "notif_prefs_prompt": (
            "*Rappels personnels*\n\n"
            "Choisissez les rappels que vous souhaitez recevoir directement. "
            "Pratique si vous n'êtes pas dans un groupe de discussion de l'église. "
            "Appuyez pour activer/désactiver chacun, puis appuyez sur Terminé."
        ),
        "notif_prefs_done": "✔️ Terminé",
        "notif_prefs_none": (
            "Vous avez désactivé tous les rappels personnels. "
            "Vous pouvez les réactiver à tout moment avec /notifications."
        ),
        "notif_prefs_saved": "✅ Vous recevrez des rappels personnels pour :\n{list}",

        # -- rubriques de /help --
        "help_topic_hint": "Pour les détails d'une commande, envoyez `/help <commande>` (p. ex. `/help appointment`).",
        "help_unknown_topic": "Je n'ai pas d'aide pour cela. Essayez : {topics}",
        "help_appointment": (
            "*/appointment* — Demandez un rendez-vous avec un responsable de l'église.\n\n"
            "Choisissez le responsable, puis saisissez une date et une heure. Lui (ou son "
            "délégué) confirmera, refusera ou proposera un autre horaire. Une fois confirmé, "
            "vous recevez un fichier de calendrier."
        ),
        "help_myappointments": (
            "*/myappointments* — Affiche vos rendez-vous à venir et passés ainsi que leur statut."
        ),
        "help_cancelappointment": (
            "*/cancelappointment* — Annulez un rendez-vous à venir. Choisissez-le dans la liste "
            "et confirmez. L'autre partie est prévenue. Les rendez-vous passés ne peuvent pas être annulés."
        ),
        "help_reschedule": (
            "*/reschedule* — Proposez un nouvel horaire pour un rendez-vous à venir. Choisissez-le, "
            "saisissez la nouvelle date/heure, et l'autre partie accepte ou refuse. En cas de refus, "
            "l'horaire d'origine est conservé. Les nouveaux horaires ne peuvent pas être dans le passé."
        ),
        "help_events": (
            "*/events* — Affiche les convocations, services et événements des 30 prochains jours, "
            "avec leurs liens de connexion."
        ),
        "help_exportcalendar": (
            "*/exportcalendar* — Téléchargez un fichier de calendrier ICS des prochains événements "
            "à importer dans votre application de calendrier."
        ),
        "help_settimezone": (
            "*/settimezone* — Définissez votre fuseau horaire pour afficher dates et heures en heure "
            "locale. Appuyez sur un fuseau courant ou saisissez un nom IANA (p. ex. `America/New_York`)."
        ),
        "help_language": "*/language* — Choisissez la langue que le bot utilise avec vous.",
        "help_notifications": (
            "*/notifications* — Choisissez les rappels que vous recevez en messages personnels "
            "(Sabbat et Saintes Convocations, Prière du dimanche matin, Événements spéciaux). "
            "Utile si vous n'êtes pas dans un groupe de discussion de l'église."
        ),

        # -- appointment statuses --
        "status_pending": "en attente",
        "status_confirmed": "confirmé",
        "status_counter_proposed": "horaire proposé",
        "status_cancelled": "annulé",
        "status_declined": "refusé",
    },

    # NOTE: isiZulu translations are a best effort and should be reviewed by a
    # native speaker before relying on them in production.
    "zu": {
        # -- welcome / help --
        "welcome": (
            "👋 Siyakwamukela ku-*{bot_name}*!\n\n"
            "Ngithumela izikhumbuzi zeMihlangano eNgcwele kaNkulunkulu, "
            "izinkonzo ezikhethekile, nemicimbi.\n\n{commands}"
        ),
        "user_commands": (
            "*Imiyalo etholakalayo:*\n"
            "/help — bonisa lo mlayezo\n"
            "/events — imicimbi ezayo (izinsuku ezingu-30 ezizayo)\n"
            "/exportcalendar — landa ifayela lekhalenda le-ICS\n"
            "/appointment — cela umhlangano nesikhulu sebandla\n"
            "/myappointments — bona ama-aphoyintimenti akho\n"
            "/cancelappointment — khansela i-aphoyintimenti elindile noma eqinisekisiwe\n"
            "/reschedule — phakamisa isikhathi esisha se-aphoyintimenti ezayo\n"
            "/settimezone — setha izoni yesikhathi sakho yezikhathi eziboniswayo\n"
            "/language — khetha ulimi lwakho\n"
            "/notifications — khetha ukuthi yiziphi izikhumbuzo ozitholayo\n"
            "/stop — yekisa ukubhalisa ezaziswayweni"
        ),
        "share_contact_prompt": (
            "Ukuze wenze umuzwa wakho ube ngowakho, sicela wabelane ngoxhumana naye "
            "(thepha inkinobho engezansi). Ungathepha okuthi Yeqa uma ungathandi."
        ),
        "share_contact_button": "📱 Yabelana ngoxhumana nami",
        "unsubscribed": "Ususiwe ekubhaliseni. Thumela /start ukuze ubhalise futhi.",
        "bcast_individual_subscribers": "Ababhalisi ngabanye",

        # -- events --
        "events_header": "*Imicimbi ezayo (izinsuku ezingu-30 ezizayo):*\n",
        "events_none": "Ayikho imicimbi ezinsukwini ezingu-30 ezizayo.",

        # -- my appointments --
        "myappts_header": "*Ama-aphoyintimenti akho:*",
        "myappts_none": "Awunawo ama-aphoyintimenti abhalisiwe.",
        "section_upcoming": "\n*Ezizayo:*",
        "section_past": "\n*Ezedlule:*",
        "appt_line": "• No: {counterparty}\n   {when} — *{status}*\n   _I-ID: {id}_",

        # -- cancel appointment --
        "cancel_none": "Awunawo ama-aphoyintimenti asebenzayo ongawakhansela.",
        "cancel_list_header": "*Ama-aphoyintimenti akho asebenzayo:*\nKhetha elilodwa ukulikhansela:",
        "cancel_confirm_prompt": (
            "Khansela i-aphoyintimenti no-*{official}* ngo-{when}?\n\n"
            "Thepha u-✅ Yebo ukuze uqinisekise ukukhansela, noma u-✖️ Cha ukuze uligcine."
        ),
        "cancel_aborted": "Ukukhansela kuyekisiwe.",
        "cancel_past": "Lelo aphoyintimenti selenzekile futhi ngeke likhanselwe.",

        # -- reschedule appointment --
        "resched_none": "Awunawo ama-aphoyintimenti azayo ongawahlela kabusha.",
        "resched_list_header": "*Hlela kabusha i-aphoyintimenti*\nKhetha elilodwa ukulihlela kabusha:",
        "resched_ask_time": "Faka usuku nesikhathi esisha (YYYY-MM-DD HH:MM):",
        "resched_bad_format": "Sicela usebenzise ifomethi ethi YYYY-MM-DD HH:MM:",
        "resched_past": "Lelo suku/sikhathi seludlulile. Sicela ufake isikhathi esizayo (YYYY-MM-DD HH:MM):",
        "resched_overlap": "Leso sikhathi sigxubha nelinye lama-aphoyintimenti akho. Sicela ukhethe esinye isikhathi (YYYY-MM-DD HH:MM):",
        "resched_no_longer": "Lelo aphoyintimenti ngeke lisahlelwa kabusha.",
        "resched_sent": "✅ Isicelo sakho sokuhlela kabusha sithunyeliwe. Uzokwaziswa lapho samukelwa noma senqatshelwa.",
        "cancel_done_by_official_to_user": (
            "❌ I-aphoyintimenti yakho (I-ID: `{id}`) no-*{official}* "
            "ikhanselwe yisikhulu."
        ),
        "cancel_done_official_ack": (
            "✅ I-aphoyintimenti `{id}` ikhanseliwe. Ocelayo waziswile."
        ),
        "cancel_done_requester_ack": "✅ I-aphoyintimenti `{id}` ikhanseliwe.",
        "cancel_done_requester_ack_notified": (
            "✅ I-aphoyintimenti `{id}` ikhanseliwe. Isikhulu saziswile."
        ),

        # -- appointment request flow --
        "appt_choose_official": "*Cela i-aphoyintimenti*\n\nUngathanda ukuhlangana nobani?",
        "appt_invalid_number": "Ukukhetha okungalungile.",
        "appt_limit_reached": (
            "Usufinyelele umkhawulo wama-aphoyintimenti angu-{max} no-{official} "
            "kunoma yisiphi isikhathi sezinsuku ezingu-{days}.\n\n"
            "Sicela ukhethe usuku olungaphandle kwaleso sikhathi, noma ukhansele "
            "i-aphoyintimenti ekhona nge-/cancelappointment."
        ),
        "appt_ask_date": "Usuku olufunayo (YYYY-MM-DD):",
        "appt_bad_date": "Sicela usebenzise ifomethi ethi YYYY-MM-DD:",
        "appt_ask_time": "Isikhathi osifunayo (HH:MM, ihora elingu-24):",
        "appt_bad_time": "Sicela usebenzise ifomethi ethi HH:MM:",
        "appt_bad_datetime": "Lolo suku/sikhathi alulungile. Sicela uphinde ufake usuku (YYYY-MM-DD):",
        "appt_past": "Lolo suku/sikhathi seludlulile. Sicela ufake usuku oluzayo (YYYY-MM-DD):",
        "appt_too_far": (
            "Ama-aphoyintimenti angabhukwa kungakapheli izinyanga ezingu-{months} "
            "(kuze kube ngu-{until}). Sicela ufake usuku oluseduze (YYYY-MM-DD):"
        ),
        "appt_overlap": (
            "Leso sikhathi sigxubha ne-aphoyintimenti yakho ekhona no-{official} "
            "ngo-{when} (I-ID: `{id}`).\n\nSicela ukhethe olunye usuku/isikhathi (YYYY-MM-DD):"
        ),
        "appt_ask_desc": "Incazelo emfushane yenjongo yomhlangano (izinhlamvu ezingu-128 ubuningi):",
        "appt_summary": (
            "*Isifinyezo sesicelo se-aphoyintimenti:*\n"
            "No: {official}\n"
            "Nini: {when}\n"
            "Incazelo: {desc}\n\n"
            "Thumela? (yebo/cha)"
        ),
        "appt_request_cancelled": "Isicelo sikhanseliwe.",
        "appt_overlap_not_submitted": (
            "Leso sikhathi sigxubha ne-aphoyintimenti yakho no-{official} "
            "(I-ID: `{id}`). Isicelo asithunyelwanga."
        ),
        "appt_limit_not_submitted": (
            "Usufinyelele umkhawulo wama-aphoyintimenti angu-{max} no-{official} "
            "kunoma yisiphi isikhathi sezinsuku ezingu-{days}. Isicelo asithunyelwanga."
        ),
        "appt_cooldown": (
            "⏳ Usanda kwenza ushintsho lwe-aphoyintimenti. Sicela ulinde cishe "
            "amasekhondi angu-{seconds} ngaphambi kokuthumela esinye isicelo."
        ),
        "appt_too_many_pending": (
            "📋 Usuvele une-{max} izicelo ezilindile impendulo. "
            "Sicela ulinde kuze kuqinisekiswe noma kwenqatshelwe esinye ngaphambi kokuthumela."
        ),
        "appt_submitted": (
            "✅ *Isicelo sithunyelwe!* (I-ID: `{id}`)\n"
            "Ngizokwazisa lapho isicelo sakho samukelwa, salahlwa, "
            "noma kuphakanyiswa isikhathi esisha."
        ),
        "appt_confirmed_user": (
            "✅ *I-aphoyintimenti yakho (I-ID: `{id}`) iqinisekisiwe!*\n"
            "No: {official}\n"
            "Nini: {when}\n\n"
            "Kunamathiselwe ifayela lekhalenda le-ICS."
        ),
        "appt_ics_caption": "Ngenisa leli fayela kuhlelo lwakho lwekhalenda.",

        # -- notifications --
        "notif_reminder_title": "🔔 *Isikhumbuzo: {name}*",
        "notif_service_begins": "Inkonzo iqala: {when}",
        "notif_join": "🔗 Joyina: {url}",
        "notif_announcements_header": "⚠️ *Izaziso:*",

        # -- /settimezone --
        "tz_prompt": (
            "*Setha izoni yakho yesikhathi*\n\n"
            "Thepha izoni engezansi, noma uthayiphe noma yiliphi igama lezoni ye-IANA "
            "(isb. `America/New_York`):"
        ),
        "tz_invalid": "Leyo akuyona izoni yesikhathi eyaziwayo. Sicela uzame futhi (noma /cancel):",
        "tz_set": "✅ Izoni yakho yesikhathi isethelwe ku-*{tz}*.\nIsikhathi sendawo samanje: {now}",

        # -- /language --
        "lang_prompt": "*Khetha ulimi lwakho*",
        "lang_set": "✅ Ulimi lusethelwe ku-*{language}*.",

        # -- /notifications (izikhumbuzo zomuntu siqu ozikhethayo) --
        "notif_cat_convocations": "ISabatha neminye iMihlangano eNgcwele",
        "notif_cat_sunday_prayer": "Umthandazo waNgeSonto Ekuseni",
        "notif_cat_special": "Imicimbi ekhethekile",
        "notif_prefs_prompt": (
            "*Izikhumbuzo zomuntu siqu*\n\n"
            "Khetha izikhumbuzo ofuna ukuzithola ngqo. "
            "Kuwusizo uma ungekho eqenjini lengxoxo lebandla. "
            "Thepha ukuvula/ukuvala ngayinye, bese uthepha okuthi Kwenziwe."
        ),
        "notif_prefs_done": "✔️ Kwenziwe",
        "notif_prefs_none": (
            "Uvale zonke izikhumbuzo zomuntu siqu. "
            "Ungaphinda uzivule noma nini nge-/notifications."
        ),
        "notif_prefs_saved": "✅ Uzothola izikhumbuzo zomuntu siqu ze-:\n{list}",

        # -- izihloko ze-/help --
        "help_topic_hint": "Ukuthola imininingwane ngomyalo, thumela `/help <umyalo>` (isb. `/help appointment`).",
        "help_unknown_topic": "Anginalo usizo ngalokho. Zama okukodwa kwalokhu: {topics}",
        "help_appointment": (
            "*/appointment* — Cela umhlangano nesikhulu sebandla.\n\n"
            "Khetha isikhulu, bese ufaka usuku nesikhathi. Sona (noma ummeleli waso) "
            "sizoqinisekisa, senqabe, noma siphakamise esinye isikhathi. Uma sekuqinisekisiwe "
            "uthola ifayela lekhalenda."
        ),
        "help_myappointments": (
            "*/myappointments* — Bonisa ama-aphoyintimenti akho azayo nadlule kanye nesimo sawo."
        ),
        "help_cancelappointment": (
            "*/cancelappointment* — Khansela i-aphoyintimenti ezayo. Yikhethe ohlwini bese uqinisekisa. "
            "Elinye iqembu liyaziswa. Ama-aphoyintimenti adlule ngeke akhanselwe."
        ),
        "help_reschedule": (
            "*/reschedule* — Phakamisa isikhathi esisha se-aphoyintimenti ezayo. Yikhethe, faka "
            "usuku/isikhathi esisha, bese elinye iqembu liyamukela noma linqabe. Uma kunqatshelwe, "
            "kugcinwa isikhathi sokuqala. Izikhathi ezintsha ngeke zibe sesikhathini esidlule."
        ),
        "help_events": (
            "*/events* — Bonisa imihlangano, izinkonzo nemicimbi yezinsuku ezingu-30 ezizayo, "
            "kanye nezixhumanisi zokujoyina."
        ),
        "help_exportcalendar": (
            "*/exportcalendar* — Landa ifayela lekhalenda le-ICS lemicimbi ezayo ukuze ulingenise "
            "kuhlelo lwakho lwekhalenda."
        ),
        "help_settimezone": (
            "*/settimezone* — Setha izoni yesikhathi sakho ukuze izinsuku nezikhathi zibonakale "
            "ngesikhathi sendawo yakho. Thepha izoni evamile noma uthayiphe igama le-IANA "
            "(isb. `America/New_York`)."
        ),
        "help_language": "*/language* — Khetha ulimi i-bhothi elilusebenzisayo uma likhuluma nawe.",
        "help_notifications": (
            "*/notifications* — Khetha izikhumbuzo ozithola njengemilayezo yomuntu siqu "
            "(iSabatha neMihlangano eNgcwele, uMthandazo waNgeSonto Ekuseni, Imicimbi ekhethekile). "
            "Kuwusizo uma ungekho eqenjini lengxoxo lebandla."
        ),

        # -- appointment statuses --
        "status_pending": "kulindile",
        "status_confirmed": "kuqinisekisiwe",
        "status_counter_proposed": "isikhathi siphakanyisiwe",
        "status_cancelled": "kukhanseliwe",
        "status_declined": "kwaliwe",
    },
}


def t(key: str, lang: str | None = None, **kwargs) -> str:
    """Translate ``key`` into ``lang`` (falling back to English), then format."""
    if lang not in CATALOG:
        lang = DEFAULT_LANG
    text = CATALOG.get(lang, {}).get(key)
    if text is None:
        text = CATALOG[DEFAULT_LANG].get(key, key)
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError):
            return text
    return text
