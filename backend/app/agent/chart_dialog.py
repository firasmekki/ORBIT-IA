"""Conversational chart building: turns a chart request - complete or not -
into either a ready-to-render chart plan, or ONE short clarification
question with clickable options, keeping what the user already said across
turns.

Deterministic on purpose (same "Voie A" split as app/agent/intent.py): what
gets plotted is decided here, from the catalog of records the user is
actually allowed to see (search_database, already ACL-filtered), never by
the LLM. The LLM is only ever asked to map loose wording ("les dépenses
informatiques") onto one of the catalog's own ids (see
CHART_ANALYST_SYSTEM_PROMPT and app/agent/orchestrator.py), and its answer
is validated against the catalog - it cannot add a metric or a value.

Flow (one call per user turn, state carried in Message.reference_context):

    request -> match metrics in the catalog
      none, but a family matches ("le budget") -> "Quel budget : RH, ... ?"
      none, unknown subject ("chiffre d'affaires") -> ask for the values /
                                                      a file, list what exists
      nothing said at all ("une courbe")          -> "Que souhaitez-vous représenter ?"
      requested years absent                      -> say so, offer the real ones
      several metrics x several periods, bar asked -> "Quelle période ?"
      otherwise                                   -> ChartPlan (no question)
"""

import re
from collections import Counter
from dataclasses import dataclass, field

from app.agent.grounding import requested_years
from app.rag.normalize import normalize

CHART_DIALOG_KIND = "chart_clarification"
ALL_PERIODS_VALUE = "Toutes les périodes"
OTHER_VALUE = "Autre"

CHART_ANALYST_SYSTEM_PROMPT = """Tu es un assistant spécialisé dans l'analyse de données et la génération de \
graphiques pour l'entreprise Orbitia.

Règles :
- Tu ne dois jamais inventer de données ni de métrique.
- Tu dois utiliser le contexte de la conversation.
- Tu identifies : la métrique, la dimension, la période, les filtres, et le type de graphique si nécessaire.
- Tu ne peux choisir une métrique QUE parmi la liste fournie, par son identifiant exact, et seulement si elle \
désigne EXACTEMENT la même chose que la demande (un synonyme strict). Des indicateurs voisins ne sont pas \
interchangeables : chiffre d'affaires, résultat net, budget et masse salariale sont quatre indicateurs différents.
- Si aucune métrique de la liste ne correspond à la demande, réponds "none" : l'application demandera \
alors à l'utilisateur quelle donnée utiliser. Ne réponds jamais simplement qu'aucune donnée n'est disponible.
- Réponds uniquement avec l'objet JSON demandé, sans texte autour."""

# Words that say "make a chart" or are grammatical glue - never a metric.
_FILLER = set(
    """
    a au aux avec ce ces cet cette d de des du en entre et je j l la le les leur leurs ma me mes moi mon
    ne nous on ou par pas pour qu que qui sa se ses sur ta te tes toi ton tu un une vos votre vous y
    est sont etre avoir ai as avons voudrais veux voulons souhaite souhaiterais aimerais peux pourrais
    pourriez merci svp stp plait s il elle ils
    donne donner donnez montre montrer montrez fais faire faites affiche afficher affichez genere generer
    generez cree creer creez trace tracer dessine dessiner voir visualiser visualise visualisation
    obtenir avoir
    graphique graphiques graphe graphes diagramme diagrammes courbe courbes histogramme histogrammes
    barre barres baton batons camembert circulaire chart charts line bar pie lineaire lineaires
    evolution evolutions tendance tendances historique progression variation variations
    compare comparer comparez comparaison comparaisons comparatif comparative versus vs
    repartition proportion proportions part parts
    donnee donnees valeur valeurs chiffre chiffres data periode periodes annee annees an ans mois
    depuis jusqu jusque toute toutes tout tous dernier derniere derniers dernieres
    simple personnalise personnalisee nouveau nouvelle autre
    """.split()
)
_EVOLUTION_RE = re.compile(r"\b(evolution|tendance|historique|progression|au fil|dans le temps)\b")
_PROPORTION_RE = re.compile(r"\b(repartition|proportion|proportions|parts? de|camembert)\b")
_YEAR_TOKEN_RE = re.compile(r"^(19|20)\d{2}$")


@dataclass
class Metric:
    id: str
    label: str
    unit: str | None
    points: list[tuple[str, float]] = field(default_factory=list)  # (period, value), sorted by period

    @property
    def periods(self) -> list[str]:
        return [p for p, _ in self.points]

    def value_at(self, period: str) -> float | None:
        return next((v for p, v in self.points if p == period), None)


@dataclass
class Clarification:
    message: str
    options: list[dict]  # [{"label": shown on the button, "value": sent as the user's answer}]
    state: dict

    def to_dict(self) -> dict:
        return {
            "type": "clarification",
            "status": "need_more_information",
            "message": self.message,
            "questions": [o["label"] for o in self.options],
            "options": self.options,
        }


@dataclass
class ChartPlan:
    chart_type: str
    title: str
    category_field: str
    category_label: str
    value_fields: list[dict]
    data: list[dict]
    drilldown: dict
    note: str | None = None
    rationale: str = ""
    metrics: list[Metric] = field(default_factory=list)
    periods: list[str] = field(default_factory=list)


@dataclass
class KpiPlan:
    """What the data supports when it is a single value per metric: a value
    card (value, unit, period, source) - never a one-bar "chart" nor a
    one-point "curve"."""

    message: str
    items: list[dict]  # [{"metric_id", "label", "period", "value", "unit"}]
    rationale: str


# A time series needs at least this many distinct, comparable periods.
MIN_TREND_PERIODS = 2
_TREND_RE = re.compile(
    r"\b(courbes?|evolution|evolue|tendance|historique|progression|variations?|au fil|dans le temps|lineaire)\b"
)


def wants_trend(text: str, explicit_type: str | None) -> bool:
    """A curve / an evolution is asked: needs >= MIN_TREND_PERIODS periods."""
    return explicit_type == "line" or bool(_TREND_RE.search(normalize(text)))


def format_amount(value: float, unit: str | None) -> str:
    """310000.0, "€" -> "310 000 €" (French grouping, no invented decimals)."""
    number = f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"
    number = number.replace(",", " ").replace(".", ",")
    return f"{number} {unit}" if unit else number


# --- catalog ----------------------------------------------------------------------


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", normalize(text)).strip("_") or "donnee"


def build_catalog(records: list[dict], unit: str = "€") -> list[Metric]:
    """search_database rows ("Budget IT / Infrastructure 2025", 2025,
    310000) -> one Metric per series ("Budget IT / Infrastructure") with
    every (period, value) the caller is allowed to see."""
    metrics: dict[str, Metric] = {}
    for record in records:
        label = str(record.get("label") or "").strip()
        if not label or record.get("amount") is None:
            continue
        year = record.get("year")
        base = re.sub(rf"\s*\b{year}\b", "", label) if year is not None else label
        base = re.sub(r"\s{2,}", " ", base).strip(" -/") or label
        metric = metrics.setdefault(_slug(base), Metric(id=_slug(base), label=base, unit=unit))
        period = str(year) if year is not None else "—"
        if period not in metric.periods:
            metric.points.append((period, float(record["amount"])))
    for metric in metrics.values():
        metric.points.sort(key=lambda point: point[0])
    return sorted(metrics.values(), key=lambda m: m.label)


# --- understanding the request ---------------------------------------------------------


def _stem(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def _content_tokens(text: str) -> set[str]:
    return {
        _stem(w)
        for w in re.findall(r"[a-z0-9]+", normalize(text))
        if len(w) >= 2 and w not in _FILLER and not _YEAR_TOKEN_RE.match(w) and not w.isdigit()
    }


@dataclass
class MetricMatch:
    selected: list[Metric]
    candidates: list[Metric]  # matched only through a word they all share ("budget")
    unmatched: list[str]  # content words that name nothing in the catalog


def match_metrics(text: str, catalog: list[Metric]) -> MetricMatch:
    user = _content_tokens(text)
    tokens = {m.id: _content_tokens(m.label) for m in catalog}
    known = set().union(*tokens.values()) if tokens else set()
    frequency = Counter(t for ts in tokens.values() for t in ts)

    specific = {t for t in user if frequency.get(t) == 1}
    selected = [m for m in catalog if tokens[m.id] & specific]
    candidates: list[Metric] = []
    shared = {t for t in user if frequency.get(t, 0) > 1}
    if shared:
        family = [m for m in catalog if shared <= tokens[m.id]]
        # "les budgets" / "tous les budgets" means the whole family (on top
        # of anything named specifically: "les budgets et le résultat net");
        # "le budget" alone means one of them - ask which.
        raw = set(re.findall(r"[a-z0-9]+", normalize(text)))
        plural = any(f"{t}s" in raw for t in shared) or bool({"tous", "toutes", "ensemble"} & raw)
        if plural and family:
            selected = [m for m in catalog if m in selected or m in family]
        elif not selected:
            if len(family) == 1:
                selected = family
            else:
                candidates = family
    return MetricMatch(selected=selected, candidates=candidates, unmatched=sorted(user - known))


def infer_chart_type(explicit: str | None, text: str, n_metrics: int, n_periods: int) -> str:
    """Explicit type wins, except a line through a single point (nothing to
    draw) becomes a bar. Otherwise: proportions -> pie, a time series ->
    line, categories -> bar."""
    if explicit in ("bar", "pie", "scatter"):
        return explicit
    if explicit == "line":
        return "line" if n_periods > 1 else "bar"
    normalized = normalize(text)
    if _PROPORTION_RE.search(normalized) and n_periods == 1 and n_metrics > 1:
        return "pie"
    if n_periods > 1 and (_EVOLUTION_RE.search(normalized) or n_metrics >= 1):
        return "line"
    return "bar"


def has_chart_subject(text: str) -> bool:
    """Does a chart request name *what* to plot, beyond chart vocabulary?"""
    return bool(_content_tokens(text))


def is_chart_followup(message: str, state: dict | None) -> bool:
    """Is this message an answer to the pending clarification (a short
    reply, a button value, a period, values), rather than a new question?"""
    if not state or state.get("kind") != CHART_DIALOG_KIND:
        return False
    normalized = normalize(message).strip()
    option_values = {normalize(v) for v in state.get("option_values", [])}
    if normalized in option_values:
        return True
    if requested_years(message) or re.search(r"\d\s*[:=]\s*\d", message):
        return True
    return len(re.findall(r"\w+", normalized)) <= 8 and not normalized.endswith("?")


# --- planning --------------------------------------------------------------------------


def _join_fr(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " ou " + items[-1]


def _options(labels: list[str]) -> list[dict]:
    return [{"label": label, "value": label} for label in labels]


def _shared_family(metrics: list[Metric]) -> str | None:
    """"budget" for Budget RH / Budget Marketing / Budget IT."""
    first_words = {normalize(m.label).split()[0] for m in metrics if m.label.split()}
    if len(first_words) == 1:
        return metrics[0].label.split()[0].lower()
    return None


def _clarify(message: str, labels: list[str], state: dict, extra_options: list[dict] | None = None) -> Clarification:
    options = _options(labels) + (extra_options or [])
    return Clarification(
        message=message, options=options, state={**state, "option_values": [o["value"] for o in options]}
    )


def context_metrics(context: str, catalog: list[Metric]) -> list[Metric]:
    """Metrics the conversation itself talked about - the only ones worth
    offering when a chart request names nothing ("tu peux donner des
    courbes ?"). Never the whole catalog."""
    if not context.strip():
        return []
    match = match_metrics(context, catalog)
    return match.selected or match.candidates


def plan_chart(
    *,
    text: str,
    explicit_type: str | None,
    catalog: list[Metric],
    state: dict | None = None,
    subject: str | None = None,
    context: str = "",
) -> ChartPlan | KpiPlan | Clarification:
    """`text` is the whole request so far (original request + every answer
    given), so nothing the user already said is asked again. `context`:
    the recent conversation, used only to decide which metrics are worth
    offering for a vague request."""
    state = dict(state or {})
    match = match_metrics(text, catalog)
    if not match.selected and state.get("candidate_ids"):
        offered = [m for m in catalog if m.id in state["candidate_ids"]]
        narrowed = match_metrics(text, offered)
        if narrowed.selected:
            match = narrowed
    metrics = match.selected or [m for m in catalog if m.id in state.get("metric_ids", [])]
    years = requested_years(text)
    wants_all_periods = normalize(ALL_PERIODS_VALUE) in normalize(text)
    explicit_type = explicit_type or state.get("chart_type")
    base = {
        "kind": CHART_DIALOG_KIND,
        "chart_type": explicit_type,
        "request": state.get("request") or text,
        "answers": state.get("answers", []),
        "subject": subject or state.get("subject"),
        "context_metric_ids": state.get("context_metric_ids")
        or [m.id for m in context_metrics(context, catalog)],
    }

    if not catalog:
        return _clarify(
            "Je peux créer ce graphique, mais je n'ai pas de données chiffrées accessibles pour le construire. "
            "Donnez-moi les valeurs à représenter (par exemple « 2024 : 120000, 2025 : 135000 ») ou "
            "indiquez le fichier Excel et la feuille à utiliser.",
            [],
            {**base, "asked": "data"},
        )

    if not metrics:
        if match.candidates:
            family = _shared_family(match.candidates) or "donnée"
            labels = [m.label for m in match.candidates]
            return _clarify(
                f"Quel {family} souhaitez-vous analyser : {_join_fr(labels)} ?",
                labels,
                {**base, "asked": "metric", "candidate_ids": [m.id for m in match.candidates]},
            )
        # The user's own wording ("chiffre d'affaires") when it is what went
        # unmatched, otherwise the unmatched words themselves.
        what = subject if subject and _content_tokens(subject) & set(match.unmatched) else " ".join(match.unmatched)
        what = what.strip()
        said_other = normalize(OTHER_VALUE) in _content_tokens_raw(text)
        if what and match.unmatched and not said_other:
            # No list of unrelated metrics here: "consommation du site" has
            # nothing to do with "Budget RH". A close catalog metric, if
            # any, is offered by with_suggestion (and confirmed by the user).
            return _clarify(
                f"Je peux créer ce graphique, mais je n'ai pas encore les données de « {what} ». "
                "Pouvez-vous me fournir les valeurs (par exemple « 2024 : 120000, 2025 : 135000 ») ou "
                "m'indiquer le fichier Excel et la feuille où elles se trouvent ?",
                [],
                {**base, "asked": "data", "subject": what},
            )
        if said_other:
            return _clarify(
                "D'accord. Donnez-moi les valeurs à représenter (par exemple « 2024 : 120000, 2025 : 135000 ») "
                "ou indiquez le fichier Excel et la feuille à utiliser.",
                [],
                {**base, "asked": "data"},
            )
        # Nothing named: offer only what the conversation talked about -
        # and, for a curve, say which of those have a single period.
        relevant = [m for m in catalog if m.id in base["context_metric_ids"]]
        if not relevant:
            return _clarify(
                "Quelle donnée souhaitez-vous représenter, et sur quelle période ?",
                [],
                {**base, "asked": "metric"},
            )
        trend = wants_trend(text, explicit_type)
        plottable = [m for m in relevant if len(m.points) >= MIN_TREND_PERIODS] if trend else relevant
        single = [m for m in relevant if m not in plottable]
        message = "Quelle donnée de la conversation souhaitez-vous représenter ?"
        if single:
            message += " " + _single_period_sentence(single)
        if not plottable:
            message = (
                _single_period_sentence(single)
                + " Il me faut au moins une autre période comparable pour tracer une courbe. "
                "Je peux afficher la valeur disponible, ou utiliser des valeurs que vous me donnez "
                "(par exemple « 2024 : 120000, 2025 : 135000 »)."
            )
        return _clarify(
            message,
            [m.label for m in (plottable or relevant)],
            {**base, "asked": "metric", "candidate_ids": [m.id for m in (plottable or relevant)]},
            extra_options=[{"label": OTHER_VALUE, "value": OTHER_VALUE}],
        )

    base["metric_ids"] = [m.id for m in metrics]
    labels = [m.label for m in metrics]
    periods = sorted({p for m in metrics for p in m.periods})

    # Different units never share an axis (€ and %, € and heures...).
    units = {m.unit for m in metrics}
    if len(units) > 1:
        return _clarify(
            "Ces données n'ont pas la même unité ("
            + ", ".join(sorted(u or "sans unité" for u in units))
            + ") : elles ne peuvent pas être tracées ensemble. Laquelle souhaitez-vous afficher ?",
            labels,
            {**base, "asked": "metric", "candidate_ids": [m.id for m in metrics], "metric_ids": []},
        )

    if years and not wants_all_periods:
        wanted = [str(y) for y in years]
        missing = [y for y in wanted if y not in periods]
        if missing:
            found = ", ".join(periods)
            if len(wanted) > 1:
                message = (
                    f"Je peux générer le graphique, mais les documents accessibles ne contiennent pas "
                    f"suffisamment de données {'/'.join(wanted)} comparables. Années disponibles : {found}."
                )
            else:
                message = (
                    f"Je ne dispose pas de données {wanted[0]} pour {_join_fr(labels)} dans les documents "
                    f"accessibles. Années disponibles : {found}. Quelle période souhaitez-vous afficher ?"
                )
            return _clarify(message, periods, {**base, "asked": "period"})
        selected_periods = wanted
    else:
        selected_periods = periods
        if (
            not wants_all_periods
            and len(metrics) > 1
            and len(periods) > 1
            and explicit_type in ("bar", "pie")
        ):
            return _clarify(
                "Quelle période souhaitez-vous comparer ?",
                periods,
                {**base, "asked": "period"},
                extra_options=[{"label": ALL_PERIODS_VALUE, "value": ALL_PERIODS_VALUE}],
            )

    def periods_of(metric: Metric) -> list[str]:
        return [p for p in selected_periods if metric.value_at(p) is not None]

    if wants_trend(text, explicit_type):
        plottable = [m for m in metrics if len(periods_of(m)) >= MIN_TREND_PERIODS]
        if not plottable:
            return _kpi_plan(metrics, selected_periods, trend=True)
        excluded = [m for m in metrics if m not in plottable]
        plan = _build_plan("line", plottable, selected_periods, catalog)
        plan.rationale = (
            f"Courbe : {len(selected_periods)} périodes comparables ({', '.join(selected_periods)}), "
            f"même unité ({plottable[0].unit or 'sans unité'})."
        )
        if excluded:
            plan.note = _single_period_sentence(excluded) + " Ces données ne sont pas tracées."
        return plan

    if len(metrics) == 1 and len(periods_of(metrics[0])) <= 1:
        return _kpi_plan(metrics, selected_periods, trend=False)
    if len(metrics) > 1 and len(selected_periods) == 1:
        present = [m for m in metrics if m.value_at(selected_periods[0]) is not None]
        if len(present) < 2:
            return _kpi_plan(present or metrics, selected_periods, trend=False)

    chart_type = infer_chart_type(explicit_type, text, len(metrics), len(selected_periods))
    plan = _build_plan(chart_type, metrics, selected_periods, catalog)
    if chart_type == "line":
        plan.rationale = f"Courbe : {len(selected_periods)} périodes ({', '.join(selected_periods)})."
    elif chart_type == "pie":
        plan.rationale = f"Répartition de {len(plan.data)} catégories de même unité sur {selected_periods[-1]}."
    else:
        plan.rationale = f"Barres : {len(plan.data)} catégories comparables, même unité, même période."
    return plan


def _single_period_sentence(metrics: list[Metric]) -> str:
    parts = [f"« {m.label} » ({', '.join(m.periods) or 'aucune période'})" for m in metrics]
    if len(parts) == 1:
        return f"Je ne dispose que d'une seule période pour {parts[0]}."
    return f"Je ne dispose que d'une seule période pour {_join_fr(parts).replace(' ou ', ' et ')}."


def _kpi_plan(metrics: list[Metric], periods: list[str], *, trend: bool) -> KpiPlan:
    """One value per metric: a value card, and - when an evolution was
    asked - the plain reason why there is no curve (never an artificial one)."""
    items = []
    for metric in metrics:
        available = [p for p in periods if metric.value_at(p) is not None] or metric.periods
        if not available:
            continue
        period = available[-1]
        items.append(
            {"metric_id": metric.id, "label": metric.label, "period": period,
             "value": metric.value_at(period), "unit": metric.unit}
        )
    described = _join_fr([f"« {i['label']} » en {i['period']} ({format_amount(i['value'], i['unit'])})" for i in items]).replace(" ou ", " et ")
    if trend:
        message = (
            f"Je dispose uniquement de la valeur de {described}. Il me faut au moins une autre période comparable "
            "pour tracer son évolution : aucune courbe n'est donc générée. Voici la valeur disponible."
        )
        rationale = "Une seule période disponible : une courbe ou une variation n'aurait aucun sens."
    else:
        message = f"Une seule valeur est disponible : {described}."
        rationale = "Une seule valeur : affichée comme indicateur, pas comme graphique."
    return KpiPlan(message=message, items=items, rationale=rationale)


def with_suggestion(clarification: Clarification, catalog: list[Metric], suggested_ids: list[str]) -> Clarification:
    """Turns the model's guess into a question, never into a chart: loose
    wording mapped by the LLM ("dépenses informatiques" -> Budget IT) is
    offered first, for the user to confirm with one click. Auto-plotting it
    was tried and rejected - the model picked a real but WRONG metric
    ("chiffre d'affaires" -> Résultat net), which is a fabricated answer
    even though every number in it is genuine."""
    suggested = [m for m in catalog if m.id in suggested_ids]
    if not suggested:
        return clarification
    what = clarification.state.get("subject") or "cette donnée"
    labels = [m.label for m in suggested]
    message = (
        f"Je n'ai pas de données nommées « {what} ». Vouliez-vous dire {_join_fr([f'« {l} »' for l in labels])} ? "
        "Sinon, fournissez les valeurs (par exemple « 2024 : 120000, 2025 : 135000 ») ou indiquez le fichier "
        "Excel et la feuille à utiliser."
    )
    # Only the suggested metrics: the rest of the catalog is unrelated to
    # what was asked.
    return _clarify(message, labels, {k: v for k, v in clarification.state.items() if k != "option_values"})


def _content_tokens_raw(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", normalize(text)))


def _build_plan(chart_type: str, metrics: list[Metric], periods: list[str], catalog: list[Metric]) -> ChartPlan:
    unit = metrics[0].unit
    note = None

    if chart_type == "line":
        category_field, category_label = "Période", "Période"
        value_fields = [{"field": m.label, "label": m.label, "unit": m.unit, "id": m.id} for m in metrics]
        data = [{"Période": p, "id": p, **{m.label: m.value_at(p) for m in metrics}} for p in periods]
        title = "Évolution : " + ", ".join(m.label for m in metrics)
    elif len(metrics) == 1 and len(periods) > 1:
        metric = metrics[0]
        category_field, category_label = "label", "Période"
        value_fields = [{"field": "amount", "label": metric.label, "unit": unit}]
        data = [{"label": p, "amount": metric.value_at(p), "id": metric.id} for p in periods]
        title = metric.label
    else:
        period = periods[-1]
        category_field, category_label = "label", "Donnée"
        value_fields = [{"field": "amount", "label": "Montant", "unit": unit}]
        data = [
            {"label": m.label, "amount": m.value_at(period), "id": m.id}
            for m in metrics
            if m.value_at(period) is not None
        ]
        title = (f"Comparaison {period} : " if len(metrics) > 1 else "") + (
            ", ".join(m.label for m in metrics) if len(metrics) > 1 else f"{metrics[0].label} ({period})"
        )

    drilldown = {
        m.id: {
            "id": m.id,
            "label": m.label,
            "unit": m.unit,
            "points": [{"period": p, "value": v} for p, v in m.points],
            "compare_with": [o.label for o in catalog if o.id != m.id][:4],
        }
        for m in metrics
    }
    return ChartPlan(
        chart_type=chart_type,
        title=title,
        category_field=category_field,
        category_label=category_label,
        value_fields=value_fields,
        data=data,
        drilldown=drilldown,
        note=note,
        metrics=list(metrics),
        periods=list(periods),
    )


# --- variation analysis (no chart) ---------------------------------------------------

_VARIATION_RE = re.compile(
    r"\b(variations?|evolution|evolue|hausse|baisse|augmentation|augmente|diminution|diminue|ecarts?|"
    r"progression|croissance|difference)\b"
)
FINANCIAL_SOURCE = "base de données financière d'ORBIT (valeurs accessibles à votre rôle)"


def is_variation_request(text: str) -> bool:
    return bool(_VARIATION_RE.search(normalize(text)))


def _delta_sentence(metric: Metric, start: str, end: str) -> str:
    a, b = metric.value_at(start), metric.value_at(end)
    diff = b - a
    sign = "+" if diff > 0 else ("−" if diff < 0 else "")
    amount = format_amount(abs(diff), metric.unit)
    if a == 0:
        pct = "pourcentage non calculable (valeur de départ nulle)"
    else:
        pct = f"{sign}{abs(diff) / abs(a) * 100:.1f} %".replace(".", ",")
    if diff == 0:
        return (
            f"« {metric.label} » reste à {format_amount(b, metric.unit)} entre {start} et {end} "
            f"(variation de 0, les deux valeurs étant connues)."
        )
    trend = "augmente" if diff > 0 else "diminue"
    return (
        f"« {metric.label} » {trend} de {sign}{amount} ({pct}) entre {start} et {end} : "
        f"{format_amount(a, metric.unit)} → {format_amount(b, metric.unit)}."
    )


def _variation_for(metric: Metric, years: list[int]) -> str:
    available = ", ".join(f"{p} ({format_amount(v, metric.unit)})" for p, v in metric.points)
    wanted = [str(y) for y in years]
    if wanted:
        missing = [y for y in wanted if metric.value_at(y) is None]
        if missing:
            return (
                f"« {metric.label} » : valeurs disponibles pour {available}. Aucune valeur pour "
                f"{', '.join(missing)} n'a été trouvée dans les sources consultées : la variation "
                f"{'–'.join(wanted) if len(wanted) > 1 else 'demandée'} ne peut donc pas être calculée."
            )
        if len(wanted) >= 2:
            return _delta_sentence(metric, wanted[0], wanted[-1])
        earlier = [p for p in metric.periods if p < wanted[0]]
        if earlier:
            return _delta_sentence(metric, earlier[-1], wanted[0])
    if len(metric.points) < 2:
        period, value = metric.points[0]
        return (
            f"La valeur disponible pour « {metric.label} » est de {format_amount(value, metric.unit)} pour {period}. "
            "Aucune valeur comparable pour une autre année n'a été trouvée dans les sources consultées. "
            "La variation annuelle ne peut donc pas être calculée : il faudrait au moins une valeur pour une autre "
            "année (par exemple l'année précédente)."
        )
    sentences = [_delta_sentence(metric, metric.periods[0], metric.periods[-1])]
    if len(metric.points) > 2:
        steps = [
            _delta_sentence(metric, metric.periods[i], metric.periods[i + 1]) for i in range(len(metric.points) - 1)
        ]
        sentences.append("Détail année par année :\n" + "\n".join(f"- {s}" for s in steps))
    return "\n\n".join(sentences)


def analyze_variation(text: str, catalog: list[Metric]) -> str | None:
    """Variation of metrics the request names explicitly, computed from the
    authorized records only. A missing period is said to be missing - never
    replaced by 0, never estimated. None when no metric is named (the
    question then goes to the document search as before)."""
    metrics = match_metrics(text, catalog).selected
    if not metrics:
        return None
    years = requested_years(text)
    parts = [_variation_for(m, years) for m in metrics[:4]]
    return "\n\n".join(parts) + f"\n\n*Source : {FINANCIAL_SOURCE}.*"
