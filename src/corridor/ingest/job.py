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
from datetime import UTC, date, datetime, time as _time, timedelta
from zoneinfo import ZoneInfo

# Market data (yfinance/Yahoo) is keyed by US/Eastern trading-day dates.
# Use _market_as_of() — not just datetime.now(UTC).date() — to compute the
# valuation date: it returns the most recent COMPLETED equity trading day,
# rolling back past midnight-to-close ET and over weekends.
_MARKET_TZ = ZoneInfo("America/New_York")
# NYSE/Nasdaq regular session closes 16:00 ET; allow 30 min for data to settle.
_MARKET_CLOSE_ET = _time(16, 30)
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
    fundamentals: list[FundamentalRecord], cal: FiscalCalendar, trailing_years: int = 3,
    as_of: date | None = None,
) -> AlignmentReport:
    """Compare EDGAR's OWN fy/fp label to our date-derived label, per period, SCOPED.

    Anchored to the ORIGINAL filing (earliest filed) for each DATE-DERIVED fiscal label
    (not raw period_end) so the +1yr COMPARATIVE artifact is ignored. Using the
    date-derived label as the dedup key rather than the raw period_end date also handles
    52/53-week fiscal year boundary shifts: original and comparative filings for the same
    quarter can have period_end dates that differ by 1-2 days (e.g. AMD Q2 2024 ends
    2024-06-30 in the original 10-Q but 2024-06-29 in the comparative) — both map to
    the same date-derived label, so the original filing always wins.

    Window is anchored to ``as_of`` when provided. Without it the anchor defaults to the
    most recent EDGAR entry, which can misclassify very old quarters as in-window for
    tickers whose EDGAR companyfacts lacks recent quarterly entries (e.g. GOOGL if only
    old entries are available). Passing ``as_of`` guarantees the window covers the last N
    fiscal years relative to today.

    COMPARATIVE-ONLY entries: some companies (e.g. AMD) file EDGAR companyfacts entries
    without a ``start`` date for certain quarters. ``_classify`` requires a start date to
    compute the period span, so those entries are excluded from ``fundamentals`` entirely.
    The only surviving entry for that quarter is then the comparative that appeared in the
    NEXT year's 10-Q (which does have a start/end pair). That entry carries the expected
    +1yr fy drift and is filed >150 days after the period_end. Since SEC large-accelerated-
    filer rules require a 10-Q within 40 days of period-end, any entry filed more than
    150 days after its period_end can only be a comparative — the drift is expected and
    the date-derived label (used for actual subtraction) is definitively correct. Such
    entries are marked ``agree=True`` (benign). A genuine fy_end_month misconfiguration
    produces drift on ORIGINAL filings (filed <40 days after period-end) and would still
    be surfaced.
    """
    # Key by DATE-DERIVED label — matches actuals_from_fundamentals. Entries for the same
    # quarter with slightly different period_end dates (52/53-week boundary shift) collapse
    # to one label; earliest-filed wins.
    original: dict[str, FundamentalRecord] = {}
    for f in fundamentals:
        src = f.source_fiscal_period
        if src is None or "Q" not in src or f.period_end_date is None or f.filed_date is None:
            continue
        date_label = label_period(f.period_end_date, cal)
        cur = original.get(date_label)
        if cur is None or f.filed_date < cur.filed_date:
            original[date_label] = f

    aligns: list[LabelAlignment] = []
    for date_label, f in sorted(original.items(),
                                key=lambda kv: (kv[1].period_end_date or date(1900, 1, 1), kv[0])):
        edgar_label = f.source_fiscal_period or ""
        labels_match = edgar_label == date_label
        # Comparative-only: original entry absent from companyfacts (no start date ->
        # _classify excluded it). The surviving entry is a comparative filed >150 days
        # after period_end — drift is expected, computation is correct.
        is_benign_comparative = (
            not labels_match
            and f.period_end_date is not None
            and (f.filed_date - f.period_end_date).days > 150
        )
        aligns.append(LabelAlignment(f.period_end_date, edgar_label, date_label,
                                     labels_match or is_benign_comparative))

    if not aligns:
        return AlignmentReport(None, None, trailing_years, [], [])
    if as_of is not None:
        anchor_fy = fiscal_year_of(as_of, cal)
    else:
        anchor_fy = max(
            fiscal_year_of(a.period_end, cal) for a in aligns if a.period_end is not None
        )
    window_start_fy = anchor_fy - (trailing_years - 1)
    in_window = [a for a in aligns
                 if a.period_end is not None and fiscal_year_of(a.period_end, cal) >= window_start_fy]
    older = [a for a in aligns
             if a.period_end is None or fiscal_year_of(a.period_end, cal) < window_start_fy]
    return AlignmentReport(window_start_fy, anchor_fy, trailing_years, in_window, older)


def _market_as_of() -> date:
    """Most recent completed US equity trading day as of now.

    Pinned to US/Eastern throughout.  Two adjustments:

    1. **Before-close rollback** — if the current ET time is before 16:30 ET,
       today's session has not closed (or its bars haven't settled).  Roll back
       one calendar day.  This covers midnight-to-4:30pm ET, including the
       window where UTC is already tomorrow but ET is still today-without-data.

    2. **Weekend skip** — roll back over Saturday/Sunday until we land on a
       weekday (Friday for a Sunday/Monday-early-morning run).  Holidays that
       fall on weekdays are not filtered here; yfinance will return zero bars
       and the daily job records a price_gap, which is correct.

    The cron fires at 22:00 local (Pi is typically Eastern, so 22:00 ET): well
    past 16:30, so adjustment 1 never applies.  Adjustment 2 never applies
    either because the cron is weekdays-only.  Both adjustments exist for
    manual runs and guard against the midnight-to-open window.
    """
    now_et = datetime.now(_MARKET_TZ)
    candidate = now_et.date()
    if now_et.time() < _MARKET_CLOSE_ET:
        candidate -= timedelta(days=1)
    while candidate.weekday() >= 5:   # 5 = Saturday, 6 = Sunday
        candidate -= timedelta(days=1)
    return candidate


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

    as_of = as_of or _market_as_of()
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
            report = check_label_alignment(fundamentals, cal, trailing_years=align_years, as_of=as_of)
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
                computed = _compute_corridor_peg_signal(
                    session, result.valuation, fundamentals, cal, as_of, config,
                )
                _insert_or_ignore(session, ValuationSnapshot,
                                  _val_row(result.valuation, config.engine_version, computed),
                                  ["ticker", "as_of_date", "engine_version"])
                # UPDATE the computed columns even when the INSERT was a no-op
                # (on_conflict_do_nothing silently skips re-runs on the same date,
                # so existing rows would otherwise keep stale NULL computed values).
                _update_computed_snapshot(
                    session, ticker, as_of, config.engine_version, computed,
                )
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


def _update_computed_snapshot(
    session: Any, ticker: str, as_of: date, engine_version: str, computed: dict[str, Any]
) -> None:
    """UPDATE the computed (derived) columns on an existing ValuationSnapshot row.

    INSERT uses on_conflict_do_nothing, so a re-run on the same as_of date skips
    the INSERT entirely — existing rows never had corridor/PEG/signal columns set
    when those were added later.  This UPDATE fills them in idempotently. Only the
    recomputable columns are touched; the core measured values (price, true_pe,
    forward_eps_ntm) are never modified.
    """
    if not computed:
        return
    from ..db.models import ValuationSnapshot as VS

    allowed = {
        "history_days", "is_thin_history",
        "pe_median", "pe_pctl_low", "pe_pctl_high",
        "corridor_low", "corridor_high", "pe_percentile",
        "forward_peg", "growth_rate", "growth_basis", "peg_suppressed",
        "signal", "notes",
    }
    updates = {k: v for k, v in computed.items() if k in allowed}
    if not updates:
        return
    session.query(VS).filter(
        VS.ticker == ticker,
        VS.as_of_date == as_of,
        VS.engine_version == engine_version,
    ).update(updates, synchronize_session=False)


def _compute_corridor_peg_signal(
    session: Any,
    v: ValuationInput,
    fundamentals: list[FundamentalRecord],
    cal: FiscalCalendar,
    as_of: date,
    config: Any,
) -> dict[str, Any]:
    """Compute corridor bands, PEG, and signal from accumulated history + today's new point.

    Queries existing certified ValuationSnapshot rows for this ticker (the rows
    already committed from prior runs), appends today's new point, then runs
    build_corridor / forward_peg / classify_signal. Returns a dict of the extra
    columns to merge into _val_row. On any error returns an empty dict so the
    primary valuation row is never blocked.
    """
    from ..db.models import ValuationSnapshot as VS
    from ..engine.corridor import TruePePoint, build_corridor
    from ..engine.peg import (forward_peg, ntm_vs_ltm_growth,
                               quarterly_actuals_from_edgar, trailing_ltm_eps)
    from ..engine.signals import classify_signal, earnings_trend as _earnings_trend

    try:
        # --- existing history (prior runs only; today not committed yet) -------
        hist_rows = (
            session.query(VS)
            .filter(
                VS.ticker == v.ticker,
                VS.engine_version == config.engine_version,
                VS.true_pe.isnot(None),
            )
            .order_by(VS.as_of_date)
            .all()
        )
        history = [TruePePoint(r.as_of_date, r.true_pe) for r in hist_rows]
        eps_pts = [(r.as_of_date, r.forward_eps_ntm)
                   for r in hist_rows if r.forward_eps_ntm is not None]

        # Append today's new point so corridor reflects the full series.
        history_today = history + [TruePePoint(as_of, v.true_pe)]
        eps_pts_today = eps_pts + [(as_of, v.forward_eps_sum)]

        # --- corridor params from config (with defaults) ----------------------
        cor_cfg = (config.valuation or {}).get("corridor", {})
        min_hist = int(cor_cfg.get("min_history_days", 60))
        lookback = int(cor_cfg.get("lookback_days", 504)) if cor_cfg.get("lookback_days") else None
        corridor = build_corridor(
            v.ticker, as_of, history_today, v.forward_eps_sum, v.price,
            pctl_low=int(cor_cfg.get("pctl_low", 20)),
            pctl_high=int(cor_cfg.get("pctl_high", 80)),
            min_history_days=min_hist,
            lookback_days=lookback,
            coverage_score=v.coverage_score,
        )

        # --- LTM EPS from EDGAR actuals (for PEG) -----------------------------
        ltm: float | None = None
        try:
            q_acts, a_acts = quarterly_actuals_from_edgar(fundamentals, cal)
            ltm = trailing_ltm_eps(q_acts, a_acts)
        except Exception:
            pass

        peg_cfg = config.peg or {}
        rate, reason = ntm_vs_ltm_growth(v.forward_eps_sum, ltm)
        peg_res = forward_peg(
            v.true_pe, v.forward_eps_sum, rate, "ntm_vs_ltm",
            growth_input=ltm, growth_reason=reason,
            min_growth_rate=float(peg_cfg.get("min_growth_rate", 0.02)),
            cheap_threshold=float(peg_cfg.get("cheap_threshold", 1.0)),
            rich_threshold=float(peg_cfg.get("rich_threshold", 2.0)),
            coverage_score=v.coverage_score, label=v.ticker,
        )

        # --- earnings trend + signal ------------------------------------------
        sig_cfg = (config.valuation or {}).get("signals", {})
        trend, _ = _earnings_trend(eps_pts_today)
        sig_res = classify_signal(
            corridor.pe_percentile, trend,
            corridor.is_thin, corridor.bands is not None,
            hard_buy_pctl=int(sig_cfg.get("hard_buy_pctl", 10)),
            buy_pctl=int(sig_cfg.get("buy_pctl", 20)),
            trim_pctl=int(sig_cfg.get("trim_pctl", 80)),
            hard_trim_pctl=int(sig_cfg.get("hard_trim_pctl", 90)),
        )

        return {
            "history_days": corridor.history_days,
            "is_thin_history": corridor.is_thin,
            "pe_median": corridor.bands.pe_median if corridor.bands else None,
            "pe_pctl_low": corridor.bands.pe_low if corridor.bands else None,
            "pe_pctl_high": corridor.bands.pe_high if corridor.bands else None,
            "corridor_low": corridor.corridor_low_price,
            "corridor_high": corridor.corridor_high_price,
            "pe_percentile": corridor.pe_percentile,
            "forward_peg": peg_res.forward_peg,
            "growth_rate": peg_res.growth_rate,
            "growth_basis": peg_res.growth_basis,
            "peg_suppressed": peg_res.suppressed,
            "signal": sig_res.signal,
            "notes": sig_res.rationale,
        }
    except Exception:
        logger.exception("corridor/PEG/signal computation failed for %s — row stored without them",
                         v.ticker)
        return {}


def _val_row(
    v: ValuationInput, engine_version: str, computed: dict[str, Any] | None = None
) -> dict[str, Any]:
    base = {
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
    if computed:
        base.update(computed)
    return base
