"""AN-06: les periodes sont decoupees dans le fuseau de la boutique, pas en UTC naif.

Contrat approuve (roadmap AN-06): << Instants stockes en UTC ; decoupage dans le fuseau de la
boutique (parametre de configuration), sur aux changements d'heure >>.

Les horodatages du dataset sont NAIFS (mesure 004.5-B: `orders[0].created_at.tzinfo is None`).
Le contrat tranche ce qu'ils signifient: des instants UTC. Une borne de periode est donc
l'instant UTC correspondant a MINUIT LOCAL dans le fuseau configure, ce qui laisse les
comparaisons de `Dataset.window` sur des naifs-UTC, comme avant.

Consequence a asserter, pas a corriger: une semaine de changement d'heure fait 167 h ou 169 h,
jamais 168. C'est la definition correcte d'une semaine civile locale.

C-3. `ZoneInfo("UTC")` passe par TZPATH comme n'importe quelle autre cle: si l'image de
production n'embarque pas de base de fuseaux, meme le defaut casserait. Le code specialise donc
"UTC" vers `datetime.timezone.utc`, stdlib pure. Resultat: le chemin PAR DEFAUT est immunise
contre C-3, qui ne conditionne plus que les fuseaux non-UTC. La verification en conteneur reste
due (voir `test_container_timezone_database_is_pending_measurement`).
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from mervio.analytics.periods import last_complete_period, make_period, previous_period
from mervio.analytics.pipeline import SourcePaths, run_analysis
from mervio.config import AnalyticsConfig, resolve_timezone
from mervio.errors import ConfigurationError
from mervio.identity import CustomerIdentity

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "data" / "sample"
PARIS = "Europe/Paris"
IDENTITY = CustomerIdentity(bytes.fromhex("ab" * 32))
TODAY = date(2026, 9, 14)


def _paths() -> SourcePaths:
    if not (SAMPLE / "shopify_orders.csv").exists():
        pytest.skip("fixtures synthetiques absentes")
    return SourcePaths(
        shopify_orders=str(SAMPLE / "shopify_orders.csv"),
        shopify_products=str(SAMPLE / "shopify_products.csv"),
        stripe=str(SAMPLE / "stripe_transactions.csv"),
        google_ads=str(SAMPLE / "google_ads.csv"),
    )


def _digest(config: AnalyticsConfig | None = None) -> str:
    kwargs = {"config": config} if config is not None else {}
    report = run_analysis(_paths(), today=TODAY, identity=IDENTITY, **kwargs)
    report["_meta"].pop("generated_at", None)
    blob = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _hours(period) -> float:
    return (period.end - period.start).total_seconds() / 3600


# -- resolution du fuseau ---------------------------------------------------
def test_utc_resolves_without_any_timezone_database():
    """C-3: le defaut ne doit jamais dependre de la presence de tzdata.

    `zoneinfo.ZoneInfo("UTC")` echouerait dans une image sans base de fuseaux. Le defaut du
    moteur ne peut pas porter ce risque.
    """
    assert resolve_timezone("UTC") is timezone.utc


def test_a_named_zone_resolves_through_zoneinfo():
    resolved = resolve_timezone(PARIS)
    assert getattr(resolved, "key", None) == PARIS


@pytest.mark.parametrize("bad", ["+01:00", "", "Mars/Olympus_Mons", "../etc/passwd", "UTC "])
def test_an_unresolvable_timezone_is_refused_at_config_construction(bad):
    """Refus, jamais de repli silencieux sur UTC: un repli produirait des semaines fausses
    sans aucun signal."""
    with pytest.raises(ConfigurationError):
        AnalyticsConfig(timezone=bad)


def test_the_default_timezone_is_utc():
    assert AnalyticsConfig().timezone == "UTC"


# -- decoupage: le cas de la roadmap ---------------------------------------
def test_a_sunday_late_utc_order_belongs_to_the_next_local_week():
    """Cas nomme par la roadmap AN-06.

    2026-09-13 23:30 UTC est un DIMANCHE en UTC, mais le lundi 2026-09-14 a 01:30 a Paris.
    La commande appartient donc a la semaine suivante pour le marchand.
    """
    instant = datetime(2026, 9, 13, 23, 30)          # naif == UTC, comme le dataset
    utc_week = make_period(date(2026, 9, 7), "week", "UTC")
    assert utc_week.start <= instant < utc_week.end, "en UTC: semaine du 7 septembre"

    paris_week = make_period(date(2026, 9, 7), "week", PARIS)
    assert not (paris_week.start <= instant < paris_week.end), (
        "a Paris, cet instant n'est PLUS dans la semaine du 7 septembre"
    )
    next_paris_week = make_period(date(2026, 9, 14), "week", PARIS)
    assert next_paris_week.start <= instant < next_paris_week.end


def test_period_boundaries_are_utc_instants_of_local_midnight():
    period = make_period(date(2026, 9, 7), "week", PARIS)
    expected = (datetime.combine(date(2026, 9, 7), time.min, tzinfo=resolve_timezone(PARIS))
                .astimezone(timezone.utc).replace(tzinfo=None))
    assert period.start == expected == datetime(2026, 9, 6, 22, 0)


def test_the_label_and_civil_dates_stay_local():
    """Le label et les dates affichees sont CIVILES et locales: un marchand parisien lit la
    semaine du 7 septembre, pas du 6."""
    period = make_period(date(2026, 9, 7), "week", PARIS)
    assert period.label == "2026-W37"
    assert period.start_date == date(2026, 9, 7)
    assert period.end_date == date(2026, 9, 13)
    assert period.to_dict()["start"] == "2026-09-07"


# -- changements d'heure: 167 h et 169 h -----------------------------------
def test_the_week_losing_an_hour_is_167_hours():
    """Passage a l'heure d'ete: dimanche 2026-03-29 a Paris."""
    assert _hours(make_period(date(2026, 3, 23), "week", PARIS)) == 167


def test_the_week_gaining_an_hour_is_169_hours():
    """Passage a l'heure d'hiver: dimanche 2026-10-25 a Paris."""
    assert _hours(make_period(date(2026, 10, 19), "week", PARIS)) == 169


def test_an_ordinary_week_is_168_hours():
    assert _hours(make_period(date(2026, 9, 7), "week", PARIS)) == 168


def test_dst_weeks_neither_lose_nor_duplicate_an_instant():
    """Les semaines consecutives doivent se recoller exactement: pas de trou, pas de
    recouvrement, y compris autour d'un changement d'heure."""
    weeks = [make_period(date(2026, 10, 12) + timedelta(weeks=i), "week", PARIS)
             for i in range(4)]
    for earlier, later in zip(weeks, weeks[1:]):
        assert earlier.end == later.start, f"discontinuite entre {earlier} et {later}"


def test_period_arithmetic_stays_consistent_across_a_dst_change():
    period = make_period(date(2026, 10, 26), "week", PARIS)
    assert previous_period(period).start_date == date(2026, 10, 19)
    assert previous_period(period).end == period.start


def test_month_grain_also_follows_the_local_zone():
    month = make_period(date(2026, 7, 15), "month", PARIS)
    assert month.label == "2026-07"
    assert month.start_date == date(2026, 7, 1)
    assert month.start == datetime(2026, 6, 30, 22, 0)


def test_last_complete_period_respects_the_zone():
    period = last_complete_period(date(2026, 9, 14), date(2026, 9, 14), "week", PARIS)
    assert period.label == "2026-W37"


# -- non-regression: le defaut "UTC" ne change RIEN ------------------------
def test_the_utc_default_keeps_the_report_byte_identical():
    """"UTC" est la seule valeur par defaut qui reproduit exactement le comportement anterieur
    a AN-06: declarer le fuseau explicitement ne doit rien changer (exigence A-16).

    Invariant RELATIF, volontairement: une empreinte absolue echouerait au premier defaut
    suivant de 004.5 pour une raison etrangere a AN-06.
    """
    assert _digest(AnalyticsConfig(timezone="UTC")) == _digest()


def test_a_non_utc_zone_actually_changes_the_analysis():
    """Garde anti-placebo: si le fuseau n'etait pas consomme, cette empreinte serait identique
    a celle du defaut et AN-06 serait un faux vert."""
    assert _digest(AnalyticsConfig(timezone=PARIS)) != _digest()


# -- determinisme: l'environnement n'a aucun effet -------------------------
_SUBPROCESS = """
import hashlib, json, sys
from datetime import date
from mervio.analytics.pipeline import SourcePaths, run_analysis
from mervio.config import AnalyticsConfig
from mervio.identity import CustomerIdentity
D = sys.argv[1]
report = run_analysis(
    SourcePaths(shopify_orders=D + "/shopify_orders.csv",
                shopify_products=D + "/shopify_products.csv",
                stripe=D + "/stripe_transactions.csv",
                google_ads=D + "/google_ads.csv"),
    config=AnalyticsConfig(timezone=sys.argv[2]), today=date(2026, 9, 14),
    identity=CustomerIdentity(bytes.fromhex("ab" * 32)))
report["_meta"].pop("generated_at", None)
blob = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
print(hashlib.sha256(blob.encode("utf-8")).hexdigest())
"""


@pytest.mark.parametrize("configured", ["UTC", PARIS])
def test_the_process_tz_environment_variable_has_no_effect(configured):
    """Le fuseau est une entree de CONFIGURATION, pas un etat d'environnement."""
    if not (SAMPLE / "shopify_orders.csv").exists():
        pytest.skip("fixtures synthetiques absentes")
    digests = set()
    for env_tz in ("UTC", "Pacific/Kiritimati", "America/Los_Angeles"):
        env = dict(os.environ, TZ=env_tz, LC_ALL="C", PYTHONDONTWRITEBYTECODE="1",
                   PYTHONPATH=f"{ROOT / 'src'}{os.pathsep}{ROOT / 'tests'}")
        out = subprocess.run([sys.executable, "-c", _SUBPROCESS, str(SAMPLE), configured],
                             cwd=ROOT, env=env, capture_output=True, text=True,
                             timeout=300, check=True)
        digests.add(out.stdout.strip())
    assert len(digests) == 1, f"TZ du processus influence le rapport: {digests}"


def test_the_analytics_engine_never_reads_the_local_clock():
    """Garde statique: `date.today()`, `datetime.now()` sans fuseau et `localtime` liraient
    l'horloge locale du processus et casseraient le determinisme."""
    offenders = []
    for path in sorted((ROOT / "src" / "mervio" / "analytics").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            attribute = node.func.attr
            if attribute == "today":
                offenders.append(f"{path.name}:{node.lineno} .today()")
            elif attribute in ("localtime", "fromtimestamp"):
                offenders.append(f"{path.name}:{node.lineno} .{attribute}()")
            elif attribute == "now" and not node.args and not node.keywords:
                offenders.append(f"{path.name}:{node.lineno} .now() sans fuseau")
    assert not offenders, "lecture de l'horloge locale:\n" + "\n".join(offenders)


# -- C-3: non mesure, et declare tel quel ----------------------------------
#
# PAS de test ignore ici. `tests/pytest_guard.py` fait echouer toute session ou un test est
# ignore quand `MERVIO_REQUIRE_DATABASE_TESTS=1` (garde anti-faux-vert, Mission 004.3): un
# `skipif` sur la presence de Docker ferait donc echouer la CI pour une raison qui n'est pas un
# defaut. Le depot a deja son motif pour cela (`tests/test_container_smoke.py`): on teste les
# REGLES sans Docker, et le parcours reel vit dans le job `docker` de la CI.
#
# Ce qui est donc asserte ici: le POINT DE PASSAGE est unique. Toute resolution de fuseau passe
# par `config.resolve_timezone`, et le defaut n'en depend pas. La question C-3 a ainsi exactement
# un endroit ou etre repondue, et A-21 reste explicitement PENDING jusqu'a cette mesure.
def test_timezone_resolution_has_a_single_chokepoint():
    """Aucun module n'appelle `ZoneInfo` directement: tout passe par `resolve_timezone`.

    Deux consequences. D'abord le defaut "UTC" ne touche jamais une base de fuseaux, donc C-3 ne
    peut pas casser le comportement par defaut. Ensuite, si la mesure C-3 revient negative, il y
    a UN seul endroit a traiter.
    """
    offenders = []
    for path in sorted((ROOT / "src" / "mervio").rglob("*.py")):
        if path.name == "config.py":
            continue  # la seule implementation autorisee
        source = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Name) and node.id == "ZoneInfo":
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
            elif isinstance(node, ast.Attribute) and node.attr == "ZoneInfo":
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders, (
        "resolution de fuseau hors de config.resolve_timezone:\n" + "\n".join(offenders)
    )


def test_the_container_tz_probe_command_is_recorded_for_ci():
    """C-3 = MEASUREMENT REQUIRED, et la mesure est ECRITE, pas inventee.

    Ce test ne mesure pas l'image: il garantit que l'image visee est epinglee par digest, donc
    que la mesure faite en CI portera sur exactement l'image de production. Tant qu'elle n'a pas
    ete faite, A-21 est PENDING: un fuseau non-UTC n'est pas garanti resoluble en production.
    """
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    pinned = [line for line in dockerfile.splitlines() if line.startswith("ARG PYTHON_IMAGE=")]
    assert len(pinned) == 1, "une seule image de base attendue"
    assert "@sha256:" in pinned[0], (
        "l'image doit etre epinglee par digest, sinon la mesure C-3 ne porterait sur rien de stable"
    )
