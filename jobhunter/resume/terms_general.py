"""Vocabulary for non-technical roles: writing, editing, communications, teaching, customer service, AI training.
Same format as the software list in terms.py: canonical name -> regex alternatives (case-insensitive)."""
from __future__ import annotations

from typing import List, Tuple

RAW: List[Tuple[str, List[str]]] = [
    # writing and editing
    ("Content writing", [r"content writ\w+", r"content creat\w+", r"web content", r"article writing", r"blog(?:ging| posts?| writing)?", r"long[- ]form"]),
    ("Copywriting", [r"copy ?writ\w+", r"ad copy", r"marketing copy", r"product descriptions?", r"landing pages?"]),
    ("Creative writing", [r"creative writ\w+", r"fiction", r"poetry", r"short stories", r"storytelling", r"narrative"]),
    ("Editing", [r"edit(?:ing|ed|or|ors)?", r"copy[- ]?edit\w*", r"line edit\w*", r"developmental edit\w*", r"revis\w+ drafts?"]),
    ("Proofreading", [r"proof ?read\w*", r"grammar", r"punctuation", r"error[- ]free", r"attention to detail"]),
    ("Style guides", [r"style guides?", r"\bap style\b", r"chicago manual", r"\bmla\b", r"\bapa\b", r"house style", r"brand voice", r"tone of voice"]),
    ("Fact-checking", [r"fact[- ]?check\w*", r"verif(?:y|ying|ication) (?:facts|sources|information)"]),
    ("Research", [r"research\w*", r"desk research", r"literature review", r"secondary research", r"source (?:gathering|material)"]),
    ("Technical writing", [r"technical writ\w+", r"documentation", r"knowledge base", r"help center", r"how-?to guides?", r"user guides?"]),
    ("UX writing", [r"ux writ\w+", r"microcopy", r"content design"]),
    ("Scriptwriting", [r"script ?writ\w+", r"scripts?", r"screenplay", r"video scripts?", r"podcast"]),
    ("Journalism", [r"journalis\w+", r"reporting", r"news(?:room| writing)?", r"interview\w*", r"feature stor\w+", r"press"]),
    ("Editorial", [r"editorial", r"publishing", r"manuscripts?", r"commissioning", r"book (?:production|publishing)"]),
    ("Content strategy", [r"content strateg\w+", r"content calendars?", r"editorial calendars?", r"content planning", r"content marketing"]),
    ("SEO", [r"\bseo\b", r"search engine optimi[sz]ation", r"keyword research", r"on-?page", r"meta (?:titles?|descriptions?)", r"semrush", r"ahrefs"]),
    ("Social media", [r"social media", r"instagram", r"linkedin", r"twitter", r"facebook", r"community management", r"reels?"]),
    ("Email marketing", [r"email (?:marketing|campaigns?|newsletters?)", r"newsletters?", r"mailchimp", r"drip"]),
    ("Communications", [r"communications?", r"corporate communications?", r"internal communications?", r"press releases?", r"public relations", r"\bpr\b", r"media relations", r"stakeholder"]),
    ("CMS & tools", [r"wordpress", r"\bcms\b", r"content management", r"contentful", r"webflow", r"medium\.com", r"notion", r"google docs", r"grammarly"]),
    ("Design tools", [r"canva", r"figma", r"photoshop", r"adobe", r"video editing", r"capcut"]),
    ("Analytics", [r"google analytics", r"\bga4\b", r"analytics", r"metrics", r"engagement rates?", r"search console", r"reporting dashboards?"]),
    ("Localisation & translation", [r"locali[sz]ation", r"translat\w+", r"transcri\w+", r"subtitl\w+", r"bilingual", r"hindi", r"multilingual"]),
    ("English language", [r"\benglish\b", r"english (?:language|literature|proficiency|fluency)", r"fluent english", r"native[- ]level", r"excellent (?:written|verbal)", r"business english", r"c1|c2|ielts|toefl"]),
    ("Linguistics", [r"linguistic\w*", r"phonetics", r"syntax", r"semantics", r"discourse", r"literary (?:analysis|theory|criticism)", r"critical (?:analysis|thinking|reading)"]),
    # teaching and chess
    ("Teaching & tutoring", [r"tutor\w*", r"teach\w*", r"instruct\w+", r"lesson (?:plans?|planning|delivery)", r"classroom", r"mentor\w*", r"coaching", r"educator"]),
    ("Curriculum design", [r"curriculum", r"course (?:design|content|development)", r"syllabus", r"learning materials?", r"worksheets?", r"instructional design", r"assessments?"]),
    ("ESL / TEFL", [r"\besl\b", r"\befl\b", r"\btefl\b", r"\btesol\b", r"\bcelta\b", r"english as a (?:second|foreign)", r"spoken english", r"conversation practice", r"phonics", r"young learners", r"kids?|children"]),
    ("Online teaching", [r"online (?:teaching|tutoring|classes|lessons|coaching)", r"\bzoom\b", r"google meet", r"virtual classroom", r"remote (?:teaching|tutoring)", r"video lessons?"]),
    ("Chess", [r"\bchess\b", r"fide", r"chess (?:coaching|coach|tournaments?|training)", r"openings?", r"endgames?"]),
    ("Public speaking", [r"public speaking", r"debate", r"elocution", r"presentations?", r"\bmun\b", r"model united nations", r"speaker"]),
    # customer service
    ("Customer service", [r"customer (?:service|support|care|experience|success|satisfaction|relations?)", r"client (?:service|support|servicing)", r"\bcx\b", r"customer-?facing", r"service desk", r"help ?desk"]),
    ("Voice support", [r"voice (?:process|support)", r"inbound", r"outbound", r"call (?:handling|centre|center)", r"phone support", r"telephon\w+"]),
    ("Chat & email support", [r"chat support", r"live chat", r"email support", r"non[- ]voice", r"written support", r"omni-?channel", r"social support"]),
    ("Complaint handling", [r"complaints?", r"escalations?", r"de-?escalat\w+", r"conflict resolution", r"grievances?", r"dispute\w*", r"resolution"]),
    ("CRM & ticketing", [r"\bcrm\b", r"zendesk", r"freshdesk", r"salesforce", r"servicenow", r"ticketing", r"tickets?", r"hubspot", r"intercom", r"genesys", r"five9"]),
    ("Service metrics", [r"\baht\b", r"average handling time", r"\bcsat\b", r"\bnps\b", r"first[- ]call resolution", r"\bfcr\b", r"\bsla\b", r"quality scores?", r"\bkpis?\b", r"targets?"]),
    ("Banking & payments", [r"banking", r"credit cards?", r"payments?", r"fintech", r"kyc", r"financial services", r"insurance", r"billing", r"refunds?"]),
    ("Data entry", [r"data entry", r"typing speed", r"\bwpm\b", r"records?", r"back[- ]office", r"documentation accuracy"]),
    ("Content moderation", [r"content moderation", r"moderat\w+", r"trust (?:&|and) safety", r"policy enforcement", r"abuse reports?"]),
    ("Sales support", [r"upsell\w*", r"cross-?sell\w*", r"lead generation", r"business development", r"inside sales", r"sales support"]),
    # AI training
    ("AI training", [r"ai (?:trainer|training|tutor)", r"llm (?:training|evaluation|trainer)", r"model (?:training|evaluation)", r"\brlhf\b", r"prompt (?:writing|engineering)", r"prompts?", r"generative ai", r"\bllms?\b", r"chatbots?", r"chatgpt", r"large language models?"]),
    ("Data annotation", [r"annotat\w+", r"data labell?ing", r"labell?ing", r"rating", r"ranking responses", r"quality rater", r"guidelines"]),
    ("Evaluation & QA", [r"quality (?:assurance|checks?|audits?|review)", r"\bqa\b", r"audits?", r"evaluat\w+", r"accuracy", r"review(?:ing)? (?:content|outputs?)"]),
    # work skills
    ("Project management", [r"project management", r"project coordination", r"deadlines?", r"deliverables?", r"workflow", r"trello", r"asana", r"coordination", r"scheduling"]),
    ("Event management", [r"events?", r"event (?:management|planning|coordination)", r"fests?", r"festival", r"logistics", r"volunteer\w*"]),
    ("Team leadership", [r"leadership", r"team lead\w*", r"led a team", r"captain", r"head of", r"organi[sz]ed", r"managed a team"]),
    ("Collaboration", [r"collaborat\w+", r"cross-?functional", r"teamwork", r"team player", r"coordinat\w+ with"]),
    ("Microsoft Office", [r"microsoft office", r"ms office", r"\bexcel\b", r"powerpoint", r"google (?:sheets|workspace|slides)", r"g suite", r"spreadsheets?"]),
    ("Communication skills", [r"communication skills?", r"written (?:and|&) verbal", r"verbal communication", r"written communication", r"interpersonal", r"active listening", r"empathy"]),
    ("Time management", [r"time management", r"prioriti[sz]\w+", r"multitask\w*", r"work under pressure", r"fast-?paced", r"self-?motivated", r"ownership"]),
    ("Problem solving", [r"problem[- ]solving", r"analytical", r"strategic thinking", r"strategy", r"decision[- ]making", r"critical thinking"]),
    ("HR & recruiting", [r"recruit\w+", r"talent acquisition", r"onboarding", r"human resources", r"\bhr\b", r"employee engagement", r"learning (?:and|&) development", r"\bl&d\b"]),
    ("Market research", [r"market research", r"survey\w*", r"consumer insights?", r"competitive analysis", r"trend analysis", r"policy research"]),
    ("Remote work", [r"remote", r"work from home", r"\bwfh\b", r"virtual", r"distributed team", r"asynchronous"]),
]

SOFT = {"Communication skills", "Time management", "Collaboration", "Problem solving", "Microsoft Office", "Remote work",
        "Team leadership", "Research", "Public speaking", "Project management", "English language", "Proofreading",
        "Analytics", "Data entry", "Service metrics"}
