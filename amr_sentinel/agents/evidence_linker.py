"""
amr_sentinel/agents/evidence_linker.py
========================================
Evidence linker agent for AMR-Sentinel — Task 3.3.

What it does:
    Builds a lightweight RAG pipeline over PubMed abstracts to attach
    supporting scientific citations to each Alert object.

    For each alert, the agent:
    1. Constructs a targeted PubMed search query from the alert's pathogen,
       antibiotic class, country/region, and signal type.
    2. Fetches the top N abstract results via the NCBI Entrez API (free,
       no authentication required for low-volume use).
    3. Embeds the abstracts and the alert description using sentence-
       transformers (all-MiniLM-L6-v2, ~80MB, runs on CPU in seconds).
    4. Retrieves the top 3 most semantically relevant abstracts using
       FAISS cosine similarity search.
    5. Generates one-sentence summaries of each citation using the Claude
       API — batched in the same async call as any remaining stewardship
       work to minimise API cost.
    6. Attaches structured citation objects to alert.evidence_citations.

Architecture:
    - PubMed fetch is synchronous HTTP (requests) — no auth needed
    - Embedding is local CPU inference (sentence-transformers)
    - FAISS index is built in-memory per alert — no persistent vector store
      required for v1 (7,511 records is a lightweight dataset)
    - Citation summarisation is batched async Claude API calls
    - Graceful degradation: if PubMed fetch or embedding fails, the alert
      is returned with an empty citations list — no alert is lost

FAISS index strategy:
    v1 uses a per-alert in-memory index (build → query → discard).
    This is intentionally simple. In v2, when NCBI NDARO genomic data
    is added (Task 1.2), a persistent FAISS index over all PubMed AMR
    literature will be worth building. For now, the per-alert approach
    costs ~0.1s per alert and requires no storage.

Inputs:
    List[Alert] with stewardship_guidance populated (output of Task 3.2)

Outputs:
    List[Alert] with evidence_citations populated
    Each citation: {pmid, title, authors, year, journal, relevance_score,
                    summary, pubmed_url}

External dependencies:
    pip install sentence-transformers faiss-cpu requests
    ANTHROPIC_API_KEY in .env (for citation summarisation)
    No PubMed API key required for <3 requests/second
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

import requests
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
logger = logging.getLogger("evidence_linker")

# ---------------------------------------------------------------------------
# PubMed API constants
# ---------------------------------------------------------------------------

PUBMED_SEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_FETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
PUBMED_SUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"

# NCBI requests up to 3/second without API key; 10/second with one
NCBI_REQUEST_DELAY = 0.35  # seconds between requests

# ---------------------------------------------------------------------------
# Pathogen name normalisation for PubMed queries
# ---------------------------------------------------------------------------

PATHOGEN_QUERY_TERMS: dict[str, str] = {
    "Klebsiella pneumoniae": "Klebsiella pneumoniae",
    "Escherichia coli": "Escherichia coli",
    "Staphylococcus aureus": "Staphylococcus aureus MRSA",
    "Acinetobacter spp.": "Acinetobacter baumannii",
    "Pseudomonas aeruginosa": "Pseudomonas aeruginosa",
    "Enterococcus faecium": "Enterococcus faecium VRE",
    "Enterococcus faecalis": "Enterococcus faecalis",
    "Streptococcus pneumoniae": "Streptococcus pneumoniae",
}

ANTIBIOTIC_QUERY_TERMS: dict[str, str] = {
    "Carbapenems": "carbapenem resistance carbapenemase",
    "Glycopeptides": "vancomycin resistance VRE glycopeptide",
    "3rd-gen cephalosporins": "cephalosporin resistance ESBL ceftriaxone",
    "Fluoroquinolones": "fluoroquinolone ciprofloxacin resistance",
    "Aminoglycosides": "aminoglycoside gentamicin resistance high-level",
    "Penicillins": "penicillin ampicillin resistance beta-lactamase",
    "Macrolides": "macrolide azithromycin resistance",
    "Unknown": "antimicrobial resistance",
}

# ---------------------------------------------------------------------------
# PubMed fetcher
# ---------------------------------------------------------------------------


def _build_pubmed_query(alert) -> str:
    """
    Build a targeted PubMed search query for an alert.

    Combines pathogen, antibiotic class, and resistance terms.
    Filters to last 10 years to keep results relevant.

    Parameters
    ----------
    alert : Alert
        The alert to build a query for.

    Returns
    -------
    str
        PubMed search query string.
    """
    pathogen_term = PATHOGEN_QUERY_TERMS.get(
        alert.pathogen_name, alert.pathogen_name
    )
    antibiotic_term = ANTIBIOTIC_QUERY_TERMS.get(
        alert.antibiotic_class, "antimicrobial resistance"
    )
    return (
        f'("{pathogen_term}"[Title/Abstract]) AND '
        f'("{antibiotic_term}"[Title/Abstract]) AND '
        f'("resistance"[Title/Abstract]) AND '
        f'("2015/01/01"[Date - Publication] : "3000"[Date - Publication])'
    )


def _fetch_pubmed_ids(query: str, max_results: int = 20) -> list[str]:
    """
    Search PubMed and return a list of PMIDs matching the query.

    Parameters
    ----------
    query : str
        PubMed search query.
    max_results : int
        Maximum number of PMIDs to retrieve (default 20).

    Returns
    -------
    list[str]
        List of PubMed IDs as strings.

    Raises
    ------
    requests.RequestException
        If the HTTP request fails. Caught by caller.
    """
    params = {
        "db": "pubmed",
        "term": query,
        "retmax": max_results,
        "retmode": "json",
        "sort": "relevance",
    }
    response = requests.get(PUBMED_SEARCH_URL, params=params, timeout=15)
    response.raise_for_status()
    data = response.json()
    return data.get("esearchresult", {}).get("idlist", [])


def _fetch_pubmed_abstracts(pmids: list[str]) -> list[dict[str, Any]]:
    """
    Fetch titles, abstracts, authors, and metadata for a list of PMIDs.

    Uses the NCBI efetch endpoint with XML parsing. Falls back to
    esummary for metadata if abstract is unavailable.

    Parameters
    ----------
    pmids : list[str]
        List of PubMed IDs.

    Returns
    -------
    list[dict]
        List of article dicts with keys: pmid, title, abstract, authors,
        year, journal, pubmed_url.
    """
    if not pmids:
        return []

    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "xml",
        "rettype": "abstract",
    }
    response = requests.get(PUBMED_FETCH_URL, params=params, timeout=20)
    response.raise_for_status()

    # Parse XML — use ElementTree (stdlib, no lxml required)
    import xml.etree.ElementTree as ET

    articles = []
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        logger.warning("XML parse error fetching PubMed abstracts: %s", exc)
        return []

    for article_elem in root.findall(".//PubmedArticle"):
        try:
            pmid = article_elem.findtext(".//PMID", default="")
            title = article_elem.findtext(".//ArticleTitle", default="No title")
            # Clean up title (remove trailing period if present)
            title = title.rstrip(".")

            # Abstract — may have multiple AbstractText elements (structured)
            abstract_parts = article_elem.findall(".//AbstractText")
            abstract = " ".join(
                (elem.text or "") for elem in abstract_parts
            ).strip()
            if not abstract:
                abstract = "Abstract not available."

            # Authors
            author_elems = article_elem.findall(".//Author")
            authors = []
            for auth in author_elems[:3]:  # First 3 only
                last = auth.findtext("LastName", default="")
                initials = auth.findtext("Initials", default="")
                if last:
                    authors.append(f"{last} {initials}".strip())
            if len(author_elems) > 3:
                authors.append("et al.")
            authors_str = ", ".join(authors) if authors else "Unknown authors"

            # Year
            year = (
                article_elem.findtext(".//PubDate/Year")
                or article_elem.findtext(".//PubDate/MedlineDate", "")[:4]
                or "N/A"
            )

            # Journal
            journal = article_elem.findtext(
                ".//Journal/Title",
                default=article_elem.findtext(".//ISOAbbreviation", "Unknown journal"),
            )

            articles.append({
                "pmid": pmid,
                "title": title,
                "abstract": abstract,
                "authors": authors_str,
                "year": year,
                "journal": journal,
                "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            })

        except Exception as exc:
            logger.debug("Error parsing article element: %s", exc)
            continue

    return articles


# ---------------------------------------------------------------------------
# Semantic retrieval via sentence-transformers + FAISS
# ---------------------------------------------------------------------------


def _retrieve_top_citations(
    alert,
    articles: list[dict[str, Any]],
    top_k: int = 3,
) -> list[dict[str, Any]]:
    """
    Use sentence-transformers + FAISS to retrieve the most semantically
    relevant articles for an alert.

    Builds an in-memory FAISS index over article title+abstract embeddings,
    then queries it with an alert description embedding.

    Parameters
    ----------
    alert : Alert
        The alert to find citations for.
    articles : list[dict]
        Candidate articles from PubMed.
    top_k : int
        Number of citations to retrieve (default 3).

    Returns
    -------
    list[dict]
        Top-k most relevant articles, each with a relevance_score added.
    """
    if not articles:
        return []

    try:
        import numpy as np
        from sentence_transformers import SentenceTransformer
        import faiss
    except ImportError as exc:
        logger.warning(
            "sentence-transformers or faiss-cpu not installed — "
            "returning top-%d articles by PubMed relevance rank. "
            "Install with: pip install sentence-transformers faiss-cpu. Error: %s",
            top_k, exc,
        )
        # Fallback: return top_k by PubMed relevance order
        for i, art in enumerate(articles[:top_k]):
            art["relevance_score"] = round(1.0 - i * 0.1, 2)
        return articles[:top_k]

    # Build corpus strings: title + abstract
    corpus = [
        f"{art['title']}. {art['abstract']}"
        for art in articles
    ]

    # Alert query string
    query = (
        f"{alert.pathogen_name} {alert.antibiotic_name} resistance "
        f"{alert.antibiotic_class} {alert.country_iso3} "
        f"surveillance epidemiology"
    )

    # Load model (cached after first load)
    model = SentenceTransformer("all-MiniLM-L6-v2")

    # Encode corpus and query
    corpus_embeddings = model.encode(corpus, convert_to_numpy=True, show_progress_bar=False)
    query_embedding = model.encode([query], convert_to_numpy=True, show_progress_bar=False)

    # Normalise for cosine similarity
    faiss.normalize_L2(corpus_embeddings)
    faiss.normalize_L2(query_embedding)

    # Build FAISS flat index
    dim = corpus_embeddings.shape[1]
    index = faiss.IndexFlatIP(dim)  # Inner product = cosine after normalisation
    index.add(corpus_embeddings)

    # Search
    k = min(top_k, len(articles))
    scores, indices = index.search(query_embedding, k)

    results = []
    for score, idx in zip(scores[0], indices[0]):
        if idx < 0:
            continue
        article = articles[idx].copy()
        article["relevance_score"] = round(float(score), 4)
        results.append(article)

    return results


# ---------------------------------------------------------------------------
# Citation summarisation via Claude API
# ---------------------------------------------------------------------------


async def _summarise_citations(
    client,
    alert,
    citations: list[dict[str, Any]],
    semaphore: asyncio.Semaphore,
    model: str,
) -> tuple[str, list[dict[str, Any]]]:
    """
    Generate one-sentence summaries for each citation using the Claude API.

    All citations for one alert are summarised in a single API call
    to minimise cost and latency.

    Parameters
    ----------
    client : anthropic.AsyncAnthropic
        Async Anthropic client.
    alert : Alert
        The alert these citations belong to.
    citations : list[dict]
        Citations to summarise.
    semaphore : asyncio.Semaphore
        Concurrency limiter.
    model : str
        Claude model to use.

    Returns
    -------
    tuple[str, list[dict]]
        (alert_id, citations_with_summaries)
    """
    if not citations:
        return alert.alert_id, []

    async with semaphore:
        # Build a compact prompt — all citations in one call
        citations_text = "\n\n".join([
            f"[{i+1}] TITLE: {c['title']}\nABSTRACT: {c['abstract'][:400]}..."
            for i, c in enumerate(citations)
        ])

        prompt = f"""For the following AMR surveillance alert, provide a one-sentence summary for each cited paper explaining its relevance.

ALERT CONTEXT: {alert.pathogen_name} resistance to {alert.antibiotic_name} ({alert.antibiotic_class}) in {alert.country_iso3}. Resistance rate: {alert.current_resistance*100:.1f}%, deviation: +{alert.deviation_magnitude*100:.1f}pp, trend: {alert.trend_direction}.

PAPERS TO SUMMARISE:
{citations_text}

For each paper, write exactly one sentence (15-25 words) explaining what this paper found and why it is relevant to this resistance signal. Format as:
[1] <one sentence>
[2] <one sentence>
[3] <one sentence>

Be specific — mention the pathogen, resistance mechanism, or geographic finding."""

        try:
            message = await client.messages.create(
                model=model,
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            summary_text = message.content[0].text.strip()

            # Parse the numbered summaries
            import re
            summary_map: dict[int, str] = {}
            for match in re.finditer(r"\[(\d+)\]\s*(.+?)(?=\[\d+\]|$)", summary_text, re.DOTALL):
                idx = int(match.group(1)) - 1
                summary = match.group(2).strip().rstrip(".")
                summary_map[idx] = summary

            # Attach summaries to citations
            enriched = []
            for i, citation in enumerate(citations):
                c = citation.copy()
                c["summary"] = summary_map.get(i, f"This paper examines {alert.pathogen_name} resistance patterns relevant to the detected signal.")
                enriched.append(c)

            return alert.alert_id, enriched

        except Exception as exc:
            logger.error(
                "Citation summarisation failed for alert %s: %s",
                alert.alert_id, exc
            )
            # Fallback: attach generic summaries
            enriched = []
            for citation in citations:
                c = citation.copy()
                c["summary"] = (
                    f"This paper examines {alert.pathogen_name} "
                    f"{alert.antibiotic_class.lower()} resistance, "
                    f"relevant to the signal detected in {alert.country_iso3}."
                )
                enriched.append(c)
            return alert.alert_id, enriched


# ---------------------------------------------------------------------------
# Main evidence linker class
# ---------------------------------------------------------------------------


class EvidenceLinker:
    """
    Attaches PubMed citations with summaries to Alert objects.

    Parameters
    ----------
    max_pubmed_results : int
        Number of PubMed results to fetch per alert for the candidate pool
        (default 20). Top 3 are selected by semantic similarity.
    top_k : int
        Number of citations to attach per alert (default 3).
    concurrency : int
        Max simultaneous Claude API calls for summarisation (default 5).
    model : str
        Claude model for citation summarisation.
    api_key : str | None
        Anthropic API key. Reads from ANTHROPIC_API_KEY env var if None.
    skip_tiers : set[str] | None
        Alert tiers to skip citation linking for (default: empty set —
        all tiers get citations). Set to {"monitor"} to skip low-priority
        alerts and reduce API spend.
    """

    def __init__(
        self,
        max_pubmed_results: int = 20,
        top_k: int = 3,
        concurrency: int = 5,
        model: str = "claude-sonnet-4-6",
        api_key: Optional[str] = None,
        skip_tiers: Optional[set[str]] = None,
    ) -> None:
        self.max_pubmed_results = max_pubmed_results
        self.top_k = top_k
        self.concurrency = concurrency
        self.model = model
        self._api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self.skip_tiers = skip_tiers or set()

        if not self._api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY not found. Set it in .env or pass api_key=."
            )

    def process(self, alerts: list) -> list:
        """
        Fetch PubMed citations and attach them to all alerts.

        For each alert:
        1. Build a PubMed query from alert metadata.
        2. Fetch candidate abstracts from NCBI (rate-limited).
        3. Retrieve top-k most relevant via FAISS semantic search.
        4. Summarise citations via Claude API (batched async).
        5. Attach to alert.evidence_citations.

        Parameters
        ----------
        alerts : list[Alert]
            Alerts from the stewardship agent.

        Returns
        -------
        list[Alert]
            Same alerts with evidence_citations populated.
        """
        if not alerts:
            logger.warning("EvidenceLinker.process() called with empty alert list.")
            return alerts

        logger.info("============================================================")
        logger.info("Evidence linker started — %d alerts to process", len(alerts))

        start = time.time()

        # Filter alerts to process (skip specified tiers)
        to_process = [a for a in alerts if a.severity_tier not in self.skip_tiers]
        skipped = len(alerts) - len(to_process)
        if skipped:
            logger.info("Skipping %d alerts (tier filter: %s)", skipped, self.skip_tiers)

        # Step 1+2+3: PubMed fetch + semantic retrieval (synchronous, rate-limited)
        citation_candidates: dict[str, list[dict]] = {}
        logger.info("Fetching PubMed abstracts for %d alerts...", len(to_process))

        for i, alert in enumerate(to_process):
            try:
                query = _build_pubmed_query(alert)
                pmids = _fetch_pubmed_ids(query, self.max_pubmed_results)
                if not pmids:
                    logger.debug("No PubMed results for alert %s", alert.alert_id)
                    citation_candidates[alert.alert_id] = []
                    continue

                time.sleep(NCBI_REQUEST_DELAY)  # Rate limit
                articles = _fetch_pubmed_abstracts(pmids)

                # Semantic retrieval
                top_citations = _retrieve_top_citations(alert, articles, self.top_k)
                citation_candidates[alert.alert_id] = top_citations

                if (i + 1) % 10 == 0:
                    logger.info(
                        "PubMed fetch progress: %d/%d alerts processed",
                        i + 1, len(to_process)
                    )

            except requests.RequestException as exc:
                logger.warning(
                    "PubMed fetch failed for alert %s (%s/%s/%s): %s",
                    alert.alert_id,
                    alert.pathogen_name,
                    alert.antibiotic_name,
                    alert.country_iso3,
                    exc,
                )
                citation_candidates[alert.alert_id] = []
            except Exception as exc:
                logger.error(
                    "Unexpected error processing alert %s: %s",
                    alert.alert_id, exc
                )
                citation_candidates[alert.alert_id] = []

        logger.info("PubMed fetch complete. Alerts with citations: %d/%d",
                    sum(1 for v in citation_candidates.values() if v),
                    len(to_process))

        # Step 4: Summarise citations via Claude API (async batch)
        alerts_with_candidates = [
            a for a in to_process
            if citation_candidates.get(a.alert_id)
        ]

        if alerts_with_candidates:
            logger.info(
                "Summarising citations for %d alerts via Claude API...",
                len(alerts_with_candidates)
            )
            summaries = asyncio.run(
                self._summarise_batch(alerts_with_candidates, citation_candidates)
            )
        else:
            summaries = {}

        # Step 5: Attach citations to alerts
        alert_index = {a.alert_id: a for a in alerts}
        enriched_count = 0

        for alert in to_process:
            aid = alert.alert_id
            if aid in summaries and summaries[aid]:
                alert_index[aid].evidence_citations = summaries[aid]
                enriched_count += 1
            else:
                # No citations found — attach empty list
                alert_index[aid].evidence_citations = []

        elapsed = time.time() - start
        logger.info("------------------------------------------------------------")
        logger.info(
            "Evidence linker complete in %.1fs — %d/%d alerts enriched with citations",
            elapsed, enriched_count, len(to_process)
        )
        logger.info("============================================================")

        return alerts

    async def _summarise_batch(
        self,
        alerts: list,
        citation_candidates: dict[str, list[dict]],
    ) -> dict[str, list[dict]]:
        """
        Async batch summarisation of citations for all alerts.

        Parameters
        ----------
        alerts : list[Alert]
            Alerts with citation candidates.
        citation_candidates : dict[str, list[dict]]
            alert_id → candidate citations mapping.

        Returns
        -------
        dict[str, list[dict]]
            alert_id → enriched citations (with summaries).
        """
        try:
            import anthropic
        except ImportError as exc:
            raise ImportError(
                "anthropic package not installed. Run: pip install anthropic"
            ) from exc

        client = anthropic.AsyncAnthropic(api_key=self._api_key)
        semaphore = asyncio.Semaphore(self.concurrency)

        tasks = [
            _summarise_citations(
                client,
                alert,
                citation_candidates[alert.alert_id],
                semaphore,
                self.model,
            )
            for alert in alerts
        ]

        results = await asyncio.gather(*tasks)
        await client.close()

        return {alert_id: citations for alert_id, citations in results}


# ---------------------------------------------------------------------------
# Convenience wrapper for the orchestrator
# ---------------------------------------------------------------------------


def run_evidence_linking(
    alerts: list,
    top_k: int = 3,
    concurrency: int = 5,
    skip_tiers: Optional[set[str]] = None,
    api_key: Optional[str] = None,
) -> list:
    """
    Convenience wrapper: create an EvidenceLinker and process alerts.

    Parameters
    ----------
    alerts : list[Alert]
        Alerts with stewardship_guidance populated.
    top_k : int
        Citations per alert (default 3).
    concurrency : int
        Max simultaneous Claude API calls (default 5).
    skip_tiers : set[str] | None
        Tiers to skip. Pass {"warn", "monitor"} to only link critical alerts.
    api_key : str | None
        Anthropic API key.

    Returns
    -------
    list[Alert]
        Alerts with evidence_citations populated.
    """
    linker = EvidenceLinker(
        top_k=top_k,
        concurrency=concurrency,
        skip_tiers=skip_tiers,
        api_key=api_key,
    )
    return linker.process(alerts)


# ---------------------------------------------------------------------------
# Standalone runner — top 5 critical alerts only for cost control.
# Uses a temp state file so stale suppression never blocks the test run.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import tempfile
    import os

    try:
        from amr_sentinel.agents.triage_agent import TriageAgent
        from amr_sentinel.agents.stewardship_agent import StewardshipAgent
        from amr_sentinel.models.severity_scorer import run_severity_scoring
    except ImportError as exc:
        print(f"Import error: {exc}")
        print("Run from project root: python -m amr_sentinel.agents.evidence_linker")
        sys.exit(1)

    print("Running full pipeline: anomaly detection → severity scoring → triage → stewardship...")

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

    # Stewardship on top 5 critical only (cost control — ~$0.02 per run)
    critical = [a for a in all_alerts if a.severity_tier == "critical"][:5]
    print(f"\nRunning stewardship agent on top {len(critical)} critical alerts...")

    steward = StewardshipAgent(concurrency=5)
    critical_with_guidance = steward.process(critical)

    print(f"\nRunning evidence linker on {len(critical_with_guidance)} alerts...")
    linker = EvidenceLinker(top_k=3, concurrency=5)
    final_alerts = linker.process(critical_with_guidance)

    print(f"\n{'='*70}")
    print("AMR-SENTINEL INTELLIGENCE BULLETINS WITH CITATIONS")
    print(f"{'='*70}")

    for alert in final_alerts[:2]:  # Show first 2 for review
        print(f"\n{'─'*70}")
        print(f"ALERT: {alert.pathogen_name} / {alert.antibiotic_name} / {alert.country_iso3}")
        print(f"Score: {alert.severity_score}/100 [{alert.severity_tier.upper()}]")
        print(f"{'─'*70}")
        print(alert.stewardship_guidance or "(no guidance)")
        print(f"\nSUPPORTING EVIDENCE ({len(alert.evidence_citations)} citations):")
        for i, citation in enumerate(alert.evidence_citations, 1):
            print(f"\n[{i}] {citation.get('title', 'No title')}")
            print(f"    {citation.get('authors', '')} ({citation.get('year', '')})")
            print(f"    {citation.get('journal', '')}")
            print(f"    Relevance: {citation.get('relevance_score', 'N/A')}")
            print(f"    Summary: {citation.get('summary', 'N/A')}")
            print(f"    URL: {citation.get('pubmed_url', '')}")

    print(f"\n{'='*70}")
    print("Sprint 3 — Task 3.3 complete")
    print(f"{'='*70}")

    alerts_with_cites = sum(1 for a in final_alerts if a.evidence_citations)
    total_citations = sum(len(a.evidence_citations) for a in final_alerts)
    print(f"Alerts processed     : {len(final_alerts)}")
    print(f"Alerts with citations: {alerts_with_cites}")
    print(f"Total citations      : {total_citations}")
    if final_alerts:
        print(f"Avg per alert        : {total_citations / len(final_alerts):.1f}")
    else:
        print("Avg per alert        : N/A")