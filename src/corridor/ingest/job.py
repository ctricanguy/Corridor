"""Daily pipeline orchestration.

``assemble_valuation`` is the PURE core: given a day's price, estimates, fiscal
periods, and the prior snapshot, it runs the window logic, builds the forward sum,
applies the consistency invariants and sanity gates, and returns either a clean
ValuationInput or a quarantine decision — with NO I/O, so it is fully unit-tested
against adversarial fixtures.

``run_daily`` is the thin wrapper that fetches via injected data sources and
persists results idempotently. Sources are injected so the whole job runs offline
in tests with fake sources.

TODO(adr): foreign/ADR support. v1 hard-rejects any ticker whose report currency
!= price currency (currency_guard). Supporting an ADR (e.g. TSM: TWD reporter, USD
ADR, 5:1 share ratio) requires FX conversion + ADR-ratio handling here, built and
tested against a real ADR. Until then such names stay 'unsupported' in config.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

# Market data (yfinance/Yahoo) is keyed by US/Eastern calendar dates. Using UTC
# would yield tomorrow's date after 20:00 ET (midnight UTC) and produce a
# start > end error because Yahoo's end is still the prior trading day.
_MARKET_TZ = ZoneInfo("America/New_York")
from typing import TYPE_CHECKING, Any

from ..constants import (
    EPS_BASIS_ADJUSTED_DILUTED,
    JOB_DAILY_REFRESH,
    REASON_CURRENCY_MISMATCH,
    REASON_INCOMPLETE_WINDOW,
    REASON_NO_ESTIMATES,
    REASON_PRICE_GAP,
    STATUS_ERROR,
    STATUS_MISSING,
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_QUARANTINE,
)
from ..datasources.base import (
    ForwardEstimateRecord,
    ForwardEstimateSource,
    FundamentalRecord,
    FundamentalsSource,
    PriceRecord,
    PriceSource,
)
from . import consistency, gates
from .fiscal import FiscalCalendar, enumerate_fiscal_quarters, fiscal_year_of, label_period
from .forward_sum import build_forward_eps_sum
from .reconcile import ntm_cross_check, quarterly_cross_check, reconcile_prices
from .records import FiscalPeriod, PricePoint, ValuationInput
from .window import unreported_window

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ..config import Config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PriorSnapshot:
    """Just enough of the previous valuation snapshot to run the day-over-day gates."""

    forward_eps_sum: float
    true_pe: float


@dataclass
class PipelineResult:
    """Outcome for one ticker on one date."""

    ticker: str
    as_of: date
    status: str  # ok | quarantine | unsupported | incomplete | error
    reason_code: str | None = None
    detail: str | None = None
    valuation: ValuationInput | None = None
    forward_records: list[ForwardEstimateRecord] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return self.status == STATUS_OK and self.valuation is not None


def _split_estimates(
    records: list[ForwardEstimateRecord],
) -> tuple[dict[str, float], dict[str, float]]:
    quarterly = {
        r.fiscal_period: r.value
        for r in records
        if r.metric == "eps" and r.period_type == "quarter"
    }
    annual = {
        r.fiscal_period: r.value
        for r in records
        if r.metric == "eps" and r.period_type == "annual"
    }
    return quarterly, annual


def assemble_valuation(
    *,
    ticker: str,
    as_of: date,
    price_point: PricePoint,
    report_currency: str,
    estimate_records: list[ForwardEstimateRecord],
    fiscal_periods: list[FiscalPeriod],
    reported_actuals: dict[str, float] | None = None,
    prior: PriorSnapshot | None = None,
    crosscheck_price_fmp: float | None = None,
    native_ntm: float | None = None,
    yf_next_quarter_eps: float | None = None,
    earnings_event: bool = False,
    thresholds: dict[str, float] | None = None,
    n_quarters: int = 4,
) -> PipelineResult:
    """Run the full consistency + quality pipeline for one ticker/date (pure)."""
    th = {
        "ntm_divergence_pct": 0.10,
        "price_disagreement_pct": 0.01,
        "true_pe_jump_factor": 2.0,
        "quarterly_xcheck_pct": 0.15,
        **(thresholds or {}),
    }

    def quarantine(reason: str, detail: str) -> PipelineResult:
        return PipelineResult(ticker, as_of, "quarantine", reason, detail)

    # 1. Hard currency-match guard (ADRs unsupported in v1) -------------------
    cur = consistency.currency_guard(report_currency, price_point.currency)
    if not cur.passed:
        return PipelineResult(ticker, as_of, "unsupported", cur.reason_code, cur.detail)

    # 2. Time alignment: paired price date must equal estimate observation date
    align = consistency.time_alignment_guard(price_point.price_date, as_of)
    if not align.passed:
        return quarantine(align.reason_code, align.detail)  # type: ignore[arg-type]

    # 3. Forward window (report-date driven, company fiscal calendar) ---------
    window = unreported_window(fiscal_periods, as_of, n=n_quarters)
    quarterly, annual = _split_estimates(estimate_records)
    fwd = build_forward_eps_sum(window, quarterly, annual, reported_actuals)
    if not fwd.complete:
        return PipelineResult(
            ticker, as_of, "incomplete", REASON_INCOMPLETE_WINDOW, fwd.construction_method
        )

    # 4. True P/E on the canonical raw contemporaneous basis ------------------
    prior_sum = prior.forward_eps_sum if prior else None
    if fwd.value <= 0:
        # A non-positive forward sum is gated below (sign flip) but division is
        # undefined; route straight to the sign-flip gate for a clean reason.
        gate = gates.gate_eps_sign_flip(fwd.value, prior_sum, earnings_event)
        if not gate.passed:
            return quarantine(gate.reason_code, gate.detail)  # type: ignore[arg-type]
    true_pe = (
        consistency.compute_true_pe(price_point.raw_close, fwd.value) if fwd.value > 0 else 0.0
    )

    # 5. Sanity gates (quarantine, never store) -------------------------------
    gate = gates.run_sanity_gates(
        price=price_point.raw_close,
        current_eps_sum=fwd.value,
        prior_eps_sum=prior.forward_eps_sum if prior else None,
        current_true_pe=true_pe,
        prior_true_pe=prior.true_pe if prior else None,
        earnings_event=earnings_event,
        split_occurred=price_point.split_ratio != 1.0,
        jump_factor=th["true_pe_jump_factor"],
    )
    if not gate.passed:
        return quarantine(gate.reason_code, gate.detail)  # type: ignore[arg-type]

    # 6. Cross-checks (flag, do NOT discard) ----------------------------------
    price_recon = reconcile_prices(
        price_point.raw_close, crosscheck_price_fmp, th["price_disagreement_pct"]
    )
    ntm = ntm_cross_check(fwd.value, native_ntm, th["ntm_divergence_pct"])
    # Quarterly cross-check: our annual-derived NEXT quarter vs yfinance's quarterly.
    derived_next_q = fwd.components[0].value if fwd.components else 0.0
    qxc = quarterly_cross_check(derived_next_q, yf_next_quarter_eps, th["quarterly_xcheck_pct"])
    if price_recon.disagreement_flag:
        logger.warning("%s price disagreement: %s", ticker, price_recon.detail)
    if ntm.divergence_flag:
        logger.warning("%s NTM divergence: %s", ticker, ntm.detail)
    if qxc.disagreement_flag:
        logger.warning("%s quarterly cross-check disagreement: %s", ticker, qxc.detail)

    valuation = ValuationInput(
        ticker=ticker,
        as_of_date=as_of,
        price=price_point.raw_close,
        forward_eps_sum=fwd.value,
        true_pe=true_pe,
        coverage_score=fwd.coverage_score,
        construction_method=fwd.construction_method,
        price_basis=consistency.TRUE_PE_PRICE_BASIS,
        eps_basis=EPS_BASIS_ADJUSTED_DILUTED,
        price_currency=price_point.currency,
        report_currency=report_currency,
        ntm_eps_native=native_ntm,
        ntm_divergence_pct=ntm.divergence_pct,
        window_divergence_flag=ntm.divergence_flag,
        price_yf=price_recon.price_yf,
        price_fmp=price_recon.price_fmp,
        price_disagreement_flag=price_recon.disagreement_flag,
        yf_next_q_eps=qxc.yf_next_q_eps,
        quarterly_xcheck_divergence_pct=qxc.divergence_pct,
        quarterly_xcheck_flag=qxc.disagreement_flag,
        components=fwd.components,
    )
    return PipelineResult(
        ticker, as_of, STATUS_OK, valuation=valuation, forward_records=estimate_records
    )


def build_fiscal_periods(
    estimate_records: list[ForwardEstimateRecord],
    reported: dict[str, date],
    cal: FiscalCalendar,
    as_of: date,
    horizon: int = 8,
) -> list[FiscalPeriod]:
    """Assemble fiscal periods for the window from the calendar + observed data.

    Quarters are ENUMERATED from the company's fiscal calendar (so a quarter we will
    derive from an annual estimate still appears), then refined with the exact
    period-end dates we actually have from quarterly estimates and with confirmed
    report dates from ``reported`` (e.g. EDGAR filing dates). Future quarters get an
    estimated report date (~21 days after period end) and stay unconfirmed.
    """
    exact_end = {
        r.fiscal_period: r.period_end_date
        for r in estimate_records
        if r.period_type == "quarter" and r.period_end_date is not None
    }
    periods: list[FiscalPeriod] = []
    for label, approx_end in enumerate_fiscal_quarters(cal, as_of, horizon=horizon):
        end = exact_end.get(label, approx_end)
        confirmed_date = reported.get(label)
        report_date = confirmed_date or date.fromordinal(end.toordinal() + 21)
        periods.append(
            FiscalPeriod(
                fiscal_period=label,
                period_end_date=end,
                report_date=report_date,
                confirmed=confirmed_date is not None,
            )
        )
    return sorted(periods, key=lambda p: (p.period_end_date, p.fiscal_period))


_QUARTER_LABEL = re.compile(r"^FY\d{4}Q[1-4]$")


def actuals_from_fundamentals(
    fundamentals: list[FundamentalRecord], cal: FiscalCalendar
) -> tuple[dict[str, date], dict[str, float]]:
    """Derive (report_dates, reported_actuals) from EDGAR quarterly fundamentals.

    CRITICAL — labels are keyed by the DATE-derived fiscal period (``label_period``
    on the EDGAR ``period_end_date``), NOT by EDGAR's own ``fy``/``fp`` label string.
    FMP estimates and the window are also date-derived, so an actual is subtracted
    from the SAME fiscal year its date belongs to even if a provider's label-string
    convention is off by one. ``check_label_alignment`` surfaces any such drift.

    For each fiscal period we take the ORIGINALLY-filed record (earliest filed_date)
    — its filed date is the confirmed report date and its value is the as-reported
    actual. Both maps come from the same source so the window roll and the
    derivation divisor never disagree.
    """
    report_dates: dict[str, date] = {}
    actuals: dict[str, float] = {}
    earliest: dict[str, date] = {}
    for f in fundamentals:
        # Quarter-ness from the as-reported label (a 10-Q entry has a Qn); the KEY
        # is re-derived from the date so it ties to the FMP annual by date.
        if "Q" not in f.fiscal_period or f.filed_date is None or f.period_end_date is None:
            continue
        key = label_period(f.period_end_date, cal)
        prev = earliest.get(key)
        if prev is None or f.filed_date < prev:
            earliest[key] = f.filed_date
            report_dates[key] = f.filed_date
            actuals[key] = f.value
    return report_dates, actuals


@dataclass(frozen=True)
class LabelAlignment:
    """One quarter's EDGAR-reported label vs our date-derived label."""

    period_end: date
    edgar_label: str  # as filed (EDGAR fy/fp)
    date_label: str  # derived from period_end + FiscalCalendar
    agree: bool


@dataclass(frozen=True)
class AlignmentReport:
    """FY-label alignment, SCOPED to the recent window the forward sum consumes.

    The forward sum only uses the current fiscal year's reported actuals, so the gate
    certifies a trailing window (default 3 fiscal years). Quarters inside it MUST
    align (``aligned`` requires zero drift there). Older quarters are EXEMPT — they
    are kept in ``older`` and logged for visibility (NVIDIA's pre-2023 period
    boundaries shifted, so a single fixed calendar can't label 15-year-old quarters),
    but they never fail the verdict. Display and verdict both use ``in_window`` so
    they can never show different ranges.
    """

    window_start_fy: int | None
    anchor_fy: int | None
    trailing_years: int
    in_window: list[LabelAlignment]
    older: list[LabelAlignment]

    @property
    def aligned(self) -> bool:
        return bool(self.in_window) and all(a.agree for a in self.in_window)

    @property
    def older_drift(self) -> list[LabelAlignment]:
        return [a for a in self.older if not a.agree]


def check_label_alignment(
    fundamentals: list[FundamentalRecord], cal: FiscalCalendar, trailing_years: int = 3
) -> AlignmentReport:
    """Compare EDGAR's OWN fy/fp label to our date-derived label, per period, SCOPED.

    Anchored to the ORIGINAL filing (earliest filed) for each period_end so the +1yr
    COMPARATIVE artifact is ignored. Partitioned into a trailing ``trailing_years``
    fiscal-year window (anchored on the most recent reported quarter) and older
    quarters: only the in-window quarters drive ``aligned``; older ones are exempt and
    reported separately. This also still catches a genuinely wrong ``fy_end_month``.
    """
    original: dict[date, FundamentalRecord] = {}
    for f in fundamentals:
        src = f.source_fiscal_period
        if src is None or "Q" not in src or f.period_end_date is None or f.filed_date is None:
            continue
        cur = original.get(f.period_end_date)
        if cur is None or (cur.filed_date is not None and f.filed_date < cur.filed_date):
            original[f.period_end_date] = f

    aligns: list[LabelAlignment] = []
    for end, f in sorted(original.items()):
        date_label = label_period(end, cal)
        edgar_label = f.source_fiscal_period or ""
        aligns.append(LabelAlignment(end, edgar_label, date_label, edgar_label == date_label))

    if not aligns:
        return AlignmentReport(None, None, trailing_years, [], [])
    anchor_fy = max(fiscal_year_of(a.period_end, cal) for a in aligns)
    window_start_fy = anchor_fy - (trailing_years - 1)
    in_window = [a for a in aligns if fiscal_year_of(a.period_end, cal) >= window_start_fy]
    older = [a for a in aligns if fiscal_year_of(a.period_end, cal) < window_start_fy]
    return AlignmentReport(window_start_fy, anchor_fy, trailing_years, in_window, older)


# --- persistence ------------------------------------------------------------
def _insert_or_ignore(
    session: Session, model: Any, values: dict[str, Any], index_elements: list[str]
) -> int:
    """INSERT OR IGNORE on the natural key (idempotent; never trips the immutability
    triggers because it is an INSERT, not an UPDATE)."""
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    stmt = (
        sqlite_insert(model).values(**values).on_conflict_do_nothing(index_elements=index_elements)
    )
    result = session.execute(stmt)
    return int(getattr(result, "rowcount", 0) or 0)


def run_daily(  # noqa: C901 - orchestration; pieces are individually tested
    config: Config,
    *,
    price_source: PriceSource,
    estimate_source: ForwardEstimateSource,
    fundamentals_source: FundamentalsSource,
    session: Session,
    as_of: date | None = None,
    reported_by_ticker: dict[str, dict[str, date]] | None = None,
    cik_by_ticker: dict[str, str] | None = None,
    dry_run: bool = False,
) -> list[PipelineResult]:
    """Fetch + assemble + persist for every supported ticker. Sources are injected.

    SELF-SUFFICIENT: upserts each parent ``securities`` row before any child snapshot,
    so it works on a fresh DB without separate seeding. RESILIENT: each ticker is
    isolated and committed on its own — one failing ticker rolls back only its own
    writes and the run continues. IDEMPOTENT: insert-or-ignore on the natural keys, so
    a re-run (or a retry after partial failure) never duplicates or trips the
    immutability triggers. Every ticker ends in ingestion_log; no silent gaps.

    Forward estimates are written even when the price is missing (a yfinance gap skips
    only the ticker's price-dependent rows). With ``dry_run`` nothing is committed.
    """
    from ..db.models import (
        ForwardEstimateSnapshot,
        IngestionLog,
        PriceSnapshot,
        Security,
        ValuationSnapshot,
    )

    as_of = as_of or datetime.now(_MARKET_TZ).date()
    cals = config.fiscal_calendars()
    thresholds = config.thresholds
    unsupported = config.unsupported_tickers
    align_years = config.alignment_trailing_years
    reported_by_ticker = reported_by_ticker or {}
    cik_by_ticker = cik_by_ticker or {}
    results: list[PipelineResult] = []

    def commit() -> None:
        if not dry_run:
            session.commit()

    for spec in config.universe:
        ticker = spec.ticker
        try:
            if ticker in unsupported:
                _log(session, IngestionLog, ticker, STATUS_QUARANTINE,
                     "configured unsupported (foreign/ADR)", REASON_CURRENCY_MISMATCH)
                results.append(PipelineResult(ticker, as_of, "unsupported"))
                commit()
                continue

            estimates = estimate_source.get_forward_estimates(ticker, as_of=as_of)
            if not estimates:
                _log(session, IngestionLog, ticker, STATUS_MISSING,
                     "no forward estimates returned", REASON_NO_ESTIMATES)
                results.append(PipelineResult(ticker, as_of, "error", detail="no estimates"))
                commit()
                continue

            # PARENT security row BEFORE any child snapshot (fixes FK rejection on a
            # fresh DB). Forward estimates are price-independent -> always written.
            _insert_or_ignore(session, Security,
                              {"ticker": ticker, "name": spec.name, "cik": spec.cik}, ["ticker"])
            for r in estimates:
                _insert_or_ignore(session, ForwardEstimateSnapshot, _fwd_row(r),
                                  ["ticker", "as_of_date", "fiscal_period", "metric", "source"])

            # Price (retried inside the adapter). Zero bars -> price gap: log it, skip
            # only the price-dependent rows; the estimates above are already written.
            price_rec = _price_on(price_source.get_prices(ticker, start=as_of), as_of)
            if price_rec is None:
                _log(session, IngestionLog, ticker, STATUS_MISSING,
                     f"no price bar for {as_of}; {len(estimates)} forward estimates written",
                     REASON_PRICE_GAP)
                results.append(PipelineResult(ticker, as_of, "price_gap",
                                              detail="no price bars (estimates written)"))
                commit()
                continue

            cal = cals.get(ticker, FiscalCalendar(fy_end_month=12))
            cik = cik_by_ticker.get(ticker) or spec.cik  # resolver wins; config is the fallback
            fundamentals = _safe_fundamentals(fundamentals_source, ticker, cik)
            edgar_dates, reported_actuals = actuals_from_fundamentals(fundamentals, cal)
            report = check_label_alignment(fundamentals, cal, trailing_years=align_years)
            for la in report.in_window:
                if not la.agree:  # IN-WINDOW drift is a real problem (e.g. bad fy_end_month)
                    logger.warning("%s FY-label drift IN GATE WINDOW: EDGAR %s vs date %s (end %s)",
                                   ticker, la.edgar_label, la.date_label, la.period_end)
                    _log(session, IngestionLog, ticker, STATUS_ERROR,
                         f"FY-label drift in gate window: EDGAR {la.edgar_label} vs date "
                         f"{la.date_label} @ {la.period_end}", None)
            if report.older_drift:  # exempt, but logged (not silently dropped)
                _log(session, IngestionLog, ticker, STATUS_PARTIAL,
                     f"{len(report.older_drift)} older quarters exempt from FY-gate "
                     f"(pre-FY{report.window_start_fy}; boundary drift)", None)
            reported = reported_by_ticker.get(ticker) or edgar_dates
            periods = build_fiscal_periods(estimates, reported, cal, as_of)
            crosscheck = _safe_fmp_price(estimate_source, ticker, as_of)
            yf_next_q = _safe_yf_next_q(price_source, ticker)

            result = assemble_valuation(
                ticker=ticker, as_of=as_of, price_point=_to_point(price_rec),
                report_currency=estimates[0].currency, estimate_records=estimates,
                fiscal_periods=periods, reported_actuals=reported_actuals,
                crosscheck_price_fmp=crosscheck, yf_next_quarter_eps=yf_next_q,
                thresholds=thresholds,
            )

            _insert_or_ignore(session, PriceSnapshot, _price_row(price_rec),
                              ["ticker", "price_date", "source"])
            if result.is_clean and result.valuation is not None:
                _insert_or_ignore(session, ValuationSnapshot,
                                  _val_row(result.valuation, config.engine_version),
                                  ["ticker", "as_of_date", "engine_version"])
                _log(session, IngestionLog, ticker, STATUS_OK,
                     result.valuation.construction_method, None, rows_written=1)
            else:
                _log(session, IngestionLog, ticker, STATUS_QUARANTINE,
                     result.detail, result.reason_code)
            results.append(result)
            commit()
        except Exception as exc:  # one bad ticker must not abort the run
            logger.exception("daily_refresh failed for %s", ticker)
            session.rollback()  # undo THIS ticker only; prior tickers are already committed
            try:
                _log(session, IngestionLog, ticker, STATUS_ERROR, str(exc)[:500], None)
                commit()
            except Exception:
                session.rollback()
            results.append(PipelineResult(ticker, as_of, "error", detail=str(exc)[:200]))

    if dry_run:
        session.rollback()
    return results


# --- small persistence/translation helpers ---------------------------------
def _log(
    session: Session,
    model: Any,
    ticker: str | None,
    status: str,
    detail: str | None,
    reason: str | None,
    rows_written: int | None = None,
) -> None:
    session.add(model(job=JOB_DAILY_REFRESH, ticker=ticker, source="pipeline", status=status,
                      detail=detail, reason_code=reason, rows_written=rows_written))


def _price_on(prices: list[PriceRecord], as_of: date) -> PriceRecord | None:
    for p in prices:
        if p.price_date == as_of:
            return p
    return None


def _to_point(p: PriceRecord) -> PricePoint:
    return PricePoint(
        price_date=p.price_date,
        raw_close=p.close if p.close is not None else 0.0,
        adj_close=p.adj_close,
        split_ratio=p.split_ratio,
        currency=p.currency,
        source=p.source,
    )


def _safe_fmp_price(estimate_source: Any, ticker: str, as_of: date) -> float | None:
    fetch = getattr(estimate_source, "fetch_price", None)
    if fetch is None:
        return None
    try:
        price: float | None = fetch(ticker, as_of)
        return price
    except Exception:
        logger.warning("FMP price cross-check unavailable for %s", ticker)
        return None


def _safe_yf_next_q(price_source: Any, ticker: str) -> float | None:
    """yfinance next-quarter ('0q') EPS for the cross-check; None if unavailable."""
    fetch = getattr(price_source, "fetch_forward_eps", None)
    if fetch is None:
        return None
    try:
        next_q: float | None = fetch(ticker).get("0q")
        return next_q
    except Exception:
        return None


def _safe_fundamentals(
    fundamentals_source: FundamentalsSource, ticker: str, cik: str | None
) -> list[FundamentalRecord]:
    """Fetch EDGAR fundamentals, tolerating a missing CIK or a network error."""
    if not cik:
        return []
    try:
        return fundamentals_source.get_fundamentals(ticker, cik=cik)
    except Exception:
        logger.warning("EDGAR fundamentals unavailable for %s", ticker)
        return []


def _fwd_row(r: ForwardEstimateRecord) -> dict[str, Any]:
    return {
        "ticker": r.ticker, "as_of_date": r.as_of_date, "period_type": r.period_type,
        "fiscal_period": r.fiscal_period, "period_end_date": r.period_end_date,
        "metric": r.metric, "value": r.value, "num_analysts": r.num_analysts,
        "basis": r.basis, "currency": r.currency, "construction_method": r.construction_method,
        "is_derived": r.is_derived, "source": r.source,
        "observation_timestamp": r.observation_timestamp,
    }


def _price_row(p: PriceRecord) -> dict[str, Any]:
    return {
        "ticker": p.ticker, "price_date": p.price_date, "open": p.open, "high": p.high,
        "low": p.low, "close": p.close, "adj_close": p.adj_close, "volume": p.volume,
        "split_ratio": p.split_ratio, "currency": p.currency, "source": p.source,
        "observation_timestamp": p.observation_timestamp,
    }


def _val_row(v: ValuationInput, engine_version: str) -> dict[str, Any]:
    return {
        "ticker": v.ticker, "as_of_date": v.as_of_date, "price": v.price,
        "forward_eps_ntm": v.forward_eps_sum, "true_pe": v.true_pe,
        "construction_method": v.construction_method, "coverage_score": v.coverage_score,
        "price_basis": v.price_basis, "eps_basis": v.eps_basis,
        "price_currency": v.price_currency, "report_currency": v.report_currency,
        "ntm_eps_native": v.ntm_eps_native, "ntm_divergence_pct": v.ntm_divergence_pct,
        "window_divergence_flag": v.window_divergence_flag, "price_yf": v.price_yf,
        "price_fmp": v.price_fmp, "price_disagreement_flag": v.price_disagreement_flag,
        "yf_next_q_eps": v.yf_next_q_eps,
        "quarterly_xcheck_divergence_pct": v.quarterly_xcheck_divergence_pct,
        "quarterly_xcheck_flag": v.quarterly_xcheck_flag,
        "is_thin_history": True, "engine_version": engine_version,
    }
