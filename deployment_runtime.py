"""Strategy-level isolation for protected Tradetron paper bridges."""
from pathlib import Path
from typing import Any

from tradetron_delivery import dispatch_snapshot_entries
from tradetron_link import TradetronLinkState, TradetronLinkStore
from tradetron_runtime import ARMING_CONFIRMATION, TradetronSignalRuntime
from live_strategy_runner import PaperStrategyRunner, RUNNER_CONFIRMATION


MIGRATION_CONFIRMATION = "move-legacy-paper-link"


class DeploymentRuntimeRegistry:
    def __init__(self, store, root, legacy_link):
        self.store = store
        self.root = Path(root)
        self.legacy_link = legacy_link
        self._items: dict[str, tuple[TradetronLinkState, TradetronSignalRuntime, PaperStrategyRunner]] = {}
        self.market = None

    def _item(self, deployment_id: str):
        if deployment_id not in self._items:
            base = self.root / (self.store.path.stem + "-deployment-" + deployment_id)
            # Use the same Windows-protection provider as the legacy link.
            # Production therefore remains DPAPI-protected, while the local
            # test server can inject its non-user-profile test provider.
            protection = getattr(getattr(self.legacy_link, "store", None), "protection", None)
            link = TradetronLinkState(TradetronLinkStore(base.with_suffix(".tradetron.dpapi"), protection))
            sender = TradetronSignalRuntime(link, base.with_suffix(".tradetron-events.sqlite3"))
            runner = PaperStrategyRunner(deployment_id, sender, base.with_suffix(".strategy-runner.sqlite3"))
            self._items[deployment_id] = (link, sender, runner)
        return self._items[deployment_id]

    def set_market(self, market) -> None:
        """Attach the read-only quote/candle adapter after local services start."""
        self.market = market

    def describe(self, deployment: dict[str, Any]) -> dict[str, Any]:
        link, sender, runner = self._item(deployment["id"])
        # `paper_ready` is set only after the guarded paper delivery succeeds.
        # The protected link is loaded afresh after a local-service restart, so
        # restore the delivery badge from that saved deployment state.
        if deployment.get("state") == "paper_ready" and link.details:
            link.mark_delivery_verified()
        result = dict(deployment)
        result["executor"] = link.status()
        result["runner"] = {**sender.status(), "automatic_runner": runner.status()["automatic_runner"],
                            "strategy": runner.status()}
        # The bridge deliberately carries no quantity or multiplier.  Every
        # event represents one base lot; Tradetron's deployment multiplier is
        # the only scaling authority for its Live Offline paper trades.
        result["bridge"] = {
            "signal_lots": 1,
            "tradetron_multiplier": "managed_in_tradetron",
            "sends_quantity": False,
        }
        return result

    def list(self):
        return [self.describe(item) for item in self.store.list_deployments()]

    def configure(self, deployment_id: str, payload: dict[str, Any]):
        self.store.get_deployment(deployment_id)
        link, sender, runner = self._item(deployment_id)
        runner.pause("Automatic paper signals were paused because the Tradetron link changed.")
        sender.disarm()
        link.configure(payload)
        self.store.set_deployment_state(deployment_id, "linked_unverified")
        return self.describe(self.store.get_deployment(deployment_id))

    def disconnect(self, deployment_id: str):
        self.store.get_deployment(deployment_id)
        link, sender, runner = self._item(deployment_id)
        runner.pause("Automatic paper signals were paused because the Tradetron bridge was disconnected.")
        sender.disarm()
        link.clear()
        self.store.set_deployment_state(deployment_id, "not_linked")
        return self.describe(self.store.get_deployment(deployment_id))

    def migrate_legacy(self, deployment_id: str, confirmation: str):
        if confirmation != MIGRATION_CONFIRMATION:
            raise ValueError("Moving the legacy protected link requires its exact local confirmation.")
        deployment = self.store.get_deployment(deployment_id)
        if deployment["state"] != "not_linked":
            raise ValueError("Move the older link only into an unlinked Tradetron bridge.")
        if not self.legacy_link.details:
            raise ValueError("There is no legacy protected Tradetron link to move.")
        link, _sender, runner = self._item(deployment_id)
        if link.details:
            raise ValueError("This deployment already has its own protected executor link.")
        # Save the destination first. If removal fails, best-effort cleanup
        # avoids silently leaving two independently usable copies.
        link.store.save(dict(self.legacy_link.details))
        try:
            self.legacy_link.clear()
        except Exception:
            try:
                link.clear()
            finally:
                raise ValueError("The legacy link could not be removed, so migration was cancelled.") from None
        link.details = link.store.load()
        link.saved, link.delivery = True, "unverified"
        link.message = "Tradetron bridge linked. It has not yet passed an exact delivery check."
        self.store.set_deployment_state(deployment_id, "linked_unverified")
        return self.describe(self.store.get_deployment(deployment_id))

    def paper_bootstrap(self, deployment_id: str, snapshot: dict[str, Any]):
        """Run the explicitly approved four-leg paper check for one deployment.

        The protected API link and its confirmation belong to this deployment;
        a linked strategy cannot cause a different deployment to receive values.
        This sends only fixed runtime values to Tradetron's Live Offline paper
        deployment and never calls a broker-order route.
        """
        deployment = self.store.get_deployment(deployment_id)
        link, sender, runner = self._item(deployment_id)
        if sender.armed or runner.enabled:
            raise ValueError("Disarm the paper signal runner before the one-time delivery check.")
        if not link.details or not link.saved:
            raise ValueError("Link this Tradetron bridge before the delivery check.")
        if link.details.get("strategy_id") != "999056854":
            raise ValueError("The delivery check is available only for the BANKNIFTY Portal executor paper strategy.")
        result = dispatch_snapshot_entries(link.details, snapshot)
        link.mark_delivery_verified()
        self.store.set_deployment_state(deployment_id, "paper_ready")
        return {"delivery": result, "deployment": self.describe(self.store.get_deployment(deployment_id))}

    def start_runner(self, deployment_id: str, confirmation: str):
        """Arm a reviewed paper-only bridge; no broker order path exists here."""
        deployment = self.store.get_deployment(deployment_id)
        if deployment["state"] != "paper_ready":
            raise ValueError("Run and review the one-time Tradetron paper delivery before starting automatic paper signals.")
        if confirmation != RUNNER_CONFIRMATION:
            raise ValueError("Starting automatic paper signals requires its exact local confirmation.")
        if self.market is None:
            raise ValueError("The local price service is unavailable. Start it during a verified market session first.")
        self.market.strategy_runner_ready()
        _link, sender, runner = self._item(deployment_id)
        sender.arm(ARMING_CONFIRMATION)
        try:
            runner.start(confirmation)
        except Exception:
            sender.disarm()
            raise
        self.store.set_deployment_state(deployment_id, "paper_running")
        return self.describe(self.store.get_deployment(deployment_id))

    def pause_runner(self, deployment_id: str):
        deployment = self.store.get_deployment(deployment_id)
        _link, sender, runner = self._item(deployment_id)
        runner.pause()
        sender.disarm()
        if deployment["state"] == "paper_running":
            self.store.set_deployment_state(deployment_id, "paused")
        return self.describe(self.store.get_deployment(deployment_id))

    def confirm_runner_event(self, deployment_id: str, event_id: str):
        deployment = self.store.get_deployment(deployment_id)
        _link, _sender, runner = self._item(deployment_id)
        runner.confirm(event_id)
        return self.describe(self.store.get_deployment(deployment_id))

    def observe_spot(self, quote: dict[str, Any], market) -> None:
        """Fan the same fresh spot observation to independently linked bridges.

        The market feed is read-only.  A blocked runner is retained for review;
        no later signal is silently sent for that deployment.
        """
        self.market = market
        for deployment in self.store.list_deployments():
            if deployment.get("state") != "paper_running":
                continue
            _link, _sender, runner = self._item(deployment["id"])
            status = runner.observe(market, quote)
            if status.get("blocked"):
                self.store.set_deployment_state(deployment["id"], "blocked")
