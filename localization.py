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
            "/settimezone — set your time zone for displayed times\n"
            "/language — choose your language\n"
            "/stop — unsubscribe from notifications"
        ),
        "share_contact_prompt": (
            "To personalise your experience, please share your contact "
            "(tap the button below). You can tap Skip if you prefer not to."
        ),
        "share_contact_button": "📱 Share my contact",
        "unsubscribed": "You have been unsubscribed. Send /start to re-subscribe.",

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
        "appt_already_with_official": (
            "You already have an appointment with {official} "
            "(ID: `{id}`, {status}).\n\n"
            "Please cancel it with /cancelappointment before requesting another, "
            "or use /myappointments to review it."
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
        "appt_already_not_submitted": (
            "You already have an appointment with {official} "
            "(ID: `{id}`). Request not submitted."
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
            "/settimezone — establecer tu zona horaria para las horas mostradas\n"
            "/language — elegir tu idioma\n"
            "/stop — cancelar la suscripción a las notificaciones"
        ),
        "share_contact_prompt": (
            "Para personalizar tu experiencia, comparte tu contacto "
            "(toca el botón de abajo). Puedes tocar Omitir si prefieres no hacerlo."
        ),
        "share_contact_button": "📱 Compartir mi contacto",
        "unsubscribed": "Has cancelado la suscripción. Envía /start para volver a suscribirte.",

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
        "appt_already_with_official": (
            "Ya tienes una cita con {official} "
            "(ID: `{id}`, {status}).\n\n"
            "Cancélala con /cancelappointment antes de solicitar otra, "
            "o usa /myappointments para revisarla."
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
        "appt_already_not_submitted": (
            "Ya tienes una cita con {official} "
            "(ID: `{id}`). Solicitud no enviada."
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
            "/settimezone — définir votre fuseau horaire pour les heures affichées\n"
            "/language — choisir votre langue\n"
            "/stop — vous désabonner des notifications"
        ),
        "share_contact_prompt": (
            "Pour personnaliser votre expérience, veuillez partager votre contact "
            "(appuyez sur le bouton ci-dessous). Vous pouvez appuyer sur Ignorer "
            "si vous préférez ne pas le faire."
        ),
        "share_contact_button": "📱 Partager mon contact",
        "unsubscribed": "Vous avez été désabonné. Envoyez /start pour vous réabonner.",

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
        "appt_already_with_official": (
            "Vous avez déjà un rendez-vous avec {official} "
            "(ID : `{id}`, {status}).\n\n"
            "Annulez-le avec /cancelappointment avant d'en demander un autre, "
            "ou utilisez /myappointments pour le consulter."
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
        "appt_already_not_submitted": (
            "Vous avez déjà un rendez-vous avec {official} "
            "(ID : `{id}`). Demande non envoyée."
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
            "/settimezone — setha izoni yesikhathi sakho yezikhathi eziboniswayo\n"
            "/language — khetha ulimi lwakho\n"
            "/stop — yekisa ukubhalisa ezaziswayweni"
        ),
        "share_contact_prompt": (
            "Ukuze wenze umuzwa wakho ube ngowakho, sicela wabelane ngoxhumana naye "
            "(thepha inkinobho engezansi). Ungathepha okuthi Yeqa uma ungathandi."
        ),
        "share_contact_button": "📱 Yabelana ngoxhumana nami",
        "unsubscribed": "Ususiwe ekubhaliseni. Thumela /start ukuze ubhalise futhi.",

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
        "appt_already_with_official": (
            "Usunayo i-aphoyintimenti no-{official} "
            "(I-ID: `{id}`, {status}).\n\n"
            "Sicela uyikhansele nge-/cancelappointment ngaphambi kokucela enye, "
            "noma usebenzise i-/myappointments ukuze uyibuyekeze."
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
        "appt_already_not_submitted": (
            "Usunayo i-aphoyintimenti no-{official} "
            "(I-ID: `{id}`). Isicelo asithunyelwanga."
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
