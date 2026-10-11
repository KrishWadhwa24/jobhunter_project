"""A dictionary of technical terms with their spellings. Used to read job descriptions, to score resumes,
and to check that a rewritten line has not introduced a technology that is not in the facts."""
from __future__ import annotations

import re
from typing import Dict, List, Pattern, Tuple

# canonical name -> list of regex alternatives (matched case-insensitively with word-ish boundaries),
# except entries starting with "CS:" which are case-sensitive.
_RAW_SOFTWARE: List[Tuple[str, List[str]]] = [
    # languages
    ("Java", [r"java(?!\s*script)"]),
    ("Python", [r"python3?"]),
    ("Golang", [r"golang", r"CS:(?<![A-Za-z])Go(?=[\s,/)\.;:]|$)(?!\s+(?:to|live|the|with|for|beyond|above|getting|ahead|through))"]),
    ("JavaScript", [r"javascript", r"(?<![A-Za-z.])js\b", r"ecmascript", r"es6"]),
    ("TypeScript", [r"typescript"]),
    ("C/C++", [r"c\+\+", r"c/c\+\+", r"CS:(?<![A-Za-z])C(?=[\s,/)\.;:]|$)(?!\s+level)"]),
    ("C#", [r"c#", r"\.net", r"dotnet"]),
    ("Kotlin", [r"kotlin"]), ("Scala", [r"scala"]), ("Rust", [r"rust"]), ("Ruby", [r"ruby"]),
    ("PHP", [r"php"]), ("Swift", [r"swift"]), ("R", [r"CS:(?<![A-Za-z])R(?=[\s,/)\.;]|$)(?<!\bI\b)(?= programming| language| studio)"]),
    ("SQL", [r"sql(?!\s*server)", r"t-sql", r"pl/sql", r"query design"]),
    ("Bash", [r"bash", r"shell scripting", r"shell script"]),
    # backend frameworks / concepts
    ("Spring Boot", [r"spring\s*boot", r"springboot", r"spring framework", r"spring mvc", r"spring"]),
    ("Node.js", [r"node\.?js", r"nodejs", r"node"]),
    ("Express.js", [r"express\.?js", r"expressjs", r"express"]),
    ("FastAPI", [r"fastapi", r"fast api"]), ("Django", [r"django"]), ("Flask", [r"flask"]),
    ("Gin", [r"CS:(?<![A-Za-z])Gin(?![A-Za-z])"]),
    ("NestJS", [r"nestjs", r"nest\.js"]),
    ("REST APIs", [r"rest(?:ful)?(?:\s+)?(?:apis?|services?|endpoints?|web services?)?", r"restful", r"http apis?", r"web services?"]),
    ("GraphQL", [r"graphql"]), ("gRPC", [r"grpc"]),
    ("Microservices", [r"micro-?services?", r"service-oriented", r"\bsoa\b"]),
    ("Event-Driven Architecture", [r"event[- ]driven", r"event streaming", r"event[- ]based"]),
    ("Distributed Systems", [r"distributed (?:systems?|computing|architecture|data)", r"distributed"]),
    ("System Design", [r"system design", r"systems design", r"low[- ]level design", r"high[- ]level design", r"\blld\b", r"\bhld\b", r"architecture design"]),
    ("OOP", [r"\boop\b", r"object[- ]oriented", r"design patterns?"]),
    ("Clean Architecture", [r"clean architecture", r"layered architecture", r"hexagonal"]),
    ("WebSockets", [r"websockets?", r"web sockets?", r"real-?time (?:communication|messaging|notifications?)"]),
    ("Webhooks", [r"webhooks?"]),
    ("JSON", [r"\bjson\b"]),
    ("Data pipelines", [r"data pipelines?", r"etl", r"elt", r"data processing", r"data engineering", r"stream processing", r"batch processing"]),
    ("Pub/Sub messaging", [r"pub/?sub", r"publish[- ]subscribe", r"message queues?", r"messaging (?:systems?|queues?|pipelines?)", r"message broker"]),
    ("Dead-letter queues", [r"dead[- ]letter", r"\bdlq\b"]),
    ("State machines", [r"state machines?", r"workflow engines?", r"workflow[- ]based"]),
    ("Scheduled job orchestration", [r"job orchestration", r"job scheduling", r"cron", r"schedulers?", r"orchestrat\w+", r"workflow automation"]),
    ("Automation", [r"automation", r"automating", r"automated workflows?"]),
    ("Third-party API integration", [r"third[- ]party (?:apis?|integrations?|saas)", r"api integrations?", r"integrat\w+ (?:with )?(?:external |third)"]),
    # messaging / streaming
    ("Apache Kafka", [r"kafka"]), ("RabbitMQ", [r"rabbitmq"]), ("SQS/SNS", [r"\bsqs\b", r"\bsns\b", r"kinesis"]),
    ("Spark", [r"apache spark", r"\bspark\b", r"pyspark"]), ("Hadoop", [r"hadoop", r"\bhdfs\b", r"mapreduce"]),
    ("Airflow", [r"airflow"]), ("Flink", [r"flink"]),
    # data
    ("PostgreSQL", [r"postgres(?:ql)?", r"psql"]), ("MySQL", [r"mysql", r"mariadb"]),
    ("MongoDB", [r"mongo(?:db)?"]), ("Redis", [r"redis"]), ("Cassandra", [r"cassandra"]),
    ("DynamoDB", [r"dynamo(?:db)?"]), ("Elasticsearch", [r"elastic ?search", r"opensearch", r"\belk\b"]),
    ("NoSQL", [r"no-?sql", r"document (?:store|database)", r"key[- ]value"]),
    ("Firebase", [r"firebase", r"firestore"]),
    ("Oracle DB", [r"oracle (?:db|database)", r"oracle sql"]), ("SQL Server", [r"sql server", r"mssql"]),
    ("Snowflake", [r"snowflake"]), ("BigQuery", [r"bigquery"]),
    ("Databases", [r"rdbms", r"relational databases?", r"\bdbms\b", r"database design", r"data modell?ing"]),
    ("Caching", [r"caching", r"memcached", r"\bcache\b"]),
    # cloud / devops
    ("AWS", [r"\baws\b", r"amazon web services", r"\bec2\b", r"\bs3\b", r"lambda", r"cloudformation"]),
    ("Azure", [r"azure"]), ("GCP", [r"\bgcp\b", r"google cloud"]),
    ("Docker", [r"docker"]), ("Kubernetes", [r"kubernetes", r"\bk8s\b", r"\beks\b", r"helm"]),
    ("Terraform", [r"terraform", r"infrastructure as code", r"\biac\b", r"ansible"]),
    ("CI/CD", [r"ci/?cd", r"continuous (?:integration|delivery|deployment)", r"jenkins", r"github actions", r"gitlab ci"]),
    ("Linux", [r"linux", r"unix"]), ("Git", [r"\bgit\b", r"github", r"gitlab", r"bitbucket", r"version control"]),
    ("Cloud", [r"cloud[- ](?:native|computing|infrastructure|platform|services?)", r"\bcloud\b"]),
    ("Observability", [r"observability", r"prometheus", r"grafana", r"datadog", r"new relic", r"splunk", r"monitoring", r"logging"]),
    ("Agile", [r"agile", r"scrum", r"kanban", r"sprints?"]),
    ("Code reviews", [r"code reviews?", r"peer reviews?", r"pull requests?"]),
    # testing
    ("Unit Testing", [r"unit tests?", r"unit testing", r"junit", r"pytest", r"mockito", r"\btdd\b", r"test[- ]driven"]),
    ("Jest", [r"\bjest\b", r"mocha"]),
    ("Integration Testing", [r"integration tests?", r"integration testing", r"functional tests?", r"functional testing", r"end[- ]to[- ]end tests?", r"\be2e\b"]),
    ("Load Testing", [r"load tests?", r"load testing", r"performance testing", r"stress testing", r"jmeter", r"gatling", r"\blocust\b", r"benchmark\w*"]),
    ("Test Automation", [r"test automation", r"automated tests?", r"automated testing", r"selenium", r"cypress", r"playwright", r"\bqa\b"]),
    ("Root-cause analysis", [r"root[- ]cause", r"\brca\b", r"troubleshoot\w*", r"debugging", r"triag\w+", r"incident"]),
    ("Performance tuning", [r"performance (?:tuning|optimi[sz]ation|engineering)", r"latency", r"throughput", r"profil(?:ing|er)", r"scalab\w+", r"low[- ]latency"]),
    ("High availability", [r"high[- ]availability", r"fault[- ]toleran\w+", r"resilien\w+", r"reliability", r"\bsre\b", r"\bslas?\b"]),
    # frontend
    ("React", [r"react(?:\.?js)?", r"redux"]), ("Next.js", [r"next\.?js"]), ("Angular", [r"angular(?:js)?"]),
    ("Vue", [r"vue(?:\.?js)?"]), ("HTML/CSS", [r"\bhtml5?\b", r"\bcss3?\b", r"tailwind", r"sass"]),
    ("Full-stack", [r"full[- ]?stack"]),
    # ai / ml (so gaps are visible)
    ("Machine Learning", [r"machine learning", r"\bml\b", r"deep learning", r"neural networks?", r"scikit-?learn", r"tensorflow", r"pytorch", r"\bnlp\b", r"computer vision"]),
    ("LLMs / GenAI", [r"\bllms?\b", r"generative ai", r"\bgenai\b", r"langchain", r"\brag\b", r"prompt engineering", r"vector databases?", r"openai", r"large language models?"]),
    ("Data analysis", [r"data analy\w+", r"pandas", r"numpy", r"tableau", r"power bi", r"looker", r"excel"]),
    # android / mobile
    ("Android", [r"android"]), ("iOS", [r"\bios\b", r"swiftui"]), ("Mobile", [r"mobile (?:app|development)", r"react native", r"flutter"]),
    # security / networking
    ("Security", [r"security", r"oauth2?", r"\bjwt\b", r"authentication", r"authorization", r"\brbac\b", r"role-based access"]),
    ("Networking", [r"computer networks?", r"networking", r"tcp/?ip", r"\bhttp\b", r"dns", r"load balanc\w+"]),
    # cs fundamentals
    ("Data Structures & Algorithms", [r"data structures?", r"algorithms?", r"\bdsa\b", r"problem[- ]solving", r"competitive programming"]),
    ("Operating Systems", [r"operating systems?", r"\bos\b internals", r"concurrency", r"multi-?thread\w*", r"multi-?processing", r"async\w*"]),
    ("Computer Networks", [r"computer networks?"]),
    ("Embedded / C++ systems", [r"embedded", r"firmware", r"rtos", r"device drivers?", r"kernel"]),
    ("Geospatial", [r"geo-?spatial", r"\bgis\b", r"geo-?location", r"gps"]),
    ("DevOps", [r"devops", r"site reliability", r"\bsre\b"]),
    ("Open Source", [r"open[- ]source"]),
]

PATTERNS: Dict[str, List[Pattern[str]]] = {}
CANONICAL: List[str] = []
SOFT: set = set()
ACTIVE = ""


def _compile(raw: List[Tuple[str, List[str]]]) -> None:
    PATTERNS.clear()
    for canon, alts in raw:
        pats: List[Pattern[str]] = []
        for a in alts:
            if a.startswith("CS:"):
                pats.append(re.compile(a[3:]))
            else:
                if a.startswith(r"\b") or a.startswith(r"(?<"):
                    pats.append(re.compile(a, re.I))
                else:
                    pats.append(re.compile(r"(?<![A-Za-z0-9])(?:" + a + r")(?![A-Za-z0-9])", re.I))
        PATTERNS[canon] = pats



_SOFT_SOFTWARE = {"Cloud", "Databases", "Caching", "Observability", "Automation", "Networking", "Security", "Open Source",
                  "Git", "JSON", "Agile", "Code reviews", "Data analysis", "DevOps", "Mobile", "Linux"}


def use(name: str) -> None:
    """Pick the vocabulary: "software" or "general" (writing, teaching, customer service, AI training).
    One process serves one profile, so this is set once from the profile's level scheme."""
    global ACTIVE, SOFT
    name = "general" if name == "general" else "software"
    if name == ACTIVE:
        return
    if name == "general":
        from . import terms_general as G
        raw, soft = G.RAW, set(G.SOFT)
    else:
        raw, soft = _RAW_SOFTWARE, set(_SOFT_SOFTWARE)
    _compile(raw)
    CANONICAL[:] = [c for c, _ in raw]
    SOFT = soft
    ACTIVE = name


def use_for(cfg) -> None:
    from .. import config
    use(config.scheme_name(cfg or config.load_config()))


def extract_terms(text: str) -> Dict[str, int]:
    """canonical term -> number of mentions."""
    out: Dict[str, int] = {}
    if not text:
        return out
    for canon, pats in PATTERNS.items():
        n = max((len(p.findall(text)) for p in pats), default=0)
        if n:
            out[canon] = n
    if ACTIVE != "software":
        return out
    # "Java" must not fire on JavaScript; "Spring" alone only counts when near boot/java context
    if "Spring Boot" in out and not re.search(r"spring\s*boot|springboot|spring framework|spring mvc", text, re.I):
        if not re.search(r"\bjava\b", text, re.I):
            out.pop("Spring Boot")
    if "Node.js" in out and not re.search(r"node\.?js|nodejs|node (?:and|/|\+)|backend|server-side|express", text, re.I):
        out.pop("Node.js")
    if "Express.js" in out and not re.search(r"express\.?js|expressjs|node|\bmern\b|\bmean\b", text, re.I):
        out.pop("Express.js")
    if "REST APIs" in out and not re.search(r"\brest\b|restful|\bapis?\b|web services?|endpoints?", text, re.I):
        out.pop("REST APIs")
    if "SQL" in out and "MySQL" in out:
        # "MySQL" matches the sql pattern boundary? it doesn't ((?<![A-Za-z]) blocks), keep both if standalone SQL exists
        if not re.search(r"(?<![A-Za-z])sql(?!\s*server)(?![A-Za-z])", text.replace("MySQL", "").replace("PostgreSQL", "")
                         .replace("NoSQL", ""), re.I):
            out.pop("SQL")
    return out


def term_set(text: str) -> set:
    return set(extract_terms(text))


def _init() -> None:
    try:
        from .. import config
        use(config.scheme_name(config.load_config()))
    except Exception:
        use("software")


_init()
