"""
Multilingual reply templates — used when no LLM is configured, and as the fallback when one
is unreachable. Jev picks the intent; this file turns (intent, language) into a sentence.

Placeholders: {name_part} (", Aishath" or ""), {location}, {airport}, {wifi}, {time}, {card}.
Missing languages for an intent fall back to English.

Dhivehi (dv) lines are deliberately short and cover the social intents only — please have a
native speaker review them before going live.
"""

# BCP-47 tags for the browser's speech recognition / synthesis
LOCALES = {
    "en": "en-GB", "dv": "dv-MV", "ar": "ar-SA", "zh": "zh-CN", "ru": "ru-RU", "hi": "hi-IN",
    "de": "de-DE", "it": "it-IT", "fr": "fr-FR", "es": "es-ES", "ja": "ja-JP", "ko": "ko-KR",
    "pt": "pt-PT", "tr": "tr-TR", "ur": "ur-PK", "ta": "ta-IN", "si": "si-LK", "bn": "bn-BD",
    "id": "id-ID", "ms": "ms-MY", "th": "th-TH", "nl": "nl-NL",
}

LANGUAGE_NAMES = {
    "en": "English", "dv": "Dhivehi", "ar": "Arabic", "zh": "Chinese", "ru": "Russian",
    "hi": "Hindi", "de": "German", "it": "Italian", "fr": "French", "es": "Spanish",
    "ja": "Japanese", "ko": "Korean", "pt": "Portuguese", "tr": "Turkish", "ur": "Urdu",
    "ta": "Tamil", "si": "Sinhala", "bn": "Bengali", "id": "Indonesian", "ms": "Malay",
    "th": "Thai", "nl": "Dutch",
}

# What Jev chooses between. Keys are stored in interactions.intent.
INTENTS = {
    "greeting": "saying hello or starting a conversation",
    "how_are_you": "asking how the kiosk/assistant is, or light small talk",
    "share_good": "sharing good news, excitement or a good mood",
    "share_bad": "sharing tiredness, stress, sadness, illness or a bad day",
    "thanks": "thanking the assistant",
    "goodbye": "saying goodbye or leaving",
    "directions": "asking where a place or facility is (gate, toilet, prayer room, cafe, exit, taxi, lounge)",
    "wifi": "asking about Wi-Fi, internet or phone charging",
    "flight": "asking about a flight, delay, check-in, boarding or baggage",
    "lost": "lost item, lost person, or needing urgent help",
    "complaint": "complaining or reporting a problem",
    "attendance": "asking about their own shift, working hours, breaks or attendance record",
    "compliment": "asking how they look, or wanting encouragement",
    "joke": "asking for a joke or something fun",
    "about": "asking who or what the assistant is",
    "other": "anything else",
}

T = {
    "greeting": {
        "en": "Hello{name_part}! Welcome to {location}. How can I help you today?",
        "dv": "އައްސަލާމު ޢަލައިކުމް{name_part}! ކިހިނެއް؟",
        "ar": "مرحباً{name_part}! أهلاً بك في {location}. كيف يمكنني مساعدتك اليوم؟",
        "zh": "你好{name_part}！欢迎来到{location}。今天我能为您做些什么？",
        "ru": "Здравствуйте{name_part}! Добро пожаловать в {location}. Чем могу помочь?",
        "hi": "नमस्ते{name_part}! {location} में आपका स्वागत है। मैं आपकी क्या मदद कर सकता हूँ?",
        "de": "Hallo{name_part}! Willkommen in {location}. Wie kann ich helfen?",
        "it": "Ciao{name_part}! Benvenuto a {location}. Come posso aiutarti oggi?",
        "fr": "Bonjour{name_part} ! Bienvenue à {location}. Comment puis-je vous aider ?",
        "es": "¡Hola{name_part}! Bienvenido a {location}. ¿En qué puedo ayudarte hoy?",
        "ja": "こんにちは{name_part}！{location}へようこそ。何かお手伝いできますか？",
    },
    "how_are_you": {
        "en": "I'm doing great, thanks for asking{name_part}! Watching over {location} keeps me busy. How about you?",
        "dv": "ރަނގަޅު، ޝުކުރިއްޔާ{name_part}! ތިބާ ކިހިނެއް؟",
        "ar": "أنا بخير، شكراً لسؤالك{name_part}! وأنت، كيف حالك؟",
        "zh": "我很好，谢谢关心{name_part}！你呢？",
        "ru": "У меня всё отлично, спасибо{name_part}! А как вы?",
        "hi": "मैं बढ़िया हूँ, पूछने के लिए धन्यवाद{name_part}! आप कैसे हैं?",
        "de": "Mir geht's prima, danke der Nachfrage{name_part}! Und Ihnen?",
        "it": "Sto benissimo, grazie{name_part}! E tu come stai?",
        "fr": "Je vais très bien, merci{name_part} ! Et vous ?",
        "es": "¡Muy bien, gracias por preguntar{name_part}! ¿Y tú?",
        "ja": "元気です、ありがとう{name_part}！あなたはいかがですか？",
    },
    "share_good": {
        "en": "That's wonderful{name_part}! Your good energy is contagious — keep it going.",
        "dv": "ވަރަށް ރަނގަޅު{name_part}! އުފާވެއްޖެ.",
        "ar": "هذا رائع{name_part}! طاقتك الإيجابية معدية — استمر!",
        "zh": "太棒了{name_part}！你的好心情很有感染力，继续保持！",
        "ru": "Это замечательно{name_part}! Ваше настроение заразительно — так держать!",
        "hi": "यह तो बहुत बढ़िया है{name_part}! आपकी ऊर्जा सबको खुश कर देती है।",
        "de": "Wunderbar{name_part}! Ihre gute Laune steckt an — weiter so.",
        "it": "Fantastico{name_part}! Il tuo buonumore è contagioso.",
        "fr": "C'est merveilleux{name_part} ! Votre bonne humeur est contagieuse.",
        "es": "¡Qué maravilla{name_part}! Tu buena energía es contagiosa.",
        "ja": "素晴らしいですね{name_part}！その笑顔がみんなを元気にします。",
    },
    "share_bad": {
        "en": "I'm sorry you're feeling that way{name_part}. Take a slow breath and a sip of water — and don't hesitate to ask your supervisor for a short break. I'll check on you later.",
        "dv": "މާފުކުރައްވާ{name_part}. ރަނގަޅަށް އަރާމުކުރައްވާ.",
        "ar": "آسف لأنك تشعر بذلك{name_part}. خذ نفساً عميقاً واشرب بعض الماء، ولا تتردد في طلب استراحة قصيرة.",
        "zh": "很抱歉你有这样的感受{name_part}。深呼吸，喝点水，需要的话可以申请短暂休息。",
        "ru": "Мне жаль, что вам тяжело{name_part}. Сделайте глубокий вдох, выпейте воды и не стесняйтесь попросить короткий перерыв.",
        "hi": "मुझे खेद है कि आप ऐसा महसूस कर रहे हैं{name_part}। गहरी साँस लें, थोड़ा पानी पिएँ, और ज़रूरत हो तो छोटा ब्रेक लें।",
        "de": "Das tut mir leid{name_part}. Atmen Sie tief durch, trinken Sie etwas Wasser — und bitten Sie ruhig um eine kurze Pause.",
        "it": "Mi dispiace{name_part}. Fai un respiro profondo, bevi un po' d'acqua e chiedi pure una breve pausa.",
        "fr": "Désolé que vous vous sentiez ainsi{name_part}. Respirez profondément, buvez un peu d'eau, et n'hésitez pas à demander une courte pause.",
        "es": "Siento que te sientas así{name_part}. Respira hondo, bebe un poco de agua y no dudes en pedir un breve descanso.",
        "ja": "つらいですね{name_part}。深呼吸して、お水を飲んでください。必要なら短い休憩を取りましょう。",
    },
    "thanks": {
        "en": "Always a pleasure{name_part}! Have a great day.",
        "dv": "ޝުކުރިއްޔާ{name_part}! ރަނގަޅު ދުވަހެއް.",
        "ar": "على الرحب والسعة{name_part}! يوماً سعيداً.",
        "zh": "不客气{name_part}！祝你今天愉快。",
        "ru": "Всегда пожалуйста{name_part}! Хорошего дня.",
        "hi": "आपका स्वागत है{name_part}! आपका दिन शुभ हो।",
        "de": "Gern geschehen{name_part}! Einen schönen Tag noch.",
        "it": "Figurati{name_part}! Buona giornata.",
        "fr": "Avec plaisir{name_part} ! Bonne journée.",
        "es": "¡Un placer{name_part}! Que tengas un gran día.",
        "ja": "どういたしまして{name_part}！良い一日を。",
    },
    "goodbye": {
        "en": "Goodbye{name_part}! Safe travels and see you soon.",
        "dv": "ފަހުން ބައްދަލުވާނީ{name_part}!",
        "ar": "مع السلامة{name_part}! رحلة آمنة.",
        "zh": "再见{name_part}！一路平安。",
        "ru": "До свидания{name_part}! Счастливого пути.",
        "hi": "अलविदा{name_part}! आपकी यात्रा सुरक्षित हो।",
        "de": "Auf Wiedersehen{name_part}! Gute Reise.",
        "it": "Arrivederci{name_part}! Buon viaggio.",
        "fr": "Au revoir{name_part} ! Bon voyage.",
        "es": "¡Adiós{name_part}! Buen viaje.",
        "ja": "さようなら{name_part}！お気をつけて。",
    },
    "directions": {  # {card} is the matching info card, written by the admin (usually English)
        "en": "{card}",
        "dv": "{card}",
        "ar": "إليك ما وجدته: {card}",
        "zh": "为您找到：{card}",
        "ru": "Вот что я нашла: {card}",
        "hi": "यह रही जानकारी: {card}",
        "de": "Das habe ich gefunden: {card}",
        "it": "Ecco cosa ho trovato: {card}",
        "fr": "Voici ce que j'ai trouvé : {card}",
        "es": "Esto es lo que encontré: {card}",
        "ja": "こちらをご覧ください：{card}",
    },
    "wifi": {
        "en": "Free Wi-Fi: {wifi}. Charging points are marked with the ⚡ sign.",
        "ar": "واي فاي مجاني: {wifi}. نقاط الشحن مميزة بعلامة ⚡.",
        "zh": "免费无线网络：{wifi}。充电点标有 ⚡ 标志。",
        "ru": "Бесплатный Wi-Fi: {wifi}. Зарядные станции отмечены знаком ⚡.",
        "hi": "मुफ़्त वाई-फ़ाई: {wifi}। चार्जिंग पॉइंट ⚡ चिह्न से चिह्नित हैं।",
        "de": "Kostenloses WLAN: {wifi}. Ladestationen sind mit ⚡ markiert.",
        "it": "Wi-Fi gratuito: {wifi}. I punti di ricarica sono segnati con ⚡.",
        "fr": "Wi-Fi gratuit : {wifi}. Les bornes de recharge sont marquées ⚡.",
        "es": "Wi-Fi gratis: {wifi}. Los puntos de carga están marcados con ⚡.",
        "ja": "無料Wi-Fi：{wifi}。充電スポットは⚡マークが目印です。",
    },
    "flight": {
        "en": "For live flight times please check the flight information screens or ask the airline counter. The team at {location} is happy to help too.",
        "ar": "لمواعيد الرحلات المباشرة يرجى مراجعة شاشات معلومات الرحلات أو كاونتر شركة الطيران.",
        "zh": "实时航班信息请查看航班显示屏或咨询航空公司柜台。",
        "ru": "Актуальное расписание рейсов — на табло или у стойки авиакомпании.",
        "hi": "लाइव फ़्लाइट जानकारी के लिए कृपया फ़्लाइट सूचना स्क्रीन देखें या एयरलाइन काउंटर से पूछें।",
        "de": "Aktuelle Flugzeiten finden Sie auf den Anzeigetafeln oder am Schalter Ihrer Airline.",
        "it": "Per gli orari dei voli consulta i monitor informativi o il banco della compagnia aerea.",
        "fr": "Pour les horaires en direct, consultez les écrans d'information ou le comptoir de la compagnie.",
        "es": "Para horarios en tiempo real, consulta las pantallas de vuelos o el mostrador de tu aerolínea.",
        "ja": "最新のフライト情報は案内表示板か航空会社カウンターでご確認ください。",
    },
    "lost": {  # followed by the "call a team member" button on the kiosk
        "en": "Don't worry — please stay here at {location}. A team member can come to you right away.",
        "ar": "لا تقلق — يرجى البقاء هنا عند {location}. يمكن لأحد الموظفين القدوم إليك فوراً.",
        "zh": "别担心——请留在{location}。工作人员可以马上过来帮您。",
        "ru": "Не волнуйтесь — оставайтесь здесь, у {location}. Сотрудник может сразу подойти к вам.",
        "hi": "चिंता न करें — कृपया यहीं {location} पर रहें। स्टाफ़ तुरंत आपके पास आ सकता है।",
        "de": "Keine Sorge — bitte bleiben Sie hier bei {location}. Jemand vom Team kann sofort kommen.",
        "it": "Non preoccuparti — resta qui a {location}. Qualcuno del personale può arrivare subito.",
        "fr": "Pas d'inquiétude — restez ici à {location}. Un membre de l'équipe peut venir tout de suite.",
        "es": "No te preocupes — quédate aquí en {location}. Alguien del equipo puede venir enseguida.",
        "ja": "ご安心ください。{location}でお待ちください。スタッフがすぐに伺えます。",
    },
    "complaint": {
        "en": "Thank you for telling me — I've recorded it so the team can follow up. I'm sorry for the trouble.",
        "ar": "شكراً لإخباري — لقد سجلت ملاحظتك ليتابعها الفريق. نعتذر عن الإزعاج.",
        "zh": "谢谢告诉我，我已记录下来，团队会跟进。给您带来不便，非常抱歉。",
        "ru": "Спасибо, что сообщили — я записал это, команда разберётся. Приносим извинения.",
        "hi": "बताने के लिए धन्यवाद — मैंने इसे दर्ज कर लिया है, टीम इस पर काम करेगी।",
        "de": "Danke für den Hinweis — ich habe es notiert, das Team kümmert sich darum.",
        "it": "Grazie per la segnalazione — l'ho registrata, il team se ne occuperà.",
        "fr": "Merci de me l'avoir signalé — c'est noté, l'équipe va s'en occuper.",
        "es": "Gracias por decírmelo — lo he registrado para que el equipo lo revise.",
        "ja": "お知らせありがとうございます。記録しましたので、担当者が対応します。",
    },
    "attendance": {
        "en": "{card}",
    },
    "compliment": {
        "en": "Honestly{name_part}? You look ready to own the day. Confidence suits you.",
        "ar": "بصراحة{name_part}؟ تبدو مستعداً ليوم رائع. الثقة تليق بك.",
        "zh": "说实话{name_part}？你看起来状态满满，自信很适合你！",
        "ru": "Честно{name_part}? Вы выглядите готовым покорить этот день!",
        "hi": "सच कहूँ{name_part}? आप आज के दिन के लिए पूरी तरह तैयार दिख रहे हैं!",
        "de": "Ehrlich{name_part}? Sie sehen aus, als würden Sie den Tag rocken.",
        "it": "Sinceramente{name_part}? Sembri pronto a conquistare la giornata.",
        "fr": "Franchement{name_part} ? Vous avez l'air prêt à conquérir la journée.",
        "es": "¿Sinceramente{name_part}? Pareces listo para comerte el día.",
        "ja": "正直に言うと{name_part}、今日も最高に輝いていますよ！",
    },
    "joke": {
        "en": "Why did the suitcase go to therapy? It had too much emotional baggage. 🧳",
        "ar": "لماذا ذهبت الحقيبة إلى الطبيب النفسي؟ لأنها تحمل الكثير من الأمتعة العاطفية! 🧳",
        "zh": "为什么行李箱去看心理医生？因为它的“包袱”太重了！🧳",
        "ru": "Почему чемодан пошёл к психологу? У него слишком много эмоционального багажа! 🧳",
        "hi": "सूटकेस थेरेपी पर क्यों गया? क्योंकि उसके पास बहुत ज़्यादा भावनात्मक सामान था! 🧳",
        "de": "Warum ging der Koffer zur Therapie? Zu viel emotionales Gepäck! 🧳",
        "it": "Perché la valigia è andata dallo psicologo? Troppo bagaglio emotivo! 🧳",
        "fr": "Pourquoi la valise est-elle allée chez le psy ? Trop de bagages émotionnels ! 🧳",
        "es": "¿Por qué la maleta fue a terapia? ¡Tenía demasiado equipaje emocional! 🧳",
        "ja": "スーツケースがカウンセリングに行った理由？心の荷物が多すぎたから！🧳",
    },
    "about": {
        "en": "I'm Aura, the digital host of {location} at {airport}. I help the team clock in, share useful info, and I'm always up for a chat.",
        "ar": "أنا أورا، المضيفة الرقمية في {location} في {airport}. أساعد الفريق وأشارك المعلومات المفيدة.",
        "zh": "我是Aura，{airport}{location}的数字助手。我帮助团队打卡，也提供实用信息。",
        "ru": "Я Аура — цифровой помощник {location} в {airport}. Помогаю команде и гостям.",
        "hi": "मैं ऑरा हूँ, {airport} में {location} की डिजिटल होस्ट। मैं टीम और यात्रियों की मदद करती हूँ।",
        "de": "Ich bin Aura, die digitale Gastgeberin von {location} am {airport}.",
        "it": "Sono Aura, l'assistente digitale di {location} a {airport}.",
        "fr": "Je suis Aura, l'hôtesse numérique de {location} à {airport}.",
        "es": "Soy Aura, la anfitriona digital de {location} en {airport}.",
        "ja": "私はAura、{airport}の{location}のデジタル案内係です。",
    },
    "other": {
        "en": "I'm still learning that one{name_part}. The team at {location} can help — or ask me about Wi-Fi, directions or your shift.",
        "dv": "މާފުކުރައްވާ{name_part}، އެކަން އަޅުގަނޑަށް ނޭނގެ.",
        "ar": "ما زلت أتعلم ذلك{name_part}. يمكن لفريق {location} مساعدتك.",
        "zh": "这个我还在学习中{name_part}。{location}的工作人员可以帮助您。",
        "ru": "Этому я ещё учусь{name_part}. Команда {location} с радостью поможет.",
        "hi": "यह मैं अभी सीख रही हूँ{name_part}। {location} की टीम आपकी मदद कर सकती है।",
        "de": "Das lerne ich noch{name_part}. Das Team von {location} hilft gern.",
        "it": "Sto ancora imparando{name_part}. Il team di {location} può aiutarti.",
        "fr": "J'apprends encore cela{name_part}. L'équipe de {location} peut vous aider.",
        "es": "Todavía estoy aprendiendo eso{name_part}. El equipo de {location} puede ayudarte.",
        "ja": "それはまだ勉強中です{name_part}。{location}のスタッフがお手伝いします。",
    },
}


def render(intent: str, lang: str, **values) -> tuple:
    """Return (text, language actually used)."""
    variants = T.get(intent) or T["other"]
    used = lang if lang in variants else "en"
    text = variants.get(used) or T["other"]["en"]
    safe = {k: ("" if v is None else v) for k, v in values.items()}
    try:
        return text.format(**safe).strip(), used
    except (KeyError, IndexError):
        return text, used


# ----------------------------------------------------------------------------- flights
# {status} comes from FLIGHT_STATUS; {extra} is ", gate 5" / ", belt 2" built with FLIGHT_WORDS.
FLIGHT_DEP = {
    "en": "{number} to {city}: {status}. Departure {time}{extra}.",
    "dv": "{number} → {city}: {status}. {time}{extra}",
    "ar": "الرحلة {number} إلى {city}: {status}. المغادرة {time}{extra}.",
    "zh": "{number} 飞往{city}：{status}。起飞时间 {time}{extra}。",
    "ru": "Рейс {number} в {city}: {status}. Вылет в {time}{extra}.",
    "hi": "{number} {city} के लिए: {status}। प्रस्थान {time}{extra}।",
    "de": "{number} nach {city}: {status}. Abflug {time}{extra}.",
    "it": "{number} per {city}: {status}. Partenza {time}{extra}.",
    "fr": "{number} pour {city} : {status}. Départ {time}{extra}.",
    "es": "{number} a {city}: {status}. Salida {time}{extra}.",
    "ja": "{number}便 {city}行き：{status}。出発 {time}{extra}。",
}
FLIGHT_ARR = {
    "en": "{number} from {city}: {status}. Arrival {time}{extra}.",
    "dv": "{number} ← {city}: {status}. {time}{extra}",
    "ar": "الرحلة {number} من {city}: {status}. الوصول {time}{extra}.",
    "zh": "{number} 来自{city}：{status}。到达时间 {time}{extra}。",
    "ru": "Рейс {number} из {city}: {status}. Прилёт в {time}{extra}.",
    "hi": "{number} {city} से: {status}। आगमन {time}{extra}।",
    "de": "{number} aus {city}: {status}. Ankunft {time}{extra}.",
    "it": "{number} da {city}: {status}. Arrivo {time}{extra}.",
    "fr": "{number} de {city} : {status}. Arrivée {time}{extra}.",
    "es": "{number} desde {city}: {status}. Llegada {time}{extra}.",
    "ja": "{number}便 {city}発：{status}。到着 {time}{extra}。",
}
FLIGHT_ASK = {
    "en": "Happy to check! What's your flight number? It's on your boarding pass, like EK653.",
    "ar": "بكل سرور! ما رقم رحلتك؟ ستجده في بطاقة الصعود، مثل EK653.",
    "zh": "乐意为您查询！请问您的航班号是多少？登机牌上有，例如 EK653。",
    "ru": "С радостью проверю! Какой у вас номер рейса? Он указан в посадочном талоне, например EK653.",
    "hi": "ज़रूर! आपका फ़्लाइट नंबर क्या है? यह बोर्डिंग पास पर होता है, जैसे EK653।",
    "de": "Gern! Wie lautet Ihre Flugnummer? Sie steht auf der Bordkarte, z. B. EK653.",
    "it": "Volentieri! Qual è il numero del tuo volo? È sulla carta d'imbarco, ad es. EK653.",
    "fr": "Avec plaisir ! Quel est votre numéro de vol ? Il figure sur la carte d'embarquement, ex. EK653.",
    "es": "¡Claro! ¿Cuál es tu número de vuelo? Está en la tarjeta de embarque, p. ej. EK653.",
    "ja": "お調べします！便名を教えてください。搭乗券に記載されています（例：EK653）。",
}
FLIGHT_STATUS = {
    "en": {"scheduled": "on schedule", "checkin": "check-in open", "boarding": "now boarding", "gate_closed": "gate closed",
           "departed": "departed", "delayed": "delayed", "cancelled": "cancelled", "expected": "on time",
           "approaching": "landing shortly", "landed": "landed", "diverted": "diverted"},
    "ar": {"scheduled": "في موعدها", "checkin": "تسجيل الوصول مفتوح", "boarding": "الصعود جارٍ الآن", "gate_closed": "البوابة مغلقة",
           "departed": "غادرت", "delayed": "متأخرة", "cancelled": "ملغاة", "expected": "في موعدها",
           "approaching": "تهبط قريباً", "landed": "هبطت", "diverted": "تم تحويلها"},
    "zh": {"scheduled": "准点", "checkin": "正在办理值机", "boarding": "正在登机", "gate_closed": "登机口已关闭",
           "departed": "已起飞", "delayed": "延误", "cancelled": "已取消", "expected": "准点",
           "approaching": "即将降落", "landed": "已到达", "diverted": "已备降"},
    "ru": {"scheduled": "по расписанию", "checkin": "идёт регистрация", "boarding": "идёт посадка", "gate_closed": "выход закрыт",
           "departed": "вылетел", "delayed": "задерживается", "cancelled": "отменён", "expected": "по расписанию",
           "approaching": "скоро приземлится", "landed": "приземлился", "diverted": "перенаправлен"},
    "hi": {"scheduled": "समय पर", "checkin": "चेक-इन खुला है", "boarding": "बोर्डिंग जारी है", "gate_closed": "गेट बंद",
           "departed": "रवाना हो गई", "delayed": "देरी से", "cancelled": "रद्द", "expected": "समय पर",
           "approaching": "जल्द उतरेगी", "landed": "उतर चुकी है", "diverted": "मार्ग बदला गया"},
    "de": {"scheduled": "planmäßig", "checkin": "Check-in geöffnet", "boarding": "Boarding läuft", "gate_closed": "Gate geschlossen",
           "departed": "gestartet", "delayed": "verspätet", "cancelled": "annulliert", "expected": "pünktlich",
           "approaching": "im Landeanflug", "landed": "gelandet", "diverted": "umgeleitet"},
    "it": {"scheduled": "in orario", "checkin": "check-in aperto", "boarding": "imbarco in corso", "gate_closed": "gate chiuso",
           "departed": "partito", "delayed": "in ritardo", "cancelled": "cancellato", "expected": "in orario",
           "approaching": "in atterraggio", "landed": "atterrato", "diverted": "dirottato"},
    "fr": {"scheduled": "à l'heure", "checkin": "enregistrement ouvert", "boarding": "embarquement en cours", "gate_closed": "porte fermée",
           "departed": "parti", "delayed": "retardé", "cancelled": "annulé", "expected": "à l'heure",
           "approaching": "atterrissage imminent", "landed": "atterri", "diverted": "dérouté"},
    "es": {"scheduled": "a tiempo", "checkin": "facturación abierta", "boarding": "embarcando", "gate_closed": "puerta cerrada",
           "departed": "despegó", "delayed": "retrasado", "cancelled": "cancelado", "expected": "a tiempo",
           "approaching": "aterrizando pronto", "landed": "aterrizó", "diverted": "desviado"},
    "ja": {"scheduled": "定刻", "checkin": "チェックイン受付中", "boarding": "搭乗中", "gate_closed": "搭乗締切",
           "departed": "出発済み", "delayed": "遅延", "cancelled": "欠航", "expected": "定刻",
           "approaching": "まもなく到着", "landed": "到着済み", "diverted": "目的地変更"},
}
FLIGHT_WORDS = {  # gate, check-in desks, baggage belt, delay "(+N min)"
    "en": ("gate", "check-in desks", "baggage belt", "+{n} min"), "ar": ("البوابة", "كاونترات التسجيل", "حزام الأمتعة", "+{n} د"),
    "zh": ("登机口", "值机柜台", "行李转盘", "+{n}分钟"), "ru": ("выход", "стойки регистрации", "лента", "+{n} мин"),
    "hi": ("गेट", "चेक-इन काउंटर", "बैगेज बेल्ट", "+{n} मिनट"), "de": ("Gate", "Check-in-Schalter", "Gepäckband", "+{n} Min."),
    "it": ("gate", "banchi check-in", "nastro bagagli", "+{n} min"), "fr": ("porte", "comptoirs", "tapis bagages", "+{n} min"),
    "es": ("puerta", "mostradores", "cinta de equipaje", "+{n} min"), "ja": ("搭乗口", "チェックインカウンター", "手荷物受取所", "+{n}分"),
}


def render_flight(f: dict, lang: str) -> tuple:
    used = lang if lang in FLIGHT_DEP else "en"
    words = FLIGHT_WORDS.get(used, FLIGHT_WORDS["en"])
    status = FLIGHT_STATUS.get(used, FLIGHT_STATUS["en"]).get(f.get("status"), f.get("status") or "")
    when = (f.get("estimated") or f.get("scheduled") or "")[11:16]
    if (f.get("delay_min") or 0) >= 15 and f.get("status") not in ("cancelled",):
        when += f" ({words[3].format(n=f['delay_min'])})"
    extra = ""
    if f["dir"] == "dep":
        if f.get("gate") and f.get("status") in ("boarding", "gate_closed", "checkin", "delayed", "scheduled"):
            extra = f", {words[0]} {f['gate']}"
        if f.get("desk") and f.get("status") in ("checkin", "scheduled", "delayed"):
            extra += f", {words[1]} {f['desk']}"
    elif f.get("belt") and f.get("status") in ("landed", "approaching"):
        extra = f", {words[2]} {f['belt']}"
    tpl = (FLIGHT_DEP if f["dir"] == "dep" else FLIGHT_ARR)[used]
    return tpl.format(number=f["number"], city=f.get("city") or f.get("city_iata") or "", status=status,
                      time=when, extra=extra), used


# ----------------------------------------------------------------------------- assistance
ASSIST_KINDS = {
    "wheelchair": ("♿", "Wheelchair / reduced mobility"),
    "medical": ("🩺", "Medical help"),
    "lost_item": ("🧳", "Lost item"),
    "lost_person": ("🧒", "Lost child / person"),
    "porter": ("🛄", "Porter / baggage help"),
    "security": ("🛡️", "Security concern"),
    "other": ("💬", "Talk to a staff member"),
}
ASSIST_OFFER = {
    "en": "I can call a team member to help you — tap the button below.",
    "ar": "يمكنني استدعاء أحد الموظفين لمساعدتك — اضغط الزر أدناه.",
    "zh": "我可以为您呼叫工作人员——请点击下方按钮。",
    "ru": "Я могу вызвать сотрудника — нажмите кнопку ниже.",
    "hi": "मैं आपकी मदद के लिए स्टाफ़ को बुला सकती हूँ — नीचे बटन दबाएँ।",
    "de": "Ich kann eine Mitarbeiterin oder einen Mitarbeiter rufen — tippen Sie unten.",
    "it": "Posso chiamare qualcuno del personale — tocca il pulsante qui sotto.",
    "fr": "Je peux appeler un membre de l'équipe — touchez le bouton ci-dessous.",
    "es": "Puedo llamar a alguien del personal — toca el botón de abajo.",
    "ja": "スタッフをお呼びできます。下のボタンを押してください。",
}
