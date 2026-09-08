"""Deterministic Decimal account engine for the frozen S2 formula contract.

This module contains no UI code. Monetary amounts remain unrounded internally;
rounding to RMB cents is a presentation operation only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_EVEN, getcontext
from typing import Any, Iterable
from zoneinfo import ZoneInfo


getcontext().prec = 38
D0 = Decimal("0")
D1 = Decimal("1")
CENT = Decimal("0.01")
EPS = Decimal("1e-24")
PENDING_OPEN = {"PENDING", "CLEARED_NOT_ELIGIBLE", "ELIGIBLE"}
UNSETTLED_OPEN = {"PENDING_SUBSCRIPTION", "REDEMPTION_RECEIVABLE"}


def dec(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def dt(value: datetime | str) -> datetime:
    value = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    # The frozen contract uses Asia/Shanghai wall-clock cut-offs.  The API
    # adapter normalizes requests before they reach the engine; this extra
    # guard keeps historical imports and direct engine calls comparable too.
    if value.tzinfo is not None:
        return value.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    return value


def display_rmb(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_EVEN)


@dataclass
class PendingCashLot:
    lot_id: str
    event_id: str
    amount: Decimal
    remaining_amount: Decimal
    created_at: datetime
    cleared_at: datetime
    eligible_batch_id: str
    status: str
    provenance: str = "EXTERNAL_CONTRIBUTION"


@dataclass
class UnsettledLot:
    lot_id: str
    event_id: str
    kind: str
    amount: Decimal
    created_at: datetime
    available_at: datetime
    purpose: str
    status: str


def solve_target_after_fee(
    current_fund: Decimal | float | str,
    active_before_trade: Decimal | float | str,
    target_exposure: Decimal | float | str,
    fee_rate: Decimal | float | str,
) -> dict[str, Decimal | str]:
    """Solve F_final=p*(A-fee(|F_final-F_current|)) piecewise."""
    f = dec(current_fund)
    a = dec(active_before_trade)
    p = dec(target_exposure)
    k = dec(fee_rate)
    # F is measured before a same-day ProfitLock, while A is measured after it.
    # Therefore F may legitimately exceed A: a net sale funds the reserve lock.
    if f < 0 or a < 0:
        raise ValueError("current fund and active wealth must be non-negative")
    if p < 0 or p > 1 or k < 0 or k >= 1:
        raise ValueError("p must be [0,1] and fee must be [0,1)")
    desired_zero_cost = p * a
    if abs(desired_zero_cost - f) <= EPS:
        final_fund = f
        direction = "HOLD"
    elif desired_zero_cost > f:
        final_fund = p * (a + k * f) / (D1 + p * k)
        direction = "BUY"
        if final_fund < f - EPS:
            raise ArithmeticError("buy branch sign inconsistency")
    else:
        denominator = D1 - p * k
        if denominator <= 0:
            raise ArithmeticError("invalid sell denominator")
        final_fund = p * (a - k * f) / denominator
        direction = "SELL"
        if final_fund > f + EPS:
            raise ArithmeticError("sell branch sign inconsistency")
    trade = final_fund - f
    cost = k * abs(trade)
    active_after_cost = a - cost
    cash_after = active_after_cost - final_fund
    if final_fund < -EPS or cash_after < -EPS:
        raise ArithmeticError("fee solution produced negative assets")
    if abs(final_fund) <= EPS:
        final_fund = D0
    if abs(cash_after) <= EPS:
        cash_after = D0
    achieved = final_fund / active_after_cost if active_after_cost > EPS else D0
    if active_after_cost > EPS and abs(achieved - p) > Decimal("1e-24"):
        raise ArithmeticError("target exposure tolerance exceeded")
    return {
        "direction": direction,
        "final_fund": final_fund,
        "trade": trade,
        "fee": cost,
        "active_after_cost": active_after_cost,
        "cash_after": cash_after,
        "achieved_exposure": achieved,
    }


class AccountEngine:
    """Event-sourced account state following the frozen v3 contract."""

    def __init__(
        self,
        fund_value: Any,
        strategy_cash: Any,
        reserve_cash: Any,
        initial_at: datetime | str,
        initial_nav: Any = "1.000000",
        dividend_source_mode: str = "TOTAL_RETURN_EMBEDDED",
        audit_enabled: bool = True,
    ) -> None:
        self.F = dec(fund_value)
        self.C = dec(strategy_cash)
        self.R = dec(reserve_cash)
        self.payable = D0
        self.pending_lots: list[PendingCashLot] = []
        self.unsettled_lots: list[UnsettledLot] = []
        self.initial_at = dt(initial_at)
        self.last_event_at = self.initial_at
        self.initial_nav = dec(initial_nav)
        if self.initial_nav <= 0:
            raise ValueError("initial NAV must be positive")
        initial_wealth = self.F + self.C + self.R
        self.units = initial_wealth / self.initial_nav if initial_wealth else D0
        self.nav_hwm = self.initial_nav
        self.deferred_profit_lock = D0
        self.dividend_source_mode = dividend_source_mode
        if dividend_source_mode not in {"TOTAL_RETURN_EMBEDDED", "PRICE_PLUS_CASH"}:
            raise ValueError("invalid dividend source mode")
        self.audit_enabled = audit_enabled
        self.audit_log: list[dict] = []
        self.processed_events: dict[str, dict] = {}
        self.external_cashflows: list[tuple[datetime, Decimal, str]] = []
        if initial_wealth:
            self.external_cashflows.append((self.initial_at, -initial_wealth, "INITIAL_CONTRIBUTION"))
        self.cumulative_contributions = initial_wealth
        self.cumulative_withdrawals = D0
        self.cumulative_market_pnl = D0
        self.cumulative_cash_income = D0
        self.cumulative_fees = D0
        self.cumulative_tax = D0
        self.trade_count = 0
        self._validate()

    @property
    def pending_total(self) -> Decimal:
        return sum((lot.remaining_amount for lot in self.pending_lots if lot.status in PENDING_OPEN), D0)

    @property
    def unsettled_total(self) -> Decimal:
        return sum((lot.amount for lot in self.unsettled_lots if lot.status in UNSETTLED_OPEN), D0)

    @property
    def subscription_pending(self) -> Decimal:
        return sum((lot.amount for lot in self.unsettled_lots if lot.status == "PENDING_SUBSCRIPTION"), D0)

    @property
    def redemption_receivable(self) -> Decimal:
        return sum((lot.amount for lot in self.unsettled_lots if lot.status == "REDEMPTION_RECEIVABLE"), D0)

    @property
    def total_wealth(self) -> Decimal:
        return self.F + self.C + self.R + self.pending_total + self.unsettled_total - self.payable

    @property
    def nav(self) -> Decimal:
        if self.units <= EPS:
            return self.initial_nav
        return self.total_wealth / self.units

    @property
    def available_cash(self) -> Decimal:
        return self.C

    def available_pending(self, at: datetime | str) -> Decimal:
        now = dt(at)
        return sum((lot.remaining_amount for lot in self.pending_lots
                    if lot.status in PENDING_OPEN and lot.cleared_at <= now), D0)

    def withdrawable_total(self, at: datetime | str, fee_rate: Any = 0) -> Decimal:
        k = dec(fee_rate)
        return self.R + self.available_pending(at) + self.C + self.F * (D1 - k)

    def investable_assets_before_trade(self, eligible_pending: Decimal = D0) -> Decimal:
        return self.F + self.C + eligible_pending

    def state(self) -> dict[str, str]:
        return {
            "FundValue": str(self.F), "StrategyCash": str(self.C),
            "ReserveCash": str(self.R), "PendingCashTotal": str(self.pending_total),
            "SubscriptionPending": str(self.subscription_pending),
            "RedemptionReceivable": str(self.redemption_receivable),
            "UnsettledAssets": str(self.unsettled_total), "Payable": str(self.payable),
            "TotalWealth": str(self.total_wealth), "Units": str(self.units),
            "NAV": str(self.nav), "NAV_HWM": str(self.nav_hwm),
            "DeferredProfitLock": str(self.deferred_profit_lock),
        }

    def _validate(self) -> None:
        values = [self.F, self.C, self.R, self.payable, self.units,
                  self.deferred_profit_lock]
        values += [lot.remaining_amount for lot in self.pending_lots]
        values += [lot.amount for lot in self.unsettled_lots if lot.status in UNSETTLED_OPEN]
        if any(not value.is_finite() for value in values):
            raise ArithmeticError("non-finite state")
        if any(value < -EPS for value in values):
            raise ArithmeticError(f"negative state: {self.state()}")
        if self.total_wealth < -EPS:
            raise ArithmeticError("negative TotalWealth")
        if self.total_wealth > EPS and self.units <= EPS:
            raise ArithmeticError("positive wealth without units")

    def accounting_expected_wealth(self) -> Decimal:
        return (self.cumulative_contributions - self.cumulative_withdrawals
                + self.cumulative_market_pnl + self.cumulative_cash_income
                - self.cumulative_fees - self.cumulative_tax)

    def accounting_residual(self) -> Decimal:
        return self.total_wealth - self.accounting_expected_wealth()

    def _begin(self, event_id: str, event_type: str, timestamp: datetime | str) -> tuple[bool, datetime, dict]:
        when = dt(timestamp)
        before = self.state()
        if event_id in self.processed_events:
            result = {"status": "ALREADY_PROCESSED", "event_id": event_id,
                      "original_status": self.processed_events[event_id]["status"]}
            self._log(event_id, event_type, when, before, before, status="ALREADY_PROCESSED")
            return False, when, result
        if when < self.last_event_at:
            result = {"status": "REJECTED", "event_id": event_id,
                      "reason": "EVENT_OUT_OF_ORDER"}
            self.processed_events[event_id] = result
            self._log(event_id, event_type, when, before, before, status="REJECTED")
            return False, when, result
        self.last_event_at = when
        return True, when, before

    def _finish(self, event_id: str, event_type: str, when: datetime,
                before: dict, result: dict, external_flow: Decimal = D0,
                internal_transfer: str = "", trade: Decimal = D0,
                fee: Decimal = D0, settlement_change: str = "") -> dict:
        self._validate()
        result = {"status": "APPLIED", "event_id": event_id, **result}
        self.processed_events[event_id] = result
        self._log(event_id, event_type, when, before, self.state(), external_flow,
                  internal_transfer, trade, fee, settlement_change, "APPLIED")
        return result

    def _reject(self, event_id: str, event_type: str, when: datetime,
                before: dict, reason: str) -> dict:
        result = {"status": "REJECTED", "event_id": event_id, "reason": reason}
        self.processed_events[event_id] = result
        self._log(event_id, event_type, when, before, before, status="REJECTED")
        return result

    def _log(self, event_id: str, event_type: str, timestamp: datetime,
             before: dict, after: dict, external_flow: Decimal = D0,
             internal_transfer: str = "", trade: Decimal = D0,
             fee: Decimal = D0, settlement_change: str = "",
             status: str = "APPLIED") -> None:
        if not self.audit_enabled:
            return
        self.audit_log.append({
            "event_id": event_id, "event_type": event_type,
            "timestamp": timestamp.isoformat(), "before_state": before,
            "external_flow": str(external_flow),
            "internal_transfer": internal_transfer, "trade": str(trade),
            "fee": str(fee), "settlement_change": settlement_change,
            "after_state": after, "nav_before": before["NAV"],
            "nav_after": after["NAV"], "units_before": before["Units"],
            "units_after": after["Units"], "hwm_before": before["NAV_HWM"],
            "hwm_after": after["NAV_HWM"], "status": status,
        })

    def contribution(self, event_id: str, timestamp: datetime | str, amount: Any,
                     lot_id: str, cleared_at: datetime | str,
                     eligible_batch_id: str) -> dict:
        proceed, when, context = self._begin(event_id, "EXTERNAL_CONTRIBUTION", timestamp)
        if not proceed:
            return context
        before = context
        value = dec(amount)
        if value <= 0:
            return self._reject(event_id, "EXTERNAL_CONTRIBUTION", when, before, "AMOUNT_MUST_BE_POSITIVE")
        if any(lot.lot_id == lot_id for lot in self.pending_lots):
            return self._reject(event_id, "EXTERNAL_CONTRIBUTION", when, before, "DUPLICATE_LOT_ID")
        nav_before = self.nav
        if self.units <= EPS and self.total_wealth <= EPS:
            nav_before = self.initial_nav
        new_units = value / nav_before
        clear_time = dt(cleared_at)
        status = "CLEARED_NOT_ELIGIBLE" if clear_time <= when else "PENDING"
        self.pending_lots.append(PendingCashLot(
            lot_id, event_id, value, value, when, clear_time,
            eligible_batch_id, status, "EXTERNAL_CONTRIBUTION"))
        self.units += new_units
        self.cumulative_contributions += value
        self.external_cashflows.append((when, -value, "CONTRIBUTION"))
        result = self._finish(event_id, "EXTERNAL_CONTRIBUTION", when, before,
                              {"amount": str(value), "new_units": str(new_units),
                               "lot_id": lot_id}, external_flow=value)
        result["nav_unchanged"] = abs(self.nav - nav_before) <= EPS
        return result

    def initialize_account(
        self, event_id: str, timestamp: datetime | str, strategy_capital: Any,
        reserve_cash: Any, lot_id: str, cleared_at: datetime | str,
        eligible_batch_id: str, fund_value: Any = D0, strategy_cash: Any = D0,
    ) -> dict:
        """Create an initial account as one external flow, then allocate it internally.

        This is intentionally an engine event rather than a UI composition of
        several events: all initial capital receives units exactly once, while
        only the strategy portion becomes a PendingCash lot.
        """
        proceed, when, context = self._begin(event_id, "ACCOUNT_INITIALIZATION", timestamp)
        if not proceed:
            return context
        before = context
        strategy, reserve, fund, cash = map(dec, (strategy_capital, reserve_cash, fund_value, strategy_cash))
        if any(value < D0 for value in (strategy, reserve, fund, cash)):
            return self._reject(event_id, "ACCOUNT_INITIALIZATION", when, before, "NEGATIVE_INITIAL_BALANCE")
        if self.total_wealth > EPS or self.units > EPS or self.pending_lots or self.unsettled_lots:
            return self._reject(event_id, "ACCOUNT_INITIALIZATION", when, before, "ACCOUNT_ALREADY_INITIALIZED")
        total = strategy + reserve + fund + cash
        if total <= D0:
            return self._reject(event_id, "ACCOUNT_INITIALIZATION", when, before, "INITIAL_CAPITAL_MUST_BE_POSITIVE")
        clear_time = dt(cleared_at)
        self.F = fund
        self.C = cash
        self.R = reserve
        if strategy > D0:
            self.pending_lots.append(PendingCashLot(
                lot_id, event_id, strategy, strategy, when, clear_time, eligible_batch_id,
                "CLEARED_NOT_ELIGIBLE" if clear_time <= when else "PENDING", "INITIAL_CONTRIBUTION"))
        self.units = total / self.initial_nav
        self.cumulative_contributions = total
        self.external_cashflows = [(when, -total, "INITIAL_CONTRIBUTION")]
        self.nav_hwm = self.initial_nav
        result = self._finish(event_id, "ACCOUNT_INITIALIZATION", when, before, {
            "amount": str(total), "strategy_capital": str(strategy), "reserve_cash": str(reserve),
            "fund_value": str(fund), "strategy_cash": str(cash), "lot_id": lot_id if strategy else "",
            "new_units": str(self.units), "nav_unchanged": True,
        }, external_flow=total, internal_transfer="Initial capital -> F/C/Pending/Reserve")
        return result

    def freeze_pending(self, batch_id: str, cutoff: datetime | str) -> list[PendingCashLot]:
        cutoff_at = dt(cutoff)
        eligible = []
        for lot in self.pending_lots:
            if lot.status == "PENDING" and lot.cleared_at <= cutoff_at:
                lot.status = "CLEARED_NOT_ELIGIBLE"
            if (lot.status in {"CLEARED_NOT_ELIGIBLE", "ELIGIBLE"}
                    and lot.eligible_batch_id == batch_id and lot.cleared_at <= cutoff_at):
                lot.status = "ELIGIBLE"
                eligible.append(lot)
        return eligible

    def _withdraw_from_pending(self, amount: Decimal, at: datetime) -> tuple[Decimal, list[str]]:
        remaining = amount
        used = D0
        lots_used = []
        for lot in sorted(self.pending_lots, key=lambda x: (x.cleared_at, x.created_at, x.lot_id)):
            if remaining <= EPS:
                break
            if lot.status not in PENDING_OPEN or lot.cleared_at > at:
                continue
            take = min(remaining, lot.remaining_amount)
            lot.remaining_amount -= take
            remaining -= take
            used += take
            lots_used.append(lot.lot_id)
            if lot.remaining_amount <= EPS:
                lot.remaining_amount = D0
                lot.status = "WITHDRAWN"
        return used, lots_used

    def withdrawal(self, event_id: str, timestamp: datetime | str, amount: Any,
                   fee_rate: Any = 0, redemption_available_at: datetime | str | None = None) -> dict:
        proceed, when, context = self._begin(event_id, "EXTERNAL_WITHDRAWAL_REQUEST", timestamp)
        if not proceed:
            return context
        before = context
        value, k = dec(amount), dec(fee_rate)
        if value <= 0:
            return self._reject(event_id, "EXTERNAL_WITHDRAWAL_REQUEST", when, before, "AMOUNT_MUST_BE_POSITIVE")
        if k < 0 or k >= 1:
            return self._reject(event_id, "EXTERNAL_WITHDRAWAL_REQUEST", when, before, "INVALID_FEE")
        if value > self.withdrawable_total(when, k) + EPS:
            return self._reject(event_id, "EXTERNAL_WITHDRAWAL_REQUEST", when, before, "INSUFFICIENT_WITHDRAWABLE_ASSETS")
        nav_before = self.nav
        remaining = value
        from_r = min(remaining, self.R); self.R -= from_r; remaining -= from_r
        from_p, lot_ids = self._withdraw_from_pending(remaining, when); remaining -= from_p
        from_c = min(remaining, self.C); self.C -= from_c; remaining -= from_c
        immediate = from_r + from_p + from_c
        if immediate > 0:
            self.units -= immediate / nav_before
            self.cumulative_withdrawals += immediate
            self.external_cashflows.append((when, immediate, "WITHDRAWAL"))
        from_f_net = remaining
        gross_sale = fee = D0
        receivable_id = ""
        if from_f_net > EPS:
            gross_sale = from_f_net / (D1 - k)
            fee = gross_sale * k
            if gross_sale > self.F + EPS:
                return self._reject(event_id, "EXTERNAL_WITHDRAWAL_REQUEST", when, before, "FUND_SALE_EXCEEDS_FUND")
            self.F -= gross_sale
            self.cumulative_fees += fee
            available = dt(redemption_available_at) if redemption_available_at else when
            receivable_id = f"UNSET-{event_id}"
            self.unsettled_lots.append(UnsettledLot(
                receivable_id, event_id, "REDEMPTION_RECEIVABLE", from_f_net,
                when, available, "WITHDRAWAL", "REDEMPTION_RECEIVABLE"))
        result = self._finish(
            event_id, "EXTERNAL_WITHDRAWAL_REQUEST", when, before,
            {"requested": str(value), "withdraw_from_reserve": str(from_r),
             "withdraw_from_pending": str(from_p), "withdraw_from_strategy_cash": str(from_c),
             "withdraw_from_fund": str(from_f_net), "withdraw_from_receivable": "0",
             "pending_lots_used": lot_ids, "gross_fund_sale": str(gross_sale),
             "receivable_id": receivable_id, "immediate_external_withdrawal": str(immediate)},
            external_flow=-immediate, trade=-gross_sale, fee=fee,
            settlement_change=f"+withdrawal receivable {from_f_net}" if from_f_net else "")
        result["source_sum_matches"] = abs(from_r + from_p + from_c + from_f_net - value) <= EPS
        result["nav_unchanged_by_immediate_flow"] = True
        return result

    def internal_reserve_to_pending(self, event_id: str, timestamp: datetime | str,
                                    amount: Any, lot_id: str, cleared_at: datetime | str,
                                    eligible_batch_id: str) -> dict:
        proceed, when, context = self._begin(event_id, "INTERNAL_RESERVE_TO_PENDING", timestamp)
        if not proceed:
            return context
        before = context; value = dec(amount)
        if value <= 0 or value > self.R + EPS:
            return self._reject(event_id, "INTERNAL_RESERVE_TO_PENDING", when, before, "INVALID_INTERNAL_TRANSFER")
        self.R -= value
        clear_time = dt(cleared_at)
        self.pending_lots.append(PendingCashLot(
            lot_id, event_id, value, value, when, clear_time, eligible_batch_id,
            "CLEARED_NOT_ELIGIBLE" if clear_time <= when else "PENDING",
            "INTERNAL_TRANSFER"))
        return self._finish(event_id, "INTERNAL_RESERVE_TO_PENDING", when, before,
                            {"amount": str(value), "lot_id": lot_id},
                            internal_transfer="R->PendingAllocationCash")

    def apply_market_return(self, event_id: str, timestamp: datetime | str, fund_return: Any) -> dict:
        proceed, when, context = self._begin(event_id, "MARKET_RETURN", timestamp)
        if not proceed:
            return context
        before = context; rate = dec(fund_return)
        if rate < -1:
            return self._reject(event_id, "MARKET_RETURN", when, before, "RETURN_BELOW_MINUS_ONE")
        pnl = self.F * rate
        self.F += pnl
        self.cumulative_market_pnl += pnl
        return self._finish(event_id, "MARKET_RETURN", when, before,
                            {"fund_return": str(rate), "market_pnl": str(pnl)})

    def dividend(self, event_id: str, timestamp: datetime | str, amount: Any,
                 route: str, lot_id: str = "", cleared_at: datetime | str | None = None,
                 eligible_batch_id: str = "") -> dict:
        proceed, when, context = self._begin(event_id, "DIVIDEND", timestamp)
        if not proceed:
            return context
        before = context; value = dec(amount)
        if value <= 0:
            return self._reject(event_id, "DIVIDEND", when, before, "AMOUNT_MUST_BE_POSITIVE")
        if self.dividend_source_mode == "TOTAL_RETURN_EMBEDDED":
            return self._reject(event_id, "DIVIDEND", when, before, "DIVIDEND_ALREADY_EMBEDDED_DOUBLE_COUNT_BLOCKED")
        if route not in {"REINVEST", "STRATEGY_CASH", "PENDING", "RESERVE", "EXTERNAL"}:
            return self._reject(event_id, "DIVIDEND", when, before, "INVALID_DIVIDEND_ROUTE")
        self.cumulative_cash_income += value
        external = D0
        if route == "REINVEST":
            self.F += value
        elif route == "STRATEGY_CASH":
            self.C += value
        elif route == "RESERVE":
            self.R += value
        elif route == "PENDING":
            clear_time = dt(cleared_at or when)
            self.pending_lots.append(PendingCashLot(
                lot_id or f"LOT-{event_id}", event_id, value, value, when, clear_time,
                eligible_batch_id, "CLEARED_NOT_ELIGIBLE" if clear_time <= when else "PENDING",
                "INTERNAL_DIVIDEND"))
        else:
            self.C += value
            nav_after_income = self.nav
            self.C -= value
            self.units -= value / nav_after_income
            self.cumulative_withdrawals += value
            self.external_cashflows.append((when, value, "DIVIDEND_DISTRIBUTION"))
            external = -value
        return self._finish(event_id, "DIVIDEND", when, before,
                            {"amount": str(value), "route": route}, external_flow=external,
                            internal_transfer=f"DIVIDEND->{route}")

    def monthly_execute(
        self, event_id: str, timestamp: datetime | str, batch_id: str,
        cutoff: datetime | str, target_exposure: Any, fee_rate: Any,
        lock_ratio: Any, subscription_available_at: datetime | str,
        redemption_available_at: datetime | str,
    ) -> dict:
        proceed, when, context = self._begin(event_id, "MONTHLY_V2_EXECUTION", timestamp)
        if not proceed:
            return context
        before = context
        p, k, alpha = dec(target_exposure), dec(fee_rate), dec(lock_ratio)
        if not (D0 <= p <= D1 and D0 <= alpha <= D1 and D0 <= k < D1):
            return self._reject(event_id, "MONTHLY_V2_EXECUTION", when, before, "INVALID_PARAMETER")
        # Open settlement lots remain part of TotalWealth but are excluded from
        # the active trade denominator. They must not suspend a later market
        # signal or rebalance; settlement is an independent state transition.
        eligible_lots = self.freeze_pending(batch_id, cutoff)
        eligible_amount = sum((lot.remaining_amount for lot in eligible_lots), D0)
        for lot in eligible_lots:
            self.C += lot.remaining_amount
            lot.remaining_amount = D0
            lot.status = "MERGED"
        prelock_nav = self.nav
        hwm_before = self.nav_hwm
        economic_new_profit = max(D0, (prelock_nav - self.nav_hwm) * self.units)
        raw_lock = economic_new_profit * alpha
        total_lock_due = raw_lock + self.deferred_profit_lock
        active_before_lock = self.F + self.C
        # A lock is funded by the active domain, but any same-day sale also
        # consumes a fee. Find the largest lock amount for which the requested
        # target remains feasible. This matters at p=0/100% lock and non-zero
        # fees; otherwise a mathematically impossible lock could partially
        # mutate state before rejection.
        def feasible_lock(lock_amount: Decimal) -> bool:
            try:
                solve_target_after_fee(self.F, active_before_lock - lock_amount, p, k)
                return True
            except (ArithmeticError, ValueError):
                return False

        max_feasible_lock = active_before_lock
        if not feasible_lock(max_feasible_lock):
            low, high = D0, active_before_lock
            for _ in range(160):
                middle = (low + high) / 2
                if feasible_lock(middle):
                    low = middle
                else:
                    high = middle
            max_feasible_lock = low
        lock_executed = min(total_lock_due, max_feasible_lock)
        self.deferred_profit_lock = total_lock_due - lock_executed
        if prelock_nav > self.nav_hwm:
            # Frozen unique rule: pre-fee NAV becomes HWM. Fees then create a real
            # drawdown and recovery to this same NAV cannot lock the same band again.
            self.nav_hwm = prelock_nav
        active_after_lock = active_before_lock - lock_executed
        solved = solve_target_after_fee(self.F, active_after_lock, p, k)
        final_fund = solved["final_fund"]
        trade = solved["trade"]
        fee = solved["fee"]
        cash_to_reserve = min(self.C, lock_executed)
        reserve_receivable = lock_executed - cash_to_reserve
        self.R += cash_to_reserve
        settlement_changes = []
        if trade > EPS:
            buy = trade
            self.C = active_after_lock - fee - final_fund
            self.unsettled_lots.append(UnsettledLot(
                f"UNSET-SUB-{event_id}", event_id, "PENDING_SUBSCRIPTION", buy,
                when, dt(subscription_available_at), "STRATEGY", "PENDING_SUBSCRIPTION"))
            settlement_changes.append(f"subscription_pending={buy}")
        elif trade < -EPS:
            gross_sale = -trade
            net_sale = gross_sale - fee
            if reserve_receivable > net_sale + EPS:
                return self._reject(event_id, "MONTHLY_V2_EXECUTION", when, before, "LOCK_LIQUIDITY_INCONSISTENCY")
            strategy_receivable = net_sale - reserve_receivable
            self.F = final_fund
            self.C = max(D0, self.C - lock_executed)
            if reserve_receivable > EPS:
                self.unsettled_lots.append(UnsettledLot(
                    f"UNSET-RES-{event_id}", event_id, "REDEMPTION_RECEIVABLE",
                    reserve_receivable, when, dt(redemption_available_at), "RESERVE",
                    "REDEMPTION_RECEIVABLE"))
                settlement_changes.append(f"reserve_receivable={reserve_receivable}")
            if strategy_receivable > EPS:
                self.unsettled_lots.append(UnsettledLot(
                    f"UNSET-CASH-{event_id}", event_id, "REDEMPTION_RECEIVABLE",
                    strategy_receivable, when, dt(redemption_available_at), "STRATEGY",
                    "REDEMPTION_RECEIVABLE"))
                settlement_changes.append(f"strategy_receivable={strategy_receivable}")
        else:
            self.C -= lock_executed
            self.R += reserve_receivable
            reserve_receivable = D0
        self.cumulative_fees += fee
        if abs(trade) > EPS:
            self.trade_count += 1
        return self._finish(
            event_id, "MONTHLY_V2_EXECUTION", when, before,
            {"batch_id": batch_id, "eligible_pending": str(eligible_amount),
             "eligible_lot_ids": [lot.lot_id for lot in eligible_lots],
             "prelock_nav": str(prelock_nav), "hwm_before": str(hwm_before),
             "economic_new_profit": str(economic_new_profit),
             "profit_lock_raw": str(raw_lock), "profit_lock_executed": str(lock_executed),
             "deferred_profit_lock": str(self.deferred_profit_lock),
             "target_exposure": str(p), "direction": solved["direction"],
             "final_economic_fund": str(final_fund), "achieved_exposure": str(solved["achieved_exposure"]),
             "single_net_trade": True, "hwm_rule": "PRE_FEE_NAV"},
            internal_transfer=f"Peligible->C; Active->{lock_executed}->ReserveDomain",
            trade=trade, fee=fee, settlement_change=";".join(settlement_changes))

    def settle_due(self, event_id: str, timestamp: datetime | str,
                   subscription_values: dict[str, Any] | None = None) -> dict:
        proceed, when, context = self._begin(event_id, "SETTLEMENT", timestamp)
        if not proceed:
            return context
        before = context; subscription_values = subscription_values or {}
        settled_ids = []; external_paid = D0; settlement_pnl = D0
        for lot in self.unsettled_lots:
            if lot.status not in UNSETTLED_OPEN or lot.available_at > when:
                continue
            if lot.kind == "PENDING_SUBSCRIPTION":
                confirmed = dec(subscription_values.get(lot.lot_id, lot.amount))
                self.F += confirmed
                pnl = confirmed - lot.amount
                settlement_pnl += pnl
                self.cumulative_market_pnl += pnl
                lot.status = "FUND_SHARES_CONFIRMED"
            elif lot.purpose == "STRATEGY":
                self.C += lot.amount; lot.status = "REDEMPTION_CASH_AVAILABLE"
            elif lot.purpose == "RESERVE":
                self.R += lot.amount; lot.status = "REDEMPTION_CASH_AVAILABLE"
            else:
                nav_before = self.nav
                self.units -= lot.amount / nav_before
                self.cumulative_withdrawals += lot.amount
                self.external_cashflows.append((when, lot.amount, "WITHDRAWAL_SETTLEMENT"))
                external_paid += lot.amount
                lot.status = "WITHDRAWAL_PAID"
            settled_ids.append(lot.lot_id)
        return self._finish(event_id, "SETTLEMENT", when, before,
                            {"settled_lot_ids": settled_ids, "external_paid": str(external_paid),
                             "settlement_pnl": str(settlement_pnl)},
                            external_flow=-external_paid,
                            settlement_change=f"settled={','.join(settled_ids)}")

    def twr(self) -> Decimal:
        return self.nav / self.initial_nav - D1

    def xirr_flows(self, terminal_at: datetime | str) -> list[tuple[datetime, Decimal, str]]:
        flows = list(self.external_cashflows)
        flows.append((dt(terminal_at), self.total_wealth, "TERMINAL_VALUE"))
        return sorted(flows, key=lambda item: item[0])

    def xirr(self, terminal_at: datetime | str) -> float | None:
        flows = self.xirr_flows(terminal_at)
        if not any(amount < 0 for _, amount, _ in flows) or not any(amount > 0 for _, amount, _ in flows):
            return None
        base = flows[0][0]

        def npv(rate: float) -> float:
            return sum(float(amount) / ((1 + rate) ** (((when - base).total_seconds() / 86400) / 365.25))
                       for when, amount, _ in flows)

        low, high = -0.999999, 1.0
        while npv(low) * npv(high) > 0 and high < 1e6:
            high *= 2
        if npv(low) * npv(high) > 0:
            return None
        for _ in range(200):
            middle = (low + high) / 2
            value = npv(middle)
            if abs(value) < 1e-10:
                return middle
            if npv(low) * value <= 0:
                high = middle
            else:
                low = middle
        return (low + high) / 2

    def deterministic_fingerprint(self) -> str:
        pending = [
            {**asdict(lot), "amount": str(lot.amount), "remaining_amount": str(lot.remaining_amount),
             "created_at": lot.created_at.isoformat(), "cleared_at": lot.cleared_at.isoformat()}
            for lot in self.pending_lots
        ]
        unsettled = [
            {**asdict(lot), "amount": str(lot.amount), "created_at": lot.created_at.isoformat(),
             "available_at": lot.available_at.isoformat()}
            for lot in self.unsettled_lots
        ]
        return repr((self.state(), pending, unsettled, self.external_cashflows,
                     self.trade_count, self.accounting_residual()))
