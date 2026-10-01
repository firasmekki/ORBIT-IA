"""ChartSpec: the one data shape generate_chart produces and
frontend/src/components/ChartBlock.tsx renders, covering all 4 supported
chart types (line/bar/pie/scatter) - see app/models/chat.py's Message.chart
docstring.

Nothing in this module ever invents a number. It only validates/reshapes
data that already came from an ACL-checked tool (read_spreadsheet_data,
search_database) or that the user typed themselves in the current message
- see app/agent/orchestrator.py::_run_generate_chart for where those
sources are resolved (in that priority order), and app/mcp/server.py's
generate_chart tool for the final structural validation before a spec is
trusted.
"""

import re
from dataclasses import dataclass, field

from app.rag.normalize import normalize

CHART_TYPES = ("line", "bar", "pie", "scatter")


@dataclass(frozen=True)
class ValueField:
    field: str
    label: str
    unit: str | None = None

    def to_dict(self) -> dict:
        return {"field": self.field, "label": self.label, "unit": self.unit}


@dataclass(frozen=True)
class ChartSource:
    document_id: str | None
    title: str
    page: int | None = None
    section: str | None = None

    def to_dict(self) -> dict:
        return {"document_id": self.document_id, "title": self.title, "page": self.page, "section": self.section}


@dataclass(frozen=True)
class ChartSpec:
    chart_type: str
    title: str
    category_field: str | None
    category_label: str | None
    value_fields: list[ValueField]
    data: list[dict]
    sources: list[ChartSource] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "chart_type": self.chart_type,
            "title": self.title,
            "category_field": self.category_field,
            "category_label": self.category_label,
            "value_fields": [v.to_dict() for v in self.value_fields],
            "data": self.data,
            "sources": [s.to_dict() for s in self.sources],
        }


class ChartValidationError(ValueError):
    pass


def validate_chart_spec(
    *,
    chart_type: str,
    title: str,
    category_field: str | None,
    value_fields: list[dict],
    data: list[dict],
) -> None:
    """Structural validation only - never touches the actual numbers, just
    checks the shape makes sense for the requested chart_type. Raises
    ChartValidationError with a message safe to show the user directly."""
    if chart_type not in CHART_TYPES:
        raise ChartValidationError(f"type de graphique inconnu : « {chart_type} »")
    if not title.strip():
        raise ChartValidationError("titre du graphique manquant")
    if not data:
        raise ChartValidationError("aucune donnée disponible pour ce graphique")
    if not value_fields:
        raise ChartValidationError("aucune série de valeurs fournie")

    if chart_type == "pie" and len(value_fields) != 1:
        raise ChartValidationError("un graphique circulaire nécessite exactement une série de valeurs")
    if chart_type == "scatter":
        if len(value_fields) != 2:
            raise ChartValidationError("un nuage de points nécessite exactement deux séries (x et y)")
        if category_field is not None:
            raise ChartValidationError("un nuage de points ne doit pas avoir de champ de catégorie")
    if chart_type in ("bar", "line") and category_field is None:
        raise ChartValidationError(f"un graphique {chart_type} nécessite un champ de catégorie (axe des X)")

    value_field_names = [v["field"] for v in value_fields]
    for row in data:
        if not isinstance(row, dict):
            raise ChartValidationError("chaque ligne de données doit être un objet")
        if category_field is not None and category_field not in row:
            raise ChartValidationError(f"une ligne ne contient pas le champ de catégorie « {category_field} »")
        for vf in value_field_names:
            if vf in row and row[vf] is not None and not isinstance(row[vf], (int, float)):
                raise ChartValidationError(
                    f"valeur non numérique pour « {vf} » : {row[vf]!r} - une valeur inconnue doit être omise ou null, jamais un texte"
                )


def summarize_chart(spec_dict: dict) -> str:
    """Short, deterministic, data-only analysis line - never touches the
    LLM. Computed strictly from spec_dict["data"], which by the time this
    runs has already passed validate_chart_spec."""
    data = spec_dict["data"]
    value_fields = spec_dict["value_fields"]
    category_field = spec_dict["category_field"]
    if not value_fields or not data:
        return ""

    primary = value_fields[0]["field"]
    unit = value_fields[0].get("unit") or ""
    points = [(row.get(category_field) if category_field else i, row[primary]) for i, row in enumerate(data) if row.get(primary) is not None]
    if not points:
        return "Aucune valeur numérique disponible pour cette série."

    values = [v for _, v in points]
    max_label, max_value = max(points, key=lambda p: p[1])
    min_label, min_value = min(points, key=lambda p: p[1])
    unit_suffix = f" {unit}" if unit else ""

    if spec_dict["chart_type"] == "line" and len(points) > 1:
        first_label, first_value = points[0]
        last_label, last_value = points[-1]
        direction = "en hausse" if last_value > first_value else ("en baisse" if last_value < first_value else "stable")
        return (
            f"Évolution {direction} entre {first_label} ({first_value:,.0f}{unit_suffix}) et "
            f"{last_label} ({last_value:,.0f}{unit_suffix}). Maximum observé : {max_value:,.0f}{unit_suffix} ({max_label})."
        ).replace(",", " ")

    if len(values) == 1:
        return f"{max_label} : {max_value:,.0f}{unit_suffix}.".replace(",", " ")

    return (
        f"Valeur la plus élevée : {max_label} ({max_value:,.0f}{unit_suffix}). "
        f"Valeur la plus basse : {min_label} ({min_value:,.0f}{unit_suffix})."
    ).replace(",", " ")


_INLINE_ROW_RE = re.compile(r"([^\d:=,;\n]{2,40}?)\s*[:=]\s*(-?\d[\d\s.,]*|inconnu|nc|n/?a)\b", re.IGNORECASE)
_UNKNOWN_TOKENS = {"inconnu", "nc", "na", "n/a"}


def parse_inline_data(message: str) -> list[tuple[str, float | None]] | None:
    """Conservative extraction of "Label: Number" / "Label = Number" pairs
    explicitly typed in the user's message - e.g. "Janvier: 120000,
    Février: 135000, Mars: inconnu". Deliberately narrow: anything that
    doesn't match this exact shape is left alone rather than guessed at,
    per the no-hallucination rule - a message with no clean matches
    returns None, and the caller falls back to search_database instead of
    a partial/wrong parse. "inconnu"/"nc"/"n/a" become None (an explicit
    missing value), never 0.
    """
    matches = _INLINE_ROW_RE.findall(message)
    if len(matches) < 2:
        return None
    rows: list[tuple[str, float | None]] = []
    for label, raw_value in matches:
        label = label.strip(" \t\n\r.,;:!?»«\"'-")
        if not label:
            return None
        normalized = raw_value.strip().lower()
        if normalized in _UNKNOWN_TOKENS:
            rows.append((label, None))
            continue
        cleaned = raw_value.strip().replace(" ", "").replace(",", ".")
        # A value with more than one "." is ambiguous (thousand separator
        # vs decimal point) - reject rather than guess.
        if cleaned.count(".") > 1:
            return None
        try:
            value = float(cleaned)
        except ValueError:
            return None
        rows.append((label, value))
    return rows


# --- spreadsheet column resolution ------------------------------------------
#
# Grounded, never-guessed column selection for app/agent/orchestrator.py's
# _run_chart_from_spreadsheet: a column is only ever picked because its
# REAL name (as read from the actual .xlsx header row) is literally present
# in the user's message - never inferred from vocabulary or invented. This
# is what enforces "requested_columns ⊆ resolved_columns" - an explicit
# "colonne X" that doesn't match any real header is a hard error, not a
# silent fallback.

_PARENTHETICAL_RE = re.compile(r"\s*\([^)]*\)")
_UNIT_RE = re.compile(r"\(([^)]*)\)\s*$")
_COLONNE_MENTION_RE = re.compile(
    r"colonne\s+([A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9 '\-]*?)(?=\s*(?:,|\.|$|\bpour\b|\bet\b))",
    re.IGNORECASE,
)


def strip_parenthetical(header: str) -> str:
    """"Chiffre d'affaires (TND)" -> "Chiffre d'affaires" - lets a user say
    "CA 2025" and match a real header "CA 2025 (TND)" without repeating the
    unit, while the unit itself is preserved separately (see extract_unit)
    for display on the chart's axis/tooltip."""
    return _PARENTHETICAL_RE.sub("", header).strip()


def extract_unit(header: str) -> str | None:
    match = _UNIT_RE.search(header)
    return match.group(1).strip() or None if match else None


def _normalized_variants(header: str) -> list[str]:
    variants = [normalize(header)]
    stripped = strip_parenthetical(header)
    if stripped != header:
        variants.append(normalize(stripped))
    return [v for v in variants if v]


def _matches_any_header(normalized_name: str, headers: list[str]) -> bool:
    for header in headers:
        for variant in _normalized_variants(header):
            if normalized_name == variant or normalized_name in variant or variant in normalized_name:
                return True
    return False


def resolve_spreadsheet_columns(headers: list[str], rows: list[dict], message: str) -> tuple[str | None, list[str]]:
    """Resolves (category_field, value_fields) for a sheet that has already
    been read in full (see app/mcp/server.py's read_spreadsheet_data).

    A header is treated as "explicitly requested" only if its real name (or
    that name with a trailing unit stripped, e.g. "CA 2025" matching
    "CA 2025 (TND)") literally appears in the message. "axe x"/"axe y" near
    a mentioned header disambiguates category vs. value; absent that cue, a
    mentioned column that is empirically all-numeric across the sheet's own
    rows becomes a value field, a mentioned non-numeric column becomes the
    category. With zero columns mentioned at all, falls back to the sheet's
    own shape: first column = category (the universal spreadsheet
    convention), every other numeric column = a value field.

    Raises ChartValidationError - never fabricates a column - if an
    explicit "colonne X" doesn't match any real header, or if no value
    field can be resolved at all.
    """
    if not headers:
        raise ChartValidationError("la feuille ne contient aucune colonne")

    normalized_message = normalize(message)

    for raw_name in _COLONNE_MENTION_RE.findall(message):
        candidate = normalize(raw_name.strip())
        if candidate and not _matches_any_header(candidate, headers):
            raise ChartValidationError(
                f"colonne « {raw_name.strip()} » introuvable dans cette feuille. "
                f"Colonnes disponibles : {', '.join(headers)}"
            )

    def is_numeric(header: str) -> bool:
        values = [row[header] for row in rows if row.get(header) is not None]
        return bool(values) and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values)

    def is_mentioned(header: str) -> bool:
        return any(variant in normalized_message for variant in _normalized_variants(header))

    def axis_cue(header: str) -> str | None:
        for variant in _normalized_variants(header):
            start = 0
            while True:
                idx = normalized_message.find(variant, start)
                if idx == -1:
                    break
                window = normalized_message[idx : idx + len(variant) + 40]
                if "axe x" in window:
                    return "x"
                if "axe y" in window:
                    return "y"
                start = idx + 1
        return None

    mentioned = [h for h in headers if is_mentioned(h)]
    category: str | None = None
    values: list[str] = []

    if mentioned:
        for header in mentioned:
            axis = axis_cue(header)
            if axis == "x" and category is None:
                category = header
            elif axis == "y" and header not in values:
                values.append(header)

        for header in mentioned:
            if header == category or header in values:
                continue
            if is_numeric(header):
                values.append(header)
            elif category is None:
                category = header

        if category is None and headers[0] not in values:
            category = headers[0]
    else:
        category = headers[0]
        values = [h for h in headers[1:] if is_numeric(h)]

    if not values:
        raise ChartValidationError("aucune colonne de valeurs numériques identifiée dans cette feuille")

    return category, values
