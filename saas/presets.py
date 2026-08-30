"""Role presets — the fix for "not a single decent job is showing up".

The old flow shipped one hand-tuned `config/targets.yaml` and then asked the user
to edit four textareas of raw match strings if the results were wrong. That is a
config file wearing a UI. Two things went wrong in practice:

  1. Everyone inherited one person's filters. A backend developer signing up got
     a feed tuned for cloud support, decided the product was broken, and left.
  2. When the filters were wrong the screen was simply empty. An empty result and
     a broken product look identical, so people assumed the worse one.

A preset is the smallest thing that fixes (1): pick the kind of work you want,
and the title rules are written for you. Combined with `report()` in fit_scoring,
which explains what the filter threw away, (2) stops being silent.

Two design notes worth keeping:

**Match terms are padded with spaces** — " sales " not "sales". `fit_scoring._norm`
pads the title it is testing, so a padded term behaves like a word boundary. This
is not cosmetic: the previous config excluded the bare string "sales", and "sales"
is a substring of "Salesforce", so every Salesforce posting was silently dropped
from a feed that had Salesforce in its company list.

**Seniority exclusions are derived from years, not hardcoded.** The old file
excluded senior/lead/principal for everyone because its author had 3.8 years. For
someone with 9 years those are exactly the right jobs, and the junior titles are
the noise. The direction of the filter has to follow the user.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Preset:
    key: str
    name: str
    tagline: str
    # Shown on the card so the choice is obvious without reading match rules.
    examples: tuple[str, ...]
    title_include: tuple[str, ...]
    # Titles that match `include` but are still the wrong job. Kept per-preset
    # because "the wrong job" is preset-specific: a support engineer moving out of
    # service desk does not want service desk back, but that is not a universal rule.
    title_exclude: tuple[str, ...] = ()
    # Used by suggest() to rank presets against a skill graph.
    signals: tuple[str, ...] = field(default=(), compare=False)


# Titles nobody in this product is looking for, whichever track they pick.
NOISE_EXCLUDE: tuple[str, ...] = (
    " sales ", " sales development ", " account executive ", " account manager ",
    " business development ", " recruiter ", " recruiting ", " talent acquisition ",
    " marketing ", " content writer ", " copywriter ", " intern ", " internship ",
    " trainee ", " apprentice ", " volunteer ", " teacher ", " faculty ",
    " nurse ", " driver ", " chef ", " security guard ",
)

# Anything naming a place has to name a place we target; these are the regions
# that most often turn up attached to the word "remote" on a global board.
REMOTE_EXCLUDE_REGIONS: tuple[str, ...] = (
    "usa", "u s a", "united states", "us only", "canada", "mexico", "brazil",
    "latam", "emea", "united kingdom", "ireland", "europe", "germany", "france",
    "netherlands", "poland", "portugal", "spain", "romania", "estonia", "israel",
    "australia", "new zealand", "japan", "korea", "china", "singapore",
    "malaysia", "indonesia", "philippines", "vietnam", "thailand", "egypt",
    "nigeria", "kenya", "south africa", "dubai", "uae", "saudi",
)

SENIOR_TITLES: tuple[str, ...] = (
    " senior ", " sr ", " lead ", " principal ", " staff ", " director ",
    " head of ", " vice president ", " vp ", " chief ", " architect ",
    " manager ", " management ",
)

JUNIOR_TITLES: tuple[str, ...] = (
    " junior ", " jr ", " graduate ", " fresher ", " entry level ", " associate ",
)


PRESETS: tuple[Preset, ...] = (
    Preset(
        key="support",
        name="Support & Operations",
        tagline="Keep production running, own incidents, unblock customers.",
        examples=(
            "Technical Support Engineer", "Cloud Support Engineer",
            "Production Support Engineer", "NOC Engineer",
        ),
        title_include=(
            "technical support", "support engineer", "customer support engineer",
            "cloud support", "product support", "application support",
            "production support", "operations engineer", "operations analyst",
            "noc", "network operations", "incident", "problem management",
            "monitoring engineer", "technical account",
        ),
        # The whole point of this track for most people is getting *out* of L1
        # ticket work. A high score on a job you are escaping is a false positive.
        title_exclude=(
            " service desk ", " servicedesk ", " help desk ", " helpdesk ",
            " desktop support ", " deskside ", " field support ", " field engineer ",
            " field technician ", " voice process ", " bpo ", " tele ",
            " customer care ", " customer service associate ", " chat support ",
        ),
        signals=(
            "incident", "itil", "servicenow", "sla", "escalation", "ticket",
            "support", "operations", "monitoring", "troubleshooting", "noc",
        ),
    ),
    Preset(
        key="sre",
        name="SRE, Cloud & DevOps",
        tagline="Reliability, automation, CI/CD and the infrastructure underneath.",
        examples=(
            "Site Reliability Engineer", "Cloud Engineer",
            "DevOps Engineer", "Platform Engineer",
        ),
        title_include=(
            "site reliability", "sre", "devops", "dev ops", "platform engineer",
            "infrastructure engineer", "cloud engineer", "cloud operations",
            "cloud infrastructure", "systems engineer", "linux engineer",
            "linux administrator", "kubernetes", "observability",
            "build and release", "release engineer", "automation engineer",
            "reliability engineer",
        ),
        signals=(
            "linux", "aws", "azure", "gcp", "kubernetes", "docker", "terraform",
            "ansible", "jenkins", "ci cd", "prometheus", "grafana", "bash",
            "shell", "devops", "sre", "cloud",
        ),
    ),
    Preset(
        key="software",
        name="Software Development",
        tagline="Write and ship the product itself — backend, frontend or full stack.",
        examples=(
            "Software Engineer", "Backend Engineer",
            "Full Stack Developer", "Frontend Engineer",
        ),
        title_include=(
            "software engineer", "software developer", "backend", "back end",
            "frontend", "front end", "full stack", "fullstack",
            "application developer", "web developer", "python developer",
            "java developer", "golang", "node js developer", "react developer",
            "api engineer", "member of technical staff",
        ),
        signals=(
            "python", "java", "javascript", "typescript", "react", "node",
            "django", "flask", "spring", "golang", "api", "rest", "sql",
            "git", "microservices",
        ),
    ),
    Preset(
        key="data",
        name="Data & Analytics",
        tagline="Pipelines, dashboards and the numbers the business runs on.",
        examples=(
            "Data Analyst", "Data Engineer",
            "Business Intelligence Analyst", "Analytics Engineer",
        ),
        title_include=(
            "data analyst", "data engineer", "analytics engineer",
            "business intelligence", "bi developer", "bi analyst",
            "data scientist", "machine learning engineer", "ml engineer",
            "reporting analyst", "data operations", "etl developer",
            "database administrator", "dba",
        ),
        signals=(
            "sql", "python", "excel", "power bi", "tableau", "looker", "etl",
            "airflow", "spark", "snowflake", "pandas", "dashboard", "reporting",
            "analytics", "data",
        ),
    ),
    Preset(
        key="qa",
        name="QA & Automation Testing",
        tagline="Find the defects before customers do, and automate the finding.",
        examples=(
            "QA Engineer", "SDET",
            "Automation Test Engineer", "Quality Engineer",
        ),
        title_include=(
            "qa engineer", "quality assurance", "quality engineer", "sdet",
            "test engineer", "automation test", "test automation",
            "software tester", "performance test", "qa analyst",
        ),
        signals=(
            "selenium", "cypress", "playwright", "pytest", "junit", "testng",
            "jmeter", "postman", "test cases", "regression", "qa", "testing",
            "automation",
        ),
    ),
    Preset(
        key="security",
        name="Cybersecurity",
        tagline="SOC monitoring, incident response and hardening.",
        examples=(
            "Security Analyst", "SOC Analyst",
            "Security Engineer", "IAM Engineer",
        ),
        title_include=(
            "security analyst", "security engineer", "soc analyst",
            "information security", "cyber security", "cybersecurity",
            "threat", "vulnerability", "penetration test", "iam engineer",
            "identity and access", "grc analyst", "security operations",
        ),
        signals=(
            "siem", "splunk", "qualys", "nessus", "firewall", "soc", "security",
            "iso 27001", "vapt", "incident response", "phishing", "endpoint",
        ),
    ),
    Preset(
        key="network",
        name="Networking & Telecom",
        tagline="Routing, switching, and the links everything else depends on.",
        examples=(
            "Network Engineer", "Network Operations Engineer",
            "Network Security Engineer", "RF / Transmission Engineer",
        ),
        title_include=(
            "network engineer", "network operations", "network administrator",
            "network support", "noc engineer", "network security",
            "telecom engineer", "transmission engineer", "rf engineer",
            "core network", "voice engineer", "wireless engineer",
        ),
        signals=(
            "cisco", "ccna", "routing", "switching", "bgp", "ospf", "tcp ip",
            "vpn", "lan", "wan", "network", "telecom", "sdwan", "firewall",
        ),
    ),
    Preset(
        key="itops",
        name="IT & Systems Administration",
        tagline="Windows, endpoints, identity and the internal IT estate.",
        examples=(
            "System Administrator", "IT Engineer",
            "Windows Administrator", "Endpoint Engineer",
        ),
        title_include=(
            "system administrator", "systems administrator", "sysadmin",
            "it engineer", "it administrator", "it operations",
            "windows administrator", "active directory", "vmware",
            "virtualization", "endpoint engineer", "intune", "office 365",
            "m365", "storage administrator", "backup administrator",
        ),
        signals=(
            "windows server", "active directory", "vmware", "hyper v", "intune",
            "office 365", "m365", "sccm", "powershell", "veeam", "storage",
            "backup", "sysadmin",
        ),
    ),
)

BY_KEY: dict[str, Preset] = {p.key: p for p in PRESETS}


# --------------------------------------------------------------------------- #
# locations
# --------------------------------------------------------------------------- #

# Grouped because a city has several names on job boards and a user should not
# have to know which one a given company uses. Picking "Bengaluru" has to match
# "Bangalore", and picking "Delhi NCR" has to match Gurgaon, Gurugram and Noida —
# they are one commute market with four names.
CITY_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Bengaluru", ("bengaluru", "bangalore", "bangaluru")),
    ("Hyderabad", ("hyderabad", "secunderabad", "telangana")),
    ("Pune", ("pune", "pimpri", "hinjewadi")),
    ("Delhi NCR", ("delhi", "new delhi", "gurgaon", "gurugram", "noida", "faridabad", "ghaziabad")),
    ("Mumbai", ("mumbai", "navi mumbai", "thane", "bombay")),
    ("Chennai", ("chennai", "madras", "tamil nadu")),
    ("Kolkata", ("kolkata", "calcutta")),
    ("Ahmedabad", ("ahmedabad", "gandhinagar", "gujarat")),
    ("Kochi", ("kochi", "cochin", "trivandrum", "thiruvananthapuram", "kerala")),
    ("Coimbatore", ("coimbatore",)),
    ("Indore", ("indore", "bhopal")),
    ("Jaipur", ("jaipur",)),
    ("Chandigarh", ("chandigarh", "mohali", "panchkula")),
    ("Bhubaneswar", ("bhubaneswar",)),
    ("Anywhere in India", ("india",)),
)

CITY_BY_LABEL: dict[str, tuple[str, ...]] = dict(CITY_GROUPS)
DEFAULT_CITIES: tuple[str, ...] = (
    "Bengaluru", "Hyderabad", "Pune", "Delhi NCR", "Mumbai", "Chennai",
    "Anywhere in India",
)


# --------------------------------------------------------------------------- #
# building targets
# --------------------------------------------------------------------------- #

MIN_DESCRIPTION_CHARS = 300


def _pad(terms: tuple[str, ...] | list[str]) -> list[str]:
    """Ensure every term is space-padded so it behaves like a whole word.

    Include terms are left unpadded on purpose: "site reliability" should match
    "Site Reliability Engineer II" and padding the tail would break that. Only
    exclusions need the boundary, because a false exclusion is invisible — the
    posting just never appears.
    """
    out = []
    for t in terms:
        t = t.strip().lower()
        if not t:
            continue
        out.append(t if t.startswith(" ") and t.endswith(" ") else f" {t} ")
    return sorted(set(out))


def pad_exclusions(terms: list[str]) -> list[str]:
    """Public wrapper for `_pad`, for hand-edited exclusion lists coming from the UI.

    A user typing "sales" means the word, not the letters, and would otherwise lose
    every Salesforce posting without ever knowing why.
    """
    return _pad(terms)


def seniority_exclusions(years: float) -> list[str]:
    """Which end of the seniority range is noise, given how much experience you have.

    Under 5 years, senior/lead/principal postings reject on years alone — five such
    postings were scored during development and every one came back weak for exactly
    that reason, at real cost. Free string matching already knew.

    Over 8 years, the opposite: junior and graduate postings are the waste.
    Between 5 and 8 nothing is excluded, because that band genuinely straddles both.
    """
    if years and years < 5:
        return list(SENIOR_TITLES)
    if years >= 8:
        return list(JUNIOR_TITLES)
    return []


def targets_for(
    preset_keys: list[str],
    *,
    years: float = 0.0,
    cities: list[str] | None = None,
    remote_ok: bool = True,
    min_salary_lpa: float = 0.0,
    current_role: str = "",
    avoid: list[str] | None = None,
    keep_constraints: dict | None = None,
) -> dict:
    """Turn a few UI choices into a complete, valid targets config.

    Everything the prefilter reads is produced here, so a user never has to type a
    match string to get a working feed. The raw lists stay editable afterwards —
    presets are a starting point, not a cage.
    """
    chosen = [BY_KEY[k] for k in preset_keys if k in BY_KEY]
    if not chosen:
        raise ValueError("Pick at least one kind of role")

    include: list[str] = []
    for p in chosen:
        for term in p.title_include:
            if term not in include:
                include.append(term)

    exclude = set(NOISE_EXCLUDE) | set(seniority_exclusions(years))
    for p in chosen:
        exclude |= set(_pad(p.title_exclude))
    # A preset's own exclusions are dropped when another chosen preset wants those
    # titles. Someone picking both Support and IT Ops is not trying to rule out
    # help desk; someone picking only Support usually is.
    include_norm = " ".join(include)
    exclude = {e for e in exclude if e.strip() not in include_norm}
    exclude |= set(_pad(avoid or []))

    labels = list(cities or DEFAULT_CITIES)
    locations: list[str] = []
    for label in labels:
        for alias in CITY_BY_LABEL.get(label, (label.lower(),)):
            if alias not in locations:
                locations.append(alias)

    constraints = dict(keep_constraints or {})
    if years:
        constraints["years_of_experience"] = years
    if current_role:
        constraints["current_role"] = current_role
    if min_salary_lpa:
        constraints["minimum_salary_inr_lpa"] = min_salary_lpa
    constraints.setdefault(
        "work_authorisation", "Based in India, no sponsorship required"
    )

    return {
        # Kept so the UI can show which cards are selected without guessing from
        # the match strings, and so a later edit does not lose the choice.
        "preset_keys": [p.key for p in chosen],
        "cities": labels,
        "title_include": include,
        "title_exclude": sorted(exclude),
        "locations": locations,
        "remote_ok": remote_ok,
        "remote_exclude_regions": list(REMOTE_EXCLUDE_REGIONS),
        "min_description_chars": MIN_DESCRIPTION_CHARS,
        "constraints": constraints,
    }


# --------------------------------------------------------------------------- #
# suggestion
# --------------------------------------------------------------------------- #


def suggest(graph: dict | None) -> list[str]:
    """Rank presets against a skill graph so the first screen is pre-filled.

    Deliberately keyword counting rather than an LLM call: it runs in microseconds
    on every page load, costs nothing, and the user can override it with one click.
    Spending a paid run to guess something the user is about to confirm anyway
    would be the wrong trade.
    """
    if not graph:
        return []

    haystack_parts = [
        str(graph.get("headline") or ""),
        str(graph.get("summary") or ""),
    ]
    for skill in graph.get("skills") or []:
        if isinstance(skill, dict):
            # Level 3+ skills weigh more: a skill someone actually works in says
            # more about the next job than one they listed once.
            weight = 2 if int(skill.get("level") or 0) >= 3 else 1
            haystack_parts += [str(skill.get("name") or "")] * weight
    for ach in graph.get("achievements") or []:
        if isinstance(ach, dict):
            haystack_parts.append(str(ach.get("text") or ach.get("description") or ""))
    for role in graph.get("roles") or graph.get("experience") or []:
        if isinstance(role, dict):
            haystack_parts.append(str(role.get("title") or ""))

    hay = " ".join(haystack_parts).lower().replace("/", " ").replace("-", " ")

    scored = []
    for p in PRESETS:
        hits = sum(1 for sig in p.signals if sig in hay)
        if hits:
            scored.append((hits, p.key))
    scored.sort(reverse=True)

    if not scored:
        return []
    # Anything within one hit of the best is worth offering; beyond two presets the
    # feed gets muddy, which is the problem we are solving.
    best = scored[0][0]
    return [key for hits, key in scored[:2] if hits >= max(1, best - 1)]


def catalogue() -> list[dict]:
    return [
        {
            "key": p.key,
            "name": p.name,
            "tagline": p.tagline,
            "examples": list(p.examples),
        }
        for p in PRESETS
    ]


def city_catalogue() -> list[str]:
    return [label for label, _ in CITY_GROUPS]
