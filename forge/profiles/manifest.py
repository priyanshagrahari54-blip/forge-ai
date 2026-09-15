"""Project profile manifests (A83): declarative, validated, no code loading.

A *project profile* is how a domain teaches Forge. A profile is pure data —
knowledge topics, build recipes, test recipes, boot recipes, constraints,
conventions — validated strictly and never imported or executed. This is the
mechanism that keeps domain knowledge out of Forge's core: ZEROOS ships as a
profile, and so can any future project.

Safety contract (mirrors :mod:`forge.plugins.manifest`):

* Validation is strict and raises ``ValueError`` with an honest message.
* Recipe commands are argv *lists*. Nothing here ever runs a shell, and the
  argv validator rejects shell metacharacters so a profile cannot smuggle
  ``; rm -rf /`` into an engine that would otherwise pass it through.
* Every recipe must declare the evidence that makes it apply, so a build
  engine can say *why* it chose a recipe.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

FORMAT_ID = "forge-project-profile"
FORMAT_VERSION = 1

NAME = re.compile(r"^[a-z][a-z0-9._-]{2,48}$")
#: Evidence is a repository-relative glob-ish path or a filename.
EVIDENCE = re.compile(r"^[A-Za-z0-9._*/+-]{1,200}$")
#: Argv tokens: printable, no shell metacharacters, no leading dash-dash abuse
#: of the form that could smuggle options into an interpreter (``--`` alone is
#: fine; it is a real separator).
ARGV_TOKEN = re.compile(r"^[A-Za-z0-9._/=,@:+\-{}\[\] ]{1,400}$")
SHELL_METACHARACTERS = set(";&|`$<>\\\n\r")

MAX_NAME = 48
MAX_DISPLAY_NAME = 120
MAX_DESCRIPTION = 2000
MAX_LIST = 64
MAX_TEXT = 4000

#: Recipe kinds a profile may declare.
RECIPE_KINDS = ("build", "test", "boot", "analyze", "package", "bench")


def _clean_version(value: Any) -> str:
    version = str(value or "").strip()
    parts = version.split(".")
    if len(parts) != 3 or not all(part.isdigit() and part for part in parts):
        raise ValueError("profile versions must look like 1.2.3")
    return version


def _string_list(value: Any, name: str, *, limit: int = MAX_LIST,
                 max_len: int = 400, pattern: "Optional[re.Pattern]" = None
                 ) -> Tuple[str, ...]:
    """Validate a list of strings; returns a de-duplicated tuple."""
    if value in (None, ""):
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ValueError("%s must be a list of strings" % name)
    if len(value) > limit:
        raise ValueError("%s has too many entries (%d max)" % (name, limit))
    out: List[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise ValueError("%s must contain only strings" % name)
        text = entry.strip()
        if not text:
            continue
        if len(text) > max_len:
            raise ValueError("%s entry exceeds %d characters" % (name, max_len))
        if pattern is not None and not pattern.match(text):
            raise ValueError(
                "%s entry %r does not match the allowed pattern" % (name, text))
        if text not in out:
            out.append(text)
    return tuple(out)


def validate_argv(argv: Any, name: str) -> Tuple[str, ...]:
    """Validate an argv list: non-empty, no shell metacharacters."""
    if not isinstance(argv, (list, tuple)) or not argv:
        raise ValueError("%s must be a non-empty list of arguments" % name)
    if len(argv) > MAX_LIST:
        raise ValueError("%s has too many arguments (%d max)" % (name, MAX_LIST))
    cleaned: List[str] = []
    for entry in argv:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError("%s arguments must be non-empty strings" % name)
        token = entry.strip()
        if any(char in SHELL_METACHARACTERS for char in token):
            raise ValueError(
                "%s argument %r contains shell metacharacters; Forge never "
                "runs a shell, so recipes must be plain argv" % (name, token))
        if not ARGV_TOKEN.match(token):
            raise ValueError("%s argument %r is not allowed" % (name, token))
        cleaned.append(token)
    return tuple(cleaned)


@dataclass(frozen=True)
class Recipe:
    """One executable recipe: what to run, and why it applies."""

    name: str
    kind: str
    argv: Tuple[str, ...]
    #: Repository-relative paths whose presence makes this recipe applicable.
    evidence: Tuple[str, ...] = ()
    #: Free-text explanation shown to the user and stored with results.
    description: str = ""
    timeout_seconds: float = 600.0
    #: Working directory relative to the repository root ("" == root).
    cwd: str = ""
    #: Result parser hint understood by the matching engine. ``auto`` (the
    #: default) lets the engine recognise the tool from its own output, which
    #: is what a recipe author almost always wants; naming a parser pins it.
    parser: str = "auto"
    #: Extra declarative options (string -> string/int/bool only).
    options: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "argv": list(self.argv),
            "evidence": list(self.evidence),
            "description": self.description,
            "timeout_seconds": self.timeout_seconds,
            "cwd": self.cwd,
            "parser": self.parser,
            "options": dict(self.options),
        }


@dataclass(frozen=True)
class KnowledgeTopic:
    """One unit of domain knowledge a profile contributes."""

    topic: str
    summary: str
    references: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {"topic": self.topic, "summary": self.summary,
                "references": list(self.references)}


def _clean_recipe(payload: Any, name: str, kind: str) -> Recipe:
    if not isinstance(payload, dict):
        raise ValueError("%s recipe must be an object" % kind)
    recipe_name = str(payload.get("name", "")).strip().lower()
    if not recipe_name or len(recipe_name) > MAX_NAME:
        raise ValueError("%s recipes need a name of 1-%d chars"
                         % (kind, MAX_NAME))
    argv = validate_argv(payload.get("argv") or payload.get("command"),
                         "%s recipe %r" % (kind, recipe_name))
    timeout = payload.get("timeout_seconds", 600.0)
    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError):
        raise ValueError(
            "%s recipe %r timeout_seconds must be a number"
            % (kind, recipe_name)) from None
    if not 1.0 <= timeout_value <= 7200.0:
        raise ValueError(
            "%s recipe %r timeout must be within [1, 7200] seconds"
            % (kind, recipe_name))
    cwd = str(payload.get("cwd", "") or "").strip()
    if cwd:
        if cwd.startswith("/") or ".." in re.split(r"[\\/]", cwd):
            raise ValueError(
                "%s recipe %r cwd must stay inside the repository"
                % (kind, recipe_name))
    options = payload.get("options") or {}
    if not isinstance(options, dict):
        raise ValueError("%s recipe %r options must be an object"
                         % (kind, recipe_name))
    for key, value in options.items():
        if not isinstance(key, str):
            raise ValueError("%s recipe %r option keys must be strings"
                             % (kind, recipe_name))
        if not isinstance(value, (str, int, float, bool)):
            raise ValueError(
                "%s recipe %r option %r must be a scalar"
                % (kind, recipe_name, key))
    return Recipe(
        name=recipe_name,
        kind=kind,
        argv=argv,
        evidence=_string_list(payload.get("evidence"),
                              "%s recipe %r evidence" % (kind, recipe_name),
                              pattern=EVIDENCE),
        description=str(payload.get("description", "") or "").strip()[:MAX_TEXT],
        timeout_seconds=timeout_value,
        cwd=cwd,
        parser=str(payload.get("parser", "auto") or "auto").strip()[:64]
        or "auto",
        options=dict(options),
    )


def _clean_recipes(payload: Any, kind: str) -> Tuple[Recipe, ...]:
    if payload in (None, "", [], {}):
        return ()
    if isinstance(payload, dict):
        items: Sequence[Any] = [
            dict(item, name=item.get("name") or key)
            if isinstance(item, dict) else item
            for key, item in payload.items()
        ]
    elif isinstance(payload, (list, tuple)):
        items = list(payload)
    else:
        raise ValueError("%s recipes must be a list or object" % kind)
    if len(items) > MAX_LIST:
        raise ValueError("too many %s recipes (%d max)" % (kind, MAX_LIST))
    return tuple(_clean_recipe(item, "%s recipe %d" % (kind, index), kind)
                 for index, item in enumerate(items))


@dataclass(frozen=True)
class ProfileManifest:
    """A validated project profile declaration."""

    name: str
    version: str
    display_name: str = ""
    description: str = ""
    #: What kinds of project this profile describes.
    project_types: Tuple[str, ...] = ()
    languages: Tuple[str, ...] = ()
    knowledge: Tuple[KnowledgeTopic, ...] = ()
    recipes: Tuple[Recipe, ...] = ()
    constraints: Tuple[str, ...] = ()
    conventions: Tuple[str, ...] = ()
    #: Capabilities this profile claims to help with (free vocabulary).
    capabilities: Tuple[str, ...] = ()
    #: Where the profile came from: ``builtin``, ``project``, ``user``.
    origin: str = "builtin"
    #: File the profile was loaded from, when loaded from disk.
    source: str = ""

    def recipes_for(self, kind: str) -> Tuple[Recipe, ...]:
        return tuple(recipe for recipe in self.recipes if recipe.kind == kind)

    def topic(self, name: str) -> Optional[KnowledgeTopic]:
        wanted = (name or "").strip().lower()
        for item in self.knowledge:
            if item.topic == wanted:
                return item
        return None

    def search_knowledge(self, query: str) -> List[KnowledgeTopic]:
        """Return topics whose text contains any query term, best first.

        Deterministic and dependency-free: term overlap score, then topic
        name. No embeddings, no model calls — the same posture as
        :mod:`forge.memory.relevance`.
        """
        terms = {term for term in re.split(r"\W+", (query or "").lower())
                 if len(term) > 2}
        if not terms:
            return []
        scored: List[Tuple[int, str, KnowledgeTopic]] = []
        for item in self.knowledge:
            haystack = " ".join(
                (item.topic, item.summary) + item.references).lower()
            score = sum(1 for term in terms if term in haystack)
            if score:
                scored.append((-score, item.topic, item))
        scored.sort(key=lambda entry: (entry[0], entry[1]))
        return [entry[2] for entry in scored]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "format": FORMAT_ID,
            "format_version": FORMAT_VERSION,
            "name": self.name,
            "version": self.version,
            "display_name": self.display_name,
            "description": self.description,
            "project_types": list(self.project_types),
            "languages": list(self.languages),
            "knowledge": [item.to_dict() for item in self.knowledge],
            "recipes": [recipe.to_dict() for recipe in self.recipes],
            "constraints": list(self.constraints),
            "conventions": list(self.conventions),
            "capabilities": list(self.capabilities),
            "origin": self.origin,
            "source": self.source,
        }


def _clean_knowledge(payload: Any) -> Tuple[KnowledgeTopic, ...]:
    if payload in (None, "", [], {}):
        return ()
    if isinstance(payload, dict):
        items: Sequence[Any] = [
            {"topic": key, **(value if isinstance(value, dict)
                             else {"summary": str(value)})}
            for key, value in payload.items()
        ]
    elif isinstance(payload, (list, tuple)):
        items = list(payload)
    else:
        raise ValueError("knowledge must be a list or object")
    if len(items) > MAX_LIST:
        raise ValueError("too many knowledge topics (%d max)" % MAX_LIST)
    out: List[KnowledgeTopic] = []
    for entry in items:
        if not isinstance(entry, dict):
            raise ValueError("each knowledge topic must be an object")
        topic = str(entry.get("topic", "")).strip().lower()[:MAX_NAME]
        summary = str(entry.get("summary", "") or "").strip()[:MAX_TEXT]
        if not topic or not summary:
            raise ValueError("knowledge topics need a topic and a summary")
        out.append(KnowledgeTopic(
            topic=topic,
            summary=summary,
            references=_string_list(entry.get("references"),
                                    "knowledge %r references" % topic),
        ))
    return tuple(out)


def validate_profile(payload: Any, *, origin: str = "builtin",
                     source: str = "") -> ProfileManifest:
    """Validate a project-profile manifest and return the cleaned record.

    Raises ``ValueError`` on any violation. Never imports or executes
    anything.
    """
    if not isinstance(payload, dict):
        raise ValueError("a project profile must be a mapping")
    if payload.get("format") not in (None, FORMAT_ID):
        raise ValueError("not a %s" % FORMAT_ID)
    version_value = payload.get("format_version", FORMAT_VERSION)
    if version_value not in (None, FORMAT_VERSION):
        raise ValueError(
            "unsupported profile format version %r" % (version_value,))
    name = str(payload.get("name", "")).strip().lower()
    if not NAME.match(name):
        raise ValueError("profile names must match [a-z][a-z0-9._-]{2,48}")
    recipes: List[Recipe] = []
    for kind in RECIPE_KINDS:
        recipes.extend(_clean_recipes(payload.get(kind + "_recipes"), kind))
    seen: set = set()
    for recipe in recipes:
        key = (recipe.kind, recipe.name)
        if key in seen:
            raise ValueError("duplicate recipe %s/%s" % key)
        seen.add(key)
    origin_value = str(origin or "builtin").strip().lower()
    if origin_value not in ("builtin", "project", "user"):
        raise ValueError("profile origin must be builtin, project, or user")
    return ProfileManifest(
        name=name,
        version=_clean_version(payload.get("version", "1.0.0")),
        display_name=str(payload.get("display_name", "")
                         or "").strip()[:MAX_DISPLAY_NAME],
        description=str(payload.get("description", "")
                        or "").strip()[:MAX_DESCRIPTION],
        project_types=_string_list(payload.get("project_types"),
                                   "project_types"),
        languages=_string_list(payload.get("languages"), "languages"),
        knowledge=_clean_knowledge(payload.get("knowledge")),
        recipes=tuple(recipes),
        constraints=_string_list(payload.get("constraints"), "constraints",
                                 max_len=MAX_TEXT),
        conventions=_string_list(payload.get("conventions"), "conventions",
                                 max_len=MAX_TEXT),
        capabilities=_string_list(payload.get("capabilities"),
                                  "capabilities"),
        origin=origin_value,
        source=source,
    )
