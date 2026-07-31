"""French (fr) strings."""

STRINGS: dict[str, str] = {
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
        "/donate — soutenir la congrégation par un don\n"
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
    "help_donate": (
        "*/donate* — Soutenez la congrégation par un don. Ouvre une page "
        "de don sécurisée où vous pouvez contribuer."
    ),

    # -- /donate --
    "donate_message": (
        "🙏 *Soutenez Christ of God Ministries*\n\n"
        "Votre générosité aide à soutenir notre ministère et notre mission. "
        "Si vous souhaitez contribuer, appuyez sur le bouton ci-dessous — "
        "merci, et que l'Éternel vous bénisse !"
    ),
    "donate_button": "💝 Faire un don",
    "donate_not_configured": (
        "Les dons en ligne ne sont pas encore configurés. Parlez à "
        "l'Ancien L. Williams des façons de contribuer."
    ),

    # -- rappels de rendez-vous --
    "appt_reminder_user": (
        "⏰ *Rappel de rendez-vous*\n\n"
        "Votre rendez-vous avec *{counterparty}* est {when}.\n_ID : {id}_"
    ),
    "appt_reminder_official": (
        "⏰ *Rappel de rendez-vous*\n\n"
        "Votre rendez-vous avec *{counterparty}* est {when}.\n_ID : {id}_"
    ),

    # -- appointment statuses --
    "status_pending": "en attente",
    "status_confirmed": "confirmé",
    "status_counter_proposed": "horaire proposé",
    "status_cancelled": "annulé",
    "status_declined": "refusé",
}
