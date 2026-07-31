"""isiZulu (zu) strings.

NOTE: these translations are a best effort (machine-generated) and should be
reviewed by a native speaker before relying on them in production."""

STRINGS: dict[str, str] = {
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
        "/donate — sekela ibandla ngomnikelo\n"
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
    "help_donate": (
        "*/donate* — Sekela ibandla ngomnikelo. Kuvula ikhasi lokupha "
        "eliphephile lapho ungakhona ukunikela."
    ),

    # -- /donate --
    "donate_message": (
        "🙏 *Sekela i-Christ of God Ministries*\n\n"
        "Ukupha kwakho kusiza ukusekela inkonzo yethu nomsebenzi wokufinyelela. "
        "Uma ufisa ukunikela, thepha inkinobho engezansi — "
        "siyabonga, futhi iNKOSI ikubusise!"
    ),
    "donate_button": "💝 Nikela manje",
    "donate_not_configured": (
        "Ukupha nge-inthanethi akukalungiselelwa okwamanje. Sicela ukhulume "
        "noMdala u-L. Williams ngezindlela zokunikela."
    ),

    # -- izikhumbuzo zama-aphoyintimenti --
    "appt_reminder_user": (
        "⏰ *Isikhumbuzo se-aphoyintimenti*\n\n"
        "I-aphoyintimenti yakho no-*{counterparty}* ngo-{when}.\n_I-ID: {id}_"
    ),
    "appt_reminder_official": (
        "⏰ *Isikhumbuzo se-aphoyintimenti*\n\n"
        "I-aphoyintimenti yakho no-*{counterparty}* ngo-{when}.\n_I-ID: {id}_"
    ),

    # -- appointment statuses --
    "status_pending": "kulindile",
    "status_confirmed": "kuqinisekisiwe",
    "status_counter_proposed": "isikhathi siphakanyisiwe",
    "status_cancelled": "kukhanseliwe",
    "status_declined": "kwaliwe",
}
