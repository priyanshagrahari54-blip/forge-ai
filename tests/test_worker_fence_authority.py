from types import SimpleNamespace

from forge.server.workers import _may_publish


class _Queue:
    def lease_held_by(self, task_id, owner):
        return True


class _FenceRegistry:
    def is_authorized(self, fence):
        return True


def test_task_bound_publish_requires_fence_authority():
    server = SimpleNamespace(queue=_Queue(), fences=None)
    assert _may_publish(server, "task", "owner", object()) is False


def test_task_bound_publish_requires_live_authorized_fence():
    server = SimpleNamespace(queue=_Queue(), fences=_FenceRegistry())
    assert _may_publish(server, "task", "owner", object()) is True
