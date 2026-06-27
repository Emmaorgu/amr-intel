"""
amr_sentinel/agents/stewardship_agent.py
==========================================
Stewardship agent for AMR-Sentinel — Task 3.2.

What it does:
    Takes Alert objects from the triage agent (Task 3.1) and generates
    structured public intelligence bulletins using the Claude API
    (claude-sonnet-4-6). Each bulletin explains:

    - What the resistance signal means clinically
    - Why it is significant at a population level
    - What the trajectory implies for the next 12 months
    - What prescribing and stewardship actions are warranted
    - How this fits into the broader global AMR picture

    This is NOT a per-patient clinical decision support tool.
    It is public health surveillance intelligence — written for clinicians,
    researchers, public health officials, and stewardship teams globally.
    The tone matches an ECDC Rapid Risk Assessment or WHO situation report,
    not a clinical guideline or prescribing leaflet.

Architecture:
    - Async batched API calls using asyncio + anthropic.AsyncAnthropic
    - Processes alerts in configurable batch sizes (default 5 concurrent)
    - Adds stewardship_guidance text to each Alert object in-place
    - Graceful degradation: if an API call fails, the alert is still
      returned with a fallback guidance message — no alert is lost
    - Cost-aware: prompt is compact, max_tokens capped, critical alerts
      get richer context than warn alerts

Inputs:
    List[Alert] from amr_sentinel.agents.triage_agent

Outputs:
    List[Alert] with stewardship_guidance field populated
    Each alert's stewardship_guidance is a structured plain-text bulletin

External dependencies:
    pip install anthropic python-dotenv
    ANTHROPIC_API_KEY in .env file at project root
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("stewardship_agent")

# ---------------------------------------------------------------------------
# WHO region display names
# ---------------------------------------------------------------------------

REGION_NAMES: dict[str, str] = {
    "EURO": "Europe",
    "AFRO": "Africa",
    "EMRO": "Eastern Mediterranean",
    "SEARO": "South-East Asia",
    "WPRO": "Western Pacific",
    "AMRO": "Americas",
    "UNKNOWN": "Global",
}

# ---------------------------------------------------------------------------
# Antibiotic class clinical context — injected into prompts
# ---------------------------------------------------------------------------

CLASS_CONTEXT: dict[str, str] = {
    "Carbapenems": (
        "Carbapenems (imipenem, meropenem, ertapenem) are last-resort antibiotics "
        "for multidrug-resistant gram-negative infections. Carbapenem resistance "
        "leaves clinicians with extremely limited options — colistin, ceftazidime-"
        "avibactam, or cefiderocol — which are scarce, expensive, or unavailable "
        "in low-income settings. WHO designates carbapenem-resistant organisms as "
        "Critical Priority pathogens."
    ),
    "Glycopeptides": (
        "Glycopeptides (vancomycin, teicoplanin) are the primary treatment for "
        "serious gram-positive infections including MRSA and Enterococcus. "
        "Vancomycin-resistant Enterococcus (VRE) is classified as a WHO High "
        "Priority pathogen and is associated with significant mortality in "
        "immunocompromised and critically ill patients."
    ),
    "3rd-gen cephalosporins": (
        "Third-generation cephalosporin resistance (e.g. to ceftriaxone, "
        "cefotaxime) in Enterobacteriaceae is a primary marker for ESBL production. "
        "ESBLs are encoded on mobile plasmids and spread rapidly within and between "
        "hospital settings. Resistance rates above 50% render these agents "
        "ineffective for empirical treatment of common community infections."
    ),
    "Fluoroquinolones": (
        "Fluoroquinolone resistance (ciprofloxacin, levofloxacin) in gram-negative "
        "pathogens is a critical stewardship concern. These agents are widely used "
        "as first-line empirical therapy for UTIs, respiratory infections, and "
        "traveller's diarrhoea. High resistance rates directly erode the "
        "effectiveness of community prescribing guidelines."
    ),
    "Penicillins": (
        "Penicillin and ampicillin resistance markers indicate the presence of "
        "beta-lactamase-producing organisms. In Enterococcus, ampicillin resistance "
        "combined with glycopeptide resistance (VRE) creates pan-resistant organisms "
        "with very limited treatment options."
    ),
    "Aminoglycosides": (
        "High-level aminoglycoside resistance in Enterococcus eliminates synergistic "
        "combination therapy options, which are critical for treatment of serious "
        "enterococcal infections including endocarditis. Gentamicin high-level "
        "resistance (HLAR) is a key clinical marker tracked in European surveillance."
    ),
    "Macrolides": (
        "Macrolide resistance in Streptococcus pneumoniae (e.g. azithromycin) "
        "undermines treatment of community-acquired respiratory infections globally. "
        "Azithromycin is the most widely used antibiotic in low-income settings, "
        "making resistance particularly impactful in sub-Saharan Africa and Asia."
    ),
    "Unknown": (
        "This resistance event involves an antibiotic class with significant clinical "
        "implications for empirical treatment protocols."
    ),
}

# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------


def _build_system_prompt() -> str:
    """
    Return the system prompt for the stewardship agent.

    Establishes the surveillance intelligence voice: authoritative public
    health intelligence, not clinical decision support. Matches the tone
    of ECDC Rapid Risk Assessments and WHO situation reports.
    """
    return """You are the intelligence analysis engine for AMR-Sentinel — an autonomous antimicrobial resistance (AMR) surveillance network.

Your role is to generate public AMR intelligence bulletins. These are read by:
- Infectious disease physicians and clinical microbiologists
- Hospital antimicrobial stewardship teams
- National public health authorities and ministries of health
- AMR researchers and epidemiologists
- Global health policy officials

YOUR OUTPUT IS NOT CLINICAL ADVICE FOR INDIVIDUAL PATIENTS.
It is population-level surveillance intelligence — the same category as an ECDC Rapid Risk Assessment, a WHO situation report, or a CDC Health Advisory.

Tone: Authoritative. Evidence-based. Clinically precise. Concise.
Format: Structured plain text. No markdown headers. No bullet lists. Paragraph form.
Length: 3–4 paragraphs for critical alerts. 2–3 paragraphs for warn alerts.

Never recommend a specific drug for a specific patient.
Always frame findings at the population and stewardship level.
Always situate the signal in global context where relevant."""


def _build_user_prompt(alert) -> str:
    """
    Build the user prompt for a single alert.

    Injects structured signal data and antibiotic class clinical context
    so the model produces grounded, accurate bulletins rather than
    generic AMR text.

    Parameters
    ----------
    alert : Alert
        The Alert object from the triage agent.

    Returns
    -------
    str
        Formatted user prompt for the Claude API call.
    """
    region_name = REGION_NAMES.get(alert.who_region, alert.who_region)
    class_context = CLASS_CONTEXT.get(alert.antibiotic_class, CLASS_CONTEXT["Unknown"])
    years_str = ", ".join(str(y) for y in alert.evidence_years)
    resistance_pct = f"{alert.current_resistance * 100:.1f}%"
    forecast_pct = f"{alert.forecasted_rate * 100:.1f}%"
    deviation_pct = f"{alert.deviation_magnitude * 100:.1f}"
    trend_str = alert.trend_direction.capitalize()

    tier_instruction = (
        "This is a CRITICAL-tier signal. Provide a full 3–4 paragraph bulletin "
        "covering: (1) signal summary and clinical significance, (2) trajectory "
        "analysis and what the multi-year pattern implies, (3) stewardship and "
        "public health implications, (4) global context and what health authorities "
        "should monitor."
        if alert.severity_tier == "critical"
        else "This is a WARN-tier signal. Provide a focused 2–3 paragraph bulletin "
        "covering: (1) signal summary and clinical significance, (2) stewardship "
        "implications and what to monitor."
    )

    return f"""Generate an AMR-Sentinel Intelligence Bulletin for the following resistance signal.

SIGNAL DATA:
- Pathogen: {alert.pathogen_name}
- Antibiotic: {alert.antibiotic_name} ({alert.antibiotic_class})
- Country: {alert.country_iso3} ({region_name})
- Observed resistance rate: {resistance_pct} (year(s): {years_str})
- Model forecast had predicted: {forecast_pct}
- Deviation above forecast: +{deviation_pct} percentage points
- Trajectory: {trend_str}
- Severity score: {alert.severity_score}/100 ({alert.severity_tier.upper()})

ANTIBIOTIC CLASS CONTEXT:
{class_context}

INSTRUCTION:
{tier_instruction}

Write the bulletin now. Begin directly with the intelligence content — no preamble, no title."""


# ---------------------------------------------------------------------------
# Async API call
# ---------------------------------------------------------------------------


async def _generate_bulletin(
    client,
    alert,
    semaphore: asyncio.Semaphore,
    model: str,
    max_tokens: int,
) -> tuple[str, Optional[str]]:
    """
    Make a single async API call to generate a stewardship bulletin.

    Parameters
    ----------
    client : anthropic.AsyncAnthropic
        The async Anthropic client.
    alert : Alert
        The alert to generate guidance for.
    semaphore : asyncio.Semaphore
        Concurrency limiter — prevents hammering the API.
    model : str
        Claude model string to use.
    max_tokens : int
        Maximum tokens in the response.

    Returns
    -------
    tuple[str, str | None]
        (alert_id, bulletin_text) — bulletin_text is None on failure.
    """
    async with semaphore:
        try:
            message = await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=_build_system_prompt(),
                messages=[
                    {"role": "user", "content": _build_user_prompt(alert)}
                ],
            )
            bulletin = message.content[0].text.strip()
            return alert.alert_id, bulletin

        except Exception as exc:
            logger.error(
                "API call failed for alert %s (%s / %s / %s): %s",
                alert.alert_id,
                alert.pathogen_name,
                alert.antibiotic_name,
                alert.country_iso3,
                exc,
            )
            return alert.alert_id, None


# ---------------------------------------------------------------------------
# Batch processor
# ---------------------------------------------------------------------------


async def _process_batch(
    alerts: list,
    client,
    model: str,
    max_tokens: int,
    concurrency: int,
) -> dict[str, str]:
    """
    Process all alerts concurrently with a semaphore-controlled concurrency limit.

    Parameters
    ----------
    alerts : list[Alert]
        Alerts to process.
    client : anthropic.AsyncAnthropic
        Async Anthropic client.
    model : str
        Claude model to use.
    max_tokens : int
        Max tokens per response.
    concurrency : int
        Max simultaneous API calls.

    Returns
    -------
    dict[str, str]
        Mapping of alert_id → bulletin_text.
    """
    semaphore = asyncio.Semaphore(concurrency)
    tasks = [
        _generate_bulletin(client, alert, semaphore, model, max_tokens)
        for alert in alerts
    ]
    results = await asyncio.gather(*tasks)
    return {alert_id: text for alert_id, text in results if alert_id is not None}


# ---------------------------------------------------------------------------
# Main stewardship agent class
# ---------------------------------------------------------------------------


class StewardshipAgent:
    """
    Generates AMR intelligence bulletins for Alert objects using the Claude API.

    Parameters
    ----------
    model : str
        Claude model to use (default: claude-sonnet-4-6).
    max_tokens_critical : int
        Max tokens for critical-tier alert bulletins (default: 800).
    max_tokens_warn : int
        Max tokens for warn-tier alert bulletins (default: 400).
    concurrency : int
        Maximum simultaneous API calls (default: 5).
    api_key : str | None
        Anthropic API key. If None, reads from ANTHROPIC_API_KEY env var.
    """

    def __init__(
        self,
        model: str = "claude-sonnet-4-6",
        max_tokens_critical: int = 1000,
        max_tokens_warn: int = 400,
        concurrency: int = 5,
        api_key: Optional[str] = None,
    ) -> None:
        self.model = model
        self.max_tokens_critical = max_tokens_critical
        self.max_tokens_warn = max_tokens_warn
        self.concurrency = concurrency
        self._api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not self._api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY not found. Set it in your .env file or pass "
                "api_key= to StewardshipAgent()."
            )

    def process(self, alerts: list) -> list:
        """
        Generate stewardship bulletins for all alerts.

        Updates each Alert's stewardship_guidance field in-place and returns
        the same list with guidance populated. Alerts that fail API calls
        receive a fallback guidance message so no alert is lost.

        Parameters
        ----------
        alerts : list[Alert]
            Output of TriageAgent.process().

        Returns
        -------
        list[Alert]
            Same alerts with stewardship_guidance populated.
        """
        if not alerts:
            logger.warning("StewardshipAgent.process() called with empty alert list.")
            return alerts

        logger.info(
            "============================================================"
        )
        logger.info("Stewardship agent started — %d alerts to process", len(alerts))
        logger.info("Model: %s | Concurrency: %d", self.model, self.concurrency)

        start = time.time()

        # Split by tier for different token budgets
        critical_alerts = [a for a in alerts if a.severity_tier == "critical"]
        warn_alerts = [a for a in alerts if a.severity_tier == "warn"]
        monitor_alerts = [a for a in alerts if a.severity_tier == "monitor"]

        logger.info(
            "Processing: %d critical | %d warn | %d monitor (skipped)",
            len(critical_alerts),
            len(warn_alerts),
            len(monitor_alerts),
        )

        # Monitor alerts get a template-based fallback — no API call needed
        for alert in monitor_alerts:
            alert.stewardship_guidance = self._fallback_guidance(alert)

        # Run async batch for critical + warn
        alerts_to_process = critical_alerts + warn_alerts
        bulletins = asyncio.run(
            self._run_async(alerts_to_process)
        )

        # Apply results
        success_count = 0
        fallback_count = 0
        alert_index = {a.alert_id: a for a in alerts}

        for alert in alerts_to_process:
            bulletin = bulletins.get(alert.alert_id)
            if bulletin:
                alert_index[alert.alert_id].stewardship_guidance = bulletin
                success_count += 1
            else:
                alert_index[alert.alert_id].stewardship_guidance = (
                    self._fallback_guidance(alert)
                )
                fallback_count += 1

        elapsed = time.time() - start
        logger.info("------------------------------------------------------------")
        logger.info(
            "Stewardship agent complete in %.1fs — Success: %d | Fallback: %d | "
            "Monitor (template): %d",
            elapsed,
            success_count,
            fallback_count,
            len(monitor_alerts),
        )
        logger.info(
            "============================================================"
        )

        return alerts

    async def _run_async(self, alerts: list) -> dict[str, str]:
        """
        Run async batch processing for a list of alerts.

        Splits into critical and warn groups to apply different token budgets,
        then merges results.

        Parameters
        ----------
        alerts : list[Alert]
            Alerts to process.

        Returns
        -------
        dict[str, str]
            alert_id → bulletin_text mapping.
        """
        try:
            import anthropic
        except ImportError as exc:
            raise ImportError(
                "anthropic package not installed. Run: pip install anthropic"
            ) from exc

        client = anthropic.AsyncAnthropic(api_key=self._api_key)

        critical = [a for a in alerts if a.severity_tier == "critical"]
        warn = [a for a in alerts if a.severity_tier == "warn"]

        results: dict[str, str] = {}

        if critical:
            logger.info("Processing %d critical alerts...", len(critical))
            critical_results = await _process_batch(
                critical,
                client,
                self.model,
                self.max_tokens_critical,
                self.concurrency,
            )
            results.update(critical_results)
            logger.info("Critical alerts done: %d/%d succeeded.", len(critical_results), len(critical))

        if warn:
            logger.info("Processing %d warn alerts...", len(warn))
            warn_results = await _process_batch(
                warn,
                client,
                self.model,
                self.max_tokens_warn,
                self.concurrency,
            )
            results.update(warn_results)
            logger.info("Warn alerts done: %d/%d succeeded.", len(warn_results), len(warn))

        await client.close()
        return results

    @staticmethod
    def _fallback_guidance(alert) -> str:
        """
        Generate a template-based guidance message when the API call fails
        or for monitor-tier alerts that don't warrant an API call.

        Parameters
        ----------
        alert : Alert
            The alert requiring fallback guidance.

        Returns
        -------
        str
            Template guidance string.
        """
        resistance_pct = f"{alert.current_resistance * 100:.1f}%"
        deviation_pct = f"{alert.deviation_magnitude * 100:.1f}"
        return (
            f"AMR-Sentinel Surveillance Alert [{alert.severity_tier.upper()}]: "
            f"{alert.pathogen_name} resistance to {alert.antibiotic_name} in "
            f"{alert.country_iso3} has reached {resistance_pct}, exceeding the "
            f"model forecast by {deviation_pct} percentage points "
            f"(trend: {alert.trend_direction}). "
            f"Stewardship teams in this region should review empirical prescribing "
            f"protocols for infections likely caused by this organism. "
            f"Full intelligence bulletin pending — please retry or check system logs."
        )


# ---------------------------------------------------------------------------
# Convenience wrapper for the orchestrator
# ---------------------------------------------------------------------------


def run_stewardship(
    alerts: list,
    model: str = "claude-sonnet-4-6",
    concurrency: int = 5,
    api_key: Optional[str] = None,
) -> list:
    """
    Convenience wrapper: create a StewardshipAgent and process alerts.

    Parameters
    ----------
    alerts : list[Alert]
        Output of run_triage().
    model : str
        Claude model to use.
    concurrency : int
        Max simultaneous API calls.
    api_key : str | None
        Anthropic API key. Reads from ANTHROPIC_API_KEY env var if None.

    Returns
    -------
    list[Alert]
        Alerts with stewardship_guidance populated.
    """
    agent = StewardshipAgent(model=model, concurrency=concurrency, api_key=api_key)
    return agent.process(alerts)


# ---------------------------------------------------------------------------
# Standalone runner — processes top 10 critical alerts for cost efficiency
# Uses a temp state file so stale suppression never blocks the test run.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import tempfile
    import os

    try:
        from amr_sentinel.agents.triage_agent import TriageAgent
        from amr_sentinel.models.severity_scorer import run_severity_scoring
    except ImportError as exc:
        print(f"Import error: {exc}")
        print("Run from project root: python -m amr_sentinel.agents.stewardship_agent")
        sys.exit(1)

    print("Running severity scorer + triage agent...")
    scored_signals = run_severity_scoring()

    if not scored_signals:
        print("No scored signals returned. Check anomaly detector and severity scorer.")
        sys.exit(1)

    # Use a temp state file so every standalone run sees all signals as fresh.
    # This never touches the real triage_state.json used by the orchestrator.
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        tmp_state_path = tmp.name

    try:
        triage = TriageAgent(
            critical_threshold=80,
            warn_threshold=50,
            stale_threshold=0.02,
            state_path=tmp_state_path,
            escalation_tiers={"warn", "critical"},
        )
        all_alerts = triage.process(scored_signals)
    finally:
        try:
            os.unlink(tmp_state_path)
        except OSError:
            pass

    if not all_alerts:
        print("No alerts from triage agent. Exiting.")
        sys.exit(1)

    # For standalone testing: process only top 10 critical alerts
    # to keep API costs low during development (~$0.02–0.03 per run)
    critical_alerts = [a for a in all_alerts if a.severity_tier == "critical"]
    test_alerts = critical_alerts[:10]

    print(f"\nRunning stewardship agent on top {len(test_alerts)} critical alerts...")
    print("(Full run processes all alerts — using subset for dev cost control)\n")

    agent = StewardshipAgent(concurrency=5)
    enriched = agent.process(test_alerts)

    print(f"\n{'='*70}")
    print("AMR-SENTINEL INTELLIGENCE BULLETINS — SAMPLE OUTPUT")
    print(f"{'='*70}")

    for alert in enriched[:3]:  # Print first 3 for review
        print(f"\n{'─'*70}")
        print(f"ALERT: {alert.pathogen_name} / {alert.antibiotic_name} / {alert.country_iso3}")
        print(f"Score: {alert.severity_score}/100 [{alert.severity_tier.upper()}]")
        print(f"Resistance: {alert.current_resistance*100:.1f}% | Deviation: +{alert.deviation_magnitude*100:.1f}pp | Trend: {alert.trend_direction}")
        print(f"Evidence years: {alert.evidence_years}")
        print(f"{'─'*70}")
        print(alert.stewardship_guidance)

    print(f"\n{'='*70}")
    print("Sprint 3 — Task 3.2 complete")
    print(f"{'='*70}")
    print(f"Alerts enriched     : {len(enriched)}")
    print(f"Model used          : claude-sonnet-4-6")
    print(f"Bulletins generated : {sum(1 for a in enriched if a.stewardship_guidance and 'pending' not in a.stewardship_guidance)}")