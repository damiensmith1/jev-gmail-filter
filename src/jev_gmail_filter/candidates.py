"""Candidate values for topic fields, found in code so Jev only has to pick.

Jev selects a field's value from these (plus "none of these"); it never
generates one. A value that isn't extracted can't be chosen, so this errs
towards recall: several plausible options are fine, a missing one isn't.

Field kinds understood here: `org` (companies, shops, organisations),
`title` (job titles) and `email`. Every field, whatever its kind, also
gets the values already stored on that topic's items.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .mail import Email

MAX_PER_FIELD = 15

# Mail providers and hiring / delivery platforms: their name isn't the
# organisation the email is about.
PROVIDERS = {
    "gmail",
    "googlemail",
    "outlook",
    "hotmail",
    "live",
    "yahoo",
    "icloud",
    "me",
    "aol",
    "proton",
    "protonmail",
    "fastmail",
    "gmx",
    "zoho",
}
PLATFORMS = {
    "greenhouse",
    "greenhouse-mail",
    "lever",
    "hire",
    "workday",
    "myworkday",
    "myworkdayjobs",
    "ashbyhq",
    "ashby",
    "smartrecruiters",
    "icims",
    "jobvite",
    "bamboohr",
    "workable",
    "linkedin",
    "indeed",
    "glassdoor",
    "ziprecruiter",
    "wellfound",
    "angellist",
    "breezy",
    "recruitee",
    "teamtailor",
    "successfactors",
    "taleo",
    "oraclecloud",
    "jazzhr",
    "sendgrid",
    "mailchimp",
    "mailgun",
    "amazonses",
    "sparkpost",
    "postmarkapp",
    "hubspot",
    "notifications",
    "email",
    "mail",
    "e",
    "em",
    "news",
}
GENERIC_LOCAL = re.compile(
    r"^(no-?reply|do-?not-?reply|notifications?|jobs?|careers?|recruit(ing|ment)?|talent|"
    r"hr|hiring|info|hello|team|support|mail|admin|updates?|news|alerts?)$"
)
SENDER_SUFFIX = re.compile(
    r"\s*(\bvia\b.*|[-|@].*|\b(careers?|recruiting|recruitment|talent( acquisition)?|"
    r"hiring team|hr|team|jobs|people( team)?|notifications?)\b.*)$",
    re.IGNORECASE,
)
# "at Acme", "to Acme Robotics", "from Initech": capitalised runs after a preposition.
AFTER_PREP = re.compile(
    r"\b(?:at|to|from|with|join|joining|by)[ \t]+"
    r"((?:[A-Z][\w&'’-]*|&)(?:[ \t]+(?:[A-Z][\w&'’-]*|&|of|and)){0,3})"
)
IS_HIRING = re.compile(r"\b((?:[A-Z][\w&'’-]*[ \t]+){0,3}[A-Z][\w&'’-]*)[ \t]+(?:is hiring|team)\b")
ROLE_WORDS = (
    "Engineer|Developer|Programmer|Manager|Designer|Analyst|Scientist|Researcher|Intern|"
    "Lead|Director|Specialist|Consultant|Architect|Administrator|Coordinator|Associate|"
    "Representative|Officer|Recruiter|Technician|Writer|Editor|Accountant|Assistant|"
    "Strategist|Owner|Partner|Head of [A-Z][\\w]+|VP[ \\w]*|Advocate|Operator|Tester"
)
TITLE = re.compile(
    r"\b((?:(?:Senior|Sr\.?|Junior|Jr\.?|Staff|Principal|Lead|Chief)[ \t]+)?"
    r"(?:[A-Z][\w+#/.&-]*[ \t]+){0,3}(?:" + ROLE_WORDS + r")(?:[ \t]+(?:I{1,3}|IV|[1-5]))?)\b"
)
STOP = {
    "The",
    "A",
    "An",
    "Our",
    "We",
    "Your",
    "You",
    "I",
    "It",
    "This",
    "That",
    "Thank",
    "Thanks",
    "Hi",
    "Hello",
    "Dear",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
    "Today",
    "Tomorrow",
    "Please",
    "If",
    "Best",
    "Regards",
    "Sincerely",
    "Unsubscribe",
    "Click",
    "View",
    "Apply",
    "Re",
    "Fwd",
    "LinkedIn",
    "Indeed",
    "Glassdoor",
    "Google",
    "Gmail",
    "Zoom",
    "Teams",
}


def extract(email: Email, kind: str | None, known: Iterable[str] = ()) -> list[str]:
    """Candidates for one field: known values first, then what the email suggests."""
    found: list[str] = [k for k in known if k]
    if kind == "org":
        found += orgs(email)
    elif kind == "title":
        found += titles(email)
    elif kind == "email":
        found += re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", f"{email.sender} {email.body}")
    return _dedupe(found)[:MAX_PER_FIELD]


def orgs(email: Email) -> list[str]:
    out: list[str] = []
    name = SENDER_SUFFIX.sub("", email.sender_name).strip(" ,.-")
    first = name.split()[0].casefold() if name else ""
    if name and first not in PLATFORMS and not _looks_like_person(name, email):
        out.append(name)
    out += _from_address(email.sender_email)
    text = f"{email.subject}\n{email.body[:3000]}"
    for rx in (AFTER_PREP, IS_HIRING):
        for m in rx.finditer(text):
            out.append(_trim(m.group(1)))
    return [o for o in out if _plausible(o)]


def titles(email: Email) -> list[str]:
    text = f"{email.subject}\n{email.body[:3000]}"
    return [_trim(m.group(1)) for m in TITLE.finditer(text)]


def _from_address(address: str) -> list[str]:
    if "@" not in address:
        return []
    local, domain = address.split("@", 1)
    labels = [p for p in domain.lower().split(".") if p]
    if len(labels) < 2:
        return []
    registrable = labels[-2]
    subs = labels[:-2]
    out: list[str] = []
    if registrable in PLATFORMS or registrable in PROVIDERS:
        # acme.myworkday.com → Acme; careers-acme@greenhouse → the local part if it's a name
        out += [s for s in subs if s not in PLATFORMS and s not in ("www", "us", "eu", "mail")]
        head = re.split(r"[.+_-]", local)[0]
        if registrable in PLATFORMS and not GENERIC_LOCAL.match(head) and "alert" not in head:
            out.append(head)
    else:
        out.append(registrable)
    return [_titlecase(o) for o in out if len(o) > 1]


def _looks_like_person(name: str, email: Email) -> bool:
    """'Sam Lee' at initech.example is a person; 'Initech Careers' isn't."""
    words = name.split()
    local = email.sender_email.split("@")[0]
    return 1 < len(words) <= 3 and words[0].lower() in local.lower()


def _plausible(value: str) -> bool:
    return bool(value) and value not in STOP and len(value) <= 60 and not value.isdigit()


def _trim(value: str) -> str:
    words = value.strip(" \t.,:;!?'\"’").split()
    while words and words[-1] in STOP | {"of", "and", "&"}:
        words.pop()
    while words and words[0] in STOP:
        words.pop(0)
    return " ".join(words)


def _titlecase(value: str) -> str:
    return value if any(c.isupper() for c in value) else value.replace("-", " ").title()


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for v in values:
        key = v.casefold().strip()
        if key and key not in seen:
            seen.add(key)
            out.append(v.strip())
    return out
