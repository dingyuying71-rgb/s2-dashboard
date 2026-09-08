"""Versioned V4 E3+A3 account reducer.

V3 remains in account_engine.py and is intentionally not modified. V4 adds
actual-order, confirmation, settlement and pending-cancellation events while
reusing the frozen Decimal accounting identity and unitized-NAV primitives.
"""
from __future__ import annotations

import copy
from datetime import datetime
from decimal import Decimal
from typing import Any

try:
    from .account_engine import (
        AccountEngine, PendingCashLot, UnsettledLot, D0, D1, EPS, dec, dt,
    )
except ImportError:  # direct import by the frozen/local runners
    from account_engine import (
        AccountEngine, PendingCashLot, UnsettledLot, D0, D1, EPS, dec, dt,
    )

V4_PENDING_OPEN = {"PENDING", "CLEARED_NOT_ELIGIBLE", "PARTIALLY_CANCELLED", "ELIGIBLE"}
V4_CANCELLABLE = {"PENDING", "CLEARED_NOT_ELIGIBLE", "PARTIALLY_CANCELLED"}
V4_UNSETTLED_OPEN = {"PENDING_SUBSCRIPTION", "REDEMPTION_RECEIVABLE", "V4_PENDING_SUBSCRIPTION", "V4_REDEMPTION_RECEIVABLE"}


class AccountEngineV4(AccountEngine):
    contract_version = "S2-account-v4.0-frozen"

    def __init__(self, fund_value: Any, strategy_cash: Any, reserve_cash: Any,
                 initial_at: datetime | str, initial_nav: Any = "1.000000",
                 dividend_source_mode: str = "TOTAL_RETURN_EMBEDDED",
                 audit_enabled: bool = True, fund_shares: Any = 0) -> None:
        self.fund_shares = dec(fund_shares)
        self.v4_orders: dict[str, dict] = {}
        self.signal_executions: dict[str, str] = {}
        super().__init__(fund_value, strategy_cash, reserve_cash, initial_at,
                         initial_nav, dividend_source_mode, audit_enabled)

    @classmethod
    def from_v3(cls, engine: AccountEngine, fund_shares: Any = 0) -> "AccountEngineV4":
        if isinstance(engine, cls):
            engine._ensure_v4()
            return engine
        upgraded = cls.__new__(cls)
        upgraded.__dict__ = copy.deepcopy(engine.__dict__)
        upgraded.fund_shares = dec(fund_shares)
        upgraded.v4_orders = {}
        upgraded.signal_executions = {}
        upgraded._ensure_v4()
        upgraded._validate()
        return upgraded

    def _ensure_v4(self) -> None:
        if not hasattr(self, "fund_shares"): self.fund_shares = D0
        if not hasattr(self, "v4_orders"): self.v4_orders = {}
        if not hasattr(self, "signal_executions"): self.signal_executions = {}

    @property
    def pending_total(self) -> Decimal:
        return sum((lot.remaining_amount for lot in self.pending_lots if lot.status in V4_PENDING_OPEN), D0)

    @property
    def unsettled_total(self) -> Decimal:
        return sum((lot.amount for lot in self.unsettled_lots if lot.status in V4_UNSETTLED_OPEN), D0)

    @property
    def subscription_pending(self) -> Decimal:
        return sum((lot.amount for lot in self.unsettled_lots if lot.status in {"PENDING_SUBSCRIPTION", "V4_PENDING_SUBSCRIPTION"}), D0)

    @property
    def redemption_receivable(self) -> Decimal:
        return sum((lot.amount for lot in self.unsettled_lots if lot.status in {"REDEMPTION_RECEIVABLE", "V4_REDEMPTION_RECEIVABLE"}), D0)

    @property
    def reserved_redemption_units(self) -> Decimal:
        return sum((dec(order.get("redemption_units", 0)) for order in self.v4_orders.values()
                    if order.get("side") == "SELL" and order.get("status") == "PENDING_CONFIRMATION"), D0)

    @property
    def available_redeemable_units(self) -> Decimal:
        return max(D0, self.fund_shares - self.reserved_redemption_units)

    def state(self) -> dict[str, str]:
        state = super().state()
        state.update({
            "FundShares": str(self.fund_shares),
            "ReservedRedemptionUnits": str(self.reserved_redemption_units),
            "AvailableRedeemableUnits": str(self.available_redeemable_units),
            "AccountContractVersion": self.contract_version,
        })
        return state

    def _validate(self) -> None:
        super()._validate()
        self._ensure_v4()
        if self.fund_shares < -EPS or self.reserved_redemption_units < -EPS:
            raise ArithmeticError("negative fund units")
        if self.reserved_redemption_units > self.fund_shares + EPS:
            raise ArithmeticError("reserved redemption units exceed holdings")

    def available_pending(self, at: datetime | str) -> Decimal:
        when = dt(at)
        return sum((lot.remaining_amount for lot in self.pending_lots
                    if lot.status in V4_PENDING_OPEN and lot.cleared_at <= when), D0)

    def _withdraw_from_pending(self, amount: Decimal, at: datetime) -> tuple[Decimal, list[str]]:
        """Keep the frozen withdrawal ordering while recognizing V4 partial lots."""
        remaining=amount;used=D0;lots_used=[]
        for lot in sorted(self.pending_lots,key=lambda x:(x.cleared_at,x.created_at,x.lot_id)):
            if remaining<=EPS:break
            if lot.status not in V4_PENDING_OPEN or lot.cleared_at>at:continue
            take=min(remaining,lot.remaining_amount);lot.remaining_amount-=take;remaining-=take;used+=take;lots_used.append(lot.lot_id)
            if lot.remaining_amount<=EPS:lot.remaining_amount=D0;lot.status="WITHDRAWN"
        return used,lots_used

    def freeze_pending(self, batch_id: str, cutoff: datetime | str) -> list[PendingCashLot]:
        cutoff_at = dt(cutoff); eligible = []
        for lot in self.pending_lots:
            if lot.status == "PENDING" and lot.cleared_at <= cutoff_at:
                lot.status = "CLEARED_NOT_ELIGIBLE"
            if (lot.status in {"CLEARED_NOT_ELIGIBLE", "PARTIALLY_CANCELLED", "ELIGIBLE"}
                    and lot.eligible_batch_id == batch_id and lot.cleared_at <= cutoff_at):
                lot.status = "ELIGIBLE"
                eligible.append(lot)
        return eligible

    def _find_pending(self, lot_id: str) -> PendingCashLot | None:
        return next((lot for lot in self.pending_lots if lot.lot_id == lot_id), None)

    def pending_cancellation(self, event_id: str, timestamp: datetime | str,
                             lot_id: str, amount: Any, reason: str = "") -> dict:
        if event_id in self.processed_events:
            return {"status":"ALREADY_PROCESSED", "event_id":event_id,
                    "original_status":self.processed_events[event_id]["status"]}
        value = dec(amount); lot = self._find_pending(lot_id)
        if value <= D0:
            return {"status":"REJECTED", "event_id":event_id, "reason":"INVALID_CANCELLATION_AMOUNT"}
        if lot is None:
            return {"status":"REJECTED", "event_id":event_id, "reason":"PENDING_LOT_NOT_FOUND"}
        if lot.status not in V4_CANCELLABLE:
            return {"status":"REJECTED", "event_id":event_id, "reason":"PENDING_NOT_CANCELLABLE"}
        if value > lot.remaining_amount + EPS:
            return {"status":"REJECTED", "event_id":event_id, "reason":"CANCELLATION_EXCEEDS_REMAINING"}
        proceed, when, context = self._begin(event_id, "PENDING_CANCELLATION", timestamp)
        if not proceed: return context
        before = context; nav_before = self.nav; hwm_before = self.nav_hwm
        units_cancelled = value / nav_before
        lot.remaining_amount -= value
        if lot.remaining_amount <= EPS:
            lot.remaining_amount = D0; lot.status = "CANCELLED"
        else:
            lot.status = "PARTIALLY_CANCELLED"
        self.units -= units_cancelled
        if abs(self.units) <= EPS: self.units = D0
        self.cumulative_withdrawals += value
        self.external_cashflows.append((when, value, "PENDING_CANCELLATION"))
        result = self._finish(event_id, "PENDING_CANCELLATION", when, before, {
            "original_pending_lot_id":lot_id, "original_contribution_event_id":lot.event_id,
            "amount":str(value), "destination":"EXTERNAL", "reason_text":reason,
            "nav_before":str(nav_before), "units_cancelled":str(units_cancelled),
            "remaining_pending_amount":str(lot.remaining_amount), "pending_status":lot.status,
        }, external_flow=-value, internal_transfer="Pending->ExternalAccount")
        result["nav_unchanged"] = abs(self.nav-nav_before) <= EPS
        result["hwm_unchanged"] = self.nav_hwm == hwm_before
        return result

    def pending_to_reserve(self, event_id: str, timestamp: datetime | str,
                           lot_id: str, amount: Any, reason: str = "") -> dict:
        if event_id in self.processed_events:
            return {"status":"ALREADY_PROCESSED", "event_id":event_id,
                    "original_status":self.processed_events[event_id]["status"]}
        value = dec(amount); lot = self._find_pending(lot_id)
        if value <= D0:
            return {"status":"REJECTED", "event_id":event_id, "reason":"INVALID_TRANSFER_AMOUNT"}
        if lot is None:
            return {"status":"REJECTED", "event_id":event_id, "reason":"PENDING_LOT_NOT_FOUND"}
        if lot.status not in V4_CANCELLABLE:
            return {"status":"REJECTED", "event_id":event_id, "reason":"PENDING_NOT_TRANSFERABLE"}
        if value > lot.remaining_amount + EPS:
            return {"status":"REJECTED", "event_id":event_id, "reason":"TRANSFER_EXCEEDS_REMAINING"}
        proceed, when, context = self._begin(event_id, "PENDING_TO_RESERVE_TRANSFER", timestamp)
        if not proceed:return context
        before=context; nav_before=self.nav; units_before=self.units; hwm_before=self.nav_hwm
        lot.remaining_amount -= value; self.R += value
        if lot.remaining_amount <= EPS:
            lot.remaining_amount=D0;lot.status="CONSUMED"
        else:lot.status="PARTIALLY_CANCELLED"
        result=self._finish(event_id,"PENDING_TO_RESERVE_TRANSFER",when,before,{
            "original_pending_lot_id":lot_id,"original_contribution_event_id":lot.event_id,
            "amount":str(value),"destination":"RESERVE","reason_text":reason,
            "remaining_pending_amount":str(lot.remaining_amount),"pending_status":lot.status,
        },internal_transfer="Pending->Reserve")
        result.update({"wealth_unchanged":self.total_wealth==dec(before["TotalWealth"]),
                       "units_unchanged":self.units==units_before,"nav_unchanged":abs(self.nav-nav_before)<=EPS,
                       "hwm_unchanged":self.nav_hwm==hwm_before})
        return result

    def report_execution(self, event_id: str, timestamp: datetime | str,
                         signal_id: str, recommendation_id: str,
                         recommended_action: str, recommended_amount: Any,
                         actual_action: str, fund_code: str,
                         cash_debited: Any | None = None,
                         fee: Any | None = None,
                         redemption_units: Any | None = None,
                         estimated_redemption_amount: Any | None = None,
                         note: str = "") -> dict:
        self._ensure_v4()
        if event_id in self.processed_events:
            return {"status":"ALREADY_PROCESSED","event_id":event_id,
                    "original_status":self.processed_events[event_id]["status"]}
        rec_action=recommended_action.upper(); side=actual_action.upper(); recommended=dec(recommended_amount)
        if side != rec_action:
            return {"status":"REJECTED","event_id":event_id,"reason":"ACTION_MISMATCH"}
        if signal_id in self.signal_executions:
            return {"status":"ALREADY_PROCESSED","event_id":event_id,
                    "reason":"SIGNAL_ALREADY_EXECUTED","original_event_id":self.signal_executions[signal_id]}
        actual = dec(cash_debited) if side=="BUY" and cash_debited is not None else dec(redemption_units) if side=="SELL" and redemption_units is not None else D0
        if actual <= D0:
            return {"status":"REJECTED","event_id":event_id,"reason":"INVALID_ACTUAL_AMOUNT"}
        known_fee = None if fee is None else dec(fee)
        if known_fee is not None and (known_fee < D0 or known_fee > actual):
            return {"status":"REJECTED","event_id":event_id,"reason":"INVALID_FEE"}
        if side=="BUY" and actual > self.C + EPS:
            return {"status":"REJECTED","event_id":event_id,"reason":"INSUFFICIENT_STRATEGY_CASH"}
        if side=="SELL" and actual > self.available_redeemable_units + EPS:
            return {"status":"REJECTED","event_id":event_id,"reason":"INSUFFICIENT_REDEEMABLE_UNITS"}
        proceed,when,context=self._begin(event_id,"EXECUTION_REPORTED",timestamp)
        if not proceed:return context
        before=context; deviation=(dec(cash_debited) if side=="BUY" else dec(estimated_redemption_amount or 0))-recommended
        order={
            "event_id":event_id,"contract_version":self.contract_version,"schema_version":"4.1",
            "signal_id":signal_id,"recommendation_id":recommendation_id,"fund_code":fund_code,
            "side":side,"recommended_action":rec_action,"recommended_amount":str(recommended),
            "cash_debited":str(dec(cash_debited)) if cash_debited is not None else None,
            "redemption_units":str(dec(redemption_units)) if redemption_units is not None else None,
            "estimated_redemption_amount":str(dec(estimated_redemption_amount)) if estimated_redemption_amount is not None else None,
            "execution_deviation":str(deviation),"reported_at":when.isoformat(),"status":"PENDING_CONFIRMATION",
            "fee_status":"UNKNOWN" if known_fee is None else "REPORTED","reported_fee":None if known_fee is None else str(known_fee),"note":note,
        }
        if side=="BUY":
            principal=actual-(known_fee or D0);self.C-=actual;self.cumulative_fees+=(known_fee or D0)
            self.unsettled_lots.append(UnsettledLot(f"V4-SUB-{event_id}",event_id,"V4_PENDING_SUBSCRIPTION",principal,when,when,"V4_EXECUTION_BUY","V4_PENDING_SUBSCRIPTION"))
            order["provisional_principal"]=str(principal)
        self.v4_orders[event_id]=order;self.signal_executions[signal_id]=event_id
        return self._finish(event_id,"EXECUTION_REPORTED",when,before,{
            "signal_id":signal_id,"recommendation_id":recommendation_id,"recommended_action":rec_action,
            "recommended_amount":str(recommended),"actual_action":side,"cash_debited":order["cash_debited"],
            "redemption_units":order["redemption_units"],"estimated_redemption_amount":order["estimated_redemption_amount"],
            "execution_deviation":str(deviation),"fee_status":order["fee_status"],"status_detail":"PENDING_CONFIRMATION",
        },internal_transfer="C->SubscriptionPending" if side=="BUY" else "Reserve redemption units")

    def confirm_order(self, event_id: str, timestamp: datetime | str,
                      execution_event_id: str, confirmed_nav: Any,
                      confirmed_units: Any, confirmed_principal: Any,
                      confirmed_fee: Any, confirmed_value: Any,
                      platform_reference: str = "") -> dict:
        if event_id in self.processed_events:
            return {"status":"ALREADY_PROCESSED","event_id":event_id,
                    "original_status":self.processed_events[event_id]["status"]}
        order=self.v4_orders.get(execution_event_id)
        if not order:return {"status":"REJECTED","event_id":event_id,"reason":"EXECUTION_NOT_FOUND"}
        if order["status"]!="PENDING_CONFIRMATION":return {"status":"REJECTED","event_id":event_id,"reason":"ORDER_NOT_CONFIRMABLE"}
        nav,units,principal,fee,value=map(dec,(confirmed_nav,confirmed_units,confirmed_principal,confirmed_fee,confirmed_value))
        if nav<=D0 or units<=D0 or principal< D0 or fee< D0 or value< D0:
            return {"status":"REJECTED","event_id":event_id,"reason":"INVALID_CONFIRMATION"}
        if order["side"]=="BUY":
            cash=dec(order["cash_debited"])
            if abs(principal+fee-cash)>Decimal("0.01"):
                return {"status":"REJECTED","event_id":event_id,"reason":"BUY_CONFIRMATION_DOES_NOT_MATCH_CASH_DEBIT"}
            reported_fee=dec(order["reported_fee"] or 0)
            if fee < reported_fee-EPS:
                return {"status":"REJECTED","event_id":event_id,"reason":"CONFIRMED_FEE_BELOW_REPORTED_FEE"}
            lot=next((x for x in self.unsettled_lots if x.event_id==execution_event_id and x.status=="V4_PENDING_SUBSCRIPTION"),None)
            if not lot:
                return {"status":"REJECTED","event_id":event_id,"reason":"SUBSCRIPTION_PENDING_NOT_FOUND"}
            if order["fee_status"]=="UNKNOWN" and abs(lot.amount-(principal+fee))>Decimal("0.01"):
                return {"status":"REJECTED","event_id":event_id,"reason":"PROVISIONAL_SUBSCRIPTION_MISMATCH"}
        else:
            requested=dec(order["redemption_units"])
            if units>requested+EPS or units>self.fund_shares+EPS:
                return {"status":"REJECTED","event_id":event_id,"reason":"CONFIRMED_UNITS_EXCEED_ORDER"}
            if value>principal+EPS or abs(principal-value-fee)>Decimal("0.01"):
                return {"status":"REJECTED","event_id":event_id,"reason":"SELL_CONFIRMATION_VALUE_MISMATCH"}
        proceed,when,context=self._begin(event_id,"FUND_ORDER_CONFIRMED",timestamp)
        if not proceed:return context
        before=context
        if order["side"]=="BUY":
            provisional=lot.amount;lot.status="FUND_SHARES_CONFIRMED";self.F+=value;self.fund_shares+=units
            extra_fee=fee-reported_fee
            self.cumulative_fees+=max(D0,extra_fee)
            settlement_pnl=value-principal;self.cumulative_market_pnl+=settlement_pnl
        else:
            self.F-=principal;self.fund_shares-=units;self.cumulative_fees+=fee
            self.unsettled_lots.append(UnsettledLot(f"V4-REC-{execution_event_id}",execution_event_id,"V4_REDEMPTION_RECEIVABLE",value,when,when,"V4_EXECUTION_SELL","V4_REDEMPTION_RECEIVABLE"))
            settlement_pnl=D0
        order.update({"status":"CONFIRMED" if order["side"]=="BUY" else "WAITING_SETTLEMENT",
                      "confirmation_event_id":event_id,"confirmed_at":when.isoformat(),"confirmed_nav":str(nav),
                      "confirmed_units":str(units),"confirmed_principal":str(principal),"confirmed_fee":str(fee),
                      "confirmed_value":str(value),"platform_reference":platform_reference})
        return self._finish(event_id,"FUND_ORDER_CONFIRMED",when,before,{
            "execution_event_id":execution_event_id,"side":order["side"],"confirmed_nav":str(nav),
            "confirmed_units":str(units),"confirmed_principal":str(principal),"confirmed_fee":str(fee),
            "confirmed_value":str(value),"execution_status":order["status"],"settlement_pnl":str(settlement_pnl),
        },fee=fee if order["side"]=="SELL" else max(D0,fee-dec(order["reported_fee"] or 0)),
          settlement_change="SubscriptionPending->Fund" if order["side"]=="BUY" else "Fund->RedemptionReceivable")

    def settle_execution(self, event_id: str, timestamp: datetime | str,
                         execution_event_id: str, destination: str = "STRATEGY_CASH") -> dict:
        if event_id in self.processed_events:
            return {"status":"ALREADY_PROCESSED","event_id":event_id,
                    "original_status":self.processed_events[event_id]["status"]}
        order=self.v4_orders.get(execution_event_id)
        if not order:return {"status":"REJECTED","event_id":event_id,"reason":"EXECUTION_NOT_FOUND"}
        if destination not in {"STRATEGY_CASH"}:
            return {"status":"REJECTED","event_id":event_id,"reason":"UNSUPPORTED_SETTLEMENT_DESTINATION"}
        if order["status"] not in {"CONFIRMED","WAITING_SETTLEMENT"}:
            return {"status":"REJECTED","event_id":event_id,"reason":"ORDER_NOT_SETTLEABLE"}
        proceed,when,context=self._begin(event_id,"SETTLEMENT",timestamp)
        if not proceed:return context
        before=context;settled=D0
        if order["side"]=="SELL":
            lot=next((x for x in self.unsettled_lots if x.event_id==execution_event_id and x.status=="V4_REDEMPTION_RECEIVABLE"),None)
            if not lot:return self._reject(event_id,"SETTLEMENT",when,before,"RECEIVABLE_NOT_FOUND")
            settled=lot.amount;self.C+=settled;lot.status="REDEMPTION_CASH_AVAILABLE"
        order["status"]="SETTLED";order["settlement_event_id"]=event_id;order["settled_at"]=when.isoformat();order["settled_cash_amount"]=str(settled)
        return self._finish(event_id,"SETTLEMENT",when,before,{
            "execution_event_id":execution_event_id,"destination":destination,"settled_cash_amount":str(settled),"execution_status":"SETTLED",
        },settlement_change="RedemptionReceivable->StrategyCash" if settled else "BUY confirmation completion")
