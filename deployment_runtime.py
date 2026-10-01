"""Strategy-deployment isolation for protected Tradetron paper executors.

Every deployment owns a different protected credential file and signal outbox.
There is intentionally no fan-out, shared counter, or automatic delivery path.
"""
from pathlib import Path
from typing import Any

from tradetron_link import TradetronLinkState, TradetronLinkStore
from tradetron_runtime import TradetronSignalRuntime


MIGRATION_CONFIRMATION = "move-legacy-paper-link"


class DeploymentRuntimeRegistry:
    def __init__(self, store, root, legacy_link):
        self.store = store
        self.root = Path(root)
        self.legacy_link = legacy_link
        self._items: dict[str, tuple[TradetronLinkState, TradetronSignalRuntime]] = {}

    def _item(self, deployment_id: str):
        if deployment_id not in self._items:
            base = self.root / (self.store.path.stem + "-deployment-" + deployment_id)
            # Use the same Windows-protection provider as the legacy link.
            # Production therefore remains DPAPI-protected, while the local
            # test server can inject its non-user-profile test provider.
            protection = getattr(getattr(self.legacy_link, "store", None), "protection", None)
            link = TradetronLinkState(TradetronLinkStore(base.with_suffix(".tradetron.dpapi"), protection))
            runner = TradetronSignalRuntime(link, base.with_suffix(".tradetron-events.sqlite3"))
            self._items[deployment_id] = (link, runner)
        return self._items[deployment_id]

    def describe(self, deployment: dict[str, Any]) -> dict[str, Any]:
        link, runner = self._item(deployment["id"])
        result = dict(deployment)
        result["executor"] = link.status()
        result["runner"] = runner.status()
        return result

    def list(self):
        return [self.describe(item) for item in self.store.list_deployments()]

    def configure(self, deployment_id: str, payload: dict[str, Any]):
        self.store.get_deployment(deployment_id)
        link, _runner = self._item(deployment_id)
        link.configure(payload)
        self.store.set_deployment_state(deployment_id, "linked_unverified")
        return self.describe(self.store.get_deployment(deployment_id))

    def disconnect(self, deployment_id: str):
        self.store.get_deployment(deployment_id)
        link, runner = self._item(deployment_id)
        runner.disarm()
        link.clear()
        self.store.set_deployment_state(deployment_id, "not_linked")
        return self.describe(self.store.get_deployment(deployment_id))

    def migrate_legacy(self, deployment_id: str, confirmation: str):
        if confirmation != MIGRATION_CONFIRMATION:
            raise ValueError("Moving the legacy protected link requires its exact local confirmation.")
        deployment = self.store.get_deployment(deployment_id)
        if deployment["state"] != "not_linked":
            raise ValueError("Move the legacy link only into an unlinked paper deployment.")
        if not self.legacy_link.details:
            raise ValueError("There is no legacy protected Tradetron link to move.")
        link, _runner = self._item(deployment_id)
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
        link.message = "Paper executor linked. It has not yet passed an exact paper-delivery check."
        self.store.set_deployment_state(deployment_id, "linked_unverified")
        return self.describe(self.store.get_deployment(deployment_id))
