"""
amr_sentinel/agents/triage_agent.py
=====================================
Triage agent for AMR-Sentinel — Task 3.1.

What it does:
    Consumes ScoredSignal objects from the severity scorer (Task 2.4) and
    transforms them into structured Alert objects ready for the stewardship
    agent (Task 3.2) and delivery layer (Sprint 4).

    Three core responsibilities:

    1. DEDUPLICATION — The anomaly detector produces one signal per
       (pathogen, antibiotic, country, year) observation. The same resistance
       crisis in Bulgaria spans 2022, 2023, and 2024 — three separate signals
       that should collapse into a single active alert. Deduplication groups
       signals by (pathogen, antibiotic, country) triplet and retains only the
       most recent year with the highest severity score as the canonical
       representative. All contributing years are recorded in the alert's
       evidence years list for clinical context.

    2. ESCALATION THRESHOLDS — Applies configurable score thresholds to
       determine which alerts are escalated for action vs. suppressed:
           critical : score >= 80 → escalated immediately
           warn     : score >= 50 → escalated for review
           monitor  : score >= 0  → logged only, not sent

    3. STALE SIGNAL SUPPRESSION — In a live deployment the orchestrator
       runs the full pipeline on a schedule. If the same triplet fires an
       alert every run with no meaningful change in resistance rate, the
       alert queue floods with duplicates. Suppression compares each new
       alert against a persistent state store (JSON file on disk) and drops
       alerts where:
           - the alert was seen in the previous run AND
           - the actual resistance rate has not changed by more than
             `stale_threshold` (default 2pp = 0.02)
       New triplets and triplets showing meaningful change always pass through.

       PRODUCTION CAVEAT (found 2026-07-17): this suppression mechanism only
       works when triage_state.json persists between runs. On GitHub Actions,
       every scheduled run starts from a fresh repository checkout with no
       memory of the previous day's state file, so `_is_stale()` never has
       prior state to compare against — every triplet is treated as new on
       every run. Combined with `alert_id` previously being a fresh random
       UUID per run, this meant the same real-world signal (e.g. a
       persistently critical Cyprus/Ciprofloxacin/K. pneumoniae alert) was
       re-inserted as a "new" alert every single day, with observed
       duplication of 15-22+ copies per signal in the live database. The fix
       (see `_deterministic_alert_id`) makes `alert_id` a stable hash of the
       triplet's identity, so alert_writer.py's UUID-based dedup on the
       write side correctly recognises and skips repeats — independent of
       whether triage_state.json persisted or not. The stale-suppression
       logic above remains useful for local development runs where the state
       file does persist, but is no longer the only thing standing between
       production and duplicate alerts.

Architecture:
    - TriageAgent is a stateful class that loads and saves alert state to a
      configurable JSON file between runs.
    - state_path accepts str, Path, or None. None uses the default path at
      data/alerts/triage_state.json under the project root. Passing a custom
      path (e.g. a tempfile) isolates state for standalone testing without
      corrupting the live state file.
    - Alert objects match the alert schema from the master build prompt.
    - Can be run standalone (python -m amr_sentinel.agents.triage_agent) for
      testing, which internally runs the anomaly detector + severity scorer.

Inputs:
    List[ScoredSignal] from amr_sentinel.models.severity_scorer

Outputs:
    List[Alert] — deduplicated, threshold-filtered, stale-suppressed alerts
    Writes active alert state to the configured state_path
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("triage_agent")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TRIAGE_STATE_PATH = PROJECT_ROOT / "data" / "alerts" / "triage_state.json"

# ---------------------------------------------------------------------------
# Alert dataclass — matches the alert schema in the master build prompt
# ---------------------------------------------------------------------------


@dataclass
class Alert:
    """
    Structured alert object produced by the triage agent.

    Matches the alert schema defined in the master build prompt.
    All fields are serialisable to JSON for storage and API delivery.
    """

    alert_id: str
    created_at: str                   # ISO 8601 UTC
    pathogen_name: str
    antibiotic_name: str
    country_iso3: str
    severity_score: int               # 0–100
    severity_tier: str                # "monitor", "warn", "critical"
    signal_type: str                  # "trajectory_deviation", "rate_spike"
    current_resistance: float         # most recent observed rate (0.0–1.0)
    forecasted_rate: float            # TFT model forecast at this date
    deviation_magnitude: float        # actual minus forecast
    trend_direction: str              # "rising", "stable", "falling"
    evidence_years: list[int]         # all contributing years for this triplet
    antibiotic_class: str             # e.g. "Carbapenems"
    who_region: str                   # e.g. "EURO", "AFRO"
    # Fields populated by later agents (null until then)
    stewardship_guidance: Optional[str] = None
    evidence_citations: list[dict[str, Any]] = field(default_factory=list)
    routing_target: Optional[str] = None
    feedback_score: Optional[int] = None
    feedback_note: Optional[str] = None
    # Forecast confidence intervals (Task 5.5)
    forecast_lower_80: Optional[float] = None
    forecast_upper_80: Optional[float] = None
    forecast_lower_50: Optional[float] = None
    forecast_upper_50: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Alert":
        """Reconstruct an Alert from a stored dictionary."""
        return cls(**d)


# ---------------------------------------------------------------------------
# Triage agent
# ---------------------------------------------------------------------------


class TriageAgent:
    """
    Consumes ScoredSignal objects and produces deduplicated, threshold-filtered,
    stale-suppressed Alert objects.

    Parameters
    ----------
    critical_threshold : int
        Minimum severity score to classify an alert as critical (default 90).
        Raised from 80 — "critical" must be genuinely rare. A score of 90+
        requires WHO Priority 1 pathogen + last-resort antibiotic + high
        deviation + rising trend simultaneously. At the Abuja demo, opening
        the dashboard to 65 critical alerts would destroy credibility.
    warn_threshold : int
        Minimum severity score to classify an alert as warn (default 70).
    max_critical : int
        Hard cap on critical alerts per run (default 10). If more than
        max_critical alerts score above critical_threshold, excess are demoted
        to warn. Enforces operational discipline: a daily brief has a fixed
        number of "immediate action" items.
    stale_threshold : float
        Minimum absolute change in resistance rate required for a previously
        seen triplet to generate a new alert rather than being suppressed
        (default 0.02 = 2 percentage points).
    state_path : str | Path | None
        Path to the JSON file used to persist alert state between runs.
        Pass None to use the default path (data/alerts/triage_state.json).
        Pass a tempfile path to isolate state during standalone testing.
    escalation_tiers : set[str] | None
        Which tiers to escalate (include in output). "monitor" tier signals
        are logged but excluded from the returned alert list by default.
        Pass {"monitor", "warn", "critical"} to include all.
    """

    def __init__(
        self,
        critical_threshold: int = 95,
        warn_threshold: int = 70,
        max_critical: int = 15,
        stale_threshold: float = 0.02,
        state_path: Union[str, Path, None] = None,
        escalation_tiers: Optional[set[str]] = None,
    ) -> None:
        self.critical_threshold = critical_threshold
        self.warn_threshold = warn_threshold
        self.max_critical = max_critical
        self.stale_threshold = stale_threshold
        # Accept str, Path, or None — None falls back to the default path
        if state_path is None:
            self.state_path = TRIAGE_STATE_PATH
        else:
            self.state_path = Path(state_path)
        self.escalation_tiers = escalation_tiers or {"monitor", "warn", "critical"}
        self._state: dict[str, dict[str, Any]] = self._load_state()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process(self, scored_signals: list) -> list[Alert]:
        """
        Run the full triage pipeline on a list of ScoredSignal objects.

        Steps:
            1. Deduplicate by (pathogen, antibiotic, country) triplet.
            2. Apply escalation thresholds to assign tier.
            3. Suppress stale signals unchanged since last run.
            4. Produce Alert objects for all non-suppressed signals.
            5. Persist updated state to disk.

        Parameters
        ----------
        scored_signals : list[ScoredSignal]
            Output of amr_sentinel.models.severity_scorer.run_severity_scoring().

        Returns
        -------
        list[Alert]
            Alerts for tiers included in self.escalation_tiers, after
            deduplication and stale suppression.
        """
        if not scored_signals:
            logger.warning("TriageAgent.process() called with empty signal list.")
            return []

        logger.info(
            "============================================================"
        )
        logger.info("Triage agent started: %s", datetime.now(timezone.utc).isoformat())
        logger.info("Input signals: %d", len(scored_signals))

        # Step 1: Deduplicate
        deduplicated = self._deduplicate(scored_signals)
        logger.info(
            "After deduplication: %d canonical triplets (from %d signals)",
            len(deduplicated),
            len(scored_signals),
        )

        # Step 2 + 3: Threshold + stale suppression → produce alerts
        alerts: list[Alert] = []
        suppressed_count = 0
        monitor_count = 0
        new_count = 0
        updated_count = 0

        for triplet_key, group in deduplicated.items():
            canonical = group["canonical"]
            all_years = group["all_years"]

            tier = self._assign_tier(canonical.severity_score)

            if tier == "monitor" and "monitor" not in self.escalation_tiers:
                monitor_count += 1
                continue

            # Stale suppression check
            is_stale, reason = self._is_stale(triplet_key, canonical)
            if is_stale:
                suppressed_count += 1
                logger.debug(
                    "Suppressed stale signal: %s — %s", triplet_key, reason
                )
                continue

            # Track whether this is new or an update
            is_new = triplet_key not in self._state
            if is_new:
                new_count += 1
            else:
                updated_count += 1

            alert = self._build_alert(canonical, tier, all_years)
            alerts.append(alert)

            # Update state
            self._state[triplet_key] = {
                "alert_id": alert.alert_id,
                "last_seen": alert.created_at,
                "last_resistance": self._get_resistance(canonical),
                "last_score": canonical.severity_score,
                "tier": tier,
            }

        # Step 5: Enforce max_critical cap
        # Sort by score desc, demote excess critical alerts to warn.
        # This enforces operational discipline: "critical" must be rare.
        critical_alerts = sorted(
            [a for a in alerts if a.severity_tier == "critical"],
            key=lambda a: a.severity_score,
            reverse=True,
        )
        if len(critical_alerts) > self.max_critical:
            demoted = critical_alerts[self.max_critical:]
            demoted_keys = {
                f"{a.pathogen_name}|{a.antibiotic_name}|{a.country_iso3}"
                for a in demoted
            }
            demoted_count = 0
            for a in alerts:
                key = f"{a.pathogen_name}|{a.antibiotic_name}|{a.country_iso3}"
                if key in demoted_keys and a.severity_tier == "critical":
                    a.severity_tier = "warn"
                    demoted_count += 1
            logger.info(
                "  Max critical cap (%d) applied — demoted %d critical → warn",
                self.max_critical, demoted_count,
            )

        # Step 6: Persist state
        self._save_state()

        # Summary logging
        logger.info("------------------------------------------------------------")
        logger.info(
            "Triage complete. Alerts produced: %d | Suppressed (stale): %d | "
            "Monitor-only (not escalated): %d",
            len(alerts),
            suppressed_count,
            monitor_count,
        )
        logger.info(
            "  New triplets: %d | Updated triplets: %d",
            new_count,
            updated_count,
        )

        tier_counts = {"critical": 0, "warn": 0, "monitor": 0}
        for a in alerts:
            tier_counts[a.severity_tier] += 1
        logger.info(
            "  Critical: %d | Warn: %d | Monitor: %d  "
            "(thresholds: critical≥%d, warn≥%d, max_critical=%d)",
            tier_counts["critical"],
            tier_counts["warn"],
            tier_counts["monitor"],
            self.critical_threshold,
            self.warn_threshold,
            self.max_critical,
        )

        if alerts:
            top = max(alerts, key=lambda a: a.severity_score)
            logger.info(
                "  Top alert: %s / %s / %s — score %d [%s]",
                top.pathogen_name,
                top.antibiotic_name,
                top.country_iso3,
                top.severity_score,
                top.severity_tier.upper(),
            )

        logger.info(
            "============================================================"
        )
        return alerts

    def get_active_alerts(self) -> list[dict[str, Any]]:
        """
        Return all alerts currently in the triage state store.

        Useful for the orchestrator to check what is already active before
        deciding whether to re-escalate.
        """
        return list(self._state.values())

    def clear_state(self) -> None:
        """
        Reset the triage state store. Use for testing or after a data refresh
        where you want all triplets treated as new.
        """
        self._state = {}
        self._save_state()
        logger.info("Triage state cleared.")

    # ------------------------------------------------------------------
    # Internal methods
    # ------------------------------------------------------------------

    def _deduplicate(self, scored_signals: list) -> dict[str, dict]:
        """
        Group signals by (pathogen, antibiotic, country) triplet.

        For each triplet, select the canonical signal: the most recent year
        with the highest severity score (ties broken by deviation magnitude).

        Returns a dict mapping triplet_key → {canonical: ScoredSignal,
        all_years: list[int]}.
        """
        groups: dict[str, list] = {}
        for sig in scored_signals:
            key = f"{sig.pathogen_name}|{sig.antibiotic_name}|{sig.country_iso3}"
            groups.setdefault(key, []).append(sig)

        deduplicated: dict[str, dict] = {}
        for key, group_signals in groups.items():
            # Sort: highest score first, then most recent year, then deviation
            group_signals.sort(
                key=lambda s: (s.severity_score, s.year, s.deviation_magnitude),
                reverse=True,
            )
            canonical = group_signals[0]
            all_years = sorted({s.year for s in group_signals})
            deduplicated[key] = {"canonical": canonical, "all_years": all_years}

        return deduplicated

    def _assign_tier(self, score: int) -> str:
        """
        Assign escalation tier based on severity score.

        Parameters
        ----------
        score : int
            Severity score 0–100.

        Returns
        -------
        str
            "critical", "warn", or "monitor".
        """
        if score >= self.critical_threshold:
            return "critical"
        if score >= self.warn_threshold:
            return "warn"
        return "monitor"

    def _is_stale(self, triplet_key: str, signal) -> tuple[bool, str]:
        """
        Determine whether a signal should be suppressed as stale.

        A signal is stale when:
        - The same triplet was seen in the previous run (exists in state), AND
        - The actual resistance rate has not changed by more than stale_threshold.

        New triplets (not in state) are never stale.
        Triplets where the resistance rate has meaningfully changed pass through.

        NOTE: this only has an effect when self._state was loaded from a
        state file that actually persisted from a previous run. On GitHub
        Actions this is not the case (see module docstring) — every triplet
        will report "new triplet" every run there. Duplicate prevention in
        production is instead handled by `_deterministic_alert_id`, which
        ensures alert_writer.py's UUID-based dedup catches the repeat even
        when this suppression logic has no prior state to work from.

        Parameters
        ----------
        triplet_key : str
            The deduplicated triplet key.
        signal : ScoredSignal
            The canonical signal for this triplet.

        Returns
        -------
        tuple[bool, str]
            (is_stale, reason_string)
        """
        if triplet_key not in self._state:
            return False, "new triplet"

        prev = self._state[triplet_key]
        prev_resistance = prev.get("last_resistance", None)
        if prev_resistance is None:
            return False, "no prior resistance recorded"

        delta = abs(self._get_resistance(signal) - prev_resistance)
        if delta >= self.stale_threshold:
            return False, f"resistance changed by {delta:.3f} (above threshold {self.stale_threshold})"

        return (
            True,
            f"resistance unchanged (delta={delta:.3f} < threshold={self.stale_threshold})",
        )

    @staticmethod
    def _get_resistance(signal) -> float:
        """
        Safely retrieve resistance rate from a ScoredSignal or AnomalySignal.

        Tries common field names in order of likelihood so the triage agent
        is robust to minor naming differences between scorer versions.
        """
        for attr in ("current_resistance", "resistance_rate", "actual_resistance", "actual_rate", "rate"):
            val = getattr(signal, attr, None)
            if val is not None:
                return float(val)
        raise AttributeError(
            f"Cannot find resistance rate on signal object {type(signal).__name__}. "
            f"Available attributes: {[a for a in dir(signal) if not a.startswith('_')]}"
        )

    @staticmethod
    def _deterministic_alert_id(
        pathogen_name: str, antibiotic_name: str, country_iso3: str
    ) -> str:
        """
        Build a date-scoped deterministic UUID from (pathogen, antibiotic,
        country, UTC-date). Stable within a calendar day, unique across days.

        WHY DATE-SCOPED:
        The alerts table is the platform moat — a permanent surveillance log
        that compounds in value over time. Each daily pipeline run must
        produce a fresh row per triplet so the history accumulates. "K.
        pneumoniae / Imipenem / BGR on 2026-07-18" is a distinct intelligence
        record from the same triplet on 2026-07-19 — different resistance
        rates, scores, and trend states. Without a date component, the UUID
        never changes and alert_writer.py skips every subsequent detection as
        "already exists", freezing the DB at the first run's 50 alerts.

        WHY STILL DETERMINISTIC (not random):
        Within a single day, multiple pipeline triggers (manual reruns, the
        scheduled 02:00 UTC run) must not produce duplicate rows for the same
        triplet. The date-scoped key guarantees exactly one row per triplet
        per UTC day regardless of run count.

        Args:
            pathogen_name: Canonical pathogen name.
            antibiotic_name: Canonical antibiotic name.
            country_iso3: ISO3 country code.

        Returns:
            UUID string derived from triplet + today's UTC date via MD5.
            Unique per day, stable within a day.
        """
        utc_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = f"triage|{pathogen_name}|{antibiotic_name}|{country_iso3}|{utc_date}"
        return str(uuid.UUID(hashlib.md5(key.encode("utf-8")).hexdigest()))

    def _build_alert(self, signal, tier: str, all_years: list[int]) -> Alert:
        """
        Construct an Alert object from a canonical ScoredSignal.

        Parameters
        ----------
        signal : ScoredSignal
            The canonical scored signal for this triplet.
        tier : str
            Escalation tier ("critical", "warn", or "monitor").
        all_years : list[int]
            All years contributing to this alert (from deduplication).

        Returns
        -------
        Alert
        """
        return Alert(
            alert_id=self._deterministic_alert_id(
                signal.pathogen_name, signal.antibiotic_name, signal.country_iso3
            ),
            created_at=datetime.now(timezone.utc).isoformat(),
            pathogen_name=signal.pathogen_name,
            antibiotic_name=signal.antibiotic_name,
            country_iso3=signal.country_iso3,
            severity_score=signal.severity_score,
            severity_tier=tier,
            signal_type=signal.signal_type,
            current_resistance=round(self._get_resistance(signal), 4),
            forecasted_rate=round(signal.forecasted_rate, 4),
            deviation_magnitude=round(signal.deviation_magnitude, 4),
            trend_direction=signal.trend_direction,
            evidence_years=all_years,
            antibiotic_class=getattr(signal, "antibiotic_class", getattr(signal, "drug_class", "Unknown")),
            who_region=getattr(signal, "region_who", getattr(signal, "who_region", "UNKNOWN")),
            forecast_lower_80=round(float(signal.forecast_lower_80), 4)
                if getattr(signal, "forecast_lower_80", None) is not None else None,
            forecast_upper_80=round(float(signal.forecast_upper_80), 4)
                if getattr(signal, "forecast_upper_80", None) is not None else None,
            forecast_lower_50=round(float(signal.forecast_lower_50), 4)
                if getattr(signal, "forecast_lower_50", None) is not None else None,
            forecast_upper_50=round(float(signal.forecast_upper_50), 4)
                if getattr(signal, "forecast_upper_50", None) is not None else None,
        )

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> dict[str, dict[str, Any]]:
        """
        Load triage state from disk.

        Returns an empty dict if the state file does not exist yet
        (first run behaviour).

        Returns
        -------
        dict[str, dict]
            Mapping of triplet_key → last-seen alert metadata.

        Raises
        ------
        json.JSONDecodeError
            If the state file is present but malformed.
        """
        if not self.state_path.exists():
            logger.info("No existing triage state found — treating all signals as new.")
            return {}

        try:
            with self.state_path.open("r", encoding="utf-8") as f:
                state = json.load(f)
            logger.info(
                "Loaded triage state: %d active triplets from %s",
                len(state),
                self.state_path,
            )
            return state
        except json.JSONDecodeError as exc:
            logger.error(
                "Triage state file is malformed (%s). Starting fresh.", exc
            )
            return {}

    def _save_state(self) -> None:
        """
        Persist current triage state to disk.

        Creates parent directories if they do not exist.

        Raises
        ------
        OSError
            If the state file cannot be written (permissions, disk full, etc.).
        """
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.state_path.open("w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2, ensure_ascii=False)
            logger.debug("Triage state saved to %s (%d entries).", self.state_path, len(self._state))
        except OSError as exc:
            logger.error("Failed to save triage state: %s", exc)
            raise


# ---------------------------------------------------------------------------
# Convenience function for the orchestrator
# ---------------------------------------------------------------------------


def run_triage(
    scored_signals: list,
    critical_threshold: int = 95,
    warn_threshold: int = 70,
    stale_threshold: float = 0.02,
    state_path: Union[str, Path, None] = None,
    escalation_tiers: Optional[set[str]] = None,
) -> list[Alert]:
    """
    Convenience wrapper: create a TriageAgent and process signals.

    Parameters
    ----------
    scored_signals : list[ScoredSignal]
        Output of run_severity_scoring().
    critical_threshold : int
        Score threshold for critical tier (default 95).
    warn_threshold : int
        Score threshold for warn tier (default 70).
    stale_threshold : float
        Minimum resistance rate change to avoid stale suppression (default 0.02).
    state_path : str | Path | None
        Path to persist triage state between runs. None uses the default path.
        Pass a tempfile path to isolate state during testing.
    escalation_tiers : set[str] | None
        Tiers to include in output. Defaults to {"warn", "critical"}.

    Returns
    -------
    list[Alert]
        Deduplicated, threshold-filtered, stale-suppressed alerts.
    """
    agent = TriageAgent(
        critical_threshold=critical_threshold,
        warn_threshold=warn_threshold,
        stale_threshold=stale_threshold,
        state_path=state_path,
        escalation_tiers=escalation_tiers or {"monitor", "warn", "critical"},
    )
    return agent.process(scored_signals)


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------


def _print_alert_table(alerts: list[Alert]) -> None:
    """Print a formatted summary table of alerts to stdout."""
    if not alerts:
        print("No alerts produced.")
        return

    header = (
        f"{'Score':>5}  {'Tier':<8}  {'Pathogen':<30}  {'Antibiotic':<28}  "
        f"{'Ctry':<4}  {'Rate':>6}  {'Dev':>7}  {'Trend':<8}  {'Years'}"
    )
    separator = "-" * 120
    print(f"\nAlert table ({len(alerts)} alerts):")
    print(separator)
    print(header)
    print(separator)
    for a in sorted(alerts, key=lambda x: -x.severity_score):
        years_str = ",".join(str(y) for y in a.evidence_years)
        print(
            f"{a.severity_score:>5}  {a.severity_tier:<8}  "
            f"{a.pathogen_name:<30}  {a.antibiotic_name:<28}  "
            f"{a.country_iso3:<4}  {a.current_resistance:>6.3f}  "
            f"{a.deviation_magnitude:>+7.3f}  {a.trend_direction:<8}  {years_str}"
        )
    print(separator)


if __name__ == "__main__":
    import sys
    import tempfile
    import os

    try:
        from amr_sentinel.models.severity_scorer import run_severity_scoring
    except ImportError as exc:
        print(f"Import error: {exc}")
        print("Run from the project root: python -m amr_sentinel.agents.triage_agent")
        sys.exit(1)

    print("Running severity scorer (includes anomaly detection)...")
    scored_signals = run_severity_scoring()

    if not scored_signals:
        print("No scored signals returned. Check anomaly detector and severity scorer.")
        sys.exit(1)

    print(f"\nScored signals: {len(scored_signals)}")

    # Use a temporary state file so the standalone runner never suppresses
    # itself or corrupts the real triage_state.json used by the orchestrator.
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        tmp_state_path = tmp.name

    try:
        # First run: all signals are new
        print("\n--- First run (all signals new) ---")
        agent = TriageAgent(
            critical_threshold=80,
            warn_threshold=50,
            stale_threshold=0.02,
            state_path=tmp_state_path,
            escalation_tiers={"warn", "critical"},
        )
        alerts_run1 = agent.process(scored_signals)
        _print_alert_table(alerts_run1)

        # Second run: simulate re-run with identical data (most should be suppressed)
        print("\n--- Second run (simulating re-run — most should be suppressed) ---")
        agent2 = TriageAgent(
            critical_threshold=80,
            warn_threshold=50,
            stale_threshold=0.02,
            state_path=tmp_state_path,
            escalation_tiers={"warn", "critical"},
        )
        alerts_run2 = agent2.process(scored_signals)
        print(f"Alerts produced on re-run: {len(alerts_run2)} (expected: 0 or very few)")

    finally:
        # Clean up temp state file
        try:
            os.unlink(tmp_state_path)
        except OSError:
            pass

    # Summary
    print(f"\n{'='*60}")
    print("Sprint 3 — Task 3.1 complete")
    print(f"{'='*60}")
    print(f"First run alerts    : {len(alerts_run1)}")
    print(f"  Critical          : {sum(1 for a in alerts_run1 if a.severity_tier == 'critical')}")
    print(f"  Warn              : {sum(1 for a in alerts_run1 if a.severity_tier == 'warn')}")
    print(f"Second run (stale)  : {len(alerts_run2)} (suppressed by stale filter)")
    print(f"Live state file     : {TRIAGE_STATE_PATH}")
    if alerts_run1:
        top = max(alerts_run1, key=lambda a: a.severity_score)
        print(f"Top alert           : {top.pathogen_name} / {top.antibiotic_name} / {top.country_iso3} — {top.severity_score}/100")
        print(f"Evidence years      : {top.evidence_years}")